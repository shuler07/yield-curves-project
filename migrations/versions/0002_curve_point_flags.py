"""Add curve point provenance and soft deletion flags.

Revision ID: 0002_curve_point_flags
Revises: 0001_create_db
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_curve_point_flags"
down_revision = "0001_create_db"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The server default supplies FALSE for existing rows as well as inserts
    # that do not specify the flags. The original migration stays unchanged.
    op.add_column(
        "curve_points",
        sa.Column(
            "is_interpolated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "curve_points",
        sa.Column(
            "is_extrapolated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "curve_points",
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("curve_points", "is_deleted")
    op.drop_column("curve_points", "is_extrapolated")
    op.drop_column("curve_points", "is_interpolated")
