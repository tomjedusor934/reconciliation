"""Raw movement TransactionID on entries + payment CreatedOn timestamp on statuses.

``transaction_id`` carries the raw ``std.Movement.TransactionID`` on both the live
and the émargement entry tables. It is the business reference only: finacle
recycles the id daily and one id covers several legs, so it gets no index, no
unique constraint, and stays out of every identity hash (``external_ref`` remains
the identity key).

``payment_timestamp`` carries ``std.Payment.CreatedOn`` per payment so the
operational view can display the reco's timestamp span and filter down to the
hour. It is TIMESTAMP **WITHOUT TIME ZONE** on purpose — the whole chain keeps the
datamart wall clock naive so the hour displayed is the hour filtered on (see
``app/models/payment_status.py``).

NOTE: like the surrounding migrations, boot-time init (Base.metadata.create_all
plus the ``_ADDED_COLUMNS`` / ``_DROPPED_COLUMNS`` DDL in ``app/db/init_reco.py``)
remains the primary deployment path; this migration is provided for
alembic-managed DBs. Idempotent so it is safe either way. Beware: the file names
are offset by one from the revision ids, and the chain still carries the duplicate
revision id "003" described in 006.

Revision ID: 010
Revises: 009
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENTRY_TABLES = (
    "reco.reconciliation_entry",
    "reco.reconciliation_entry_emargement",
)


def upgrade() -> None:
    for table in _ENTRY_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS transaction_id VARCHAR(128)")
    op.execute(
        "ALTER TABLE reco.entry_payment_status "
        "ADD COLUMN IF NOT EXISTS payment_timestamp TIMESTAMP"
    )
    # Superseded: a DATE cannot carry the hour the operational filter needs.
    op.execute("ALTER TABLE reco.entry_payment_status DROP COLUMN IF EXISTS req_exec_date")


def downgrade() -> None:
    op.execute("ALTER TABLE reco.entry_payment_status DROP COLUMN IF EXISTS payment_timestamp")
    for table in _ENTRY_TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS transaction_id")
