-- Switchover to (PACS008 × MSGID) buckets — run ONCE per environment, then
-- re-run the ingest_finacle_bb DAG with a backfill.
--
-- Lots built by the retired union-find clustering have no translation into
-- buckets: they are connected components, not pairs, and their uuids are random
-- rather than derived from an identity. So the lot tables are emptied and the
-- pending Finacle entries are unkeyed; the next DAG run rebuilds everything
-- deterministically.
--
-- What is deliberately NOT touched:
--   * reconciliation_entry_emargement — reconciled history keeps the reco_id it
--     was matched under. Rewriting it would falsify what an operator validated.
--   * entries of any other flow (MT940, MOSEL…) — this only unkeys the
--     batch-booking source.
--
-- Usage (adjust the flow/source codes if yours differ):
--   psql "$DATABASE_URL" -v flow_code=float_account_outward \
--                        -v source_code=finacle_db \
--                        -f backend/scripts/reset_bb_buckets.sql

\set ON_ERROR_STOP on

BEGIN;

-- The source being reset, resolved once.
CREATE TEMP TABLE _target ON COMMIT DROP AS
SELECT f.id AS flow_id, s.id AS flow_source_id
FROM reco.flow f
JOIN reco.flow_source s ON s.flow_id = f.id
WHERE f.code = :'flow_code' AND s.code = :'source_code';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM _target) THEN
        RAISE EXCEPTION 'flow/source not found — check the codes passed with -v';
    END IF;
END $$;

-- 1. Ghost entries: app-side constructs with no datamart row behind them, so
--    they are dropped outright rather than unkeyed. The next run recreates the
--    ones that still make sense.
DELETE FROM reco.reconciliation_entry e
USING _target t
WHERE e.flow_id = t.flow_id
  AND e.split_parent_hash IS NOT NULL;

-- 2. Real movements that had been withdrawn in favour of ghosts are re-ingested
--    by the next run (the DAG re-streams std.Movement), so the parent registry
--    can go.
DELETE FROM reco.movement_split s
USING _target t
WHERE s.flow_source_id = t.flow_source_id;

-- 3. The lots themselves. movement_lot_member and movement_lot_key cascade.
DELETE FROM reco.movement_lot l
USING _target t
WHERE l.flow_source_id = t.flow_source_id;

-- 4. Unkey the surviving PENDING entries so the next run assigns bucket uuids.
--    'Not Supported' is preserved: it means "shape known, not handled", which
--    this change does not affect, and the DAG retries it anyway.
UPDATE reco.reconciliation_entry e
SET reco_id = NULL
FROM _target t
WHERE e.flow_id = t.flow_id
  AND e.status = 'PENDING'
  AND e.reco_id IS DISTINCT FROM 'Not Supported';

-- 5. Payment statuses are keyed by reco_id, i.e. by the lots just deleted.
--    They are a pure sync cache; sync_payment_status repopulates them.
DELETE FROM reco.entry_payment_status ep
WHERE NOT EXISTS (
    SELECT 1 FROM reco.movement_lot l WHERE l.id = ep.reco_id
)
AND NOT EXISTS (
    SELECT 1 FROM reco.reconciliation_entry e WHERE e.reco_id = ep.reco_id
)
AND NOT EXISTS (
    SELECT 1 FROM reco.reconciliation_entry_emargement e WHERE e.reco_id = ep.reco_id
);

COMMIT;

-- Sanity check — every count should be 0 except the unkeyed entries.
SELECT
    (SELECT COUNT(*) FROM reco.movement_lot)                                 AS lots_left,
    (SELECT COUNT(*) FROM reco.movement_split)                               AS splits_left,
    (SELECT COUNT(*) FROM reco.reconciliation_entry
      WHERE split_parent_hash IS NOT NULL)                                   AS ghosts_left,
    (SELECT COUNT(*) FROM reco.reconciliation_entry
      WHERE status = 'PENDING' AND reco_id IS NULL)                          AS entries_to_rebucket;
