"""Real PostgreSQL commits: publication, repeat loads and independent run audit."""

import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.db.models import EtlRun, ProcessedCurvePoint, RawCurvePoint, StressShock
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
                    "TRUNCATE processed_curve_points, raw_curve_points, stress_shocks, etl_runs "
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
def test_replay_preserves_twelve_raw_points_and_seven_complete_curves(
    factory, tmp_path, run_type
):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    first = load(factory, folder, run_type=run_type)
    assert (first.status, first.rows_processed, first.rows_saved) == (
        "success",
        12,
        145,
    )
    assert (first.interpolated, first.extrapolated) == (4, 3)
    with factory() as session:
        ids = session.execute(
            select(ProcessedCurvePoint.id, ProcessedCurvePoint.created_at).order_by(
                ProcessedCurvePoint.id
            )
        ).all()
        raw_ids = session.execute(
            select(RawCurvePoint.id, RawCurvePoint.created_at).order_by(
                RawCurvePoint.id
            )
        ).all()
    second = load(factory, folder, run_type=run_type)
    assert second.status == "success" and second.rows_saved == 145
    with factory() as session:
        assert (
            session.execute(
                select(ProcessedCurvePoint.id, ProcessedCurvePoint.created_at).order_by(
                    ProcessedCurvePoint.id
                )
            ).all()
            == ids
        )
        assert (
            session.execute(
                select(RawCurvePoint.id, RawCurvePoint.created_at).order_by(
                    RawCurvePoint.id
                )
            ).all()
            == raw_ids
        )
        assert [
            count(session, m)
            for m in (RawCurvePoint, ProcessedCurvePoint, StressShock, EtlRun)
        ] == [12, 133, 6, 2]
        curves = session.execute(
            select(ProcessedCurvePoint.scenario, func.count()).group_by(
                ProcessedCurvePoint.scenario
            )
        ).all()
        assert len(curves) == 7 and all(n == 19 for _, n in curves)
        for scenario, _ in curves:
            assert (
                tuple(
                    session.scalars(
                        select(ProcessedCurvePoint.tenor)
                        .where(ProcessedCurvePoint.scenario == scenario)
                        .order_by(ProcessedCurvePoint.tenor)
                    )
                )
                == TARGET_TERMS
            )
        assert set(session.scalars(select(ProcessedCurvePoint.etl_run_id))) == {
            second.run_id
        }
        assert set(session.scalars(select(RawCurvePoint.etl_run_id))) == {second.run_id}
        assert set(session.scalars(select(RawCurvePoint.tenor))) == set(SOURCE_TERMS)
        point = session.scalar(
            select(ProcessedCurvePoint).where(
                ProcessedCurvePoint.scenario == "Parallel_Up",
                ProcessedCurvePoint.tenor == 1,
            )
        )
        assert point.yield_rate == Decimal("0.14")
        assert point.discount_factor == Decimal("0.877192982456140351")
        run = session.get(EtlRun, second.run_id)
        assert run.run_type == run_type and run.finished_at >= run.started_at


def test_missing_rates_are_only_restored_in_processed_table(factory, tmp_path):
    folder = source(tmp_path, [(DAY, "18:54:59", {Decimal(1): "10", Decimal(5): "14"})])
    merged = merge_run(folder).output_file
    before = merged.read_bytes()
    result = load(factory, folder)
    assert (result.rows_processed, result.rows_saved) == (2, 135)
    assert (result.interpolated, result.extrapolated) == (4, 13)
    assert before == merged.read_bytes() and b"NA" in before
    with factory() as session:
        assert set(session.scalars(select(RawCurvePoint.tenor))) == {
            Decimal(1),
            Decimal(5),
        }
        assert count(session, ProcessedCurvePoint) == 133
        points = list(
            session.scalars(
                select(ProcessedCurvePoint).where(ProcessedCurvePoint.tenor == 2)
            )
        )
        assert len(points) == 7 and all(p.is_interpolated for p in points)
        base = next(p for p in points if p.scenario == "Baseline")
        assert base.yield_rate == Decimal("0.11") and base.scenario_version is None


@pytest.mark.parametrize(
    "rates", [{}, {Decimal(1): "10.123456789012345678901234567890"}]
)
def test_failed_calculation_retains_original_points(factory, tmp_path, rates):
    folder = source(tmp_path, [(DAY, "18:54:59", rates)])
    result = load(factory, folder)
    assert result.status == "failed" and result.rows_saved == len(rates)
    with factory() as session:
        assert count(session, RawCurvePoint) == len(rates)
        assert count(session, ProcessedCurvePoint) == 0
        if rates:
            assert session.scalar(select(RawCurvePoint.yield_rate)) == Decimal(
                "0.10123456789012345678901234567890"
            )
        run = session.get(EtlRun, result.run_id)
        assert run.error and run.finished_at is not None


def test_source_corrections_and_failed_date_invalidate_stale_curves(factory, tmp_path):
    old = source(tmp_path, [(DAY, "18:00:00", complete())], name="old")
    load(factory, old)
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
    assert (result.rows_processed, result.rows_saved) == (25, 158)
    assert result.saved_dates == [dates.start] and list(result.errors) == [
        DAY.isoformat()
    ]
    with factory() as session:
        assert count(session, RawCurvePoint) == 25
        assert count(session, ProcessedCurvePoint) == 133
        assert set(session.scalars(select(ProcessedCurvePoint.curve_date))) == {
            dates.start
        }
        assert DAY.isoformat() in session.get(EtlRun, result.run_id).error


def test_processed_database_failure_keeps_raw_and_audit_then_replay_repairs(
    factory, tmp_path
):
    folder = source(tmp_path, [(DAY, "18:00:00", complete())])
    engine = factory.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO processed_curve_points"):
            raise IntegrityError(
                statement, parameters, ValueError("injected DB failure")
            )

    event.listen(engine, "before_cursor_execute", fail)
    try:
        result = load(factory, folder)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert result.status == "failed" and result.rows_saved == 12
    with factory() as session:
        assert [
            count(session, m) for m in (RawCurvePoint, ProcessedCurvePoint, StressShock)
        ] == [12, 0, 0]
        assert "injected DB failure" in session.get(EtlRun, result.run_id).error
    assert load(factory, folder).status == "success"
    with factory() as session:
        assert count(session, ProcessedCurvePoint) == 133


def test_parameter_versions_coexist_until_source_changes(factory, tmp_path):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    load(factory, folder)
    params = ScenarioParameters(parallel_bps=500)
    assert load(factory, folder, parameters=params).status == "success"
    assert load(factory, folder, parameters=params).status == "success"
    with factory() as session:
        assert count(session, ProcessedCurvePoint) == 247
        assert count(session, StressShock) == 12
    corrected = source(tmp_path, [(DAY, "18:54:59", complete("12"))], name="corrected")
    assert load(factory, corrected, parameters=params).status == "success"
    with factory() as session:
        assert count(session, ProcessedCurvePoint) == 133
        assert set(session.scalars(select(ProcessedCurvePoint.scenario_version))) == {
            None,
            params.version,
        }
        assert set(session.scalars(select(RawCurvePoint.yield_rate))) == {
            Decimal("0.12")
        }
    sparse = source(
        tmp_path,
        [(DAY, "18:54:59", {Decimal(1): "10", Decimal(5): "14"})],
        name="sparse",
    )
    assert load(factory, sparse).status == "success"
    with factory() as session:
        assert (
            count(session, RawCurvePoint) == 2
            and count(session, ProcessedCurvePoint) == 133
        )


def test_concurrent_runs_are_idempotent(factory, tmp_path):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(load, factory, folder) for _ in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert all(result.status == "success" for result in results)
    with factory() as session:
        assert [
            count(session, m) for m in (RawCurvePoint, ProcessedCurvePoint, EtlRun)
        ] == [12, 133, 2]


def test_failed_recalculation_keeps_previous_results_when_raw_data_is_unchanged(
    factory, tmp_path, monkeypatch
):
    folder = source(tmp_path, [(DAY, "18:54:59", complete())])
    first = load(factory, folder)

    def fail(*args, **kwargs):
        raise ArithmeticError("calculation failed")

    monkeypatch.setattr(pipeline, "calculate_snapshot", fail)
    result = load(factory, folder)
    assert result.status == "failed" and result.rows_saved == 12
    with factory() as session:
        assert count(session, ProcessedCurvePoint) == 133
        assert set(session.scalars(select(ProcessedCurvePoint.etl_run_id))) == {
            first.run_id
        }
        assert set(session.scalars(select(RawCurvePoint.etl_run_id))) == {result.run_id}


def test_source_error_is_audited_and_cli_can_replay(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)

    def fail(dates, run_dir, timeout):
        with factory() as session:
            assert session.scalar(select(EtlRun.status)) == "running"
        raise TimeoutError("source unavailable")

    monkeypatch.setattr(pipeline, "collect_curves", fail)
    result = pipeline.run_pipeline(DATES, session_factory=factory)
    assert result.status == "failed"
    folder = source(tmp_path, [(DAY, "18:00:00", complete())])
    result = CliRunner().invoke(main, ["--from-run-dir", str(folder)])
    assert result.exit_code == 0, result.output
    with factory() as session:
        assert (
            count(session, RawCurvePoint) == 12
            and count(session, ProcessedCurvePoint) == 133
        )


@pytest.mark.parametrize("outcome", ["success", "failed"])
def test_run_finishes_if_database_clock_moves_backwards(
    factory, tmp_path, monkeypatch, outcome
):
    folder = source(tmp_path, [(DAY, "18:00:00", complete())])

    def collect(dates, run_dir, timeout):
        # Simulate an earlier wall clock at completion without changing the real
        # server clock: the already committed start now lies in the future.
        with factory.begin() as session:
            session.execute(
                update(EtlRun)
                .where(EtlRun.status == "running")
                .values(started_at=func.clock_timestamp() + text("interval '1 hour'"))
            )
        if outcome == "failed":
            raise TimeoutError("source unavailable after clock adjustment")
        return merge_run(folder)

    monkeypatch.setattr(pipeline, "collect_curves", collect)
    result = pipeline.run_pipeline(DATES, session_factory=factory)
    assert result.status == outcome
    with factory() as session:
        run = session.get(EtlRun, result.run_id)
        assert run.status == outcome
        assert run.finished_at == run.started_at
        if outcome == "failed":
            assert "source unavailable" in run.error


def test_no_data_and_missing_dates_are_not_fabricated(factory, tmp_path):
    empty = source(tmp_path, [])
    result = load(factory, empty)
    assert result.status == "no_data"
    dates = DateRange(date(2026, 10, 29), DAY)
    folder = source(tmp_path, [(DAY, "18:00:00", complete())], dates, name="range")
    result = load(factory, folder, dates)
    assert result.status == "success" and result.missing_dates == [dates.start]
    with factory() as session:
        assert set(session.scalars(select(RawCurvePoint.curve_date))) == {DAY}
        assert str(dates.start) in session.get(EtlRun, result.run_id).reason


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
