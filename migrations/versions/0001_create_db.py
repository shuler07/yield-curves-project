"""Create curve storage and ETL run history.

Revision ID: 0001_create_db
Revises: None
"""

from pathlib import Path

from alembic import op

revision = "0001_create_db"
down_revision = None
branch_labels = None
depends_on = None


def _execute_script(name: str) -> None:
    script = Path(__file__).resolve().parents[1] / "sql" / name
    # Explicit boundaries allow PL/pgSQL bodies to contain semicolons and work
    # with both psycopg's execution protocol and Alembic's offline SQL output.
    for statement in script.read_text(encoding="utf-8").split("-- statement-break"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    _execute_script("0001_up.sql")


def downgrade() -> None:
    _execute_script("0001_down.sql")
