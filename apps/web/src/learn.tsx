import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { DEFAULT_GLOSSARY_ENTRIES, type GlossaryEntry } from "@stonks/contracts";

const formatReviewedAt = (value: string): string => new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(`${value}T00:00:00Z`)).replace("Sept", "Sep");
const assetLabel = { equity: "NSE equity", etf: "NSE ETF", mutual_fund: "AMFI mutual fund" } as const;

export interface LearnGlossaryApi { getGlossary?(): Promise<{ readonly data: readonly GlossaryEntry[] }>; }

export function LearnView({ entries, api, initialSlug }: { readonly entries?: readonly GlossaryEntry[]; readonly api?: LearnGlossaryApi; readonly initialSlug?: string | undefined }) {
  const [query, setQuery] = useState("");
  const [selectedSlug, setSelectedSlug] = useState(initialSlug ?? "");
  const [remoteEntries, setRemoteEntries] = useState<readonly GlossaryEntry[] | null>(null);
  useEffect(() => {
    if (entries || !api?.getGlossary) return undefined;
    let active = true;
    api.getGlossary().then((page) => { if (active) setRemoteEntries(page.data); }).catch(() => undefined);
    return () => { active = false; };
  }, [api, entries]);
  const resolvedEntries = entries ?? remoteEntries ?? DEFAULT_GLOSSARY_ENTRIES;
  const normalized = query.trim().toLocaleLowerCase("en-IN");
  const filtered = useMemo(() => resolvedEntries.filter((entry) => !normalized || [entry.term, entry.slug, ...entry.aliases, entry.summary].some((field) => field.toLocaleLowerCase("en-IN").includes(normalized))), [resolvedEntries, normalized]);
  const selected = resolvedEntries.find((entry) => entry.slug === selectedSlug);
  return <section className="learn-view" aria-labelledby="learn-title">
    <div className="hero-row"><div><p className="eyebrow">REFERENCE / REVIEWED CONTENT</p><h1 id="learn-title">Learn the language</h1><p className="lede">Short, source-linked explanations for the metrics used in this private research workspace.</p></div></div>
    <div className="learn-search"><label className="field-label" htmlFor="learn-searchbox">Search Learn</label><input id="learn-searchbox" className="text-input" type="search" role="searchbox" aria-label="Search Learn" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search a term or alias" /></div>
    {selected ? <GlossaryDetail entry={selected} onBack={() => setSelectedSlug("")} /> : <div className="glossary-grid" aria-live="polite">{filtered.length ? filtered.map((entry) => <article className="glossary-card" key={entry.slug}><span className="screen-label">{entry.assetClasses.map((assetClass) => assetLabel[assetClass]).join(" · ")}</span><h2>{entry.term}</h2><p>{entry.summary}</p><button className="button button-secondary small" type="button" onClick={() => setSelectedSlug(entry.slug)}>Read {entry.term}</button></article>) : <div className="empty-card"><h2>No matching terms</h2><p>Try an alias or a shorter search.</p></div>}</div>}
  </section>;
}

function GlossaryDetail({ entry, onBack }: { readonly entry: GlossaryEntry; readonly onBack: () => void }) {
  return <article className="glossary-detail"><button className="back-link" type="button" onClick={onBack}>← All Learn terms</button><div className="section-heading"><div><p className="eyebrow">TERM / {entry.slug}</p><h2>{entry.term}</h2></div><span className="coverage-badge">Reviewed {formatReviewedAt(entry.reviewedAt)}</span></div><p className="glossary-summary">{entry.summary}</p>{entry.formula ? <section className="glossary-section" aria-labelledby="formula-title"><h3 id="formula-title">Formula</h3><code className="query-preview">{entry.formula.expression}</code><p className="section-note">Formula provenance:</p><SourceLinks sources={entry.formula.provenance} /></section> : null}<section className="glossary-section" aria-labelledby="interpretation-title"><h3 id="interpretation-title">Interpretation</h3><p>{entry.interpretation}</p></section><section className="glossary-section" aria-labelledby="pitfalls-title"><h3 id="pitfalls-title">Pitfalls</h3><ul className="clause-list">{entry.pitfalls.map((pitfall) => <li key={pitfall}>{pitfall}</li>)}</ul></section><section className="glossary-section" aria-labelledby="applicability-title"><h3 id="applicability-title">Applies to</h3><p>{entry.assetClasses.map((assetClass) => assetLabel[assetClass]).join(" · ")}</p></section><section className="glossary-section" aria-labelledby="sources-title"><h3 id="sources-title">Authoritative sources</h3><SourceLinks sources={entry.sources} /></section></article>;
}

function SourceLinks({ sources }: { readonly sources: readonly { label: string; url: string }[] }) { return <ul className="source-links">{sources.map((source) => <li key={source.url}>{source.url.startsWith("/") ? <a href={source.url}>{source.label} <span aria-hidden="true">→</span></a> : <a href={source.url} target="_blank" rel="noreferrer noopener">{source.label} <span aria-hidden="true">↗</span><span className="sr-only"> (opens in a new tab)</span></a>}</li>)}</ul>; }

export function TermPopover({ entry, onLearn, children }: { readonly entry: GlossaryEntry; readonly onLearn?: (slug: string) => void; readonly children: ReactNode }) {
  const [open, setOpen] = useState(false); const rootRef = useRef<HTMLSpanElement>(null); const triggerRef = useRef<HTMLSpanElement>(null); const dialogId = `term-popover-${useId().replaceAll(":", "")}`;
  useEffect(() => { if (!open) return undefined; const closeOutside = (event: MouseEvent) => { if (!rootRef.current?.contains(event.target as Node)) { setOpen(false); triggerRef.current?.focus(); } }; const closeEscape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); setOpen(false); triggerRef.current?.focus(); } }; document.addEventListener("mousedown", closeOutside); document.addEventListener("keydown", closeEscape); return () => { document.removeEventListener("mousedown", closeOutside); document.removeEventListener("keydown", closeEscape); }; }, [open]);
  const toggle = () => setOpen((value) => !value);
  return <span className="term-popover-wrap" ref={rootRef}><span ref={triggerRef} role="button" tabIndex={0} className="term-trigger" aria-haspopup="dialog" aria-expanded={open} aria-controls={dialogId} aria-label={`Learn about ${entry.term}`} onClick={toggle} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } }}>{children}</span>{open ? <span className="term-popover" id={dialogId} role="dialog" aria-label={`${entry.term} definition`} tabIndex={-1}><strong>{entry.term}</strong><p>{entry.summary}</p>{entry.formula ? <code>{entry.formula.expression}</code> : null}<p className="term-popover-meta">Applies to {entry.assetClasses.map((assetClass) => assetLabel[assetClass]).join(" · ")}. Reviewed {formatReviewedAt(entry.reviewedAt)}.</p><a href={`/learn/${entry.slug}`} onClick={(event) => { if (onLearn) { event.preventDefault(); onLearn(entry.slug); } }}>Full {entry.term} entry <span aria-hidden="true">→</span></a></span> : null}</span>;
}
