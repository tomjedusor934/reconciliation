import api from '@/api/axios';
import type { ForceMatchRequest } from '@/types';

const resource = '/match-groups';

export default {
  list(params: { flow_id?: number; mode?: string; skip?: number; limit?: number } = {}) {
    return api.get(`${resource}/`, { params });
  },
  get(id: number) {
    return api.get(`${resource}/${id}`);
  },
  forceMatch(data: ForceMatchRequest) {
    return api.post(`${resource}/force`, data);
  },
};
