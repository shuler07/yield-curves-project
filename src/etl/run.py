"""Unified CLI: MOEX -> raw CSV -> calculations -> PostgreSQL."""

from pathlib import Path

import click
from sqlalchemy.exc import SQLAlchemyError

from src.etl.common import DateRange, read_manifest
from src.etl.pipeline import run_pipeline


@click.command()
@click.option("--date", "single_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--date-from", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--date-to", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option(
    "--run-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Новый каталог для скачивания файлов MOEX.",
)
@click.option(
    "--from-run-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Повторить загрузку из исходных файлов и диапазона manifest.json.",
)
@click.option(
    "--run-type",
    type=click.Choice(["manual", "scheduled"]),
    default="manual",
    show_default=True,
    help="Источник запуска; scheduled задаёт планировщик.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=180,
    show_default=True,
    help="Бюджет сбора MOEX, секунды.",
)
def main(single_date, date_from, date_to, run_dir, from_run_dir, run_type, timeout):
    """Получить кривые за дату/диапазон и записать базу + 6 сценариев в БД."""
    if from_run_dir is not None:
        if any(
            value is not None for value in (single_date, date_from, date_to, run_dir)
        ):
            raise click.UsageError("--from-run-dir используется без дат и --run-dir")
    elif single_date is not None:
        if date_from is not None or date_to is not None:
            raise click.UsageError("Используйте --date либо --date-from и --date-to")
        date_from = date_to = single_date
    elif date_from is None or date_to is None:
        raise click.UsageError("Нужен --date, диапазон или --from-run-dir")
    try:
        dates = (
            read_manifest(from_run_dir)[0]
            if from_run_dir is not None
            else DateRange(date_from.date(), date_to.date())
        )
        result = run_pipeline(
            dates,
            run_type=run_type,
            run_dir=run_dir,
            from_run_dir=from_run_dir,
            timeout=timeout,
        )
    except SQLAlchemyError as exc:
        raise click.ClickException(
            "Не удалось записать журнал ETL в БД. Проверьте DATABASE_URL, "
            "доступность PostgreSQL и alembic upgrade head."
        ) from exc
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"ETL run: {result.run_id}; статус: {result.status}")
    if result.run_dir is not None:
        click.echo(f"Файлы: {result.run_dir}")
    click.echo(
        f"Дат сохранено: {len(result.saved_dates)}; снимков: {result.snapshot_count}; "
        f"обработано точек: {result.rows_processed}; записано строк: {result.rows_saved}"
    )
    click.echo(
        f"Интерполировано: {result.interpolated}; экстраполировано: {result.extrapolated}"
    )
    if result.missing_dates:
        click.echo("Нет наблюдений: " + ", ".join(map(str, result.missing_dates)))
    for day, error in result.errors.items():
        click.echo(f"Ошибка [{day}]: {error}", err=True)
    if result.status != "success":
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    main()
