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

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .anomaly.categories import get_pattern_mix
from .anomaly.models import EntitySnapshot
from .date_filter import date_in_range, datetime_bounds, occurred_at_bounds
from .health.models import PaymentHealthScore
from .models import CanonicalEvent, Individual, Merchant
from .operations.models import OperationalIssue
from .reconciliation.models import ReconciliationBreak
from .review.service import get_review_quality_trend, get_review_summary


def _parse_occurred_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _operational_issue_date(db: Session, tenant_bank_id: str, issue: OperationalIssue) -> datetime | None:
    """The real date an operational issue's own detected condition
    occurred, not when our engine detected it (detected_at is a batch
    compute timestamp, not spread meaningfully over time -- see
    priority.py's docstring for the same distinction). Rate-based issues
    (spikes) carry their own real window_start/window_end; the other
    types need the same reference_id/batch_id join app/exposure.py's
    _operational_claims already uses for their dollar amounts.
    """
    if issue.window_end is not None:
        return issue.window_end
    if issue.issue_type == "BATCH_NOT_SETTLED":
        event = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, batch_id=issue.reference_id).first()
    else:
        event = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, transaction_id=issue.reference_id).first()
    return _parse_occurred_at(event.transaction_occurred_at) if event else None


def _reconciliation_break_date(db: Session, tenant_bank_id: str, brk: ReconciliationBreak) -> datetime | None:
    event = (
        db.query(CanonicalEvent)
        .filter_by(tenant_bank_id=tenant_bank_id, rail_type=brk.rail_type, transaction_id=brk.transaction_id)
        .one_or_none()
    )
    return _parse_occurred_at(event.transaction_occurred_at) if event else None


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
    operational_issue_counts: dict[str, int] = defaultdict(int)
    for issue in db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _operational_issue_date(db, tenant_bank_id, issue)
        if date_in_range(occurred, start_date, end_date):
            operational_issue_counts[issue.issue_type] += 1

    reconciliation_break_counts: dict[str, int] = defaultdict(int)
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _reconciliation_break_date(db, tenant_bank_id, brk)
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
    break_counts_by_rail: dict[str, int] = defaultdict(int)
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        occurred = _reconciliation_break_date(db, tenant_bank_id, brk)
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

    start_date/end_date scope coverage and exposure-identified-early to
    that real transaction window. confirmation_rate/false_positive_rate/
    quality_trend are deliberately NOT scoped by it -- those are about
    *when an analyst reviewed a claim*, a different real timeline than
    when the underlying transaction happened, and filtering by transaction
    date would misrepresent review activity that happened outside the
    selected window but on a claim whose transaction fell inside it (or
    vice versa). pattern_mix is a current-state snapshot of every flagged
    entity's category match, same reasoning.
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
    early_breaks = [
        b
        for b in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id, detection_type="PROVISIONAL_VARIANCE").all()
        if date_in_range(_reconciliation_break_date(db, tenant_bank_id, b), start_date, end_date)
    ]
    exposure_identified_early = sum(abs(b.amount) for b in early_breaks if b.amount is not None)

    review = get_review_summary(db, tenant_bank_id)
    reviewed_count = review["confirmed"] + review["dismissed"]

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
        "quality_trend": get_review_quality_trend(db, tenant_bank_id),
        "pattern_mix": get_pattern_mix(db, tenant_bank_id),
    }


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
