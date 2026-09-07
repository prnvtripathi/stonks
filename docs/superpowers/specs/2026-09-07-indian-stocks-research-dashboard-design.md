# Indian Markets Research Dashboard Design

**Date:** 2026-09-07  
**Status:** Approved design  
**Audience:** Project owner and implementers

## Purpose

Build a strictly private, end-of-day research dashboard for medium- and long-term analysis of Indian equities, ETFs, and mutual funds. The product lets its owner write and save Screener.in-style queries, review mechanically ranked matches, inspect the calculations behind each result, and learn unfamiliar financial terms from contextual explanations and authoritative sources.

The dashboard is a research tool. It presents "top matches," not buy recommendations, and never promises returns.

## Scope

### Included in version 1

- All actively traded NSE `EQ`-series ordinary shares.
- NSE-listed ETFs.
- All AMFI-listed mutual-fund schemes using daily NAV data.
- Three years of daily price or NAV history.
- Available quarterly and annual company fundamentals covering the same period.
- Official, publicly downloadable end-of-day data and corporate filings.
- Saved text queries with execution history and result counts.
- Momentum-led ranking with an explainable score breakdown.
- Benchmark-relative strength and a transparent 1–99 percentile RS rating.
- Instrument research pages, comparisons, data provenance, and a financial glossary.
- Private access from desktop and mobile browsers.

### Excluded from version 1

- Real-time or intraday market data.
- BSE-only securities, SME securities, REITs, InvITs, preference shares, derivatives, commodities, bonds, and special series.
- Brokerage connectivity, portfolio monitoring, order placement, and trade execution.
- Public accounts, public APIs, social features, and public sharing.
- AI-generated investment advice or glossary content.
- US securities. Provider and identity boundaries must allow a US-market module later without changing existing query semantics.

## Source Policy

The system uses official public artifacts rather than undocumented website endpoints or third-party scraping:

- NSE daily reports for EOD price, volume, delivery, security, ETF, market-cap, and related market files.
- NSE corporate filings and their available CSV/XBRL artifacts for company results and corporate actions.
- AMFI daily and historical NAV data for mutual funds.
- Nifty 500 official closing values for benchmark calculations.

Only artifacts that are publicly downloadable and permitted for this private use may be automated. Formal NSE historical or real-time subscription products are not assumed to be free. Before enabling a source adapter in production, its usage and retention terms must be recorded in the adapter metadata. A source whose terms do not permit the intended automation is disabled rather than replaced by an undocumented endpoint.

Every downloaded artifact is retained with its source URL, retrieval timestamp, effective date, checksum, adapter version, and license/terms reference. Displayed and derived records retain enough lineage to identify their input artifact and calculation version.

## System Architecture

```text
NSE reports + NSE filings + AMFI NAVs
                  |
           Provider adapters
                  |
     Raw snapshots in Cloudflare R2
                  |
       Validate and normalize data
                  |
     Calculate returns and factors
                  |
       +----------+-----------+
       |                      |
 D1 query database      R2 history/archive
       |                      |
       +----------+-----------+
                  |
       Private Cloudflare Worker
                  |
        Browser dashboard
```

### Deployment responsibilities

- **GitHub Actions:** runs scheduled and manual data jobs, validates sources, normalizes records, calculates factors, and publishes a verified dataset. The repository remains private.
- **Cloudflare R2:** stores immutable raw artifacts and compressed three-year historical datasets.
- **Cloudflare D1:** stores compact, indexed, query-ready instrument snapshots, derived fields, fundamentals, saved queries, run history, glossary metadata, and pipeline status.
- **Cloudflare Worker and static assets:** serve the authenticated application and lightweight API.
- **Cloudflare Access:** permits only the owner's allowlisted identity. There is no anonymous route or public data endpoint.

This split keeps expensive parsing and analytics outside request-time Worker execution and fits a personal workload within the free allowances. Usage counters for Worker requests, D1 reads/writes/storage, R2 operations/storage, and GitHub Actions minutes are monitored. Exceeding a warning threshold creates a visible system warning rather than silently losing data.

## Ingestion and Publication

The scheduled workflow runs after the expected publication time of the day's EOD files. It is idempotent and may be manually rerun for late or corrected source data.

For each run, the pipeline:

1. Resolves the expected trading and NAV dates for each source.
2. Downloads artifacts through isolated provider adapters.
3. Writes immutable originals and metadata to R2.
4. Validates file schema, checksum, effective date, row counts, duplicates, missing values, instrument identities, and suspicious changes.
5. Normalizes instruments and observations into versioned canonical records.
6. Applies corporate-action adjustments to historical equity prices before return calculations.
7. Calculates derived price, volume, momentum, risk, benchmark, and fundamental fields.
8. Loads D1 staging tables and runs reconciliation checks.
9. Atomically promotes the staging dataset only when every required check passes.
10. Records completion status, coverage, warnings, formula versions, and the effective date.

A failed or incomplete run never replaces the last known-good production dataset. Source dates are tracked independently, so a delayed AMFI file does not masquerade as current merely because NSE data loaded successfully.

## Instrument Model

The system assigns a stable internal instrument ID and stores provider identifiers as versioned aliases. This prevents symbol changes from breaking history.

Asset classes share identity, date, source, and return concepts but expose separate metric catalogs:

| Asset class | Supported metric families |
| --- | --- |
| NSE ordinary share | Price, volume, delivery, momentum, market cap, valuation, profitability, growth, balance sheet, cash flow, corporate actions |
| NSE ETF | Price, volume, momentum, drawdown, volatility, fund and tracking metadata when officially available |
| AMFI mutual fund | NAV, returns, category rank, volatility, drawdown, scheme and AMC metadata |

The value states `present`, `missing`, and `not applicable` remain distinct. An absent company result is missing; ROCE for a mutual fund is not applicable. Current screens exclude inactive instruments, while inactive and delisted instruments remain available in historical records.

## Query Language

Users create saved screens with a case-insensitive, Screener.in-style expression language. A representative query is:

```text
Return over 1day > 3 AND
Volume > 500000 AND
Volume > Volume 1week average * 1.5 AND
Market Capitalization > 500
```

Version 1 supports:

- Boolean operators `AND`, `OR`, and `NOT`.
- Parentheses with standard precedence.
- Comparisons `>`, `>=`, `<`, `<=`, `=`, and `!=`.
- Arithmetic `+`, `-`, `*`, and `/`.
- Numeric and percentage literals with explicit field units.
- A versioned allowlist of named fields and aliases.
- Editor autocomplete, inline validation, and plain-language diagnostics.

Division by zero and unavailable operands evaluate to unknown, not true. A filter only matches when its final expression evaluates to true. A field that is not applicable to the selected asset class produces an editor warning and cannot accidentally match that asset class.

The backend parses input into an abstract syntax tree, type-checks it against the metric catalog, and compiles it into parameterized, allowlisted operations. Users cannot submit SQL, identifiers outside the catalog, or arbitrary functions.

Saved queries store the source text, parsed-language version, creation/update times, and run history. If a later language version changes interpretation, the system retains the old version until the owner explicitly migrates the query.

## Relative Strength and Momentum

### Benchmark relative strength

For a chosen period, benchmark RS compares the instrument's adjusted price return with the Nifty 500 price return over identical trading endpoints:

```text
Benchmark RS = ((1 + instrument return) / (1 + benchmark return) - 1) * 100
```

Positive values indicate outperformance; negative values indicate underperformance. The default display provides 3-, 6-, and 12-month values. Mutual-fund benchmark comparison is shown only when a suitable official benchmark mapping is available; it is otherwise marked unavailable.

### RS rating

The system calculates a transparent IBD-style score without claiming to reproduce a proprietary IBD formula. For each eligible NSE ordinary share, it calculates four non-overlapping trailing three-month adjusted price returns. The weighted performance score is:

```text
40% * most recent quarter
20% * second-most-recent quarter
20% * third-most-recent quarter
20% * fourth-most-recent quarter
```

The performance score is converted to an integer percentile from 1 through 99 within the eligible NSE ordinary-share universe for that effective date. Instruments without sufficient history receive no rating. ETFs and mutual funds are ranked within their own asset-class or category cohorts and are never mixed into the equity RS distribution.

### Default momentum rank

After a saved query determines eligibility, qualifying shares and ETFs are ranked by:

| Component | Weight |
| --- | ---: |
| Weighted 12-month RS percentile | 35% |
| Six-month performance | 20% |
| Three-month performance | 15% |
| Trend strength versus 50- and 200-day moving averages | 10% |
| Proximity to 52-week high | 10% |
| Volume confirmation | 10% |

Each component is normalized within the applicable cohort before weighting. Missing components do not silently become zero: the result shows insufficient coverage unless the documented minimum coverage rule is met. The full component breakdown, cohort, calculation date, and formula version are visible for every score.

Mutual funds use a separate momentum rank based on 3-, 6-, and 12-month returns, category-relative rank, volatility, and maximum drawdown. They have no volume component and are not compared directly with exchange-traded instruments.

## Product Experience

### Overview

- Effective dates and pipeline health by source.
- Saved-screen cards with current match counts.
- Top momentum-ranked matches for each screen.
- Entries into and exits from each screen since its prior successful run.

### Screens

- Text query editor with autocomplete.
- Searchable field catalog with definitions, units, applicability, and freshness.
- Validation, save, duplicate, and run actions.
- Execution history and result-count changes.

### Results

- Sortable, filterable result table.
- Momentum breakdown and a plain-language "why this matched" view.
- Side-by-side comparison for selected instruments.
- Explicit missing-data and stale-data markers.

### Instrument detail

- Three-year adjusted price or NAV chart.
- Returns, momentum, risk, relevant fundamentals, and benchmark comparisons.
- Corporate-action markers where applicable.
- Source dates, calculation versions, and data-quality warnings.
- Contextual links to glossary entries.

### Learn

- Searchable, version-controlled financial glossary.
- Plain-language definitions, formulas, interpretation guidance, examples, and common pitfalls.
- Links to authoritative material from SEBI, NSE, AMFI, company filings, and selectively curated educational sources.
- Contextual term popovers throughout the product.

Glossary content is authored and reviewed content stored with the application. Version 1 does not call a paid or generative-AI API.

The interface uses "top matches" rather than "recommended buys." Research results are mechanical, explanations are always available, and a persistent disclosure states that the application does not provide investment advice or guarantee returns.

## Security and Privacy

- Cloudflare Access protects every application and API route with the owner's allowlisted identity.
- There is no public registration, anonymous access, or public result-sharing endpoint.
- GitHub Actions authenticates with a narrowly scoped service credential that can publish only the required application data.
- Secrets live only in Cloudflare or GitHub secret stores, never in source control, D1 content, browser storage, or logs.
- The browser receives only the data required for the current view.
- Saved-query contents and authentication details are excluded from third-party analytics and operational logs.
- State-changing endpoints enforce authenticated identity, origin checks, method restrictions, payload validation, and request-size limits.
- Dependencies and deployment artifacts are pinned and scanned in continuous integration.

Portfolio data is absent from version 1, reducing the sensitivity of stored application data. Periodic D1 exports and immutable R2 inputs provide recovery for application state and derived datasets.

## Failure Handling and Observability

The application exposes source health without publishing partial results. Status includes expected date, loaded date, state, coverage, and a concise error category.

```text
Source       Expected date   Loaded date   Status
NSE EOD      07 Sep 2026     07 Sep 2026   Complete
NSE filings  07 Sep 2026     07 Sep 2026   Complete
AMFI NAV     07 Sep 2026     06 Sep 2026   Delayed
```

Operational categories include source unavailable, unexpected schema, invalid effective date, coverage regression, calculation failure, staging validation failure, publish failure, and free-tier budget warning. Detailed logs remain in the private pipeline environment and contain no secrets.

Daily reconciliation compares instrument counts, missing-data rates, extreme price/NAV changes, corporate actions, benchmark availability, and RS distributions with the previous successful day. Threshold breaches require review or a documented adapter rule before publication.

## Testing Strategy

### Unit and property tests

- Independently verified fixtures for returns, moving averages, volume ratios, drawdowns, benchmark RS, percentile RS, and composite momentum.
- Query tokenization, parsing, precedence, type checking, unknown propagation, unit handling, and SQL-injection resistance.
- Stable instrument identity across symbol and metadata changes.

### Contract tests

- Versioned representative NSE and AMFI source fixtures.
- Required columns, date formats, encodings, archive contents, and uniqueness constraints.
- Tests that unexpected upstream changes fail closed rather than publish incomplete data.

### Integration tests

- Ingest a small historical date range through the complete raw-to-staging flow.
- Verify normalization, factor calculations, lineage, checksums, reconciliation, and atomic promotion.
- Prove that an interrupted or invalid run leaves the last known-good dataset untouched.

### Browser tests

- Reject unauthenticated access.
- Create, validate, save, duplicate, and run a query.
- Inspect, sort, filter, and compare results.
- Open equity, ETF, and mutual-fund detail pages.
- Open contextual glossary content and its source links.
- Verify usable desktop and mobile layouts.

## Acceptance Criteria

- Three years of supported history are loaded within documented source permissions.
- Every active NSE `EQ` ordinary share and NSE-listed ETF has a stable instrument record.
- Every current AMFI scheme has a stable scheme record and current available NAV.
- The representative screening query returns independently verified matches.
- Both RS calculations match reference fixtures, and every score exposes its component values.
- Every displayed market or fundamental value has an effective date and source lineage.
- Failed or incomplete ingestion cannot replace the last known-good data.
- A typical saved screen returns within two seconds under personal-use load.
- Desktop and mobile views are usable and accessible by keyboard.
- Monitored daily work remains inside Cloudflare and GitHub free-tier allowances.
- Every route rejects unauthenticated access.

## Delivery Sequence

The project is implemented as four ordered subprojects, each leaving a testable vertical capability:

1. **Data foundation:** source-policy registry, provider adapters, raw storage, canonical identities, three-year backfill, validation, and staging publication.
2. **Analytics and screening:** normalized metric catalog, calculations, RS models, query parser/compiler, saved-query model, and reference tests.
3. **Private product:** authentication, API, overview, editor, results, instrument views, responsive layout, and pipeline-status experience.
4. **Education and hardening:** contextual glossary, authoritative source links, accessibility, security review, failure drills, free-tier monitoring, backup, and production launch checks.

The implementation plan must preserve this order because the UI and educational explanations depend on stable metric definitions and reliable data lineage.

## Source References

- [NSE daily reports](https://www.nseindia.com/all-reports/)
- [NSE corporate financial-result filings](https://www.nseindia.com/companies-listing/corporate-filings-financial-results)
- [NSE data usage and sharing policy](https://www.nseindia.com/static/market-data/nse-data-policy)
- [SEBI mutual-fund and AMFI resource directory](https://www.sebi.gov.in/curation/mutual_funds_and_rtas.html)
- [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [Cloudflare D1 pricing and free limits](https://developers.cloudflare.com/d1/platform/pricing/)
- [Cloudflare R2 pricing and free limits](https://developers.cloudflare.com/r2/pricing/)
- [GitHub Actions billing and included usage](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
