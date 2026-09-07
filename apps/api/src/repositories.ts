import { DEFAULT_METRIC_CATALOG, type AssetClass, type MetricDefinition, type SavedScreen, type ScreenMatch, type ScreenRun } from "@stonks/contracts";
import { compileQuery, evaluateQuery, parseQuery, type QueryAst, typecheckQuery } from "@stonks/query";

export interface MetricRow { readonly metric: string; readonly value: number | null; readonly state: "present" | "missing" | "not_applicable"; readonly effectiveDate?: string | null; readonly stale?: boolean; }
export interface InstrumentRow { readonly instrumentId: string; readonly symbol: string | null; readonly name: string | null; readonly assetClass: AssetClass; readonly active: boolean; readonly metrics?: Readonly<Record<string, number | null>>; readonly metricRows?: readonly MetricRow[]; }
export interface Explanation { readonly matched: boolean; readonly text: string; readonly metrics: readonly string[]; }
export interface StatusDto { readonly effectiveDate: string | null; readonly datasetId: string | null; readonly sources: readonly { sourceId: string; expectedDate: string | null; loadedDate: string | null; status: string; stale?: boolean; coverage?: number | null }[]; }
export interface RunDetail extends ScreenRun { readonly matches: readonly (ScreenMatch & { readonly explanation: Explanation; readonly entered: boolean; readonly exited: boolean })[]; }
type RunMatch = RunDetail["matches"][number];
export type ChartBody = string | ArrayBuffer | Uint8Array | ReadableStream<Uint8Array>;
export interface ChartObject { readonly body: ChartBody; readonly contentType?: string; readonly contentEncoding?: string; }

export interface ResearchStore {
  activeDatasetId(): Promise<string | null>;
  status(datasetId: string | null): Promise<StatusDto>;
  metrics(): Promise<MetricDefinition[]>;
  listScreens(): Promise<SavedScreen[]>;
  listInstruments(datasetId: string): Promise<InstrumentRow[]>;
  getScreen(screenId: string): Promise<SavedScreen | null>;
  createScreen(input: { name: string; source: string; languageVersion: string; createdAt: string; updatedAt: string }): Promise<SavedScreen>;
  listRuns(screenId: string): Promise<RunDetail[]>;
  runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string): Promise<RunDetail>;
  instrument(datasetId: string, instrumentId: string): Promise<InstrumentRow | null>;
  chart(datasetId: string, instrumentId: string): Promise<ChartObject | null>;
}

export class MemoryResearchStore implements ResearchStore {
  public readonly screens = new Map<string, SavedScreen>();
  public readonly runs = new Map<string, RunDetail[]>();
  public readonly instruments = new Map<string, InstrumentRow>();
  public readonly charts = new Map<string, ChartObject>();
  public constructor(private dataset = "dataset-1") {}
  public setActiveDataset(datasetId: string): void { this.dataset = datasetId; }
  public async activeDatasetId(): Promise<string> { return this.dataset; }
  public async status(datasetId: string | null): Promise<StatusDto> { return { effectiveDate: datasetId ? "2026-09-04" : null, datasetId, sources: [] }; }
  public async metrics(): Promise<MetricDefinition[]> { return [...DEFAULT_METRIC_CATALOG]; }
  public async listScreens(): Promise<SavedScreen[]> { return [...this.screens.values()]; }
  public async listInstruments(_datasetId: string): Promise<InstrumentRow[]> { return [...this.instruments.values()]; }
  public async getScreen(screenId: string): Promise<SavedScreen | null> { return this.screens.get(screenId) ?? null; }
  public async createScreen(input: { name: string; source: string; languageVersion: string; createdAt: string; updatedAt: string }): Promise<SavedScreen> {
    const screen: SavedScreen = { id: crypto.randomUUID(), ...input };
    this.screens.set(screen.id, screen);
    return screen;
  }
  public async listRuns(screenId: string): Promise<RunDetail[]> { return [...this.runs.entries()].filter(([key]) => key.endsWith(`:${screenId}`)).flatMap(([, runs]) => runs); }
  public async runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string): Promise<RunDetail> {
    const parsed = parseQuery(screen.source);
    if (!parsed.value || parsed.diagnostics.length > 0) throw new Error("Invalid saved screen");
    const matches = [...this.instruments.values()].filter((item) => item.active && item.metrics && evaluateQuery(parsed.value!, item.metrics) === "true").sort((left, right) => (right.metrics?.momentum_score ?? -Infinity) - (left.metrics?.momentum_score ?? -Infinity) || left.instrumentId.localeCompare(right.instrumentId));
    const previous = (this.runs.get(`${datasetId}:${screen.id}`) ?? []).find((run) => run.status === "complete");
    const previousIds = new Set(previous?.matches.map((match) => match.instrumentId) ?? []);
    const currentIds = new Set(matches.map((match) => match.instrumentId));
    const currentMatches: RunMatch[] = matches.map((item, index) => ({ instrumentId: item.instrumentId, rank: index + 1, score: item.metrics?.momentum_score ?? null, explanation: { matched: true, text: `Matched ${screen.source}`, metrics: Object.keys(item.metrics ?? {}) }, entered: !previousIds.has(item.instrumentId), exited: false }));
    const exitedMatches: RunMatch[] = previous?.matches.filter((match) => !currentIds.has(match.instrumentId)).map((match) => ({ ...match, rank: 0, explanation: { ...match.explanation, matched: false }, entered: false, exited: true })) ?? [];
    const detail: RunDetail = { id: crypto.randomUUID(), screenId: screen.id, datasetId, effectiveDate, matchCount: matches.length, status: "complete", matches: [...currentMatches, ...exitedMatches] };
    this.runs.set(`${datasetId}:${screen.id}`, [detail, ...(this.runs.get(`${datasetId}:${screen.id}`) ?? [])]);
    return detail;
  }
  public async instrument(_datasetId: string, instrumentId: string): Promise<InstrumentRow | null> { return this.instruments.get(instrumentId) ?? null; }
  public async chart(_datasetId: string, instrumentId: string): Promise<ChartObject | null> { return this.charts.get(instrumentId) ?? null; }
}

export interface D1Result { readonly results?: readonly Record<string, unknown>[]; readonly success?: boolean; readonly meta?: Record<string, unknown>; }
export interface D1Statement { bind(...values: unknown[]): D1Statement; first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null>; all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }>; run(): Promise<D1Result>; }
export interface D1Database { prepare(sql: string): D1Statement; }

/** Thin parameterized D1 repository. It keeps all query data constrained to the active dataset. */
export class D1ResearchStore implements ResearchStore {
  public constructor(private readonly db: D1Database, private readonly chartStore?: { get(key: string): Promise<ChartObject | null> }) {}
  public async activeDatasetId(): Promise<string | null> { return (await this.db.prepare("SELECT dataset_id FROM active_dataset WHERE singleton = 1").first<{ dataset_id: string }>())?.dataset_id ?? null; }
  public async status(datasetId: string | null): Promise<StatusDto> {
    if (!datasetId) return { effectiveDate: null, datasetId: null, sources: [] };
    const dataset = await this.db.prepare("SELECT effective_date FROM datasets WHERE dataset_id = ?").bind(datasetId).first<{ effective_date: string | null }>();
    const result = await this.db.prepare("SELECT source_id, effective_date, status, metadata_json FROM sources WHERE dataset_id = ? ORDER BY source_id").bind(datasetId).all<{ source_id: string; effective_date: string | null; status: string; metadata_json: string }>();
    return { effectiveDate: dataset?.effective_date ?? null, datasetId, sources: result.results.map((row) => { const metadata = parseMetadata(row.metadata_json); const expectedDate = typeof metadata.expected_date === "string" ? metadata.expected_date : row.effective_date; const loadedDate = typeof metadata.loaded_date === "string" ? metadata.loaded_date : row.effective_date; return { sourceId: row.source_id, expectedDate, loadedDate, status: row.status, stale: Boolean(expectedDate && loadedDate && loadedDate < expectedDate) }; }) };
  }
  public async metrics(): Promise<MetricDefinition[]> { return [...DEFAULT_METRIC_CATALOG]; }
  public async listScreens(): Promise<SavedScreen[]> { const result = await this.db.prepare("SELECT screen_id, name, expression, created_at, updated_at FROM saved_screens ORDER BY updated_at DESC").all<Record<string, unknown>>(); return result.results.map((row) => this.screen(row)); }
  public async listInstruments(datasetId: string): Promise<InstrumentRow[]> { const result = await this.db.prepare("SELECT instrument_id, symbol, name, asset_class, active FROM instruments WHERE dataset_id = ? AND active = 1 ORDER BY symbol, instrument_id").bind(datasetId).all<Record<string, unknown>>(); return result.results.map((row) => ({ instrumentId: String(row.instrument_id), symbol: row.symbol == null ? null : String(row.symbol), name: row.name == null ? null : String(row.name), assetClass: String(row.asset_class) as AssetClass, active: Boolean(row.active) })); }
  public async getScreen(screenId: string): Promise<SavedScreen | null> { const row = await this.db.prepare("SELECT screen_id, name, expression, created_at, updated_at FROM saved_screens WHERE screen_id = ?").bind(screenId).first<Record<string, unknown>>(); return row ? this.screen(row) : null; }
  public async createScreen(input: { name: string; source: string; languageVersion: string; createdAt: string; updatedAt: string }): Promise<SavedScreen> { const id = crypto.randomUUID(); const result = await this.db.prepare("INSERT INTO saved_screens (screen_id, name, expression, created_at, updated_at) VALUES (?, ?, ?, ?, ?)").bind(id, input.name, input.source, input.createdAt, input.updatedAt).run(); ensureD1Success(result); return { id, name: input.name, source: input.source, languageVersion: input.languageVersion, createdAt: input.createdAt, updatedAt: input.updatedAt }; }
  public async listRuns(screenId: string): Promise<RunDetail[]> { const result = await this.db.prepare("SELECT run_id, screen_id, dataset_id, effective_date, result_count, status FROM screen_runs WHERE screen_id = ? ORDER BY effective_date DESC, run_id DESC").bind(screenId).all<Record<string, unknown>>(); return Promise.all(result.results.map(async (row) => { const matches = await this.db.prepare("SELECT instrument_id, ordinal, score, explanation_json, entered, exited FROM screen_matches WHERE dataset_id = ? AND run_id = ? ORDER BY ordinal").bind(String(row.dataset_id), String(row.run_id)).all<Record<string, unknown>>(); return { id: String(row.run_id), screenId: String(row.screen_id), datasetId: String(row.dataset_id), effectiveDate: String(row.effective_date), matchCount: Number(row.result_count), status: row.status === "failed" ? "failed" : "complete", matches: matches.results.map((match) => ({ instrumentId: String(match.instrument_id), rank: Number(match.ordinal), score: match.score == null ? null : Number(match.score), explanation: parseExplanation(match.explanation_json), entered: Boolean(match.entered), exited: Boolean(match.exited) })) }; }));
  }
  public async runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string): Promise<RunDetail> {
    const parsed = parseQuery(screen.source);
    if (!parsed.value || parsed.diagnostics.length > 0) throw new Error("Invalid saved screen");
    const checked = typecheckScreen(parsed.value);
    if (!checked) throw new Error("Invalid saved screen");
    const compiled = compileQuery(checked, DEFAULT_METRIC_CATALOG, { relation: "eav", includeDatasetFilter: false });
    const rows = await this.db.prepare(`SELECT i.instrument_id, (SELECT m.value FROM latest_metrics AS m WHERE m.dataset_id = i.dataset_id AND m.instrument_id = i.instrument_id AND m.metric = 'momentum_score' AND m.state = 'present') AS score FROM instruments AS i WHERE i.dataset_id = ? AND i.active = 1 AND ${compiled.whereSql} ORDER BY score DESC NULLS LAST, i.instrument_id`).bind(datasetId, ...compiled.params).all<{ instrument_id: string; score: number | null }>();
    const prior = (await this.listRuns(screen.id)).filter((run) => run.datasetId === datasetId).find((run) => run.status === "complete");
    const priorIds = new Set(prior?.matches.map((match) => match.instrumentId) ?? []);
    const currentIds = new Set(rows.results.map((row) => row.instrument_id));
    const currentMatches: RunMatch[] = rows.results.map((row, index) => ({ instrumentId: row.instrument_id, rank: index + 1, score: row.score == null ? null : Number(row.score), explanation: { matched: true, text: `Matched ${screen.source}`, metrics: compiled.referencedMetricIds }, entered: !priorIds.has(row.instrument_id), exited: false }));
    const exitedMatches: RunMatch[] = prior?.matches.filter((match) => !currentIds.has(match.instrumentId)).map((match) => ({ ...match, rank: 0, explanation: { ...match.explanation, matched: false }, entered: false, exited: true })) ?? [];
    const matches: RunMatch[] = [...currentMatches, ...exitedMatches];
    const runId = crypto.randomUUID();
    ensureD1Success(await this.db.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, query_version) VALUES (?, ?, ?, ?, 'complete', ?, 'v1')").bind(datasetId, runId, screen.id, effectiveDate, rows.results.length).run());
    for (const match of matches) ensureD1Success(await this.db.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, explanation_json, entered, exited) VALUES (?, ?, ?, ?, ?, ?, ?, ?)").bind(datasetId, runId, match.rank, match.instrumentId, match.score, JSON.stringify(match.explanation), match.entered ? 1 : 0, match.exited ? 1 : 0).run());
    return { id: runId, screenId: screen.id, datasetId, effectiveDate, matchCount: rows.results.length, status: "complete", matches };
  }
  public async instrument(datasetId: string, instrumentId: string): Promise<InstrumentRow | null> { const row = await this.db.prepare("SELECT instrument_id, symbol, name, asset_class, active FROM instruments WHERE dataset_id = ? AND instrument_id = ?").bind(datasetId, instrumentId).first<Record<string, unknown>>(); if (!row) return null; const dataset = await this.db.prepare("SELECT effective_date FROM datasets WHERE dataset_id = ?").bind(datasetId).first<{ effective_date: string | null }>(); const metrics = await this.db.prepare("SELECT metric, value, state, effective_date FROM latest_metrics WHERE dataset_id = ? AND instrument_id = ? ORDER BY metric").bind(datasetId, instrumentId).all<{ metric: string; value: number | null; state: MetricRow["state"]; effective_date: string | null }>(); return { instrumentId: String(row.instrument_id), symbol: row.symbol == null ? null : String(row.symbol), name: row.name == null ? null : String(row.name), assetClass: String(row.asset_class) as AssetClass, active: Boolean(row.active), metricRows: metrics.results.map((metric) => ({ metric: metric.metric, value: metric.value, state: metric.state, effectiveDate: metric.effective_date, stale: Boolean(dataset?.effective_date && metric.effective_date && metric.effective_date < dataset.effective_date) })) }; }
  public async chart(datasetId: string, instrumentId: string): Promise<ChartObject | null> { return this.chartStore ? this.chartStore.get(`charts/${datasetId}/${instrumentId}.json.gz`) : null; }
  private screen(row: Record<string, unknown>): SavedScreen { return { id: String(row.screen_id), name: String(row.name), source: String(row.expression), languageVersion: "v1", createdAt: String(row.created_at), updatedAt: String(row.updated_at) }; }
}

function parseExplanation(value: unknown): Explanation { if (typeof value !== "string") return { matched: true, text: "", metrics: [] }; try { const parsed: unknown = JSON.parse(value); if (typeof parsed === "object" && parsed !== null && "matched" in parsed && "text" in parsed && "metrics" in parsed && Array.isArray(parsed.metrics)) return { matched: Boolean(parsed.matched), text: String(parsed.text), metrics: parsed.metrics.map(String) }; } catch { /* corrupted explanation is represented, not executed */ } return { matched: true, text: "", metrics: [] }; }
function parseMetadata(value: unknown): Record<string, unknown> { if (typeof value !== "string") return {}; try { const parsed: unknown = JSON.parse(value); return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {}; } catch { return {}; } }

function typecheckScreen(ast: QueryAst): QueryAst | null { const checked = typecheckQuery(ast, DEFAULT_METRIC_CATALOG); return checked.valid ? checked.ast : null; }
function ensureD1Success(result: D1Result): void { if (result.success === false) throw new Error("D1 mutation failed"); }
