import type { MetricDefinition, SavedScreen, ScreenRun } from "@stonks/contracts";

export interface StatusSource {
  readonly sourceId: string;
  readonly expectedDate: string | null;
  readonly loadedDate: string | null;
  readonly status: string;
  readonly stale?: boolean;
  readonly coverage?: number | null;
}

export interface StatusDto {
  readonly effectiveDate: string | null;
  readonly datasetId: string | null;
  readonly sources: readonly StatusSource[];
}

export interface Page<T> {
  readonly data: readonly T[];
  readonly pagination: { readonly limit: number; readonly offset: number; readonly total: number };
}

export interface DashboardApi {
  getStatus(): Promise<StatusDto>;
  getMetrics(): Promise<readonly MetricDefinition[]>;
  getScreens(): Promise<Page<SavedScreen>>;
  getRuns?(screenId: string): Promise<{ readonly screen: SavedScreen; readonly runs: readonly ScreenRunSummary[] }>;
  createScreen(input: { readonly name: string; readonly source: string }): Promise<SavedScreen>;
  runScreen(screenId: string): Promise<ScreenRun>;
}

export interface ScreenRunSummary extends ScreenRun {
  readonly matches?: readonly { readonly entered: boolean; readonly exited: boolean }[];
}

export interface ApiClientOptions {
  readonly baseUrl?: string;
  readonly fetcher?: typeof fetch;
}

export function createApiClient(options: ApiClientOptions = {}): DashboardApi {
  const baseUrl = options.baseUrl ?? "";
  const fetcher = options.fetcher ?? fetch;
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetcher(`${baseUrl}${path}`, { ...init, credentials: "include", headers: { Accept: "application/json", ...(init?.headers ?? {}) } });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return response.json() as Promise<T>;
  }
  return {
    getStatus: () => request<StatusDto>("/api/v1/status"),
    getMetrics: async () => (await request<{ metrics: readonly MetricDefinition[] }>("/api/v1/metrics")).metrics,
    getScreens: () => request<Page<SavedScreen>>("/api/v1/screens"),
    getRuns: (screenId) => request<{ screen: SavedScreen; runs: readonly ScreenRunSummary[] }>(`/api/v1/screens/${encodeURIComponent(screenId)}/runs`),
    createScreen: (input) => request<SavedScreen>("/api/v1/screens", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }),
    runScreen: (screenId) => request<ScreenRun>(`/api/v1/screens/${encodeURIComponent(screenId)}/runs`, { method: "POST", headers: { "Content-Type": "application/json" } }),
  };
}
