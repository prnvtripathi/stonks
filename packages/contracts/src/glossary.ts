import type { AssetClass } from "./metrics";

export interface GlossarySource {
  readonly label: string;
  readonly url: string;
}

export interface GlossaryFormula {
  readonly expression: string;
  readonly provenance: readonly GlossarySource[];
}

export interface GlossaryEntry {
  readonly slug: string;
  readonly term: string;
  readonly aliases: readonly string[];
  readonly summary: string;
  readonly formula?: GlossaryFormula;
  readonly interpretation: string;
  readonly pitfalls: readonly string[];
  readonly assetClasses: readonly AssetClass[];
  readonly sources: readonly GlossarySource[];
  readonly reviewedAt: string;
}

const SOURCE = {
  sebi: { label: "SEBI Investor glossary", url: "https://investor.sebi.gov.in/pdf/1315462034888.pdf" },
  sebiTechnical: { label: "SEBI Investor: technical and fundamental analysis", url: "https://investor.sebi.gov.in/tech_fund_analysis.html" },
  sebiNav: { label: "SEBI Investor: Net Asset Value", url: "https://investor.sebi.gov.in/securities-mf-investments.html" },
  amfiNav: { label: "AMFI: Net Asset Value", url: "https://www.amfiindia.com/investor/knowledge-center-info?zoneName=NetAssetValueNAV" },
  nseTechnical: { label: "SEBI Investor: technical and fundamental analysis", url: "https://investor.sebi.gov.in/tech_fund_analysis.html" },
  nseCourse: { label: "NSE India: technical and fundamental analysis course", url: "https://www.nseindia.com/static/learn/technical-fundamental-analysis-capital-market" },
  niftyIndicators: { label: "Nifty Indices: important indicators", url: "https://niftyindices.com/resources/tutorial/important-indicators" },
  cfaRatios: { label: "CFA Institute: financial ratio list", url: "https://www.cfainstitute.org/sites/default/files/-/media/documents/support/programs/cfa/cfa_program_level_ii_financial_ratio_list.pdf" },
  cfaRoe: { label: "CFA Institute: DuPont return on equity explainer", url: "https://blogs.cfainstitute.org/blog/2013/01/23/how-much-does-apple-make-a-dupont-analysis/" },
  cfaCashFlow: { label: "CFA Institute: analyzing statements of cash flows", url: "https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/analyzing-statements-of-cash-flows-i" },
} as const satisfies Record<string, GlossarySource>;

const REVIEWED_AT = "2026-09-07";
const ALL: readonly AssetClass[] = ["equity", "etf", "mutual_fund"];
const PRICE: readonly AssetClass[] = ["equity", "etf"];
const EQUITY: readonly AssetClass[] = ["equity"];
const MF: readonly AssetClass[] = ["mutual_fund"];

export const DEFAULT_GLOSSARY_ENTRIES: readonly GlossaryEntry[] = [
  { slug: "return", term: "Return", aliases: ["Performance", "Price return"], summary: "The percentage change between two observations, using the adjusted close or NAV series chosen by the dataset.", formula: { expression: "(ending value ÷ starting value − 1) × 100", provenance: [SOURCE.nseTechnical, SOURCE.amfiNav] }, interpretation: "Positive return means the ending observation is higher over the selected period; it does not describe the path taken.", pitfalls: ["A price return omits distributions unless the series is total-return adjusted.", "Calendar windows can have fewer trading observations."], assetClasses: ALL, sources: [SOURCE.nseTechnical, SOURCE.amfiNav], reviewedAt: REVIEWED_AT },
  { slug: "volume", term: "Volume", aliases: ["Traded volume", "Daily volume"], summary: "The number of units traded for a listed equity or ETF during a session.", interpretation: "Volume helps put a price move in participation context; unusually high volume can reflect either genuine interest or event-driven trading.", pitfalls: ["Volume is not the same as traded value.", "A single session is noisy and can be distorted by corporate actions or special trades."], assetClasses: PRICE, sources: [SOURCE.sebiTechnical], reviewedAt: REVIEWED_AT },
  { slug: "average-volume", term: "Average volume", aliases: ["Weekly average volume", "Volume average"], summary: "The arithmetic mean of daily traded units across a stated lookback window.", formula: { expression: "sum of daily volumes ÷ number of observed sessions", provenance: [SOURCE.sebiTechnical] }, interpretation: "Comparing current volume with its own average can show whether participation is expanding or contracting.", pitfalls: ["Averages are sensitive to outlier sessions.", "Do not compare raw volume across securities with different units outstanding."], assetClasses: PRICE, sources: [SOURCE.sebiTechnical], reviewedAt: REVIEWED_AT },
  { slug: "market-cap", term: "Market capitalisation", aliases: ["Market capitalization", "Market cap"], summary: "The market value of a listed company’s outstanding shares at the quoted price.", formula: { expression: "current share price × shares outstanding", provenance: [SOURCE.sebiTechnical, SOURCE.sebi] }, interpretation: "It is a size measure used to group companies; it is not the same as enterprise value or intrinsic value.", pitfalls: ["Price and share-count changes can move it independently.", "Free-float market cap and total market cap answer different questions."], assetClasses: EQUITY, sources: [SOURCE.sebiTechnical, SOURCE.sebi], reviewedAt: REVIEWED_AT },
  { slug: "moving-average", term: "Moving average", aliases: ["MA", "Simple moving average", "SMA"], summary: "A rolling average of observed prices or NAVs used to smooth short-term variation.", formula: { expression: "sum of the last N observations ÷ N", provenance: [SOURCE.nseTechnical] }, interpretation: "A price above a moving average can indicate recent strength relative to that smoothed baseline, but it is a lagging description.", pitfalls: ["The lookback length changes the signal.", "It is not a forecast and can whipsaw in sideways markets."], assetClasses: ALL, sources: [SOURCE.nseTechnical], reviewedAt: REVIEWED_AT },
  { slug: "52-week-high", term: "52-week high", aliases: ["52 week high", "One-year high"], summary: "The highest observed price in the dataset’s trailing 52-week window.", formula: { expression: "max(observed adjusted prices over the trailing 52 weeks)", provenance: [SOURCE.nseCourse] }, interpretation: "Distance to the high describes where the latest observation sits within its recent range.", pitfalls: ["Corporate-action adjustment conventions must be consistent.", "Touching a high does not establish support or future direction."], assetClasses: PRICE, sources: [SOURCE.nseCourse], reviewedAt: REVIEWED_AT },
  { slug: "relative-strength-benchmark", term: "Relative strength versus benchmark", aliases: ["Benchmark-relative RS", "Relative performance"], summary: "This dashboard’s documented methodology compares an asset’s return with a chosen benchmark over the same interval.", formula: { expression: "((1 + asset return) ÷ (1 + benchmark return) − 1) × 100", provenance: [{ label: "Stonks methodology", url: "/learn/relative-strength-benchmark" }] }, interpretation: "A positive result means the asset outperformed the benchmark for that window; it is not the RSI oscillator.", pitfalls: ["Benchmark selection changes the result.", "Short windows can be dominated by noise and timing."], assetClasses: PRICE, sources: [SOURCE.nseTechnical], reviewedAt: REVIEWED_AT },
  { slug: "relative-strength-rating", term: "Relative strength rating", aliases: ["RS Rating", "Cohort RS rating", "Relative Strength Rating"], summary: "This dashboard’s documented methodology ranks an instrument’s approved relative-strength measure within its asset-class cohort.", formula: { expression: "stable percentile rank within the applicable cohort, scaled 1–99", provenance: [{ label: "Stonks methodology", url: "/learn/relative-strength-rating" }] }, interpretation: "Higher values indicate stronger relative performance within this dataset’s cohort, not a guarantee of future returns.", pitfalls: ["Scores are cohort-relative and cannot be compared across different universes without care.", "Ties and missing history affect rank coverage."], assetClasses: PRICE, sources: [SOURCE.nseTechnical, SOURCE.nseCourse], reviewedAt: REVIEWED_AT },
  { slug: "volatility", term: "Volatility", aliases: ["Annualized volatility", "Price volatility"], summary: "A measure of return variation, annualized from the trailing 252 valid sessions for comparison.", formula: { expression: "standard deviation of 252 trailing session returns × √252", provenance: [SOURCE.niftyIndicators] }, interpretation: "Higher volatility means a wider historical range of outcomes, not an automatic sign of loss or opportunity.", pitfalls: ["A full 252-session return window plus its preceding price endpoint is required; otherwise the one-year value is unavailable.", "Historical volatility is backward-looking and can change quickly."], assetClasses: ALL, sources: [SOURCE.niftyIndicators], reviewedAt: REVIEWED_AT },
  { slug: "drawdown", term: "Drawdown", aliases: ["Maximum drawdown", "Peak-to-trough decline"], summary: "The deepest decline from a prior running peak within the trailing one-year price or NAV window.", formula: { expression: "min(current value ÷ prior running peak − 1) across the 253 observations supporting 252 sessions", provenance: [{ label: "Stonks methodology", url: "/learn/drawdown" }] }, interpretation: "Maximum drawdown summarizes the deepest peak-to-trough fall in the selected one-year history.", pitfalls: ["A full 252-session return window plus its preceding price endpoint is required; otherwise the one-year value is unavailable.", "The recovery period is not captured by the percentage alone."], assetClasses: ALL, sources: [SOURCE.nseTechnical], reviewedAt: REVIEWED_AT },
  { slug: "nav", term: "Net asset value (NAV)", aliases: ["NAV", "Net asset value per unit"], summary: "The per-unit value of a mutual fund scheme after valuing assets and subtracting liabilities.", formula: { expression: "(scheme assets − scheme liabilities) ÷ units outstanding", provenance: [SOURCE.amfiNav, SOURCE.sebiNav] }, interpretation: "NAV is the accounting value per unit; percentage change in NAV is more useful for comparing similar schemes than the absolute NAV level.", pitfalls: ["A lower NAV does not make a scheme cheaper or better.", "Applicable transaction NAV and cut-off rules can differ by scheme and transaction."], assetClasses: MF, sources: [SOURCE.amfiNav, SOURCE.sebiNav], reviewedAt: REVIEWED_AT },
  { slug: "roe", term: "Return on equity (ROE)", aliases: ["ROE", "Return on shareholders’ equity"], summary: "A profitability ratio relating net income to the equity provided by owners.", formula: { expression: "net income ÷ average or reported total equity", provenance: [SOURCE.cfaRoe, SOURCE.cfaRatios] }, interpretation: "It indicates how much accounting profit was generated per unit of equity under the chosen period and denominator convention.", pitfalls: ["Leverage, buybacks, losses, and denominator timing can materially change it.", "ROE is not comparable when accounting policies or business models differ."], assetClasses: EQUITY, sources: [SOURCE.cfaRoe, SOURCE.cfaRatios], reviewedAt: REVIEWED_AT },
  { slug: "roce", term: "Return on capital employed (ROCE)", aliases: ["ROCE", "Return on capital"], summary: "A profitability ratio relating operating earnings to the capital committed to the business.", formula: { expression: "operating profit or EBIT ÷ capital employed", provenance: [SOURCE.cfaRatios] }, interpretation: "It can help assess operating efficiency independent of the financing mix when numerator and denominator are defined consistently.", pitfalls: ["Definitions of EBIT and capital employed vary.", "Asset age, leases, and capital intensity affect comparisons."], assetClasses: EQUITY, sources: [SOURCE.cfaRatios], reviewedAt: REVIEWED_AT },
  { slug: "debt-equity", term: "Debt-to-equity", aliases: ["D/E", "Debt equity ratio"], summary: "A leverage ratio comparing a company’s debt with the equity supporting it.", formula: { expression: "total debt ÷ total equity", provenance: [SOURCE.cfaRatios, SOURCE.sebiTechnical] }, interpretation: "A higher ratio generally indicates more debt relative to equity and therefore greater sensitivity to financing conditions.", pitfalls: ["Debt definitions differ across datasets and industries.", "A ratio alone does not show maturity, interest cost, or cash coverage."], assetClasses: EQUITY, sources: [SOURCE.cfaRatios, SOURCE.sebiTechnical], reviewedAt: REVIEWED_AT },
  { slug: "pe", term: "Price-to-earnings ratio (P/E)", aliases: ["P/E", "PE ratio", "Price earnings"], summary: "A valuation multiple comparing a share price with earnings per share over a stated period.", formula: { expression: "market price per share ÷ earnings per share", provenance: [SOURCE.sebiTechnical] }, interpretation: "It expresses how much the market price represents for each unit of reported earnings, subject to the earnings definition.", pitfalls: ["Negative or near-zero earnings make it undefined or unstable.", "Trailing, forward, standalone, and adjusted earnings are not interchangeable."], assetClasses: EQUITY, sources: [SOURCE.sebiTechnical], reviewedAt: REVIEWED_AT },
  { slug: "cash-flow", term: "Cash flow", aliases: ["Operating cash flow", "Cash flow statement"], summary: "The movement of cash and cash equivalents through operating, investing, and financing activities during a period.", formula: { expression: "closing cash − opening cash, reconciled through operating, investing, and financing flows", provenance: [SOURCE.cfaCashFlow] }, interpretation: "Cash flow complements accrual profit by showing how cash was generated and used; operating cash flow is often the most direct operating lens.", pitfalls: ["Cash flow is period- and classification-sensitive.", "A positive total can coexist with weak operations if financing or asset sales supplied the cash."], assetClasses: EQUITY, sources: [SOURCE.cfaCashFlow], reviewedAt: REVIEWED_AT },
];

export function parseGlossaryEntry(value: unknown): GlossaryEntry {
  if (!isRecord(value)) throw new Error("Glossary entry must be an object");
  const knownKeys = new Set(["slug", "term", "aliases", "summary", "formula", "interpretation", "pitfalls", "assetClasses", "sources", "reviewedAt"]);
  if (Object.keys(value).some((key) => !knownKeys.has(key))) throw new Error("Glossary entry contains an unknown or additional field");
  const slug = text(value.slug, "slug");
  const term = text(value.term, "term");
  const aliases = strings(value.aliases, "aliases");
  const summary = text(value.summary, "summary");
  const interpretation = text(value.interpretation, "interpretation");
  const pitfalls = strings(value.pitfalls, "pitfalls");
  const assetClasses = strings(value.assetClasses, "assetClasses");
  if (assetClasses.some((assetClass) => !["equity", "etf", "mutual_fund"].includes(assetClass))) throw new Error("Glossary entry has an invalid asset class");
  const sources = parseSources(value.sources, "sources");
  const reviewedAt = text(value.reviewedAt, "reviewedAt");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(reviewedAt) || Number.isNaN(Date.parse(`${reviewedAt}T00:00:00Z`))) throw new Error("Glossary entry requires a valid review date");
  const formulaValue = value.formula;
  let formula: GlossaryFormula | undefined;
  if (formulaValue !== undefined) {
    if (!isRecord(formulaValue)) throw new Error("Glossary formula must be an object");
    if (Object.keys(formulaValue).some((key) => key !== "expression" && key !== "provenance")) throw new Error("Glossary formula contains an unknown field");
    const provenance = parseSources(formulaValue.provenance, "formula provenance");
    formula = { expression: text(formulaValue.expression, "formula expression"), provenance };
  }
  return { slug, term, aliases, summary, ...(formula ? { formula } : {}), interpretation, pitfalls, assetClasses: assetClasses as AssetClass[], sources, reviewedAt };
}

export function validateGlossaryEntries(values: readonly unknown[]): readonly GlossaryEntry[] {
  const entries = values.map(parseGlossaryEntry);
  const slugs = new Set<string>(); const terms = new Set<string>(); const aliases = new Set<string>();
  for (const entry of entries) {
    const slug = entry.slug.toLocaleLowerCase("en-IN"); const term = entry.term.toLocaleLowerCase("en-IN");
    if (slugs.has(slug)) throw new Error(`Duplicate glossary slug: ${entry.slug}`);
    if (terms.has(term)) throw new Error(`Duplicate glossary term: ${entry.term}`);
    slugs.add(slug); terms.add(term);
    for (const alias of entry.aliases) { const normalized = alias.toLocaleLowerCase("en-IN"); if (aliases.has(normalized)) throw new Error(`Duplicate glossary alias: ${alias}`); aliases.add(normalized); }
  }
  return entries;
}

function parseSources(value: unknown, label: string): GlossarySource[] {
  if (!Array.isArray(value) || value.length === 0) throw new Error(`Glossary entry requires at least one ${label}`);
  return value.map((source) => { if (!isRecord(source)) throw new Error(`${label} must contain objects`); if (Object.keys(source).some((key) => key !== "label" && key !== "url")) throw new Error(`${label} contains an unknown field`); const sourceLabel = text(source.label, `${label} label`); const url = text(source.url, `${label} URL`); if (!url.startsWith("/")) { try { const parsed = new URL(url); if (parsed.protocol !== "https:" || !parsed.hostname) throw new Error(); } catch { throw new Error(`${label} URLs must use HTTPS with a host or a root-relative app path`); } } return { label: sourceLabel, url }; });
}
function strings(value: unknown, label: string): string[] { if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || item.trim().length === 0)) throw new Error(`Glossary ${label} must be a non-empty string array`); return value.map((item) => item.trim()); }
function text(value: unknown, label: string): string { if (typeof value !== "string" || value.trim().length === 0) throw new Error(`Glossary ${label} is required`); return value.trim(); }
function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }

validateGlossaryEntries(DEFAULT_GLOSSARY_ENTRIES);
