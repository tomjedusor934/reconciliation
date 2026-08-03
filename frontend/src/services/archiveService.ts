import api from '@/api/axios';
import type { SnapshotResponse, DailyActivityResponse } from '@/types';

const resource = '/archive';

export default {
  getSnapshot(params: { date: string; flow_id?: number; status?: string; skip?: number; limit?: number }) {
    return api.get<SnapshotResponse>(`${resource}/snapshot`, { params });
  },
  getActivity(params: { date: string; flow_id?: number; user_id?: number; skip?: number; limit?: number }) {
    return api.get<DailyActivityResponse>(`${resource}/activity`, { params });
  },
  getExportUrl(date: string, flowId?: number) {
    const baseUrl = api.defaults.baseURL || '';
    let url = `${baseUrl}${resource}/export?date=${date}`;
    if (flowId) url += `&flow_id=${flowId}`;
    return url;
  },
};
