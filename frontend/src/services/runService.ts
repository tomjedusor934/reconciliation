import api from '@/api/axios';

export default {
  ingestionRuns(params: { flow_id?: number; status?: string; skip?: number; limit?: number } = {}) {
    return api.get(`/ingestion-runs/`, { params });
  },
  reconciliationRuns(params: { skip?: number; limit?: number } = {}) {
    return api.get(`/reconciliation-runs/`, { params });
  },
};
