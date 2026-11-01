from datetime import date, time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RawPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenor: Decimal
    yield_rate: Decimal
    etl_run_id: UUID


class ProcessedPoint(RawPoint):
    discount_factor: Decimal
    is_interpolated: bool
    is_extrapolated: bool


class RawSnapshotResponse(BaseModel):
    curve_date: date
    quote_time: time
    points: list[RawPoint]


class CurveResponse(BaseModel):
    scenario: str
    scenario_version: str | None
    points: list[ProcessedPoint]


class ProcessedSnapshotResponse(BaseModel):
    curve_date: date
    quote_time: time
    curves: list[CurveResponse]


class RawHistoryResponse(BaseModel):
    date_from: date
    date_to: date
    missing_dates: list[date]
    snapshots: list[RawSnapshotResponse]


class ProcessedHistoryResponse(BaseModel):
    date_from: date
    date_to: date
    missing_dates: list[date]
    snapshots: list[ProcessedSnapshotResponse]
