import api from '@/api/axios';

export default {
  overview() {
    return api.get(`/reconciliation-dashboard/`);
  },
  ingestionCalendar(flowId: number, year: number, month: number) {
    return api.get(`/reconciliation-dashboard/ingestion-calendar`, {
      params: { flow_id: flowId, year, month },
    });
  },
  transversal(reco_id: string, currency?: string) {
    return api.get(`/reconciliation-dashboard/transversal`, { params: { reco_id, currency } });
  },
};
