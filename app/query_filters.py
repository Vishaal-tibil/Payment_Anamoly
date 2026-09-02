"""Shared date-range filtering for the Insights pages' Date Range filter.

Every list/aggregate view that accepts start_date/end_date (plain
"YYYY-MM-DD" strings) narrows to rows whose own natural timestamp column
falls in that range, inclusive on both ends. There's no single shared
"transaction date" across every table here -- CanonicalEvent has a real
per-transaction timestamp (transaction_occurred_at), but EntitySnapshot/
BeneficiarySnapshot are windowed (window_end), and OperationalIssue/
ReconciliationBreak/AnalystReview/PaymentHealthScoreHistory are each
keyed by their own detected_at/reviewed_at/computed_at -- so each caller
filters on whichever column is that table's own honest "when" (see each
function's own docstring for which one it picked).
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_date_bound(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parses a "YYYY-MM-DD" (or full ISO8601) bound into an aware
    datetime. end_of_day=True pushes a bare date to 23:59:59.999999 so an
    end_date bound is inclusive of that whole calendar day, not just its
    first instant.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and len(value) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def string_date_bounds(start_date: str | None, end_date: str | None) -> tuple[str | None, str | None]:
    """For CanonicalEvent.transaction_occurred_at -- a plain ISO8601
    string column, not a real DateTime (see that model's docstring).
    Lexicographic comparison against ISO date strings already matches
    chronological order, so no parsing is needed here, just an inclusive
    end-of-day bound on end_date.
    """
    end = f"{end_date}T23:59:59.999999" if end_date and len(end_date) <= 10 else end_date
    return start_date, end
