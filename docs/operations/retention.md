# Retention: publication bundles and derived-data cleanup

How historical partitions, publication bundles, and R2 storage cleanup fit
together (finding F14 / task R07). This is a design and operator reference,
not a script -- garbage collection here is always a dry-run list, never an
automatic delete.

## 1. Content-addressed history keys

Every yearly Parquet history partition is keyed as:

```
history/{asset_class}/{instrument_id}/{year}/{sha256}.parquet
```

`{sha256}` is the SHA-256 of the partition's own bytes
(`market_pipeline.storage.history_store.history_key`). Chart objects stay
dataset-scoped and unhashed: `charts/{dataset_id}/{instrument_id}.json.gz`,
because a chart is inherently a per-publication snapshot, not a historical
series that could be corrected in place.

Every write to `HistoryStore.write_history`/`write_chart` uses
`put_if_absent` -- there is no "replace" operation anywhere in the object
client. Consequences:

- **Same-year append.** Adding today's observation to the still-open current
  year changes that year's bytes, so it resolves to a *new* key. The previous
  day's key and its bytes are left exactly as they were; nothing is
  overwritten.
- **Closed-year correction.** Correcting a value in an already-closed year
  behaves identically: new bytes, new key, previous key untouched. Both
  objects remain individually readable.
- **Exact retry.** Retrying a candidate with byte-identical inputs resolves
  to the same key every time (`put_if_absent` on an existing, byte-identical
  object is a safe no-op).
- **Failed publication.** If a candidate's history/chart writes fail partway,
  whatever was already written is a harmless orphan under its own hash-keyed
  path. The active dataset pointer never moves until every object for the
  candidate is written and the pointer swap runs last
  (`publication/d1_export.py`), so a failed or partial candidate can never be
  observed as active, and it can never change what an existing key resolves
  to.

## 2. The publication bundle

`market_pipeline.publication.bundle.PublicationBundle` (version 1) is the
single authoritative description of what one candidate needs:

```json
{
  "version": 1,
  "dataset_id": "amfi-nav-2026-09-01-<fingerprint>",
  "input_manifest_hash": "<sha256 of R02's input manifest>",
  "source_dates": ["2026-09-01"],
  "projection_version": "d1-projection-v1",
  "sql_checksum": "<sha256 of the exported D1 import SQL, filled in at export time>",
  "objects": [
    {"key": "history/mutual_fund/<id>/2026/<sha256>.parquet", "sha256": "...", "bytes": 12345, "kind": "history"},
    {"key": "charts/<dataset_id>/<id>.json.gz", "sha256": "...", "bytes": 456, "kind": "chart"}
  ]
}
```

The bundle is built in two steps:

1. **Candidate time** (`jobs/publish.py`, `publish_checkpointed_dataset`):
   after `HistoryStore.write_history`/`write_chart` return their written
   object refs, `bundle.build_bundle` restricts them to the rolling
   **three-calendar-year serving window** (the effective date's year and the
   two years before it -- `bundle.serving_years`) and stores the result as
   `candidate["metadata"]["publication_bundle"]`, which is persisted
   verbatim into the `datasets.metadata_json` column alongside every other
   dataset. History outside the window is still durably written (and still
   read during analytics, as a "calculation boundary observation" -- e.g. the
   trailing data a 12-month return needs) but is not part of what this bundle
   asks the export/sync layers to serve.
2. **Export time** (`publication/d1_export.py`, `export_active_dataset`):
   the exporter reads the active dataset's own recorded bundle back out of
   `datasets.metadata_json` (`_load_bundle`) and byte-verifies exactly those
   objects against the local history root (`_verify_bundle_objects`) --
   **it never globs the history root to discover what's required.** Once the
   D1 import SQL is generated, the bundle is finalized with
   `bundle.sql_checksum(sql_text)` and written to `--object-manifest` as the
   full bundle document. This is what closes F14's second half: a stale or
   unrelated object left over from another dataset can never be picked up as
   "required" just because it happens to exist under the history root.

## 3. Rollback: keeping bundles reachable

A dataset row is never deleted -- `D1Publisher.promote`/`export_active_dataset`
only ever flip `status` between `active` and `superseded`. Because each
dataset's own bundle travels with it in `metadata_json`, the previous
successfully-promoted dataset's bundle stays reachable for as long as its row
does, which is exactly what rollback (`docs/operations/recovery.md`, section
2) needs: after repointing `active_dataset`, the "new" active dataset's
bundle (still `metadata_json.publication_bundle` on that row) is immediately
the authoritative object list again, without any separate bundle store.

`publication/d1_export.py`'s `_reachable_bundles` returns the active dataset's
bundle plus the most recently superseded dataset's bundle by default. If a
saved run (owner-managed, immutable per R05/R06) pins an older dataset's
history for display, extend the reachable set passed to
`bundle.plan_garbage_collection` to include that dataset's bundle too, before
running any cleanup pass -- retention must follow what the product actually
exposes, not just the two most recent promotions.

## 4. Garbage collection: dry-run only

`bundle.plan_garbage_collection(reachable_bundles, existing_objects)` lists
every object under `history/` or `charts/` that is present in the store but
referenced by none of `reachable_bundles`. It is intentionally a **pure
list**, not a deletion:

- Only `history/` and `charts/` keys are ever candidates. Raw artifacts
  (`raw/...`, `market_pipeline.storage.raw_store`) are never proposed for
  deletion by this function, under any circumstance -- storage quota pressure
  is never a reason to discard the auditable raw record.
- Every export run that supplies a `history_root`/`object_manifest` also
  computes this plan (via `d1_export._existing_object_sizes` and
  `_reachable_bundles`) and folds its counts into `--publication-plan` under
  `retained_objects`, `retained_bytes`, and `garbage_collection` (itself
  `{dry_run: true, candidate_count, candidate_bytes, candidates: [...]}`), so
  the same budget artifact R09 validates also shows what cleanup would
  reclaim.
- Actually deleting the listed candidates is a separate, explicit operator
  action outside this pipeline (or a future task's explicit opt-in) -- review
  the dry-run list, confirm nothing on it is still referenced by a saved run
  you care about, and only then delete.

## 5. Operator checklist

1. After a daily/backfill run, confirm `publication["dataset_id"]` and
   `history_objects > 0` in the run's JSON output.
2. Before a remote export, run `export_active_dataset` with `--history-root`
   and `--object-manifest`; if it raises `DatasetExportError` mentioning "no
   recorded publication bundle" or "does not match its recorded checksum",
   stop -- do not hand-construct a manifest, fix the candidate and re-publish
   locally.
3. Inspect `--publication-plan`'s `garbage_collection.candidates` before any
   cleanup pass. An empty list is normal on a fresh store; a growing list
   over many days is expected as older intra-year partitions age out of the
   rolling three-year window once superseded datasets are pruned by policy.
4. Never delete a `candidates` entry that also appears in the *previous*
   promoted dataset's bundle (`_reachable_bundles` already excludes these,
   but a hand-run cleanup script must not skip this check) or in any saved
   run's referenced history.
