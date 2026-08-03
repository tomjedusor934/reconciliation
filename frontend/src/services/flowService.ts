import api from '@/api/axios';
import type { FlowCreate, FlowUpdate } from '@/types';

const resource = '/flows';

export default {
  getAll() {
    return api.get(`${resource}/`);
  },
  get(id: number) {
    return api.get(`${resource}/${id}`);
  },
  create(data: FlowCreate) {
    return api.post(`${resource}/`, data);
  },
  update(id: number, data: FlowUpdate) {
    return api.put(`${resource}/${id}`, data);
  },
  delete(id: number) {
    return api.delete(`${resource}/${id}`);
  },
  toggleSource(flowId: number, sourceId: number, isActive: boolean) {
    return api.patch(`${resource}/${flowId}/sources/${sourceId}/toggle`, { is_active: isActive });
  },
};
