"""Collect MOEX exports for explicit dates and publish validated snapshots."""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from pathlib import Path
from time import gmtime, monotonic, strftime
from uuid import uuid4

import click
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.etl.common import (
    DEFAULT_DOWNLOADS_DIR,
    SOURCE_URL,
    TERMS,
    DateRange,
    write_manifest,
)
from src.etl.merge import MergeResult, merge_run, print_result

DATE_INPUTS = (By.CSS_SELECTOR, "input.hasDatepicker")
TERM_INPUT = (By.CSS_SELECTOR, "input.ui-input__field")
DOWNLOAD_BUTTON = (
    By.CSS_SELECTOR,
    'button[aria-label="Скачать данные за выбранный период"]',
)
LOGGER = logging.getLogger(__name__)
MIN_SOURCE_DATE = date(2014, 1, 1)
EXPORT_ERROR = (By.CSS_SELECTOR, ".ui-row.error")


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Истёк общий таймаут получения файлов MOEX")
    return remaining


def create_driver(run_dir: Path):
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-dev-shm-usage")
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(run_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
        },
    )
    return webdriver.Chrome(options=options)


def _wait_for_download(
    driver, run_dir: Path, before: set[Path], deadline: float
) -> Path:
    previous = None

    def completed(_driver):
        nonlocal previous
        errors = [
            element.text.strip()
            for element in driver.find_elements(*EXPORT_ERROR)
            if element.is_displayed() and element.text.strip()
        ]
        if errors:
            raise ValueError("MOEX отклонила выгрузку: " + "; ".join(errors))
        new_files = set(run_dir.iterdir()) - before
        if any(
            path.suffix.lower() in {".crdownload", ".tmp", ".part"}
            for path in new_files
        ):
            previous = None
            return False
        candidates = [
            path
            for path in new_files
            if path.suffix.lower() in {".csv", ".zip"} and path.is_file()
        ]
        if len(candidates) > 1:
            raise ValueError("MOEX вернула несколько файлов на один запрос")
        if not candidates:
            return False
        path = candidates[0]
        stat = path.stat()
        current = (path, stat.st_size, stat.st_mtime_ns)
        if stat.st_size > 0 and current == previous:
            return path
        previous = current
        return False

    return WebDriverWait(driver, _remaining(deadline), poll_frequency=0.2).until(
        completed,
        message="Не дождались завершения скачивания CSV/ZIP MOEX",
    )


def _download_range(driver, run_dir: Path, dates: DateRange, deadline: float) -> Path:
    WebDriverWait(driver, _remaining(deadline)).until(
        lambda browser: len(browser.find_elements(*DATE_INPUTS)) == 2,
    )
    # MOEX's form rejects from == till. Widen only the upstream request;
    # the manifest and local filter retain the originally requested date.
    source_dates = dates
    if dates.start == dates.end:
        if dates.start == MIN_SOURCE_DATE:
            source_dates = DateRange(dates.start, dates.end + timedelta(days=1))
        else:
            source_dates = DateRange(dates.start - timedelta(days=1), dates.end)
    # Re-find fields: changing dates may re-render the form.
    for index, value in enumerate((source_dates.start, source_dates.end)):
        field = driver.find_elements(*DATE_INPUTS)[index]
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(value.strftime("%d.%m.%Y"), Keys.ENTER)
    field = WebDriverWait(driver, _remaining(deadline)).until(
        EC.element_to_be_clickable(TERM_INPUT)
    )
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(",".join(str(term) for term in TERMS), Keys.ENTER)
    button = WebDriverWait(driver, _remaining(deadline)).until(
        EC.element_to_be_clickable(DOWNLOAD_BUTTON)
    )
    before = set(run_dir.iterdir())
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    button.click()
    path = _wait_for_download(driver, run_dir, before, deadline)
    destination = (
        run_dir
        / f"curve_{dates.start.isoformat()}_{dates.end.isoformat()}{path.suffix.lower()}"
    )
    path.rename(destination)
    return destination


def collect_curves(
    dates: DateRange, run_dir: Path | None = None, timeout: float = 180
) -> MergeResult:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Таймаут должен быть положительным конечным числом")
    if dates.start < MIN_SOURCE_DATE:
        raise ValueError("Архив MOEX доступен с 2014-01-01")
    if run_dir is None:
        stamp = strftime("%Y%m%dT%H%M%SZ", gmtime())
        run_dir = DEFAULT_DOWNLOADS_DIR / f"{stamp}_{uuid4().hex[:12]}"
    run_dir = run_dir.resolve()
    # Exclusive creation rules out old downloads and concurrent writers.
    run_dir.mkdir(parents=True, exist_ok=False)
    files = []
    driver = None
    deadline = monotonic() + timeout
    try:
        driver = create_driver(run_dir)
        driver.set_page_load_timeout(_remaining(deadline))
        driver.set_script_timeout(_remaining(deadline))
        driver.get(SOURCE_URL)
        for period in dates.split_by_year():
            _remaining(deadline)
            files.append(_download_range(driver, run_dir, period, deadline))
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                LOGGER.warning("Не удалось закрыть браузер Selenium", exc_info=True)
    write_manifest(run_dir, dates, files)
    return merge_run(run_dir)


@click.command()
@click.option(
    "--date",
    "single_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Одна дата, YYYY-MM-DD.",
)
@click.option(
    "--date-from",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Начало диапазона включительно.",
)
@click.option(
    "--date-to",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Конец диапазона включительно.",
)
@click.option(
    "--run-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Новый каталог запуска; по умолчанию создаётся уникальный в downloads.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=180,
    show_default=True,
    help="Общий бюджет получения файлов, секунды.",
)
def main(single_date, date_from, date_to, run_dir, timeout):
    """Загрузить и проверить кривые MOEX; сохранить CSV без сценариев и БД."""
    if single_date is not None:
        if date_from is not None or date_to is not None:
            raise click.UsageError(
                "Используйте либо --date, либо --date-from вместе с --date-to"
            )
        date_from = date_to = single_date
    elif date_from is None or date_to is None:
        raise click.UsageError("Нужен --date или оба параметра --date-from и --date-to")
    try:
        dates = DateRange(date_from.date(), date_to.date())
        print_result(collect_curves(dates, run_dir, timeout))
    except (OSError, ValueError, WebDriverException) as exc:
        message = str(exc).strip() or type(exc).__name__
        raise click.ClickException(message) from exc


if __name__ == "__main__":
    main()
