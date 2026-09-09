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
export interface RunDetail extends ScreenRun { readonly matches: readonly (ScreenMatch & { readonly explanation: Explanation; readonly momentum?: MomentumBreakdown; readonly entered: boolean; readonly exited: boolean })[]; }
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
  updateScreen(input: { id: string; name: string; source: string; updatedAt: string }): Promise<SavedScreen>;
  listRuns(screenId: string): Promise<RunDetail[]>;
  runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string): Promise<RunDetail>;
  instrument(datasetId: string, instrumentId: string): Promise<InstrumentRow | null>;
  chart(datasetId: string, instrumentId: string): Promise<ChartObject | null>;
  listGlossary(): Promise<readonly GlossaryEntry[]>;
  glossary(slug: string): Promise<GlossaryEntry | null>;
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
  public async updateScreen(input: { id: string; name: string; source: string; updatedAt: string }): Promise<SavedScreen> {
    const existing = this.screens.get(input.id);
    if (!existing) throw new Error("Screen not found");
    const screen: SavedScreen = { ...existing, name: input.name, source: input.source, updatedAt: input.updatedAt };
    this.screens.set(input.id, screen);
    return screen;
  }
  public async listRuns(screenId: string): Promise<RunDetail[]> {
    return [...this.runs.entries()]
      .filter(([key]) => key.endsWith(`:${screenId}`))
      .flatMap(([, runs]) => runs)
      .sort((left, right) => right.effectiveDate.localeCompare(left.effectiveDate) || (right.completedAt ?? "").localeCompare(left.completedAt ?? "") || right.id.localeCompare(left.id));
  }
  public async runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string): Promise<RunDetail> {
    const parsed = parseQuery(screen.source);
    if (!parsed.value || parsed.diagnostics.length > 0) throw new Error("Invalid saved screen");
    const matches = [...this.instruments.values()].filter((item) => { const metrics = item.metrics ?? Object.fromEntries((item.metricRows ?? []).map((row) => [row.metric, row.value])); return item.active && evaluateQuery(parsed.value!, metrics) === "true"; }).sort((left, right) => (right.metricRows?.find((row) => row.metric === "momentum_score")?.value ?? right.metrics?.momentum_score ?? -Infinity) - (left.metricRows?.find((row) => row.metric === "momentum_score")?.value ?? left.metrics?.momentum_score ?? -Infinity) || left.instrumentId.localeCompare(right.instrumentId));
    const previous = (await this.listRuns(screen.id)).find((run) => run.status === "complete");
    const previousIds = new Set(previous?.matches.filter((match) => !match.exited).map((match) => match.instrumentId) ?? []);
    const currentIds = new Set(matches.map((match) => match.instrumentId));
    const currentMatches: RunMatch[] = matches.map((item, index) => { const explanation = buildExplanation(screen.source, item, parsed.value!); const score = item.metricRows?.find((row) => row.metric === "momentum_score")?.value ?? item.metrics?.momentum_score ?? null; return { instrumentId: item.instrumentId, rank: index + 1, score, explanation, ...(explanation.momentum ? { momentum: explanation.momentum } : {}), entered: !previousIds.has(item.instrumentId), exited: false }; });
    const exitedMatches: RunMatch[] = previous?.matches.filter((match) => !match.exited && !currentIds.has(match.instrumentId)).map((match) => ({ ...match, rank: 0, explanation: { ...match.explanation, matched: false }, entered: false, exited: true })) ?? [];
    const detail: RunDetail = { id: crypto.randomUUID(), screenId: screen.id, datasetId, effectiveDate, completedAt: new Date().toISOString(), matchCount: matches.length, status: "complete", matches: [...currentMatches, ...exitedMatches] };
    this.runs.set(`${datasetId}:${screen.id}`, [detail, ...(this.runs.get(`${datasetId}:${screen.id}`) ?? [])]);
    return detail;
  }
  public async instrument(_datasetId: string, instrumentId: string): Promise<InstrumentRow | null> { const instrument = this.instruments.get(instrumentId); return instrument ? { ...instrument, momentum: instrument.momentum ?? buildMomentum(instrument) } : null; }
  public async chart(_datasetId: string, instrumentId: string): Promise<ChartObject | null> { return this.charts.get(instrumentId) ?? null; }
  public async listGlossary(): Promise<readonly GlossaryEntry[]> { return DEFAULT_GLOSSARY_ENTRIES; }
  public async glossary(slug: string): Promise<GlossaryEntry | null> { return DEFAULT_GLOSSARY_ENTRIES.find((entry) => entry.slug === slug) ?? null; }
}

export interface D1Result { readonly results?: readonly Record<string, unknown>[]; readonly success?: boolean; readonly meta?: Record<string, unknown>; }
export interface D1Statement { bind(...values: unknown[]): D1Statement; first<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<T | null>; all<T extends Record<string, unknown> = Record<string, unknown>>(): Promise<{ results: readonly T[] }>; run(): Promise<D1Result>; }
export interface D1Database { prepare(sql: string): D1Statement; batch?(statements: readonly D1Statement[]): Promise<readonly D1Result[]>; }

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
  public async listRuns(screenId: string): Promise<RunDetail[]> { const result = await this.db.prepare("SELECT run_id, screen_id, dataset_id, effective_date, completed_at, result_count, status FROM screen_runs WHERE screen_id = ? ORDER BY effective_date DESC, completed_at DESC, run_id DESC").bind(screenId).all<Record<string, unknown>>(); return Promise.all(result.results.map(async (row) => { const matches = await this.db.prepare("SELECT instrument_id, ordinal, score, explanation_json, entered, exited FROM screen_matches WHERE dataset_id = ? AND run_id = ? ORDER BY ordinal").bind(String(row.dataset_id), String(row.run_id)).all<Record<string, unknown>>(); return { id: String(row.run_id), screenId: String(row.screen_id), datasetId: String(row.dataset_id), effectiveDate: String(row.effective_date), ...(row.completed_at ? { completedAt: String(row.completed_at) } : {}), matchCount: Number(row.result_count), status: row.status === "failed" ? "failed" : "complete", matches: matches.results.map((match) => ({ instrumentId: String(match.instrument_id), rank: match.exited ? 0 : Number(match.ordinal), score: match.score == null ? null : Number(match.score), explanation: parseExplanation(match.explanation_json), entered: Boolean(match.entered), exited: Boolean(match.exited) })) }; }));
  }
  public async runScreen(datasetId: string, screen: SavedScreen, effectiveDate: string): Promise<RunDetail> {
    const parsed = parseQuery(screen.source);
    if (!parsed.value || parsed.diagnostics.length > 0) throw new Error("Invalid saved screen");
    const checked = typecheckScreen(parsed.value);
    if (!checked) throw new Error("Invalid saved screen");
    const compiled = compileQuery(checked, DEFAULT_METRIC_CATALOG, { relation: "snapshot", includeDatasetFilter: false });
    const rows = await this.db.prepare(`SELECT i.instrument_id, json_extract(i.metric_values_json, '$.momentum_score') AS score FROM instrument_snapshots AS i WHERE i.dataset_id = ? AND i.active = 1 AND ${compiled.whereSql} ORDER BY score DESC NULLS LAST, i.instrument_id`).bind(datasetId, ...compiled.params).all<{ instrument_id: string; score: number | null }>();
    const prior = (await this.listRuns(screen.id)).find((run) => run.status === "complete");
    const priorIds = new Set(prior?.matches.filter((match) => !match.exited).map((match) => match.instrumentId) ?? []);
    const currentIds = new Set(rows.results.map((row) => row.instrument_id));
    const currentMatches: RunMatch[] = await Promise.all(rows.results.map(async (row, index) => { const instrument = await this.instrument(datasetId, row.instrument_id); const explanation = instrument ? buildExplanation(screen.source, instrument, checked) : { matched: true, text: `Matched ${screen.source}`, metrics: compiled.referencedMetricIds, clauses: [{ clause: screen.source, result: "Matched" as const }] }; const score = row.score == null ? instrument?.metricRows?.find((metric) => metric.metric === "momentum_score")?.value ?? null : Number(row.score); return { instrumentId: row.instrument_id, rank: index + 1, score, explanation, ...(explanation.momentum ? { momentum: explanation.momentum } : {}), entered: !priorIds.has(row.instrument_id), exited: false }; }));
    const exitedMatches: RunMatch[] = prior?.matches.filter((match) => !match.exited && !currentIds.has(match.instrumentId)).map((match) => ({ ...match, rank: 0, explanation: { ...match.explanation, matched: false }, entered: false, exited: true })) ?? [];
    const matches: RunMatch[] = [...currentMatches, ...exitedMatches];
    const runId = crypto.randomUUID();
    const matchStatements = matches.map((match, index) => this.db.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, explanation_json, entered, exited) VALUES (?, ?, ?, ?, ?, ?, ?, ?)").bind(datasetId, runId, index + 1, match.instrumentId, match.score, JSON.stringify(match.explanation), match.entered ? 1 : 0, match.exited ? 1 : 0));
    const completedAt = new Date().toISOString();
    const runStatement = this.db.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, query_version, completed_at) VALUES (?, ?, ?, ?, 'complete', ?, 'v1', ?)").bind(datasetId, runId, screen.id, effectiveDate, rows.results.length, completedAt);
    if (this.db.batch) {
      const results = await this.db.batch([...matchStatements, runStatement]);
      results.forEach(ensureD1Success);
    } else {
      for (const statement of matchStatements) ensureD1Success(await statement.run());
      ensureD1Success(await runStatement.run());
    }
    return { id: runId, screenId: screen.id, datasetId, effectiveDate, completedAt, matchCount: rows.results.length, status: "complete", matches };
  }
  public async instrument(datasetId: string, instrumentId: string): Promise<InstrumentRow | null> { const row = await this.db.prepare("SELECT instrument_id, symbol, name, asset_class, active, metadata_json, metric_rows_json, fundamental_periods_json, corporate_actions_json FROM instrument_snapshots WHERE dataset_id = ? AND instrument_id = ?").bind(datasetId, instrumentId).first<Record<string, unknown>>(); if (!row) return null; const metrics = parseMetricRows(row.metric_rows_json); const instrument: InstrumentRow = { instrumentId: String(row.instrument_id), symbol: row.symbol == null ? null : String(row.symbol), name: row.name == null ? null : String(row.name), assetClass: String(row.asset_class) as AssetClass, active: Boolean(row.active), metadata: parseMetadata(row.metadata_json) as Record<string, string | number | null>, metricRows: metrics, fundamentalPeriods: parsePeriods(row.fundamental_periods_json), corporateActions: parseActions(row.corporate_actions_json) }; return { ...instrument, momentum: buildMomentum(instrument) }; }
  public async chart(datasetId: string, instrumentId: string): Promise<ChartObject | null> { return this.chartStore ? this.chartStore.get(`charts/${datasetId}/${instrumentId}.json.gz`) : null; }
  public async listGlossary(): Promise<readonly GlossaryEntry[]> { return DEFAULT_GLOSSARY_ENTRIES; }
  public async glossary(slug: string): Promise<GlossaryEntry | null> { return DEFAULT_GLOSSARY_ENTRIES.find((entry) => entry.slug === slug) ?? null; }
  private screen(row: Record<string, unknown>): SavedScreen { return { id: String(row.screen_id), name: String(row.name), source: String(row.expression), languageVersion: "v1", createdAt: String(row.created_at), updatedAt: String(row.updated_at) }; }
}

function parseExplanation(value: unknown): Explanation { if (typeof value !== "string") return { matched: true, text: "", metrics: [] }; try { const parsed: unknown = JSON.parse(value); if (typeof parsed === "object" && parsed !== null && "matched" in parsed && "text" in parsed && "metrics" in parsed && Array.isArray(parsed.metrics)) { const record = parsed as Record<string, unknown>; const metrics = record.metrics as readonly unknown[]; return { matched: Boolean(record.matched), text: String(record.text), metrics: metrics.map(String), ...(Array.isArray(record.clauses) ? { clauses: record.clauses as ExplanationClause[] } : {}), ...(typeof record.momentum === "object" && record.momentum !== null ? { momentum: record.momentum as MomentumBreakdown } : {}) }; } } catch { /* corrupted explanation is represented, not executed */ } return { matched: true, text: "", metrics: [] }; }
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
