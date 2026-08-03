"""Move reference accounts from flow to flow_source; drop finacle query/connection.

Reference accounts only make sense for Finacle-DB sources (WHERE account IN ...),
so they now live on the source. The Finacle extraction itself moved to the
Airflow DAG (connected via an Airflow connection), making `finacle_query` and
`finacle_connection_id` dead config.

Revision ID: 003
Revises: 002
Create Date: 2026-06-10 00:00:00.000000

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
    # 1. Create flow_source_account table
    op.create_table(
        "flow_source_account",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "flow_source_id",
            sa.Integer(),
            sa.ForeignKey("reco.flow_source.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("account_number", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.UniqueConstraint("flow_source_id", "account_number", name="uq_flow_source_account"),
        schema="reco",
    )

    # 2. Copy existing flow-level accounts to every finacle_db source of the flow.
    #    UPPER() because ORM-written rows store enum names ('FINACLE_DB') while
    #    rows migrated by 001 may hold the lowercase value.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO reco.flow_source_account (flow_source_id, account_number, label) "
            "SELECT fs.id, fa.account_number, fa.label "
            "FROM reco.flow_account fa "
            "JOIN reco.flow_source fs ON fs.flow_id = fa.flow_id "
            "WHERE UPPER(fs.source_type) = 'FINACLE_DB'"
        )
    )

    # 3. Drop flow-level accounts table
    op.drop_table("flow_account", schema="reco")

    # 4. Drop dead Finacle config columns (extraction now happens in the DAG)
    op.drop_column("flow_source", "finacle_query", schema="reco")
    op.drop_column("flow_source", "finacle_connection_id", schema="reco")


def downgrade() -> None:
    # 1. Re-add Finacle config columns to flow_source
    op.add_column("flow_source", sa.Column("finacle_query", sa.Text(), nullable=True), schema="reco")
    op.add_column(
        "flow_source",
        sa.Column(
            "finacle_connection_id",
            sa.Integer(),
            sa.ForeignKey("reco.source_connection.id"),
            nullable=True,
        ),
        schema="reco",
    )

    # 2. Recreate flow_account
    op.create_table(
        "flow_account",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "flow_id",
            sa.Integer(),
            sa.ForeignKey("reco.flow.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("account_number", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.UniqueConstraint("flow_id", "account_number", name="uq_flow_account"),
        schema="reco",
    )

    # 3. Move accounts back up to the flow (deduplicated across sources)
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO reco.flow_account (flow_id, account_number, label) "
            "SELECT DISTINCT ON (fs.flow_id, fsa.account_number) "
            "fs.flow_id, fsa.account_number, fsa.label "
            "FROM reco.flow_source_account fsa "
            "JOIN reco.flow_source fs ON fs.id = fsa.flow_source_id "
            "ORDER BY fs.flow_id, fsa.account_number, fsa.id"
        )
    )

    # 4. Drop flow_source_account
    op.drop_table("flow_source_account", schema="reco")
