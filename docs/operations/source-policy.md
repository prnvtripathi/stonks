# Market data source policy

The pipeline automates only the official public artifacts listed below. It does not
scrape undocumented website endpoints or use third-party market-data services.

| Source ID | Official artifacts | Automation |
| --- | --- | --- |
| `nse-eod` | NSE daily reports for end-of-day prices, volume, delivery, security, ETF and related market files | Enabled only while the terms reference permits private automation |
| `nse-filings-xbrl` | NSE corporate filings and available CSV/XBRL artifacts | Enabled only while the terms reference permits private automation |
| `amfi-nav` | AMFI daily and historical mutual-fund NAV data | Enabled |
| `nifty-500` | Official Nifty 500 closing values | Enabled only while the terms reference permits private automation |

Each adapter records its source URL, retrieval timestamp, effective date, SHA-256
checksum, adapter version and terms reference on every artifact. Raw bytes are
immutable. Repeating an identical write is idempotent; attempting to write different
bytes under an existing object key fails.

If a source's usage or retention terms do not permit the intended private automation,
its policy must be disabled. The pipeline must fail closed with a `SourcePolicyError`;
it must not substitute an undocumented endpoint or a third-party feed. Terms and
retention decisions must be reviewed before enabling an adapter in production.

## Acquisition mode: network fetch versus a supplied file

Every `SourceArtifact` records an `acquisition_mode` of either `"network"` (the
default, and the only mode that existed before this section was added) or
`"supplied"`. These are two different permission questions, checked separately:

- **`"network"`** — the artifact was produced by a live `SourceAdapter.fetch()`
  call. Admission requires `SourcePolicy.automation_allowed=True` (and
  `retention_allowed=True`, and a recorded `permission_reference`), exactly as
  before this section. `nse-eod` and `nse-filings-xbrl` both have
  `automation_allowed=False` today, so network-mode admission for either
  remains denied — this document does not change that.
- **`"supplied"`** — the artifact was provided by an operator (a locally
  downloaded, manually saved official file) and was never fetched over the
  network by this pipeline. Admission requires a *separate* permission,
  `SourcePolicy.supplied_use_allowed=True`, plus a matching
  `supplied_use_permission_reference`. **No source in the canonical registry
  (`SOURCE_POLICIES` in `sources/registry.py`) has this permission recorded
  today.** A real NSE file an operator has downloaded by hand is therefore
  still not admitted in production until an operator/owner explicitly records
  that decision on the registry entry — do not interpret "an operator saved
  this file manually" as an automatically-licensed use.

Both modes still require an artifact's `source_url` to fall within the
policy's `approved_url_prefixes` and its `terms_url` to match exactly; a
supplied artifact must also cite the specific permission that authorized it
via `permission_record_id`, which must equal the policy's
`supplied_use_permission_reference`.

`sources/registry.py` exposes `admit(artifact, policy) -> SourceArtifact` as
the mode-aware admission entry point: it validates provenance and permission
without ever fetching or writing bytes. `LocalRawStore.put`/`R2RawStore.put`
call it (via `get_source_policy`) before ever writing to the immutable raw
store, so a bad acquisition-mode/permission combination fails before any
object is created. `assert_artifact_policy(..., acquisition_mode=...)`
remains available as a narrower, keyword-based variant used by adapters that
do not construct a full `SourceArtifact` up front (its default
`acquisition_mode="network"` matches this function's original, pre-existing
behavior, so no caller that predates this section needs to change).

### Supplied-artifact roles (`jobs/source_inputs.py`)

A source ID alone does not say what an artifact contains for a given run —
NSE alone publishes a security master, EOD observations, filings, and
corporate actions as related but distinct official downloads.
`jobs/source_inputs.py` defines `SourceInputRole` (`security_master`,
`eod_observations`, `filings`, `corporate_actions`, `benchmark_observations`)
and a `SourceInput` record tying one admitted artifact's role to its coverage
dates (`expected_date`/`loaded_date`), lineage (`artifact_id`, `checksum`,
`object_key`, `adapter_version`), and `acquisition_mode`.

`resolve_supplied_source_input(...)` is the supplied-file counterpart to a
network adapter's `fetch()`: given an explicit `manifest_root` directory, a
`relative_path` naming the file within it, and a resolved `SourcePolicy`, it

1. resolves `relative_path` strictly within `manifest_root` (rejecting any
   path that escapes it, and any entry whose file does not exist — a missing
   companion file is rejected before admission is even attempted);
2. rejects a manifest entry whose declared `effective_date` does not match
   the job's requested `expected_date`;
3. builds the artifact's `SourceArtifact` from the file's real bytes (so its
   checksum cannot itself be "wrong" — it is always computed from what was
   actually read) and admits it via `admit(...)`;
4. returns the admitted artifact, its bytes, and a role-tagged `SourceInput`.

`require_roles(inputs, required_roles)` checks that every required role is
present in a run's resolved inputs before that run is check-pointed —  it
does not fetch, admit, or write anything itself.

Example manifest entry describing one supplied EOD file (illustrative; the
actual JSON shape read by a manifest loader may differ):

```json
{
  "source_id": "nse-eod",
  "role": "eod_observations",
  "relative_path": "2026-09-07/cm07SEP2026bhav.csv.zip",
  "expected_date": "2026-09-07",
  "source_url": "https://archives.nseindia.com/content/historical/EQUITIES/2026/SEP/cm07SEP2026bhav.csv.zip",
  "terms_url": "https://www.nseindia.com/terms-of-use",
  "adapter_version": "1.0.0",
  "permission_record_id": "<the recorded supplied-use permission reference — none exists for nse-eod today>"
}
```

This example is deliberately not admittable today: `nse-eod`'s canonical
policy has `supplied_use_allowed=False`, so `resolve_supplied_source_input`
raises `SourcePolicyError` for it regardless of how well-formed the manifest
entry and file are. Only a test-injected `SourcePolicy` fixture (never a
change to `SOURCE_POLICIES`) can demonstrate a `"supplied"` artifact actually
being admitted; see
`pipeline/tests/integration/test_supplied_source_admission.py`.
