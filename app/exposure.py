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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .anomaly.models import EntitySnapshot
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
        })
    return claims


def _reconciliation_claims(db: Session, tenant_bank_id: str) -> list[dict[str, Any]]:
    breaks = db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all()
    claims = []
    for brk in breaks:
        event = (
            db.query(CanonicalEvent)
            .filter_by(tenant_bank_id=tenant_bank_id, rail_type=brk.rail_type, transaction_id=brk.transaction_id)
            .one_or_none()
        )
        occurred = _parse_occurred_at(event.transaction_occurred_at) if event else None
        claims.append({
            "signal_type": "reconciliation_break",
            "reference_id": str(brk.id),
            "amount": abs(brk.amount) if brk.amount is not None else 0.0,
            "rail_type": brk.rail_type,
            "week_start": _week_start(occurred) if occurred else None,
        })
    return claims


def _operational_claims(db: Session, tenant_bank_id: str) -> list[dict[str, Any]]:
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
            events = (
                db.query(CanonicalEvent)
                .filter_by(tenant_bank_id=tenant_bank_id, transaction_id=issue.reference_id)
                .all()
            )
        else:  # BATCH_NOT_SETTLED -- every transaction in the overdue batch
            events = (
                db.query(CanonicalEvent)
                .filter_by(tenant_bank_id=tenant_bank_id, batch_id=issue.reference_id)
                .all()
            )
        amount = sum(abs(e.amount) for e in events if e.amount is not None)
        occurred = _parse_occurred_at(events[0].transaction_occurred_at) if events else None
        rail_type = events[0].rail_type if events else None
        claims.append({
            "signal_type": "operational_issue",
            "reference_id": str(issue.id),
            "amount": amount,
            "rail_type": rail_type,
            "week_start": _week_start(occurred) if occurred else None,
        })
    return claims


def _all_claims(db: Session, tenant_bank_id: str) -> list[dict[str, Any]]:
    return (
        _fraud_anomaly_claims(db, tenant_bank_id)
        + _reconciliation_claims(db, tenant_bank_id)
        + _operational_claims(db, tenant_bank_id)
    )


def get_payment_normalcy(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """"Payments completed normally" -- the fraction of real transactions
    NOT touched by any detected reconciliation break or transaction/batch-
    level operational issue. Deliberately transaction-level, unlike
    exposure-by-domain: fraud/anomaly and Funnel Account are entity- or
    beneficiary-level judgments, not "this specific payment had a
    problem," so they're excluded here the same way the two rate-based
    operational issue types are excluded from _all_claims()'s $ figures --
    there's no single transaction to mark as "not normal" for either.
    """
    total_transactions = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id).count()

    touched: set[tuple[str, str]] = set()
    for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        touched.add((brk.rail_type, brk.transaction_id))

    for issue in (
        db.query(OperationalIssue)
        .filter(OperationalIssue.tenant_bank_id == tenant_bank_id, OperationalIssue.issue_type.in_(_TRANSACTION_LEVEL_ISSUE_TYPES))
        .all()
    ):
        event = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, transaction_id=issue.reference_id).first()
        if event:
            touched.add((event.rail_type, event.transaction_id))

    for issue in (
        db.query(OperationalIssue)
        .filter(OperationalIssue.tenant_bank_id == tenant_bank_id, OperationalIssue.issue_type.in_(_BATCH_LEVEL_ISSUE_TYPES))
        .all()
    ):
        for event in db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, batch_id=issue.reference_id).all():
            touched.add((event.rail_type, event.transaction_id))

    return {
        "rate": ((total_transactions - len(touched)) / total_transactions) if total_transactions else None,
        "total_transactions": total_transactions,
        "touched_transactions": len(touched),
    }


def get_exposure_by_domain(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    claims = _all_claims(db, tenant_bank_id)
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


def get_exposure_trend(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Weekly, not daily -- this dataset's real granularity (Track A's own
    windowing is weekly; detection-run timestamps cluster on whichever day
    the pipeline happened to run, so bucketing by *that* would produce a
    fake-looking single-day spike, not a real trend). Bucketed by the real
    week the underlying transaction occurred (Monday-start, same
    convention as app/anomaly/features.py), not by when it was detected.
    """
    claims = _all_claims(db, tenant_bank_id)
    by_week: dict[datetime, float] = defaultdict(float)
    for claim in claims:
        if claim["week_start"] is not None:
            by_week[claim["week_start"]] += claim["amount"]

    points = [
        {"week_start": week.date().isoformat(), "amount": round(amount, 2)}
        for week, amount in sorted(by_week.items())
    ]
    return {"points": points}


def get_mitigation_progress(db: Session, tenant_bank_id: str) -> dict[str, Any]:
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
    claims = _all_claims(db, tenant_bank_id)
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


def get_mitigation_progress_by_domain(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Same residual/mitigated split as get_mitigation_progress(), broken
    out per engine instead of combined into one total -- what the
    Anomalies page's "Mitigated vs. Residual Exposure" column reads.
    """
    claims = _all_claims(db, tenant_bank_id)
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


def get_payment_value_by_rail(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Protected vs Impacted per real rail (ACH/WIRE/CARD/FEDNOW/CHEQUE --
    never a rail that doesn't exist in this schema). Impacted is only ever
    computed from Reconciliation breaks (the only claim type with a
    definite per-rail amount and rail_type on every row); Protected is
    each rail's real total transaction volume minus that.
    """
    rail_totals: dict[str, float] = defaultdict(float)
    for (rail_type, amount) in db.query(CanonicalEvent.rail_type, CanonicalEvent.amount).filter(
        CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.amount.isnot(None),
    ).all():
        rail_totals[rail_type] += amount

    impacted_by_rail: dict[str, float] = defaultdict(float)
    for claim in _reconciliation_claims(db, tenant_bank_id):
        if claim["rail_type"]:
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
