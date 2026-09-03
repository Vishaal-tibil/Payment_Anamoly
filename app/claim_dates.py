"""The real date each detected claim's own condition occurred.

Shared by app/dashboard.py and app/review/service.py. Lives in its own
module rather than in either of them because dashboard.py already imports
review/service.py (for get_review_summary), so review/service.py could
not import these back from dashboard.py without a circular import -- and
both genuinely need the same answer, or a date-scoped review summary
would disagree with a date-scoped overview about which claims fall in a
window.

The distinction these encode: `detected_at` on OperationalIssue /
ReconciliationBreak is a batch-compute-run timestamp (every row from one
compute run shares it), NOT a real event time -- see app/priority.py and
app/investigation/trend.py for the same finding. The real chronological
anchor is either the row's own window_end (rate-based issues and both
snapshot types carry a real one) or the underlying CanonicalEvent's
transaction_occurred_at, via the same join app/exposure.py's
_operational_claims already uses for dollar amounts.
"""
from __future__ import annotations

from datetime import datetime

from .canonical_event_lookup import CanonicalEventLookup
from .operations.models import OperationalIssue
from .reconciliation.models import ReconciliationBreak


def parse_occurred_at(value: str | None) -> datetime | None:
    """CanonicalEvent.transaction_occurred_at is a String/ISO8601 column,
    not a DateTime -- parse leniently and return None on anything
    unparseable rather than raising, so one malformed source row can't
    break a whole aggregation.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def operational_issue_date(lookup: CanonicalEventLookup, issue: OperationalIssue) -> datetime | None:
    """Rate-based issues (spikes) carry their own real window_start/
    window_end; the other types need the reference_id/batch_id join.

    Takes a CanonicalEventLookup (one query per caller, built once) --
    not a db session -- since this used to run one CanonicalEvent query
    per issue row, confirmed via direct latency measurement to be the
    dominant real cost behind slow page loads.
    """
    if issue.window_end is not None:
        return issue.window_end
    if issue.issue_type == "BATCH_NOT_SETTLED":
        event = lookup.first_by_batch_id(issue.reference_id)
    else:
        event = lookup.first_by_transaction_id(issue.reference_id)
    return parse_occurred_at(event.transaction_occurred_at) if event else None


def reconciliation_break_date(lookup: CanonicalEventLookup, brk: ReconciliationBreak) -> datetime | None:
    """Rail-scoped (not just transaction_id) -- a transaction_id alone
    isn't guaranteed unique across rails for one tenant; only
    (tenant, rail, transaction_id) is.
    """
    event = lookup.by_rail_and_transaction_id(brk.rail_type, brk.transaction_id)
    return parse_occurred_at(event.transaction_occurred_at) if event else None
