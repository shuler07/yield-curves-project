"""CSV input and optional Excel output around the independent calculation layer."""

from __future__ import annotations

import csv
import json
import math
import tempfile
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import click

from src.etl.calculations import (
    PRECISION,
    SCENARIOS,
    CalculatedSnapshot,
    RawSnapshot,
    ScenarioParameters,
    calculate_snapshot,
)
from src.etl.common import (
    MISSING_RATE_TOKENS,
    OUTPUT_NAME,
    TERMS,
    DataValidationError,
    NoDataError,
)


def read_snapshots(input_file: Path) -> tuple[RawSnapshot, ...]:
    """Read merged CSV; convert percentages to fractions, NA to None.

    The source CSV is never modified, and this adapter does not fill gaps.
    """
    input_file = Path(input_file)
    snapshots = []
    seen = set()
    with input_file.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, delimiter=";", strict=True)
        try:
            header = next(reader, [])
            if len(header) != len(TERMS) + 2 or header[:2] != [
                "tradedate",
                "tradetime",
            ]:
                raise ValueError(
                    "ожидался merged CSV: tradedate;tradetime и все 12 period_..."
                )
            terms = []
            for column in header[2:]:
                if not column.startswith("period_"):
                    raise ValueError(f"неизвестная колонка {column!r}")
                term = Decimal(column.removeprefix("period_"))
                if not term.is_finite() or term not in TERMS or term in terms:
                    raise ValueError(f"неожиданный или повторный срок {column!r}")
                terms.append(term)
            for row in reader:
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) != len(header):
                    raise ValueError("число ячеек не совпадает с заголовком")
                key = datetime.strptime(f"{row[0]} {row[1]}", "%d.%m.%Y %H:%M:%S")
                if key in seen:
                    raise ValueError(f"повторный снимок {key}")
                seen.add(key)
                rates = {}
                with localcontext() as context:
                    context.prec = PRECISION
                    for term, value in zip(terms, row[2:], strict=True):
                        if value.strip().upper() in MISSING_RATE_TOKENS:
                            rates[term] = None
                            continue
                        rate = Decimal(value.strip().replace(",", "."))
                        if not rate.is_finite():
                            raise ValueError(
                                f"ставка для срока {term} должна быть конечной"
                            )
                        rates[term] = rate / Decimal(100)
                snapshots.append(RawSnapshot(key.date(), key.time(), rates))
        except (ValueError, InvalidOperation, csv.Error) as exc:
            raise DataValidationError(
                f"{input_file.name}, строка {reader.line_num}: {exc}"
            ) from exc
    if not snapshots:
        raise NoDataError(f"В {input_file} нет снимков для расчёта")
    return tuple(
        sorted(
            snapshots, key=lambda snapshot: (snapshot.curve_date, snapshot.quote_time)
        )
    )


def calculate_history(
    input_file: Path,
    parameters: ScenarioParameters | None = None,
) -> tuple[CalculatedSnapshot, ...]:
    return tuple(
        calculate_snapshot(snapshot, parameters)
        for snapshot in read_snapshots(input_file)
    )


def _excel_value(value):
    if isinstance(value, Decimal):
        number = float(value)
        if not math.isfinite(number):
            raise DataValidationError("Значение не представимо числом Excel")
        return number
    return value


def export_history_xlsx(
    history: Sequence[CalculatedSnapshot], output_file: Path
) -> Path:
    """Export without rounding or mutating the Decimal calculation objects."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    output_file = Path(output_file)
    if output_file.suffix.lower() != ".xlsx":
        raise DataValidationError("Файл Excel должен иметь расширение .xlsx")
    if not history:
        raise DataValidationError("Нет рассчитанных снимков для экспорта")
    workbook = Workbook()
    workbook.remove(workbook.active)
    baseline = workbook.create_sheet("Baseline")
    baseline.append(
        [
            "curve_date",
            "quote_time",
            "tenor",
            "yield_rate",
            "discount_factor",
            "is_interpolated",
            "is_extrapolated",
            "is_deleted",
        ]
    )
    scenario_sheets = {}
    for name in SCENARIOS:
        sheet = workbook.create_sheet(name)
        sheet.append(
            [
                "curve_date",
                "quote_time",
                "tenor",
                "base_yield",
                "stressed_yield",
                "discount_factor",
                "scenario_version",
            ]
        )
        scenario_sheets[name] = sheet
    definitions = {}
    for snapshot in history:
        base_by_tenor = {point.tenor: point for point in snapshot.base_points}
        for point in snapshot.base_points:
            baseline.append(
                [
                    _excel_value(value)
                    for value in (
                        point.curve_date.isoformat(),
                        point.quote_time.isoformat(),
                        point.tenor,
                        point.yield_rate,
                        point.discount_factor,
                        point.is_interpolated,
                        point.is_extrapolated,
                        point.is_deleted,
                    )
                ]
            )
        for curve in snapshot.scenarios:
            definitions[(curve.scenario, curve.parameters.version)] = (
                curve.definition_record()
            )
            for point in curve.points:
                scenario_sheets[curve.scenario].append(
                    [
                        _excel_value(value)
                        for value in (
                            snapshot.curve_date.isoformat(),
                            snapshot.quote_time.isoformat(),
                            point.tenor,
                            base_by_tenor[point.tenor].yield_rate,
                            point.stressed_yield,
                            point.discount_factor,
                            curve.parameters.version,
                        )
                    ]
                )
    metadata = workbook.create_sheet("Parameters")
    metadata.append(["scenario", "version", "parameters_json"])
    for definition in definitions.values():
        metadata.append(
            [
                definition["scenario"],
                definition["version"],
                json.dumps(definition["parameters"], sort_keys=True),
            ]
        )
    for sheet in workbook:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24476B")
        for index, heading in enumerate(sheet[1], start=1):
            sheet.column_dimensions[get_column_letter(index)].width = min(
                max(len(heading.value) + 2, 16), 44
            )
            for column in sheet.iter_cols(min_col=index, max_col=index, min_row=2):
                for cell in column:
                    if heading.value in {"yield_rate", "base_yield", "stressed_yield"}:
                        cell.number_format = "0.00%"
                    elif heading.value == "discount_factor":
                        cell.number_format = "0.000000000000"
    metadata.column_dimensions["C"].width = 80
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_file.parent, suffix=".xlsx", delete=False
        ) as stream:
            temporary = Path(stream.name)
        workbook.save(temporary)
        temporary.replace(output_file)
    finally:
        workbook.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output_file


def generate_irrbb_history(
    input_file: Path,
    output_file: Path | None = None,
    parameters: ScenarioParameters | None = None,
) -> tuple[CalculatedSnapshot, ...]:
    """Compatibility wrapper for explicitly requested Excel history exports."""
    history = calculate_history(input_file, parameters)
    output = (
        Path(output_file)
        if output_file is not None
        else Path(input_file).parent / "IRRBB_Scenarios_History.xlsx"
    )
    export_history_xlsx(history, output)
    return history


@click.command()
@click.option(
    "--run-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Каталог сбора с merged_zcyc_ranges.csv.",
)
@click.option(
    "--input",
    "input_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Явный путь к merged CSV вместо --run-dir.",
)
@click.option(
    "--xlsx",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Необязательный экспорт рассчитанных значений в Excel.",
)
@click.option(
    "--shock-bps",
    default=None,
    help="Явно задать одинаковый шок для всех трёх типов, б.п.; без ключа RUB: 400/500/300.",
)
def main(run_dir, input_file, xlsx, shock_bps):
    """Рассчитать базовую и шесть стрессовых кривых в памяти; CSV не изменяется."""
    if (run_dir is None) == (input_file is None):
        raise click.UsageError("Нужен либо --run-dir, либо --input")
    source = run_dir / OUTPUT_NAME if run_dir is not None else input_file
    try:
        parameters = (
            ScenarioParameters()
            if shock_bps is None
            else ScenarioParameters(
                parallel_bps=shock_bps, short_bps=shock_bps, long_bps=shock_bps
            )
        )
        history = calculate_history(source, parameters)
        click.echo(
            f"Снимков: {len(history)}; базовых точек: {len(history) * len(TERMS)}; стрессовых точек: {len(history) * len(TERMS) * len(SCENARIOS)}"
        )
        points = [point for snapshot in history for point in snapshot.base_points]
        click.echo(
            f"Интерполировано: {sum(point.is_interpolated for point in points)}; экстраполировано: {sum(point.is_extrapolated for point in points)}"
        )
        click.echo(
            f"Дисконтирование: годовая капитализация; версия сценариев: {parameters.version}"
        )
        if xlsx is not None:
            export_history_xlsx(history, xlsx)
            click.echo(f"Excel: {xlsx}")
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
