"""Separate original MOEX observations and all seven processed curves.

Revision ID: 0004_raw_processed_curves
Revises: 0003_remove_discount_convention
"""

from pathlib import Path

from alembic import op

revision = "0004_raw_processed_curves"
down_revision = "0003_remove_discount_convention"
branch_labels = None
depends_on = None


def _execute_file(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(
        encoding="utf-8"
    )
    for statement in sql.split("-- statement-break"):
        if statement.strip():
            op.execute(statement.strip())


def upgrade() -> None:
    _execute_file("0004_up.sql")


def downgrade() -> None:
    # A failed calculation may leave raw observations with no valid baseline.
    # The old schema cannot represent this state; abort rather than discard them.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM raw_curve_points r
                LEFT JOIN processed_curve_points p
                ON p.curve_date = r.curve_date AND p.quote_time = r.quote_time
                AND p.tenor = r.tenor AND p.scenario = 'Baseline'
                WHERE p.id IS NULL OR p.yield_rate <> round(r.yield_rate, 10)
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade: raw observations without a matching baseline';
            END IF;
        END $$;
    """)
    _execute_file("0004_legacy_tables.sql")
    _execute_file("0004_down.sql")
