from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
UNPACK_DIR = DOWNLOADS_DIR / "unpacked"
OUTPUT_FILE = DOWNLOADS_DIR / "merged_zcyc_ranges.csv"


def sort_period_columns(columns: list[str]) -> list[str]:
    def period_key(name: str):
        if not name.startswith("period_"):
            return (1, name)
        value_str = name.split("_", 1)[1]
        try:
            return (0, float(value_str))
        except ValueError:
            return (1, value_str)

    return sorted(columns, key=period_key)


def extract_and_merge() -> None:
    UNPACK_DIR.mkdir(exist_ok=True)

    merged_rows: dict[tuple[str, str], dict[str, str]] = {}
    period_columns: list[str] = []

    zip_files = sorted(DOWNLOADS_DIR.glob("*.zip"))
    if not zip_files:
        raise FileNotFoundError(f"Не найдено zip-файлов в папке: {DOWNLOADS_DIR}")

    for archive_path in zip_files:
        with zipfile.ZipFile(archive_path) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not members:
                continue

            for csv_name in members:
                extracted_name = f"{archive_path.stem}_{Path(csv_name).name}"
                extracted_path = UNPACK_DIR / extracted_name
                extracted_path.write_bytes(archive.read(csv_name))

                text = extracted_path.read_text(encoding="utf-8-sig", errors="replace")
                reader = csv.reader(io.StringIO(text), delimiter=';')

                header_found = False
                header = []

                for row in reader:
                    if not row:
                        continue

                    first_cell = row[0].strip().lower()
                    if first_cell == "tradedate":
                        header = row
                        header_found = True
                        break

                if not header_found or len(header) < 3:
                    continue

                period_column = header[2].strip()
                if period_column not in period_columns:
                    period_columns.append(period_column)

                for row in reader:
                    if len(row) < 3:
                        continue

                    tradedate = row[0].strip()
                    tradetime = row[1].strip()
                    value = row[2].strip()

                    if not tradedate or not tradetime:
                        continue

                    key = (tradedate, tradetime)
                    if key not in merged_rows:
                        merged_rows[key] = {}

                    if period_column in merged_rows[key] and merged_rows[key][period_column] != value:
                        raise ValueError(
                            f"Обнаружено конфликтующее значение для ключа {key} "
                            f"в колонке {period_column}: "
                            f"{merged_rows[key][period_column]!r} vs {value!r}"
                        )

                    merged_rows[key][period_column] = value

    if not merged_rows:
        raise ValueError("После обработки архивов не найдено ни одной строки данных.")

    period_columns = sort_period_columns(period_columns)

    with OUTPUT_FILE.open("w", encoding="utf-8", newline="") as out_file:
        writer = csv.writer(out_file, delimiter=';', lineterminator='\n')
        writer.writerow(["tradedate", "tradetime", *period_columns])

        for key in sorted(
            merged_rows,
            key=lambda item: datetime.strptime(f"{item[0]} {item[1]}", "%d.%m.%Y %H:%M:%S"),
        ):
            row = [key[0], key[1]]
            for column in period_columns:
                row.append(merged_rows[key].get(column, ""))
            writer.writerow(row)

    print(f"Готово: распаковано {len(zip_files)} архивов и создан файл {OUTPUT_FILE}")
    print(f"Строк объединено: {len(merged_rows)}")
    print(f"Колонок итоговой таблицы: {len(period_columns)}")


if __name__ == "__main__":
    extract_and_merge()
