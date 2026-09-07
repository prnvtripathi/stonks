"""Labeled storage-budget reporting for the daily-run safety checks.

This module intentionally does not duplicate threshold math: it wraps the
existing :class:`market_pipeline.storage.budgets.StorageBudget`, which already
owns the used/limit/ratio/warning computation shared by D1 and R2 stores. The
addition here is a ``source`` label (so an operator report can show "d1" vs
"r2" side by side) and an ``exceeded`` flag for values at or over the hard
limit, which is distinct from ``warning`` (at/over the softer 80% threshold).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from market_pipeline.storage.budgets import StorageBudget


@dataclass(frozen=True)
class BudgetReport:
    source: str
    budget: StorageBudget

    @property
    def used(self) -> int:
        return self.budget.used_bytes

    @property
    def limit(self) -> int | None:
        return self.budget.limit_bytes

    @property
    def ratio(self) -> float | None:
        return self.budget.ratio

    @property
    def warning(self) -> bool:
        return self.budget.warning

    @property
    def exceeded(self) -> bool:
        """True once usage has reached or passed the hard limit (ratio >= 1.0)."""

        return self.budget.ratio is not None and self.budget.ratio >= 1.0

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "exceeded": self.exceeded, **self.budget.as_dict()}


def budget_report(
    used: int,
    limit: int | None,
    *,
    warning_threshold: float = 0.8,
    source: str = "default",
) -> BudgetReport:
    """Build a labeled :class:`BudgetReport` around the shared :class:`StorageBudget`."""

    return BudgetReport(source=source, budget=StorageBudget(used, limit, warning_threshold))


def budget_reports(
    usage: Mapping[str, tuple[int, int | None]],
    *,
    warning_threshold: float = 0.8,
) -> tuple[BudgetReport, ...]:
    """Build one labeled report per ``source -> (used_bytes, limit_bytes)`` entry."""

    return tuple(
        budget_report(used, limit, warning_threshold=warning_threshold, source=source)
        for source, (used, limit) in usage.items()
    )


__all__ = ["BudgetReport", "budget_report", "budget_reports"]
