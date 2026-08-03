"""Per-entry payment statuses (std.Payment sync).

Adds ``reco.entry_payment_status``: one row per (reconciliation entry,
payment) with the payment's ``std.Payment.Status``, synced by the
sync_payment_status DAG task and shown as an aggregate column in the
operational view. ``entry_id`` is a LOGICAL reference to
``reco.reconciliation_entry.id`` (partitioned table → no FK, same convention
as ``reco.exclusion``); the id survives the move to the émargement table.

NOTE: the migration chain still has the duplicate revision id "003" described
in 006 — boot-time init (Base.metadata.create_all) remains the primary
deployment path; this migration is provided for alembic-managed DBs once the
chain is repaired.

Revision ID: 006
Revises: 005
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reco.entry_payment_status (
            id BIGSERIAL PRIMARY KEY,
            entry_id BIGINT NOT NULL,
            po_id VARCHAR(64) NOT NULL,
            status VARCHAR(32),
            -- naive timestamps with Python-side defaults, matching Base /
            -- create_all output for every other reco.* table
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            CONSTRAINT uq_entry_payment_status_entry_po UNIQUE (entry_id, po_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_payment_status_entry_id "
        "ON reco.entry_payment_status (entry_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_payment_status_po_id "
        "ON reco.entry_payment_status (po_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.entry_payment_status")
