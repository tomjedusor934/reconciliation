import api from '@/api/axios';
import type {
  LotDetail,
  LotGraphData,
  PaginatedLotKeyValuesResponse,
  PaginatedLotMembersResponse,
  PaginatedLotsResponse,
  SplitDetail,
} from '@/types';

const resource = '/lots';

export interface LotFilters {
  flow_id?: number;
  status?: string;
  balanced?: boolean;
  date_from?: string;
  date_to?: string;
  search?: string;
  bucket_kind?: string;
  synthetic_only?: boolean;
  payment_gap?: boolean;
  parent_mismatch?: boolean;
  skip?: number;
  limit?: number;
}

export interface LotMemberFilters {
  movement_type?: string;
  direction?: string;
  entry_status?: string;
  search?: string;
  key_type?: string;
  key_value?: string;
  skip?: number;
  limit?: number;
}

export interface LotGraphParams {
  key_type?: string;
  key_value?: string;
}

export interface LotKeyValueFilters {
  key_type: string;
  search?: string;
  skip?: number;
  limit?: number;
}

export default {
  list(params: LotFilters = {}) {
    return api.get<PaginatedLotsResponse>(`${resource}/`, { params });
  },
  get(lotId: string) {
    return api.get<LotDetail>(`${resource}/${lotId}`);
  },
  getGraph(lotId: string, params: LotGraphParams = {}) {
    return api.get<LotGraphData>(`${resource}/${lotId}/graph`, { params });
  },
  getKeys(lotId: string, params: LotKeyValueFilters) {
    return api.get<PaginatedLotKeyValuesResponse>(`${resource}/${lotId}/keys`, { params });
  },
  getMembers(lotId: string, params: LotMemberFilters = {}) {
    return api.get<PaginatedLotMembersResponse>(`${resource}/${lotId}/members`, { params });
  },
  // The real movement behind a ghost, its sibling ghosts across every bucket,
  // and whether the amounts still add back up to it.
  getSplit(parentSourceHash: string) {
    return api.get<SplitDetail>(`/splits/${parentSourceHash}`);
  },
};
