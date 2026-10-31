import csv
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner
from openpyxl import load_workbook

from src.etl.calculations import SCENARIOS, ScenarioParameters
from src.etl.common import OUTPUT_NAME, TERMS, DataValidationError, DateRange
from src.etl.merge import extract_and_merge
from src.etl.postprocessing import (
    calculate_history,
    export_history_xlsx,
    generate_irrbb_history,
    main,
    read_snapshots,
)


@pytest.fixture
def merged_file(tmp_path, moex_csv):
    raw = tmp_path / "raw.csv"
    raw.write_text(moex_csv, encoding="utf-8")
    return extract_and_merge(
        [raw], tmp_path / OUTPUT_NAME, DateRange(date(2026, 10, 27), date(2026, 10, 30))
    ).output_file


def test_csv_input_converts_percent_once_and_uses_all_snapshot_times(merged_file):
    snapshots = read_snapshots(merged_file)
    assert len(snapshots) == 4
    assert snapshots[0].rates[Decimal("0.25")] == Decimal("0.1309")
    history = calculate_history(merged_file)
    assert len(history) == 4
    assert history[0].base_points[0].yield_rate == Decimal("0.1309")
    assert len(history[0].scenarios) == 6


def test_missing_values_are_restored_in_memory_without_changing_csv(merged_file):
    merged_file.write_text(
        merged_file.read_text(encoding="utf-8").replace("13.73", "NA"), encoding="utf-8"
    )
    before = merged_file.read_bytes()
    snapshots = read_snapshots(merged_file)
    assert snapshots[0].rates[Decimal("0.75")] is None
    history = calculate_history(merged_file)
    point = history[0].base_points[2]
    assert point.yield_rate == Decimal("0.1372")
    assert point.is_interpolated is True
    assert merged_file.read_bytes() == before
    assert not list(merged_file.parent.glob("prepared*.csv"))


def test_snapshot_with_less_than_two_values_is_not_borrowed_from_other_days(
    merged_file,
):
    with merged_file.open(encoding="utf-8") as stream:
        rows = list(csv.reader(stream, delimiter=";"))
    rows[2][3:] = ["NA"] * (len(TERMS) - 1)
    with merged_file.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, delimiter=";").writerows(rows)
    with pytest.raises(DataValidationError, match="2026-10-28.*две"):
        calculate_history(merged_file)


@pytest.mark.parametrize("replacement", ["Infinity", "garbage"])
def test_invalid_rate_is_not_treated_as_missing(merged_file, replacement):
    merged_file.write_text(
        merged_file.read_text(encoding="utf-8").replace("13.09", replacement),
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError):
        read_snapshots(merged_file)


def test_cli_uses_explicit_run_directory_without_writing_files(merged_file):
    before = set(merged_file.parent.iterdir())
    result = CliRunner().invoke(main, ["--run-dir", str(merged_file.parent)])
    assert result.exit_code == 0, result.output
    assert "базовых точек: 48" in result.output
    assert "стрессовых точек: 288" in result.output
    assert ScenarioParameters().version in result.output
    assert set(merged_file.parent.iterdir()) == before


def test_excel_export_preserves_calculation_precision_and_source_csv(
    merged_file, tmp_path
):
    history = calculate_history(merged_file)
    before = deepcopy(history)
    source = merged_file.read_bytes()
    path = export_history_xlsx(history, tmp_path / "history.xlsx")
    workbook = load_workbook(path, data_only=True)
    try:
        assert workbook.sheetnames == ["Baseline", *SCENARIOS, "Parameters"]
        sheet = workbook["Baseline"]
        assert sheet.max_row == 49
        assert sheet["D2"].value == pytest.approx(0.1309)
        assert sheet["D2"].number_format == "0.00%"
        assert sheet["F2"].value is False
        assert "discount_convention" not in [cell.value for cell in sheet[1]]
        assert "discount_convention" not in [
            cell.value for cell in workbook["Parallel_Up"][1]
        ]
        assert workbook["Parallel_Up"]["E2"].value == pytest.approx(0.1709)
        assert workbook["Parameters"].max_row == 7
    finally:
        workbook.close()
    assert history == before
    assert merged_file.read_bytes() == source


def test_legacy_excel_wrapper_places_output_next_to_input(merged_file):
    history = generate_irrbb_history(merged_file)
    assert len(history) == 4
    assert (merged_file.parent / "IRRBB_Scenarios_History.xlsx").is_file()


@pytest.mark.parametrize("args", [[], ["--shock-bps", "-1"]])
def test_cli_requires_explicit_input(args):
    assert CliRunner().invoke(main, args).exit_code != 0


def test_cli_rejects_invalid_parameters_and_preserves_output_on_failure(
    merged_file, tmp_path
):
    output = tmp_path / "history.xlsx"
    output.write_text("previous")
    result = CliRunner().invoke(
        main, ["--input", str(merged_file), "--shock-bps", "NaN", "--xlsx", str(output)]
    )
    assert result.exit_code != 0
    assert output.read_text() == "previous"
