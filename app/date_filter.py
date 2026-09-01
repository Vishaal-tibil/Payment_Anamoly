"""Shared date-range filtering for the Insights tab's Date Range filter.

Two real date representations exist in this schema and need two different
filter strategies:

- CanonicalEvent.transaction_occurred_at is a String/ISO8601 column (see
  its own docstring in app/models.py for why) -- lexicographic string
  comparison matches chronological order for it, the same assumption
  app/dashboard.py::get_overview already relies on for its MIN/MAX
  date_range_start/end.
- EntitySnapshot.window_start/window_end are real DateTime(timezone=True)
  columns -- ordinary date/datetime comparison applies.

Both take the same start_date/end_date convention: real `date` objects
(FastAPI/Pydantic validates the "YYYY-MM-DD" query param into one at the
route boundary, so a malformed value 422s before it ever reaches here),
both optional, end_date inclusive of that whole calendar day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def occurred_at_bounds(start_date: date | None, end_date: date | None) -> tuple[str | None, str | None]:
    """Bounds for a direct SQL filter on transaction_occurred_at (a String
    column). Upper bound is the next calendar day, exclusive, so the whole
    end_date day is included regardless of what time-of-day suffix a real
    timestamp carries.
    """
    lower = start_date.isoformat() if start_date else None
    upper = (end_date + timedelta(days=1)).isoformat() if end_date else None
    return lower, upper


def datetime_bounds(start_date: date | None, end_date: date | None) -> tuple[datetime | None, datetime | None]:
    """Bounds for a direct SQL filter on a real DateTime column (e.g.
    EntitySnapshot.window_end). Same inclusive-end-of-day convention as
    occurred_at_bounds above.
    """
    lower = datetime(start_date.year, start_date.month, start_date.day) if start_date else None
    upper = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=1) if end_date else None
    return lower, upper


def date_in_range(value: date | datetime | None, start_date: date | None, end_date: date | None) -> bool:
    """Python-side range check for claims already assembled by a join loop
    (e.g. app/exposure.py's _all_claims, app/dashboard.py's issue/break
    counts) rather than a single SQL query. `value` with no real date
    attached (the join found nothing) is excluded once a range is active --
    an honest "can't place this in time" outcome, not a silent include.
    """
    if start_date is None and end_date is None:
        return True
    if value is None:
        return False
    value_date = value.date() if isinstance(value, datetime) else value
    if start_date and value_date < start_date:
        return False
    if end_date and value_date > end_date:
        return False
    return True
