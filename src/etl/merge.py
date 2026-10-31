"""Validate explicit CSV/ZIP exports and publish complete MOEX snapshots."""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click

from src.etl.common import (
    OUTPUT_NAME,
    TERMS,
    DataValidationError,
    DateRange,
    NoDataError,
    atomic_write_csv,
    read_manifest,
)


@dataclass(frozen=True)
class MergeResult:
    output_file: Path
    snapshot_count: int
    missing_dates: tuple[date, ...]


def _csv_sources(path: Path):
    """Read archive members in memory; never extract untrusted paths to disk."""
    try:
        if path.suffix.lower() == ".csv":
            yield path.name, path.read_text(encoding="utf-8-sig")
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise DataValidationError(
                        f"Нарушена целостность ZIP {path.name}: {bad_member}"
                    )
                members = [
                    m
                    for m in archive.infolist()
                    if not m.is_dir() and m.filename.lower().endswith(".csv")
                ]
                if not members:
                    raise DataValidationError(f"В ZIP {path.name} нет CSV")
                for member in members:
                    yield (
                        f"{path.name}/{member.filename}",
                        archive.read(member).decode("utf-8-sig"),
                    )
        else:
            raise DataValidationError(f"Ожидался CSV или ZIP: {path.name}")
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, EOFError) as exc:
        raise DataValidationError(f"Не удалось прочитать {path.name}: {exc}") from exc


def _parse_date(value: str) -> date:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"некорректная дата {value!r}")


def _parse_term(column: str) -> Decimal:
    if not column.startswith("period_"):
        raise ValueError(f"неизвестная колонка {column!r}")
    try:
        term = Decimal(column.removeprefix("period_"))
    except InvalidOperation as exc:
        raise ValueError(f"некорректный срок {column!r}") from exc
    if not term.is_finite() or term not in TERMS:
        raise ValueError(f"неожиданный срок {column!r}")
    return term


def _read_rows(source: str, content: str, dates: DateRange, merged: dict) -> None:
    reader = csv.reader(io.StringIO(content), delimiter=";", strict=True)
    try:
        for header in reader:
            if header and header[0].strip().lower() == "tradedate":
                break
        else:
            raise ValueError("не найден заголовок tradedate")
        header = [cell.strip().lower() for cell in header]
        if len(header) < 3 or header[1] != "tradetime":
            raise ValueError("ожидались колонки tradedate;tradetime;period_...")
        terms = [_parse_term(column) for column in header[2:]]
        if len(set(terms)) != len(terms):
            raise ValueError("повторяющиеся сроки в заголовке")
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != len(header):
                raise ValueError(
                    f"ожидалось {len(header)} колонок, получено {len(row)}"
                )
            tradedate = _parse_date(row[0].strip())
            if not dates.start <= tradedate <= dates.end:
                continue
            tradetime = datetime.strptime(row[1].strip(), "%H:%M:%S").time()
            key = (tradedate, tradetime)
            snapshot = merged.setdefault(key, {})
            for term, raw in zip(terms, row[2:], strict=True):
                try:
                    value = Decimal(raw.strip().replace(",", "."))
                except InvalidOperation as exc:
                    raise ValueError(
                        f"некорректная ставка {raw!r} для срока {term}"
                    ) from exc
                if not value.is_finite():
                    raise ValueError(
                        f"ставка для срока {term} должна быть конечным числом"
                    )
                if term in snapshot and snapshot[term] != value:
                    raise ValueError(
                        f"конфликт ставок для {tradedate} {tradetime}, срок {term}"
                    )
                snapshot[term] = value
    except (ValueError, InvalidOperation, csv.Error) as exc:
        raise DataValidationError(f"{source}, строка {reader.line_num}: {exc}") from exc


def extract_and_merge(
    files: list[Path], output_file: Path, dates: DateRange
) -> MergeResult:
    if not files:
        raise DataValidationError("Не переданы файлы для объединения")
    if output_file.resolve() in {path.resolve() for path in files}:
        raise DataValidationError("Итоговый CSV не может заменять исходный файл")
    merged = {}
    for path in files:
        for source, content in _csv_sources(path):
            _read_rows(source, content, dates, merged)
    if not merged:
        raise NoDataError(f"Нет котировок за {dates.start} — {dates.end}")
    expected = set(TERMS)
    for (tradedate, tradetime), values in sorted(merged.items()):
        missing = expected - values.keys()
        if missing:
            raise DataValidationError(
                f"Неполный снимок {tradedate} {tradetime}: отсутствуют сроки "
                + ", ".join(str(term) for term in sorted(missing))
            )

    def write_rows(stream):
        writer = csv.writer(stream, delimiter=";", lineterminator="\n")
        writer.writerow(
            ["tradedate", "tradetime", *(f"period_{term}" for term in TERMS)]
        )
        for (tradedate, tradetime), values in sorted(merged.items()):
            writer.writerow(
                [
                    tradedate.strftime("%d.%m.%Y"),
                    tradetime.strftime("%H:%M:%S"),
                    *(format(values[term], "f") for term in TERMS),
                ]
            )

    atomic_write_csv(output_file, write_rows)
    observed_dates = {key[0] for key in merged}
    return MergeResult(
        output_file,
        len(merged),
        tuple(day for day in dates.days() if day not in observed_dates),
    )


def merge_run(run_dir: Path) -> MergeResult:
    dates, files = read_manifest(run_dir)
    return extract_and_merge(files, run_dir / OUTPUT_NAME, dates)


def print_result(result: MergeResult) -> None:
    click.echo(f"CSV: {result.output_file}")
    click.echo(
        f"Полных снимков: {result.snapshot_count}; сроков в каждом: {len(TERMS)}"
    )
    if result.missing_dates:
        click.echo(
            "Даты без котировок (не заполняются): "
            + ", ".join(day.isoformat() for day in result.missing_dates)
        )


@click.command()
@click.option(
    "--run-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Каталог запуска с manifest.json.",
)
def main(run_dir: Path) -> None:
    """Повторная проверка и объединение файлов конкретного запуска без браузера."""
    try:
        print_result(merge_run(run_dir))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
