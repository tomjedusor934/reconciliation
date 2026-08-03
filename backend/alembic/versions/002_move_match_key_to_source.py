"""Move match_key_strategy from flow to flow_source; add file_pattern.

Revision ID: 002
Revises: 001
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add match_key_strategy column to flow_source
    op.add_column(
        "flow_source",
        sa.Column(
            "match_key_strategy",
            sa.String(32),
            nullable=True,
        ),
        schema="reco",
    )

    # 2. Add file_pattern column to flow_source
    op.add_column(
        "flow_source",
        sa.Column("file_pattern", sa.String(128), nullable=True),
        schema="reco",
    )

    # 3. Migrate existing values: copy match_key_strategy from flow to all its sources
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE reco.flow_source fs "
            "SET match_key_strategy = f.match_key_strategy "
            "FROM reco.flow f "
            "WHERE fs.flow_id = f.id"
        )
    )

    # 4. Set default for any remaining NULLs
    conn.execute(
        sa.text(
            "UPDATE reco.flow_source "
            "SET match_key_strategy = 'reco_id_amount' "
            "WHERE match_key_strategy IS NULL"
        )
    )

    # 5. Make match_key_strategy NOT NULL now that data is migrated
    op.alter_column(
        "flow_source",
        "match_key_strategy",
        nullable=False,
        server_default="reco_id_amount",
        schema="reco",
    )

    # 6. Drop match_key_strategy from flow table
    op.drop_column("flow", "match_key_strategy", schema="reco")


def downgrade() -> None:
    # 1. Re-add match_key_strategy to flow
    op.add_column(
        "flow",
        sa.Column(
            "match_key_strategy",
            sa.String(32),
            nullable=False,
            server_default="reco_id_amount",
        ),
        schema="reco",
    )

    # 2. Migrate back: take match_key_strategy from first source of each flow
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE reco.flow f "
            "SET match_key_strategy = fs.match_key_strategy "
            "FROM reco.flow_source fs "
            "WHERE fs.flow_id = f.id AND fs.id = ("
            "  SELECT MIN(fs2.id) FROM reco.flow_source fs2 WHERE fs2.flow_id = f.id"
            ")"
        )
    )

    # 3. Drop match_key_strategy and file_pattern from flow_source
    op.drop_column("flow_source", "match_key_strategy", schema="reco")
    op.drop_column("flow_source", "file_pattern", schema="reco")
