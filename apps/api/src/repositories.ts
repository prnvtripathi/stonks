import { DEFAULT_GLOSSARY_ENTRIES, DEFAULT_METRIC_CATALOG, type AssetClass, type GlossaryEntry, type MetricDefinition, type SavedScreen, type ScreenMatch, type ScreenRun } from "@stonks/contracts";
import { compileQuery, evaluateQuery, parseQuery, printAst, type Expression, type QueryAst, typecheckQuery } from "@stonks/query";

export interface MetricRow { readonly metric: string; readonly value: number | null; readonly state: "present" | "missing" | "not_applicable"; readonly effectiveDate?: string | null; readonly stale?: boolean; readonly rawValue?: string | null; readonly normalizedValue?: number | null; readonly formulaVersion?: string | null; readonly sourceArtifactId?: string | null; readonly metadata?: Readonly<Record<string, unknown>>; }
export interface FundamentalPeriodRow { readonly periodId: string; readonly periodEnd: string; readonly periodType: string; readonly filingId: string; readonly filedAt: string; readonly metrics: Readonly<Record<string, unknown>>; readonly sourceArtifactId?: string | null; }
export interface CorporateActionRow { readonly actionId: string; readonly actionDate: string; readonly actionType: string; readonly numerator?: number | null; readonly denominator?: number | null; readonly metadata?: Readonly<Record<string, unknown>>; readonly sourceArtifactId?: string | null; }
export interface InstrumentRow { readonly instrumentId: string; readonly symbol: string | null; readonly name: string | null; readonly assetClass: AssetClass; readonly active: boolean; readonly metrics?: Readonly<Record<string, number | null>>; readonly metricRows?: readonly MetricRow[]; readonly metadata?: Readonly<Record<string, string | number | null>>; readonly fundamentalPeriods?: readonly FundamentalPeriodRow[]; readonly corporateActions?: readonly CorporateActionRow[]; readonly momentum?: MomentumBreakdown; }
export interface ExplanationClause { readonly clause: string; readonly metric?: string; readonly result: "Matched" | "Unavailable" | "Not applicable" | "Not matched"; readonly value?: Omit<MetricRow, "metric">; }
export interface MomentumComponent { readonly componentId: string; readonly label: string; readonly unit: "percent" | "ratio" | "count"; readonly raw: number | null; readonly normalized: number | null; readonly weight: number; readonly contribution: number | null; }
export interface MomentumBreakdown { readonly components: readonly MomentumComponent[]; readonly cohort: string; readonly formulaVersion: string; readonly coverage: number; readonly sourceDate: string | null; readonly warning?: string; }
export interface Explanation { readonly matched: boolean; readonly text: string; readonly metrics: readonly string[]; readonly clauses?: readonly ExplanationClause[]; readonly momentum?: MomentumBreakdown; }
export interface StatusDto { readonly effectiveDate: string | null; readonly datasetId: string | null; readonly sources: readonly { sourceId: string; expectedDate: string | null; loadedDate: string | null; status: string; stale?: boolean; coverage?: number | null }[]; }

/** One page of a run's live (non-exited) "top matches", plus the total count of that set. */
export type ResultSortField = "rank" | "score" | "symbol" | "assetClass";
export type SortDirection = "asc" | "desc";
export interface ResultPageOptions { readonly sort: ResultSortField; readonly direction: SortDirection; readonly limit: number; readonly offset: number; }
export interface RunMatchDto extends ScreenMatch { readonly explanation: Explanation; readonly momentum?: MomentumBreakdown; readonly entered: boolean; readonly exited: boolean; }
export interface RunMatchPage { readonly matches: readonly RunMatchDto[]; readonly total: number; }
export type ChartBody = string | ArrayBuffer | Uint8Array | ReadableStream<Uint8Array>;
export interface ChartObject { readonly body: ChartBody; readonly contentType?: string; readonly contentEncoding?: string; }

/**
 * A screen run's matches are never embedded on `ScreenRun` itself: a
 * historical run can carry thousands of matches, and materializing them
 * whenever a run summary is fetched is the N+1/unbounded-retrieval pattern
 * this store exists to avoid. Callers fetch a run's matches, one bounded
 * page at a time, through `pageRunMatches`.
 */
export interface ResearchStore {
  activeDatasetId(): Promise<string | null>;
  status(datasetId: string | null): Promise<StatusDto>;
  metrics(): Promise<MetricDefinition[]>;
  listScreens(): Promise<SavedScreen[]>;
  listInstruments(datasetId: string): Promise<InstrumentRow[]>;
  getScreen(screenId: string): Promise<SavedScreen | null>;
  createScreen(input: { name: string; source: string; languageVersion: string; createdAt: string; updatedAt: string }): Promise<SavedScreen>;
  updateScreen(input: { id: string; name: string; source: string; updatedAt: string }): Promise<SavedScreen>;
  /** Run summaries only, most-recently-executed first. Never embeds matches. */
  listRuns(screenId: string): Promise<ScreenRun[]>;
  /** One run summary: the run named by `runId`, or the latest complete run when omitted. */
  getRun(screenId: string, runId?: string): Promise<ScreenRun | null>;
  /** One bounded, sorted page of a run's live matches, plus the total live-match count. */
  pageRunMatches(runId: string, options: ResultPageOptions): Promise<RunMatchPage>;
  runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string, completedAt?: string): Promise<ScreenRun>;
  instrument(datasetId: string, instrumentId: string): Promise<InstrumentRow | null>;
  chart(datasetId: string, instrumentId: string): Promise<ChartObject | null>;
  listGlossary(): Promise<readonly GlossaryEntry[]>;
  glossary(slug: string): Promise<GlossaryEntry | null>;
}

interface StoredMatch { readonly instrumentId: string; readonly ordinal: number; readonly score: number | null; readonly symbol: string | null; readonly name: string | null; readonly assetClass: AssetClass | null; readonly metricRows: readonly MetricRow[]; readonly entered: boolean; readonly exited: boolean; }
interface StoredRun extends ScreenRun { readonly matches: readonly StoredMatch[]; }

const SORT_TIEBREAK = (left: StoredMatch, right: StoredMatch): number => left.instrumentId.localeCompare(right.instrumentId);

function sortStoredMatches(matches: readonly StoredMatch[], options: ResultPageOptions): StoredMatch[] {
  const factor = options.direction === "asc" ? 1 : -1;
  const value = (match: StoredMatch): number | string => {
    if (options.sort === "rank") return match.ordinal;
    if (options.sort === "score") return match.score ?? -Infinity;
    if (options.sort === "symbol") return match.symbol ?? "";
    return match.assetClass ?? "";
  };
  return [...matches].sort((left, right) => {
    const leftValue = value(left);
    const rightValue = value(right);
    const comparison = typeof leftValue === "string" ? leftValue.localeCompare(String(rightValue)) : Number(leftValue) - Number(rightValue);
    return comparison * factor || SORT_TIEBREAK(left, right);
  });
}

export class MemoryResearchStore implements ResearchStore {
  public readonly screens = new Map<string, SavedScreen>();
  public readonly runs = new Map<string, StoredRun[]>();
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
  public async updateScreen(input: { id: string; name: string; source: string; updatedAt: string }): Promise<SavedScreen> {
    const existing = this.screens.get(input.id);
    if (!existing) throw new Error("Screen not found");
    const screen: SavedScreen = { ...existing, name: input.name, source: input.source, updatedAt: input.updatedAt };
    this.screens.set(input.id, screen);
    return screen;
  }
  private sortedInternalRuns(screenId: string): StoredRun[] {
    return [...this.runs.entries()]
      .filter(([key]) => key.endsWith(`:${screenId}`))
      .flatMap(([, runs]) => runs)
      .sort(compareRunsByLatestExecution);
  }
  private findRunById(runId: string): StoredRun | undefined {
    for (const runs of this.runs.values()) { const found = runs.find((run) => run.id === runId); if (found) return found; }
    return undefined;
  }
  public async listRuns(screenId: string): Promise<ScreenRun[]> { return this.sortedInternalRuns(screenId).map(stripMatches); }
  public async getRun(screenId: string, runId?: string): Promise<ScreenRun | null> {
    const runs = this.sortedInternalRuns(screenId);
    const run = runId ? runs.find((candidate) => candidate.id === runId) : runs.find((candidate) => candidate.status === "complete");
    return run ? stripMatches(run) : null;
  }
  public async pageRunMatches(runId: string, options: ResultPageOptions): Promise<RunMatchPage> {
    const run = this.findRunById(runId);
    if (!run) return { matches: [], total: 0 };
    const parsed = run.source ? parseQuery(run.source).value ?? undefined : undefined;
    const live = run.matches.filter((match) => !match.exited);
    const ordered = sortStoredMatches(live, options);
    const page = ordered.slice(options.offset, options.offset + options.limit);
    const matches = page.map((match) => storedMatchToDto(match, run.source, parsed));
    return { matches, total: live.length };
  }
  public async runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string, completedAt = new Date().toISOString()): Promise<ScreenRun> {
    const parsed = parseQuery(screen.source);
    if (!parsed.value || parsed.diagnostics.length > 0) throw new Error("Invalid saved screen");
    const matches = [...this.instruments.values()].filter((item) => { const metrics = item.metrics ?? Object.fromEntries((item.metricRows ?? []).map((row) => [row.metric, row.value])); return item.active && evaluateQuery(parsed.value!, metrics) === "true"; }).sort((left, right) => (right.metricRows?.find((row) => row.metric === "momentum_score")?.value ?? right.metrics?.momentum_score ?? -Infinity) - (left.metricRows?.find((row) => row.metric === "momentum_score")?.value ?? left.metrics?.momentum_score ?? -Infinity) || left.instrumentId.localeCompare(right.instrumentId));
    const previous = this.sortedInternalRuns(screen.id).find((run) => run.status === "complete");
    const previousIds = new Set(previous?.matches.filter((match) => !match.exited).map((match) => match.instrumentId) ?? []);
    const currentIds = new Set(matches.map((match) => match.instrumentId));
    const currentMatches: StoredMatch[] = matches.map((item, index) => { const metricRows = item.metricRows ?? Object.entries(item.metrics ?? {}).map(([metric, value]) => ({ metric, value, state: value == null ? "missing" as const : "present" as const })); const score = item.metricRows?.find((row) => row.metric === "momentum_score")?.value ?? item.metrics?.momentum_score ?? null; return { instrumentId: item.instrumentId, ordinal: index + 1, score, symbol: item.symbol, name: item.name, assetClass: item.assetClass, metricRows, entered: !previousIds.has(item.instrumentId), exited: false }; });
    const exitedMatches: StoredMatch[] = (previous?.matches.filter((match) => !match.exited && !currentIds.has(match.instrumentId)) ?? []).map((match, index) => ({ ...match, ordinal: currentMatches.length + index + 1, entered: false, exited: true }));
    const run: StoredRun = { id: crypto.randomUUID(), screenId: screen.id, datasetId, effectiveDate, completedAt, matchCount: currentMatches.length, status: "complete", source: screen.source, languageVersion: screen.languageVersion, matches: [...currentMatches, ...exitedMatches] };
    this.runs.set(`${datasetId}:${screen.id}`, [run, ...(this.runs.get(`${datasetId}:${screen.id}`) ?? [])]);
    return stripMatches(run);
  }
  public async instrument(_datasetId: string, instrumentId: string): Promise<InstrumentRow | null> { const instrument = this.instruments.get(instrumentId); return instrument ? { ...instrument, momentum: instrument.momentum ?? buildMomentum(instrument) } : null; }
  public async chart(_datasetId: string, instrumentId: string): Promise<ChartObject | null> { return this.charts.get(instrumentId) ?? null; }
  public async listGlossary(): Promise<readonly GlossaryEntry[]> { return DEFAULT_GLOSSARY_ENTRIES; }
  public async glossary(slug: string): Promise<GlossaryEntry | null> { return DEFAULT_GLOSSARY_ENTRIES.find((entry) => entry.slug === slug) ?? null; }
}

function stripMatches(run: StoredRun): ScreenRun { const { matches: _matches, ...summary } = run; return summary; }
function storedMatchToDto(match: StoredMatch, source: string | null, ast?: Expression): RunMatchDto {
  const instrumentLike: InstrumentRow = { instrumentId: match.instrumentId, symbol: match.symbol, name: match.name, assetClass: (match.assetClass ?? "equity") as AssetClass, active: true, metricRows: match.metricRows };
  const explanation = source ? buildExplanation(source, instrumentLike, ast) : { matched: true, text: "", metrics: [] };
  return { instrumentId: match.instrumentId, rank: match.ordinal, score: match.score, symbol: match.symbol, name: match.name, assetClass: match.assetClass, explanation, ...(explanation.momentum ? { momentum: explanation.momentum } : {}), entered: match.entered, exited: false };
}

export interface D1Result { readonly results?: readonly Record<string, unknown>[]; readonly success?: boolean; readonly meta?: Record<string, unknown>; }
export interface D1Statement { bind(...values: unknown[]): D1Statement; first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null>; all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }>; run(): Promise<D1Result>; }
export interface D1Database { prepare(sql: string): D1Statement; batch?(statements: readonly D1Statement[]): Promise<readonly D1Result[]>; }

/** Most recently *executed* complete run for a screen: julianday(completed_at) DESC when parseable, else effective_date/run_id as a stable fallback. */
const RUN_ORDER_SQL = "CASE WHEN completed_at IS NOT NULL AND julianday(completed_at) IS NOT NULL THEN 0 ELSE 1 END ASC, julianday(completed_at) DESC, effective_date DESC, run_id DESC";
const PREVIOUS_RUN_SQL = `SELECT run_id FROM screen_runs WHERE screen_id = ? AND status = 'complete' ORDER BY ${RUN_ORDER_SQL} LIMIT 1`;
const SORT_COLUMNS: Readonly<Record<ResultSortField, string>> = { rank: "ordinal", score: "score", symbol: "symbol", assetClass: "asset_class" };

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
  public async listInstruments(datasetId: string): Promise<InstrumentRow[]> { const result = await this.db.prepare("SELECT instrument_id, symbol, name, asset_class, active, metadata_json FROM instrument_snapshots WHERE dataset_id = ? AND active = 1 ORDER BY symbol, instrument_id").bind(datasetId).all<Record<string, unknown>>(); return result.results.map((row) => ({ instrumentId: String(row.instrument_id), symbol: row.symbol == null ? null : String(row.symbol), name: row.name == null ? null : String(row.name), assetClass: String(row.asset_class) as AssetClass, active: Boolean(row.active), metadata: parseMetadata(row.metadata_json) as Record<string, string | number | null> })); }
  public async getScreen(screenId: string): Promise<SavedScreen | null> { const row = await this.db.prepare("SELECT screen_id, name, expression, created_at, updated_at FROM saved_screens WHERE screen_id = ?").bind(screenId).first<Record<string, unknown>>(); return row ? this.screen(row) : null; }
  public async createScreen(input: { name: string; source: string; languageVersion: string; createdAt: string; updatedAt: string }): Promise<SavedScreen> { const id = crypto.randomUUID(); const result = await this.db.prepare("INSERT INTO saved_screens (screen_id, name, expression, created_at, updated_at) VALUES (?, ?, ?, ?, ?)").bind(id, input.name, input.source, input.createdAt, input.updatedAt).run(); ensureD1Success(result); return { id, name: input.name, source: input.source, languageVersion: input.languageVersion, createdAt: input.createdAt, updatedAt: input.updatedAt }; }
  public async updateScreen(input: { id: string; name: string; source: string; updatedAt: string }): Promise<SavedScreen> {
    const existing = await this.getScreen(input.id);
    if (!existing) throw new Error("Screen not found");
    const result = await this.db.prepare("UPDATE saved_screens SET name = ?, expression = ?, updated_at = ? WHERE screen_id = ?").bind(input.name, input.source, input.updatedAt, input.id).run();
    ensureD1Success(result);
    return { ...existing, name: input.name, source: input.source, updatedAt: input.updatedAt };
  }
  private runSummary(row: Record<string, unknown>): ScreenRun {
    return { id: String(row.run_id), screenId: String(row.screen_id), datasetId: String(row.dataset_id), effectiveDate: String(row.effective_date), ...(row.completed_at ? { completedAt: String(row.completed_at) } : {}), matchCount: Number(row.result_count), status: row.status === "failed" ? "failed" : "complete", source: row.source == null ? null : String(row.source), languageVersion: row.language_version == null ? null : String(row.language_version) };
  }
  public async listRuns(screenId: string): Promise<ScreenRun[]> {
    const result = await this.db.prepare(`SELECT run_id, screen_id, dataset_id, effective_date, completed_at, result_count, status, source, language_version FROM screen_runs WHERE screen_id = ? ORDER BY ${RUN_ORDER_SQL}`).bind(screenId).all<Record<string, unknown>>();
    return result.results.map((row) => this.runSummary(row));
  }
  public async getRun(screenId: string, runId?: string): Promise<ScreenRun | null> {
    const row = runId
      ? await this.db.prepare("SELECT run_id, screen_id, dataset_id, effective_date, completed_at, result_count, status, source, language_version FROM screen_runs WHERE screen_id = ? AND run_id = ?").bind(screenId, runId).first<Record<string, unknown>>()
      : await this.db.prepare(`SELECT run_id, screen_id, dataset_id, effective_date, completed_at, result_count, status, source, language_version FROM screen_runs WHERE screen_id = ? AND status = 'complete' ORDER BY ${RUN_ORDER_SQL} LIMIT 1`).bind(screenId).first<Record<string, unknown>>();
    return row ? this.runSummary(row) : null;
  }
  public async pageRunMatches(runId: string, options: ResultPageOptions): Promise<RunMatchPage> {
    const column = SORT_COLUMNS[options.sort];
    if (!column) throw new Error("Invalid sort field");
    if (options.direction !== "asc" && options.direction !== "desc") throw new Error("Invalid sort direction");
    const totalRow = await this.db.prepare("SELECT COUNT(*) AS total FROM screen_matches WHERE run_id = ? AND exited = 0").bind(runId).first<{ total: number }>();
    const total = Number(totalRow?.total ?? 0);
    const direction = options.direction.toUpperCase();
    const pageResult = await this.db.prepare(`SELECT instrument_id, ordinal, score, symbol, name, asset_class, metrics_json, entered FROM screen_matches WHERE run_id = ? AND exited = 0 ORDER BY ${column} ${direction}, instrument_id ASC LIMIT ? OFFSET ?`).bind(runId, options.limit, options.offset).all<Record<string, unknown>>();
    const runRow = await this.db.prepare("SELECT source, language_version FROM screen_runs WHERE run_id = ?").bind(runId).first<{ source: string | null }>();
    const source = runRow?.source ?? null;
    const ast = source ? parseQuery(source).value ?? undefined : undefined;
    const matches = pageResult.results.map((row) => this.matchDto(row, source, ast));
    return { matches, total };
  }
  private matchDto(row: Record<string, unknown>, source: string | null, ast?: Expression): RunMatchDto {
    const metricRows = parseMetricRows(row.metrics_json);
    const symbol = row.symbol == null ? null : String(row.symbol);
    const name = row.name == null ? null : String(row.name);
    const assetClass = row.asset_class == null ? null : (String(row.asset_class) as AssetClass);
    const instrumentLike: InstrumentRow = { instrumentId: String(row.instrument_id), symbol, name, assetClass: (assetClass ?? "equity") as AssetClass, active: true, metricRows };
    const explanation = source ? buildExplanation(source, instrumentLike, ast) : { matched: true, text: "", metrics: [] };
    return { instrumentId: String(row.instrument_id), rank: Number(row.ordinal), score: row.score == null ? null : Number(row.score), symbol, name, assetClass, explanation, ...(explanation.momentum ? { momentum: explanation.momentum } : {}), entered: Boolean(row.entered), exited: false };
  }
  /**
   * Runs a saved screen with set-based SQL, independent of universe size:
   * one lookup resolves the previous successful run, two `INSERT ... SELECT`
   * statements snapshot the matching instrument IDs, score/rank, identity,
   * and only the metric rows the screen's predicates (plus the momentum
   * breakdown) need -- never a per-instrument read or per-match write -- and
   * a third marks the run complete. The three writes are batched atomically
   * so a run is never observable as complete without its matches.
   */
  public async runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string, completedAt = new Date().toISOString()): Promise<ScreenRun> {
    const parsed = parseQuery(screen.source);
    if (!parsed.value || parsed.diagnostics.length > 0) throw new Error("Invalid saved screen");
    const checked = typecheckScreen(parsed.value);
    if (!checked) throw new Error("Invalid saved screen");
    const compiled = compileQuery(checked, DEFAULT_METRIC_CATALOG, { relation: "snapshot", includeDatasetFilter: false });
    const runId = crypto.randomUUID();
    const referencedMetricsJson = JSON.stringify(compiled.referencedMetricIds);
    // Resolved once, up front, and bound as a literal below rather than a
    // correlated subquery: SQLite cannot hoist a per-candidate-row lookup of
    // "the previous run" out of a NOT EXISTS clause on its own, and an
    // uncached lookup there would rescan `screen_runs` once per matching
    // instrument instead of once per run.
    const previousRunRow = await this.db.prepare(PREVIOUS_RUN_SQL).bind(screen.id).first<{ run_id: string }>();
    const previousRunId = previousRunRow?.run_id ?? null;

    const insertCurrent = this.db.prepare(
      `INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited)
       SELECT ?, ?,
         ROW_NUMBER() OVER (ORDER BY m.score DESC NULLS LAST, m.instrument_id),
         m.instrument_id, m.score, m.symbol, m.name, m.asset_class, m.metrics_json,
         CASE WHEN NOT EXISTS (
           SELECT 1 FROM screen_matches pm
           WHERE pm.exited = 0 AND pm.instrument_id = m.instrument_id AND pm.run_id = ?
         ) THEN 1 ELSE 0 END,
         0
       FROM (
         SELECT i.instrument_id AS instrument_id, i.symbol AS symbol, i.name AS name, i.asset_class AS asset_class,
           json_extract(i.metric_values_json, '$.momentum_score') AS score,
           (SELECT COALESCE(json_group_array(json(je.value)), '[]') FROM json_each(i.metric_rows_json) AS je
            WHERE json_extract(je.value, '$.metric') IN (SELECT value FROM json_each(?))
               OR substr(json_extract(je.value, '$.metric'), 1, 9) = 'momentum_') AS metrics_json
         FROM instrument_snapshots AS i
         WHERE i.dataset_id = ? AND i.active = 1 AND ${compiled.whereSql}
       ) AS m`,
    ).bind(datasetId, runId, previousRunId, referencedMetricsJson, datasetId, ...compiled.params);

    const insertExited = this.db.prepare(
      `INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited)
       SELECT ?, ?,
         (SELECT COALESCE(MAX(x.ordinal), 0) FROM screen_matches x WHERE x.run_id = ?) + ROW_NUMBER() OVER (ORDER BY pm.instrument_id),
         pm.instrument_id, pm.score, pm.symbol, pm.name, pm.asset_class, pm.metrics_json, 0, 1
       FROM screen_matches pm
       WHERE pm.exited = 0
         AND pm.run_id = ?
         AND NOT EXISTS (SELECT 1 FROM screen_matches cm WHERE cm.run_id = ? AND cm.exited = 0 AND cm.instrument_id = pm.instrument_id)`,
    ).bind(datasetId, runId, runId, previousRunId, runId);

    const insertRun = this.db.prepare(
      `INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, query_version, source, language_version, completed_at)
       VALUES (?, ?, ?, ?, 'complete', (SELECT COUNT(*) FROM screen_matches WHERE run_id = ? AND exited = 0), ?, ?, ?, ?)`,
    ).bind(datasetId, runId, screen.id, effectiveDate, runId, screen.languageVersion, screen.source, screen.languageVersion, completedAt);

    if (this.db.batch) {
      const results = await this.db.batch([insertCurrent, insertExited, insertRun]);
      results.forEach(ensureD1Success);
    } else {
      ensureD1Success(await insertCurrent.run());
      ensureD1Success(await insertExited.run());
      ensureD1Success(await insertRun.run());
    }
    const run = await this.getRun(screen.id, runId);
    if (!run) throw new Error("Run creation failed");
    return run;
  }
  public async instrument(datasetId: string, instrumentId: string): Promise<InstrumentRow | null> { const row = await this.db.prepare("SELECT instrument_id, symbol, name, asset_class, active, metadata_json, metric_rows_json, fundamental_periods_json, corporate_actions_json FROM instrument_snapshots WHERE dataset_id = ? AND instrument_id = ?").bind(datasetId, instrumentId).first<Record<string, unknown>>(); if (!row) return null; const metrics = parseMetricRows(row.metric_rows_json); const instrument: InstrumentRow = { instrumentId: String(row.instrument_id), symbol: row.symbol == null ? null : String(row.symbol), name: row.name == null ? null : String(row.name), assetClass: String(row.asset_class) as AssetClass, active: Boolean(row.active), metadata: parseMetadata(row.metadata_json) as Record<string, string | number | null>, metricRows: metrics, fundamentalPeriods: parsePeriods(row.fundamental_periods_json), corporateActions: parseActions(row.corporate_actions_json) }; return { ...instrument, momentum: buildMomentum(instrument) }; }
  public async chart(datasetId: string, instrumentId: string): Promise<ChartObject | null> { return this.chartStore ? this.chartStore.get(`charts/${datasetId}/${instrumentId}.json.gz`) : null; }
  public async listGlossary(): Promise<readonly GlossaryEntry[]> { return DEFAULT_GLOSSARY_ENTRIES; }
  public async glossary(slug: string): Promise<GlossaryEntry | null> { return DEFAULT_GLOSSARY_ENTRIES.find((entry) => entry.slug === slug) ?? null; }
  private screen(row: Record<string, unknown>): SavedScreen { return { id: String(row.screen_id), name: String(row.name), source: String(row.expression), languageVersion: "v1", createdAt: String(row.created_at), updatedAt: String(row.updated_at) }; }
}

function compareRunsByLatestExecution(left: ScreenRun, right: ScreenRun): number {
  const leftExecution = validExecutionTime(left.completedAt);
  const rightExecution = validExecutionTime(right.completedAt);
  if (leftExecution !== null || rightExecution !== null) {
    if (leftExecution === null) return 1;
    if (rightExecution === null) return -1;
    if (leftExecution !== rightExecution) return rightExecution - leftExecution;
  }
  return right.effectiveDate.localeCompare(left.effectiveDate) || right.id.localeCompare(left.id);
}

function validExecutionTime(value: string | undefined): number | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function buildExplanation(source: string, item: InstrumentRow, ast?: Expression): Explanation {
  const parsed = ast ?? parseQuery(source).value;
  const values = item.metrics ?? Object.fromEntries((item.metricRows ?? []).map((row) => [row.metric, row.value]));
  const predicates = parsed ? collectPredicates(parsed) : [];
  const metrics = [...new Set(predicates.flatMap((predicate) => predicate.metrics))];
  const clauses: ExplanationClause[] = predicates.map((predicate) => {
    const states = predicate.metrics.map((metric) => item.metricRows?.find((candidate) => candidate.metric === metric)?.state ?? (values[metric] == null ? "missing" : "present"));
    // predicate state is derived only from its referenced rows
    const triState = evaluateQuery(predicate.node, values);
    const knownMatch = predicate.negated ? triState === "false" : triState === "true";
    const result = states.includes("not_applicable") ? "Not applicable" : states.includes("missing") ? "Unavailable" : triState === "unknown" ? "Unavailable" : knownMatch ? "Matched" : "Not matched";
    const metric = predicate.metrics[0];
    const row = metric ? item.metricRows?.find((candidate) => candidate.metric === metric) : undefined;
    const fallback = metric ? { value: values[metric] ?? null, state: values[metric] == null ? "missing" as const : "present" as const } : undefined;
    return { clause: `${predicate.context.length ? `${predicate.context.join(" / ")}: ` : ""}${printAst(predicate.node)}`, ...(metric ? { metric } : {}), result, ...(row ? { value: row } : fallback ? { value: fallback } : {}) };
  });
  const momentum = buildMomentum(item);
  return { matched: evaluateQuery(parsed ?? { kind: "number", value: 0, percent: false, span: { start: 0, end: 0 } }, values) === "true", text: `Matched ${source}${predicates.length ? ` · ${predicates.map((predicate) => predicate.context.join("/")).filter(Boolean).join("; ")}` : ""}`, metrics, clauses, momentum };
}
interface Predicate { readonly node: Expression; readonly metrics: readonly string[]; readonly context: readonly string[]; readonly negated: boolean; }
function collectPredicates(node: Expression, context: readonly string[] = [], negated = false): Predicate[] {
  if (node.kind === "binary" && [">", ">=", "<", "<=", "=", "!="].includes(node.operator)) return [{ node, metrics: collectMetricIds(node), context, negated }];
  if (node.kind === "binary" && (node.operator === "and" || node.operator === "or")) return [...collectPredicates(node.left, [...context, node.operator.toUpperCase()], negated), ...collectPredicates(node.right, [...context, node.operator.toUpperCase()], negated)];
  if (node.kind === "unary" && node.operator === "not") return collectPredicates(node.operand, [...context, "NOT"], !negated);
  return [];
}
function collectMetricIds(node: Expression): string[] { if (node.kind === "metric") return [node.id]; if (node.kind === "number") return []; if (node.kind === "unary") return collectMetricIds(node.operand); return [...collectMetricIds(node.left), ...collectMetricIds(node.right)]; }
function buildMomentum(item: InstrumentRow): MomentumBreakdown {
  const labels: Record<string, string> = { weighted_12m_rs_percentile: "Weighted 12-month RS percentile", six_month_performance: "Six-month performance", three_month_performance: "Three-month performance", trend_strength: "Trend strength vs moving averages", proximity_to_52_week_high: "Proximity to 52-week high", volume_confirmation: "Volume confirmation", three_month_return: "Three-month return", six_month_return: "Six-month return", twelve_month_return: "Twelve-month return", category_rank: "Category rank", inverse_volatility: "Inverse volatility", inverse_max_drawdown: "Inverse max drawdown" };
  const score = item.metricRows?.find((row) => row.metric === "momentum_score");
  const rows = (item.metricRows ?? []).filter((row) => row.metric.startsWith("momentum_") && row.metric !== "momentum_score");
  const components = rows.map((row) => { const id = row.metric.slice("momentum_".length); const metadata = parseMetadata((row as MetricRow & { metadata?: unknown }).metadata); const normalized = typeof metadata.normalized === "number" ? metadata.normalized : row.normalizedValue ?? null; const weight = typeof metadata.weight === "number" ? metadata.weight : 0; const contribution = typeof metadata.contribution === "number" ? metadata.contribution : normalized === null ? null : normalized * weight; const unit: MomentumComponent["unit"] = metadata.unit === "percent" || metadata.unit === "count" ? metadata.unit : "ratio"; return { componentId: id, label: labels[id] ?? id, unit, raw: row.value, normalized, weight, contribution }; });
  const scoreMetadata = parseMetadata((score as MetricRow & { metadata?: unknown } | undefined)?.metadata); const cohort = typeof scoreMetadata.cohort === "string" ? scoreMetadata.cohort : item.assetClass === "mutual_fund" ? "mutual_fund:unknown" : item.assetClass; const sourceDate = typeof scoreMetadata.source_date === "string" ? scoreMetadata.source_date : score?.effectiveDate ?? null; const coverage = typeof scoreMetadata.coverage === "number" ? scoreMetadata.coverage : components.length ? components.filter((component) => component.normalized !== null).length / components.length : score?.state === "present" ? 1 : 0; const formulaVersion = score?.formulaVersion ?? "momentum-v2-cohort"; const warning = typeof scoreMetadata.warning === "string" ? scoreMetadata.warning : score?.state === "missing" ? "Momentum score is unavailable for this instrument." : undefined;
  return { components, cohort, formulaVersion, coverage, sourceDate, ...(warning ? { warning } : {}) };
}
function parseMetadata(value: unknown): Record<string, unknown> { if (typeof value === "object" && value !== null && !Array.isArray(value)) return value as Record<string, unknown>; if (typeof value !== "string") return {}; try { const parsed: unknown = JSON.parse(value); return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {}; } catch { return {}; } }
function parseArray(value: unknown): readonly Record<string, unknown>[] { if (typeof value !== "string") return []; try { const parsed: unknown = JSON.parse(value); return Array.isArray(parsed) ? parsed.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null) : []; } catch { return []; } }
function parseMetricRows(value: unknown): MetricRow[] { return parseArray(value).map((metric) => ({ metric: String(metric.metric), value: metric.value == null ? null : Number(metric.value), state: metric.state === "not_applicable" ? "not_applicable" : metric.state === "missing" ? "missing" : "present", effectiveDate: metric.effectiveDate == null ? null : String(metric.effectiveDate), rawValue: metric.rawValue == null ? null : String(metric.rawValue), normalizedValue: metric.normalizedValue == null ? null : Number(metric.normalizedValue), formulaVersion: metric.formulaVersion == null ? null : String(metric.formulaVersion), sourceArtifactId: metric.sourceArtifactId == null ? null : String(metric.sourceArtifactId), metadata: parseMetadata(metric.metadata) })); }
function parsePeriods(value: unknown): FundamentalPeriodRow[] { return parseArray(value).map((period) => ({ periodId: String(period.periodId), periodEnd: String(period.periodEnd), periodType: String(period.periodType), filingId: String(period.filingId), filedAt: String(period.filedAt), metrics: typeof period.metrics === "object" && period.metrics !== null && !Array.isArray(period.metrics) ? period.metrics as Record<string, unknown> : {}, sourceArtifactId: period.sourceArtifactId == null ? null : String(period.sourceArtifactId) })); }
function parseActions(value: unknown): CorporateActionRow[] { return parseArray(value).map((action) => ({ actionId: String(action.actionId), actionDate: String(action.actionDate), actionType: String(action.actionType), numerator: action.numerator == null ? null : Number(action.numerator), denominator: action.denominator == null ? null : Number(action.denominator), metadata: typeof action.metadata === "object" && action.metadata !== null && !Array.isArray(action.metadata) ? action.metadata as Record<string, unknown> : {}, sourceArtifactId: action.sourceArtifactId == null ? null : String(action.sourceArtifactId) })); }

function typecheckScreen(ast: QueryAst): QueryAst | null { const checked = typecheckQuery(ast, DEFAULT_METRIC_CATALOG); return checked.valid ? checked.ast : null; }
function ensureD1Success(result: D1Result): void { if (result.success === false) throw new Error("D1 mutation failed"); }
