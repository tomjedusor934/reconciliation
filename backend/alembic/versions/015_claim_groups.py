"""Claim groups: ghosts once per aggregate key + the parent_mismatch lot tag.

Ghosts used to be emitted per split parent. When Finacle books one batch as N
entries sharing an aggregate key, all N resolve the same payment group, so every
bucket's ghost was duplicated N times — and the prorata that capped each parent
at its own amount left largest-remainder cents across the lots. Ghosts are now
emitted once per CLAIM GROUP ``(flow_source_id, claim_key_type,
claim_key_value)`` at the buckets' exact payment sums, and the second
reconciliation compares Σ(parents.amount) with Σ(ghosts that exist) per group:
a non-zero delta tags every lot carrying one of the group's ghosts
(``movement_lot.parent_mismatch``), matched or not.

``payment_trusted`` recorded which valuation regime priced a parent's ghosts;
with the prorata regime gone it has nothing left to say and is dropped.

NOTE: like the surrounding migrations, boot-time init (Base.metadata.create_all
plus the DDL in ``app/db/init_reco.py``) remains the primary deployment path;
this migration is provided for alembic-managed DBs. Idempotent so it is safe
either way. Beware: the file names are offset by one from the revision ids, and
the chain still carries the duplicate revision id "003" described in 006.

Revision ID: 014
Revises: 013
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS claim_key_type VARCHAR(16) NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS claim_key_value VARCHAR(128) NOT NULL DEFAULT ''"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_movement_split_claim "
        "ON reco.movement_split (flow_source_id, claim_key_type, claim_key_value)"
    )
    op.execute(
        "ALTER TABLE reco.movement_lot "
        "ADD COLUMN IF NOT EXISTS parent_mismatch BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS payment_trusted")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS payment_trusted BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute("ALTER TABLE reco.movement_lot DROP COLUMN IF EXISTS parent_mismatch")
    op.execute("DROP INDEX IF EXISTS reco.ix_movement_split_claim")
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS claim_key_value")
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS claim_key_type")
