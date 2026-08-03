"""entry_payment_status: key on reco_id instead of entry_id.

The table used to hold one row per ENTRY. All entries sharing a reco_id are the
same reconciliation — and for the batch-booking flow reco_id is the lot uuid —
so the per-entry keying fanned a lot's payments out over every member and
produced tens of millions of duplicate rows. It now holds one row per
reconciliation GROUP.

The table is a pure sync cache (the sync_payment_status DAG re-pushes every
pending reco on each run, with no datamart watermark), so the legacy rows are
dropped rather than migrated: they are exactly the fan-out this change removes.
Run the DAG once with PS_FULL_SYNC to also repopulate already-émargé recos.

NOTE: this file closes the revision-"008" hole referenced by 010's docstring.
The chain is still not runnable end to end — 003_add_exclusion_cancelled_fields
and 004_move_accounts_to_finacle_source both declare revision "003". Boot-time
init (Base.metadata.create_all + app/db/init_reco.py) remains the primary
deployment path; ``_rebuild_legacy_entry_payment_status`` there performs this
same conversion idempotently.

Revision ID: 008
Revises: 007
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CREATE = """
CREATE TABLE IF NOT EXISTS reco.entry_payment_status (
    id BIGSERIAL PRIMARY KEY,
    {key_column} NOT NULL,
    po_id VARCHAR(64) NOT NULL,
    status VARCHAR(32),
    amount NUMERIC(20, 4),
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    CONSTRAINT {constraint} UNIQUE ({key_name}, po_id)
)
"""


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.entry_payment_status")
    op.execute(
        _CREATE.format(
            key_column="reco_id VARCHAR(128)",
            key_name="reco_id",
            constraint="uq_entry_payment_status_reco_po",
        )
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_payment_status_reco_id "
        "ON reco.entry_payment_status (reco_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_payment_status_po_id "
        "ON reco.entry_payment_status (po_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.entry_payment_status")
    op.execute(
        _CREATE.format(
            key_column="entry_id BIGINT",
            key_name="entry_id",
            constraint="uq_entry_payment_status_entry_po",
        )
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_payment_status_entry_id "
        "ON reco.entry_payment_status (entry_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entry_payment_status_po_id "
        "ON reco.entry_payment_status (po_id)"
    )
