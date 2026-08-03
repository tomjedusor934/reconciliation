"""Postgres-side initialization for the reconciliation app.

Creates:
- schemas ``reco`` and ``audit``
- generic audit trigger function and per-table triggers for every metier table
- helper function to set the current user id (audit context)

Idempotent: safe to call on every backend start.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

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

    # 4. Enum evolution — parser_type gained FINACLE_BATCH_BOOKING_TRUE.
    # DBs provisioned by create_all carry a native PG enum (labels = Python enum
    # NAMES) that predates the value; add it idempotently, resolving the type's
    # actual schema (SQLAlchemy creates it unqualified → usually public).
    # Alembic-001 DBs store this column as varchar and have no such type → no-op.
    # PG12+ allows ADD VALUE inside a transaction as long as the label is only
    # USED in later transactions (the seed parser flip runs after this commit).
    db.execute(
        text(
            """
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
                        v_schema, 'FINACLE_BATCH_BOOKING_TRUE'
                    );
                END IF;
            END $$;
            """
        )
    )
    db.commit()
