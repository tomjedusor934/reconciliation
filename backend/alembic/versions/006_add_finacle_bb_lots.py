"""Finacle Batch Booking True: movement lot tables + parser flip.

Adds the three ``reco.movement_lot*`` tables (lots / members / keys) built by
the ingest_finacle_bb DAG, registers the ``FINACLE_BATCH_BOOKING_TRUE`` label
on the native ``parser_type`` enum when one exists (create_all-provisioned DBs;
alembic-001 DBs store the column as varchar → no-op), and flips the
``float_account_outward``/``finacle_db`` source to the new parser.

NOTE: the migration chain currently has a DUPLICATE revision id "003"
(003_add_exclusion_cancelled_fields.py and 004_move_accounts_to_finacle_source.py
both declare it) — ``alembic upgrade head`` fails to load until that is fixed.
Boot-time init (create_all + init_reco_db + seed_flows) performs the exact same
changes and remains the primary deployment path; this migration is provided for
alembic-managed DBs once the chain is repaired.

Revision ID: 005
Revises: 004
Create Date: 2026-07-13 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADD_ENUM_LABEL = """
DO $$
DECLARE v_schema text;
BEGIN
    SELECT n.nspname INTO v_schema
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typname = 'parser_type' AND t.typtype = 'e'
    LIMIT 1;
    IF v_schema IS NOT NULL THEN
        EXECUTE format(
            'ALTER TYPE %I.parser_type ADD VALUE IF NOT EXISTS %L',
            v_schema, 'FINACLE_BATCH_BOOKING_TRUE'
        );
    END IF;
END $$;
"""

# ORM-written rows store enum NAMES ('FINACLE_DB'); rows copied by migration 001
# into a varchar column may hold lowercase values — UPPER() covers both.
_FLIP_PARSER = """
UPDATE reco.flow_source fs
SET parser_type = 'FINACLE_BATCH_BOOKING_TRUE'
FROM reco.flow f
WHERE fs.flow_id = f.id
  AND f.code = 'float_account_outward'
  AND fs.code = 'finacle_db'
  AND UPPER(fs.parser_type::text) = 'FINACLE_DB'
"""

_UNFLIP_PARSER = """
UPDATE reco.flow_source fs
SET parser_type = 'FINACLE_DB'
FROM reco.flow f
WHERE fs.flow_id = f.id
  AND f.code = 'float_account_outward'
  AND fs.code = 'finacle_db'
  AND UPPER(fs.parser_type::text) = 'FINACLE_BATCH_BOOKING_TRUE'
"""


def upgrade() -> None:
    # New enum labels must be committed before any row can use them.
    with op.get_context().autocommit_block():
        op.execute(_ADD_ENUM_LABEL)

    op.create_table(
        "movement_lot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("flow_id", sa.Integer, sa.ForeignKey("reco.flow.id"), nullable=False),
        sa.Column("flow_source_id", sa.Integer, sa.ForeignKey("reco.flow_source.id"), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="EUR"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("merged_into_lot_id", sa.String(36), sa.ForeignKey("reco.movement_lot.id"), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_conflict", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="reco",
    )
    op.create_index("ix_movement_lot_source_status", "movement_lot", ["flow_source_id", "status"], schema="reco")
    op.create_index("ix_movement_lot_flow", "movement_lot", ["flow_id"], schema="reco")

    op.create_table(
        "movement_lot_member",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "lot_id", sa.String(36),
            sa.ForeignKey("reco.movement_lot.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("movement_type", sa.String(16), nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("account", sa.String(64), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("direction", sa.String(8), nullable=True),
        sa.Column("value_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transaction_particulars", sa.String(512), nullable=True),
        sa.Column("ref_no", sa.String(128), nullable=True),
        sa.Column("remarks_1", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_hash", name="uq_movement_lot_member_source_hash"),
        schema="reco",
    )
    op.create_index("ix_movement_lot_member_lot_id", "movement_lot_member", ["lot_id"], schema="reco")

    op.create_table(
        "movement_lot_key",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "member_id", sa.BigInteger,
            sa.ForeignKey("reco.movement_lot_member.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("key_type", sa.String(8), nullable=False),
        sa.Column("key_value", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("member_id", "key_type", "key_value", name="uq_movement_lot_key_member"),
        schema="reco",
    )
    op.create_index("ix_movement_lot_key_member_id", "movement_lot_key", ["member_id"], schema="reco")
    op.create_index("ix_movement_lot_key_value", "movement_lot_key", ["key_type", "key_value"], schema="reco")

    op.execute(_FLIP_PARSER)


def downgrade() -> None:
    op.execute(_UNFLIP_PARSER)
    op.drop_table("movement_lot_key", schema="reco")
    op.drop_table("movement_lot_member", schema="reco")
    op.drop_table("movement_lot", schema="reco")
    # A native enum label cannot be dropped — 'FINACLE_BATCH_BOOKING_TRUE' stays.
