import api from '@/api/axios';

const resource = '/rcp-reattribution';

/** What one uploaded file actually held — a dump whose msgid column is empty
 *  (the 2026-08-12 reject extract) is unusable and must say so. */
export interface RcpFileReport {
  file_name: string;
  rows: number;
  rows_with_msgid: number;
  distinct_msgids: number;
  has_msgid_column: boolean;
  amount_column: string;
  delimiter: string;
  error: string;
  /** {serviceid: rows} over the whole workbook — kept and skipped types alike. */
  services: Record<string, number>;
}

/** Part 1: the movement's dump rows vs what it booked. */
export interface RcpControl {
  msgid: string;
  status: 'OK' | 'NOT_FOUND' | 'COUNT_MISMATCH' | 'AMOUNT_MISMATCH' | 'DUPLICATE_MOVEMENT';
  expected_count: number;
  found_count: number;
  expected_amount: string;
  found_amount: string;
  delta_count: number;
  delta_amount: string;
  files: string[];
  service_id: string;
  direction: string;
  tran_id: string;
  sp_date: string;
}

export interface RcpEntry {
  /** null when the movement has no live row left: it was withdrawn in favour of
   *  its ghosts and comes back described by its movement_split parent. */
  id: number | null;
  flow_id: number;
  flow_source_id: number | null;
  source_hash: string;
  reco_id: string | null;
  account: string | null;
  currency: string;
  amount: string;
  direction: string | null;
  value_date: string;
  operation_date: string | null;
  external_ref: string | null;
  transaction_particulars: string | null;
  ref_no: string | null;
  remarks_1: string | null;
  status: string | null;
}

/** LOT = a movement_lot uuid (batch-booking flow); RECO = the flow's own
 *  reconciliation key (classic bulk flow, which has no lots). */
export type RcpTargetKind = 'LOT' | 'RECO';

/** One destination the movement will be split into, worth its payments' exact sum. */
export interface RcpTarget {
  target_id: string;
  target_kind: RcpTargetKind;
  bucket_kind: string;
  bucket_pacs008: string;
  bucket_msgid: string;
  bucket_po: string;
  bucket_ref: string;
  label: string;
  resolved_via: string;
  amount: string;
  payment_count: number;
  pos: string[];
}

export interface RcpUnresolved {
  po: string;
  amount: string;
  reason: string;
}

export type RcpProposalStatus =
  | 'PROPOSED'
  | 'TO_RECOMMIT'
  | 'TO_REPLAY'
  | 'ALREADY_COMMITTED'
  | 'EMARGED'
  | 'ENTRY_NOT_FOUND'
  | 'ENTRY_AMBIGUOUS'
  | 'ENTRY_NOT_PENDING'
  | 'NO_DUMP_ROWS'
  | 'NO_TARGET'
  | 'FLOW_UNSUPPORTED';

export interface RcpProposal {
  msgid: string;
  status: RcpProposalStatus;
  message: string;
  service_id: string;
  /** Where the movement was found — and therefore what it is split onto. */
  flow_id: number | null;
  flow_code: string;
  source_code: string;
  target_kind: RcpTargetKind | '';
  direction: string;
  tran_id: string;
  sp_date: string;
  num_records: number;
  settlement_amount: string;
  control_status: string;
  control_delta_count: number;
  control_delta_amount: string;
  entry: RcpEntry | null;
  candidates: RcpEntry[];
  targets: RcpTarget[];
  unresolved_payments: RcpUnresolved[];
  resolved_amount: string;
}

export interface RcpAnalyzeResponse {
  link_file: RcpFileReport;
  dump_files: RcpFileReport[];
  duplicate_msgids: string[];
  controls: RcpControl[];
  control_summary: Record<string, number>;
  proposals: RcpProposal[];
  summary: Record<string, number>;
  datamart_error: string;
}

export interface RcpCommitItem {
  msgid: string;
  entry_source_hash: string;
  targets: { target_id: string; amount: string; payment_count: number; pos: string[] }[];
}

export interface RcpCommitResult {
  msgid: string;
  applied: boolean;
  error: string;
  ghost_total: string | null;
  booked_amount: string | null;
  parents_emarged: number;
  targets: { target_id: string; target_kind: RcpTargetKind; amount: string; payment_count: number }[];
}

export interface RcpCommitResponse {
  applied: number;
  failed: number;
  results: RcpCommitResult[];
}

/** Statuses that can actually be committed. TO_REPLAY is a movement already
 *  reattributed once: it no longer has a live row (the ingestion guard keeps it
 *  withdrawn) and is replayed from its movement_split parent — the ghosts keep
 *  their identities, so a replay updates them instead of duplicating them. */
export const RCP_ACTIONABLE: RcpProposalStatus[] = ['PROPOSED', 'TO_RECOMMIT', 'TO_REPLAY'];

/** The workbook serviceids the backend treats (mirrors REATTRIBUTABLE_SERVICES).
 *  Anything else in the file is counted and skipped — NCP being the bulk of it. */
export const RCP_SERVICES = ['RCP', 'RCC', 'RRS', 'WCC'];

/** ── Non rattachés ───────────────────────────────────────────────────
 *  The other half of the tool: movements the ingestion could not key at all
 *  ('Not Supported' / no reco_id). No upload — the movement's own ref_no and
 *  particulars are the input (see backend app/services/rcp_orphan_service.py).
 */

/** Which rule found the key — shown as the EVIDENCE behind a proposal, in
 *  decreasing order of trust. REF_NO is a field Finacle filled deliberately;
 *  TP_DIGITS is a digit run scraped out of a free-text label. */
export type RcpOrphanRule = 'REF_NO' | 'TP_RETURN_SHAPE' | 'TP_NAMES_MOVEMENT' | 'TP_DIGITS' | '';

export type RcpOrphanStatus = 'PROPOSED' | 'NO_TARGET' | 'NO_KEY' | 'KEY_AMBIGUOUS';

export interface RcpOrphan {
  source_hash: string;
  entry_id: number | null;
  flow_id: number;
  source_code: string;
  reco_id: string | null;
  amount: string;
  currency: string;
  direction: string | null;
  value_date: string;
  external_ref: string | null;
  transaction_particulars: string | null;
  ref_no: string | null;
  remarks_1: string | null;
  rule: RcpOrphanRule;
  keys: string[];
  target_kind: RcpTargetKind | '';
  target_id: string;
  target_label: string;
  candidates: string[];
  status: RcpOrphanStatus;
  message: string;
}

export interface RcpOrphanAnalyzeResponse {
  flow_id: number;
  proposals: RcpOrphan[];
  summary: Record<string, number>;
  datamart_error: string;
}

export interface RcpOrphanCommitItem {
  source_hash: string;
  target_id: string;
  rule?: string;
  key?: string;
}

export interface RcpOrphanCommitResult {
  source_hash: string;
  applied: boolean;
  error: string;
  target_id: string;
  target_kind: RcpTargetKind | '';
}

export interface RcpOrphanCommitResponse {
  applied: number;
  failed: number;
  results: RcpOrphanCommitResult[];
}

/** The only orphan status that can be committed. Unlike the link half there is
 *  no replay: an orphan is RETARGETED, never split, so committing it twice is
 *  refused by the backend (its reco_id is no longer the sentinel). */
export const RCP_ORPHAN_ACTIONABLE: RcpOrphanStatus[] = ['PROPOSED'];

/** A background run. Both /analyze and /commit answer with one of these
 *  straight away; the batch itself is polled through /jobs/{id}. That is what
 *  keeps every request short enough to never meet a proxy timeout. */
export interface RcpJob<T> {
  job_id: string;
  kind: 'analyze' | 'commit';
  status: 'running' | 'done' | 'error';
  phase: string;
  done: number;
  total: number;
  result: T | null;
  error: string;
  started_at: string | null;
  finished_at: string | null;
  /** Running commentary of the batch — what it read, found, and failed on. */
  logs: string[];
}

/** One past run in the history — counters only, the report stays server-side
 *  until it is explicitly re-opened (a report is ~2.6 MB of JSON). */
export interface RcpRun {
  job_id: string;
  kind: 'analyze' | 'commit';
  status: 'running' | 'done' | 'error';
  phase: string;
  error: string;
  label: string;
  movements: number;
  actionable: number;
  applied: number;
  failed: number;
  started_at: string | null;
  finished_at: string | null;
}

export default {
  /** Starts the analysis (read-only). Returns a job to poll. */
  startAnalyze(params: {
    linkFile: File;
    dumpFiles: File[];
    connectionId?: number | null;
    flowId?: number | null;
  }) {
    const form = new FormData();
    form.append('link_file', params.linkFile);
    params.dumpFiles.forEach((file) => form.append('dump_files', file));
    if (params.connectionId != null) form.append('connection_id', String(params.connectionId));
    if (params.flowId != null) form.append('flow_id', String(params.flowId));
    // Only the upload happens in this request; the batch runs server-side.
    return api.post<RcpJob<RcpAnalyzeResponse>>(`${resource}/analyze`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 10 * 60 * 1000, // covers a slow upload, not the analysis
    });
  },
  /** Starts the commit: applies only the ticked movements, re-validated server-side. */
  startCommit(items: RcpCommitItem[]) {
    return api.post<RcpJob<RcpCommitResponse>>(`${resource}/commit`, { items });
  },
  /** Starts the analysis of a flow's unattached movements (read-only). */
  startOrphanAnalyze(params: { flowId: number; connectionId?: number | null; limit?: number }) {
    return api.post<RcpJob<RcpOrphanAnalyzeResponse>>(`${resource}/orphans/analyze`, {
      flow_id: params.flowId,
      connection_id: params.connectionId ?? null,
      limit: params.limit ?? 5000,
    });
  },
  /** Retargets the ticked movements — refused server-side for anything that is
   *  no longer stranded. */
  startOrphanCommit(items: RcpOrphanCommitItem[]) {
    return api.post<RcpJob<RcpOrphanCommitResponse>>(`${resource}/orphans/commit`, { items });
  },
  /** Cheap by construction — safe to poll every couple of seconds. Falls back
   *  to the persisted run once the in-memory registry has forgotten. */
  getJob<T>(jobId: string) {
    return api.get<RcpJob<T>>(`${resource}/jobs/${jobId}`);
  },
  /** Recent runs, without their payload. */
  listRuns(limit = 20) {
    return api.get<RcpRun[]>(`${resource}/runs`, { params: { limit } });
  },
  /** Re-opens a past run, report included. */
  getRun<T>(runId: string) {
    return api.get<RcpJob<T>>(`${resource}/runs/${runId}`);
  },
};
