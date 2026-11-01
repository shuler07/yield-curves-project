from datetime import date, time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.api.main import create_app
from src.api.settings import Settings
from src.db.models import EtlRun, ProcessedCurvePoint, RawCurvePoint
from src.db.session import get_db
from src.etl.calculations import RawSnapshot, ScenarioParameters
from src.etl.common import SOURCE_TERMS
from src.etl.pipeline import _publish_date

DAY = date(2026, 10, 30)


def seed(session, day=DAY, quote_time=time(18), parameters=None, rates=None):
    run = EtlRun(run_type="manual", date_from=day, date_to=day)
    session.add(run)
    session.flush()
    raw = RawSnapshot(
        day, quote_time, rates or dict.fromkeys(SOURCE_TERMS, Decimal("0.1"))
    )
    result = _publish_date(session, [raw], parameters or ScenarioParameters(), run.id)
    assert result[3] is None
    return run.id


@pytest.fixture
def client(db):
    app = create_app(Settings(scheduler_enabled=False))
    with Session(bind=db) as session:

        def dependency():
            yield session

        app.dependency_overrides[get_db] = dependency
        with TestClient(app) as client:
            yield client, session


def test_raw_single_date_range_precision_and_multiple_snapshots(client):
    http, session = client
    seed(
        session,
        rates={
            Decimal(1): Decimal("0.123456789012345678901234567890"),
            Decimal(5): Decimal("0.14"),
        },
    )
    seed(session, quote_time=time(19))
    response = http.get("/curves/raw", params={"date": str(DAY)})
    assert response.status_code == 200
    body = response.json()
    assert len(body["snapshots"]) == 2 and body["missing_dates"] == []
    point = body["snapshots"][0]["points"][0]
    assert point["yield_rate"] == "0.123456789012345678901234567890"
    assert len(body["snapshots"][0]["points"]) == 2
    response = http.get(
        "/curves/raw", params={"date_from": "2026-10-29", "date_to": "2026-10-30"}
    )
    assert response.json()["missing_dates"] == ["2026-10-29"]
    assert len(response.json()["snapshots"]) == 2


def test_processed_returns_seven_sorted_curves_with_flags(client):
    http, session = client
    seed(session)
    seed(session, day=date(2026, 10, 29))
    response = http.get("/curves/processed", params={"date": str(DAY)})
    assert response.status_code == 200
    snapshots = response.json()["snapshots"]
    assert len(snapshots) == 1
    curves = snapshots[0]["curves"]
    assert len(curves) == 7 and curves[0]["scenario"] == "Baseline"
    assert curves[0]["scenario_version"] is None
    for curve in curves:
        assert len(curve["points"]) == 19
        terms = [Decimal(p["tenor"]) for p in curve["points"]]
        assert terms == sorted(terms)
        assert curve["points"][0]["is_extrapolated"] is True
    assert Decimal(curves[1]["points"][0]["yield_rate"]) == Decimal("0.14")
    response = http.get(
        "/curves/processed", params={"date_from": "2026-10-28", "date_to": "2026-10-30"}
    )
    assert len(response.json()["snapshots"]) == 2
    assert response.json()["missing_dates"] == ["2026-10-28"]


def test_processed_selects_one_complete_version_and_supports_explicit_version(client):
    http, session = client
    old = ScenarioParameters()
    new = ScenarioParameters(parallel_bps=500)
    old_run = seed(session, parameters=old)
    seed(session, parameters=new)
    session.execute(
        update(EtlRun)
        .where(EtlRun.id == old_run)
        .values(started_at=func.now() - text("interval '1 hour'"))
    )

    def versions(params):
        response = http.get("/curves/processed", params=params)
        assert response.status_code == 200
        return {
            curve["scenario_version"]
            for curve in response.json()["snapshots"][0]["curves"]
        }

    assert versions({"date": str(DAY)}) == {None, new.version}
    assert versions({"date": str(DAY), "scenario_version": old.version}) == {
        None,
        old.version,
    }
    assert (
        http.get(
            "/curves/processed",
            params={"date": str(DAY), "scenario_version": "missing"},
        ).json()["snapshots"]
        == []
    )
    # A partial calibration must never be mixed with another one to make seven curves.
    session.execute(
        delete(ProcessedCurvePoint).where(
            ProcessedCurvePoint.scenario_version == new.version,
            ProcessedCurvePoint.scenario == "Short_Up",
        )
    )
    assert versions({"date": str(DAY)}) == {None, old.version}


def test_soft_deleted_or_incomplete_results_are_not_published(client):
    http, session = client
    seed(session)
    session.execute(
        update(ProcessedCurvePoint)
        .where(
            ProcessedCurvePoint.scenario == "Baseline", ProcessedCurvePoint.tenor == 1
        )
        .values(is_deleted=True)
    )
    body = http.get("/curves/processed", params={"date": str(DAY)}).json()
    assert body["snapshots"] == [] and body["missing_dates"] == [str(DAY)]
    assert (
        len(http.get("/curves/raw", params={"date": str(DAY)}).json()["snapshots"]) == 1
    )


@pytest.mark.parametrize("path", ["raw", "processed"])
def test_empty_database_returns_missing_dates_without_fabrication(client, path):
    http, session = client
    before = session.scalars(select(EtlRun.id)).all()
    response = http.get(f"/curves/{path}", params={"date": str(DAY)})
    assert response.status_code == 200
    assert response.json()["snapshots"] == []
    assert response.json()["missing_dates"] == [str(DAY)]
    assert session.scalars(select(EtlRun.id)).all() == before
    assert session.scalars(select(RawCurvePoint.id)).all() == []


@pytest.mark.parametrize("path", ["raw", "processed"])
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"date": "bad-date"},
        {"date_from": "2026-10-30"},
        {"date_from": "2026-10-30", "date_to": "2026-10-29"},
        {"date": "2026-10-30", "date_from": "2026-10-29", "date_to": "2026-10-30"},
    ],
)
def test_invalid_dates_are_rejected_before_database_access(path, params):
    app = create_app(Settings(scheduler_enabled=False))

    # None cannot execute a query; invalid input must never reach the handler.
    def unused_session():
        yield None

    app.dependency_overrides[get_db] = unused_session
    with TestClient(app) as http:
        assert http.get(f"/curves/{path}", params=params).status_code == 422


def test_database_errors_are_503_and_openapi_has_both_routes():
    app = create_app(Settings(scheduler_enabled=False))

    def broken():
        raise OperationalError("query", {}, RuntimeError("private connection details"))
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = broken
    with TestClient(app) as http:
        response = http.get("/curves/raw", params={"date": str(DAY)})
        assert response.status_code == 503 and "private" not in response.text
        assert set(http.get("/openapi.json").json()["paths"]) == {
            "/curves/raw",
            "/curves/processed",
        }
