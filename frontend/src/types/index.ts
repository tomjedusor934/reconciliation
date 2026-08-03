export interface AccessiblePage {
  path: string;
  access_level: 'ALL' | 'EDIT' | 'NONE';
}

export interface Role {
  id: number;
  name: string;
  description?: string;
  level: string;
  accessible_pages: AccessiblePage[];
}

export interface User {
  id: number;
  email: string;
  full_name?: string;
  is_active: boolean;
  is_superuser: boolean;
  roles: Role[];
  blocked: boolean;
  count_tentative: number;
}

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface LoginResponse {
  message: string;
  user: { email: string; id: number } | null;
  password_days_remaining: number | null;
  must_change_password: boolean;
  is_blocked: boolean;
  remaining_attempts: number | null;
}

export interface RegisterData {
  email: string;
  password: string;
  full_name?: string;
}

export type ToastType = 'success' | 'error' | 'info' | 'warning';
export interface ToastMessage {
  id: number;
  message: string;
  type: ToastType;
  duration?: number;
}

// SSO -----------------------------------------------------------------------
export type SSOProviderType = 'google' | 'azure' | 'github' | 'okta' | 'generic_oidc';

export interface SSOPublicProvider {
  name: string;
  display_name: string;
  provider_type: SSOProviderType;
  icon?: string | null;
  button_color?: string | null;
  order: number;
}

export interface SSOPublicConfig {
  sso_enabled: boolean;
  sso_force: boolean;
  password_login_enabled: boolean;
  providers: SSOPublicProvider[];
}

export interface SSOSettings {
  sso_enabled: boolean;
  sso_force: boolean;
  sso_create_account_on_login: boolean;
  sso_default_role_id: number | null;
}

export interface SSOProvider {
  id: number;
  name: string;
  display_name: string;
  provider_type: SSOProviderType;
  client_id: string;
  authorization_url?: string | null;
  token_url?: string | null;
  userinfo_url?: string | null;
  jwks_url?: string | null;
  issuer?: string | null;
  scopes: string;
  tenant_id?: string | null;
  icon?: string | null;
  button_color?: string | null;
  enabled: boolean;
  order: number;
  has_secret: boolean;
}

export interface SSOProviderInput {
  name: string;
  display_name: string;
  provider_type: SSOProviderType;
  client_id: string;
  client_secret?: string;
  authorization_url?: string | null;
  token_url?: string | null;
  userinfo_url?: string | null;
  jwks_url?: string | null;
  issuer?: string | null;
  scopes?: string;
  tenant_id?: string | null;
  icon?: string | null;
  button_color?: string | null;
  enabled?: boolean;
  order?: number;
}

export interface SSOPresetField {
  name: string;
  label: string;
  required: boolean;
  placeholder?: string | null;
}

export interface SSOPreset {
  type: SSOProviderType;
  label: string;
  default_scopes: string;
  fields: SSOPresetField[];
}

// ============================================================================
// Reconciliation app
// ============================================================================

export type FlowSourceType = 'file' | 'finacle_db';
export type ParserType =
  | 'cobol_mosel'
  | 'csv'
  | 'xml'
  | 'mt940'
  | 'finacle_db'
  | 'finacle_batch_booking_true'
  | 'excel';
export type MatchKeyStrategy = 'reco_id_amount' | 'file_ref_amount' | 'ref_amount';

export interface FlowSourceAccount {
  id?: number;
  flow_source_id?: number;
  account_number: string;
  label?: string | null;
}

export interface FlowSource {
  id?: number;
  flow_id?: number;
  code: string;
  name: string;
  description?: string | null;
  is_active: boolean;
  source_type: FlowSourceType;
  parser_type: ParserType;
  match_key_strategy: MatchKeyStrategy;
  inbox_subfolder?: string | null;
  file_pattern?: string | null;
  parser_config?: Record<string, any> | null;
  accounts: FlowSourceAccount[];
  created_at?: string;
  updated_at?: string;
}

export type FlowSourceCreate = Omit<FlowSource, 'id' | 'flow_id' | 'created_at' | 'updated_at'>;
export type FlowSourceUpdate = Partial<FlowSourceCreate>;

export interface Flow {
  id: number;
  code: string;
  name: string;
  description?: string | null;
  is_active: boolean;
  default_currency?: string | null;
  sources: FlowSource[];
}

export type FlowCreate = Omit<Flow, 'id' | 'sources'> & {
  sources: FlowSourceCreate[];
};
export type FlowUpdate = Partial<Omit<FlowCreate, 'code'>>;

export type EntryStatus = 'pending' | 'matched' | 'forced' | 'excluded';
export type EntryDirection = 'debit' | 'credit';

export interface ReconciliationEntry {
  id: number;
  flow_id: number;
  ingestion_run_id?: number | null;
  reco_id?: string | null;
  account?: string | null;
  currency: string;
  amount: string;
  direction?: EntryDirection | null;
  value_date: string;
  operation_date?: string | null;
  event_type?: string | null;
  external_ref?: string | null;
  file_name?: string | null;
  transaction_particulars?: string | null;
  ref_no?: string | null;
  remarks_1?: string | null;
  payload_raw?: Record<string, any> | null;
  status: EntryStatus;
  match_group_id?: number | null;
  matched_at?: string | null;
  // {payment status: count} for the movement's payments (std.Payment sync);
  // null/absent for flows without payment tracking
  payment_statuses?: Record<string, number> | null;
}

export interface MatchGroup {
  id: number;
  flow_id: number;
  reco_id?: string | null;
  currency: string;
  total: string;
  mode: 'auto' | 'forced';
  created_by_user_id?: number | null;
  created_at: string;
  comment?: string | null;
  reconciliation_run_id?: number | null;
}

// ── Movement lots (Finacle Batch Booking True) ──────────────────────

export type LotStatus = 'pending' | 'matched' | 'merged';
export type LotKeyType = 'PACS008' | 'MSGID' | 'PO';

export interface LotSummary {
  lot_id: string;
  flow_id: number;
  flow_source_id: number;
  currency: string;
  currencies: string[];
  status: LotStatus;
  is_balanced: boolean;
  member_count: number;
  pending_count: number;
  matched_count: number;
  excluded_count: number;
  total_debit: string;
  total_credit: string;
  net_amount: string;
  pending_payment_amount: string; // Σ amount of members with a PDNG payment
  pending_payment_count: number;
  first_value_date?: string | null;
  last_value_date?: string | null;
  merge_conflict: boolean;
  merged_into_lot_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface LotMember {
  id: number;
  source_hash: string;
  movement_type: string; // SCTXB | SDDXB | SDXBB | NDGB | NDRJ | SWIFT | BKRTP
  external_ref?: string | null;
  account?: string | null;
  currency: string;
  amount: string;
  direction?: EntryDirection | null;
  value_date: string;
  operation_date?: string | null;
  transaction_particulars?: string | null;
  ref_no?: string | null;
  remarks_1?: string | null;
  entry_status?: EntryStatus | null;
  entry_id?: number | null;
  match_group_id?: number | null;
}

export interface LotKey {
  id: number;
  member_id: number;
  key_type: LotKeyType;
  key_value: string;
}

export interface LotDetail {
  lot: LotSummary;
  members: LotMember[];
  keys: LotKey[];
  // false = giant lot, members/keys not inlined: use /graph + /members instead
  members_included?: boolean;
}

export interface PaginatedLotsResponse {
  items: LotSummary[];
  total_count: number;
}

export interface PaginatedLotMembersResponse {
  items: LotMember[];
  total_count: number;
}

// Mega-aggregate graph for lots too big to render one node per movement:
// exactly 3 link nodes (PACS008/MSGID/PO) + one movement node per
// (movement_type, direction).
export interface LotKeyTypeSummary {
  key_type: LotKeyType;
  distinct_count: number; // nb of distinct values — the "number of links" shown
  member_link_count: number; // total member↔key links of this type
}

export interface LotTypeDirectionGroup {
  movement_type: string;
  direction: string; // debit | credit
  member_count: number;
  total_amount: string;
  pending_count: number;
  matched_count: number;
  excluded_count: number;
  pending_payment_amount: string; // Σ amount of this group's PDNG-payment members
}

export interface LotGraphEdge {
  movement_type: string;
  direction: string;
  key_type: LotKeyType;
}

export interface LotGraphMeta {
  member_count: number;
  type_counts: Record<string, number>;
  pending_payment_amount: string; // lot-wide Σ (aggregate pending node)
  pending_payment_count: number;
}

export interface LotGraphData {
  lot_id: string;
  key_types: LotKeyTypeSummary[];
  groups: LotTypeDirectionGroup[];
  edges: LotGraphEdge[];
  meta: LotGraphMeta;
}

export interface LotKeyValue {
  key_value: string;
  member_count: number; // members of the lot carrying this exact value
}

export interface PaginatedLotKeyValuesResponse {
  items: LotKeyValue[];
  total_count: number;
}

export interface ForceMatchRequest {
  entry_ids: number[];
  comment?: string;
}

export interface ExclusionCreate {
  entry_id: number;
  reason: string;
}

export interface UnexcludeRequest {
  entry_id: number;
  reason: string;
}

export interface IngestionRun {
  id: number;
  flow_id: number;
  flow_source_id?: number | null;
  source_file?: string | null;
  started_at: string;
  finished_at?: string | null;
  status: 'pending' | 'running' | 'success' | 'partial' | 'failed';
  rows_in: number;
  rows_ok: number;
  rows_ko: number;
  rows_duplicate: number;
  error?: string | null;
}

export interface ReconciliationRun {
  id: number;
  started_at: string;
  finished_at?: string | null;
  entries_scanned: number;
  groups_created: number;
  entries_matched: number;
  duration_ms?: number | null;
  triggered_by?: string | null;
}

export interface IngestionRunSummary {
  run_id: number;
  row_count: number;
  run_date: string;
  source_code?: string | null;
}

export interface FlowKPI {
  flow_id: number;
  flow_code: string;
  flow_name: string;
  pending_count: number;
  matched_count: number;
  excluded_count: number;
  pending_amount_total: string;
  pending_amount_credit: string;
  pending_amount_debit: string;
  last_ingestion_at?: string | null;
  new_entries_delta: number;
  total_entries_current: number;
  oldest_unmatched_date?: string | null;
  ingestion_runs: IngestionRunSummary[];
  is_active: boolean;
}

export interface DashboardData {
  flows: FlowKPI[];
  total_matched_count: number;
  last_reconciliation_run?: ReconciliationRun | null;
}

export interface IngestionCalendarDay {
  day: number;
  count: number;
}

export interface IngestionCalendarResponse {
  flow_id: number;
  year: number;
  month: number;
  days: IngestionCalendarDay[];
}

export interface TransversalGroup {
  reco_id: string;
  currency: string;
  entries: ReconciliationEntry[];
  sum: string;
  is_balanced: boolean;
}

export interface AuditLog {
  id: number;
  table_name: string;
  row_pk?: string | null;
  op: string;
  old_data?: Record<string, any> | null;
  new_data?: Record<string, any> | null;
  user_id?: number | null;
  ts: string;
}

export interface UIActionLog {
  id: number;
  user_id?: number | null;
  action: string;
  target_type?: string | null;
  target_id?: string | null;
  details?: Record<string, any> | null;
  ts: string;
}

// ============================================================================
// Archive (time-machine)
// ============================================================================

export interface SnapshotEntry {
  id: number;
  flow_id: number;
  reco_id?: string | null;
  account?: string | null;
  currency: string;
  amount: string;
  direction?: EntryDirection | null;
  value_date: string;
  operation_date?: string | null;
  event_type?: string | null;
  external_ref?: string | null;
  file_name?: string | null;
  status_at_snapshot: string;
  match_group_id?: number | null;
  matched_at?: string | null;
  exclusion_reason?: string | null;
}

export interface SnapshotSummary {
  snapshot_date: string;
  total_entries: number;
  pending_count: number;
  matched_count: number;
  forced_count: number;
  excluded_count: number;
}

export interface SnapshotResponse {
  summary: SnapshotSummary;
  items: SnapshotEntry[];
  total_count: number;
}

export interface DailyActivityEntry {
  id: number;
  user_id?: number | null;
  user_name?: string | null;
  action: string;
  target_type?: string | null;
  target_id?: string | null;
  details?: Record<string, any> | null;
  ts: string;
}

export interface DailyActivityResponse {
  activity_date: string;
  actions: DailyActivityEntry[];
  total_count: number;
}
