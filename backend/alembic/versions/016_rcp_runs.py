"""Persist RCP reattribution runs so a finished report survives the browser.

The analysis is a batch and the operator should not have to watch it: the
in-memory job registry dies with the polling loop and with any backend restart,
so a run left alone for twenty minutes came back to an empty screen. Each run is
now recorded here (running → done/error) with its full report in ``result``.

Logs are not stored — they are live commentary; the ``phase`` reached and the
``error`` are what a finished run needs to explain itself.

NOTE: like the surrounding migrations, boot-time init
(``Base.metadata.create_all``) remains the primary deployment path; this
migration is provided for alembic-managed DBs and is idempotent either way.
Beware: the file names are offset by one from the revision ids, and the chain
still carries the duplicate revision id "003" described in 006.

Revision ID: 015
Revises: 014
Create Date: 2026-08-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reco.rcp_run (
            id           VARCHAR(36) PRIMARY KEY,
            kind         VARCHAR(16) NOT NULL,
            status       VARCHAR(16) NOT NULL,
            phase        VARCHAR(128) NOT NULL DEFAULT '',
            user_id      INTEGER NULL,
            label        VARCHAR(512) NOT NULL DEFAULT '',
            movements    INTEGER NOT NULL DEFAULT 0,
            actionable   INTEGER NOT NULL DEFAULT 0,
            applied      INTEGER NOT NULL DEFAULT 0,
            failed       INTEGER NOT NULL DEFAULT 0,
            error        TEXT NOT NULL DEFAULT '',
            result       JSONB NULL,
            started_at   TIMESTAMPTZ NOT NULL,
            finished_at  TIMESTAMPTZ NULL,
            created_at   TIMESTAMP NULL,
            updated_at   TIMESTAMP NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rcp_run_started_at "
        "ON reco.rcp_run (started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reco.rcp_run")
