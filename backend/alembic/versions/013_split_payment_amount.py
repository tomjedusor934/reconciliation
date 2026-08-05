"""A ghost shares out its movement's amount; the payment gap becomes data.

Ghosts used to be priced AT their bucket's ``std.Payment`` sum, with a residual
ghost absorbing whatever that did not match. A movement could therefore emit a
slice unrelated to what the bank booked: in prod, 184 NDGB movements resolved the
same 20 000-payment group, each claimed the whole of it, and one bucket reached
-334 M€ while residual ghosts "conserved" the totals with equally monstrous
opposites.

Ghosts now share out the movement's own amount, so ``Σ children == amount`` holds
by construction and no residual is ever emitted. What ``std.Payment`` says moves
to ``payment_amount`` — comparing it to ``amount`` is the real data-quality
control, and it belongs on the movement, not inside a bucket.
``shared_key_movements`` records how many movements resolved through the same
aggregate key, which is what makes such a gap likely.

Existing ``RESIDUAL`` lots and inflated ghosts are NOT migrated: the amounts are
wrong at the source. Run ``backend/scripts/reset_bb_buckets.sql`` and re-ingest.

NOTE: like the surrounding migrations, boot-time init (Base.metadata.create_all
plus the DDL in ``app/db/init_reco.py``) remains the primary deployment path;
this migration is provided for alembic-managed DBs. Idempotent so it is safe
either way. Beware: the file names are offset by one from the revision ids, and
the chain still carries the duplicate revision id "003" described in 006.

Revision ID: 012
Revises: 011
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS payment_amount NUMERIC(20, 4) NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS shared_key_movements INTEGER NOT NULL DEFAULT 1"
    )
    # Superseded: the gap is now amount - payment_amount, carried by no ghost.
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS residual_amount")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS residual_amount NUMERIC(20, 4) NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS shared_key_movements")
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS payment_amount")
