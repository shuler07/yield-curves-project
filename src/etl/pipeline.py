"""Collect, calculate and publish curves; each date has its own transaction."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import CurvePoint, EtlRun, StressCurve, StressShock
from src.db.session import get_session_factory
from src.etl.calculations import (
    PRECISION,
    RawSnapshot,
    ScenarioParameters,
    calculate_snapshot,
)
from src.etl.common import (
    DEFAULT_DOWNLOADS_DIR,
    OUTPUT_NAME,
    SOURCE_TERMS,
    DataValidationError,
    DateRange,
    NoDataError,
    read_manifest,
)
from src.etl.merge import extract_and_merge
from src.etl.parse import collect_curves
from src.etl.postprocessing import read_snapshots


@dataclass
class RunResult:
    run_id: UUID
    status: str = "running"
    run_dir: Path | None = None
    snapshot_count: int = 0
    rows_processed: int = 0
    rows_saved: int = 0
    interpolated: int = 0
    extrapolated: int = 0
    saved_dates: list[date] = field(default_factory=list)
    missing_dates: list[date] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _message(exc: Exception) -> str:
    # Avoid SQLAlchemy's huge statement/parameter dump in the run journal and CLI.
    original = getattr(exc, "orig", exc)
    return f"{type(exc).__name__}: {str(original).strip() or type(original).__name__}"


def _stored(value: Decimal, scale: int) -> Decimal:
    """Compare with PostgreSQL NUMERIC rounding, not unrounded calculation output."""
    with localcontext() as context:
        context.prec = PRECISION
        return value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def _base_changed(existing: CurvePoint, record: dict) -> bool:
    return any(
        getattr(existing, name) != _stored(record[name], scale)
        for name, scale in (("yield_rate", 10), ("discount_factor", 18))
    ) or any(
        getattr(existing, name) != record[name]
        for name in ("is_interpolated", "is_extrapolated", "is_deleted")
    )


def _publish_date(
    session: Session,
    snapshots: list[RawSnapshot],
    parameters: ScenarioParameters,
    run_id: UUID,
) -> tuple[int, int, int]:
    """Caller owns the transaction. No commits here, including scenario definitions."""
    curve_date = snapshots[0].curve_date
    # A stable two-int advisory key serializes concurrent writers for the same date.
    # Different dates can still load concurrently; PostgreSQL releases it on rollback.
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :day)"),
        {"namespace": 205, "day": curve_date.toordinal()},
    )
    interpolated = extrapolated = saved = 0
    for raw in snapshots:
        calculated = calculate_snapshot(raw, parameters)
        records = [point.record(run_id) for point in calculated.base_points]
        old = {
            point.tenor: point
            for point in session.scalars(
                select(CurvePoint).where(
                    CurvePoint.curve_date == raw.curve_date,
                    CurvePoint.quote_time == raw.quote_time,
                )
            )
        }
        if any(
            record["tenor"] in old and _base_changed(old[record["tenor"]], record)
            for record in records
        ):
            # All old scenario versions refer to this mutable baseline. Invalidate
            # the entire snapshot's old results before publishing the current six.
            session.execute(
                delete(StressCurve).where(
                    StressCurve.curve_point_id.in_([point.id for point in old.values()])
                )
            )
        statement = insert(CurvePoint).values(records)
        statement = statement.on_conflict_do_update(
            constraint="uq_curve_points_snapshot_tenor",
            set_={
                name: getattr(statement.excluded, name)
                for name in (
                    "yield_rate",
                    "discount_factor",
                    "etl_run_id",
                    "is_interpolated",
                    "is_extrapolated",
                    "is_deleted",
                )
            },
        ).returning(CurvePoint.tenor, CurvePoint.id)
        ids = dict(session.execute(statement).all())
        saved += len(records)
        interpolated += sum(point.is_interpolated for point in calculated.base_points)
        extrapolated += sum(point.is_extrapolated for point in calculated.base_points)
        for curve in calculated.scenarios:
            definition = curve.definition_record()
            # Definitions are immutable; even a no-op UPDATE would fire the trigger.
            session.execute(
                insert(StressShock)
                .values(**definition)
                .on_conflict_do_nothing(constraint="uq_stress_shocks_scenario_version")
            )
            shock = session.scalar(
                select(StressShock).where(
                    StressShock.scenario == definition["scenario"],
                    StressShock.version == definition["version"],
                )
            )
            if shock.parameters != definition["parameters"]:
                raise DataValidationError("Версия сценария уже имеет другие параметры")
            stress_records = [
                point.record(ids[point.tenor], shock.id, run_id)
                for point in curve.points
            ]
            statement = insert(StressCurve).values(stress_records)
            session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_stress_curves_point_shock",
                    set_={
                        name: getattr(statement.excluded, name)
                        for name in ("stressed_yield", "discount_factor", "etl_run_id")
                    },
                )
            )
            saved += len(stress_records)
    return saved, interpolated, extrapolated


def run_pipeline(
    dates: DateRange,
    *,
    run_type: str = "manual",
    run_dir: Path | None = None,
    from_run_dir: Path | None = None,
    timeout: float = 180,
    parameters: ScenarioParameters | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> RunResult:
    """Shared synchronous entry point for CLI, future FastAPI and scheduler.

    A fresh session/transaction records start and finish independently of curves.
    Reuse requires the exact manifest range. CSV remains raw with NA markers.
    rows_saved counts upserted base + stress rows, including repeat writes.
    """
    if run_type not in {"manual", "scheduled"}:
        raise ValueError("run_type должен быть manual или scheduled")
    if run_dir is not None and from_run_dir is not None:
        raise ValueError("run_dir и from_run_dir несовместимы")
    parameters = parameters or ScenarioParameters()
    factory = session_factory or get_session_factory()
    working_dir = (
        Path(from_run_dir or run_dir).resolve()
        if (from_run_dir is not None or run_dir is not None)
        else (
            DEFAULT_DOWNLOADS_DIR
            / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid4().hex[:12]}"
        )
    )
    with factory.begin() as session:
        run = EtlRun(
            run_type=run_type,
            date_from=dates.start,
            date_to=dates.end,
            reason=f"run_dir={working_dir}",
        )
        session.add(run)
        session.flush()
        result = RunResult(run.id, run_dir=working_dir)
    try:
        if from_run_dir is None:
            merged = collect_curves(dates, working_dir, timeout)
            result.run_dir = merged.output_file.parent.resolve()
            snapshots = read_snapshots(merged.output_file)
        else:
            result.run_dir = Path(from_run_dir).resolve()
            manifest_dates, files = read_manifest(result.run_dir)
            if manifest_dates != dates:
                raise DataValidationError("Диапазон должен совпадать с manifest.json")
            # Independent workspace: concurrent replays never replace a CSV that
            # another process is reading (important for Windows file sharing).
            with TemporaryDirectory(prefix="yield-curves-") as directory:
                merged = extract_and_merge(files, Path(directory) / OUTPUT_NAME, dates)
                snapshots = read_snapshots(merged.output_file)
        grouped = defaultdict(list)
        for snapshot in snapshots:
            if not dates.start <= snapshot.curve_date <= dates.end:
                raise DataValidationError("Снимок вне запрошенного диапазона")
            grouped[snapshot.curve_date].append(snapshot)
        result.missing_dates = [day for day in dates.days() if day not in grouped]
        result.rows_processed = len(snapshots) * len(SOURCE_TERMS)
        for day, day_snapshots in sorted(grouped.items()):
            try:
                with factory.begin() as session:
                    saved, interpolated, extrapolated = _publish_date(
                        session, day_snapshots, parameters, result.run_id
                    )
                # Only count committed dates, never work rolled back on failure.
                result.saved_dates.append(day)
                result.snapshot_count += len(day_snapshots)
                result.rows_saved += saved
                result.interpolated += interpolated
                result.extrapolated += extrapolated
            except Exception as exc:  # noqa: BLE001 - isolate dates and persist failures
                result.errors[day.isoformat()] = _message(exc)
        result.status = (
            "partial_success"
            if result.errors and result.saved_dates
            else "failed"
            if result.errors
            else "success"
        )
    except NoDataError as exc:
        result.status = "no_data"
        result.missing_dates = list(dates.days())
        reason = _message(exc)
    except Exception as exc:  # noqa: BLE001 - persist unexpected source failures too
        result.status = "failed"
        result.errors["source"] = _message(exc)

    reason_parts = []
    if result.run_dir is not None:
        reason_parts.append(f"run_dir={result.run_dir}")
    if result.missing_dates:
        reason_parts.append(
            "Нет наблюдений: "
            + ", ".join(day.isoformat() for day in result.missing_dates)
        )
    if result.status == "no_data":
        reason_parts.append(reason)
    # The failed curve transaction is closed before this independent journal write.
    with factory.begin() as session:
        session.execute(
            update(EtlRun)
            .where(EtlRun.id == result.run_id)
            .values(
                status=result.status,
                finished_at=func.clock_timestamp(),
                rows_processed=result.rows_processed,
                rows_saved=result.rows_saved,
                reason="; ".join(reason_parts) or None,
                error="\n".join(
                    f"{day}: {error}" for day, error in result.errors.items()
                )
                or None,
            )
        )
    return result
