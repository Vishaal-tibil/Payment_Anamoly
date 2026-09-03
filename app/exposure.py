"""Real-dollar exposure views for the Overview page's Domain/Trend/
Mitigation/Payment-Value charts.

Not an engine -- like app/dashboard.py, this only reshapes already-real
facts (ReconciliationBreak.amount, EntitySnapshot.amount_total,
CanonicalEvent.amount via join, and the review workflow's PENDING/
CONFIRMED/DISMISSED status), never invents a number. Replaces the
original prototype's static "exposure"/"mitigation"/"payment value" mock
data on the frontend, which had no backend concept behind it at all --
no table anywhere tracked a dollar "exposure" figure or a "protected vs
impacted" split before this module existed.

Scope, and why "exposure" only covers 3 of the 5 detected issue types:
a clean $ figure requires an identifiable transaction (or batch of
transactions) behind the claim. Reconciliation breaks and Fraud/Anomaly's
flagged entities always have one; of Operational Issues' 4 types, only
Duplicate Payment, Formatting Rejection, and Batch Never Settles do. The
two rate-based ones (Format Rejection Spike, Network/Processor Timeout
Spike) describe a *rate* problem over a window, not a specific
transaction -- there's no honest single dollar figure to attribute to
them, so they're excluded from every $ total here. Their counts are
still visible elsewhere (dashboard.py's operational_issue_counts).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .anomaly.models import EntitySnapshot
from .canonical_event_lookup import CanonicalEventLookup
from .date_filter import date_in_range, occurred_at_bounds
from .models import CanonicalEvent
from .operations.models import OperationalIssue
from .reconciliation.models import ReconciliationBreak
from .review.models import CONFIRMED, DISMISSED
from .review.service import get_review

_MATERIAL_ANOMALY_BANDS = ("High", "Critical")
_TRANSACTION_LEVEL_ISSUE_TYPES = ("DUPLICATE_PAYMENT", "FORMAT_REJECTION")
_BATCH_LEVEL_ISSUE_TYPES = ("BATCH_NOT_SETTLED",)

# One (signal_type, reference_id, amount, rail_type, week_start) tuple per
# dollar-attributable claim -- the shared unit every view below is built
# from, so "exposure by domain," "by week," "by rail," and "reviewed vs
# not" are always reading the same underlying real amounts, never four
# separately-computed (and possibly inconsistent) numbers.


def _week_start(dt: datetime) -> datetime:
    # Normalized to naive on the way out regardless of the input's own
    # awareness -- EntitySnapshot.window_end is timezone-aware (a real
    # DateTime(timezone=True) column) while transaction_occurred_at is a
    # plain string, and real samples in this data are inconsistent about
    # carrying a timezone suffix (some 'Z'-suffixed, some bare
    # 'YYYY-MM-DD HH:MM:SS'). Bucketing is by calendar week only, so
    # dropping tzinfo here loses no real distinction, just keeps every
    # bucket key comparable/sortable regardless of which source it came from.
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_occurred_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fraud_anomaly_claims(db: Session, tenant_bank_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(EntitySnapshot)
        .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.anomaly_band.in_(_MATERIAL_ANOMALY_BANDS))
        .all()
    )
    claims = []
    for row in rows:
        claims.append({
            "signal_type": "fraud_anomaly",
            "reference_id": str(row.id),
            "amount": row.amount_total or 0.0,
            "rail_type": None,  # an entity, not a single-rail transaction
            "week_start": _week_start(row.window_end) if row.window_end else None,
            "occurred": row.window_end,
        })
    return claims


def _reconciliation_claims(db: Session, tenant_bank_id: str, lookup: CanonicalEventLookup) -> list[dict[str, Any]]:
    breaks = db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all()
    claims = []
    for brk in breaks:
        event = lookup.by_rail_and_transaction_id(brk.rail_type, brk.transaction_id)
        occurred = _parse_occurred_at(event.transaction_occurred_at) if event else None
        claims.append({
            "signal_type": "reconciliation_break",
            "reference_id": str(brk.id),
            "amount": abs(brk.amount) if brk.amount is not None else 0.0,
            "rail_type": brk.rail_type,
            "week_start": _week_start(occurred) if occurred else None,
            "occurred": occurred,
        })
    return claims


def _operational_claims(db: Session, tenant_bank_id: str, lookup: CanonicalEventLookup) -> list[dict[str, Any]]:
    issues = (
        db.query(OperationalIssue)
        .filter(
            OperationalIssue.tenant_bank_id == tenant_bank_id,
            OperationalIssue.issue_type.in_(_TRANSACTION_LEVEL_ISSUE_TYPES + _BATCH_LEVEL_ISSUE_TYPES),
        )
        .all()
    )
    claims = []
    for issue in issues:
        if issue.issue_type in _TRANSACTION_LEVEL_ISSUE_TYPES:
            events = lookup.all_by_transaction_id(issue.reference_id)
        else:  # BATCH_NOT_SETTLED -- every transaction in the overdue batch
            events = lookup.all_by_batch_id(issue.reference_id)
        amount = sum(abs(e.amount) for e in events if e.amount is not None)
        occurred = _parse_occurred_at(events[0].transaction_occurred_at) if events else None
        rail_type = events[0].rail_type if events else None
        claims.append({
            "signal_type": "operational_issue",
            "reference_id": str(issue.id),
            "amount": amount,
            "rail_type": rail_type,
            "week_start": _week_start(occurred) if occurred else None,
            "occurred": occurred,
        })
    return claims


def _all_claims(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> list[dict[str, Any]]:
    # One batched CanonicalEvent lookup for this whole call -- confirmed
    # via direct latency measurement that querying it once per break/issue
    # row (the previous shape) was the dominant real cost behind slow page
    # loads, once WAL mode ruled out DB-level lock contention. Accepts a
    # pre-built lookup (not just db) so GET /dashboard/exposure -- which
    # calls six of this module's functions in one request, each ultimately
    # reaching _all_claims() -- can share ONE lookup across all six
    # instead of each independently re-scanning every CanonicalEvent row;
    # confirmed via direct measurement that skipping this made that
    # endpoint slower, not faster (6 full scans instead of ~94 small
    # point-queries). Standalone callers (tests, other endpoints) still
    # work unchanged -- lookup is built here if not supplied.
    if lookup is None:
        lookup = CanonicalEventLookup(db, tenant_bank_id)
    claims = (
        _fraud_anomaly_claims(db, tenant_bank_id)
        + _reconciliation_claims(db, tenant_bank_id, lookup)
        + _operational_claims(db, tenant_bank_id, lookup)
    )
    if start_date is None and end_date is None:
        return claims
    # Real occurred date per claim (see each _*_claims() above) -- a claim
    # whose join found no underlying transaction date is excluded once a
    # range is active, an honest "can't place this in time" rather than a
    # silent include. Same reasoning as date_filter.date_in_range's own
    # docstring.
    return [c for c in claims if date_in_range(c["occurred"], start_date, end_date)]


def get_payment_normalcy(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> dict[str, Any]:
    """"Payments completed normally" -- the fraction of real transactions
    NOT touched by any detected reconciliation break or transaction/batch-
    level operational issue. Deliberately transaction-level, unlike
    exposure-by-domain: fraud/anomaly and Funnel Account are entity- or
    beneficiary-level judgments, not "this specific payment had a
    problem," so they're excluded here the same way the two rate-based
    operational issue types are excluded from _all_claims()'s $ figures --
    there's no single transaction to mark as "not normal" for either.

    start_date/end_date scope both totals to the same real transaction
    window -- both total_transactions and touched are counted from
    within-range events only, so the rate stays a real ratio, not a
    filtered numerator over an unfiltered denominator.
    """
    lower, upper = occurred_at_bounds(start_date, end_date)

    def _scoped(query):
        if lower:
            query = query.filter(CanonicalEvent.transaction_occurred_at >= lower)
        if upper:
            query = query.filter(CanonicalEvent.transaction_occurred_at < upper)
        return query

    total_transactions = _scoped(db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)).count()
    in_range_events: set[tuple[str, str]] = {
        (rail_type, transaction_id)
        for (rail_type, transaction_id) in _scoped(
            db.query(CanonicalEvent.rail_type, CanonicalEvent.transaction_id).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
        ).all()
    }

    touched: set[tuple[str, str]] = set()
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        key = (brk.rail_type, brk.transaction_id)
        if key in in_range_events:
            touched.add(key)

    event_lookup = lookup if lookup is not None else CanonicalEventLookup(db, tenant_bank_id)
    for issue in (
        db.query(OperationalIssue)
        .filter(OperationalIssue.tenant_bank_id == tenant_bank_id, OperationalIssue.issue_type.in_(_TRANSACTION_LEVEL_ISSUE_TYPES))
        .all()
    ):
        event = event_lookup.first_by_transaction_id(issue.reference_id)
        if event:
            key = (event.rail_type, event.transaction_id)
            if key in in_range_events:
                touched.add(key)

    for issue in (
        db.query(OperationalIssue)
        .filter(OperationalIssue.tenant_bank_id == tenant_bank_id, OperationalIssue.issue_type.in_(_BATCH_LEVEL_ISSUE_TYPES))
        .all()
    ):
        for event in event_lookup.all_by_batch_id(issue.reference_id):
            key = (event.rail_type, event.transaction_id)
            if key in in_range_events:
                touched.add(key)

    return {
        "rate": ((total_transactions - len(touched)) / total_transactions) if total_transactions else None,
        "total_transactions": total_transactions,
        "touched_transactions": len(touched),
    }


def get_exposure_by_domain(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> dict[str, Any]:
    claims = _all_claims(db, tenant_bank_id, start_date, end_date, lookup)
    totals: dict[str, float] = defaultdict(float)
    for claim in claims:
        totals[claim["signal_type"]] += claim["amount"]

    labels = {
        "fraud_anomaly": "Fraud / Anomaly",
        "reconciliation_break": "Reconciliation",
        "operational_issue": "Operational Issues",
    }
    domains = [
        {"id": key, "label": label, "amount": round(totals.get(key, 0.0), 2)}
        for key, label in labels.items()
    ]
    return {"domains": domains, "total": round(sum(totals.values()), 2)}


def get_exposure_trend(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> dict[str, Any]:
    """Weekly, not daily -- this dataset's real granularity (Track A's own
    windowing is weekly; detection-run timestamps cluster on whichever day
    the pipeline happened to run, so bucketing by *that* would produce a
    fake-looking single-day spike, not a real trend). Bucketed by the real
    week the underlying transaction occurred (Monday-start, same
    convention as app/anomaly/features.py), not by when it was detected.
    """
    claims = _all_claims(db, tenant_bank_id, start_date, end_date, lookup)
    by_week: dict[datetime, float] = defaultdict(float)
    for claim in claims:
        if claim["week_start"] is not None:
            by_week[claim["week_start"]] += claim["amount"]

    points = [
        {"week_start": week.date().isoformat(), "amount": round(amount, 2)}
        for week, amount in sorted(by_week.items())
    ]
    return {"points": points}


def get_mitigation_progress(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> dict[str, Any]:
    """Residual = dollar exposure of claims no analyst has acted on yet
    (PENDING, or never reviewed at all). Mitigated = dollar exposure of
    claims an analyst has CONFIRMED or DISMISSED. This is the one real
    concept in the whole platform "mitigation" can honestly map onto --
    reviewing IS the mitigating action here, there's no separate
    remediation-tracking system. A single point-in-time split, not a
    fabricated smooth trend line: with review activity this recent, a
    real trend would mostly be zero followed by a step change, which
    would look more like a chart bug than real data.
    """
    claims = _all_claims(db, tenant_bank_id, start_date, end_date, lookup)
    residual = 0.0
    mitigated = 0.0
    for claim in claims:
        review = get_review(db, claim["signal_type"], claim["reference_id"], tenant_bank_id)
        if review and review.status in (CONFIRMED, DISMISSED):
            mitigated += claim["amount"]
        else:
            residual += claim["amount"]
    total = residual + mitigated
    return {
        "residual": round(residual, 2),
        "mitigated": round(mitigated, 2),
        "total": round(total, 2),
        # "Mitigation Effectiveness": what fraction of total dollar-
        # attributable exposure an analyst has actually reviewed. None
        # (not 0) when there's no exposure at all -- a rate over zero
        # would be a division, not a real answer.
        "effectiveness_rate": (mitigated / total) if total else None,
    }


def get_mitigation_progress_by_domain(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> dict[str, Any]:
    """Same residual/mitigated split as get_mitigation_progress(), broken
    out per engine instead of combined into one total -- what the
    Anomalies page's "Mitigated vs. Residual Exposure" column reads.
    """
    claims = _all_claims(db, tenant_bank_id, start_date, end_date, lookup)
    labels = {
        "fraud_anomaly": "Fraud / Anomaly",
        "reconciliation_break": "Reconciliation",
        "operational_issue": "Operational Issues",
    }
    residual: dict[str, float] = defaultdict(float)
    mitigated: dict[str, float] = defaultdict(float)
    for claim in claims:
        review = get_review(db, claim["signal_type"], claim["reference_id"], tenant_bank_id)
        bucket = mitigated if review and review.status in (CONFIRMED, DISMISSED) else residual
        bucket[claim["signal_type"]] += claim["amount"]

    domains = [
        {
            "id": key,
            "label": label,
            "residual": round(residual.get(key, 0.0), 2),
            "mitigated": round(mitigated.get(key, 0.0), 2),
        }
        for key, label in labels.items()
    ]
    return {"domains": domains}


def get_exposure_by_rail(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    """Same claims _all_claims() already computes, grouped by rail_type
    instead of domain. Fraud/Anomaly claims carry rail_type=None (an
    entity, not a single-rail transaction -- see _fraud_anomaly_claims),
    so they're excluded here the same way they're excluded from
    get_payment_value_by_rail() -- no invented rail attribution.
    """
    claims = _all_claims(db, tenant_bank_id, start_date, end_date)
    totals: dict[str, float] = defaultdict(float)
    for claim in claims:
        if claim["rail_type"]:
            totals[claim["rail_type"]] += claim["amount"]

    rails = [{"rail_type": rail, "exposure": round(amount, 2)} for rail, amount in sorted(totals.items())]
    return {"rails": rails, "total": round(sum(totals.values()), 2)}


def get_anomaly_heatmap(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    """Rail x category cross-tab, counting claims (not dollar amounts).
    Same rail-attribution limitation as get_exposure_by_rail() -- Fraud/
    Anomaly claims have no single rail, so only Operational and
    Reconciliation claims appear here.
    """
    claims = _all_claims(db, tenant_bank_id, start_date, end_date)
    domain_labels = {"operational_issue": "Operational", "reconciliation_break": "Reconciliation"}
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for claim in claims:
        label = domain_labels.get(claim["signal_type"])
        if claim["rail_type"] and label:
            counts[(claim["rail_type"], label)] += 1

    cells = [
        {"rail_type": rail, "category": category, "count": count}
        for (rail, category), count in sorted(counts.items())
    ]
    return {"cells": cells}


def get_payment_value_by_rail(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
    lookup: CanonicalEventLookup | None = None,
) -> dict[str, Any]:
    """Protected vs Impacted per real rail (ACH/WIRE/CARD/FEDNOW/CHEQUE --
    never a rail that doesn't exist in this schema). Impacted is only ever
    computed from Reconciliation breaks (the only claim type with a
    definite per-rail amount and rail_type on every row); Protected is
    each rail's real total transaction volume minus that.
    """
    if lookup is None:
        lookup = CanonicalEventLookup(db, tenant_bank_id)
    lower, upper = occurred_at_bounds(start_date, end_date)
    events_query = db.query(CanonicalEvent.rail_type, CanonicalEvent.amount).filter(
        CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.amount.isnot(None),
    )
    if lower:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at >= lower)
    if upper:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at < upper)

    rail_totals: dict[str, float] = defaultdict(float)
    for (rail_type, amount) in events_query.all():
        rail_totals[rail_type] += amount

    impacted_by_rail: dict[str, float] = defaultdict(float)
    for claim in _reconciliation_claims(db, tenant_bank_id, lookup):
        if claim["rail_type"] and date_in_range(claim["occurred"], start_date, end_date):
            impacted_by_rail[claim["rail_type"]] += claim["amount"]

    rails = []
    for rail_type in sorted(rail_totals):
        total = rail_totals[rail_type]
        impacted = min(impacted_by_rail.get(rail_type, 0.0), total)
        rails.append({
            "rail_type": rail_type,
            "total_amount": round(total, 2),
            "protected_amount": round(total - impacted, 2),
            "impacted_amount": round(impacted, 2),
        })
    return {"rails": rails}
