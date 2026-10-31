from datetime import date, time
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import CurvePoint, EtlRun, StressCurve, StressShock


def insert_run(db, **overrides):
    values = {
        "run_type": "manual",
        "date_from": date(2026, 10, 19),
        "date_to": date(2026, 10, 19),
    }
    values.update(overrides)
    return db.execute(
        EtlRun.__table__.insert().values(**values).returning(EtlRun.id)
    ).scalar_one()


def point_values(run_id, **overrides):
    values = {
        "curve_date": date(2026, 10, 19),
        "quote_time": time(18, 50),
        "tenor": Decimal(1),
        "yield_rate": Decimal("0.15"),
        "discount_factor": Decimal("0.860707976425057808"),
        "discount_convention": "test-continuous-v1",
        "etl_run_id": run_id,
    }
    values.update(overrides)
    return values


def insert_point(db, run_id, **overrides):
    return db.execute(
        CurvePoint.__table__.insert()
        .values(**point_values(run_id, **overrides))
        .returning(CurvePoint.id)
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


def stress_values(run_id, point_id, shock_id, **overrides):
    values = {
        "curve_point_id": point_id,
        "stress_shock_id": shock_id,
        "stressed_yield": Decimal("0.19"),
        "discount_factor": Decimal("0.826959133943362334"),
        "discount_convention": "test-continuous-v1",
        "etl_run_id": run_id,
    }
    values.update(overrides)
    return values


def test_schema_matches_models_and_migration_round_trip(migrated_db):
    engine, config = migrated_db
    command.check(config)
    command.downgrade(config, "0001_create_db")
    with engine.begin() as connection:
        run_id = insert_run(connection)
        point_id = connection.execute(
            text(
                "INSERT INTO curve_points (curve_date, quote_time, tenor, yield_rate, "
                "discount_factor, discount_convention, etl_run_id) "
                "VALUES ('2026-10-19', '18:50:00', 1, 0.15, 0.86, 'test-v1', :run_id) "
                "RETURNING id"
            ),
            {"run_id": run_id},
        ).scalar_one()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        point = (
            connection.execute(
                select(CurvePoint.__table__).where(CurvePoint.id == point_id)
            )
            .mappings()
            .one()
        )
        assert point["yield_rate"] == Decimal("0.15")
        assert all(
            point[name] is False
            for name in ("is_interpolated", "is_extrapolated", "is_deleted")
        )
    command.downgrade(config, "0001_create_db")
    assert not {"is_interpolated", "is_extrapolated", "is_deleted"} & {
        column["name"] for column in inspect(engine).get_columns("curve_points")
    }
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT id FROM curve_points WHERE id = :id"), {"id": point_id}
            )
            == point_id
        )
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.check(config)
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version",
        "etl_runs",
        "curve_points",
        "stress_shocks",
        "stress_curves",
    }


def test_curve_point_flags_default_to_false_and_can_be_updated(db):
    point_id = insert_point(db, insert_run(db))
    with Session(bind=db) as session:
        point = session.get(CurvePoint, point_id)
        assert point.is_interpolated is False
        assert point.is_extrapolated is False
        assert point.is_deleted is False
        point.is_interpolated = True
        point.is_extrapolated = True
        point.is_deleted = True
        session.flush()
        session.expire(point)
        assert point.is_interpolated is True
        assert point.is_extrapolated is True
        assert point.is_deleted is True


@pytest.mark.parametrize("field", ["is_interpolated", "is_extrapolated", "is_deleted"])
def test_curve_point_flags_do_not_allow_null(db, field):
    run_id = insert_run(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id, **{field: None})


def test_models_round_trip_and_scenario_versions(db):
    run_id = insert_run(db)
    point_id = insert_point(db, run_id)
    for version in ("test-v1", "test-v2"):
        shock_id = insert_shock(db, version)
        db.execute(
            StressCurve.__table__.insert().values(
                **stress_values(run_id, point_id, shock_id)
            )
        )
    with Session(bind=db) as session:
        point = session.get(CurvePoint, point_id)
        assert point.yield_rate == Decimal("0.15")
        assert point.discount_factor == Decimal("0.860707976425057808")
        assert point.created_at.utcoffset() is not None
        assert session.scalar(select(func.count()).select_from(StressCurve)) == 2


def test_duplicate_point_rejected_but_another_quote_time_allowed(db):
    run_id = insert_run(db)
    insert_point(db, run_id)
    insert_point(db, run_id, quote_time=time(19, 0))
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id)


def test_stress_duplicate_and_foreign_keys(db):
    run_id = insert_run(db)
    point_id = insert_point(db, run_id)
    shock_id = insert_shock(db)
    values = stress_values(run_id, point_id, shock_id)
    db.execute(StressCurve.__table__.insert().values(**values))
    for invalid in (
        values,
        {**values, "curve_point_id": -1},
        {**values, "stress_shock_id": -1},
        {
            **values,
            "etl_run_id": uuid4(),
            "stress_shock_id": insert_shock(db, "test-v2"),
        },
    ):
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(StressCurve.__table__.insert().values(**invalid))
    for table, key in (
        (EtlRun.__table__, run_id),
        (CurvePoint.__table__, point_id),
        (StressShock.__table__, shock_id),
    ):
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(table.delete().where(table.c.id == key))


@pytest.mark.parametrize(
    "changes",
    [
        {"tenor": 0},
        {"tenor": -1},
        {"tenor": Decimal("NaN")},
        {"yield_rate": Decimal("NaN")},
        {"discount_factor": 0},
        {"discount_factor": -1},
        {"discount_factor": Decimal("NaN")},
        {"discount_convention": " "},
        {"etl_run_id": uuid4()},
    ],
)
def test_invalid_base_points_rejected(db, changes):
    run_id = insert_run(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"stressed_yield": Decimal("NaN")},
        {"discount_factor": 0},
        {"discount_factor": Decimal("NaN")},
        {"discount_convention": ""},
    ],
)
def test_invalid_stress_points_rejected(db, changes):
    run_id = insert_run(db)
    values = stress_values(
        run_id, insert_point(db, run_id), insert_shock(db), **changes
    )
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(StressCurve.__table__.insert().values(**values))


def test_negative_rate_and_discount_above_one_allowed(db):
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


def test_failed_transaction_leaves_no_partial_points(db):
    run_id = insert_run(db)
    with pytest.raises(IntegrityError), db.begin_nested():
        insert_point(db, run_id)
        insert_point(db, run_id, tenor=0)
    assert db.scalar(select(func.count()).select_from(CurvePoint)) == 0
    assert db.scalar(select(func.count()).select_from(EtlRun)) == 1
