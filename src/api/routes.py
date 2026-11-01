from collections import defaultdict
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.schemas import (
    CurveResponse,
    ProcessedHistoryResponse,
    ProcessedPoint,
    ProcessedSnapshotResponse,
    RawHistoryResponse,
    RawPoint,
    RawSnapshotResponse,
)
from src.db.models import EtlRun, ProcessedCurvePoint, RawCurvePoint
from src.db.session import get_db
from src.etl.calculations import SCENARIOS
from src.etl.common import TARGET_TERMS, DateRange

router = APIRouter(prefix="/curves", tags=["curves"])


def requested_dates(
    date: Date | None = None,
    date_from: Date | None = None,
    date_to: Date | None = None,
) -> DateRange:
    if date is not None and date_from is None and date_to is None:
        return DateRange(date, date)
    if (
        date is None
        and date_from is not None
        and date_to is not None
        and date_from <= date_to
    ):
        return DateRange(date_from, date_to)
    raise HTTPException(
        422, "Нужен date либо date_from и date_to; начало не позже конца"
    )


Dates = Annotated[DateRange, Depends(requested_dates)]
Database = Annotated[Session, Depends(get_db)]


@router.get("/raw", response_model=RawHistoryResponse, summary="Исходные ставки MOEX")
def raw_curves(dates: Dates, session: Database):
    rows = session.scalars(
        select(RawCurvePoint)
        .where(RawCurvePoint.curve_date.between(dates.start, dates.end))
        .order_by(
            RawCurvePoint.curve_date, RawCurvePoint.quote_time, RawCurvePoint.tenor
        )
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.curve_date, row.quote_time)].append(RawPoint.model_validate(row))
    snapshots = [
        RawSnapshotResponse(curve_date=day, quote_time=time, points=points)
        for (day, time), points in grouped.items()
    ]
    available = {snapshot.curve_date for snapshot in snapshots}
    return RawHistoryResponse(
        date_from=dates.start,
        date_to=dates.end,
        missing_dates=[day for day in dates.days() if day not in available],
        snapshots=snapshots,
    )


@router.get(
    "/processed",
    response_model=ProcessedHistoryResponse,
    summary="Базовая и шесть стрессовых кривых",
)
def processed_curves(
    dates: Dates,
    session: Database,
    scenario_version: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
):
    statement = (
        select(ProcessedCurvePoint, EtlRun.started_at)
        .join(EtlRun, EtlRun.id == ProcessedCurvePoint.etl_run_id)
        .where(
            ProcessedCurvePoint.curve_date.between(dates.start, dates.end),
            ProcessedCurvePoint.is_deleted.is_(False),
        )
        .order_by(
            ProcessedCurvePoint.curve_date,
            ProcessedCurvePoint.quote_time,
            ProcessedCurvePoint.tenor,
        )
    )
    if scenario_version is not None:
        statement = statement.where(
            (ProcessedCurvePoint.scenario == "Baseline")
            | (ProcessedCurvePoint.scenario_version == scenario_version)
        )
    grouped = defaultdict(list)
    for row, started_at in session.execute(statement):
        grouped[(row.curve_date, row.quote_time)].append((row, started_at))
    snapshots = []
    target = set(TARGET_TERMS)
    for (day, time), entries in grouped.items():
        baseline = [row for row, _ in entries if row.scenario == "Baseline"]
        if {row.tenor for row in baseline} != target:
            continue
        versions = defaultdict(lambda: defaultdict(list))
        latest = {}
        for row, started_at in entries:
            if row.scenario != "Baseline":
                versions[row.scenario_version][row.scenario].append(row)
                latest[row.scenario_version] = max(
                    latest.get(row.scenario_version, started_at), started_at
                )
        complete = [
            version
            for version, scenarios in versions.items()
            if set(scenarios) == set(SCENARIOS)
            and all(
                {row.tenor for row in scenarios[name]} == target for name in SCENARIOS
            )
        ]
        if not complete:
            continue
        # Select one complete calibration, never mix scenarios from different versions.
        version = max(complete, key=lambda item: (latest[item], item))
        curves = [
            CurveResponse(
                scenario="Baseline",
                scenario_version=None,
                points=[ProcessedPoint.model_validate(row) for row in baseline],
            )
        ]
        curves.extend(
            CurveResponse(
                scenario=name,
                scenario_version=version,
                points=[
                    ProcessedPoint.model_validate(row)
                    for row in versions[version][name]
                ],
            )
            for name in SCENARIOS
        )
        snapshots.append(
            ProcessedSnapshotResponse(curve_date=day, quote_time=time, curves=curves)
        )
    available = {snapshot.curve_date for snapshot in snapshots}
    return ProcessedHistoryResponse(
        date_from=dates.start,
        date_to=dates.end,
        missing_dates=[day for day in dates.days() if day not in available],
        snapshots=snapshots,
    )
