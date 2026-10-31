"""Storage contract. Rates are decimal fractions; tenors are years.

No rate-to-discount formula or scenario calibration is chosen by this module.
The producer must supply its convention and versioned scenario parameters.
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EtlRun(Base):
    __tablename__ = "etl_runs"
    __table_args__ = (
        CheckConstraint("run_type IN ('manual', 'scheduled')", name="ck_etl_runs_type"),
        CheckConstraint(
            "status IN ('running', 'success', 'partial_success', 'failed', "
            "'skipped_calendar', 'no_data')",
            name="ck_etl_runs_status",
        ),
        CheckConstraint("date_from <= date_to", name="ck_etl_runs_date_range"),
        CheckConstraint(
            "rows_processed >= 0 AND rows_saved >= 0", name="ck_etl_runs_counts"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="ck_etl_runs_times"
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_etl_runs_finished",
        ),
        CheckConstraint(
            "status NOT IN ('skipped_calendar', 'no_data') OR "
            "(reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="ck_etl_runs_reason",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'partial_success') OR "
            "(error IS NOT NULL AND length(btrim(error)) > 0)",
            name="ck_etl_runs_error",
        ),
        Index("ix_etl_runs_started_at", "started_at"),
        Index("ix_etl_runs_status_started_at", "status", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    run_type: Mapped[str] = mapped_column(String(16))
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), server_default=text("'running'"))
    rows_processed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_saved: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class CurvePoint(Base):
    __tablename__ = "curve_points"
    __table_args__ = (
        UniqueConstraint(
            "curve_date", "quote_time", "tenor", name="uq_curve_points_snapshot_tenor"
        ),
        CheckConstraint(
            "tenor > 0 AND tenor < 'Infinity'::numeric", name="ck_curve_points_tenor"
        ),
        CheckConstraint(
            "yield_rate > '-Infinity'::numeric AND yield_rate < 'Infinity'::numeric",
            name="ck_curve_points_yield",
        ),
        CheckConstraint(
            "discount_factor > 0 AND discount_factor < 'Infinity'::numeric",
            name="ck_curve_points_discount",
        ),
        CheckConstraint(
            "length(btrim(discount_convention)) > 0", name="ck_curve_points_convention"
        ),
        Index("ix_curve_points_etl_run_id", "etl_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    curve_date: Mapped[date] = mapped_column(Date)
    quote_time: Mapped[time] = mapped_column(Time(timezone=False))
    tenor: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    yield_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    discount_factor: Mapped[Decimal] = mapped_column(Numeric(28, 18))
    discount_convention: Mapped[str] = mapped_column(String(100))
    etl_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("etl_runs.id", name="fk_curve_points_etl_run", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_interpolated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_extrapolated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )


class StressShock(Base):
    """One immutable scenario parameter set. New calibration = new version."""

    __tablename__ = "stress_shocks"
    __table_args__ = (
        UniqueConstraint(
            "scenario", "version", name="uq_stress_shocks_scenario_version"
        ),
        CheckConstraint(
            "scenario IN ('Parallel_Up', 'Parallel_Down', 'Short_Up', "
            "'Short_Down', 'Steepener', 'Flattener')",
            name="ck_stress_shocks_scenario",
        ),
        CheckConstraint("length(btrim(version)) > 0", name="ck_stress_shocks_version"),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object' AND parameters <> '{}'::jsonb",
            name="ck_stress_shocks_parameters",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StressCurve(Base):
    __tablename__ = "stress_curves"
    __table_args__ = (
        UniqueConstraint(
            "curve_point_id", "stress_shock_id", name="uq_stress_curves_point_shock"
        ),
        CheckConstraint(
            "stressed_yield > '-Infinity'::numeric AND stressed_yield < 'Infinity'::numeric",
            name="ck_stress_curves_yield",
        ),
        CheckConstraint(
            "discount_factor > 0 AND discount_factor < 'Infinity'::numeric",
            name="ck_stress_curves_discount",
        ),
        CheckConstraint(
            "length(btrim(discount_convention)) > 0", name="ck_stress_curves_convention"
        ),
        Index("ix_stress_curves_shock_point", "stress_shock_id", "curve_point_id"),
        Index("ix_stress_curves_etl_run_id", "etl_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    curve_point_id: Mapped[int] = mapped_column(
        ForeignKey(
            "curve_points.id", name="fk_stress_curves_point", ondelete="RESTRICT"
        )
    )
    stress_shock_id: Mapped[int] = mapped_column(
        ForeignKey(
            "stress_shocks.id", name="fk_stress_curves_shock", ondelete="RESTRICT"
        )
    )
    stressed_yield: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    discount_factor: Mapped[Decimal] = mapped_column(Numeric(28, 18))
    discount_convention: Mapped[str] = mapped_column(String(100))
    etl_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("etl_runs.id", name="fk_stress_curves_etl_run", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
