"""Immutable, compact R2 history objects and private chart snapshots."""

from __future__ import annotations

import gzip
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from market_pipeline.storage.budgets import StorageBudget


class HistoryStoreError(RuntimeError):
    """Raised for malformed keys or attempts to overwrite history."""


class HistoryObjectClient(Protocol):
    def put_if_absent(self, key: str, body: bytes) -> bool: ...

    def get(self, key: str) -> bytes | None: ...



class UsageReportingObjectClient(HistoryObjectClient, Protocol):
    """Optional R2 client extension for provider-reported byte usage."""

    def usage_bytes(self) -> int: ...


class _LocalObjectClient:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise HistoryStoreError("object key escapes history root") from exc
        return path

    def put_if_absent(self, key: str, body: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(body)
            return True
        except FileExistsError:
            if path.read_bytes() != body:
                raise HistoryStoreError(f"immutable history object differs: {key}")
            return False

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None


def _safe(value: Any) -> Any:
    if isinstance(value, (date, datetime, UUID)):
        return value.isoformat() if isinstance(value, (date, datetime)) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _row_date(row: Mapping[str, Any]) -> date:
    value = row.get("effective_date", row.get("date"))
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise HistoryStoreError(f"history row has invalid date: {value!r}") from exc
    raise HistoryStoreError("history row requires effective_date or date")


def history_key(asset_class: str, instrument_id: str | UUID, year: int) -> str:
    asset = str(asset_class).lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]+", asset):
        raise HistoryStoreError("asset class contains unsupported characters")
    instrument = str(instrument_id)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", instrument):
        raise HistoryStoreError("instrument ID contains unsupported characters")
    if year < 1900 or year > 2200:
        raise HistoryStoreError("history year is outside supported range")
    return f"history/{asset}/{instrument}/{year}.parquet"


def chart_key(dataset_id: str, instrument_id: str | UUID) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", dataset_id):
        raise HistoryStoreError("dataset ID contains unsupported characters")
    instrument = str(instrument_id)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", instrument):
        raise HistoryStoreError("instrument ID contains unsupported characters")
    return f"charts/{dataset_id}/{instrument}.json.gz"


class HistoryStore:
    """Write immutable yearly Parquet and dataset-scoped gzip chart objects."""

    def __init__(
        self,
        target: HistoryObjectClient | str | Path,
        *,
        budget_limit_bytes: int | None = None,
        budget_warning_threshold: float = 0.8,
        usage_provider: Callable[[], int] | None = None,
    ) -> None:
        self.client: HistoryObjectClient = (
            _LocalObjectClient(Path(target)) if isinstance(target, (str, Path)) else target
        )
        self.budget_limit_bytes = budget_limit_bytes
        self.budget_warning_threshold = budget_warning_threshold
        self.usage_provider = usage_provider

    def budget_report(self) -> StorageBudget:
        if self.usage_provider is not None:
            used = self.usage_provider()
        elif isinstance(self.client, _LocalObjectClient):
            used = sum(path.stat().st_size for path in self.client.root.rglob("*") if path.is_file())
        else:
            provider = getattr(self.client, "usage_bytes", None)
            if not callable(provider):
                provider = getattr(self.client, "get_usage_bytes", None)
            used = int(provider()) if callable(provider) else 0
        return StorageBudget(used, self.budget_limit_bytes, self.budget_warning_threshold)

    storage_budget = budget_report

    def put_if_absent(self, key: str, body: bytes) -> str:
        if not self.client.put_if_absent(key, body):
            existing = self.client.get(key)
            if existing != body:
                raise HistoryStoreError(f"immutable history object differs: {key}")
        return key

    def write_history(
        self,
        asset_class: str,
        instrument_id: str | UUID,
        records: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        if not records:
            return []
        grouped: dict[int, list[dict[str, Any]]] = {}
        for record in records:
            row = _safe(record)
            grouped.setdefault(_row_date(record).year, []).append(row)
        keys: list[str] = []
        for year in sorted(grouped):
            rows = sorted(grouped[year], key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
            try:
                table = pa.Table.from_pylist(rows)
                output = pa.BufferOutputStream()
                pq.write_table(
                    table,
                    output,
                    compression="zstd",
                    compression_level=3,
                    use_dictionary=False,
                    write_statistics=False,
                    version="2.6",
                )
                body = output.getvalue().to_pybytes()
            except (pa.ArrowException, TypeError, ValueError) as exc:
                raise HistoryStoreError("history records cannot be encoded as Parquet") from exc
            keys.append(self.put_if_absent(history_key(asset_class, instrument_id, year), body))
        return keys

    def read_history(self, asset_class: str, instrument_id: str | UUID, year: int) -> list[dict[str, Any]]:
        key = history_key(asset_class, instrument_id, year)
        body = self.client.get(key)
        if body is None:
            raise KeyError(key)
        try:
            table = pq.read_table(io.BytesIO(body))
            return [dict(row) for row in table.to_pylist()]
        except (pa.ArrowException, OSError) as exc:
            raise HistoryStoreError(f"history Parquet object is invalid: {key}") from exc

    def write_chart(self, dataset_id: str, instrument_id: str | UUID, chart: Any) -> str:
        payload = json.dumps(_safe(chart), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        output = io.BytesIO()
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as handle:
            handle.write(payload)
        return self.put_if_absent(chart_key(dataset_id, instrument_id), output.getvalue())

    def read_chart(self, dataset_id: str, instrument_id: str | UUID) -> Any:
        key = chart_key(dataset_id, instrument_id)
        body = self.client.get(key)
        if body is None:
            raise KeyError(key)
        try:
            return json.loads(gzip.decompress(body))
        except (OSError, json.JSONDecodeError) as exc:
            raise HistoryStoreError(f"chart object is invalid: {key}") from exc


class LocalHistoryStore(HistoryStore):
    """Filesystem-backed history store, useful for local jobs and tests."""

    def __init__(self, root: str | Path, *, budget_limit_bytes: int | None = None, budget_warning_threshold: float = 0.8, usage_provider: Callable[[], int] | None = None) -> None:
        super().__init__(root, budget_limit_bytes=budget_limit_bytes, budget_warning_threshold=budget_warning_threshold, usage_provider=usage_provider)


class R2HistoryStore(HistoryStore):
    """History store backed by an injected S3/R2-compatible object client."""

    def __init__(self, client: HistoryObjectClient, *, budget_limit_bytes: int | None = None, budget_warning_threshold: float = 0.8, usage_provider: Callable[[], int] | None = None) -> None:
        super().__init__(client, budget_limit_bytes=budget_limit_bytes, budget_warning_threshold=budget_warning_threshold, usage_provider=usage_provider)


__all__ = [
    "HistoryObjectClient",
    "UsageReportingObjectClient",
    "HistoryStore",
    "HistoryStoreError",
    "LocalHistoryStore",
    "R2HistoryStore",
    "chart_key",
    "history_key",
]
