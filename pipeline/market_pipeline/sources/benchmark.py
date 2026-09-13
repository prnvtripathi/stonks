"""Parser for admitted official Nifty 500 benchmark observations, and loader
for the reviewed instrument/category -> benchmark mapping file.

Nifty Indices (``nifty-500`` in the registry) already has a recorded
automation permission (``automation_allowed=True``), but no network-fetch
adapter exists yet -- this module only parses an artifact that has already
passed S01's ``admit()`` gate (or, directly, a hand-built ``SourceArtifact``
for a unit test), exactly like every other source in this pipeline that has
no wired network adapter.

The reviewed mapping file (``content/benchmarks/mappings.json``) is a
human-curated record of which instrument or category compares against which
official benchmark ID, over what valid date range, citing a reviewed official
source. An empty mapping file (``[]``) is valid and simply means every lookup
finds nothing: R03 already removed a fabricated category-composite
MF-benchmark substitution, and this module must never reintroduce an
equivalent "helpful" fallback -- no mapping means the caller reports
``missing``, honestly, not a guessed value.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from market_pipeline.domain.models import SourceArtifact
from market_pipeline.sources.registry import (
    SOURCE_POLICIES,
    SourcePolicyError,
    assert_source_url_allowed,
)

BENCHMARK_SOURCE_ID = "nifty-500"
DEFAULT_BENCHMARK_ID = "NIFTY500"


class BenchmarkProvenanceError(ValueError):
    """Raised when a benchmark artifact is not demonstrably official."""


class BenchmarkMappingError(ValueError):
    """Raised when the reviewed mapping file is structurally invalid."""


@dataclass(frozen=True)
class BenchmarkObservation:
    """One official benchmark closing level on one calendar date."""

    benchmark_id: str
    observation_date: date
    close: Decimal
    source_artifact_id: UUID | None


@dataclass(frozen=True)
class BenchmarkMapping:
    """One reviewed instrument/category -> official benchmark record."""

    identifier: str
    identifier_type: str  # "instrument" | "category"
    benchmark_id: str
    valid_from: date
    valid_to: date | None
    source_reference: str

    def covers(self, effective_date: date) -> bool:
        if effective_date < self.valid_from:
            return False
        if self.valid_to is not None and effective_date > self.valid_to:
            return False
        return True


def _validate_provenance(artifact: SourceArtifact) -> None:
    try:
        policy = SOURCE_POLICIES[BENCHMARK_SOURCE_ID]
        if artifact.source_id != policy.source_id:
            raise SourcePolicyError("benchmark artifact has an unexpected source")
        assert_source_url_allowed(artifact.source_id, artifact.source_url)
        if artifact.terms_url != policy.terms_url:
            raise SourcePolicyError("benchmark artifact terms reference is not canonical")
    except (KeyError, SourcePolicyError) as exc:
        raise BenchmarkProvenanceError(str(exc)) from exc


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported benchmark observation date format: {value!r}")


def parse_nifty500_observations(
    body: bytes | str,
    *,
    source_artifact: SourceArtifact,
    benchmark_id: str = DEFAULT_BENCHMARK_ID,
) -> list[BenchmarkObservation]:
    """Parse an admitted, official Nifty 500 daily-closing-level CSV.

    Expected columns (case-insensitive; a couple of official-report aliases
    tolerated): ``Date``/``Index Date`` and ``Close``/``Closing Index Value``.
    Every row's date/close must parse strictly -- a malformed row raises
    rather than being silently skipped or coerced into a fabricated value.
    """

    raw = body.encode("utf-8") if isinstance(body, str) else body
    if source_artifact.checksum.lower() != sha256(raw).hexdigest().lower():
        raise BenchmarkProvenanceError("benchmark artifact checksum does not match supplied bytes")
    _validate_provenance(source_artifact)
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("benchmark observation CSV is missing a header")
    normalized = {key.strip().lower(): key for key in reader.fieldnames if key is not None}
    date_col = normalized.get("date") or normalized.get("index date")
    close_col = (
        normalized.get("close")
        or normalized.get("closing index value")
        or normalized.get("close price")
    )
    if not date_col or not close_col:
        raise ValueError("benchmark observation CSV requires Date and Close columns")
    observations: list[BenchmarkObservation] = []
    for row in reader:
        raw_date = (row.get(date_col) or "").strip()
        raw_close = (row.get(close_col) or "").strip()
        if not raw_date or not raw_close:
            raise ValueError(f"benchmark observation row is missing date/close: {row!r}")
        observation_date = _parse_date(raw_date)
        try:
            close = Decimal(raw_close)
        except InvalidOperation as exc:
            raise ValueError(f"benchmark observation close is not numeric: {raw_close!r}") from exc
        observations.append(
            BenchmarkObservation(
                benchmark_id=benchmark_id,
                observation_date=observation_date,
                close=close,
                source_artifact_id=source_artifact.artifact_id,
            )
        )
    return observations


def load_benchmark_mappings(path: str | Path) -> tuple[BenchmarkMapping, ...]:
    """Load and validate the reviewed instrument/category -> benchmark file.

    An empty file (a JSON array, ``[]``) is valid: every downstream lookup
    then legitimately finds no mapping, and benchmark RS reports ``missing``
    rather than a fabricated substitute (see R03).
    """

    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkMappingError(f"benchmark mapping file is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise BenchmarkMappingError("benchmark mapping file must be a JSON array")
    mappings: list[BenchmarkMapping] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise BenchmarkMappingError("benchmark mapping entry must be an object")
        try:
            identifier = str(entry["identifier"])
            identifier_type = str(entry["identifier_type"])
            benchmark_id = str(entry["benchmark_id"])
            valid_from = date.fromisoformat(str(entry["valid_from"]))
            valid_to_raw: Any = entry.get("valid_to")
            valid_to = date.fromisoformat(str(valid_to_raw)) if valid_to_raw else None
            source_reference = str(entry["source_reference"])
        except (KeyError, ValueError) as exc:
            raise BenchmarkMappingError(f"benchmark mapping entry is malformed: {entry!r} ({exc})") from exc
        if identifier_type not in ("instrument", "category"):
            raise BenchmarkMappingError(f"unsupported benchmark mapping identifier_type: {identifier_type!r}")
        if not source_reference.startswith("https://"):
            raise BenchmarkMappingError("benchmark mapping source_reference must be an official https URL")
        if valid_to is not None and valid_to < valid_from:
            raise BenchmarkMappingError(f"benchmark mapping valid_to precedes valid_from: {entry!r}")
        mappings.append(
            BenchmarkMapping(
                identifier=identifier,
                identifier_type=identifier_type,
                benchmark_id=benchmark_id,
                valid_from=valid_from,
                valid_to=valid_to,
                source_reference=source_reference,
            )
        )
    return tuple(mappings)


__all__ = [
    "BENCHMARK_SOURCE_ID",
    "DEFAULT_BENCHMARK_ID",
    "BenchmarkMapping",
    "BenchmarkMappingError",
    "BenchmarkObservation",
    "BenchmarkProvenanceError",
    "load_benchmark_mappings",
    "parse_nifty500_observations",
]
