"""Periodic ETL worker; no source requests or threads at import time."""

import logging
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from src.api.settings import Settings
from src.db.models import EtlRun
from src.db.session import get_engine
from src.etl.common import DateRange
from src.etl.pipeline import RunResult, run_pipeline

LOGGER = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


def scheduled_update(
    settings: Settings, *, engine=None, runner=None, now=None
) -> RunResult | None:
    """One current-date refresh; a shared DB lock prevents overlapping collectors."""
    engine = engine or get_engine()
    runner = runner or run_pipeline
    today = (now or datetime.now(UTC)).astimezone(MOSCOW).date()
    with engine.connect() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_lock(206, 1)"))
        connection.commit()
        if not locked:
            LOGGER.info("Scheduled ETL already running in another process")
            return None
        try:
            # After a worker restart or with multiple API workers, avoid redoing
            # a refresh already attempted within the configured interval.
            recent = connection.scalar(
                select(EtlRun.id)
                .where(
                    EtlRun.run_type == "scheduled",
                    EtlRun.date_from == today,
                    EtlRun.date_to == today,
                    EtlRun.started_at
                    >= func.clock_timestamp()
                    - timedelta(hours=settings.interval_hours),
                )
                .limit(1)
            )
            connection.commit()
            if recent is not None:
                return None
            result = runner(
                DateRange(today, today),
                run_type="scheduled",
                timeout=settings.timeout_seconds,
            )
            log = (
                LOGGER.error
                if result.status in {"failed", "partial_success"}
                else LOGGER.info
            )
            log(
                "Scheduled ETL date=%s run=%s status=%s errors=%s",
                today,
                result.run_id,
                result.status,
                result.errors,
            )
            return result
        finally:
            # If the liveness query failed, clear its failed transaction first.
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(206, 1)"))
            connection.commit()


class EtlScheduler:
    def __init__(self, settings: Settings, *, callback=None):
        self.settings = settings
        self._callback = callback or (lambda: scheduled_update(settings))
        self._stop = Event()
        self._thread = Thread(target=self._loop, name="moex-etl", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Finish an active ETL cleanly so its transaction and journal are complete.
        self._thread.join()

    def _loop(self):
        while not self._stop.is_set():
            started = monotonic()
            delay = self.settings.interval_hours * 3600
            try:
                result = self._callback()
                if result is None:
                    # Recheck an interval owned by another/restarted process soon.
                    delay = min(delay, 60)
            except Exception:
                LOGGER.exception("Scheduled ETL failed; retry at the next interval")
            self._stop.wait(max(0, started + delay - monotonic()))
