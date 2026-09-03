"""Shared date-range filtering for the Insights pages' Date Range filter.

Every list/aggregate view that accepts start_date/end_date (plain
"YYYY-MM-DD" strings) narrows to rows whose own natural timestamp column
falls in that range, inclusive on both ends. There's no single shared
"transaction date" across every table here -- CanonicalEvent has a real
per-transaction timestamp (transaction_occurred_at), but EntitySnapshot/
BeneficiarySnapshot are windowed (window_end), and AnalystReview/
PaymentHealthScoreHistory are each keyed by their own reviewed_at/
computed_at -- so each caller filters on whichever column is that
table's own honest "when" (see each function's own docstring).

OperationalIssue.detected_at and ReconciliationBreak.detected_at are
deliberately NOT used for date filtering, even though the column exists
and looks like an obvious candidate -- confirmed against real data that
every row of both tables shares one single detected_at instant (the
moment someone ran the detection pipeline), completely decoupled from
the real transaction dates the rows describe. Filtering on it would
silently return zero rows for any picked range that doesn't happen to
include that one instant, regardless of how many real transactions fall
inside the picked range. operational_issue_ids_in_date_range() and
reconciliation_break_ids_in_date_range() below resolve the real
transaction date instead, via CanonicalEvent.transaction_occurred_at.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session


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


def operational_issue_ids_in_date_range(
    db: Session, tenant_bank_id: str, start_date: str | None, end_date: str | None,
) -> set[int] | None:
    """OperationalIssue.id values whose real transaction date falls in
    [start_date, end_date] -- see this module's docstring for why that's
    NOT detected_at. Returns None (not an empty set) when no range was
    given, so callers can tell "no filter" apart from "filter matched
    nothing." TRANSACTION/BATCH-referenced rows are checked against the
    real CanonicalEvent row(s) they reference; PARTY-referenced rows (the
    two rate-based spike types, NETWORK_TIMEOUT_SPIKE/FORMAT_REJECTION_
    SPIKE) describe a party's rate over a window, not one transaction --
    same "no single transaction to attribute to" reasoning app/exposure.py
    already excludes them from every $ figure for -- so they're always
    included rather than excluded by a filter they have no real date to
    honestly check against.
    """
    from .models import CanonicalEvent
    from .operations.models import OperationalIssue

    if not start_date and not end_date:
        return None

    start_str, end_str = string_date_bounds(start_date, end_date)
    events_query = db.query(CanonicalEvent.transaction_id, CanonicalEvent.batch_id).filter(
        CanonicalEvent.tenant_bank_id == tenant_bank_id,
    )
    if start_str:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at >= start_str)
    if end_str:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at <= end_str)

    txn_ids_in_range: set[str] = set()
    batch_ids_in_range: set[str] = set()
    for txn_id, batch_id in events_query.all():
        if txn_id:
            txn_ids_in_range.add(txn_id)
        if batch_id:
            batch_ids_in_range.add(batch_id)

    matching_ids: set[int] = set()
    for issue_id, reference_type, reference_id in (
        db.query(OperationalIssue.id, OperationalIssue.reference_type, OperationalIssue.reference_id)
        .filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
        .all()
    ):
        if reference_type == "PARTY":
            matching_ids.add(issue_id)
        elif reference_type == "TRANSACTION" and reference_id in txn_ids_in_range:
            matching_ids.add(issue_id)
        elif reference_type == "BATCH" and reference_id in batch_ids_in_range:
            matching_ids.add(issue_id)
    return matching_ids


def reconciliation_break_ids_in_date_range(
    db: Session, tenant_bank_id: str, start_date: str | None, end_date: str | None,
) -> set[int] | None:
    """ReconciliationBreak.id values whose real transaction date falls in
    [start_date, end_date] -- see operational_issue_ids_in_date_range()'s
    docstring for why not detected_at. Matched on (rail_type,
    transaction_id) together, not transaction_id alone -- the same
    real-world id can recur across unrelated rails (see CanonicalEvent's
    own docstring on why its uniqueness key includes rail_type).
    """
    from .models import CanonicalEvent
    from .reconciliation.models import ReconciliationBreak

    if not start_date and not end_date:
        return None

    start_str, end_str = string_date_bounds(start_date, end_date)
    events_query = db.query(CanonicalEvent.rail_type, CanonicalEvent.transaction_id).filter(
        CanonicalEvent.tenant_bank_id == tenant_bank_id,
    )
    if start_str:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at >= start_str)
    if end_str:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at <= end_str)
    keys_in_range = {(rail, txn) for rail, txn in events_query.all()}

    matching_ids: set[int] = set()
    for brk_id, rail_type, transaction_id in (
        db.query(ReconciliationBreak.id, ReconciliationBreak.rail_type, ReconciliationBreak.transaction_id)
        .filter(ReconciliationBreak.tenant_bank_id == tenant_bank_id)
        .all()
    ):
        if (rail_type, transaction_id) in keys_in_range:
            matching_ids.add(brk_id)
    return matching_ids
