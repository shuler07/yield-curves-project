import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import sessionmaker

from src.api import loading
from src.api.main import create_app
from src.api.settings import Settings
from src.db.models import EtlRun, ProcessedCurvePoint, RawCurvePoint
from src.db.session import get_db
from src.etl import pipeline
from src.etl.calculations import ScenarioParameters
from src.etl.common import SOURCE_TERMS, DateRange, write_manifest
from src.etl.merge import merge_run

DAY = date(2026, 10, 30)


@pytest.fixture
def api(migrated_db, tmp_path, monkeypatch):
    engine, _ = migrated_db
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    calls = []
    control = {"empty": set(), "sparse": False, "error": None, "wait": None}

    def collect(dates, run_dir, timeout):
        calls.append(dates)
        if control["wait"]:
            control["wait"]()
        if control["error"]:
            raise control["error"]
        folder = tmp_path / uuid4().hex
        folder.mkdir()
        path = folder / "curve.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(
                ["tradedate", "tradetime", *(f"period_{t}" for t in SOURCE_TERMS)]
            )
            for day in dates.days():
                if day not in control["empty"]:
                    rates = ["10"] + ["NA"] * 11 if control["sparse"] else ["10"] * 12
                    writer.writerow([day.strftime("%d.%m.%Y"), "18:00:00", *rates])
        write_manifest(folder, dates, [path])
        return merge_run(folder)

    monkeypatch.setattr(pipeline, "collect_curves", collect)
    app = create_app(Settings(scheduler_enabled=False, timeout_seconds=3))

    def database():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app) as http:
            yield http, factory, calls, control, app
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


@pytest.mark.parametrize("path", ["raw", "processed"])
def test_missing_date_is_loaded_calculated_and_then_served_without_refetch(api, path):
    http, factory, calls, _, _ = api
    result = http.get(f"/curves/{path}", params={"date": str(DAY)})
    assert result.status_code == 200, result.text
    assert result.json()["missing_dates"] == []
    assert len(result.json()["snapshots"]) == 1
    assert calls == [DateRange(DAY, DAY)]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(RawCurvePoint)) == 12
        assert (
            session.scalar(select(func.count()).select_from(ProcessedCurvePoint)) == 133
        )
        run = session.scalar(select(EtlRun))
        assert run.run_type == "manual" and run.status == "success"
    # Loading either endpoint populates both the original and processed data.
    for endpoint in ("raw", "processed"):
        result = http.get(f"/curves/{endpoint}", params={"date": str(DAY)})
        assert result.status_code == 200 and result.json()["missing_dates"] == []
    assert len(calls) == 1


def test_only_missing_contiguous_ranges_are_fetched(api):
    http, _, calls, _, _ = api
    assert http.get("/curves/raw", params={"date": "2026-10-29"}).status_code == 200
    result = http.get(
        "/curves/processed", params={"date_from": "2026-10-27", "date_to": "2026-10-31"}
    )
    assert result.status_code == 200 and len(result.json()["snapshots"]) == 5
    assert calls[1:] == [
        DateRange(date(2026, 10, 27), date(2026, 10, 28)),
        DateRange(DAY, date(2026, 10, 31)),
    ]


def test_source_without_quotes_returns_missing_dates_and_no_data_audit(api):
    http, factory, _, control, _ = api
    control["empty"].add(DAY)
    result = http.get("/curves/processed", params={"date": str(DAY)})
    assert result.status_code == 200
    assert result.json()["snapshots"] == [] and result.json()["missing_dates"] == [
        str(DAY)
    ]
    with factory() as session:
        run = session.scalar(select(EtlRun))
        assert run.status == "no_data" and run.run_type == "manual"
        assert session.scalar(select(func.count()).select_from(RawCurvePoint)) == 0


def test_source_failure_is_not_silently_reported_as_missing_data(api):
    http, factory, _, control, _ = api
    control["error"] = TimeoutError("private source diagnostic")
    result = http.get("/curves/raw", params={"date": str(DAY)})
    assert result.status_code == 502 and "private source diagnostic" not in result.text
    with factory() as session:
        run = session.scalar(select(EtlRun))
        assert run.status == "failed" and "private source diagnostic" in run.error
        assert result.json()["detail"]["runs"][0]["etl_run_id"] == str(run.id)
    control["error"] = None
    assert http.get("/curves/raw", params={"date": str(DAY)}).status_code == 200


def test_raw_observations_are_returned_even_if_there_are_too_few_to_calculate(api):
    http, factory, _, control, _ = api
    control["sparse"] = True
    result = http.get("/curves/raw", params={"date": str(DAY)})
    assert (
        result.status_code == 200 and len(result.json()["snapshots"][0]["points"]) == 1
    )
    result = http.get("/curves/processed", params={"date": str(DAY)})
    assert result.status_code == 502
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(RawCurvePoint)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ProcessedCurvePoint)) == 0
        )


def test_incomplete_processed_date_is_repaired(api):
    http, factory, calls, _, _ = api
    http.get("/curves/processed", params={"date": str(DAY)})
    with factory.begin() as session:
        session.execute(
            delete(ProcessedCurvePoint).where(
                ProcessedCurvePoint.scenario == "Short_Up",
                ProcessedCurvePoint.tenor == 1,
            )
        )
    result = http.get("/curves/processed", params={"date": str(DAY)})
    assert (
        result.status_code == 200 and len(result.json()["snapshots"][0]["curves"]) == 7
    )
    assert len(calls) == 2


def test_known_calibration_is_reused_and_unknown_version_does_not_fetch_source(api):
    http, factory, calls, _, _ = api
    custom = ScenarioParameters(parallel_bps=500)
    pipeline.run_pipeline(
        DateRange(DAY, DAY), parameters=custom, session_factory=factory
    )
    previous = date(2026, 10, 29)
    result = http.get(
        "/curves/processed",
        params={"date": str(previous), "scenario_version": custom.version},
    )
    assert result.status_code == 200
    assert {
        curve["scenario_version"] for curve in result.json()["snapshots"][0]["curves"]
    } == {None, custom.version}
    count = len(calls)
    result = http.get(
        "/curves/processed",
        params={"date": "2026-10-28", "scenario_version": "unknown"},
    )
    assert result.status_code == 404 and len(calls) == count


def test_parallel_requests_recheck_after_lock_instead_of_downloading_twice(
    api, monkeypatch
):
    http, _, calls, control, _ = api
    collecting, second_waiting, release = Event(), Event(), Event()

    def wait():
        collecting.set()
        assert release.wait(3)

    control["wait"] = wait
    original_lock = loading._collector_lock
    attempts = []

    @contextmanager
    def lock(*args):
        attempts.append(1)
        if len(attempts) == 2:
            second_waiting.set()
        with original_lock(*args):
            yield

    monkeypatch.setattr(loading, "_collector_lock", lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            http.get, "/curves/processed", params={"date": str(DAY)}
        )
        assert collecting.wait(3)
        second = executor.submit(http.get, "/curves/raw", params={"date": str(DAY)})
        try:
            assert second_waiting.wait(3)
        finally:
            release.set()
        assert (
            first.result(timeout=5).status_code
            == second.result(timeout=5).status_code
            == 200
        )
    assert len(calls) == 1


def test_waiting_for_another_collector_has_a_timeout(api):
    http, factory, calls, _, app = api
    app.state.settings = Settings(scheduler_enabled=False, timeout_seconds=0.02)
    with factory.kw["bind"].connect() as guard:
        guard.execute(text("SELECT pg_advisory_lock(206, 1)"))
        guard.commit()
        try:
            result = http.get("/curves/raw", params={"date": str(DAY)})
            assert result.status_code == 504 and calls == []
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(206, 1)"))
            guard.commit()
