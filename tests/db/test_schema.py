from datetime import date, time
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from src.db.models import EtlRun, ProcessedCurvePoint, RawCurvePoint, StressShock


def insert_run(db, **overrides):
    values = {
        "run_type": "manual",
        "date_from": date(2026, 10, 19),
        "date_to": date(2026, 10, 19),
        **overrides,
    }
    return db.execute(
        EtlRun.__table__.insert().values(**values).returning(EtlRun.id)
    ).scalar_one()


def insert_shock(db, version="test-v1"):
    return db.execute(
        StressShock.__table__.insert()
        .values(
            scenario="Parallel_Up",
            version=version,
            parameters={
                "formula": "parallel",
                "shift": "0.04",
                "rate_unit": "fraction",
            },
        )
        .returning(StressShock.id)
    ).scalar_one()


def insert_point(db, run_id, **overrides):
    values = {
        "curve_date": date(2026, 10, 19),
        "quote_time": time(18, 50),
        "tenor": Decimal(1),
        "yield_rate": Decimal("0.15"),
        "discount_factor": Decimal("0.869565217391304348"),
        "scenario": "Baseline",
        "scenario_version": None,
        "etl_run_id": run_id,
        **overrides,
    }
    return db.execute(
        ProcessedCurvePoint.__table__.insert()
        .values(**values)
        .returning(ProcessedCurvePoint.id)
    ).scalar_one()


def insert_raw(db, run_id, **overrides):
    return db.execute(
        RawCurvePoint.__table__.insert()
        .values(
            curve_date=date(2026, 10, 19),
            quote_time=time(18, 50),
            **{
                "tenor": Decimal(1),
                "yield_rate": Decimal("0.15"),
                "etl_run_id": run_id,
                **overrides,
            },
        )
        .returning(RawCurvePoint.id)
    ).scalar_one()


def test_schema_migrates_existing_data_and_round_trips(migrated_db):
    engine, config = migrated_db
    command.check(config)
    command.downgrade(config, "0003_remove_discount_convention")
    with engine.begin() as connection:
        run_id = insert_run(connection)
        shock_id = insert_shock(connection)
        for tenor, interpolated, extrapolated in (
            (1, False, False),
            (1.5, True, False),
            (30, False, True),
        ):
            point_id = connection.scalar(
                text(
                    "INSERT INTO curve_points (curve_date, quote_time, tenor, yield_rate, discount_factor, "
                    "etl_run_id, is_interpolated, is_extrapolated) "
                    "VALUES ('2026-10-19', '18:50', :tenor, 0.15, 0.86, :run, :i, :e) RETURNING id"
                ),
                {"tenor": tenor, "run": run_id, "i": interpolated, "e": extrapolated},
            )
            connection.execute(
                text(
                    "INSERT INTO stress_curves (curve_point_id, stress_shock_id, stressed_yield, "
                    "discount_factor, etl_run_id) VALUES (:point, :shock, 0.19, 0.84, :run)"
                ),
                {"point": point_id, "shock": shock_id, "run": run_id},
            )
    command.upgrade(config, "head")
    command.check(config)
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version",
        "etl_runs",
        "stress_shocks",
        "raw_curve_points",
        "processed_curve_points",
    }
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(RawCurvePoint)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(ProcessedCurvePoint))
            == 6
        )
        assert connection.scalar(select(RawCurvePoint.yield_rate)) == Decimal("0.15")
        stress = (
            connection.execute(
                select(ProcessedCurvePoint.__table__).where(
                    ProcessedCurvePoint.scenario == "Parallel_Up",
                    ProcessedCurvePoint.tenor == 30,
                )
            )
            .mappings()
            .one()
        )
        assert stress["is_extrapolated"]
        assert stress["scenario_version"] == "test-v1"
        assert stress["yield_rate"] == Decimal("0.19")
        assert stress["discount_factor"] == Decimal("0.84")
        assert stress["etl_run_id"] == run_id
        assert stress["created_at"].utcoffset() is not None
    # Refuse a downgrade that would silently discard raw-only failed snapshots.
    with engine.begin() as connection:
        extra = insert_raw(connection, run_id, tenor=2)
    with pytest.raises(
        DBAPIError, match="raw observations without a matching baseline"
    ):
        command.downgrade(config, "0003_remove_discount_convention")
    assert "raw_curve_points" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        connection.execute(
            RawCurvePoint.__table__.delete().where(RawCurvePoint.id == extra)
        )
    command.downgrade(config, "0003_remove_discount_convention")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM curve_points")) == 3
        assert connection.scalar(text("SELECT count(*) FROM stress_curves")) == 3
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    command.upgrade(config, "head")
    command.check(config)


def test_raw_points_preserve_precision_and_have_no_synthetic_fields(db):
    run_id = insert_run(db)
    value = Decimal("0.12345678901234567890123456789012345678")
    point_id = insert_raw(db, run_id, yield_rate=value)
    assert (
        db.scalar(select(RawCurvePoint.yield_rate).where(RawCurvePoint.id == point_id))
        == value
    )
    assert "discount_factor" not in RawCurvePoint.__table__.c
    assert "is_interpolated" not in RawCurvePoint.__table__.c
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_raw(db, run_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"yield_rate": None},
        {"yield_rate": Decimal("NaN")},
        {"yield_rate": Decimal("Infinity")},
        {"tenor": 0},
        {"etl_run_id": uuid4()},
    ],
)
def test_invalid_raw_points_rejected(db, changes):
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_raw(db, insert_run(db), **changes)


def test_baseline_and_two_versions_share_coordinates_without_duplicates(db):
    run_id = insert_run(db)
    insert_point(db, run_id)
    for version in ("test-v1", "test-v2"):
        insert_shock(db, version)
        insert_point(db, run_id, scenario="Parallel_Up", scenario_version=version)
    assert db.scalar(select(func.count()).select_from(ProcessedCurvePoint)) == 3
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id)  # NULL version must not allow duplicate baselines.
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id, scenario="Parallel_Up", scenario_version="test-v1")
    insert_point(db, run_id, quote_time=time(19))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(StressShock.__table__.delete())
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(EtlRun.__table__.delete())


@pytest.mark.parametrize(
    "changes",
    [
        {"scenario": "unknown"},
        {"scenario_version": "v1"},
        {"scenario": "Parallel_Up"},
        {"scenario": "Parallel_Up", "scenario_version": "missing"},
        {"scenario": "Short_Up", "scenario_version": "test-v1"},
        {"tenor": 0},
        {"tenor": Decimal("NaN")},
        {"yield_rate": Decimal("NaN")},
        {"discount_factor": 0},
        {"discount_factor": Decimal("NaN")},
        {"etl_run_id": uuid4()},
    ],
)
def test_invalid_processed_points_rejected(db, changes):
    run_id = insert_run(db)
    insert_shock(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id, **changes)


@pytest.mark.parametrize("field", ["is_interpolated", "is_extrapolated", "is_deleted"])
def test_flags_default_false_and_reject_null(db, field):
    run_id = insert_run(db)
    point_id = insert_point(db, run_id)
    with Session(bind=db) as session:
        point = session.get(ProcessedCurvePoint, point_id)
        assert getattr(point, field) is False
        setattr(point, field, True)
        session.flush()
        session.expire(point)
        assert getattr(point, field) is True
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id, tenor=2, **{field: None})


def test_negative_rates_and_discount_above_one_allowed(db):
    insert_point(
        db, insert_run(db), yield_rate=Decimal("-0.01"), discount_factor=Decimal("1.01")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"run_type": "unknown"},
        {"status": "unknown"},
        {"rows_saved": -1},
        {"date_to": date(2026, 10, 18)},
        {"status": "success"},
        {"status": "failed", "finished_at": func.now()},
        {"status": "partial_success", "finished_at": func.now(), "error": " "},
        {"status": "no_data", "finished_at": func.now()},
        {"status": "skipped_calendar", "finished_at": func.now(), "reason": " "},
        {"finished_at": func.now()},
        {"status": "success", "finished_at": text("now() - interval '1 hour'")},
    ],
)
def test_invalid_run_states_rejected(db, changes):
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_run(db, **changes)


@pytest.mark.parametrize(
    "status,details",
    [
        ("success", {}),
        ("failed", {"error": "source unavailable"}),
        ("partial_success", {"error": "one date unavailable"}),
        ("no_data", {"reason": "publication delayed"}),
        ("skipped_calendar", {"reason": "no publication expected"}),
    ],
)
def test_terminal_run_states(db, status, details):
    run_id = insert_run(db, run_type="scheduled")
    db.execute(
        EtlRun.__table__.update()
        .where(EtlRun.id == run_id)
        .values(
            status=status,
            finished_at=func.now(),
            **details,
        )
    )


def test_scenario_parameters_are_versioned_and_immutable(db):
    shock_id = insert_shock(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_shock(db)
    with pytest.raises(IntegrityError, match="immutable"), db.begin_nested():
        db.execute(
            StressShock.__table__.update()
            .where(StressShock.id == shock_id)
            .values(parameters={"shift": "0.05"})
        )
    for values in (
        {"scenario": "unknown", "version": "v1", "parameters": {"shift": "0.04"}},
        {"scenario": "Parallel_Up", "version": " ", "parameters": {"shift": "0.04"}},
        {"scenario": "Parallel_Up", "version": "v2", "parameters": {}},
        {"scenario": "Parallel_Up", "version": "v2", "parameters": []},
    ):
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(StressShock.__table__.insert().values(**values))
