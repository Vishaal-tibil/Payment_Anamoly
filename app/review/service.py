"""Analyst review: read/write the status of one claim, plus the
tenant-wide summary the senior view reads from.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.categories import FUNNEL_ACCOUNT_THRESHOLD
from ..anomaly.models import BeneficiarySnapshot, EntitySnapshot
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


def _claim_count(db: Session, tenant_bank_id: str) -> dict[str, int]:
    counts = {}
    for signal_type, model in _CLAIM_MODELS.items():
        counts[signal_type] = db.query(model).filter_by(tenant_bank_id=tenant_bank_id).count()
    counts["fraud_anomaly"] = (
        db.query(EntitySnapshot)
        .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.anomaly_band.in_(_MATERIAL_ANOMALY_BANDS))
        .count()
    )
    counts["funnel_account"] = (
        db.query(BeneficiarySnapshot)
        .filter(BeneficiarySnapshot.tenant_bank_id == tenant_bank_id, BeneficiarySnapshot.funnel_drift_score >= FUNNEL_ACCOUNT_THRESHOLD)
        .count()
    )
    return counts


def get_review_summary(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Tenant-wide review completion, the number the senior view leads
    with: how much of what's been detected has an analyst actually
    looked at, broken down by outcome and by which engine found it.
    """
    total_claims_by_type = _claim_count(db, tenant_bank_id)
    total_claims = sum(total_claims_by_type.values())

    reviews = db.query(AnalystReview).filter_by(tenant_bank_id=tenant_bank_id).all()
    confirmed = sum(1 for r in reviews if r.status == CONFIRMED)
    dismissed = sum(1 for r in reviews if r.status == DISMISSED)
    reviewed = confirmed + dismissed
    pending = max(0, total_claims - reviewed)

    by_type = {}
    for signal_type, total in total_claims_by_type.items():
        type_reviews = [r for r in reviews if r.signal_type == signal_type]
        type_reviewed = sum(1 for r in type_reviews if r.status in (CONFIRMED, DISMISSED))
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
