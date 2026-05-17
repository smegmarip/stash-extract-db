/**
 * Typed client over the bridge's /api/admin/* endpoints (and the bridge's
 * featurization + match passthroughs). All paths go through the viewer's
 * Express proxy — see viewer/server/routes.ts.
 *
 * Shapes mirror bridge/app/api/admin.py and bridge/app/models.py. Keep them
 * in sync; the bridge OpenAPI is the source of truth.
 */

export interface Pagination {
  page: number;
  limit: number;
  total: number;
  totalPages: number;
}

export interface PaginatedResponse<T> {
  data: T[];
  pagination: Pagination;
}

// --- /api/admin shapes -----------------------------------------------------

export interface JobSummary {
  job_id: string;
  job_name: string;
  completed_at: string;
  record_count: number;
  feature_state: string | null;
  feature_progress: number | null;
  feature_started_at: string | null;
  feature_finished_at: string | null;
  feature_error: string | null;
}

export interface RecordSummary {
  job_id: string;
  job_name: string;
  result_index: number;
  record_id: string | null;
  id: string | null;
  title: string | null;
  details: string | null;
  url: string | null;
  date: string | null;
  cover_image: string | null;
  images: string[];
  performers: string[];
  performer_count: number;
  image_count: number;
  feature_state: string | null;
  feature_progress: number | null;
}

export interface PerformerSummary {
  name_lower: string;
  name_display: string;
  record_count: number;
  job_count: number;
}

export interface PerformerDetail {
  name_lower: string;
  name_display: string;
  record_count: number;
  job_count: number;
  jobs: { job_id: string; job_name: string; records: RecordSummary[] }[];
}

// --- /api/featurization shapes ---------------------------------------------

export interface FleetStatus {
  queued: number;
  in_progress: number;
  ready: number;
  failed: number;
  concurrency_limit: number;
  lifecycle_enabled: boolean;
}

export interface PerJobFeatureStatus {
  state: string;
  progress: number;
  started_at: string;
  finished_at: string | null;
  error: string | null;
}

// --- /match shapes ---------------------------------------------------------

export type MatchMode = "scrape" | "search";
export type ImageMode = "cover" | "sprite" | "both";
export type HashAlgorithm = "phash" | "dhash" | "ahash" | "whash";
export type ImageChannel = "phash" | "color_hist" | "tone" | "embedding";

export interface MatchParams {
  mode: MatchMode;
  image_mode?: ImageMode | null;
  threshold?: number | null;
  limit?: number | null;
  hash_algorithm?: HashAlgorithm | null;
  hash_size?: number | null;
  sprite_sample_size?: number | null;
  image_gamma?: number | null;
  image_count_k?: number | null;
  image_channels?: ImageChannel[] | null;
  image_min_contribution?: number | null;
  image_bonus_per_extra?: number | null;
  image_search_floor?: number | null;
}

export interface FragmentMatchRequest extends MatchParams {
  scene_id: string;
}
export interface NameMatchRequest extends MatchParams {
  name: string;
}

// --- internals -------------------------------------------------------------

function buildQueryString(params: object): string {
  const sp = new URLSearchParams();
  Object.entries(params as Record<string, unknown>).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  });
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

async function get<T>(endpoint: string): Promise<T> {
  const res = await fetch(endpoint);
  if (!res.ok) throw new Error(`API ${res.status} ${res.statusText} (${endpoint})`);
  return res.json();
}

async function postJson<T>(endpoint: string, body: unknown): Promise<{ status: number; data: T; retryAfter: number | null }> {
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const retryAfterHeader = res.headers.get("Retry-After");
  const retryAfter = retryAfterHeader ? parseInt(retryAfterHeader, 10) : null;
  let data: any = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    data = await res.json();
  } else {
    data = await res.text();
  }
  return { status: res.status, data: data as T, retryAfter };
}

// --- public API ------------------------------------------------------------

export interface RecordsListParams {
  job_id?: string;
  q?: string;
  sort?: "id" | "title" | "date" | "performer";
  dir?: "asc" | "desc";
  page?: number;
  limit?: number;
}

export interface PerformersListParams {
  job_id?: string;
  q?: string;
  page?: number;
  limit?: number;
}

export const api = {
  listJobs: () => get<JobSummary[]>("/api/admin/jobs"),

  listRecords: (params: RecordsListParams = {}) =>
    get<PaginatedResponse<RecordSummary>>(`/api/admin/records${buildQueryString(params)}`),

  getRecord: (jobId: string, resultIndex: number) =>
    get<RecordSummary>(`/api/admin/records/${encodeURIComponent(jobId)}/${resultIndex}`),

  getRecordByUuid: (recordId: string) =>
    get<RecordSummary>(`/api/admin/records/by-uuid/${encodeURIComponent(recordId)}`),

  listPerformers: (params: PerformersListParams = {}) =>
    get<PaginatedResponse<PerformerSummary>>(`/api/admin/performers${buildQueryString(params)}`),

  getPerformer: (name: string) =>
    get<PerformerDetail>(`/api/admin/performers/${encodeURIComponent(name)}`),

  fleetStatus: () => get<FleetStatus>("/api/featurization/status"),

  perJobStatus: (jobId: string) =>
    get<PerJobFeatureStatus>(`/api/extraction/${encodeURIComponent(jobId)}/features`),

  matchByFragment: (req: FragmentMatchRequest, debug = false) =>
    postJson<unknown>(`/match/fragment${debug ? "?debug=1" : ""}`, req),

  matchByName: (req: NameMatchRequest, debug = false) =>
    postJson<unknown>(`/match/name${debug ? "?debug=1" : ""}`, req),
};
