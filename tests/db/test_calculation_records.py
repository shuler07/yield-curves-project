from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import EtlRun, ProcessedCurvePoint, RawCurvePoint, StressShock
from src.etl.calculations import RawSnapshot, calculate_scenarios, prepare_base_curve


@pytest.mark.parametrize("run_type", ["manual", "scheduled"])
def test_calculation_records_fit_database_and_preserve_provenance(db, run_type):
    """Record adapters fit the raw and processed tables, with correct provenance."""
    with Session(bind=db) as session:
        run = EtlRun(
            run_type=run_type, date_from=date(2026, 10, 30), date_to=date(2026, 10, 30)
        )
        session.add(run)
        session.flush()
        raw = RawSnapshot(
            date(2026, 10, 30),
            time(18, 54, 59),
            {
                Decimal(1): Decimal("0.1"),
                Decimal(5): Decimal("0.14"),
            },
        )
        session.add_all([RawCurvePoint(**record) for record in raw.records(run.id)])
        base = prepare_base_curve(raw)
        points = [ProcessedCurvePoint(**point.record(run.id)) for point in base]
        session.add_all(points)
        session.flush()
        bases = {point.tenor: point for point in base}
        for curve in calculate_scenarios(base):
            definition = StressShock(**curve.definition_record())
            session.add(definition)
            session.flush()
            session.add_all(
                [
                    ProcessedCurvePoint(
                        **point.record(
                            bases[point.tenor],
                            curve.scenario,
                            curve.parameters.version,
                            run.id,
                        )
                    )
                    for point in curve.points
                ]
            )
        session.flush()
        session.expire_all()
        assert session.scalar(select(func.count()).select_from(RawCurvePoint)) == 2
        assert (
            session.scalar(select(func.count()).select_from(ProcessedCurvePoint)) == 133
        )
        assert session.scalar(select(func.count()).select_from(StressShock)) == 6
        point = session.scalar(
            select(ProcessedCurvePoint).where(
                ProcessedCurvePoint.tenor == 2,
                ProcessedCurvePoint.scenario == "Baseline",
            )
        )
        assert point.is_interpolated is True
        assert point.is_extrapolated is False
        assert point.is_deleted is False
        assert point.yield_rate == Decimal("0.11")
        extrapolated = session.scalar(
            select(ProcessedCurvePoint).where(
                ProcessedCurvePoint.tenor == 20,
                ProcessedCurvePoint.scenario == "Baseline",
            )
        )
        assert extrapolated.is_extrapolated is True
        stress = session.scalar(
            select(ProcessedCurvePoint).where(
                ProcessedCurvePoint.scenario == "Parallel_Up",
                ProcessedCurvePoint.tenor == 1,
            )
        )
        assert stress.yield_rate == Decimal("0.14")
        assert stress.discount_factor == Decimal("0.877192982456140351")
