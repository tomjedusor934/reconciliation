"""Add cancelled_at and cancelled_by_user_id to exclusion table.

Revision ID: 003
Revises: 002
Create Date: 2026-05-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "exclusion",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        schema="reco",
    )
    op.add_column(
        "exclusion",
        sa.Column("cancelled_by_user_id", sa.Integer(), nullable=True),
        schema="reco",
    )
    op.create_foreign_key(
        "fk_exclusion_cancelled_by_user",
        "exclusion",
        "user",
        ["cancelled_by_user_id"],
        ["id"],
        source_schema="reco",
        referent_schema="public",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_exclusion_cancelled_by_user",
        "exclusion",
        type_="foreignkey",
        schema="reco",
    )
    op.drop_column("exclusion", "cancelled_by_user_id", schema="reco")
    op.drop_column("exclusion", "cancelled_at", schema="reco")
