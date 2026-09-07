"""Configurable storage-budget telemetry shared by D1 and R2 stores."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageBudget:
    used_bytes: int
    limit_bytes: int | None
    warning_threshold: float = 0.8

    def __post_init__(self) -> None:
        if self.used_bytes < 0:
            raise ValueError("used bytes cannot be negative")
        if self.limit_bytes is not None and self.limit_bytes <= 0:
            raise ValueError("limit bytes must be positive")
        if not 0 < self.warning_threshold <= 1:
            raise ValueError("warning threshold must be between zero and one")

    @property
    def ratio(self) -> float | None:
        return None if self.limit_bytes is None else self.used_bytes / self.limit_bytes

    @property
    def used(self) -> int:
        return self.used_bytes

    @property
    def limit(self) -> int | None:
        return self.limit_bytes

    @property
    def warning(self) -> bool:
        return self.ratio is not None and self.ratio >= self.warning_threshold

    def as_dict(self) -> dict[str, int | float | bool | None]:
        return {
            "used": self.used_bytes,
            "limit": self.limit_bytes,
            "ratio": self.ratio,
            "warning": self.warning,
        }


__all__ = ["StorageBudget"]
