"""Record which regime valued a split's ghosts.

A ghost is worth its bucket's payment sum to the cent — that exactness is what
makes the bucket balance against the real movement booked for the same payments.
When several movements claim one payment group, the sums cannot be trusted and
the movement's own amount is shared out instead; those buckets are not expected
to balance exactly.

``payment_trusted`` says which happened, so a lot's behaviour is explainable
without rerunning the DAG. Defaults to true: rows written before this column
existed were all valued at the payment sums.

NOTE: like the surrounding migrations, boot-time init (Base.metadata.create_all
plus the DDL in ``app/db/init_reco.py``) remains the primary deployment path;
this migration is provided for alembic-managed DBs. Idempotent so it is safe
either way. Beware: the file names are offset by one from the revision ids, and
the chain still carries the duplicate revision id "003" described in 006.

Revision ID: 013
Revises: 012
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE reco.movement_split "
        "ADD COLUMN IF NOT EXISTS payment_trusted BOOLEAN NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE reco.movement_split DROP COLUMN IF EXISTS payment_trusted")
