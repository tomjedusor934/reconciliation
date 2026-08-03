"""Add flow_source table and migrate source config from flow.

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create flow_source table
    op.create_table(
        "flow_source",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("flow_id", sa.Integer(), sa.ForeignKey("reco.flow.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("parser_type", sa.String(32), nullable=False),
        sa.Column("inbox_subfolder", sa.String(128), nullable=True),
        sa.Column("parser_config", postgresql.JSONB(), nullable=True),
        sa.Column("finacle_query", sa.Text(), nullable=True),
        sa.Column("finacle_connection_id", sa.Integer(), sa.ForeignKey("reco.source_connection.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("flow_id", "code", name="uq_flow_source_code"),
        schema="reco",
    )

    # 2. Add flow_source_id FK to ingestion_run
    op.add_column(
        "ingestion_run",
        sa.Column("flow_source_id", sa.Integer(), sa.ForeignKey("reco.flow_source.id", ondelete="SET NULL"), nullable=True, index=True),
        schema="reco",
    )

    # 3. Migrate existing flow source config into flow_source rows
    conn = op.get_bind()
    flows = conn.execute(
        sa.text(
            "SELECT id, code, name, source_type, parser_type, inbox_subfolder, "
            "parser_config, finacle_query, finacle_connection_id "
            "FROM reco.flow"
        )
    ).fetchall()

    for f in flows:
        conn.execute(
            sa.text(
                "INSERT INTO reco.flow_source "
                "(flow_id, code, name, description, is_active, source_type, parser_type, "
                "inbox_subfolder, parser_config, finacle_query, finacle_connection_id) "
                "VALUES (:flow_id, :code, :name, :desc, true, :source_type, :parser_type, "
                ":inbox_subfolder, :parser_config, :finacle_query, :finacle_connection_id)"
            ),
            {
                "flow_id": f.id,
                "code": f"legacy_{f.code}",
                "name": f"{f.name} (migrated)",
                "desc": f"Auto-migrated from flow {f.code}",
                "source_type": f.source_type or "file",
                "parser_type": f.parser_type or "csv",
                "inbox_subfolder": f.inbox_subfolder,
                "parser_config": f.parser_config,
                "finacle_query": f.finacle_query,
                "finacle_connection_id": f.finacle_connection_id,
            },
        )

    # 4. Drop old source columns from flow
    for col in [
        "source_type", "parser_type", "inbox_subfolder",
        "parser_config", "finacle_query", "finacle_connection_id",
    ]:
        try:
            op.drop_column("flow", col, schema="reco")
        except Exception:
            pass  # column may already be absent


def downgrade() -> None:
    # Re-add source columns to flow
    op.add_column("flow", sa.Column("source_type", sa.String(32), nullable=True), schema="reco")
    op.add_column("flow", sa.Column("parser_type", sa.String(32), nullable=True), schema="reco")
    op.add_column("flow", sa.Column("inbox_subfolder", sa.String(128), nullable=True), schema="reco")
    op.add_column("flow", sa.Column("parser_config", postgresql.JSONB(), nullable=True), schema="reco")
    op.add_column("flow", sa.Column("finacle_query", sa.Text(), nullable=True), schema="reco")
    op.add_column("flow", sa.Column("finacle_connection_id", sa.Integer(), nullable=True), schema="reco")

    # Migrate data back from flow_source to flow (take first source per flow)
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE reco.flow f SET "
            "source_type = fs.source_type, "
            "parser_type = fs.parser_type, "
            "inbox_subfolder = fs.inbox_subfolder, "
            "parser_config = fs.parser_config, "
            "finacle_query = fs.finacle_query, "
            "finacle_connection_id = fs.finacle_connection_id "
            "FROM reco.flow_source fs "
            "WHERE fs.flow_id = f.id AND fs.id = ("
            "  SELECT MIN(fs2.id) FROM reco.flow_source fs2 WHERE fs2.flow_id = f.id"
            ")"
        )
    )

    # Drop flow_source_id from ingestion_run
    op.drop_column("ingestion_run", "flow_source_id", schema="reco")

    # Drop flow_source table
    op.drop_table("flow_source", schema="reco")
