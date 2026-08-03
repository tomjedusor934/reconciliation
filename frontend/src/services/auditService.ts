import api from '@/api/axios';

export default {
  data(params: { table_name?: string; user_id?: number; skip?: number; limit?: number } = {}) {
    return api.get(`/audit/data`, { params });
  },
  uiActions(params: { user_id?: number; action?: string; skip?: number; limit?: number } = {}) {
    return api.get(`/audit/ui-actions`, { params });
  },
};
