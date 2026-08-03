"""Amount per payment on reco.entry_payment_status.

Adds ``amount`` (std.Payment amount) so the lot view can sum the REAL pending
amount over a lot's PDNG payments — precise even for partially-pending bulks,
where summing the member's own movement amount over-counts. Nullable: existing
rows stay NULL until the sync_payment_status DAG re-runs (PS_FULL_SYNC
backfills).

NOTE: like the surrounding migrations, boot-time init
(Base.metadata.create_all) remains the primary deployment path; this migration
is provided for alembic-managed DBs. Idempotent so it is safe either way.

Revision ID: 007
Revises: 006
Create Date: 2026-07-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reco.entry_payment_status "
        "ADD COLUMN IF NOT EXISTS amount NUMERIC(20, 4)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reco.entry_payment_status DROP COLUMN IF EXISTS amount")
