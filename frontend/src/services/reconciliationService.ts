import api from '@/api/axios';
import type { ExclusionCreate, UnexcludeRequest, ReconciliationEntry } from '@/types';

const resource = '/reconciliation-entries';

export interface EntryFilters {
  flow_id?: number;
  status?: string;
  reco_id?: string;
  amount_min?: number | string;
  amount_max?: number | string;
  payment_statuses?: string[];
  date_from?: string;
  date_to?: string;
  skip?: number;
  limit?: number;
}

export interface PaginatedEntriesResponse {
  items: ReconciliationEntry[];
  total_count: number;
}

export default {
  list(params: EntryFilters = {}) {
    // indexes:null → payment_statuses=A&payment_statuses=B (FastAPI List[str]),
    // not the bracketed form axios emits by default.
    return api.get<PaginatedEntriesResponse>(`${resource}/`, {
      params,
      paramsSerializer: { indexes: null },
    });
  },
  paymentStatusOptions() {
    return api.get<string[]>(`${resource}/payment-status-options`);
  },
  getExportUrl(filters: EntryFilters = {}) {
    const baseUrl = api.defaults.baseURL || '';
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value === '' || value === undefined || value === null) return;
      if (Array.isArray(value)) {
        value.forEach((v) => params.append(key, String(v)));
      } else {
        params.append(key, String(value));
      }
    });
    const qs = params.toString();
    return `${baseUrl}${resource}/export${qs ? `?${qs}` : ''}`;
  },
  exclude(data: ExclusionCreate) {
    return api.post(`${resource}/exclude`, data);
  },
  unexclude(data: UnexcludeRequest) {
    return api.post(`${resource}/unexclude`, data);
  },
  transversal(reco_id: string, currency?: string) {
    return api.get(`/dashboards/transversal`, { params: { reco_id, currency } });
  },
};
