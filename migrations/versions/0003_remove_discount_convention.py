"""Remove per-point discount conventions; use one annual MOEX convention.

Revision ID: 0003_remove_discount_convention
Revises: 0002_curve_point_flags
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_remove_discount_convention"
down_revision = "0002_curve_point_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_curve_points_convention", "curve_points", type_="check")
    op.drop_constraint("ck_stress_curves_convention", "stress_curves", type_="check")
    op.drop_column("curve_points", "discount_convention")
    op.drop_column("stress_curves", "discount_convention")


def downgrade() -> None:
    # Old per-row labels cannot be recovered after dropping their columns.
    # Restore the previous schema, backfilling the sole supported convention.
    for table, constraint in (
        ("curve_points", "ck_curve_points_convention"),
        ("stress_curves", "ck_stress_curves_convention"),
    ):
        op.add_column(
            table,
            sa.Column(
                "discount_convention",
                sa.String(100),
                nullable=False,
                server_default=sa.text("'annual_effective_v1'"),
            ),
        )
        op.alter_column(table, "discount_convention", server_default=None)
        op.create_check_constraint(
            constraint, table, "length(btrim(discount_convention)) > 0"
        )
