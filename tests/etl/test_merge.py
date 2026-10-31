import csv
import io
import json
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from click.testing import CliRunner

from src.etl.common import (
    OUTPUT_NAME,
    TERMS,
    DataValidationError,
    DateRange,
    NoDataError,
    write_manifest,
)
from src.etl.merge import extract_and_merge, main, merge_run

DATES = DateRange(date(2026, 10, 27), date(2026, 10, 30))


def save_source(tmp_path, content, name="source.csv"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8-sig")
    return path


def replace_cell(content, column, value):
    rows = list(csv.reader(io.StringIO(content), delimiter=";"))
    for index, row in enumerate(rows):
        if row and row[0] == "tradedate":
            rows[index + 1][column] = value
            break
    out = io.StringIO()
    csv.writer(out, delimiter=";", lineterminator="\n").writerows(rows)
    return out.getvalue()


def test_real_moex_export_is_filtered_sorted_and_not_rounded(tmp_path, moex_csv):
    source = save_source(tmp_path, moex_csv.replace("12,84", "12,84123456789"))
    dates = DateRange(date(2026, 10, 28), date(2026, 10, 29))
    result = extract_and_merge([source], tmp_path / OUTPUT_NAME, dates)
    with result.output_file.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        rows = list(reader)
        assert reader.fieldnames == [
            "tradedate",
            "tradetime",
            *(f"period_{t}" for t in TERMS),
        ]
    assert result.snapshot_count == 2
    assert result.missing_dates == ()
    assert [row["tradedate"] for row in rows] == ["28.10.2026", "29.10.2026"]
    assert Decimal(rows[0]["period_0.25"]) == Decimal("12.84123456789")
    assert rows[0]["tradetime"] == "18:54:59"


def test_zip_parts_combine_into_complete_snapshots_without_extraction(
    tmp_path, moex_csv
):
    rows = list(csv.reader(io.StringIO(moex_csv), delimiter=";"))[2:]
    sources = []
    for column in range(2, 14):
        part = io.StringIO()
        csv.writer(part, delimiter=";", lineterminator="\n").writerows(
            [[*row[:2], row[column]] for row in rows if row]
        )
        archive = tmp_path / f"term_{column}.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../unpacked/source.csv", part.getvalue())
        sources.append(archive)
    result = extract_and_merge(sources, tmp_path / OUTPUT_NAME, DATES)
    assert result.snapshot_count == 4
    assert not (tmp_path / "unpacked").exists()


@pytest.mark.parametrize("value", ["", "abc", "NaN", "Infinity", "-Infinity"])
def test_invalid_rates_fail_before_publishing(tmp_path, moex_csv, value):
    source = save_source(tmp_path, replace_cell(moex_csv, 2, value))
    output = tmp_path / OUTPUT_NAME
    with pytest.raises(DataValidationError):
        extract_and_merge([source], output, DATES)
    assert not output.exists()


@pytest.mark.parametrize("column,value", [(0, "31.02.2026"), (1, "25:00:00")])
def test_invalid_quote_date_or_time_rejected(tmp_path, moex_csv, column, value):
    source = save_source(tmp_path, replace_cell(moex_csv, column, value))
    with pytest.raises(DataValidationError):
        extract_and_merge([source], tmp_path / OUTPUT_NAME, DATES)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda content: content.replace("period_20.0", "period_30"),
        lambda content: content.replace("period_20.0", "period_1"),
        lambda content: content.replace("tradedate;tradetime;", "date;time;"),
        lambda content: content.replace(";16,56", ""),
    ],
)
def test_changed_source_format_is_not_silently_accepted(tmp_path, moex_csv, mutation):
    source = save_source(tmp_path, mutation(moex_csv))
    with pytest.raises(DataValidationError):
        extract_and_merge([source], tmp_path / OUTPUT_NAME, DATES)


def test_incomplete_snapshot_preserves_previous_good_output(tmp_path, moex_csv):
    lines = [
        line.rsplit(";", 1)[0] if ";" in line else line
        for line in moex_csv.splitlines()
    ]
    source = save_source(tmp_path, "\n".join(lines))
    output = tmp_path / OUTPUT_NAME
    output.write_text("previous good result", encoding="utf-8")
    with pytest.raises(DataValidationError, match="20"):
        extract_and_merge([source], output, DATES)
    assert output.read_text(encoding="utf-8") == "previous good result"
    assert not list(tmp_path.glob("*.tmp"))


def test_duplicates_are_numeric_but_conflicts_are_errors(tmp_path, moex_csv):
    source = save_source(tmp_path, moex_csv)
    duplicate = save_source(
        tmp_path, moex_csv.replace("13,09", "13.090"), "duplicate.csv"
    )
    assert (
        extract_and_merge(
            [source, duplicate], tmp_path / OUTPUT_NAME, DATES
        ).snapshot_count
        == 4
    )
    duplicate.write_text(moex_csv.replace("13,09", "13,10"), encoding="utf-8")
    with pytest.raises(DataValidationError, match="конфликт"):
        extract_and_merge([source, duplicate], tmp_path / OUTPUT_NAME, DATES)


def test_missing_dates_are_reported_without_synthetic_quotes(tmp_path, moex_csv):
    source = save_source(tmp_path, moex_csv)
    dates = DateRange(date(2026, 10, 27), date(2026, 10, 31))
    result = extract_and_merge([source], tmp_path / OUTPUT_NAME, dates)
    assert result.snapshot_count == 4
    assert result.missing_dates == (date(2026, 10, 31),)


def test_no_data_is_distinct_from_incomplete_curve(tmp_path, moex_csv):
    source = save_source(tmp_path, moex_csv)
    with pytest.raises(NoDataError):
        extract_and_merge(
            [source],
            tmp_path / OUTPUT_NAME,
            DateRange(date(2026, 10, 31), date(2026, 10, 31)),
        )
    assert not (tmp_path / OUTPUT_NAME).exists()


@pytest.mark.parametrize("kind", ["not_zip", "crc", "no_csv", "encoding"])
def test_bad_archives_rejected(tmp_path, moex_csv, kind):
    archive = tmp_path / "source.zip"
    if kind == "not_zip":
        archive.write_bytes(b"not a zip")
    else:
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(
                "source.txt" if kind == "no_csv" else "source.csv",
                b"\xff" if kind == "encoding" else moex_csv.encode(),
            )
        if kind == "crc":
            archive.write_bytes(archive.read_bytes().replace(b"13,09", b"13,10", 1))
    with pytest.raises(DataValidationError):
        extract_and_merge([archive], tmp_path / OUTPUT_NAME, DATES)


def test_manifest_excludes_stale_files_and_cli_reprocesses_without_browser(
    tmp_path, moex_csv
):
    source = save_source(tmp_path, moex_csv)
    save_source(tmp_path, "broken old export", "old.csv")
    write_manifest(tmp_path, DATES, [source])
    result = CliRunner().invoke(main, ["--run-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert merge_run(tmp_path).snapshot_count == 4


@pytest.mark.parametrize(
    "files",
    [
        ["../source.csv"],
        ["sub/source.csv"],
        ["sub\\source.csv"],
        ["missing.csv"],
        ["source.csv", "source.csv"],
        [],
    ],
)
def test_manifest_cannot_reference_other_runs_or_missing_files(
    tmp_path, moex_csv, files
):
    save_source(tmp_path, moex_csv)
    path = write_manifest(tmp_path, DATES, [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["files"] = files
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataValidationError):
        merge_run(tmp_path)


def test_source_file_cannot_be_overwritten_as_output(tmp_path, moex_csv):
    source = save_source(tmp_path, moex_csv)
    with pytest.raises(DataValidationError):
        extract_and_merge([source], source, DATES)
    assert source.read_text(encoding="utf-8-sig") == moex_csv
