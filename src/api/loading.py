"""Fill missing history through the same audited ETL used by the CLI."""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import fields
from datetime import timedelta
from time import monotonic, sleep

from fastapi import HTTPException
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import StressShock
from src.etl.calculations import SCENARIOS, ScenarioParameters
from src.etl.common import DateRange
from src.etl.pipeline import run_pipeline


def _parameters(session: Session, version: str | None) -> ScenarioParameters:
    default = ScenarioParameters()
    if version is None or version == default.version:
        return default
    definitions = session.scalars(
        select(StressShock).where(StressShock.version == version)
    ).all()
    if {row.scenario for row in definitions} != set(SCENARIOS):
        raise HTTPException(404, "Не найдена полная версия параметров сценариев")
    payload = definitions[0].parameters
    try:
        parameters = ScenarioParameters(
            **{field.name: payload[field.name] for field in fields(ScenarioParameters)}
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            422, "Версия параметров не поддерживается расчётным слоем"
        ) from exc
    if parameters.version != version or any(
        row.parameters != payload for row in definitions
    ):
        raise HTTPException(422, "Версия параметров не поддерживается расчётным слоем")
    return parameters


def _ranges(days):
    start = end = days[0]
    for day in days[1:]:
        if day == end + timedelta(days=1):
            end = day
        else:
            yield DateRange(start, end)
            start = end = day
    yield DateRange(start, end)


@contextmanager
def _collector_lock(engine: Engine, timeout: float):
    deadline = monotonic() + timeout
    while True:
        guard = engine.connect()
        try:
            locked = guard.scalar(text("SELECT pg_try_advisory_lock(206, 1)"))
            guard.commit()
        except Exception:
            guard.close()
            raise
        if locked:
            break
        # Do not exhaust the SQLAlchemy pool with connections waiting on a lock:
        # the active collector still needs connections for its ETL transactions.
        guard.close()
        if monotonic() >= deadline:
            raise HTTPException(504, "Истекло время ожидания уже выполняемой загрузки")
        sleep(min(0.1, max(0, deadline - monotonic())))
    try:
        yield
    finally:
        try:
            guard.rollback()
            guard.execute(text("SELECT pg_advisory_unlock(206, 1)"))
            guard.commit()
        finally:
            guard.close()


def load_history(
    read: Callable, session: Session, *, timeout: float, version: str | None = None
):
    history = read()
    if not history.missing_dates:
        return history
    parameters = _parameters(session, version)
    engine = session.get_bind()
    if not isinstance(engine, Engine):
        raise TypeError("On-demand history loading requires an engine-bound session")
    # Release the HTTP read transaction before waiting for source collection.
    session.rollback()
    with _collector_lock(engine, timeout):
        # Same collector lock as the scheduler: concurrent requests wait, then
        # recheck the database instead of collecting identical dates again.
        history = read()
        ranges = (
            list(_ranges(sorted(history.missing_dates)))
            if history.missing_dates
            else []
        )
        session.rollback()
        failures = []
        for dates in ranges:
            result = run_pipeline(
                dates,
                run_type="manual",
                timeout=timeout,
                parameters=parameters,
                session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            )
            if result.status in {"failed", "partial_success"}:
                failures.append(
                    {
                        "etl_run_id": str(result.run_id),
                        "status": result.status,
                        "date_from": str(dates.start),
                        "date_to": str(dates.end),
                    }
                )
        session.expire_all()
        history = read()
        if failures and history.missing_dates:
            raise HTTPException(
                502,
                {
                    "message": "Не удалось загрузить или рассчитать часть дат; подробности в etl_runs",
                    "runs": failures,
                    "missing_dates": [str(day) for day in history.missing_dates],
                },
            )
        return history


def get_history_loader():
    return load_history
