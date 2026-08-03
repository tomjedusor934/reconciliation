"""Bulk-booked payments: po_id -> bulk reco_id map.

Adds ``reco.payment_bulk_key``. An instant payment is normally 1 PaymentNumber =
1 MessageID, which is why the finacle movement's ``ref_no`` is used directly as
``reco_id`` (and why the MT940 leg resolves to that same PaymentNumber). Payments
coming from multiline break it: N instant payments share one MessageID and are
booked in bulk, so the counterpart is a single line while our N movements sit on
N distinct keys — the group can never balance.

The ingestion DAG detects those groups against std.Payment (>= 2 PaymentNumbers
on one MessageID, or a multiline ``InitModule``) and stores the mapping here; it
is read back at the start of every run so a late movement of a known bulk is
keyed on the MessageID straight away. Only bulk members are stored.

NOTE: the migration chain still carries the duplicate revision id "003" described
in 006 — boot-time init (Base.metadata.create_all) remains the primary deployment
path; this migration is provided for alembic-managed DBs once the chain is
repaired. Beware: the file names are offset by one from the revision ids
(009_entry_payment_status_by_reco_id.py holds revision "008").

Revision ID: 009
Revises: 008
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reco.payment_bulk_key (
            po_id VARCHAR(64) PRIMARY KEY,
            reco_id VARCHAR(128) NOT NULL,
            init_module VARCHAR(32),
            member_count INTEGER,
            -- naive timestamps with Python-side defaults, matching Base /
            -- create_all output for every other reco.* table
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payment_bulk_key_reco_id "
        "ON reco.payment_bulk_key (reco_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.payment_bulk_key")
