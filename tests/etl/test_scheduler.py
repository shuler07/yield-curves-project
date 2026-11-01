from datetime import UTC, date, datetime
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from src.api.main import create_app
from src.api.settings import Settings
from src.db.models import EtlRun
from src.etl.pipeline import RunResult
from src.etl.scheduler import EtlScheduler, scheduled_update


@pytest.fixture
def scheduler_engine(migrated_db):
    engine, _ = migrated_db
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")).startswith(
                "test_db_"
            )
            connection.execute(
                text(
                    "TRUNCATE processed_curve_points, raw_curve_points, stress_shocks, etl_runs"
                )
            )


def test_moscow_date_scheduled_audit_and_restart_deduplication(scheduler_engine):
    factory = sessionmaker(bind=scheduler_engine, expire_on_commit=False)
    calls = []

    def runner(dates, run_type, timeout):
        calls.append((dates, run_type, timeout))
        with factory.begin() as session:
            run = EtlRun(
                run_type=run_type,
                date_from=dates.start,
                date_to=dates.end,
                status="no_data",
                finished_at=func.now(),
                reason="No quotes",
            )
            session.add(run)
            session.flush()
            return RunResult(run.id, status="no_data")

    settings = Settings(timeout_seconds=120)
    now = datetime(2026, 11, 1, 21, 30, tzinfo=UTC)
    result = scheduled_update(settings, engine=scheduler_engine, runner=runner, now=now)
    assert result.status == "no_data"
    assert calls[0][0].start == calls[0][0].end == date(2026, 11, 2)
    assert calls[0][1:] == ("scheduled", 120)
    assert (
        scheduled_update(settings, engine=scheduler_engine, runner=runner, now=now)
        is None
    )
    assert len(calls) == 1
    # Once the interval expires a new attempt is allowed, including after no_data.
    with scheduler_engine.begin() as connection:
        connection.execute(
            text("UPDATE etl_runs SET started_at = started_at - interval '7 hours'")
        )
    assert (
        scheduled_update(
            settings, engine=scheduler_engine, runner=runner, now=now
        ).status
        == "no_data"
    )
    assert len(calls) == 2


def test_lock_prevents_overlapping_collectors_and_is_released_on_error(
    scheduler_engine,
):
    def unexpected(*args, **kwargs):
        pytest.fail("Another worker already owns the collector lock")

    with scheduler_engine.connect() as owner:
        owner.execute(text("SELECT pg_advisory_lock(206, 1)"))
        owner.commit()
        try:
            assert (
                scheduled_update(Settings(), engine=scheduler_engine, runner=unexpected)
                is None
            )
        finally:
            owner.execute(text("SELECT pg_advisory_unlock(206, 1)"))
            owner.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("collector failed")

    with pytest.raises(RuntimeError, match="collector failed"):
        scheduled_update(Settings(), engine=scheduler_engine, runner=fail)
    with scheduler_engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_try_advisory_lock(206, 1)"))
        connection.execute(text("SELECT pg_advisory_unlock(206, 1)"))
        connection.commit()


def test_worker_runs_immediately_retries_errors_and_stops_without_waiting_hours():
    called = Event()
    count = []

    def callback():
        count.append(1)
        if len(count) == 1:
            raise RuntimeError("temporary error")
        called.set()

    worker = EtlScheduler(Settings(interval_hours=0.00001), callback=callback)
    worker.start()
    try:
        assert called.wait(3)
    finally:
        worker.stop()
    assert len(count) >= 2
    assert not worker._thread.is_alive()


def test_api_lifespan_starts_and_stops_scheduler(monkeypatch):
    calls = []

    class Worker:
        def __init__(self, settings):
            calls.append(settings.interval_hours)

        def start(self):
            calls.append("start")

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr("src.api.main.EtlScheduler", Worker)
    with TestClient(create_app(Settings(interval_hours=6))):
        assert calls == [6, "start"]
    assert calls == [6, "start", "stop"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval_hours": 0},
        {"interval_hours": float("nan")},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("inf")},
    ],
)
def test_invalid_scheduler_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_settings_use_environment_over_env_file(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "ETL_INTERVAL_HOURS=12\nETL_SCHEDULER_ENABLED=true\n"
    )
    monkeypatch.setattr("src.api.settings.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("ETL_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("ETL_INTERVAL_HOURS", "3")
    settings = Settings.from_env()
    assert settings.scheduler_enabled is False and settings.interval_hours == 3
