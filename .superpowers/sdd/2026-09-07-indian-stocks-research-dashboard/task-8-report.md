# Task 8 implementation report — Safe Screener-Style Language

## Scope

Implemented the shared metric/screen contracts and a closed-world Screener-style query package. The language supports case-insensitive, longest-match metric aliases; Boolean operators (`AND`, `OR`, `NOT`); comparisons; arithmetic; parentheses; numeric and percent literals; spans; structured diagnostics; catalog/class checking; parameterized SQL compilation; and a tri-state reference evaluator.

Unknown/unavailable values and division by zero propagate as `unknown`. SQL identifiers are emitted only after catalog lookup and identifier validation; every literal is a bound parameter, division uses `NULLIF`, and the compiled root predicate is wrapped in `IS TRUE`. No SQL tokens, arbitrary function calls, comments, or raw SQL escape hatch are accepted.

## TDD evidence

Initial focused run before implementation:

```text
pnpm --dir packages/query exec vitest run --config vitest.config.ts
No test files found
```

After adding the failing tests, the empty package run failed with `parseQuery is not a function` for all seven focused cases. The implementation then made the focused suite green.

## Verification

```text
CI=true pnpm test                         # 3 files, 10 tests passed
CI=true pnpm typecheck                    # all workspace packages passed
CI=true pnpm lint                         # all workspace packages passed
git diff --check                          # passed
```

The focused query suite includes precedence/alias tests, tri-state division and missing-value tests, parameter binding and allowlisted SQL tests, unknown-field suggestions, malformed/SQL-injection payloads, and malicious catalog-column rejection. The contracts suite verifies the public metric aliases.

## Notes / concerns

- Fix round 1 added a tested EAV compiler (`compileEavQuery`) for Task 7's `latest_metrics` relation. It emits correlated, catalog-only metric lookups for `instruments AS i`, binds dataset and metric identifiers as parameters, and was executed against SQLite's CLI in the query tests. A bounded generated-row test compares SQLite results with `evaluateQuery`, including null and division-by-zero unknowns.
- Fix round 1 added unit checking. Canonical percent metric storage follows Task 6/7 and is fractional (`0.03` = 3%); percent literals normalize to fractions (`3%` binds `0.03`), while bare numeric literals remain dimensionless thresholds. Incompatible units such as `Volume > 10%` are rejected, while `Return over 1day > 3%` is valid.
- A SQLite runtime is not added as an npm dependency; the executable contract uses the system SQLite-compatible CLI in tests and remains at the D1 schema boundary for Task 9.
- `typecheckQuery` emits a warning for a metric unavailable across all selected asset classes while preserving SQL `NULL` behavior, so such a metric cannot match a row with missing applicability data.
- `@stonks/contracts` is a workspace dependency of `@stonks/query`; the lockfile importer is updated.

## Review round 1

Addressed all four findings: unit semantics are explicit and tested; EAV SQL is executable and safely parameterized; generated rows establish evaluator/SQLite truth parity; and bounded malformed-token plus identifier-payload cases supplement the static injection cases.

Ruling: retain both wide-projection `compileQuery` and explicit `compileEavQuery`; the latter makes the EAV correlation contract visible and prevents callers from accidentally embedding a predicate without the required `instruments AS i` context. Cost if wrong: callers must choose the correct relation explicitly, but an ambiguous default could produce invalid or dangerously broad D1 queries.

Review round 2: constrained the outer EAV instrument row to the selected dataset and made dataset binding the first parameter. Replaced the earlier one-metric parity fixture with two metrics, two datasets sharing an instrument ID, true/false/missing outcomes, and a separately executed division-by-zero query. Corrected percentage storage to fractional returns and added end-to-end `0.03` assertions.
