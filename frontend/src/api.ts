// VITE_API_BASE may be a full URL (local dev) or a bare hostname (Render's
// fromService injects the API service host with no scheme) — normalize to a
// full https:// origin in the latter case.
const RAW_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';
const BASE = /^https?:\/\//.test(RAW_BASE) ? RAW_BASE : `https://${RAW_BASE}`;

export type TierHit = 'L1_MEMORY' | 'L2_REDIS' | 'MISS';

export interface QueryResponse {
  node_id: string;
  tier_hit: TierHit;
  latency_ms: number;
  wall_ms: number;
  rewrites_applied: string[];
  routing_reason: string;
  template_id: string;
  columns: string[];
  rows: (string | number | null)[][];
  row_count: number;
  truncated: boolean;
}

export interface SampleQuery {
  name: string;
  sql: string;
}

export interface NodeStats {
  node_id: string;
  cache: {
    l1: { hits: number; misses: number; hit_rate: number };
    l2: { hits: number; misses: number; hit_rate: number };
  };
  templates_tracked: number;
}

export interface ApiError {
  detail: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({ detail: res.statusText }))) as ApiError;
    throw new Error(body.detail || `request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>('/api/health'),
  samples: () => request<SampleQuery[]>('/api/samples'),
  query: (sql: string) =>
    request<QueryResponse>('/api/query', { method: 'POST', body: JSON.stringify({ sql }) }),
  stats: () => request<{ nodes: NodeStats[] }>('/api/stats'),
};
