"""Re-key reco.entry_payment_status by reco_id instead of entry_id.

Per-entry keying fanned a batch's payments out across every member movement
(batch bookings share a MessageID/pacs008), producing tens of millions of
duplicate rows. reco_id keys the payment set to the reconciliation GROUP once
(for the batch-booking flow reco_id == the lot uuid), so a lot's payments are
stored a single time. This DROPs and recreates the table (clearing the old
fanned-out rows); re-run the sync_payment_status DAG afterwards to repopulate.

NOTE: boot-time init (Base.metadata.create_all) remains the primary deployment
path — dropping the table and restarting the backend recreates it with this
schema. This migration is provided for alembic-managed DBs.

Revision ID: 008
Revises: 007
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.entry_payment_status")
    op.execute(
        """
        CREATE TABLE reco.entry_payment_status (
            id BIGSERIAL PRIMARY KEY,
            reco_id VARCHAR(128) NOT NULL,
            po_id VARCHAR(64) NOT NULL,
            status VARCHAR(32),
            amount NUMERIC(20, 4),
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            CONSTRAINT uq_entry_payment_status_reco_po UNIQUE (reco_id, po_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_entry_payment_status_reco_id "
        "ON reco.entry_payment_status (reco_id)"
    )
    op.execute(
        "CREATE INDEX ix_entry_payment_status_po_id "
        "ON reco.entry_payment_status (po_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.entry_payment_status")
