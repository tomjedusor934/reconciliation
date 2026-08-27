"""Add the WERO parser type so the datamart WERO source can be persisted.

WERO is a payment reconciliation (the datamart WERO table against std.Payment /
std.[Return] on the end-to-end reference), not a std.Movement extraction, so it
gets its own parser_type: the finacle and batch-booking extractors self-select
on that value and therefore ignore it, and the new ingest_wero DAG picks it up.

Only the enum label is added here. The flow/source rows themselves come from
``seed_flows.py`` (idempotent, run at boot), and the WERO flow is seeded
INACTIVE until the datamart identifiers are confirmed — so nothing starts
extracting as a side effect of this migration.

NOTE: like the surrounding migrations, boot-time init
(``Base.metadata.create_all`` plus the DDL in ``app/db/init_reco.py``) remains
the primary deployment path; this migration is provided for alembic-managed DBs
and is idempotent either way. Beware: the file names are offset by one from the
revision ids, and the chain still carries the duplicate revision id "003"
described in 006.

Revision ID: 016
Revises: 015
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Labels are the Python enum NAMES (uppercase) — SQLAlchemy stores names, not
# values. Resolve the type's real schema: create_all builds it unqualified
# (usually public), while alembic-001 DBs keep the column as varchar and have
# no such type at all, in which case this is a clean no-op.
_ADD_ENUM_LABEL = """
DO $$
DECLARE v_schema text;
BEGIN
    SELECT n.nspname INTO v_schema
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typname = 'parser_type' AND t.typtype = 'e'
    LIMIT 1;
    IF v_schema IS NOT NULL THEN
        EXECUTE format(
            'ALTER TYPE %I.parser_type ADD VALUE IF NOT EXISTS %L',
            v_schema, 'WERO'
        );
    END IF;
END $$;
"""


def upgrade() -> None:
    # New enum labels must be committed before any row can use them.
    with op.get_context().autocommit_block():
        op.execute(_ADD_ENUM_LABEL)


def downgrade() -> None:
    # A native enum label cannot be dropped (same as 006). Nothing to undo:
    # removing the WERO source is a seed/data concern, not a schema one.
    pass
