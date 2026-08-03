import apiClient from '@/api/axios';

export interface EnvironmentInfo {
    environment: string;
    reset_ingestion_allowed: boolean;
}

export interface ResetIngestionResult {
    status: string;
    counts: Record<string, number>;
    total_deleted: number;
}

class AdminService {
    async getEnvironment() {
        return apiClient.get<EnvironmentInfo>('/admin/environment');
    }

    async resetIngestion() {
        return apiClient.post<ResetIngestionResult>('/admin/reset-ingestion');
    }

    async resetIngestionFlow(flowId: number) {
        return apiClient.post<ResetIngestionResult>('/admin/reset-ingestion/flow', { flow_id: flowId });
    }
}

export default new AdminService();
