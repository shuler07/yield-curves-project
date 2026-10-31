"""Shared input contract for collecting and reprocessing curve snapshots."""

import json
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

SOURCE_TERMS = tuple(
    Decimal(value)
    for value in (
        "0.25",
        "0.5",
        "0.75",
        "1",
        "2",
        "3",
        "4",
        "5",
        "7",
        "10",
        "15",
        "20",
    )
)
# Years, rounded to the NUMERIC(12, 6) precision used by curve_points.tenor.
# Keep these keys identical in calculations, PostgreSQL RETURNING and repeat loads.
DAY_TENOR = Decimal("0.002740")  # 1 / 365 (ACT/365)
MONTH_TENOR = Decimal("0.083333")  # 1 / 12
TARGET_TERMS = tuple(
    sorted(
        (
            *SOURCE_TERMS,
            DAY_TENOR,
            MONTH_TENOR,
            *(Decimal(value) for value in ("1.5", "6", "8", "9", "30")),
        )
    )
)
SOURCE_URL = "https://www.moex.com/ru/marketdata/indices/state/g-curve/archive/"
DEFAULT_DOWNLOADS_DIR = Path(__file__).resolve().parent / "downloads"
MANIFEST_NAME = "manifest.json"
OUTPUT_NAME = "merged_zcyc_ranges.csv"
MISSING_RATE = "NA"
MISSING_RATE_TOKENS = frozenset({"", "NA", "N/A", "NULL", "NAN"})


class DataValidationError(ValueError):
    """The export has an invalid structure or invalid observed values."""


class NoDataError(DataValidationError):
    """The source contains no observations for the requested dates."""


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("Начальная дата не может быть позже конечной")

    def split_by_year(self):
        start = self.start
        while start <= self.end:
            end = min(self.end, date(start.year, 12, 31))
            yield DateRange(start, end)
            if end == self.end:
                break
            start = end + timedelta(days=1)

    def days(self):
        for offset in range((self.end - self.start).days + 1):
            yield self.start + timedelta(days=offset)


def write_manifest(run_dir: Path, dates: DateRange, files: list[Path]) -> Path:
    payload = {
        "version": 1,
        "source_url": SOURCE_URL,
        "export_mode": "range",
        "date_from": dates.start.isoformat(),
        "date_to": dates.end.isoformat(),
        "files": [path.name for path in files],
    }
    path = run_dir / MANIFEST_NAME
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def read_manifest(run_dir: Path) -> tuple[DateRange, list[Path]]:
    """Only direct files listed by this run are eligible for merging."""
    try:
        payload = json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("неподдерживаемая версия")
        dates = DateRange(
            date.fromisoformat(payload["date_from"]),
            date.fromisoformat(payload["date_to"]),
        )
        names = payload["files"]
        if not isinstance(names, list) or not names:
            raise ValueError("пустой список файлов")
        files = []
        for name in names:
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                raise ValueError("имя файла должно быть без каталогов")
            path = (run_dir / name).resolve()
            if path.parent != run_dir.resolve() or path.suffix.lower() not in {
                ".csv",
                ".zip",
            }:
                raise ValueError("недопустимый путь к файлу")
            if not path.is_file():
                raise ValueError(f"файл отсутствует: {name}")
            files.append(path)
        if len(set(files)) != len(files):
            raise ValueError("повторяющиеся файлы")
        return dates, files
    except (KeyError, TypeError, ValueError) as exc:
        raise DataValidationError(f"Некорректный {MANIFEST_NAME}: {exc}") from exc


def atomic_write_csv(output: Path, write_rows) -> None:
    """Publish only a fully written file and preserve previous good output."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output.parent,
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            write_rows(stream)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
