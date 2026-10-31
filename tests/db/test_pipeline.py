"""Real PostgreSQL commits: publication, repeat loads and independent run audit."""

import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from click.testing import CliRunner
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.db.models import CurvePoint, EtlRun, StressCurve, StressShock
from src.etl import pipeline
from src.etl.calculations import ScenarioParameters
from src.etl.common import SOURCE_TERMS, TARGET_TERMS, DateRange, write_manifest
from src.etl.merge import merge_run
from src.etl.run import main

DAY = date(2026, 10, 30)
DATES = DateRange(DAY, DAY)


@pytest.fixture
def factory(migrated_db):
    engine, _ = migrated_db
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")).startswith("test_db_")
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        # Only this suite's disposable schema; keep production tables untouched.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE stress_curves, curve_points, stress_shocks, etl_runs "
                    "RESTART IDENTITY"
                )
            )


def source(tmp_path, rows, dates=DATES, name="source"):
    folder = tmp_path / name
    folder.mkdir()
    path = folder / "curve.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            ["tradedate", "tradetime", *(f"period_{t}" for t in SOURCE_TERMS)]
        )
        for day, time, rates in rows:
            writer.writerow(
                [
                    day.strftime("%d.%m.%Y"),
                    time,
                    *(rates.get(t, "NA") for t in SOURCE_TERMS),
                ]
            )
    write_manifest(folder, dates, [path])
    return folder


def complete(rate="10"):
    return dict.fromkeys(SOURCE_TERMS, rate)


def load(factory, folder, dates=DATES, **kwargs):
    return pipeline.run_pipeline(
        dates, from_run_dir=folder, session_factory=factory, **kwargs
    )


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("run_type", ["manual", "scheduled"])
def test_full_pipeline_and_repeat_preserve_ids_without_duplicates(
    factory, tmp_path, run_type
):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    first = load(factory, folder, run_type=run_type)
    assert first.interpolated == 4
    assert first.extrapolated == 3
    with factory() as session:
        ids = list(session.scalars(select(CurvePoint.id).order_by(CurvePoint.tenor)))
        run = session.get(EtlRun, first.run_id)
        assert (run.status, run.run_type, run.rows_processed, run.rows_saved) == (
            "success",
            run_type,
            12,
            133,
        )
        assert run.finished_at >= run.started_at
    second = load(factory, folder, run_type=run_type)
    assert first.run_id != second.run_id
    assert second.status == "success"
    assert second.rows_saved == 133
    with factory() as session:
        assert (
            list(session.scalars(select(CurvePoint.id).order_by(CurvePoint.tenor)))
            == ids
        )
        assert [
            count(session, model)
            for model in (CurvePoint, StressCurve, StressShock, EtlRun)
        ] == [19, 114, 6, 2]
        assert (
            tuple(session.scalars(select(CurvePoint.tenor).order_by(CurvePoint.tenor)))
            == TARGET_TERMS
        )
        point = session.scalar(select(CurvePoint).where(CurvePoint.tenor == 1))
        assert point.yield_rate == Decimal("0.1")
        assert point.discount_factor == Decimal("0.909090909090909091")
        assert point.etl_run_id == second.run_id
        stress = session.scalar(
            select(StressCurve)
            .join(StressShock)
            .where(
                StressCurve.curve_point_id == point.id,
                StressShock.scenario == "Parallel_Up",
            )
        )
        assert stress.stressed_yield == Decimal("0.14")
        assert stress.etl_run_id == second.run_id


def test_replay_extends_existing_twelve_point_snapshot_without_replacing_source_points(
    factory, tmp_path
):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    load(factory, folder)
    with factory.begin() as session:
        extra_ids = select(CurvePoint.id).where(CurvePoint.tenor.not_in(SOURCE_TERMS))
        session.execute(
            delete(StressCurve).where(StressCurve.curve_point_id.in_(extra_ids))
        )
        session.execute(delete(CurvePoint).where(CurvePoint.tenor.not_in(SOURCE_TERMS)))
        original = dict(session.execute(select(CurvePoint.tenor, CurvePoint.id)).all())
        assert count(session, CurvePoint) == 12
        assert count(session, StressCurve) == 72
    result = load(factory, folder)
    assert result.status == "success"
    with factory() as session:
        assert count(session, CurvePoint) == 19
        assert count(session, StressCurve) == 114
        assert (
            dict(
                session.execute(
                    select(CurvePoint.tenor, CurvePoint.id).where(
                        CurvePoint.tenor.in_(SOURCE_TERMS)
                    )
                ).all()
            )
            == original
        )


def test_missing_rates_are_filled_only_at_publication(factory, tmp_path):
    folder = source(tmp_path, [(DAY, "18:54:59", {Decimal(1): "10", Decimal(5): "14"})])
    merged = merge_run(folder).output_file
    original = merged.read_bytes()
    result = load(factory, folder)
    assert result.status == "success"
    assert result.interpolated == 4
    assert result.extrapolated == 13
    assert merged.read_bytes() == original
    assert b"NA" in original
    with factory() as session:
        point = session.scalar(select(CurvePoint).where(CurvePoint.tenor == 2))
        assert point.yield_rate == Decimal("0.11")
        assert point.is_interpolated and not point.is_extrapolated
        point = session.scalar(select(CurvePoint).where(CurvePoint.tenor == 20))
        assert point.is_extrapolated and not point.is_interpolated
        assert count(session, StressCurve) == 114


def test_date_rollback_keeps_other_dates_and_old_data(factory, tmp_path):
    old = source(tmp_path, [(DAY, "18:00:00", complete())], name="old")
    old_run = load(factory, old)
    dates = DateRange(date(2026, 10, 29), DAY)
    folder = source(
        tmp_path,
        [
            (dates.start, "18:00:00", complete()),
            (DAY, "18:00:00", complete("11")),
            (DAY, "19:00:00", {Decimal(1): "10"}),
        ],
        dates=dates,
    )
    result = load(factory, folder, dates)
    assert result.status == "partial_success"
    assert result.rows_processed == 36
    assert result.rows_saved == 133
    assert result.saved_dates == [dates.start]
    assert list(result.errors) == [DAY.isoformat()]
    with factory() as session:
        assert count(session, CurvePoint) == 38
        assert count(session, StressCurve) == 228
        points = list(
            session.scalars(select(CurvePoint).where(CurvePoint.curve_date == DAY))
        )
        assert len(points) == 19
        assert all(p.yield_rate == Decimal("0.1") for p in points)
        assert all(p.etl_run_id == old_run.run_id for p in points)
        run = session.get(EtlRun, result.run_id)
        assert run.status == "partial_success"
        assert DAY.isoformat() in run.error
        assert run.finished_at is not None


def test_database_error_rolls_back_base_and_scenarios_but_keeps_journal(
    factory, tmp_path
):
    folder = source(tmp_path, [(DAY, "18:00:00", complete())])
    engine = factory.kw["bind"]

    def fail_stress(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO stress_curves"):
            raise IntegrityError(
                statement, parameters, ValueError("injected DB failure")
            )

    event.listen(engine, "before_cursor_execute", fail_stress)
    try:
        result = load(factory, folder)
    finally:
        event.remove(engine, "before_cursor_execute", fail_stress)
    assert result.status == "failed"
    assert result.rows_saved == 0
    with factory() as session:
        assert [
            count(session, model) for model in (CurvePoint, StressCurve, StressShock)
        ] == [0, 0, 0]
        run = session.get(EtlRun, result.run_id)
        assert run.status == "failed"
        assert "injected DB failure" in run.error
        assert run.finished_at is not None


def test_new_parameters_keep_old_versions_until_source_correction(factory, tmp_path):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    first = load(factory, folder)
    params = ScenarioParameters(parallel_bps=500)
    load(factory, folder, parameters=params)
    # Repeating the unrounded calculation must NOT treat NUMERIC rounding as a
    # correction and invalidate the earlier parameter version.
    load(factory, folder, parameters=params)
    with factory() as session:
        assert count(session, StressShock) == 12
        assert count(session, StressCurve) == 228
    corrected = source(tmp_path, [(DAY, "18:54:59", complete("12"))], name="corrected")
    result = load(factory, corrected, parameters=params)
    assert result.status == "success"
    with factory() as session:
        assert count(session, CurvePoint) == 19
        assert count(session, StressShock) == 12
        assert count(session, StressCurve) == 114
        assert session.get(EtlRun, first.run_id).status == "success"
        assert set(session.scalars(select(StressShock.version).join(StressCurve))) == {
            params.version
        }
        assert set(session.scalars(select(CurvePoint.yield_rate))) == {Decimal("0.12")}


def test_concurrent_runs_for_one_date_do_not_duplicate_or_fail(factory, tmp_path):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(load, factory, folder) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert all(result.status == "success" for result in results), results
    with factory() as session:
        assert [
            count(session, model)
            for model in (CurvePoint, StressCurve, StressShock, EtlRun)
        ] == [19, 114, 6, 2]


def test_start_committed_before_collection_and_source_failure_logged(
    factory, tmp_path, monkeypatch
):
    def fail_source(dates, run_dir, timeout):
        with factory() as session:
            run = session.scalar(select(EtlRun))
            assert run.status == "running"
            assert run.finished_at is None
        raise TimeoutError("source unavailable")

    monkeypatch.setattr(pipeline, "collect_curves", fail_source)
    result = pipeline.run_pipeline(DATES, session_factory=factory)
    assert result.status == "failed"
    with factory() as session:
        run = session.get(EtlRun, result.run_id)
        assert "source unavailable" in run.error
        assert run.finished_at is not None
        assert count(session, CurvePoint) == 0


def test_no_data_and_missing_dates_are_not_fabricated(factory, tmp_path):
    empty = source(tmp_path, [])
    result = load(factory, empty)
    assert result.status == "no_data"
    with factory() as session:
        run = session.get(EtlRun, result.run_id)
        assert run.reason
        assert run.error is None
        assert count(session, CurvePoint) == 0
    dates = DateRange(date(2026, 10, 29), DAY)
    folder = source(tmp_path, [(DAY, "18:00:00", complete())], dates, name="range")
    result = load(factory, folder, dates)
    assert result.status == "success"
    assert result.missing_dates == [dates.start]
    assert result.saved_dates == [DAY]
    with factory() as session:
        assert str(dates.start) in session.get(EtlRun, result.run_id).reason
        assert set(session.scalars(select(CurvePoint.curve_date))) == {DAY}


def test_cli_live_collection_and_reuse(factory, tmp_path, monkeypatch):
    folder = source(tmp_path, [(DAY, "18:00:00", complete())])
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)

    def collect(dates, run_dir, timeout):
        assert dates == DATES
        assert timeout == 60
        return merge_run(folder)

    monkeypatch.setattr(pipeline, "collect_curves", collect)
    runner = CliRunner()
    result = runner.invoke(main, ["--date", str(DAY), "--timeout", "60"])
    assert result.exit_code == 0, result.output
    run_id = UUID(result.output.split("ETL run: ")[1].split(";")[0])
    with factory() as session:
        assert session.get(EtlRun, run_id).status == "success"
    result = runner.invoke(main, ["--from-run-dir", str(folder)])
    assert result.exit_code == 0, result.output
    with factory() as session:
        assert count(session, CurvePoint) == 19
        assert count(session, StressCurve) == 114


def test_cli_failure_returns_nonzero_and_run_id(factory, tmp_path, monkeypatch):
    folder = source(tmp_path, [(DAY, "18:00:00", {Decimal(1): "10"})])
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    result = CliRunner().invoke(main, ["--from-run-dir", str(folder)])
    assert result.exit_code == 1
    assert "ETL run:" in result.output
    assert "failed" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--date", "2026-10-30", "--date-from", "2026-10-29"],
        ["--date-from", "2026-10-30"],
        ["--date", "not-a-date"],
    ],
)
def test_cli_invalid_options_do_not_start_database_work(arguments, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid options must not call the pipeline")

    monkeypatch.setattr("src.etl.run.run_pipeline", unexpected)
    assert CliRunner().invoke(main, arguments).exit_code == 2
