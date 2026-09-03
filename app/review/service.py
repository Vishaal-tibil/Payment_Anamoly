"""Analyst review: read/write the status of one claim, plus the
tenant-wide summary the senior view reads from.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.categories import FUNNEL_ACCOUNT_THRESHOLD
from ..anomaly.models import BeneficiarySnapshot, EntitySnapshot
from ..canonical_event_lookup import CanonicalEventLookup
from ..claim_dates import operational_issue_date, reconciliation_break_date
from ..date_filter import date_in_range
from ..operations.models import OperationalIssue
from ..reconciliation.models import ReconciliationBreak
from .models import CONFIRMED, DISMISSED, PENDING, STATUSES, AnalystReview

# Only High/Critical snapshots count as a reviewable "claim" -- Normal/
# Low-Medium rows aren't a detected issue, same scope the Anomalies page
# itself already filters "Material Anomaly Events" to.
_MATERIAL_ANOMALY_BANDS = ("High", "Critical")

_CLAIM_MODELS: dict[str, type] = {
    "operational_issue": OperationalIssue,
    "reconciliation_break": ReconciliationBreak,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _review_id(signal_type: str, tenant_bank_id: str, reference_id: str) -> str:
    return f"{signal_type}:{tenant_bank_id}:{reference_id}"


def get_review(db: Session, signal_type: str, reference_id: str, tenant_bank_id: str) -> AnalystReview | None:
    """Returns the review row if one exists, else None (== implicitly
    PENDING -- callers that need a status string, not a row, should
    treat a None here as PENDING rather than treating it as an error).
    """
    return db.get(AnalystReview, _review_id(signal_type, tenant_bank_id, reference_id))


def set_review(
    db: Session,
    signal_type: str,
    reference_id: str,
    tenant_bank_id: str,
    status: str,
    reviewed_by: str | None = None,
    notes: str | None = None,
) -> AnalystReview:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")

    review_id = _review_id(signal_type, tenant_bank_id, reference_id)
    row = db.get(AnalystReview, review_id)
    if row is None:
        row = AnalystReview(
            id=review_id, signal_type=signal_type, reference_id=reference_id, tenant_bank_id=tenant_bank_id,
        )
        db.add(row)

    row.status = status
    row.reviewed_by = reviewed_by
    row.notes = notes
    row.reviewed_at = _utcnow() if status != PENDING else None
    db.commit()
    return row


def _claim_ids_by_type(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, set[str]]:
    """The real reference_id of every claim per signal_type, scoped to the
    real window each claim's own condition occurred in (never detected_at
    -- see app/claim_dates.py). Returns ids, not just counts, so the
    reviewed/pending split below can be computed over exactly the same
    set of claims rather than counting reviews that belong to claims
    outside the window.
    """
    lookup = CanonicalEventLookup(db, tenant_bank_id) if (start_date or end_date) else None

    issues = db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).all()
    breaks = db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all()
    snapshots = (
        db.query(EntitySnapshot)
        .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.anomaly_band.in_(_MATERIAL_ANOMALY_BANDS))
        .all()
    )
    beneficiaries = (
        db.query(BeneficiarySnapshot)
        .filter(BeneficiarySnapshot.tenant_bank_id == tenant_bank_id, BeneficiarySnapshot.funnel_drift_score >= FUNNEL_ACCOUNT_THRESHOLD)
        .all()
    )

    if lookup is None:  # no window requested -- every real claim counts
        return {
            "operational_issue": {str(i.id) for i in issues},
            "reconciliation_break": {str(b.id) for b in breaks},
            "fraud_anomaly": {str(s.id) for s in snapshots},
            "funnel_account": {str(s.id) for s in beneficiaries},
        }

    return {
        "operational_issue": {
            str(i.id) for i in issues if date_in_range(operational_issue_date(lookup, i), start_date, end_date)
        },
        "reconciliation_break": {
            str(b.id) for b in breaks if date_in_range(reconciliation_break_date(lookup, b), start_date, end_date)
        },
        # Both snapshot types carry their own real window_end -- no join needed.
        "fraud_anomaly": {str(s.id) for s in snapshots if date_in_range(s.window_end, start_date, end_date)},
        "funnel_account": {str(s.id) for s in beneficiaries if date_in_range(s.window_end, start_date, end_date)},
    }


def get_review_summary(
    db: Session, tenant_bank_id: str, start_date: date | None = None, end_date: date | None = None,
) -> dict[str, Any]:
    """Tenant-wide review completion, the number the senior view leads
    with: how much of what's been detected has an analyst actually
    looked at, broken down by outcome and by which engine found it.

    start_date/end_date scope this to the claims whose own real condition
    occurred in that window (app/claim_dates.py), and count only reviews
    belonging to those claims -- so review_rate stays a true "of what
    happened in this window, how much have we looked at" rather than
    mixing a windowed denominator with an all-time numerator.
    """
    claim_ids_by_type = _claim_ids_by_type(db, tenant_bank_id, start_date, end_date)
    total_claims = sum(len(ids) for ids in claim_ids_by_type.values())

    all_reviews = db.query(AnalystReview).filter_by(tenant_bank_id=tenant_bank_id).all()
    reviews = [r for r in all_reviews if r.reference_id in claim_ids_by_type.get(r.signal_type, set())]

    confirmed = sum(1 for r in reviews if r.status == CONFIRMED)
    dismissed = sum(1 for r in reviews if r.status == DISMISSED)
    reviewed = confirmed + dismissed
    pending = max(0, total_claims - reviewed)

    by_type = {}
    for signal_type, claim_ids in claim_ids_by_type.items():
        total = len(claim_ids)
        type_reviewed = sum(1 for r in reviews if r.signal_type == signal_type and r.status in (CONFIRMED, DISMISSED))
        by_type[signal_type] = {
            "total_claims": total,
            "reviewed": type_reviewed,
            "pending": max(0, total - type_reviewed),
        }

    return {
        "tenant_bank_id": tenant_bank_id,
        "total_claims": total_claims,
        "pending": pending,
        "confirmed": confirmed,
        "dismissed": dismissed,
        "review_rate": round(reviewed / total_claims, 3) if total_claims else None,
        "by_signal_type": by_type,
    }


def get_review_quality_trend(db: Session, tenant_bank_id: str) -> list[dict[str, Any]]:
    """Real cumulative confirmation/false-positive rate, one point per
    real review action, in the order analysts actually reviewed_at them --
    not a fabricated smooth trend line. Reconstructed entirely from real
    AnalystReview.reviewed_at timestamps already stored (no new snapshot
    table needed, unlike Payment Health's history -- confirmation rate is
    a simple running count, not a composite score computed at one instant).

    Honestly sparse early on: with N total real review actions so far,
    this returns exactly N points. Grows a real point every time an
    analyst confirms or dismisses a claim, same "grows via the feedback
    loop" shape as get_review_summary()'s confirmation_rate.
    """
    reviews = (
        db.query(AnalystReview)
        .filter(
            AnalystReview.tenant_bank_id == tenant_bank_id,
            AnalystReview.status.in_((CONFIRMED, DISMISSED)),
            AnalystReview.reviewed_at.isnot(None),
        )
        .order_by(AnalystReview.reviewed_at.asc())
        .all()
    )

    points = []
    confirmed_so_far = 0
    dismissed_so_far = 0
    for review in reviews:
        if review.status == CONFIRMED:
            confirmed_so_far += 1
        else:
            dismissed_so_far += 1
        reviewed_so_far = confirmed_so_far + dismissed_so_far
        points.append({
            "reviewed_at": review.reviewed_at,
            "confirmation_rate": confirmed_so_far / reviewed_so_far,
            "false_positive_rate": dismissed_so_far / reviewed_so_far,
            "reviewed_count": reviewed_so_far,
        })
    return points


def get_review_quality_trend_daily(db: Session, tenant_bank_id: str, days: int = 7) -> list[dict[str, Any]]:
    """Confirmed/dismissed counts per calendar day, over the last `days`
    days of this tenant's own review activity (not wall-clock "now" --
    same reasoning app/dashboard.py's _new_patterns_detected() uses:
    this is synthetic pilot data with its own fixed timeline, so a
    real-time cutoff would silently go to zero once wall-clock time moves
    past that range). Honestly sparse/empty on days with no real review
    activity -- never backfilled or interpolated.
    """
    reviews = (
        db.query(AnalystReview)
        .filter(
            AnalystReview.tenant_bank_id == tenant_bank_id,
            AnalystReview.status.in_((CONFIRMED, DISMISSED)),
            AnalystReview.reviewed_at.isnot(None),
        )
        .order_by(AnalystReview.reviewed_at.asc())
        .all()
    )
    if not reviews:
        return []

    def _as_utc(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    latest = max(_as_utc(r.reviewed_at) for r in reviews)
    cutoff = (latest - timedelta(days=days - 1)).date()

    by_day: dict[Any, dict[str, int]] = {}
    for review in reviews:
        day = _as_utc(review.reviewed_at).date()
        if day < cutoff:
            continue
        bucket = by_day.setdefault(day, {"confirmed": 0, "dismissed": 0})
        bucket["confirmed" if review.status == CONFIRMED else "dismissed"] += 1

    return [
        {"date": day.isoformat(), "confirmed": counts["confirmed"], "dismissed": counts["dismissed"]}
        for day, counts in sorted(by_day.items())
    ]
