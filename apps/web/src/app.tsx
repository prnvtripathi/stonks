import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { MetricDefinition, SavedScreen } from "@stonks/contracts";
import { DEFAULT_GLOSSARY_ENTRIES, DEFAULT_METRIC_CATALOG } from "@stonks/contracts";
import { parseQuery, typecheckQuery } from "@stonks/query";
import { createApiClient, type DashboardApi, type ScreenRunSummary, type StatusDto } from "./api";
import { InstrumentView, ScreenResultsView } from "./research";
import { LearnView } from "./learn";
import "./styles.css";

export type { DashboardApi } from "./api";

interface AppProps { readonly api?: DashboardApi; }
type View = "overview" | "editor" | "results" | "instrument" | "learn";
type EditorMode = "new" | "edit" | "duplicate";

const fallbackStatus: StatusDto = { effectiveDate: null, datasetId: null, sources: [] };
const formatDate = (value: string | null | undefined): string => value ? new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(`${value}T00:00:00Z`)).replace("Sept", "Sep") : "—";
const sourceLabel = (source: string): string => source.toUpperCase() === "AMFI NAV" ? "AMFI NAV" : source;
const statusText = (source: StatusDto["sources"][number]): string => source.stale || source.status.toLowerCase() === "delayed" ? "delayed" : source.status.toLowerCase();

function metricUnit(metric: MetricDefinition): string {
  return metric.unit === "currency" ? "₹ crore" : metric.unit === "percent" ? "%" : metric.unit;
}

function metricFragment(value: string, caret: number): { readonly start: number; readonly end: number; readonly text: string } {
  const safeCaret = Math.max(0, Math.min(caret, value.length));
  const before = value.slice(0, safeCaret);
  const match = before.match(/(?:^|[><=()+*/%,-]|\bAND\b|\bOR\b)\s*([A-Za-z][A-Za-z0-9 _-]*)$/i);
  if (!match || match[1] === undefined) return { start: safeCaret, end: safeCaret, text: "" };
  return { start: safeCaret - match[1].length, end: safeCaret, text: match[1] };
}

export function App({ api }: AppProps) {
  const defaultApiRef = useRef<DashboardApi | undefined>(undefined);
  if (!defaultApiRef.current) defaultApiRef.current = createApiClient();
  const resolvedApi = api ?? defaultApiRef.current;
  const initialPath = typeof window === "undefined" ? "/" : window.location.pathname;
  const initialParts = initialPath.split("/").filter(Boolean);
  const [view, setView] = useState<View>(initialParts[0] === "instruments" ? "instrument" : initialParts[0] === "screens" ? (initialParts[1] === "new" || initialParts[2] === "edit" ? "editor" : "results") : initialParts[0] === "learn" ? "learn" : "overview");
  const [routeId, setRouteId] = useState(initialParts[1] ?? "");
  const [originScreenId, setOriginScreenId] = useState(() => typeof window === "undefined" ? "" : new URLSearchParams(window.location.search).get("fromScreen") ?? "");
  const [status, setStatus] = useState<StatusDto>(fallbackStatus);
  const [metrics, setMetrics] = useState<readonly MetricDefinition[]>(DEFAULT_METRIC_CATALOG);
  const [screens, setScreens] = useState<readonly SavedScreen[]>([]);
  const [runs, setRuns] = useState<Readonly<Record<string, ScreenRunSummary | undefined>>>({});
  const [editorMode, setEditorMode] = useState<EditorMode>("new");
  const [editing, setEditing] = useState<SavedScreen | undefined>(undefined);
  const [dirty, setDirty] = useState(false);
  const [showGuard, setShowGuard] = useState(false);
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const loadWorkspace = useCallback(async () => {
    setLoading(true); setLoadError(null);
    try {
      const [nextStatus, nextMetrics, nextScreens] = await Promise.all([resolvedApi.getStatus(), resolvedApi.getMetrics(), resolvedApi.getScreens()]);
      setStatus(nextStatus); setMetrics(nextMetrics); setScreens(nextScreens.data);
    } catch {
      setLoadError("Unable to load the research workspace. Check your connection and retry.");
    } finally { setLoading(false); }
  }, [resolvedApi]);
  useEffect(() => { void loadWorkspace(); }, [loadWorkspace]);
  useEffect(() => {
    if (view !== "editor" || initialParts[2] !== "edit" || !routeId || editing) return;
    const screen = screens.find((candidate) => candidate.id === routeId);
    if (screen) { setEditing(screen); setEditorMode("edit"); }
  }, [editing, initialParts, routeId, screens, view]);
  useEffect(() => {
    if (!screens.length || !resolvedApi.getRuns) { setHistoryLoading(false); return; }
    let active = true;
    setHistoryLoading(true);
    setHistoryError(null);
    Promise.all(screens.map(async (screen) => { const result = await resolvedApi.getRuns!(screen.id); return [screen.id, result.runs[0]] as const; })).then((entries) => {
      if (active) setRuns((current) => ({ ...current, ...Object.fromEntries(entries) }));
    }).catch(() => { if (active) setHistoryError("Unable to load one or more screen histories."); }).finally(() => { if (active) setHistoryLoading(false); });
    return () => { active = false; };
  }, [resolvedApi, screens]);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const navigate = useCallback((path: string, nextView: View, id = "") => { window.history.pushState({}, "", path); setView(nextView); setRouteId(id); }, []);
  useEffect(() => { const onPopState = () => { const parts = window.location.pathname.split("/").filter(Boolean); setRouteId(parts[1] ?? ""); setOriginScreenId(new URLSearchParams(window.location.search).get("fromScreen") ?? ""); setView(parts[0] === "instruments" ? "instrument" : parts[0] === "screens" && parts[1] && parts[1] !== "new" && parts[2] !== "edit" ? "results" : parts[0] === "screens" ? "editor" : parts[0] === "learn" ? "learn" : "overview"); }; window.addEventListener("popstate", onPopState); return () => window.removeEventListener("popstate", onPopState); }, []);
  const openOverview = () => { if (dirty) setShowGuard(true); else navigate("/", "overview"); };
  const openEditor = (mode: EditorMode, screen?: SavedScreen) => { setEditorMode(mode); setEditing(screen); setDirty(false); navigate(screen && mode === "edit" ? `/screens/${encodeURIComponent(screen.id)}/edit` : "/screens/new", "editor", screen?.id ?? ""); };
  const confirmLeave = () => { setShowGuard(false); setDirty(false); navigate("/", "overview"); };
  const onSaved = (screen: SavedScreen) => { setScreens((current) => [screen, ...current.filter((item) => item.id !== screen.id)]); setEditing(screen); setDirty(false); };
  const onRun = async (screen: SavedScreen) => {
    try { const run = await resolvedApi.runScreen(screen.id); setRuns((current) => ({ ...current, [screen.id]: run })); setActionError(null); }
    catch { setActionError(`Unable to run ${screen.name}. Your saved screen is unchanged.`); }
  };

  const resolvedScreenId = screens.find((screen) => screen.id === routeId || screen.name.toLocaleLowerCase("en-IN").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") === routeId)?.id ?? routeId;
  const openInstrument = (instrumentId: string) => { const source = view === "results" ? resolvedScreenId : ""; setOriginScreenId(source); const query = source ? `?fromScreen=${encodeURIComponent(source)}` : ""; navigate(`/instruments/${encodeURIComponent(instrumentId)}${query}`, "instrument", instrumentId); };
  const backFromInstrument = () => originScreenId ? navigate(`/screens/${encodeURIComponent(originScreenId)}`, "results", originScreenId) : navigate("/", "overview");
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="site-header" aria-hidden={showGuard ? true : undefined}>
      <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">S</span><span><strong>STONKS</strong><small>INDIA RESEARCH</small></span></div>
      <nav aria-label="Primary navigation" className="primary-nav">
        <button className={view === "overview" ? "nav-link active" : "nav-link"} onClick={openOverview}>Overview</button>
        <button className={view === "editor" || view === "results" ? "nav-link active" : "nav-link"} onClick={() => openEditor("new")}>Screens</button>
        <button className={view === "learn" ? "nav-link active" : "nav-link"} type="button" onClick={() => navigate("/learn", "learn")}>Learn</button>
      </nav>
      <div className="private-badge"><span className="status-dot" aria-hidden="true" />Private workspace</div>
    </header>

    <main id="main-content" tabIndex={-1} aria-hidden={showGuard ? true : undefined}>
      <div className="page-frame">
        {view === "overview" ? <Overview status={status} screens={screens} runs={runs} loading={loading} historyLoading={historyLoading} loadError={loadError} historyError={historyError} actionError={actionError} onRetry={() => void loadWorkspace()} onNew={() => openEditor("new")} onEdit={(screen) => openEditor("edit", screen)} onDuplicate={(screen) => openEditor("duplicate", screen)} onRun={onRun} onResults={(screen) => navigate(`/screens/${encodeURIComponent(screen.id)}`, "results", screen.id)} /> : view === "editor" ? <ScreenEditor {...(editing ? { initialScreen: editing } : {})} metrics={metrics} mode={editorMode} api={resolvedApi} onDirtyChange={setDirty} onSaved={onSaved} onRun={onRun} onBack={openOverview} /> : view === "results" ? <ScreenResultsView screenId={resolvedScreenId} api={resolvedApi} onBack={openOverview} onOpenInstrument={openInstrument} /> : view === "learn" ? <LearnView entries={DEFAULT_GLOSSARY_ENTRIES} initialSlug={routeId || undefined} /> : <InstrumentView instrumentId={routeId} api={resolvedApi} onBack={backFromInstrument} />}
      </div>
    </main>
    <div aria-hidden={showGuard ? true : undefined}><Disclosure effectiveDate={status.effectiveDate} /></div>
    {showGuard ? <UnsavedChangesDialog onStay={() => setShowGuard(false)} onLeave={confirmLeave} /> : null}
  </div>;
}

interface OverviewProps { readonly status?: StatusDto; readonly screens?: readonly SavedScreen[]; readonly runs?: Readonly<Record<string, ScreenRunSummary | undefined>>; readonly loading?: boolean; readonly historyLoading?: boolean; readonly loadError?: string | null; readonly historyError?: string | null; readonly actionError?: string | null; readonly onRetry?: () => void; readonly onNew?: () => void; readonly onEdit?: (screen: SavedScreen) => void; readonly onDuplicate?: (screen: SavedScreen) => void; readonly onRun?: (screen: SavedScreen) => Promise<void>; readonly onResults?: (screen: SavedScreen) => void; }
export function Overview({ status = fallbackStatus, screens = [], runs = {}, loading = false, historyLoading = false, loadError = null, historyError = null, actionError = null, onRetry = () => undefined, onNew = () => undefined, onEdit = () => undefined, onDuplicate = () => undefined, onRun = async () => undefined, onResults = () => undefined }: OverviewProps) {
  const totalMatches = Object.values(runs).reduce((sum, run) => sum + (run?.matchCount ?? 0), 0);
  return <>
    {loadError ? <div className="error-banner" role="alert"><span>{loadError}</span><button className="button button-quiet small" onClick={onRetry}>Retry</button></div> : null}
    {historyError ? <div className="error-banner" role="alert"><span>{historyError}</span><button className="button button-quiet small" onClick={onRetry}>Retry history</button></div> : null}
    {actionError ? <div className="error-banner" role="alert"><span>{actionError}</span></div> : null}
    {historyLoading ? <div className="loading-card" aria-live="polite">Loading saved run history…</div> : null}
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
    <section className="section-block" aria-labelledby="screens-title"><div className="section-heading"><div><p className="eyebrow">MECHANICAL FILTERS</p><h2 id="screens-title">Saved screens</h2></div><span className="result-summary">{screens.length} saved · {totalMatches} top matches</span></div>{loading ? <div className="loading-card" aria-live="polite">Loading your research workspace…</div> : screens.length ? <div className="screen-grid">{screens.map((screen) => <ScreenCard key={screen.id} screen={screen} run={runs[screen.id]} onEdit={onEdit} onDuplicate={onDuplicate} onRun={onRun} onResults={onResults} />)}</div> : <div className="empty-card"><h3>Start with a question</h3><p>Build a Screener-style query to find the strongest candidates in your universe.</p><button className="button button-secondary" onClick={onNew}>Create your first screen</button></div>}</section>
  </>;
}

function SourceCard({ source }: { readonly source: StatusDto["sources"][number] }) {
  const state = statusText(source); const healthy = state === "complete";
  return <article className="source-card"><div className="card-topline"><span className={healthy ? "source-state healthy" : "source-state delayed"}><span className="status-dot" aria-hidden="true" />{sourceLabel(source.sourceId)} {state}</span><span className="source-kind">OFFICIAL</span></div><dl className="date-list"><div><dt>Expected</dt><dd>{formatDate(source.expectedDate)}</dd></div><div><dt>Loaded</dt><dd>{formatDate(source.loadedDate)}</dd></div></dl>{source.coverage !== undefined && source.coverage !== null ? <div className="coverage"><span>Coverage</span><strong>{Math.round(source.coverage * 100)}%</strong></div> : null}</article>;
}

interface ScreenCardProps { readonly screen: SavedScreen; readonly run: ScreenRunSummary | undefined; readonly onEdit: (screen: SavedScreen) => void; readonly onDuplicate: (screen: SavedScreen) => void; readonly onRun: (screen: SavedScreen) => Promise<void>; readonly onResults: (screen: SavedScreen) => void; }
function ScreenCard({ screen, run, onEdit, onDuplicate, onRun, onResults }: ScreenCardProps) {
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const runNow = async () => { setRunning(true); setRunError(null); try { await onRun(screen); } catch { setRunError(`Unable to run ${screen.name}. Try again.`); } finally { setRunning(false); } };
  const entries = run?.matches?.filter((match) => match.entered).length ?? 0;
  const exits = run?.matches?.filter((match) => match.exited).length ?? 0;
  return <article className="screen-card"><div className="screen-card-head"><div><span className="screen-label">SAVED SCREEN</span><h3>{screen.name}</h3></div><button className="icon-button" aria-label={`Edit ${screen.name}`} onClick={() => onEdit(screen)}>Edit</button></div><code className="query-preview">{screen.source}</code>{runError ? <div className="inline-error" role="alert">{runError}</div> : null}<div className="screen-card-meta"><div><span className="context-label">Last run</span><strong>{run ? formatDate(run.effectiveDate) : "Not run"}</strong></div><div><span className="context-label">Top matches</span><strong>{run?.matchCount ?? "—"}</strong></div><div><span className="context-label">Since prior run</span><strong className={entries || exits ? "text-positive" : ""}>{run ? `+${entries} / −${exits}` : "—"}</strong></div></div><div className="card-actions"><button className="button button-primary small" onClick={() => void runNow()} disabled={running}>{running ? "Running…" : "Run screen"}</button>{run ? <button className="button button-secondary small" onClick={() => onResults(screen)}>View results</button> : null}<button className="button button-quiet small" onClick={() => onDuplicate(screen)}>Duplicate</button></div>{run?.matches?.find((match) => !match.exited) ? <a className="result-link" href={`/screens/${encodeURIComponent(screen.id)}`} onClick={(event) => { event.preventDefault(); onResults(screen); }}>View first match</a> : null}</article>;
}

interface EditorProps { readonly metrics?: readonly MetricDefinition[]; readonly initialScreen?: SavedScreen; readonly initialValue?: string; readonly mode?: EditorMode; readonly api?: DashboardApi; readonly onDirtyChange?: (dirty: boolean) => void; readonly onSaved?: (screen: SavedScreen) => void; readonly onRun?: (screen: SavedScreen) => Promise<void>; readonly onBack?: () => void; }
export function ScreenEditor({ metrics = DEFAULT_METRIC_CATALOG, initialScreen, initialValue, mode = "new", api = createApiClient(), onDirtyChange = () => undefined, onSaved = () => undefined, onRun = async () => undefined, onBack = () => undefined }: EditorProps) {
  const initialName = initialScreen && mode !== "duplicate" ? initialScreen.name : initialScreen ? `${initialScreen.name} copy` : "";
  const startingSource = initialValue ?? initialScreen?.source ?? "";
  const [name, setName] = useState(initialName); const [source, setSource] = useState(startingSource); const [caret, setCaret] = useState(initialValue?.length ?? startingSource.length); const [diagnostic, setDiagnostic] = useState<string | null>(null); const [saveError, setSaveError] = useState<string | null>(null); const [validating, setValidating] = useState(false); const [saving, setSaving] = useState(false); const [suggestionsOpen, setSuggestionsOpen] = useState(Boolean(initialValue)); const [selectedSuggestion, setSelectedSuggestion] = useState(0); const [saved, setSaved] = useState<SavedScreen | undefined>(initialScreen && mode === "edit" ? initialScreen : undefined); const [savedName, setSavedName] = useState(mode === "edit" ? initialName : ""); const [savedSource, setSavedSource] = useState(mode === "edit" ? (initialScreen?.source ?? "") : ""); const editorRef = useRef<HTMLTextAreaElement>(null);
  const currentFragment = useMemo(() => metricFragment(source, caret), [source, caret]);
  const suggestions = useMemo(() => { const query = currentFragment.text.toLocaleLowerCase("en-IN"); if (!query || !metrics.length) return []; return metrics.filter((metric) => metric.label.toLocaleLowerCase("en-IN").includes(query) || metric.aliases.some((alias) => alias.toLocaleLowerCase("en-IN").includes(query))).slice(0, 6); }, [currentFragment.text, metrics]);
  useEffect(() => { onDirtyChange(name !== savedName || source !== savedSource); }, [name, source, savedName, savedSource, onDirtyChange]);
  useEffect(() => { setValidating(true); const timer = window.setTimeout(() => { if (!source.trim()) { setDiagnostic(null); setValidating(false); return; } const parsed = parseQuery(source); if (!parsed.value || parsed.diagnostics.length) setDiagnostic(parsed.diagnostics[0]?.message ?? "Add a complete condition to validate this query."); else { const checked = typecheckQuery(parsed.value, metrics); setDiagnostic(checked.valid ? "Query looks valid for the selected universe." : checked.diagnostics[0]?.message ?? "Review this query."); } setValidating(false); }, 350); return () => window.clearTimeout(timer); }, [source, metrics]);
  useEffect(() => { if (initialValue && editorRef.current) { editorRef.current.focus(); editorRef.current.setSelectionRange(initialValue.length, initialValue.length); setCaret(initialValue.length); } }, [initialValue]);
  const selectSuggestion = (metric: MetricDefinition) => { const fragment = metricFragment(source, editorRef.current?.selectionStart ?? caret); const nextSource = `${source.slice(0, fragment.start)}${metric.label}${source.slice(fragment.end)}`; const nextCaret = fragment.start + metric.label.length; setSource(nextSource); setCaret(nextCaret); setSuggestionsOpen(false); setSelectedSuggestion(0); requestAnimationFrame(() => { editorRef.current?.focus(); editorRef.current?.setSelectionRange(nextCaret, nextCaret); }); };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (!suggestionsOpen || !suggestions.length) return; if (event.key === "ArrowDown") { event.preventDefault(); setSelectedSuggestion((value) => (value + 1) % suggestions.length); } else if (event.key === "ArrowUp") { event.preventDefault(); setSelectedSuggestion((value) => (value - 1 + suggestions.length) % suggestions.length); } else if (event.key === "Enter") { event.preventDefault(); const suggestion = suggestions[selectedSuggestion]; if (suggestion) selectSuggestion(suggestion); } else if (event.key === "Escape") setSuggestionsOpen(false); };
  const save = async () => { if (!name.trim() || !source.trim()) { setDiagnostic("Give this screen a name and query before saving."); return; } const parsed = parseQuery(source); if (!parsed.value || parsed.diagnostics.length) { setDiagnostic(parsed.diagnostics[0]?.message ?? "Review this query."); return; } const checked = typecheckQuery(parsed.value, metrics); if (!checked.valid) { setDiagnostic(checked.diagnostics[0]?.message ?? "Review this query."); return; } setSaving(true); setSaveError(null); try { let next: SavedScreen; if (saved && mode === "edit") { if (!api.updateScreen) throw new Error("Screen updates are unavailable"); next = await api.updateScreen(saved.id, { name: name.trim(), source: source.trim() }); } else next = await api.createScreen({ name: name.trim(), source: source.trim() }); setSaved(next); setSavedName(next.name); setSavedSource(next.source); onSaved(next); setDiagnostic("Saved privately."); } catch { setSaveError("Unable to save this screen. Check your connection and try again."); } finally { setSaving(false); } };
  return <section className="editor-view" aria-labelledby="editor-title"><div className="editor-header"><div><button className="back-link" onClick={onBack}>← Overview</button><p className="eyebrow">SCREEN BUILDER / {mode.toUpperCase()}</p><h1 id="editor-title">{mode === "duplicate" ? "Duplicate screen" : mode === "edit" ? "Edit screen" : "New screen"}</h1></div><div className="editor-actions"><button className="button button-quiet" onClick={onBack}>Cancel</button><button className="button button-primary" disabled={saving} onClick={() => void save()}>{saving ? "Saving…" : saved ? "Save changes" : "Save screen"}</button></div></div><div className="editor-layout"><div className="editor-main"><label className="field-label" htmlFor="screen-name">Screen name</label><input id="screen-name" className="text-input" value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Breakout with volume" /><div className="field-header"><label className="field-label" htmlFor="screen-query">Screen query</label><span className="field-hint">AND / OR conditions · percentages use %</span></div><div className="autocomplete-wrap"><textarea id="screen-query" ref={editorRef} className="query-editor" aria-describedby="query-help query-status" aria-autocomplete="list" aria-controls="metric-suggestions" value={source} onChange={(event) => { setSource(event.target.value); setCaret(event.target.selectionStart); setSuggestionsOpen(true); }} onFocus={(event) => { setCaret(event.target.selectionStart); setSuggestionsOpen(source.length > 0); }} onKeyDown={onKeyDown} rows={7} placeholder={'Return over 1day > 3% AND\nVolume > Volume 1week average * 1.5'} />{suggestionsOpen && suggestions.length ? <div className="suggestion-popover" id="metric-suggestions" role="listbox" aria-label="Metric suggestions">{suggestions.map((metric, index) => <button role="option" aria-label={metric.label} aria-selected={index === selectedSuggestion} className={index === selectedSuggestion ? "suggestion selected" : "suggestion"} key={metric.id} onMouseDown={(event) => event.preventDefault()} onClick={() => selectSuggestion(metric)}><span aria-hidden="true">{metric.label}</span><small aria-hidden="true">{metricUnit(metric)} · {metric.assetClasses.join(" / ")}</small></button>)}</div> : null}</div><p id="query-help" className="field-hint">Start typing a metric to see known fields, units, and applicability.</p>{saveError ? <div className="inline-error" role="alert">{saveError}</div> : null}<div id="query-status" role="status" aria-live="polite" className={diagnostic?.toLowerCase().includes("valid") || diagnostic?.toLowerCase().includes("saved") ? "validation success" : diagnostic ? "validation error" : "validation"}>{validating ? "Checking query…" : diagnostic ?? "Your query is private to this workspace."}</div><div className="editor-footer"><button className="button button-secondary" disabled={!saved || saving} onClick={() => saved && void onRun(saved)}>Run saved screen</button><span className="save-note">{saved ? `Saved ${formatDate(saved.updatedAt)}` : "Not saved yet"}</span></div></div><aside className="metric-browser" aria-labelledby="catalog-title"><div className="catalog-heading"><p className="eyebrow">REFERENCE</p><h2 id="catalog-title">Metric catalog</h2></div><p className="catalog-copy">Every field shows its unit and supported universe.</p><div className="metric-list">{metrics.slice(0, 12).map((metric) => <button className="metric-row" key={metric.id} onClick={() => selectSuggestion(metric)}><span>{metric.label}</span><small>{metricUnit(metric)} · {metric.assetClasses.includes("mutual_fund") ? "All assets" : metric.assetClasses.join(" / ")}</small></button>)}</div></aside></div></section>;
}

function Disclosure({ effectiveDate }: { readonly effectiveDate: string | null }) { return <footer className="disclosure"><div><span className="disclosure-mark" aria-hidden="true">i</span><span><strong>Research tool, not investment advice.</strong> Mechanical matches do not guarantee returns. Review source dates before acting.</span></div><span>Effective date: {formatDate(effectiveDate)}</span></footer>; }
function UnsavedChangesDialog({ onStay, onLeave }: { readonly onStay: () => void; readonly onLeave: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null); const stayRef = useRef<HTMLButtonElement>(null); const leaveRef = useRef<HTMLButtonElement>(null); const restoreRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    stayRef.current?.focus();
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onStay(); return; }
      if (event.key !== "Tab") return;
      const active = document.activeElement;
      if (!dialogRef.current?.contains(active)) { event.preventDefault(); (event.shiftKey ? leaveRef.current : stayRef.current)?.focus(); }
      else if (event.shiftKey && active === stayRef.current) { event.preventDefault(); leaveRef.current?.focus(); }
      else if (!event.shiftKey && active === leaveRef.current) { event.preventDefault(); stayRef.current?.focus(); }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => { document.removeEventListener("keydown", handleKeyDown); restoreRef.current?.focus(); };
  }, [onStay]);
  return <div className="modal-backdrop"><div className="modal" ref={dialogRef} role="dialog" tabIndex={-1} aria-modal="true" aria-labelledby="unsaved-title" aria-describedby="unsaved-copy"><h2 id="unsaved-title">Leave without saving?</h2><p id="unsaved-copy">Your screen changes are still local and have not been saved.</p><div className="modal-actions"><button ref={stayRef} className="button button-quiet" onClick={onStay}>Stay</button><button ref={leaveRef} className="button button-primary" onClick={onLeave}>Leave</button></div></div></div>;
}
