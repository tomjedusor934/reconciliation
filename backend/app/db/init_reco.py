"""Postgres-side initialization for the reconciliation app.

Creates:
- schemas ``reco`` and ``audit``
- generic audit trigger function and per-table triggers for every metier table
- helper function to set the current user id (audit context)
- schema evolution ``Base.metadata.create_all()`` cannot do (it creates missing
  tables but never ALTERs an existing one, and the alembic chain is not usable)

Idempotent: safe to call on every backend start.
"""
import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.payment_status import EntryPaymentStatus

logger = logging.getLogger(__name__)

# Native PG enum labels added to ``parser_type`` after the type was first
# created. Labels are the Python enum NAMES (uppercase), not their values —
# SQLAlchemy stores names. A label can never be dropped from a PG enum, so this
# list only ever grows; every entry is a no-op once applied (see step 4).
_PARSER_TYPE_LABELS = (
    "FINACLE_BATCH_BOOKING_TRUE",
    "WERO",
)
_ENUM_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Tables (under schema ``reco``) that should be audited via triggers.
# ``reconciliation_entry`` is excluded (very high volume — audit on inserts
# would explode). Only declarative metadata changes are audited.
_AUDITED_TABLES = [
    "flow",
    "flow_source",
    "flow_source_account",
    "source_connection",
    "match_group",
    "exclusion",
    "ingestion_run",
    "reconciliation_run",
]

# Columns added to already-existing tables. ``Base.metadata.create_all()`` creates
# missing TABLES but never ALTERs an existing one, so a new column on a deployed
# database has to be added here (same reasoning as init_db.py for the nullable
# hashed_password). ``ADD COLUMN IF NOT EXISTS`` makes every entry a no-op once
# applied, so the list is safe to keep growing.
_ADDED_COLUMNS = [
    # Raw std.Movement.TransactionID (business reference, see ReconciliationEntry).
    ("reco.reconciliation_entry", "transaction_id", "VARCHAR(128)"),
    ("reco.reconciliation_entry_emargement", "transaction_id", "VARCHAR(128)"),
    # std.Payment amount + creation timestamp (see EntryPaymentStatus). The
    # timestamp is deliberately WITHOUT TIME ZONE — see the model for why.
    ("reco.entry_payment_status", "amount", "NUMERIC(20, 4)"),
    ("reco.entry_payment_status", "payment_timestamp", "TIMESTAMP"),
    # Batch-booking buckets (see MovementLot). Absent identity components are ''
    # rather than NULL so uq_movement_lot_bucket actually bites.
    ("reco.movement_lot", "bucket_kind", "VARCHAR(16) NOT NULL DEFAULT 'LEGACY'"),
    ("reco.movement_lot", "bucket_pacs008", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("reco.movement_lot", "bucket_msgid", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("reco.movement_lot", "bucket_po", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("reco.movement_lot", "bucket_ref", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("reco.movement_lot", "synthetic_only", "BOOLEAN NOT NULL DEFAULT false"),
    # Ghost -> real movement link (see MovementSplit).
    ("reco.reconciliation_entry", "split_parent_hash", "VARCHAR(64)"),
    ("reco.reconciliation_entry_emargement", "split_parent_hash", "VARCHAR(64)"),
    ("reco.movement_lot_member", "split_parent_hash", "VARCHAR(64)"),
    ("reco.movement_lot_member", "payment_count", "INTEGER"),
    # What std.Payment says about a split movement, and how many movements share
    # its aggregate key (see MovementSplit). The gap amount - payment_amount
    # replaced the residual ghost.
    ("reco.movement_split", "payment_amount", "NUMERIC(20, 4) NOT NULL DEFAULT 0"),
    ("reco.movement_split", "shared_key_movements", "INTEGER NOT NULL DEFAULT 1"),
    # The claim group a split parent belongs to (see MovementSplit): ghosts are
    # emitted once per (flow_source_id, claim_key_type, claim_key_value) and the
    # second reconciliation compares Σ(parents) to Σ(ghosts) per group.
    ("reco.movement_split", "claim_key_type", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("reco.movement_split", "claim_key_value", "VARCHAR(128) NOT NULL DEFAULT ''"),
    # Set by the second reconciliation: this lot carries a ghost of a claim
    # group whose parents do not add up to its ghosts — even matched, the lot
    # is not fully validated (see MovementLot).
    ("reco.movement_lot", "parent_mismatch", "BOOLEAN NOT NULL DEFAULT false"),
]

# Indexes on pre-existing tables — ``create_all`` only builds the ones declared
# on a table it creates itself, so an index added to a deployed table lands here
# (same reasoning as _ADDED_COLUMNS). Applied AFTER the column loop, and after
# _backfill_legacy_lot_buckets for the ones that enforce uniqueness.
_ADDED_INDEXES = [
    (
        "uq_movement_lot_bucket",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_movement_lot_bucket "
        "ON reco.movement_lot "
        "(flow_source_id, bucket_kind, bucket_pacs008, bucket_msgid, bucket_po, bucket_ref)",
    ),
    (
        "ix_reconciliation_entry_split_parent",
        "CREATE INDEX IF NOT EXISTS ix_reconciliation_entry_split_parent "
        "ON reco.reconciliation_entry (split_parent_hash)",
    ),
    (
        "ix_emargement_split_parent",
        "CREATE INDEX IF NOT EXISTS ix_emargement_split_parent "
        "ON reco.reconciliation_entry_emargement (split_parent_hash)",
    ),
    (
        "ix_movement_lot_member_split_parent",
        "CREATE INDEX IF NOT EXISTS ix_movement_lot_member_split_parent "
        "ON reco.movement_lot_member (split_parent_hash)",
    ),
    (
        "ix_movement_split_claim",
        "CREATE INDEX IF NOT EXISTS ix_movement_split_claim "
        "ON reco.movement_split (flow_source_id, claim_key_type, claim_key_value)",
    ),
]

# Columns removed from already-existing tables — same reasoning as _ADDED_COLUMNS
# in reverse. ``DROP COLUMN IF EXISTS`` makes every entry a no-op once applied.
_DROPPED_COLUMNS = [
    # Superseded by payment_timestamp (std.Payment.CreatedOn): a DATE
    # cannot carry the hour the operational filter needs. Never populated — the
    # table had just been rebuilt empty when it shipped.
    ("reco.entry_payment_status", "req_exec_date"),
    # Superseded by payment_amount: the gap between std.Payment and the
    # accounting is now data on the movement, not a residual ghost in a bucket.
    ("reco.movement_split", "residual_amount"),
    # Superseded by claim groups: ghosts are always priced at their bucket's
    # exact payment sums, once per group — the prorata regime this flag used to
    # record is gone, and the group delta is what the second reconciliation
    # surfaces instead.
    ("reco.movement_split", "payment_trusted"),
]


def _rebuild_legacy_entry_payment_status(db: Session) -> None:
    """Convert ``entry_payment_status`` from the legacy ``entry_id`` key to ``reco_id``.

    The table used to hold one row per ENTRY; it now holds one row per
    reconciliation GROUP (see EntryPaymentStatus). The migration flipping it
    never made it into the alembic chain, and ``create_all`` does not ALTER an
    existing table — so a database provisioned before the change still carries
    ``entry_id``, and every read on ``reco_id`` fails with UndefinedColumn.

    The table is a pure sync cache: ``sync_payment_status`` re-pushes every
    pending reco on each run (no datamart watermark). The legacy rows are
    therefore dropped rather than migrated — they are precisely the per-entry
    fan-out that keying on reco_id removes, and they carry neither ``amount``
    nor ``payment_timestamp``. Re-run the DAG once with PS_FULL_SYNC to also
    repopulate the already-émargé recos.
    """
    is_legacy = db.execute(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'reco'
              AND table_name = 'entry_payment_status'
              AND column_name = 'entry_id'
            """
        )
    ).scalar()
    if not is_legacy:
        return

    logger.warning(
        "[init_reco] reco.entry_payment_status still keyed by entry_id — dropping "
        "and recreating it on reco_id; the sync_payment_status DAG repopulates it."
    )
    db.execute(text("DROP TABLE reco.entry_payment_status"))
    db.commit()
    EntryPaymentStatus.__table__.create(bind=db.get_bind(), checkfirst=True)
    db.commit()


def _backfill_legacy_lot_buckets(db: Session) -> None:
    """Give every pre-bucket lot a unique bucket identity before the constraint.

    Lots built by the retired union-find clustering are not buckets: they have no
    (pacs008, MessageID) pair to name them. They all default to kind 'LEGACY'
    with empty components, which would collide under ``uq_movement_lot_bucket``.
    Using their own id as ``bucket_ref`` keeps each one unique and marks them
    plainly as inherited. Idempotent: only touches rows still at the default.
    """
    result = db.execute(
        text(
            "UPDATE reco.movement_lot SET bucket_kind = 'LEGACY', bucket_ref = id "
            "WHERE bucket_kind = 'LEGACY' AND bucket_ref = ''"
        )
    )
    if result.rowcount:
        logger.info(
            "[init_reco] %d pre-bucket movement_lot row(s) marked LEGACY", result.rowcount
        )


_AUDIT_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION audit.fn_audit_row() RETURNS trigger AS $$
DECLARE
    v_user_id integer;
    v_pk text;
BEGIN
    BEGIN
        v_user_id := current_setting('app.current_user_id')::integer;
    EXCEPTION WHEN others THEN
        v_user_id := NULL;
    END;

    IF (TG_OP = 'DELETE') THEN
        v_pk := (to_jsonb(OLD)->>'id');
        INSERT INTO audit.audit_log (table_name, row_pk, op, old_data, new_data, user_id, ts)
        VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, v_pk, 'DELETE',
                to_jsonb(OLD), NULL, v_user_id, now());
        RETURN OLD;
    ELSIF (TG_OP = 'UPDATE') THEN
        v_pk := (to_jsonb(NEW)->>'id');
        INSERT INTO audit.audit_log (table_name, row_pk, op, old_data, new_data, user_id, ts)
        VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, v_pk, 'UPDATE',
                to_jsonb(OLD), to_jsonb(NEW), v_user_id, now());
        RETURN NEW;
    ELSE
        v_pk := (to_jsonb(NEW)->>'id');
        INSERT INTO audit.audit_log (table_name, row_pk, op, old_data, new_data, user_id, ts)
        VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, v_pk, 'INSERT',
                NULL, to_jsonb(NEW), v_user_id, now());
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;
"""


def init_reco_db(db: Session) -> None:
    """Idempotent Postgres-side init for the reconciliation app."""
    # 1. Schemas
    db.execute(text("CREATE SCHEMA IF NOT EXISTS reco"))
    db.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))
    db.commit()

    # 2. Audit trigger function (recreated each start to pick up updates)
    db.execute(text(_AUDIT_TRIGGER_FN))

    # 3. Per-table audit triggers
    for table in _AUDITED_TABLES:
        trg_name = f"trg_audit_{table}"
        db.execute(
            text(
                f"""
                DO $$ BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'reco' AND table_name = '{table}'
                    ) AND NOT EXISTS (
                        SELECT 1 FROM pg_trigger WHERE tgname = '{trg_name}'
                    ) THEN
                        CREATE TRIGGER {trg_name}
                            AFTER INSERT OR UPDATE OR DELETE ON reco."{table}"
                            FOR EACH ROW EXECUTE FUNCTION audit.fn_audit_row();
                    END IF;
                END $$;
                """
            )
        )

    db.commit()

    # 4. Enum evolution — parser_type gained FINACLE_BATCH_BOOKING_TRUE, then WERO.
    # DBs provisioned by create_all carry a native PG enum (labels = Python enum
    # NAMES) that predates the value; add it idempotently, resolving the type's
    # actual schema (SQLAlchemy creates it unqualified → usually public).
    # Alembic-001 DBs store this column as varchar and have no such type → no-op.
    # PG12+ allows ADD VALUE inside a transaction as long as the label is only
    # USED in later transactions (the seed parser flip runs after this commit).
    # The label is inlined, never bound: the DO body carries format()'s %I / %L,
    # and psycopg2 only starts interpreting '%' once a statement has parameters.
    for label in _PARSER_TYPE_LABELS:
        if not _ENUM_LABEL_RE.match(label):  # our own constants — belt and braces
            raise ValueError(f"invalid parser_type enum label: {label!r}")
        db.execute(
            text(
                f"""
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
                            v_schema, '{label}'
                        );
                    END IF;
                END $$;
                """
            )
        )
    db.commit()

    # 5. Table evolution create_all cannot do — runs BEFORE the column loop so a
    # table about to be rebuilt is not ALTERed for nothing.
    _rebuild_legacy_entry_payment_status(db)

    # 6. Column evolution on pre-existing tables (see _ADDED_COLUMNS).
    for table, column, sql_type in _ADDED_COLUMNS:
        db.execute(
            text(f'ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS "{column}" {sql_type}')
        )
    for table, column in _DROPPED_COLUMNS:
        db.execute(
            text(f'ALTER TABLE IF EXISTS {table} DROP COLUMN IF EXISTS "{column}"')
        )
    db.commit()

    # 7. Data fixups the new columns require, then the indexes over them.
    _backfill_legacy_lot_buckets(db)
    db.commit()
    for name, ddl in _ADDED_INDEXES:
        try:
            db.execute(text(ddl))
            db.commit()
        except Exception as exc:  # noqa: BLE001
            # A duplicate bucket identity in legacy data must not stop the boot:
            # the app reads fine without the constraint, and the ingest DAG is
            # what would surface the real problem.
            db.rollback()
            logger.warning("[init_reco] could not create index %s: %s", name, exc)
