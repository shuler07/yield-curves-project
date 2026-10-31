"""Pure calculations for the future database loader; no CSV or database writes.

Rates are decimal fractions, tenors are years. CSV percentages must be divided
by 100 at the input boundary. Missing quotes are restored in memory only.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import date, time
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from uuid import UUID

from src.etl.common import TERMS, DataValidationError

PRECISION = 38
SCENARIOS = (
    "Parallel_Up",
    "Parallel_Down",
    "Short_Up",
    "Short_Down",
    "Steepener",
    "Flattener",
)


def _decimal(value, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataValidationError(
            f"{label}: ожидалось число, получено {value!r}"
        ) from exc
    if not result.is_finite():
        raise DataValidationError(f"{label}: нужно конечное число")
    return result


def discount_factor(
    rate: Decimal,
    tenor: Decimal,
) -> Decimal:
    rate, tenor = _decimal(rate, "Ставка"), _decimal(tenor, "Срок")
    if tenor < 0:
        raise DataValidationError("Срок дисконтирования не может быть отрицательным")
    if rate <= -1:
        raise DataValidationError(
            "Для годовой капитализации ставка должна быть больше -100%"
        )
    try:
        with localcontext() as context:
            context.prec = PRECISION
            result = (Decimal(1) + rate) ** (-tenor)
    except DecimalException as exc:
        raise DataValidationError(
            "Не удалось вычислить коэффициент дисконтирования"
        ) from exc
    if not result.is_finite() or result <= 0:
        raise DataValidationError(
            "Коэффициент дисконтирования должен быть положительным конечным числом"
        )
    return result


@dataclass(frozen=True)
class ScenarioParameters:
    # RUB amplitudes from the scenario table supplied by the project curator.
    parallel_bps: Decimal = Decimal(400)
    short_bps: Decimal = Decimal(500)
    long_bps: Decimal = Decimal(300)
    decay_years: Decimal = Decimal(4)
    steepener_short: Decimal = Decimal("0.65")
    steepener_long: Decimal = Decimal("0.9")
    flattener_short: Decimal = Decimal("0.8")
    flattener_long: Decimal = Decimal("0.6")
    floor: Decimal | None = Decimal(0)

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "floor" and value is None:
                continue
            value = _decimal(value, field.name)
            object.__setattr__(self, field.name, value)
            if field.name == "floor":
                if value > 0:
                    raise DataValidationError(
                        "Нижняя граница стрессовых ставок должна быть <= 0"
                    )
            elif value < 0 or (field.name == "decay_years" and value == 0):
                raise DataValidationError(f"Некорректный параметр {field.name}")

    def payload(self) -> dict:
        with localcontext() as context:
            context.prec = PRECISION
            result = {
                field.name: None
                if getattr(self, field.name) is None
                else format(getattr(self, field.name).normalize(), "f")
                for field in fields(self)
            }
        return {
            "formula_version": "irrbb_shapes_v1",
            "rate_unit": "fraction",
            "shock_unit": "basis_points",
            **result,
        }

    @property
    def version(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return "irrbb_v1_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class RawSnapshot:
    curve_date: date
    quote_time: time
    rates: Mapping[Decimal, Decimal | None]


@dataclass(frozen=True)
class BasePoint:
    curve_date: date
    quote_time: time
    tenor: Decimal
    yield_rate: Decimal
    discount_factor: Decimal
    is_interpolated: bool = False
    is_extrapolated: bool = False
    is_deleted: bool = False

    def record(self, etl_run_id: UUID) -> dict:
        return {**asdict(self), "etl_run_id": etl_run_id}


@dataclass(frozen=True)
class StressPoint:
    tenor: Decimal
    stressed_yield: Decimal
    discount_factor: Decimal

    def record(
        self, curve_point_id: int, stress_shock_id: int, etl_run_id: UUID
    ) -> dict:
        return {
            "curve_point_id": curve_point_id,
            "stress_shock_id": stress_shock_id,
            "stressed_yield": self.stressed_yield,
            "discount_factor": self.discount_factor,
            "etl_run_id": etl_run_id,
        }


@dataclass(frozen=True)
class ScenarioCurve:
    scenario: str
    parameters: ScenarioParameters
    points: tuple[StressPoint, ...]

    def definition_record(self) -> dict:
        return {
            "scenario": self.scenario,
            "version": self.parameters.version,
            "parameters": self.parameters.payload(),
        }


@dataclass(frozen=True)
class CalculatedSnapshot:
    curve_date: date
    quote_time: time
    base_points: tuple[BasePoint, ...]
    scenarios: tuple[ScenarioCurve, ...]


def prepare_base_curve(
    snapshot: RawSnapshot,
) -> tuple[BasePoint, ...]:
    """Called at the load boundary, before inserting base points, never by parse.

    Missing rates require at least two original observations of this very
    snapshot. Interior gaps are linear interpolation; edges use the two
    nearest original points for linear extrapolation. No temporal fallback.
    """
    known = {}
    seen = set()
    for tenor, rate in snapshot.rates.items():
        tenor = _decimal(tenor, "Срок")
        if tenor not in TERMS or tenor in seen:
            raise DataValidationError(f"Неожиданный или повторный срок {tenor}")
        seen.add(tenor)
        if rate is not None:
            known[tenor] = _decimal(rate, "Ставка")
    if len(known) < 2:
        raise DataValidationError(
            f"{snapshot.curve_date} {snapshot.quote_time}: нужно минимум две известные ставки"
        )
    anchors = sorted(known)
    result = []
    with localcontext() as context:
        context.prec = PRECISION
        for tenor in TERMS:
            interpolated = extrapolated = False
            if tenor in known:
                rate = known[tenor]
            else:
                index = bisect_left(anchors, tenor)
                if index == 0:
                    left, right = anchors[:2]
                    extrapolated = True
                elif index == len(anchors):
                    left, right = anchors[-2:]
                    extrapolated = True
                else:
                    left, right = anchors[index - 1 : index + 1]
                    interpolated = True
                rate = known[left] + (known[right] - known[left]) * (tenor - left) / (
                    right - left
                )
            result.append(
                BasePoint(
                    snapshot.curve_date,
                    snapshot.quote_time,
                    tenor,
                    rate,
                    discount_factor(rate, tenor),
                    interpolated,
                    extrapolated,
                )
            )
    return tuple(result)


def calculate_scenarios(
    base_points: Sequence[BasePoint], parameters: ScenarioParameters | None = None
) -> tuple[ScenarioCurve, ...]:
    """Calculate scenarios from restored base rates, independent of DB/Excel."""
    parameters = parameters or ScenarioParameters()
    if len(base_points) != len(TERMS) or {point.tenor for point in base_points} != set(
        TERMS
    ):
        raise DataValidationError(
            "Для сценариев нужна базовая кривая со всеми 12 сроками"
        )
    if len({(point.curve_date, point.quote_time) for point in base_points}) != 1:
        raise DataValidationError("Нельзя смешивать точки разных снимков")
    points = {name: [] for name in SCENARIOS}
    try:
        with localcontext() as context:
            context.prec = PRECISION
            parallel = parameters.parallel_bps / Decimal(10000)
            for base in sorted(base_points, key=lambda point: point.tenor):
                rate = _decimal(base.yield_rate, "Базовая ставка")
                decay = (-base.tenor / parameters.decay_years).exp()
                short = parameters.short_bps / Decimal(10000) * decay
                long = parameters.long_bps / Decimal(10000) * (1 - decay)
                shifts = {
                    "Parallel_Up": parallel,
                    "Parallel_Down": -parallel,
                    "Short_Up": short,
                    "Short_Down": -short,
                    "Steepener": -parameters.steepener_short * short
                    + parameters.steepener_long * long,
                    "Flattener": parameters.flattener_short * short
                    - parameters.flattener_long * long,
                }
                for name, shift in shifts.items():
                    stressed = rate + shift
                    if parameters.floor is not None:
                        stressed = max(stressed, parameters.floor)
                    points[name].append(
                        StressPoint(
                            base.tenor,
                            stressed,
                            discount_factor(stressed, base.tenor),
                        )
                    )
    except DecimalException as exc:
        raise DataValidationError("Численная ошибка расчёта сценариев") from exc
    return tuple(
        ScenarioCurve(name, parameters, tuple(points[name])) for name in SCENARIOS
    )


def calculate_snapshot(
    snapshot: RawSnapshot,
    parameters: ScenarioParameters | None = None,
) -> CalculatedSnapshot:
    base = prepare_base_curve(snapshot)
    return CalculatedSnapshot(
        snapshot.curve_date,
        snapshot.quote_time,
        base,
        calculate_scenarios(base, parameters),
    )
