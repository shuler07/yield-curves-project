from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import CurvePoint, EtlRun, StressCurve, StressShock
from src.etl.calculations import RawSnapshot, calculate_scenarios, prepare_base_curve


@pytest.mark.parametrize("run_type", ["manual", "scheduled"])
def test_calculation_records_fit_database_and_preserve_provenance(db, run_type):
    """Exercise the future loader's order and links in a rolled-back test DB."""
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
        base = prepare_base_curve(raw)
        points = [CurvePoint(**point.record(run.id)) for point in base]
        session.add_all(points)
        session.flush()
        ids = {point.tenor: point.id for point in points}
        for curve in calculate_scenarios(base):
            definition = StressShock(**curve.definition_record())
            session.add(definition)
            session.flush()
            session.add_all(
                [
                    StressCurve(**point.record(ids[point.tenor], definition.id, run.id))
                    for point in curve.points
                ]
            )
        session.flush()
        session.expire_all()
        assert session.scalar(select(func.count()).select_from(CurvePoint)) == 12
        assert session.scalar(select(func.count()).select_from(StressCurve)) == 72
        assert session.scalar(select(func.count()).select_from(StressShock)) == 6
        point = session.scalar(select(CurvePoint).where(CurvePoint.tenor == 2))
        assert point.is_interpolated is True
        assert point.is_extrapolated is False
        assert point.is_deleted is False
        assert point.yield_rate == Decimal("0.11")
        extrapolated = session.scalar(select(CurvePoint).where(CurvePoint.tenor == 20))
        assert extrapolated.is_extrapolated is True
        stress = session.scalar(
            select(StressCurve)
            .join(StressShock)
            .join(CurvePoint)
            .where(
                StressShock.scenario == "Parallel_Up",
                CurvePoint.tenor == 1,
            )
        )
        assert stress.stressed_yield == Decimal("0.14")
        assert stress.discount_factor == Decimal("0.877192982456140351")
        assert stress.discount_convention == "annual_effective_v1"
