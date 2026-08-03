import apiClient from '@/api/axios';

export interface AccessiblePage {
    path: string;
    access_level: 'ALL' | 'EDIT' | 'NONE';
}

export interface Role {
    id: number;
    name: string;
    description?: string;
    level: 'COMPANY_ACCESS' | 'GROUP_ACCESS';
    accessible_pages: AccessiblePage[];
}

export interface RoleCreate {
    name: string;
    description?: string;
    level?: 'COMPANY_ACCESS' | 'GROUP_ACCESS';
    accessible_pages?: AccessiblePage[];
}

export interface RoleUpdate {
    name?: string;
    description?: string;
    level?: 'COMPANY_ACCESS' | 'GROUP_ACCESS';
    accessible_pages?: AccessiblePage[];
}

class RoleService {
    async getAll() {
        return apiClient.get<Role[]>('/roles/');
    }

    async get(id: number) {
        return apiClient.get<Role>(`/roles/${id}`);
    }

    async create(role: RoleCreate) {
        return apiClient.post<Role>('/roles/', role);
    }

    async update(id: number, role: RoleUpdate) {
        return apiClient.put<Role>(`/roles/${id}`, role);
    }
    
    async delete(id: number) {
        return apiClient.delete(`/roles/${id}`);
    }
}

export default new RoleService();
