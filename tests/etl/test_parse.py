import importlib
from datetime import date

import pytest
from click.testing import CliRunner
from selenium.common.exceptions import TimeoutException, WebDriverException

from src.etl import parse
from src.etl.common import (
    MANIFEST_NAME,
    OUTPUT_NAME,
    SOURCE_TERMS,
    DateRange,
    atomic_write_csv,
)
from src.etl.merge import merge_run

DATES = DateRange(date(2026, 10, 27), date(2026, 10, 30))


class Driver:
    closed = False

    def set_page_load_timeout(self, value):
        self.page_timeout = value

    def set_script_timeout(self, value):
        self.script_timeout = value

    def get(self, url):
        self.url = url

    def find_elements(self, *args):
        return []

    def quit(self):
        self.closed = True


def test_import_does_not_launch_browser_or_create_downloads(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Browser launched during module import")

    monkeypatch.setattr(parse.webdriver, "Chrome", unexpected)
    monkeypatch.chdir(tmp_path)
    importlib.reload(parse)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--date-from", "2026-10-27"],
        ["--date-to", "2026-10-30"],
        ["--date", "2026-02-30"],
        ["--date", "2026-10-27", "--date-to", "2026-10-30"],
        ["--date-from", "2026-10-30", "--date-to", "2026-10-27"],
        ["--date", "2026-10-27", "--timeout", "0"],
    ],
)
def test_invalid_cli_arguments_do_not_start_collection(monkeypatch, args):
    def unexpected(*args, **kwargs):
        pytest.fail("Collection started for invalid arguments")

    monkeypatch.setattr(parse, "collect_curves", unexpected)
    assert CliRunner().invoke(parse.main, args).exit_code != 0


def test_cli_single_date_and_run_directory(tmp_path, moex_csv, monkeypatch):
    driver = Driver()
    monkeypatch.setattr(parse, "create_driver", lambda path: driver)
    requested = []

    def download(browser, directory, dates, deadline):
        requested.append(dates)
        path = directory / "source.csv"
        path.write_text(moex_csv, encoding="utf-8")
        return path

    monkeypatch.setattr(parse, "_download_range", download)
    run_dir = tmp_path / "run"
    result = CliRunner().invoke(
        parse.main, ["--date", "2026-10-28", "--run-dir", str(run_dir)]
    )
    assert result.exit_code == 0, result.output
    assert requested == [DateRange(date(2026, 10, 28), date(2026, 10, 28))]
    assert merge_run(run_dir).snapshot_count == 1
    assert driver.closed


def test_cross_year_ranges_are_split_and_use_one_deadline(
    tmp_path, moex_csv, monkeypatch
):
    driver = Driver()
    monkeypatch.setattr(parse, "create_driver", lambda path: driver)
    requests = []

    def download(browser, directory, dates, deadline):
        requests.append((dates, deadline))
        path = directory / f"{dates.start.year}.csv"
        path.write_text(moex_csv, encoding="utf-8")
        return path

    monkeypatch.setattr(parse, "_download_range", download)
    result = parse.collect_curves(
        DateRange(date(2025, 12, 30), date(2026, 10, 30)), tmp_path / "run"
    )
    assert [request[0] for request in requests] == [
        DateRange(date(2025, 12, 30), date(2025, 12, 31)),
        DateRange(date(2026, 1, 1), date(2026, 10, 30)),
    ]
    assert requests[0][1] == requests[1][1]
    assert result.snapshot_count == 4
    assert driver.closed


def test_existing_directory_is_not_used_or_modified(tmp_path):
    marker = tmp_path / "old.csv"
    marker.write_text("old data")
    with pytest.raises(FileExistsError):
        parse.collect_curves(DATES, tmp_path)
    assert marker.read_text() == "old data"


@pytest.mark.parametrize(
    "failure", [TimeoutException("network timeout"), RuntimeError("source failed")]
)
def test_download_failure_closes_browser_and_publishes_nothing(
    tmp_path, monkeypatch, failure
):
    driver = Driver()
    monkeypatch.setattr(parse, "create_driver", lambda path: driver)

    def download(*args):
        raise failure

    monkeypatch.setattr(parse, "_download_range", download)
    run_dir = tmp_path / "run"
    with pytest.raises(type(failure)):
        parse.collect_curves(DATES, run_dir)
    assert driver.closed
    assert not (run_dir / MANIFEST_NAME).exists()
    assert not (run_dir / OUTPUT_NAME).exists()


def test_global_timeout_is_not_reset_for_next_file(tmp_path, monkeypatch, moex_csv):
    clock = [0]
    monkeypatch.setattr(parse, "monotonic", lambda: clock[0])
    driver = Driver()
    monkeypatch.setattr(parse, "create_driver", lambda path: driver)
    requests = []

    def download(browser, directory, dates, deadline):
        requests.append(dates)
        path = directory / "first.csv"
        path.write_text(moex_csv, encoding="utf-8")
        clock[0] = 11
        return path

    monkeypatch.setattr(parse, "_download_range", download)
    with pytest.raises(TimeoutError):
        parse.collect_curves(
            DateRange(date(2025, 12, 31), date(2026, 1, 1)),
            tmp_path / "run",
            timeout=10,
        )
    assert len(requests) == 1
    assert driver.closed
    assert not (tmp_path / "run" / OUTPUT_NAME).exists()


def test_insufficient_export_closes_browser_and_preserves_missing_rates(
    tmp_path, monkeypatch
):
    driver = Driver()
    monkeypatch.setattr(parse, "create_driver", lambda path: driver)

    def download(browser, directory, dates, deadline):
        path = directory / "source.csv"
        path.write_text(
            "tradedate;tradetime;period_1\n27.10.2026;18:00:00;13,5", encoding="utf-8"
        )
        return path

    monkeypatch.setattr(parse, "_download_range", download)
    result = parse.collect_curves(DATES, tmp_path / "run")
    assert result.snapshot_count == 1
    assert driver.closed
    assert (tmp_path / "run" / MANIFEST_NAME).exists()
    assert (tmp_path / "run" / OUTPUT_NAME).exists()
    assert "NA" in (tmp_path / "run" / OUTPUT_NAME).read_text(encoding="utf-8")


def test_download_waits_for_partial_files_and_stable_size(tmp_path, monkeypatch):
    partial = tmp_path / "source.csv.crdownload"
    completed = tmp_path / "source.csv"

    class Wait:
        def __init__(self, *args, **kwargs):
            pass

        def until(self, condition, message):
            partial.write_text("partial")
            assert not condition(None)
            partial.rename(completed)
            assert not condition(None)
            completed.write_text("complete data")
            assert not condition(None)
            return condition(None)

    monkeypatch.setattr(parse, "WebDriverWait", Wait)
    path = parse._wait_for_download(Driver(), tmp_path, set(), parse.monotonic() + 10)
    assert path == completed
    assert path.read_text() == "complete data"


def test_atomic_publication_keeps_previous_result_when_write_fails(tmp_path):
    output = tmp_path / "result.csv"
    output.write_text("previous")

    def fail(stream):
        stream.write("half of new CSV")
        raise OSError("disk full")

    with pytest.raises(OSError):
        atomic_write_csv(output, fail)
    assert output.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [output]


def test_quit_failure_does_not_hide_original_download_error(tmp_path, monkeypatch):
    class BrokenDriver(Driver):
        def quit(self):
            raise WebDriverException("quit failed")

    monkeypatch.setattr(parse, "create_driver", lambda path: BrokenDriver())

    def download(*args):
        raise TimeoutError("source timeout")

    monkeypatch.setattr(parse, "_download_range", download)
    with pytest.raises(TimeoutError, match="source timeout"):
        parse.collect_curves(DATES, tmp_path / "run")


@pytest.mark.parametrize(
    "target,expected",
    [
        (date(2026, 10, 30), ("29.10.2026", "30.10.2026")),
        (date(2026, 1, 1), ("31.12.2025", "01.01.2026")),
        (date(2014, 1, 1), ("01.01.2014", "02.01.2014")),
    ],
)
def test_single_day_uses_accepted_upstream_range(
    tmp_path, monkeypatch, target, expected
):
    class Element:
        def __init__(self):
            self.keys = []

        def send_keys(self, *keys):
            self.keys.append(keys)

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            pass

    date_fields = [Element(), Element()]
    term, button = Element(), Element()

    class FormDriver(Driver):
        def find_elements(self, *args):
            return date_fields

        def find_element(self, *args):
            return term if args == parse.TERM_INPUT else button

        def execute_script(self, *args):
            pass

    def downloaded(*args):
        path = tmp_path / "source.csv"
        path.write_text("downloaded CSV")
        return path

    monkeypatch.setattr(parse, "_wait_for_download", downloaded)
    result = parse._download_range(
        FormDriver(), tmp_path, DateRange(target, target), parse.monotonic() + 10
    )
    assert [element.keys[1][0] for element in date_fields] == list(expected)
    assert term.keys[1][0] == ",".join(map(str, SOURCE_TERMS))
    assert len(term.keys[1][0].split(",")) == 12
    assert result.name == f"curve_{target}_{target}.csv"


def test_source_form_error_is_reported_without_download_timeout(tmp_path):
    class Error:
        text = "Invalid date range"

        def is_displayed(self):
            return True

    class ErrorDriver(Driver):
        def find_elements(self, *args):
            return [Error()]

    with pytest.raises(ValueError, match="Invalid date range"):
        parse._wait_for_download(ErrorDriver(), tmp_path, set(), parse.monotonic() + 10)
