import type { AssetClass, MetricDefinition, MetricState, SavedScreen, ScreenRun } from "@stonks/contracts";

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

export interface MetricValueDto {
  readonly value: number | null;
  readonly state: MetricState;
  readonly effectiveDate?: string | null;
  readonly stale?: boolean;
  readonly rawValue?: string | null;
  readonly normalizedValue?: number | null;
  readonly formulaVersion?: string | null;
  readonly sourceArtifactId?: string | null;
  readonly metadata?: Readonly<Record<string, unknown>>;
}

export interface FundamentalPeriodDto { readonly periodId: string; readonly periodEnd: string; readonly periodType: string; readonly filingId: string; readonly filedAt: string; readonly metrics: Readonly<Record<string, unknown>>; readonly sourceArtifactId?: string | null; }
export interface CorporateActionDto { readonly actionId: string; readonly actionDate: string; readonly actionType: string; readonly numerator?: number | null; readonly denominator?: number | null; readonly metadata?: Readonly<Record<string, unknown>>; readonly sourceArtifactId?: string | null; }

export interface MomentumComponentDto {
  readonly componentId: string;
  readonly label: string;
  readonly unit: "percent" | "ratio" | "count";
  readonly raw: number | null;
  readonly normalized: number | null;
  readonly weight: number;
  readonly contribution: number | null;
}

export interface MomentumBreakdownDto {
  readonly components: readonly MomentumComponentDto[];
  readonly cohort: string;
  readonly formulaVersion: string;
  readonly coverage: number;
  readonly sourceDate: string | null;
  readonly warning?: string;
}

export interface ClauseExplanationDto {
  readonly clause: string;
  readonly metric?: string;
  readonly result: "Matched" | "Unavailable" | "Not applicable" | "Not matched";
  readonly value?: MetricValueDto;
}

export interface ExplanationDto {
  readonly matched: boolean;
  readonly text: string;
  readonly metrics: readonly string[];
  readonly clauses?: readonly ClauseExplanationDto[];
  readonly momentum?: MomentumBreakdownDto;
}

export interface InstrumentDto {
  readonly instrumentId: string;
  readonly symbol: string | null;
  readonly name: string | null;
  readonly assetClass: AssetClass;
  readonly active: boolean;
  readonly metrics?: Readonly<Record<string, number | null>>;
  readonly metricRows?: readonly ({ readonly metric: string } & MetricValueDto)[];
  readonly metadata?: Readonly<Record<string, string | number | null>>;
  readonly momentum?: MomentumBreakdownDto;
  readonly fundamentalPeriods?: readonly FundamentalPeriodDto[];
  readonly corporateActions?: readonly CorporateActionDto[];
}

export interface ResultMatchDto {
  readonly instrumentId: string;
  readonly rank: number;
  readonly score: number | null;
  readonly entered: boolean;
  readonly exited: boolean;
  readonly symbol?: string | null;
  readonly name?: string | null;
  readonly assetClass?: AssetClass;
  readonly explanation?: ExplanationDto;
  readonly momentum?: MomentumBreakdownDto;
}

export interface ChartPointDto { readonly date: string; readonly value: number | null; }
export interface ChartDto { readonly points: readonly ChartPointDto[]; readonly valueLabel?: string; readonly sourceDate?: string | null; }
export interface ScreenResultsDto {
  readonly screen: SavedScreen;
  readonly run: ScreenRun & { readonly matches: readonly ResultMatchDto[] };
  readonly pagination: Page<ResultMatchDto>["pagination"];
}

export interface DashboardApi {
  getStatus(): Promise<StatusDto>;
  getMetrics(): Promise<readonly MetricDefinition[]>;
  getScreens(): Promise<Page<SavedScreen>>;
  getRuns?(screenId: string): Promise<{ readonly screen: SavedScreen; readonly runs: readonly ScreenRunSummary[] }>;
  getResults?(screenId: string, options?: { readonly runId?: string; readonly limit?: number; readonly offset?: number; readonly sort?: "rank" | "score" | "symbol" | "assetClass"; readonly direction?: "asc" | "desc" }): Promise<ScreenResultsDto>;
  getInstrument?(instrumentId: string): Promise<InstrumentDto>;
  getChart?(instrumentId: string): Promise<ChartDto>;
  createScreen(input: { readonly name: string; readonly source: string }): Promise<SavedScreen>;
  updateScreen?(screenId: string, input: { readonly name: string; readonly source: string }): Promise<SavedScreen>;
  runScreen(screenId: string): Promise<ScreenRun & { readonly matches?: readonly ResultMatchDto[] }>;
}

export interface ScreenRunSummary extends ScreenRun {
  readonly matches?: readonly ResultMatchDto[];
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
    getResults: (screenId, options = {}) => {
      const params = new URLSearchParams();
      if (options.runId) params.set("runId", options.runId);
      if (options.limit !== undefined) params.set("limit", String(options.limit));
      if (options.offset !== undefined) params.set("offset", String(options.offset));
      if (options.sort) params.set("sort", options.sort);
      if (options.direction) params.set("direction", options.direction);
      return request<ScreenResultsDto>(`/api/v1/screens/${encodeURIComponent(screenId)}/results${params.toString() ? `?${params}` : ""}`);
    },
    getInstrument: (instrumentId) => request<InstrumentDto>(`/api/v1/instruments/${encodeURIComponent(instrumentId)}`),
    getChart: async (instrumentId) => request<ChartDto>(`/api/v1/instruments/${encodeURIComponent(instrumentId)}/chart`),
    createScreen: (input) => request<SavedScreen>("/api/v1/screens", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }),
    updateScreen: (screenId, input) => request<SavedScreen>(`/api/v1/screens/${encodeURIComponent(screenId)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }),
    runScreen: (screenId) => request<ScreenRun & { readonly matches?: readonly ResultMatchDto[] }>(`/api/v1/screens/${encodeURIComponent(screenId)}/runs`, { method: "POST", headers: { "Content-Type": "application/json" } }),
  };
}
