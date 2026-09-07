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

- SQLite/D1 execution is intentionally not performed in this package because the workspace has no SQLite runtime dependency yet; SQL output is constrained and reference semantics are covered in the package tests. Task 9 can add a D1 integration parity test at the repository boundary.
- `typecheckQuery` emits a warning for a metric unavailable across all selected asset classes while preserving SQL `NULL` behavior, so such a metric cannot match a row with missing applicability data.
- `@stonks/contracts` is a workspace dependency of `@stonks/query`; the lockfile importer is updated.
