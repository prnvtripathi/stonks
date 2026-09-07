import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { MetricDefinition, SavedScreen } from "@stonks/contracts";
import { DEFAULT_METRIC_CATALOG } from "@stonks/contracts";
import { parseQuery, typecheckQuery } from "@stonks/query";
import { createApiClient, type DashboardApi, type ScreenRunSummary, type StatusDto } from "./api";
import "./styles.css";

export type { DashboardApi } from "./api";

interface AppProps { readonly api?: DashboardApi; }
type View = "overview" | "editor";
type EditorMode = "new" | "edit" | "duplicate";

const fallbackStatus: StatusDto = { effectiveDate: null, datasetId: null, sources: [] };
const formatDate = (value: string | null | undefined): string => value ? new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(`${value}T00:00:00Z`)).replace("Sept", "Sep") : "—";
const sourceLabel = (source: string): string => source.toUpperCase() === "AMFI NAV" ? "AMFI NAV" : source;
const statusText = (source: StatusDto["sources"][number]): string => source.stale || source.status.toLowerCase() === "delayed" ? "delayed" : source.status.toLowerCase();

function metricUnit(metric: MetricDefinition): string {
  return metric.unit === "currency" ? "₹ crore" : metric.unit === "percent" ? "%" : metric.unit;
}

export function App({ api = createApiClient() }: AppProps) {
  const [view, setView] = useState<View>("overview");
  const [status, setStatus] = useState<StatusDto>(fallbackStatus);
  const [metrics, setMetrics] = useState<readonly MetricDefinition[]>(DEFAULT_METRIC_CATALOG);
  const [screens, setScreens] = useState<readonly SavedScreen[]>([]);
  const [runs, setRuns] = useState<Readonly<Record<string, ScreenRunSummary | undefined>>>({});
  const [editorMode, setEditorMode] = useState<EditorMode>("new");
  const [editing, setEditing] = useState<SavedScreen | undefined>(undefined);
  const [dirty, setDirty] = useState(false);
  const [showGuard, setShowGuard] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    Promise.all([api.getStatus(), api.getMetrics(), api.getScreens()]).then(([nextStatus, nextMetrics, nextScreens]) => {
      if (!active) return;
      setStatus(nextStatus); setMetrics(nextMetrics); setScreens(nextScreens.data); setLoading(false);
    }).catch(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api]);
  useEffect(() => {
    if (!screens.length || !api.getRuns) return;
    let active = true;
    Promise.all(screens.map(async (screen) => { const result = await api.getRuns!(screen.id); return [screen.id, result.runs[0]] as const; })).then((entries) => {
      if (active) setRuns((current) => ({ ...current, ...Object.fromEntries(entries) }));
    }).catch(() => undefined);
    return () => { active = false; };
  }, [api, screens]);

  const openOverview = () => { if (dirty) setShowGuard(true); else setView("overview"); };
  const openEditor = (mode: EditorMode, screen?: SavedScreen) => { setEditorMode(mode); setEditing(screen); setDirty(false); setView("editor"); };
  const confirmLeave = () => { setShowGuard(false); setDirty(false); setView("overview"); };
  const onSaved = (screen: SavedScreen) => { setScreens((current) => [screen, ...current.filter((item) => item.id !== screen.id)]); setEditing(screen); setDirty(false); };
  const onRun = async (screen: SavedScreen) => { const run = await api.runScreen(screen.id); setRuns((current) => ({ ...current, [screen.id]: run })); };

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="site-header">
      <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">S</span><span><strong>STONKS</strong><small>INDIA RESEARCH</small></span></div>
      <nav aria-label="Primary navigation" className="primary-nav">
        <button className={view === "overview" ? "nav-link active" : "nav-link"} onClick={openOverview}>Overview</button>
        <button className={view === "editor" ? "nav-link active" : "nav-link"} onClick={() => openEditor("new")}>Screens</button>
        <a className="nav-link" href="#learn">Learn</a>
      </nav>
      <div className="private-badge"><span className="status-dot" aria-hidden="true" />Private workspace</div>
    </header>

    <main id="main-content" tabIndex={-1}>
      <div className="page-frame">
        {view === "overview" ? <Overview status={status} screens={screens} runs={runs} loading={loading} onNew={() => openEditor("new")} onEdit={(screen) => openEditor("edit", screen)} onDuplicate={(screen) => openEditor("duplicate", screen)} onRun={onRun} /> : <ScreenEditor {...(editing ? { initialScreen: editing } : {})} metrics={metrics} mode={editorMode} api={api} onDirtyChange={setDirty} onSaved={onSaved} onRun={onRun} onBack={openOverview} />}
      </div>
    </main>
    <Disclosure effectiveDate={status.effectiveDate} />
    {showGuard ? <UnsavedChangesDialog onStay={() => setShowGuard(false)} onLeave={confirmLeave} /> : null}
  </div>;
}

interface OverviewProps { readonly status?: StatusDto; readonly screens?: readonly SavedScreen[]; readonly runs?: Readonly<Record<string, ScreenRunSummary | undefined>>; readonly loading?: boolean; readonly onNew?: () => void; readonly onEdit?: (screen: SavedScreen) => void; readonly onDuplicate?: (screen: SavedScreen) => void; readonly onRun?: (screen: SavedScreen) => Promise<void>; }
export function Overview({ status = fallbackStatus, screens = [], runs = {}, loading = false, onNew = () => undefined, onEdit = () => undefined, onDuplicate = () => undefined, onRun = async () => undefined }: OverviewProps) {
  const totalMatches = Object.values(runs).reduce((sum, run) => sum + (run?.matchCount ?? 0), 0);
  return <>
    <section className="hero-row" aria-labelledby="overview-title">
      <div><p className="eyebrow">MARKET PULSE / EOD</p><h1 id="overview-title">Research overview</h1><p className="lede">A focused view of momentum across actively traded NSE equities, ETFs, and mutual funds.</p></div>
      <button className="button button-primary" onClick={onNew}><span aria-hidden="true">+</span> New screen</button>
    </section>
    <section className="context-strip" aria-label="Dataset context">
      <div><span className="context-label">Data through</span><strong> {formatDate(status.effectiveDate)}</strong></div>
      <div><span className="context-label">Universe</span><strong>NSE EQ · ETFs · AMFI</strong></div>
      <div><span className="context-label">Refresh cadence</span><strong>End of day</strong></div>
    </section>
    <section className="section-block" aria-labelledby="health-title"><div className="section-heading"><div><p className="eyebrow">LINEAGE</p><h2 id="health-title">Source health</h2></div><span className="section-note">Independent freshness by source</span></div><div className="health-grid">{status.sources.length ? status.sources.map((source) => <SourceCard key={source.sourceId} source={source} />) : <div className="empty-card">No source status available yet.</div>}</div></section>
    <section className="section-block" aria-labelledby="screens-title"><div className="section-heading"><div><p className="eyebrow">MECHANICAL FILTERS</p><h2 id="screens-title">Saved screens</h2></div><span className="result-summary">{screens.length} saved · {totalMatches} top matches</span></div>{loading ? <div className="loading-card" aria-live="polite">Loading your research workspace…</div> : screens.length ? <div className="screen-grid">{screens.map((screen) => <ScreenCard key={screen.id} screen={screen} run={runs[screen.id]} onEdit={onEdit} onDuplicate={onDuplicate} onRun={onRun} />)}</div> : <div className="empty-card"><h3>Start with a question</h3><p>Build a Screener-style query to find the strongest candidates in your universe.</p><button className="button button-secondary" onClick={onNew}>Create your first screen</button></div>}</section>
  </>;
}

function SourceCard({ source }: { readonly source: StatusDto["sources"][number] }) {
  const state = statusText(source); const healthy = state === "complete";
  return <article className="source-card"><div className="card-topline"><span className={healthy ? "source-state healthy" : "source-state delayed"}><span className="status-dot" aria-hidden="true" />{sourceLabel(source.sourceId)} {state}</span><span className="source-kind">OFFICIAL</span></div><dl className="date-list"><div><dt>Expected</dt><dd>{formatDate(source.expectedDate)}</dd></div><div><dt>Loaded</dt><dd>{formatDate(source.loadedDate)}</dd></div></dl>{source.coverage !== undefined && source.coverage !== null ? <div className="coverage"><span>Coverage</span><strong>{Math.round(source.coverage * 100)}%</strong></div> : null}</article>;
}

interface ScreenCardProps { readonly screen: SavedScreen; readonly run: ScreenRunSummary | undefined; readonly onEdit: (screen: SavedScreen) => void; readonly onDuplicate: (screen: SavedScreen) => void; readonly onRun: (screen: SavedScreen) => Promise<void>; }
function ScreenCard({ screen, run, onEdit, onDuplicate, onRun }: ScreenCardProps) {
  const [running, setRunning] = useState(false);
  const runNow = async () => { setRunning(true); try { await onRun(screen); } finally { setRunning(false); } };
  const entries = run?.matches?.filter((match) => match.entered).length ?? 0;
  const exits = run?.matches?.filter((match) => match.exited).length ?? 0;
  return <article className="screen-card"><div className="screen-card-head"><div><span className="screen-label">SAVED SCREEN</span><h3>{screen.name}</h3></div><button className="icon-button" aria-label={`Edit ${screen.name}`} onClick={() => onEdit(screen)}>Edit</button></div><code className="query-preview">{screen.source}</code><div className="screen-card-meta"><div><span className="context-label">Last run</span><strong>{run ? formatDate(run.effectiveDate) : "Not run"}</strong></div><div><span className="context-label">Top matches</span><strong>{run?.matchCount ?? "—"}</strong></div><div><span className="context-label">Since prior run</span><strong className={entries || exits ? "text-positive" : ""}>{run ? `+${entries} / −${exits}` : "—"}</strong></div></div><div className="card-actions"><button className="button button-primary small" onClick={runNow} disabled={running}>{running ? "Running…" : "Run screen"}</button><button className="button button-quiet small" onClick={() => onDuplicate(screen)}>Duplicate</button></div></article>;
}

interface EditorProps { readonly metrics?: readonly MetricDefinition[]; readonly initialScreen?: SavedScreen; readonly initialValue?: string; readonly mode?: EditorMode; readonly api?: DashboardApi; readonly onDirtyChange?: (dirty: boolean) => void; readonly onSaved?: (screen: SavedScreen) => void; readonly onRun?: (screen: SavedScreen) => Promise<void>; readonly onBack?: () => void; }
export function ScreenEditor({ metrics = DEFAULT_METRIC_CATALOG, initialScreen, initialValue, mode = "new", api = createApiClient(), onDirtyChange = () => undefined, onSaved = () => undefined, onRun = async () => undefined, onBack = () => undefined }: EditorProps) {
  const initialName = initialScreen && mode !== "duplicate" ? initialScreen.name : initialScreen ? `${initialScreen.name} copy` : "";
  const startingSource = initialValue ?? initialScreen?.source ?? "";
  const [name, setName] = useState(initialName); const [source, setSource] = useState(startingSource); const [diagnostic, setDiagnostic] = useState<string | null>(null); const [validating, setValidating] = useState(false); const [suggestionsOpen, setSuggestionsOpen] = useState(Boolean(initialValue)); const [selectedSuggestion, setSelectedSuggestion] = useState(0); const [saved, setSaved] = useState<SavedScreen | undefined>(initialScreen && mode === "edit" ? initialScreen : undefined); const [savedName, setSavedName] = useState(mode === "edit" ? initialName : ""); const [savedSource, setSavedSource] = useState(mode === "edit" ? (initialScreen?.source ?? "") : ""); const editorRef = useRef<HTMLTextAreaElement>(null);
  const suggestions = useMemo(() => { const query = source.trim().toLocaleLowerCase("en-IN"); if (!query || !metrics.length) return []; return metrics.filter((metric) => metric.label.toLocaleLowerCase("en-IN").includes(query) || metric.aliases.some((alias) => alias.toLocaleLowerCase("en-IN").includes(query))).slice(0, 6); }, [metrics, source]);
  useEffect(() => { onDirtyChange(name !== savedName || source !== savedSource); }, [name, source, savedName, savedSource, onDirtyChange]);
  useEffect(() => { setValidating(true); const timer = window.setTimeout(() => { if (!source.trim()) { setDiagnostic(null); setValidating(false); return; } const parsed = parseQuery(source); if (!parsed.value || parsed.diagnostics.length) setDiagnostic(parsed.diagnostics[0]?.message ?? "Add a complete condition to validate this query."); else { const checked = typecheckQuery(parsed.value, metrics); setDiagnostic(checked.valid ? "Query looks valid for the selected universe." : checked.diagnostics[0]?.message ?? "Review this query."); } setValidating(false); }, 350); return () => window.clearTimeout(timer); }, [source, metrics]);
  const selectSuggestion = (metric: MetricDefinition) => { setSource(metric.label); setSuggestionsOpen(false); setSelectedSuggestion(0); editorRef.current?.focus(); };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (!suggestionsOpen || !suggestions.length) return; if (event.key === "ArrowDown") { event.preventDefault(); setSelectedSuggestion((value) => (value + 1) % suggestions.length); } else if (event.key === "ArrowUp") { event.preventDefault(); setSelectedSuggestion((value) => (value - 1 + suggestions.length) % suggestions.length); } else if (event.key === "Enter") { event.preventDefault(); const suggestion = suggestions[selectedSuggestion]; if (suggestion) selectSuggestion(suggestion); } else if (event.key === "Escape") setSuggestionsOpen(false); };
  const save = async () => { if (!name.trim() || !source.trim()) { setDiagnostic("Give this screen a name and query before saving."); return; } const parsed = parseQuery(source); if (!parsed.value || parsed.diagnostics.length) { setDiagnostic(parsed.diagnostics[0]?.message ?? "Review this query."); return; } const checked = typecheckQuery(parsed.value, metrics); if (!checked.valid) { setDiagnostic(checked.diagnostics[0]?.message ?? "Review this query."); return; } const next = await api.createScreen({ name: name.trim(), source: source.trim() }); setSaved(next); setSavedName(next.name); setSavedSource(next.source); onSaved(next); setDiagnostic("Saved privately."); };
  return <section className="editor-view" aria-labelledby="editor-title"><div className="editor-header"><div><button className="back-link" onClick={onBack}>← Overview</button><p className="eyebrow">SCREEN BUILDER / {mode.toUpperCase()}</p><h1 id="editor-title">{mode === "duplicate" ? "Duplicate screen" : mode === "edit" ? "Edit screen" : "New screen"}</h1></div><div className="editor-actions"><button className="button button-quiet" onClick={onBack}>Cancel</button><button className="button button-primary" onClick={() => void save()}>{saved ? "Save changes" : "Save screen"}</button></div></div><div className="editor-layout"><div className="editor-main"><label className="field-label" htmlFor="screen-name">Screen name</label><input id="screen-name" className="text-input" value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Breakout with volume" /><div className="field-header"><label className="field-label" htmlFor="screen-query">Screen query</label><span className="field-hint">AND / OR conditions · percentages use %</span></div><div className="autocomplete-wrap"><textarea id="screen-query" ref={editorRef} className="query-editor" aria-describedby="query-help query-status" aria-autocomplete="list" aria-controls="metric-suggestions" value={source} onChange={(event) => { setSource(event.target.value); setSuggestionsOpen(true); }} onFocus={() => setSuggestionsOpen(source.length > 0)} onKeyDown={onKeyDown} rows={7} placeholder={'Return over 1day > 3% AND\nVolume > Volume 1week average * 1.5'} />{suggestionsOpen && suggestions.length ? <div className="suggestion-popover" id="metric-suggestions" role="listbox" aria-label="Metric suggestions">{suggestions.map((metric, index) => <button role="option" aria-label={metric.label} aria-selected={index === selectedSuggestion} className={index === selectedSuggestion ? "suggestion selected" : "suggestion"} key={metric.id} onMouseDown={(event) => event.preventDefault()} onClick={() => selectSuggestion(metric)}><span aria-hidden="true">{metric.label}</span><small aria-hidden="true">{metricUnit(metric)} · {metric.assetClasses.join(" / ")}</small></button>)}</div> : null}</div><p id="query-help" className="field-hint">Start typing a metric to see known fields, units, and applicability.</p><div id="query-status" role="status" aria-live="polite" className={diagnostic?.toLowerCase().includes("valid") || diagnostic?.toLowerCase().includes("saved") ? "validation success" : diagnostic ? "validation error" : "validation"}>{validating ? "Checking query…" : diagnostic ?? "Your query is private to this workspace."}</div><div className="editor-footer"><button className="button button-secondary" disabled={!saved} onClick={() => saved && void onRun(saved)}>Run saved screen</button><span className="save-note">{saved ? `Saved ${formatDate(saved.updatedAt)}` : "Not saved yet"}</span></div></div><aside className="metric-browser" aria-labelledby="catalog-title"><div className="catalog-heading"><p className="eyebrow">REFERENCE</p><h2 id="catalog-title">Metric catalog</h2></div><p className="catalog-copy">Every field shows its unit and supported universe.</p><div className="metric-list">{metrics.slice(0, 12).map((metric) => <button className="metric-row" key={metric.id} onClick={() => selectSuggestion(metric)}><span>{metric.label}</span><small>{metricUnit(metric)} · {metric.assetClasses.includes("mutual_fund") ? "All assets" : metric.assetClasses.join(" / ")}</small></button>)}</div></aside></div></section>;
}

function Disclosure({ effectiveDate }: { readonly effectiveDate: string | null }) { return <footer className="disclosure"><div><span className="disclosure-mark" aria-hidden="true">i</span><span><strong>Research tool, not investment advice.</strong> Mechanical matches do not guarantee returns. Review source dates before acting.</span></div><span>Effective date: {formatDate(effectiveDate)}</span></footer>; }
function UnsavedChangesDialog({ onStay, onLeave }: { readonly onStay: () => void; readonly onLeave: () => void }) { return <div className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="unsaved-title"><h2 id="unsaved-title">Leave without saving?</h2><p>Your screen changes are still local and have not been saved.</p><div className="modal-actions"><button className="button button-quiet" onClick={onStay}>Stay</button><button className="button button-primary" onClick={onLeave}>Leave</button></div></div></div>; }
