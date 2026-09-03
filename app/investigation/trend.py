"""Real weekly trend for an investigation case's (category, payment_rail)
-- tenant-wide, not per-merchant (too sparse: even the busiest real
merchant has only 45 transactions across the whole ~60-day pilot
window). Feeds facts_for_investigation_case() a real number the AI case
summary can reference, instead of never mentioning a trend at all.

Critical constraint this respects: OperationalIssue.detected_at and
ReconciliationBreak.detected_at are batch-compute-run timestamps, NOT
real event times -- confirmed directly against real data that every
ReconciliationBreak row shares one identical detected_at (whenever
compute_cases() was last run). Bucketing by detected_at would collapse
every alert into one instant. The real chronological anchor instead:
- ReconciliationBreak / transaction-or-batch-referenced OperationalIssue:
  the underlying CanonicalEvent's real transaction_occurred_at, via the
  same join app/investigation/cases.py's _rail_for_operational_issue()
  already does.
- Party-level rate-spike OperationalIssue (NETWORK_TIMEOUT_SPIKE,
  FORMAT_REJECTION_SPIKE) and fraud-sourced EntitySnapshot: already carry
  their own real window_end, no join needed.

Same honesty rule as app/anomaly/timeseries.py's _zscore(): below
_MIN_WEEKS_FOR_TREND real weeks of history, return None ("insufficient
trend history") rather than force a number out of noise.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import EntitySnapshot
from ..canonical_event_lookup import CanonicalEventLookup
from ..operations.models import OperationalIssue
from ..reconciliation.models import ReconciliationBreak

_MIN_WEEKS_FOR_TREND = 2

_PARTY_LEVEL_ISSUE_TYPES = {"NETWORK_TIMEOUT_SPIKE", "FORMAT_REJECTION_SPIKE"}

# A week's count more than this multiple above/below the mean of prior
# weeks reads as a real directional change, not just week-to-week noise.
_DIRECTION_BAND = 0.2


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _week_start(dt: datetime) -> datetime:
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _issue_anchor_dates(
    db: Session, tenant_bank_id: str, issue_type: str, rail: str | None, lookup: CanonicalEventLookup,
) -> list[datetime]:
    issues = db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id, issue_type=issue_type).all()

    if issue_type in _PARTY_LEVEL_ISSUE_TYPES:
        return [i.window_end for i in issues if i.window_end]

    dates = []
    for issue in issues:
        if issue.reference_type == "TRANSACTION":
            event = lookup.first_by_transaction_id(issue.reference_id)
        else:
            event = lookup.first_by_batch_id(issue.reference_id)
        if event is None:
            continue
        # Was accepting `rail` as a param but never applying it -- every
        # rail's chart for a TRANSACTION/BATCH-referenced category
        # (DUPLICATE_PAYMENT, FORMAT_REJECTION, BATCH_NOT_SETTLED) showed
        # the identical all-rails count, contradicting this function's
        # own "(category, rail)" docstring. Confirmed live: 5 different
        # cases (one per rail) all rendered byte-identical charts.
        if rail and event.rail_type != rail:
            continue
        ts = _parse_ts(event.transaction_occurred_at)
        if ts:
            dates.append(ts)
    return dates


def _break_anchor_dates(db: Session, tenant_bank_id: str, detection_type: str, rail: str | None, lookup: CanonicalEventLookup) -> list[datetime]:
    query = db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id, detection_type=detection_type)
    if rail:
        query = query.filter_by(rail_type=rail)

    dates = []
    for brk in query.all():
        # Rail-scoped (not just transaction_id) -- a transaction_id alone
        # isn't guaranteed unique across rails for one tenant (only
        # (tenant, rail, transaction_id) is), same lookup key
        # app/dashboard.py's _reconciliation_break_date already uses.
        event = lookup.by_rail_and_transaction_id(brk.rail_type, brk.transaction_id)
        ts = _parse_ts(event.transaction_occurred_at) if event else None
        if ts:
            dates.append(ts)
    return dates


def _fraud_anchor_dates(db: Session, tenant_bank_id: str, anomaly_band: str, rail: str | None) -> list[datetime]:
    snapshots = (
        db.query(EntitySnapshot)
        .filter_by(tenant_bank_id=tenant_bank_id, anomaly_band=anomaly_band)
        .all()
    )
    if rail:
        snapshots = [s for s in snapshots if (s.rails_used or []) == [rail]]
    return [s.window_end for s in snapshots if s.window_end]


def category_weekly_trend(db: Session, tenant_bank_id: str, category: str, rail: str | None) -> dict[str, Any] | None:
    """Real tenant-wide weekly count of every alert matching this
    (category, rail), for whichever real chronological anchor applies to
    that category. None if there isn't enough real history (< 2 real
    weeks) for a trend to mean anything.
    """
    if category.startswith("FRAUD_"):
        band = category.removeprefix("FRAUD_").capitalize()  # "FRAUD_CRITICAL" -> "Critical"
        anchors = _fraud_anchor_dates(db, tenant_bank_id, band, rail)
    else:
        # One batched CanonicalEvent lookup for either branch, instead of
        # a query per issue/break row -- confirmed via direct latency
        # measurement that the previous per-row shape was the dominant
        # real cost behind slow page loads.
        lookup = CanonicalEventLookup(db, tenant_bank_id)
        if category in ("CONFIRMED_BREAK", "PROVISIONAL_VARIANCE"):
            anchors = _break_anchor_dates(db, tenant_bank_id, category, rail, lookup)
        else:
            anchors = _issue_anchor_dates(db, tenant_bank_id, category, rail, lookup)

    if not anchors:
        return None

    weekly: dict[datetime, int] = defaultdict(int)
    for dt in anchors:
        weekly[_week_start(dt)] += 1
    weeks_sorted = sorted(weekly.items())

    if len(weeks_sorted) < _MIN_WEEKS_FOR_TREND:
        return None

    counts = [c for _week, c in weeks_sorted]
    *prior_counts, latest_count = counts
    prior_mean = statistics.mean(prior_counts)

    if latest_count > prior_mean * (1 + _DIRECTION_BAND):
        direction = "increasing"
    elif latest_count < prior_mean * (1 - _DIRECTION_BAND):
        direction = "decreasing"
    else:
        direction = "stable"

    return {
        "weeks_observed": len(weeks_sorted),
        "counts_by_week": [{"week_start": w.date().isoformat(), "count": c} for w, c in weeks_sorted],
        "latest_week_count": latest_count,
        "prior_weeks_average_count": round(prior_mean, 1),
        "direction": direction,
    }
