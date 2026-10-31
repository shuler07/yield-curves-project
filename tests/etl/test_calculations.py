from copy import deepcopy
from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from uuid import uuid4

import pytest

from src.etl.calculations import (
    SCENARIOS,
    RawSnapshot,
    ScenarioParameters,
    calculate_scenarios,
    calculate_snapshot,
    discount_factor,
    prepare_base_curve,
)
from src.etl.common import TERMS, DataValidationError


def snapshot(rates=None):
    return RawSnapshot(
        date(2026, 10, 30),
        time(18, 54, 59),
        rates if rates is not None else {term: Decimal("0.1") for term in TERMS},
    )


def assert_close(actual, expected, tolerance="1e-35"):
    assert abs(actual - Decimal(expected)) < Decimal(tolerance)


def test_annual_discount_uses_fractional_rates_and_fractional_years():
    assert_close(
        discount_factor(Decimal("0.1"), Decimal(1)),
        "0.90909090909090909090909090909090909091",
    )
    assert_close(
        discount_factor(Decimal("0.1"), Decimal(2)),
        "0.82644628099173553719008264462809917355",
    )
    assert_close(
        discount_factor(Decimal("0.21"), Decimal("0.5")),
        "0.90909090909090909090909090909090909091",
    )
    assert discount_factor(Decimal(0), Decimal(20)) == 1
    assert discount_factor(Decimal("0.1"), Decimal(0)) == 1
    assert discount_factor(Decimal("-0.01"), Decimal(1)) > 1


@pytest.mark.parametrize(
    "rate,tenor",
    [
        ("-1", "1"),
        ("-1.01", "1"),
        ("NaN", "1"),
        ("Infinity", "1"),
        ("0.1", "-1"),
        ("0.1", "NaN"),
    ],
)
def test_invalid_discount_inputs_rejected(rate, tenor):
    with pytest.raises(DataValidationError):
        discount_factor(Decimal(rate), Decimal(tenor))


def test_restore_rates_and_flags_using_original_neighbouring_points_only():
    rates = {
        Decimal(1): Decimal("0.1"),
        Decimal(3): Decimal("0.14"),
        Decimal(10): Decimal("0.14"),
    }
    before = dict(rates)
    points = {point.tenor: point for point in prepare_base_curve(snapshot(rates))}
    assert rates == before
    assert set(points) == set(TERMS)
    assert points[Decimal(2)].yield_rate == Decimal("0.12")
    assert points[Decimal(2)].is_interpolated
    assert not points[Decimal(2)].is_extrapolated
    assert points[Decimal("0.25")].yield_rate == Decimal("0.085")
    assert points[Decimal("0.25")].is_extrapolated
    assert points[Decimal(20)].yield_rate == Decimal("0.14")
    assert points[Decimal(20)].is_extrapolated
    assert not points[Decimal(1)].is_interpolated
    assert not points[Decimal(1)].is_extrapolated
    assert not any(point.is_deleted for point in points.values())
    assert points[Decimal(5)].yield_rate == Decimal("0.14")


def test_zero_is_an_observation_and_negative_extrapolation_is_preserved():
    points = {
        point.tenor: point
        for point in prepare_base_curve(
            snapshot({Decimal(1): Decimal(0), Decimal(3): Decimal("0.02")})
        )
    }
    assert points[Decimal(1)].yield_rate == 0
    assert not points[Decimal(1)].is_interpolated
    assert points[Decimal("0.25")].yield_rate == Decimal("-0.0075")
    assert points[Decimal("0.25")].discount_factor > 1


@pytest.mark.parametrize(
    "rates", [{}, {Decimal(1): Decimal("0.1")}, {term: None for term in TERMS}]
)
def test_less_than_two_observations_fails_with_snapshot_context(rates):
    with pytest.raises(DataValidationError, match="2026-10-30.*две"):
        prepare_base_curve(snapshot(rates))


@pytest.mark.parametrize(
    "rates",
    [
        {Decimal(30): Decimal("0.1"), Decimal(1): Decimal("0.12")},
        {Decimal(1): Decimal("NaN"), Decimal(3): Decimal("0.12")},
    ],
)
def test_invalid_observed_points_are_not_interpolated(rates):
    with pytest.raises(DataValidationError):
        prepare_base_curve(snapshot(rates))


def test_six_scenarios_and_their_own_discount_factors():
    result = calculate_snapshot(snapshot())
    assert tuple(curve.scenario for curve in result.scenarios) == SCENARIOS
    assert len(result.base_points) == 12
    assert all(len(curve.points) == 12 for curve in result.scenarios)
    point = {
        curve.scenario: next(point for point in curve.points if point.tenor == 1)
        for curve in result.scenarios
    }
    assert point["Parallel_Up"].stressed_yield == Decimal("0.14")
    assert point["Parallel_Down"].stressed_yield == Decimal("0.06")
    assert_close(
        point["Parallel_Up"].discount_factor, "0.87719298245614035087719298245614035088"
    )
    assert point["Parallel_Up"].discount_factor != result.base_points[3].discount_factor
    # Reference e^-1 for t=4, independent of the implementation's exponential.
    decay = Decimal("0.36787944117144232159552377016146086745")
    at_four = {
        curve.scenario: next(
            point for point in curve.points if point.tenor == 4
        ).stressed_yield
        for curve in result.scenarios
    }
    assert_close(at_four["Short_Up"], Decimal("0.1") + Decimal("0.05") * decay, "1e-27")
    assert_close(
        at_four["Short_Down"], Decimal("0.1") - Decimal("0.05") * decay, "1e-27"
    )
    assert_close(
        at_four["Steepener"],
        Decimal("0.1") - Decimal("0.0325") * decay + Decimal("0.027") * (1 - decay),
        "1e-27",
    )
    assert_close(
        at_four["Flattener"],
        Decimal("0.1") + Decimal("0.04") * decay - Decimal("0.018") * (1 - decay),
        "1e-27",
    )


def test_independent_short_long_amplitudes():
    params = ScenarioParameters(short_bps=600, long_bps=200, parallel_bps=400)
    curves = calculate_scenarios(prepare_base_curve(snapshot()), params)
    point = next(p for p in curves[2].points if p.tenor == 4)
    assert_close(point.stressed_yield, "0.12207276647028653929573142620968765205")
    assert curves[0].points[0].stressed_yield == Decimal("0.14")


def test_floor_is_applied_to_all_scenarios_but_not_baseline():
    result = calculate_snapshot(snapshot({term: Decimal("-0.5") for term in TERMS}))
    assert all(point.yield_rate == Decimal("-0.5") for point in result.base_points)
    assert all(
        point.stressed_yield == 0 and point.discount_factor == 1
        for curve in result.scenarios
        for point in curve.points
    )
    unfloored = calculate_snapshot(
        snapshot({term: Decimal("-0.5") for term in TERMS}),
        ScenarioParameters(floor=None),
    )
    assert all(
        point.stressed_yield < 0
        for curve in unfloored.scenarios
        for point in curve.points
    )


def test_scenario_parameter_versions_are_reproducible_and_json_serializable():
    params = ScenarioParameters()
    assert params.version == ScenarioParameters(parallel_bps=Decimal("400.00")).version
    assert params.version != ScenarioParameters(short_bps=450).version
    assert params.version != ScenarioParameters(floor=None).version
    definition = calculate_snapshot(snapshot(), params).scenarios[0].definition_record()
    assert definition["scenario"] == "Parallel_Up"
    assert definition["version"] == params.version
    assert definition["parameters"]["parallel_bps"] == "400"
    assert definition["parameters"]["short_bps"] == "500"
    assert definition["parameters"]["long_bps"] == "300"
    assert definition["parameters"]["floor"] == "0"
    import json

    json.dumps(definition)


@pytest.mark.parametrize(
    "kwargs",
    [{"decay_years": 0}, {"short_bps": -1}, {"parallel_bps": "NaN"}, {"floor": "0.01"}],
)
def test_invalid_scenario_parameters_rejected(kwargs):
    with pytest.raises(DataValidationError):
        ScenarioParameters(**kwargs)


def test_scenario_input_cannot_mix_snapshots_or_skip_tenors():
    points = prepare_base_curve(snapshot())
    with pytest.raises(DataValidationError):
        calculate_scenarios(points[:-1])
    with pytest.raises(DataValidationError):
        calculate_scenarios((replace(points[0], quote_time=time(17)), *points[1:]))


def test_records_match_database_columns_without_mutating_results():
    result = calculate_snapshot(
        snapshot({Decimal(1): Decimal("0.1"), Decimal(5): Decimal("0.14")})
    )
    before = deepcopy(result)
    run_id = uuid4()
    base_record = result.base_points[0].record(run_id)
    assert base_record["is_extrapolated"] is True
    assert base_record["is_interpolated"] is False
    assert base_record["etl_run_id"] == run_id
    record = result.scenarios[0].points[0].record(123, 456, run_id)
    assert record["curve_point_id"] == 123
    assert record["stress_shock_id"] == 456
    assert "tenor" not in record
    assert result == before
