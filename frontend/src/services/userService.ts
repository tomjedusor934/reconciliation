import api from '@/api/axios';

const resource = '/users';

export default {
  getAll() {
    return api.get(`${resource}/`);
  },
  get(id: number) {
    return api.get(`${resource}/${id}`);
  },
  create(data: any) {
    return api.post(`${resource}/`, data);
  },
  update(id: number, data: any) {
    return api.put(`${resource}/${id}`, data);
  },
  updateMe(data: any) {
    return api.put(`${resource}/me`, data);
  },
  delete(id: number) {
    return api.delete(`${resource}/${id}`);
  },
  unblock(id: number) {
    return api.post(`${resource}/${id}/unblock`);
  }
};
