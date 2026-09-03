"""Head of Operations persona component -- the executive rollup (single
Payment Health score, review completion, per-engine finding totals) plus
the underlying analyst review workflow those rollups are computed from.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..dashboard import get_anomaly_detection_categories, get_senior_overview
from ..database import get_db
from ..incident_impact import get_incident_enterprise_impact
from ..health.models import PaymentHealthScore
from ..health.scoring import get_health_history
from ..review.models import STATUSES as REVIEW_STATUSES
from ..review.service import get_review, get_review_summary, set_review

router = APIRouter()


def _health_score_summary(row: PaymentHealthScore) -> dict:
    return {
        "tenant_bank_id": row.tenant_bank_id,
        "health_score": row.health_score,
        "health_band": row.health_band,
        "components": {
            "settlement": row.settlement_component,
            "anomaly": row.anomaly_component,
            "operational": row.operational_component,
            "reconciliation": row.reconciliation_component,
        },
        "facts": {
            "total_transactions": row.total_transactions,
            "settled_transactions": row.settled_transactions,
            "total_scored_entities": row.total_scored_entities,
            "critical_anomaly_count": row.critical_anomaly_count,
            "high_anomaly_count": row.high_anomaly_count,
            "operational_issue_count": row.operational_issue_count,
            "reconciliation_break_count": row.reconciliation_break_count,
        },
        "computed_at": row.computed_at,
    }


@router.get("/health/scores")
async def get_health_score(tenant_bank_id: str, db: Session = Depends(get_db)):
    row = db.get(PaymentHealthScore, tenant_bank_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Payment Health score computed yet for {tenant_bank_id} -- call POST /health/compute first",
        )
    return _health_score_summary(row)


@router.get("/health/history")
async def get_health_history_endpoint(tenant_bank_id: str, limit: int = 90, db: Session = Depends(get_db)):
    """Real health-score history for this tenant -- see
    get_health_history()'s docstring for why this can honestly be a
    1-point list today and grow over real time, never backfilled.
    """
    return {"points": get_health_history(db, tenant_bank_id, limit=limit)}


# --- Analyst review workflow ---
# Tracks whether a human has actually looked at a detected claim --
# PENDING until reviewed, then CONFIRMED or DISMISSED. See
# app/review/service.py. What the senior view's review-completion
# numbers (get_senior_overview) are computed from.

class SetReviewRequest(BaseModel):
    signal_type: str  # "operational_issue" | "reconciliation_break" | "fraud_anomaly"
    signal_id: int  # the primary key of the underlying row, same convention as /agent/narrate
    tenant_bank_id: str
    status: str  # "PENDING" | "CONFIRMED" | "DISMISSED"
    reviewed_by: str | None = None
    notes: str | None = None


def _review_summary(row) -> dict:
    return {
        "signal_type": row.signal_type,
        "reference_id": row.reference_id,
        "tenant_bank_id": row.tenant_bank_id,
        "status": row.status,
        "reviewed_by": row.reviewed_by,
        "notes": row.notes,
        "reviewed_at": row.reviewed_at,
    }


@router.post("/review/set")
async def set_review_endpoint(body: SetReviewRequest, db: Session = Depends(get_db)):
    if body.status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {REVIEW_STATUSES}")
    row = set_review(
        db, body.signal_type, str(body.signal_id), body.tenant_bank_id,
        body.status, reviewed_by=body.reviewed_by, notes=body.notes,
    )
    return _review_summary(row)


@router.get("/review/status")
async def get_review_status_endpoint(
    signal_type: str, signal_id: int, tenant_bank_id: str, db: Session = Depends(get_db),
):
    row = get_review(db, signal_type, str(signal_id), tenant_bank_id)
    if row is None:
        # No review row yet == implicitly PENDING, not a 404 -- the vast
        # majority of claims will never get an explicit row until an
        # analyst acts on them.
        return {
            "signal_type": signal_type, "reference_id": str(signal_id), "tenant_bank_id": tenant_bank_id,
            "status": "PENDING", "reviewed_by": None, "notes": None, "reviewed_at": None,
        }
    return _review_summary(row)


@router.get("/review/summary")
async def get_review_summary_endpoint(tenant_bank_id: str, db: Session = Depends(get_db)):
    return get_review_summary(db, tenant_bank_id)


@router.get("/dashboard/anomaly-detection-categories")
async def dashboard_anomaly_detection_categories():
    return get_anomaly_detection_categories()


@router.get("/incidents/enterprise-impact")
async def incident_enterprise_impact(
    tenant_bank_id: str, signal_type: str, signal_id: int, db: Session = Depends(get_db),
):
    """Real cross-referenced "Enterprise Impact" for one incident (Incident
    Details page) -- see app/incident_impact.py's docstring for exactly
    which of the original 6 rows are real vs. honestly never returned
    (chargeback rate / dispute resolution time have no concept anywhere
    in this schema).
    """
    result = get_incident_enterprise_impact(db, tenant_bank_id, signal_type, signal_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No {signal_type} row with id={signal_id} for this tenant")
    return result


@router.get("/dashboard/senior-overview")
async def dashboard_senior_overview(tenant_bank_id: str, db: Session = Depends(get_db)):
    """The executive rollup: one health score + review completion +
    per-engine finding totals. No per-item detail -- that's the analyst
    view's job (GET /operations/issues, /reconciliation/breaks,
    /anomaly/snapshots, each with its own review status).
    """
    return get_senior_overview(db, tenant_bank_id)
