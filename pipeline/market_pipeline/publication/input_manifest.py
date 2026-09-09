"""Canonical raw-input manifests used to identify immutable datasets."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping, Sequence


class InputManifestError(ValueError):
    """Raised when dataset lineage cannot be stated unambiguously."""


_INPUT_FIELDS = (
    "source_id",
    "effective_date",
    "artifact_id",
    "checksum",
    "adapter_version",
    "raw_object_key",
)
_TIMESTAMP_FIELDS = {"generated_at", "retrieved_at", "fetched_at", "created_at", "updated_at"}


def _canonical_input(value: Mapping[str, str]) -> dict[str, Any]:
    missing = [field for field in _INPUT_FIELDS if not str(value.get(field, "")).strip()]
    if missing:
        raise InputManifestError(f"input is missing required lineage fields: {', '.join(missing)}")
    # Retrieval/generation timestamps explain job execution, not the bytes or
    # formula inputs used by a dataset. Deliberately omit them from its identity.
    semantic = {str(key): item for key, item in value.items() if str(key) not in _TIMESTAMP_FIELDS}
    return {key: semantic[key] for key in sorted(semantic)}


def canonical_inputs(inputs: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    """Return inputs in a stable order, rejecting ambiguous source/date revisions."""

    canonical = [_canonical_input(item) for item in inputs]
    by_source_date: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in canonical:
        by_source_date.setdefault((str(item["source_id"]), str(item["effective_date"])), []).append(item)
    ambiguous = [key for key, rows in by_source_date.items() if len(rows) > 1]
    if ambiguous:
        source_id, effective_date = sorted(ambiguous)[0]
        raise InputManifestError(
            f"{source_id}/{effective_date}: conflicting artifact revisions have no ordering rule"
        )
    return sorted(
        canonical,
        key=lambda item: tuple(str(item[field]) for field in _INPUT_FIELDS),
    )


def input_manifest(inputs: Sequence[Mapping[str, str]], versions: Mapping[str, str]) -> dict[str, Any]:
    """Build the persisted, timestamp-free lineage record for one candidate."""

    if not versions or any(not str(key).strip() or not str(value).strip() for key, value in versions.items()):
        raise InputManifestError("normalization, analytics, and projection versions are required")
    required_versions = {"normalization", "analytics", "projection"}
    missing = required_versions - {str(key) for key in versions}
    if missing:
        raise InputManifestError(f"missing formula/projection versions: {', '.join(sorted(missing))}")
    selected = canonical_inputs(inputs)
    return {
        "inputs": selected,
        "versions": {str(key): str(versions[key]) for key in sorted(versions, key=str)},
        "revision_selection": {
            "rule": "one immutable artifact per source_id/effective_date; ambiguous revisions block publication",
        },
    }


def dataset_fingerprint(inputs: Sequence[Mapping[str, str]], versions: Mapping[str, str]) -> str:
    """Hash all semantic raw inputs and calculation/projection versions."""

    payload = json.dumps(input_manifest(inputs, versions), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def manifest_with_fingerprint(inputs: Sequence[Mapping[str, str]], versions: Mapping[str, str]) -> dict[str, Any]:
    """Return the persisted manifest and the content hash that identifies it."""

    manifest = input_manifest(inputs, versions)
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**manifest, "fingerprint": sha256(payload.encode("utf-8")).hexdigest()}


__all__ = ["InputManifestError", "canonical_inputs", "dataset_fingerprint", "input_manifest", "manifest_with_fingerprint"]
