import api from '@/api/axios';

const resource = '/source-connections';

export interface SourceConnection {
  id: number;
  code: string;
  name: string;
  type: string;
  dsn?: string | null;
  // sanitized (never contains the secret); has_password says whether one is stored
  extra?: Record<string, any> | null;
  has_password?: boolean;
}

// Create/update payload: structured MSSQL fields (+ write-only password) or a
// raw dsn for other connection types. Omit password on update to keep it.
export interface SourceConnectionInput {
  code?: string;
  name?: string;
  type?: string;
  host?: string | null;
  port?: number | null;
  database?: string | null;
  username?: string | null;
  password?: string | null;
  odbc_driver?: string | null;
  encrypt?: boolean | null;
  trust_server_certificate?: boolean | null;
  dsn?: string | null;
}

export interface TestQueryResult {
  columns: string[];
  rows: any[][];
  row_count: number;
  truncated: boolean;
}

export default {
  getAll() {
    return api.get<SourceConnection[]>(`${resource}/`);
  },
  create(payload: SourceConnectionInput) {
    return api.post<SourceConnection>(`${resource}/`, payload);
  },
  update(connectionId: number, payload: SourceConnectionInput) {
    return api.put<SourceConnection>(`${resource}/${connectionId}`, payload);
  },
  testQuery(connectionId: number, query: string) {
    return api.post<TestQueryResult>(`${resource}/${connectionId}/test-query`, { query });
  },
};
