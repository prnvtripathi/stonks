"""Governed NSE EOD adapter and safe archive handling.

The adapter is intentionally disabled by the canonical source registry.  An
operator may still parse an official download locally, and a future licensed
provider can be injected without adding a network client to this package.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import PurePosixPath

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.normalization.nse import (
    NseParsedRow,
    NseRowError,
)
from market_pipeline.normalization.nse import (
    parse_nse_bhavcopy as _parse_nse_bhavcopy,
)
from market_pipeline.normalization.nse import (
    parse_nse_security_master as _parse_nse_security_master,
)
from market_pipeline.sources.base import SourceAdapter
from market_pipeline.sources.registry import get_source_policy


class NseArchiveError(ValueError):
    """Raised when an NSE archive is unsafe or structurally invalid."""


def parse_nse_security_master(body: bytes | str) -> list[NseParsedRow]:
    """Parse a security master and present schema errors as archive errors."""

    try:
        if isinstance(body, bytes) and body[:2] == b"PK":
            body = read_nse_archive(body)
        return _parse_nse_security_master(body)
    except NseRowError as exc:
        raise NseArchiveError(str(exc)) from exc


def parse_nse_bhavcopy(body: bytes | str) -> list[NseParsedRow]:
    """Parse a bhavcopy CSV/ZIP and convert schema failures to archive errors."""

    try:
        if isinstance(body, bytes) and body[:2] == b"PK":
            body = read_nse_archive(body)
        return _parse_nse_bhavcopy(body)
    except NseRowError as exc:
        raise NseArchiveError(str(exc)) from exc


MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_MEMBER_BYTES = 25 * 1024 * 1024
MAX_MEMBERS = 16


def _safe_members(body: bytes) -> list[zipfile.ZipInfo]:
    if len(body) > MAX_ARCHIVE_BYTES:
        raise NseArchiveError("NSE archive exceeds maximum size")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as exc:
        raise NseArchiveError("NSE artifact is not a valid ZIP archive") from exc
    members = archive.infolist()
    if not members or len(members) > MAX_MEMBERS:
        raise NseArchiveError("NSE archive has an invalid member count")
    names: set[str] = set()
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or name in names
            or member.is_dir()
            or member.file_size > MAX_MEMBER_BYTES
            or member.flag_bits & 0x1
        ):
            raise NseArchiveError(f"unsafe NSE archive member: {name!r}")
        names.add(name)
    archive.close()
    return members


def read_nse_archive(body: bytes, *, expected_member: str | None = None) -> bytes:
    """Read one safe CSV member from an operator-supplied NSE ZIP."""

    members = _safe_members(body)
    csv_members = [member for member in members if member.filename.lower().endswith((".csv", ".txt"))]
    if expected_member is not None:
        csv_members = [member for member in csv_members if member.filename == expected_member]
    if len(csv_members) != 1:
        raise NseArchiveError("NSE archive must contain exactly one selected CSV/TXT member")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return archive.read(csv_members[0])


def parse_nse_archive(body: bytes) -> list[NseParsedRow]:
    """Parse a safe one-report ZIP or a plain bhavcopy CSV."""

    if body[:2] == b"PK":
        body = read_nse_archive(body)
    return parse_nse_bhavcopy(body)


class _NseEodImplementation:
    source_id = "nse-eod"
    adapter_version = "1.0.0"
    policy = get_source_policy("nse-eod")

    def __init__(self, provider: Callable[[date], bytes | FetchedArtifact] | None) -> None:
        self.provider = provider

    def _fetch(self, effective_date: date) -> list[FetchedArtifact]:
        if self.provider is None:
            raise NseArchiveError("NSE EOD requires an operator-supplied official artifact")
        supplied = self.provider(effective_date)
        if isinstance(supplied, FetchedArtifact):
            return [supplied]
        body = supplied
        artifact = SourceArtifact(
            source_id=self.source_id,
            source_url=self.policy.source_url,
            retrieved_at=datetime.now(timezone.utc),
            effective_date=effective_date,
            checksum=sha256(body).hexdigest(),
            adapter_version=self.adapter_version,
            terms_url=self.policy.terms_url,
            filename=f"nse-eod-{effective_date.isoformat()}.zip",
        )
        return [FetchedArtifact(artifact=artifact, body=body)]


class NseEodAdapter(SourceAdapter):
    """Policy-gated adapter; public fetch remains disabled until permission exists."""

    def __init__(self, provider: Callable[[date], bytes | FetchedArtifact] | None = None) -> None:
        super().__init__(_NseEodImplementation(provider))


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_MEMBER_BYTES",
    "NseArchiveError",
    "NseEodAdapter",
    "parse_nse_archive",
    "parse_nse_bhavcopy",
    "parse_nse_security_master",
    "read_nse_archive",
]
