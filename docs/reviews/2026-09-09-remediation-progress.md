# Correctness and publication remediation progress

| task | finding IDs | base | head | focused checks | review round | open findings | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R01: validate the candidate and reconcile actual source coverage | F01, F02 | `1469b1c` | `9f102c3` | `.venv/bin/python -m pytest pipeline/tests/integration/test_candidate_gate.py pipeline/tests/integration/test_cli.py pipeline/tests/integration/test_publication.py pipeline/tests/validation/test_reconcile.py -q` (59 passed); `.venv/bin/python -m pytest pipeline/tests -q` (209 passed); `git diff --check` | 2 review/fix rounds | Deferred Minor: source-calendar observation (named AMFI market-holiday calendar remains future work) | DONE_WITH_DEFERRED_MINOR |
