"""Read-only aggregation views for the frontend dashboard.

Unlike app/anomaly, app/operations, app/reconciliation, this module is
not an engine -- it writes nothing, has no output table of its own.
Every function here is a live query reshaping already-computed engine
output (EntitySnapshot, OperationalIssue, ReconciliationBreak) plus raw
CanonicalEvent facts, for direct UI consumption. If a number here looks
wrong, the fix belongs in the engine that computed the underlying data,
not here.

Every rail-level figure uses the five real rail_type values in this
schema (ACH, WIRE, CARD, FEDNOW, CHEQUE) -- never the frontend
prototype's placeholder names (RTP, SWIFT, CHIPS, Fedwire), which don't
exist anywhere in the actual data.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .anomaly.categories import get_pattern_mix
from .anomaly.models import EntitySnapshot
from .canonical_event_lookup import CanonicalEventLookup
from .claim_dates import operational_issue_date as _operational_issue_date
from .claim_dates import parse_occurred_at as _parse_occurred_at
from .claim_dates import reconciliation_break_date as _reconciliation_break_date
from .date_filter import date_in_range, datetime_bounds, occurred_at_bounds
from .health.models import PaymentHealthScore
from .models import CanonicalEvent, Individual, Merchant
from .operations.models import OperationalIssue
from .priority import priority_levels_for_breaks, priority_levels_for_issues
from .reconciliation.models import ReconciliationBreak
from .review.service import get_review_quality_trend, get_review_summary


def get_overview(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    """start_date/end_date (optional "YYYY-MM-DD") scope every real
    transaction-derived figure to that real window. Entity counts
    (total_merchants/total_individuals) and date_range_start/end stay
    unfiltered -- a merchant onboarded outside the window doesn't stop
    existing, and date_range_start/end are the real full addressable
    range the UI's own date picker bounds itself to, not an echo of the
    current selection.
    """
    lower, upper = occurred_at_bounds(start_date, end_date)

    events_query = db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    if lower:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at >= lower)
    if upper:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at < upper)

    total_transactions = events_query.count()
    settled_transactions = events_query.filter(CanonicalEvent.status == "SETTLED").count()
    total_merchants = db.query(Merchant).filter_by(tenant_bank_id=tenant_bank_id).count()
    total_individuals = db.query(Individual).filter_by(tenant_bank_id=tenant_bank_id).count()

    # transaction_occurred_at is a String/ISO8601 column (see CanonicalEvent's
    # own docstring for why) -- MIN/MAX on it is still correct chronological
    # ordering since ISO8601's lexicographic order matches time order.
    date_range_start, date_range_end = (
        db.query(func.min(CanonicalEvent.transaction_occurred_at), func.max(CanonicalEvent.transaction_occurred_at))
        .filter(CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.transaction_occurred_at.isnot(None))
        .one()
    )

    # EntitySnapshot.window_end is a real DateTime column -- filterable
    # directly, no join needed.
    dt_lower, dt_upper = datetime_bounds(start_date, end_date)
    snapshot_query = db.query(EntitySnapshot.anomaly_band).filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    if dt_lower:
        snapshot_query = snapshot_query.filter(EntitySnapshot.window_end >= dt_lower)
    if dt_upper:
        snapshot_query = snapshot_query.filter(EntitySnapshot.window_end < dt_upper)
    anomaly_band_counts: dict[str, int] = defaultdict(int)
    for (band,) in snapshot_query.all():
        if band:
            anomaly_band_counts[band] += 1

    # OperationalIssue/ReconciliationBreak have no meaningful date column of
    # their own (detected_at is a batch compute timestamp -- see
    # _operational_issue_date's docstring) -- real date comes from the
    # underlying transaction via the same join app/exposure.py's
    # _all_claims() already uses for dollar amounts.
    event_lookup = CanonicalEventLookup(db, tenant_bank_id)
    operational_issue_counts: dict[str, int] = defaultdict(int)
    for issue in db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _operational_issue_date(event_lookup, issue)
        if date_in_range(occurred, start_date, end_date):
            operational_issue_counts[issue.issue_type] += 1

    reconciliation_break_counts: dict[str, int] = defaultdict(int)
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _reconciliation_break_date(event_lookup, brk)
        if date_in_range(occurred, start_date, end_date):
            reconciliation_break_counts[brk.detection_type] += 1

    return {
        "total_transactions": total_transactions,
        "settled_transactions": settled_transactions,
        "settlement_rate": (settled_transactions / total_transactions) if total_transactions else None,
        "total_merchants": total_merchants,
        "total_individuals": total_individuals,
        "date_range_start": date_range_start,
        "date_range_end": date_range_end,
        "anomaly_band_counts": dict(anomaly_band_counts),
        "operational_issue_counts": dict(operational_issue_counts),
        "reconciliation_break_counts": dict(reconciliation_break_counts),
    }


def get_transaction_status_breakdown(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Real counts per CanonicalEvent.status -- the actual final states a
    transaction lands in (SETTLED/FAILED/PENDING/NOT_SUBMITTED, or unset).
    Replaces the Analyst Payment Rails page's previous fully fabricated
    multi-stage "Payment Processing Funnel" (Validated/Processed/Posted/
    Settled -- no such intermediate pipeline stages exist anywhere in
    this schema, only a final status per transaction). Coarser than
    that fiction, but real.
    """
    rows = (
        db.query(CanonicalEvent.status, func.count(CanonicalEvent.id))
        .filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
        .group_by(CanonicalEvent.status)
        .all()
    )
    counts = {(status or "UNKNOWN"): count for status, count in rows}
    return {"total_transactions": sum(counts.values()), "counts_by_status": counts}


def get_rail_stats(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    lower, upper = occurred_at_bounds(start_date, end_date)
    events_query = db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    if lower:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at >= lower)
    if upper:
        events_query = events_query.filter(CanonicalEvent.transaction_occurred_at < upper)
    events = events_query.all()

    by_rail: dict[str, list[CanonicalEvent]] = defaultdict(list)
    for event in events:
        by_rail[event.rail_type].append(event)

    # ReconciliationBreak has no date column of its own -- same join as
    # get_overview's reconciliation_break_counts.
    event_lookup = CanonicalEventLookup(db, tenant_bank_id)
    break_counts_by_rail: dict[str, int] = defaultdict(int)
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _reconciliation_break_date(event_lookup, brk)
        if date_in_range(occurred, start_date, end_date):
            break_counts_by_rail[brk.rail_type] += 1

    rails = []
    for rail_type in sorted(by_rail.keys()):
        rail_events = by_rail[rail_type]
        settled_count = sum(1 for e in rail_events if e.status == "SETTLED")
        amounts = [e.amount for e in rail_events if e.amount is not None]
        rails.append({
            "rail_type": rail_type,
            "transaction_count": len(rail_events),
            "settled_count": settled_count,
            "settlement_rate": (settled_count / len(rail_events)) if rail_events else None,
            "total_amount": sum(amounts) if amounts else None,
            "reconciliation_break_count": break_counts_by_rail.get(rail_type, 0),
        })

    return {"rails": rails}


def get_senior_overview(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """The executive rollup: one Payment Health score, analyst review
    completion, and per-engine finding totals -- deliberately no
    per-item list. The analyst view is where individual claims get
    worked; this is the "how are we doing overall" read a senior
    stakeholder actually wants first. Composes app/health (Step 6d) and
    app/review's already-computed output -- same "reshape, don't
    recompute" rule as every other function in this module.
    """
    base = get_overview(db, tenant_bank_id)
    health_row = db.get(PaymentHealthScore, tenant_bank_id)

    return {
        "tenant_bank_id": tenant_bank_id,
        "health": (
            {
                "score": health_row.health_score,
                "band": health_row.health_band,
                "components": {
                    "settlement": health_row.settlement_component,
                    "anomaly": health_row.anomaly_component,
                    "operational": health_row.operational_component,
                    "reconciliation": health_row.reconciliation_component,
                },
                "computed_at": health_row.computed_at,
            }
            if health_row
            else None  # POST /health/compute hasn't been run yet for this tenant
        ),
        "review": get_review_summary(db, tenant_bank_id),
        "engine_totals": {
            "anomaly_band_counts": base["anomaly_band_counts"],
            "operational_issue_counts": base["operational_issue_counts"],
            "reconciliation_break_counts": base["reconciliation_break_counts"],
        },
        "settlement_rate": base["settlement_rate"],
        "total_transactions": base["total_transactions"],
        "date_range_start": base["date_range_start"],
        "date_range_end": base["date_range_end"],
    }


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _detection_latencies(
    db: Session, tenant_bank_id: str, lookup: CanonicalEventLookup,
    start_date: date | None = None, end_date: date | None = None,
) -> list[tuple[str | None, float]]:
    """(rail_type, latency_seconds) for every signal where a real
    transaction timestamp is resolvable: TRANSACTION-referenced
    OperationalIssue rows and all ReconciliationBreak rows (both always
    carry a transaction_id). Party/batch-referenced OperationalIssue rows
    and EntitySnapshot windows have no single transaction to diff
    against, so they're excluded -- a real subset, not padded to cover
    every signal type.

    start_date/end_date scope by when the underlying transaction really
    occurred (the same `occurred` already needed for the latency itself),
    not by detected_at -- consistent with every other date-scoped figure
    in this module.
    """
    latencies: list[tuple[str | None, float]] = []

    for issue in db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id, reference_type="TRANSACTION").all():
        event = lookup.first_by_transaction_id(issue.reference_id)
        occurred = _parse_occurred_at(event.transaction_occurred_at) if event else None
        if occurred is None or not date_in_range(occurred, start_date, end_date):
            continue
        latency = (_as_utc(issue.detected_at) - _as_utc(occurred)).total_seconds()
        if latency >= 0:
            latencies.append((event.rail_type, latency))

    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        event = lookup.by_rail_and_transaction_id(brk.rail_type, brk.transaction_id)
        occurred = _parse_occurred_at(event.transaction_occurred_at) if event else None
        if occurred is None or not date_in_range(occurred, start_date, end_date):
            continue
        latency = (_as_utc(brk.detected_at) - _as_utc(occurred)).total_seconds()
        if latency >= 0:
            latencies.append((brk.rail_type, latency))

    return latencies


def _detection_volume_by_category(
    db: Session, tenant_bank_id: str, lookup: CanonicalEventLookup,
    start_date: date | None = None, end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Reclassifies counts already computed elsewhere (operational issue
    rows, reconciliation break rows, scored fraud snapshots) into one
    percentage breakdown -- not new data, a different grouping of it.
    Date-scoped by each row's own real occurrence (app/claim_dates.py for
    issues/breaks, window_end for snapshots).
    """
    operational_count = sum(
        1 for i in db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).all()
        if date_in_range(_operational_issue_date(lookup, i), start_date, end_date)
    )
    reconciliation_count = sum(
        1 for b in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all()
        if date_in_range(_reconciliation_break_date(lookup, b), start_date, end_date)
    )
    fraud_count = sum(
        1 for s in db.query(EntitySnapshot)
        .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.anomaly_band.isnot(None)).all()
        if date_in_range(s.window_end, start_date, end_date)
    )
    total = operational_count + reconciliation_count + fraud_count

    def _pct(count: int) -> float | None:
        return (count / total) if total else None

    return [
        {"category": "Operational", "count": operational_count, "percentage": _pct(operational_count)},
        {"category": "Fraud", "count": fraud_count, "percentage": _pct(fraud_count)},
        {"category": "Reconciliation", "count": reconciliation_count, "percentage": _pct(reconciliation_count)},
    ]


def _detection_performance_by_rail(
    db: Session, tenant_bank_id: str, latencies: list[tuple[str | None, float]], lookup: CanonicalEventLookup,
    start_date: date | None = None, end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Per rail: success_rate (fraction of that rail's transactions NOT
    referenced by any OperationalIssue/ReconciliationBreak) + median
    detection latency for the signals on that rail with a resolvable one.
    Both numerator and denominator are scoped to the same real window, so
    success_rate stays a true fraction rather than mixing a windowed
    flagged-count with an all-time transaction total.
    """
    latency_by_rail: dict[str, list[float]] = defaultdict(list)
    for rail, lat in latencies:
        if rail:
            latency_by_rail[rail].append(lat)

    flagged_txn_ids_by_rail: dict[str, set[str]] = defaultdict(set)
    for issue in db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id, reference_type="TRANSACTION").all():
        event = lookup.first_by_transaction_id(issue.reference_id)
        if event and date_in_range(_parse_occurred_at(event.transaction_occurred_at), start_date, end_date):
            flagged_txn_ids_by_rail[event.rail_type].add(issue.reference_id)
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        if date_in_range(_reconciliation_break_date(lookup, brk), start_date, end_date):
            flagged_txn_ids_by_rail[brk.rail_type].add(brk.transaction_id)

    lower, upper = occurred_at_bounds(start_date, end_date)
    rail_types = [r for (r,) in db.query(CanonicalEvent.rail_type).filter_by(tenant_bank_id=tenant_bank_id).distinct().all()]
    result = []
    for rail_type in sorted(rail_types):
        rail_total_query = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, rail_type=rail_type)
        if lower:
            rail_total_query = rail_total_query.filter(CanonicalEvent.transaction_occurred_at >= lower)
        if upper:
            rail_total_query = rail_total_query.filter(CanonicalEvent.transaction_occurred_at < upper)
        rail_total = rail_total_query.count()
        flagged = len(flagged_txn_ids_by_rail.get(rail_type, set()))
        result.append({
            "rail_type": rail_type,
            "success_rate": (1 - flagged / rail_total) if rail_total else None,
            "median_detection_latency_seconds": (
                statistics.median(latency_by_rail[rail_type]) if latency_by_rail.get(rail_type) else None
            ),
        })
    return result


def _new_patterns_detected(db: Session, tenant_bank_id: str, recent_days: int = 7) -> int:
    """Count of distinct categories (OperationalIssue.issue_type /
    ReconciliationBreak.detection_type) whose EARLIEST detected_at for
    this tenant falls within the most recent `recent_days` -- i.e. a
    pattern genuinely new this period, not one that's always been present
    and just recurred. "Recent" is relative to the data's own most recent
    detected_at, not wall-clock now() -- this is synthetic pilot data
    with a fixed date range, so a real-time cutoff would silently go to
    zero once wall-clock time moves past that range (same reasoning
    compute_snapshots() uses the data's own timestamps for its
    chronological train/test split instead of real time).
    """
    first_seen: dict[str, datetime] = {}
    all_detected_ats: list[datetime] = []

    for issue_type, detected_at in db.query(OperationalIssue.issue_type, OperationalIssue.detected_at).filter_by(tenant_bank_id=tenant_bank_id).all():
        ts = _as_utc(detected_at)
        all_detected_ats.append(ts)
        if issue_type not in first_seen or ts < first_seen[issue_type]:
            first_seen[issue_type] = ts
    for detection_type, detected_at in db.query(ReconciliationBreak.detection_type, ReconciliationBreak.detected_at).filter_by(tenant_bank_id=tenant_bank_id).all():
        ts = _as_utc(detected_at)
        all_detected_ats.append(ts)
        if detection_type not in first_seen or ts < first_seen[detection_type]:
            first_seen[detection_type] = ts

    if not all_detected_ats:
        return 0
    recent_cutoff = max(all_detected_ats) - timedelta(days=recent_days)
    return sum(1 for ts in first_seen.values() if ts >= recent_cutoff)


def get_detection_performance(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    """Real detection-performance metrics -- deliberately NOT the
    "Detection Effectiveness %" / "False Positive Rate" the original
    prototype showed as static mock numbers. Those aren't computable at
    all in an unsupervised system with no ground-truth fraud label...
    except that the analyst review workflow (app/review) *is* a real
    human ground-truth signal, just one that only exists once a claim
    has actually been reviewed. So confirmation/false-positive rate here
    are real, computed only from actually-reviewed claims, and honestly
    small-sample early on -- they grow more statistically meaningful as
    more review activity accumulates. reviewed_count is returned
    alongside so the UI never shows a rate without also showing how much
    real feedback it's based on.

    Coverage and "exposure identified early" don't have that
    growing-over-time caveat -- both are computable right now:
    - Coverage: the fraction of transactions belonging to a resolved
      merchant/individual (merchant_id or individual_id set) -- i.e.
      actually eligible for this platform's behavioral monitoring.
    - Exposure identified early: real dollar sum of PROVISIONAL_VARIANCE
      reconciliation breaks -- a variance flagged before the source
      system's own official BREAK verdict caught up to it (see
      app/reconciliation/breaks.py's module docstring).

    start_date/end_date scope every per-window figure here to that real
    transaction window: coverage, exposure-identified-early,
    confirmation_rate/false_positive_rate/reviewed/confirmed/dismissed
    (via get_review_summary, which scopes to claims whose own condition
    occurred in the window and counts only those claims' reviews),
    median_detection_time_seconds, detection_volume_by_category and
    detection_performance_by_rail.

    Three fields are deliberately NOT scoped, because a transaction-date
    window is the wrong axis for them rather than because it's unbuilt:
    - quality_trend: one point per real review action in reviewed_at
      order -- a review-activity timeline, not a transaction timeline.
    - pattern_mix: a current-state snapshot of every flagged entity's
      category match.
    - new_patterns_detected: "new" is already defined relative to the
      data's own most recent detected_at (see _new_patterns_detected).
    """
    lower, upper = occurred_at_bounds(start_date, end_date)

    def _scoped(query):
        if lower:
            query = query.filter(CanonicalEvent.transaction_occurred_at >= lower)
        if upper:
            query = query.filter(CanonicalEvent.transaction_occurred_at < upper)
        return query

    total_transactions = _scoped(db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)).count()
    resolved_filter = or_(CanonicalEvent.merchant_id.isnot(None), CanonicalEvent.individual_id.isnot(None))
    covered_transactions = _scoped(
        db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id, resolved_filter)
    ).count()

    coverage_by_rail = []
    rail_types = [r for (r,) in db.query(CanonicalEvent.rail_type).filter_by(tenant_bank_id=tenant_bank_id).distinct().all()]
    for rail_type in sorted(rail_types):
        rail_total = _scoped(
            db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.rail_type == rail_type)
        ).count()
        rail_covered = _scoped(
            db.query(CanonicalEvent).filter(
                CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.rail_type == rail_type, resolved_filter,
            )
        ).count()
        coverage_by_rail.append({
            "rail_type": rail_type,
            "coverage_rate": (rail_covered / rail_total) if rail_total else None,
        })

    # Same join as get_overview's reconciliation_break_counts -- a break
    # has no date column of its own.
    event_lookup = CanonicalEventLookup(db, tenant_bank_id)
    early_breaks = [
        b
        for b in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id, detection_type="PROVISIONAL_VARIANCE").all()
        if date_in_range(_reconciliation_break_date(event_lookup, b), start_date, end_date)
    ]
    exposure_identified_early = sum(abs(b.amount) for b in early_breaks if b.amount is not None)

    review = get_review_summary(db, tenant_bank_id, start_date, end_date)
    reviewed_count = review["confirmed"] + review["dismissed"]
    latencies = _detection_latencies(db, tenant_bank_id, event_lookup, start_date, end_date)

    return {
        "coverage_rate": (covered_transactions / total_transactions) if total_transactions else None,
        "covered_transactions": covered_transactions,
        "total_transactions": total_transactions,
        "coverage_by_rail": coverage_by_rail,
        "exposure_identified_early": round(exposure_identified_early, 2),
        "provisional_variance_count": len(early_breaks),
        "confirmation_rate": (review["confirmed"] / reviewed_count) if reviewed_count else None,
        "false_positive_rate": (review["dismissed"] / reviewed_count) if reviewed_count else None,
        "reviewed_count": reviewed_count,
        "pending_count": review["pending"],
        "confirmed_count": review["confirmed"],
        "dismissed_count": review["dismissed"],
        "median_detection_time_seconds": statistics.median([lat for _, lat in latencies]) if latencies else None,
        "detection_volume_by_category": _detection_volume_by_category(db, tenant_bank_id, event_lookup, start_date, end_date),
        "detection_performance_by_rail": _detection_performance_by_rail(db, tenant_bank_id, latencies, event_lookup, start_date, end_date),
        "new_patterns_detected": _new_patterns_detected(db, tenant_bank_id),
        "quality_trend": get_review_quality_trend(db, tenant_bank_id),
        "pattern_mix": get_pattern_mix(db, tenant_bank_id),
    }


def get_priority_distribution(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    """Critical/High/Medium/Low counts across all three engines' real
    findings -- sourced from the SAME real, peer-relative per-item
    severity_score/priority_level app/priority.py computes for
    GET /operations/issues and /reconciliation/breaks (never a separate
    heuristic here, so this summary can never disagree with an analyst's
    own per-item filtered list). Fraud counts use the existing real
    anomaly_band, same scope as get_overview's anomaly_band_counts.
    start_date/end_date scope every count to that real transaction
    window, same join-based date attribution as get_overview.
    """
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}

    dt_lower, dt_upper = datetime_bounds(start_date, end_date)
    snapshot_query = db.query(EntitySnapshot.anomaly_band).filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    if dt_lower:
        snapshot_query = snapshot_query.filter(EntitySnapshot.window_end >= dt_lower)
    if dt_upper:
        snapshot_query = snapshot_query.filter(EntitySnapshot.window_end < dt_upper)
    for (band,) in snapshot_query.all():
        if band == "Critical":
            counts["critical"] += 1
        elif band == "High":
            counts["high"] += 1
        elif band == "Low-Medium":
            counts["medium"] += 1

    event_lookup = CanonicalEventLookup(db, tenant_bank_id)
    issue_priorities = priority_levels_for_issues(db, tenant_bank_id)
    for issue in db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _operational_issue_date(event_lookup, issue)
        if date_in_range(occurred, start_date, end_date):
            counts[issue_priorities[issue.id]["priority_level"].lower()] += 1

    break_priorities = priority_levels_for_breaks(db, tenant_bank_id)
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _reconciliation_break_date(event_lookup, brk)
        if date_in_range(occurred, start_date, end_date):
            counts[break_priorities[brk.id]["priority_level"].lower()] += 1

    return counts


def get_detection_attention(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Flags rails whose success_rate sits below this tenant's own
    cross-rail average -- a relative comparison against this tenant's own
    data, not an arbitrary absolute threshold (no external benchmark
    exists for what counts as "good" on any of these rails). Reuses
    get_detection_performance()'s already-computed per-rail figures, same
    "reshape, don't recompute" rule as the rest of this module. Not
    date-scoped -- see get_detection_performance's docstring for why
    detection_performance_by_rail itself isn't.
    """
    performance = get_detection_performance(db, tenant_bank_id)
    by_rail = performance["detection_performance_by_rail"]
    rates = [r["success_rate"] for r in by_rail if r["success_rate"] is not None]
    if not rates:
        return {"areas": []}

    average = sum(rates) / len(rates)
    areas = []
    for r in by_rail:
        if r["success_rate"] is not None and r["success_rate"] < average:
            areas.append({
                "rail_type": r["rail_type"],
                "reason": f"success_rate {r['success_rate']:.1%} -- below this tenant's {average:.1%} average across rails",
                "severity": "high" if r["success_rate"] < average - 0.05 else "medium",
            })
    areas.sort(key=lambda a: a["rail_type"])
    return {"areas": areas}


def get_anomaly_detection_categories() -> dict[str, Any]:
    """Static -- describes what the platform's three detection engines
    actually screen for and how, matching the recursive category-tree
    shape Settings > Anomaly Detection expects. Not a DB query: this is
    documentation-as-an-endpoint, grounded in the real detection logic
    (app/anomaly/, app/operations/, app/reconciliation/), not invented
    copy. Read-only -- no toggles/thresholds exist to write back yet.
    """
    return {
        "title": "Anomaly Detection Settings",
        "subtitle": "What this platform screens for, and how each category is actually detected",
        "items": [
            {
                "id": "fraud-anomaly",
                "title": "Fraud & Anomaly Detection",
                "description": (
                    "Unsupervised, profile-based detection -- Isolation Forest, "
                    "HDBSCAN clustering, and time-series drift, combined into one "
                    "0-100 score per merchant/individual per week."
                ),
                "categories": [
                    {
                        "id": "new-payee-risk", "code": "FRAUD-1", "title": "New Payee Risk",
                        "description": "First-time payments to a counterparty -- Isolation Forest's new_counterparty_ratio feature.",
                    },
                    {
                        "id": "funnel-account", "code": "FRAUD-2", "title": "Funnel Account",
                        "description": "Multiple distinct senders suddenly paying the same beneficiary -- weekly z-score drift on distinct_senders/new_sender_ratio.",
                    },
                    {
                        "id": "velocity-checks", "code": "FRAUD-3", "title": "Velocity Checks",
                        "description": "Unusual increase in transaction frequency -- time-series z-score drift on transaction_count.",
                    },
                    {
                        "id": "structuring", "code": "FRAUD-4", "title": "Structuring",
                        "description": "Amounts clustered just under the $10,000 CTR reporting threshold -- Isolation Forest's near_threshold_ratio feature.",
                    },
                ],
            },
            {
                "id": "operational-issues",
                "title": "Operational Issues",
                "description": (
                    "Did the payment pipeline itself work correctly -- mostly "
                    "deterministic fact-checks against the source's own operational "
                    "data, one rolling z-score check."
                ),
                "categories": [
                    {
                        "id": "duplicate-payment", "code": "OPS-1", "title": "Duplicate Payment",
                        "description": "A retry and its original both reached SETTLED status -- an idempotency-key join.",
                    },
                    {
                        "id": "formatting-rejection", "code": "OPS-2", "title": "Formatting Rejection",
                        "description": "Transactions that failed format validation, plus a rolling z-score on the reject rate to catch a spike.",
                    },
                    {
                        "id": "batch-never-settles", "code": "OPS-3", "title": "Batch Never Settles",
                        "description": "A batch past its expected settlement time with unsettled transactions.",
                    },
                    {
                        "id": "network-processor-timeout", "code": "OPS-4", "title": "Network/Processor Timeout",
                        "description": "A merchant's network timeout rate spiking above its own historical baseline.",
                    },
                ],
            },
            {
                "id": "reconciliation",
                "title": "Reconciliation",
                "description": (
                    "Does the settled amount match what the ledger posted -- reads "
                    "the source's own completed network-vs-ledger comparison."
                ),
                "categories": [
                    {
                        "id": "confirmed-break", "code": "REC-1", "title": "Confirmed Break",
                        "description": "The source's own reconciliation process already flagged this as a break.",
                    },
                    {
                        "id": "provisional-variance", "code": "REC-2", "title": "Provisional Variance",
                        "description": "Not yet confirmed as a break, but the variance amount is already nonzero -- an early-warning signal.",
                    },
                ],
            },
        ],
    }
