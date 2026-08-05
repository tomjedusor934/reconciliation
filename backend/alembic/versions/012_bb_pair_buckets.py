"""Batch-booking lots become (PACS008 × MSGID) buckets, with ghost movements.

The union-find clustering over {PACS008, MSGID, PO} keys is retired: label-like
MessageIDs ('LUXEMBOURG', 'ESCH/ALZETTE') glued unrelated pacs008 together and
produced 50k-member lots that could never balance. A lot is now ONE bucket,
identified by the pair it stands for, and a movement whose payments span several
buckets is replaced by one ghost entry per bucket.

Adds:
- bucket identity + ``synthetic_only`` on ``movement_lot``, with a uniqueness
  constraint over the identity. Absent components are '' rather than NULL so
  the constraint actually bites (Postgres treats NULLs as distinct). Lots
  inherited from the clustering are backfilled to kind 'LEGACY' with their own
  id as ``bucket_ref``, which keeps each of them unique.
- ``reco.movement_split``: the real movements that were replaced by ghosts.
- ``split_parent_hash`` on both entry tables and on ``movement_lot_member``,
  plus ``payment_count`` on the member.

NOTE: like the surrounding migrations, boot-time init (Base.metadata.create_all
plus the DDL in ``app/db/init_reco.py``) remains the primary deployment path;
this migration is provided for alembic-managed DBs. Idempotent so it is safe
either way. Beware: the file names are offset by one from the revision ids, and
the chain still carries the duplicate revision id "003" described in 006.

Revision ID: 011
Revises: 010
Create Date: 2026-08-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENTRY_TABLES = (
    "reco.reconciliation_entry",
    "reco.reconciliation_entry_emargement",
)

_BUCKET_COLUMNS = (
    ("bucket_kind", "VARCHAR(16) NOT NULL DEFAULT 'LEGACY'"),
    ("bucket_pacs008", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("bucket_msgid", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("bucket_po", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("bucket_ref", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("synthetic_only", "BOOLEAN NOT NULL DEFAULT false"),
)

_MOVEMENT_SPLIT = """
CREATE TABLE IF NOT EXISTS reco.movement_split (
    source_hash             VARCHAR(64) PRIMARY KEY,
    flow_id                 INTEGER NOT NULL REFERENCES reco.flow(id),
    flow_source_id          INTEGER NOT NULL REFERENCES reco.flow_source(id),
    movement_type           VARCHAR(16) NOT NULL,
    external_ref            VARCHAR(128),
    account                 VARCHAR(64),
    currency                VARCHAR(8) NOT NULL,
    amount                  NUMERIC(20, 4) NOT NULL,
    direction               VARCHAR(8),
    value_date              TIMESTAMPTZ NOT NULL,
    operation_date          TIMESTAMPTZ,
    transaction_particulars VARCHAR(512),
    ref_no                  VARCHAR(128),
    remarks_1               VARCHAR(255),
    payload_raw             JSONB,
    child_count             INTEGER NOT NULL DEFAULT 0,
    payment_count           INTEGER NOT NULL DEFAULT 0,
    child_amount            NUMERIC(20, 4) NOT NULL DEFAULT 0,
    residual_amount         NUMERIC(20, 4) NOT NULL DEFAULT 0,
    parent_emarged          BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def upgrade() -> None:
    # 1. Bucket identity on the lot.
    for column, sql_type in _BUCKET_COLUMNS:
        op.execute(
            f"ALTER TABLE reco.movement_lot ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        )
    # Pre-bucket lots keep their own id as identity so no two of them collide
    # under the constraint created below.
    op.execute(
        "UPDATE reco.movement_lot SET bucket_kind = 'LEGACY', bucket_ref = id "
        "WHERE bucket_kind = 'LEGACY' AND bucket_ref = ''"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_movement_lot_bucket "
        "ON reco.movement_lot "
        "(flow_source_id, bucket_kind, bucket_pacs008, bucket_msgid, bucket_po, bucket_ref)"
    )

    # 2. The split parents.
    op.execute(_MOVEMENT_SPLIT)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_movement_split_source_date "
        "ON reco.movement_split (flow_source_id, value_date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_movement_split_external_ref "
        "ON reco.movement_split (external_ref)"
    )

    # 3. Ghost -> parent links.
    for table in _ENTRY_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS split_parent_hash VARCHAR(64)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reconciliation_entry_split_parent "
        "ON reco.reconciliation_entry (split_parent_hash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_emargement_split_parent "
        "ON reco.reconciliation_entry_emargement (split_parent_hash)"
    )
    op.execute(
        "ALTER TABLE reco.movement_lot_member "
        "ADD COLUMN IF NOT EXISTS split_parent_hash VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE reco.movement_lot_member ADD COLUMN IF NOT EXISTS payment_count INTEGER"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_movement_lot_member_split_parent "
        "ON reco.movement_lot_member (split_parent_hash)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS reco.ix_movement_lot_member_split_parent")
    op.execute("ALTER TABLE reco.movement_lot_member DROP COLUMN IF EXISTS payment_count")
    op.execute("ALTER TABLE reco.movement_lot_member DROP COLUMN IF EXISTS split_parent_hash")
    op.execute("DROP INDEX IF EXISTS reco.ix_emargement_split_parent")
    op.execute("DROP INDEX IF EXISTS reco.ix_reconciliation_entry_split_parent")
    for table in _ENTRY_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS split_parent_hash")
    op.execute("DROP TABLE IF EXISTS reco.movement_split")
    op.execute("DROP INDEX IF EXISTS reco.uq_movement_lot_bucket")
    for column, _sql_type in _BUCKET_COLUMNS:
        op.execute(f"ALTER TABLE reco.movement_lot DROP COLUMN IF EXISTS {column}")
