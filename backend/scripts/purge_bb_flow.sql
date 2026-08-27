-- FULL purge of a batch-booking flow before the claim-group re-ingest
-- (2026-08 — ghosts renamed from per-parent to per-claim-group identities).
--
-- Why a full purge and not an incremental migration: ghost identities changed
-- (external_ref is now a pure function of the claim key and the bucket, hashed
-- on the group's canonical parent), so every existing ghost — INCLUDING the
-- émargé ones, which are immutable by design — would double count against the
-- re-emitted ones. The datamart remains the source of truth: a re-ingest with
-- a backfill rebuilds everything, and the lot uuids are deterministic (uuid5)
-- so the lots come back identical.
--
-- What this deletes, scoped to ONE flow/source:
--   * every reconciliation_entry of the flow (live AND émargement — real
--     movements and ghosts alike; the émargé history of this flow is rebuilt
--     by the re-reconcile, this is the assumed cost of the re-keying);
--   * the exclusions pointing at those entries;
--   * every match_group of the flow;
--   * every movement_split of the source (re-registered by the re-ingest);
--   * every movement_lot of the source (members and keys cascade);
--   * the orphaned entry_payment_status rows (pure sync cache);
--   * the source's ingestion watermark (last_success_at), so the next run
--     re-streams the whole window. Set backfill_since on the source FIRST if
--     the default lookback does not cover your history.
--
-- Runbook (prod):
--   1. Pause the ingest/orchestrate DAGs.
--   2. Check/set reco.flow_source.backfill_since for the source.
--   3. psql "$DATABASE_URL" -v flow_code=float_account_outward \
--                           -v source_code=finacle_db \
--                           -f backend/scripts/purge_bb_flow.sql
--   4. Deploy the new backend + DAG together (wire format changed).
--   5. Trigger ingest_finacle_bb, then POST /tasks/reconcile (or the
--      orchestrate DAG), then sync_payment_status with PS_FULL_SYNC=1.
--   6. Controls: no lot with a ±0,01 net; the LUXEMBOURG/ESCH label lots
--      balanced and tagged parent_mismatch; salary/ADEM groups untagged;
--      ghost volume sharply down (one ghost set per claim group).

\set ON_ERROR_STOP on

BEGIN;

-- The flow/source being purged, resolved once.
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

-- 1. Exclusions pointing at the flow's entries (entry_id carries no FK — they
--    would silently dangle otherwise).
DELETE FROM reco.exclusion x
WHERE x.entry_id IN (
    SELECT e.id FROM reco.reconciliation_entry e JOIN _target t ON e.flow_id = t.flow_id
    UNION ALL
    SELECT e.id FROM reco.reconciliation_entry_emargement e JOIN _target t ON e.flow_id = t.flow_id
);

-- 2. Every entry of the flow, live and émargé. Real movements are re-ingested
--    from the datamart; ghosts are re-emitted per claim group.
DELETE FROM reco.reconciliation_entry e
USING _target t WHERE e.flow_id = t.flow_id;

DELETE FROM reco.reconciliation_entry_emargement e
USING _target t WHERE e.flow_id = t.flow_id;

-- 3. The flow's match groups (their entries are gone).
DELETE FROM reco.match_group mg
USING _target t WHERE mg.flow_id = t.flow_id;

-- 4. The split registry (re-registered with claim keys by the re-ingest).
DELETE FROM reco.movement_split s
USING _target t WHERE s.flow_source_id = t.flow_source_id;

-- 5. The lots. movement_lot_member and movement_lot_key cascade. Deterministic
--    uuids mean the re-ingest recreates the same ids.
DELETE FROM reco.movement_lot l
USING _target t WHERE l.flow_source_id = t.flow_source_id;

-- 6. Payment statuses are keyed by reco_id, i.e. by lots/entries just deleted.
--    Pure sync cache; sync_payment_status (PS_FULL_SYNC) repopulates it.
DELETE FROM reco.entry_payment_status ep
WHERE NOT EXISTS (SELECT 1 FROM reco.movement_lot l WHERE l.id = ep.reco_id)
  AND NOT EXISTS (SELECT 1 FROM reco.reconciliation_entry e WHERE e.reco_id = ep.reco_id)
  AND NOT EXISTS (SELECT 1 FROM reco.reconciliation_entry_emargement e WHERE e.reco_id = ep.reco_id);

-- 7. Rewind the ingestion watermark: the next run re-streams from
--    backfill_since (set it beforehand if the default lookback is too short).
UPDATE reco.flow_source s
SET last_success_at = NULL
FROM _target t WHERE s.id = t.flow_source_id;

COMMIT;

-- Sanity check — everything should be 0 for the purged flow.
SELECT
    (SELECT COUNT(*) FROM reco.movement_lot)                             AS lots_left,
    (SELECT COUNT(*) FROM reco.movement_split)                           AS splits_left,
    (SELECT COUNT(*) FROM reco.reconciliation_entry e
      JOIN reco.flow f ON f.id = e.flow_id WHERE f.code = :'flow_code')  AS live_entries_left,
    (SELECT COUNT(*) FROM reco.reconciliation_entry_emargement e
      JOIN reco.flow f ON f.id = e.flow_id WHERE f.code = :'flow_code')  AS emarged_entries_left,
    (SELECT COUNT(*) FROM reco.match_group mg
      JOIN reco.flow f ON f.id = mg.flow_id WHERE f.code = :'flow_code') AS match_groups_left;
