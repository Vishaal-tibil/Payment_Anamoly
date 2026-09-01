"""Analyst persona component -- Investigation Queue + Insights
(Overview/Anomalies/Payment Rails/Detection Performance). Read-only over
data the Pipeline router's *_compute endpoints already produced, plus the
LLM narration and Investigation Cases writes below.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agent.models import AgentNarrative
from ..agent.narration import (
    facts_for_beneficiary_snapshot,
    facts_for_entity_snapshot,
    facts_for_operational_issue,
    facts_for_reconciliation_break,
    get_or_create_narrative,
)
from ..anomaly.categories import FUNNEL_ACCOUNT_THRESHOLD, categories_for_snapshots
from ..anomaly.models import BeneficiarySnapshot, EntitySnapshot
from ..dashboard import (
    get_detection_attention,
    get_detection_performance,
    get_overview,
    get_priority_distribution,
    get_rail_stats,
)
from ..database import get_db
from ..exposure import (
    get_anomaly_heatmap,
    get_exposure_by_domain,
    get_exposure_by_rail,
    get_exposure_trend,
    get_mitigation_progress,
    get_mitigation_progress_by_domain,
    get_payment_normalcy,
    get_payment_value_by_rail,
)
from ..investigation.cases import compute_cases as compute_investigation_cases
from ..investigation.cases import get_anomaly_type_counts
from ..investigation.models import InvestigationCase, InvestigationCaseAlert
from ..review.service import get_review_quality_trend_daily
from ..operations.models import OperationalIssue
from ..reconciliation.models import ReconciliationBreak

router = APIRouter()


def _snapshot_summary(s: EntitySnapshot, matched_categories: list[str] | None = None) -> dict:
    return {
        "id": s.id,
        "matched_categories": matched_categories or [],
        "party_id": s.party_id,
        "party_type": s.party_type,
        "tenant_bank_id": s.tenant_bank_id,
        "segment": s.segment,
        "window_type": s.window_type,
        "window_start": s.window_start,
        "window_end": s.window_end,
        "transaction_count": s.transaction_count,
        "amount_total": s.amount_total,
        "amount_avg": s.amount_avg,
        "amount_median": s.amount_median,
        "amount_std": s.amount_std,
        "unique_counterparties": s.unique_counterparties,
        "new_counterparty_ratio": s.new_counterparty_ratio,
        "retry_ratio": s.retry_ratio,
        "avg_response_time_ms": s.avg_response_time_ms,
        "timeout_ratio": s.timeout_ratio,
        "format_reject_ratio": s.format_reject_ratio,
        "rails_used": s.rails_used,
        "account_age_days": s.account_age_days,
        "split": s.split,
        "computed_at": s.computed_at,
        "isolation_forest_score": s.isolation_forest_score,
        "cluster_id": s.cluster_id,
        "cluster_changed": s.cluster_changed,
        "timeseries_drift_score": s.timeseries_drift_score,
        "final_anomaly_score": s.final_anomaly_score,
        "anomaly_band": s.anomaly_band,
    }


@router.get("/anomaly/snapshots")
async def list_snapshots(
    tenant_bank_id: str,
    party_id: str | None = None,
    segment: str | None = None,
    split: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(EntitySnapshot).filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    if party_id:
        base_query = base_query.filter(EntitySnapshot.party_id == party_id)
    if segment:
        base_query = base_query.filter(EntitySnapshot.segment == segment.upper())
    if split:
        base_query = base_query.filter(EntitySnapshot.split == split.lower())
    total = base_query.count()
    rows = (
        base_query.order_by(EntitySnapshot.party_id, EntitySnapshot.window_end)
        .offset(offset)
        .limit(limit)
        .all()
    )

    # Computed against each row's whole segment population, not just this
    # page -- a row's tags shouldn't change depending on pagination. Cheap
    # at this data volume (one query per distinct segment on the page).
    category_map: dict[int, list[str]] = {}
    for seg in {s.segment for s in rows}:
        category_map.update(categories_for_snapshots(db, tenant_bank_id, seg))

    return {
        "total": total,
        "snapshots": [_snapshot_summary(s, category_map.get(s.id)) for s in rows],
    }


@router.get("/anomaly/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    snapshot = db.get(EntitySnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="entity snapshot not found")
    # matched_categories is relative to this snapshot's whole segment
    # population (same reasoning list_snapshots() above uses) -- not
    # something derivable from this one row alone.
    category_map = categories_for_snapshots(db, snapshot.tenant_bank_id, snapshot.segment)
    return _snapshot_summary(snapshot, category_map.get(snapshot.id))


def _beneficiary_snapshot_summary(s: BeneficiarySnapshot) -> dict:
    return {
        "id": s.id,
        "beneficiary_key": s.beneficiary_key,
        "beneficiary_name": s.beneficiary_name,
        "tenant_bank_id": s.tenant_bank_id,
        "window_start": s.window_start,
        "window_end": s.window_end,
        "transaction_count": s.transaction_count,
        "amount_total": s.amount_total,
        "distinct_senders": s.distinct_senders,
        "distinct_new_senders": s.distinct_new_senders,
        "new_sender_ratio": s.new_sender_ratio,
        "sender_party_types": s.sender_party_types,
        "funnel_drift_score": s.funnel_drift_score,
        "matched_categories": (
            ["Funnel Account"]
            if s.funnel_drift_score is not None and s.funnel_drift_score >= FUNNEL_ACCOUNT_THRESHOLD
            else []
        ),
        "computed_at": s.computed_at,
    }


@router.get("/anomaly/beneficiary-snapshots")
async def list_beneficiary_snapshots(
    tenant_bank_id: str,
    beneficiary_key: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(BeneficiarySnapshot).filter(BeneficiarySnapshot.tenant_bank_id == tenant_bank_id)
    if beneficiary_key:
        base_query = base_query.filter(BeneficiarySnapshot.beneficiary_key == beneficiary_key)
    total = base_query.count()
    rows = (
        base_query.order_by(BeneficiarySnapshot.beneficiary_key, BeneficiarySnapshot.window_start)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "snapshots": [_beneficiary_snapshot_summary(s) for s in rows],
    }


@router.get("/anomaly/beneficiary-snapshots/{snapshot_id}")
async def get_beneficiary_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    snapshot = db.get(BeneficiarySnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="beneficiary snapshot not found")
    return _beneficiary_snapshot_summary(snapshot)


def _operational_issue_summary(issue: OperationalIssue) -> dict:
    return {
        "id": issue.id,
        "issue_type": issue.issue_type,
        "tenant_bank_id": issue.tenant_bank_id,
        "reference_type": issue.reference_type,
        "reference_id": issue.reference_id,
        "window_start": issue.window_start,
        "window_end": issue.window_end,
        "severity_score": issue.severity_score,
        "details": issue.details,
        "detected_at": issue.detected_at,
    }


@router.get("/operations/issues")
async def list_operational_issues(
    tenant_bank_id: str,
    issue_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(OperationalIssue).filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    if issue_type:
        base_query = base_query.filter(OperationalIssue.issue_type == issue_type.upper())
    total = base_query.count()
    rows = (
        base_query.order_by(OperationalIssue.detected_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "issues": [_operational_issue_summary(i) for i in rows],
    }


@router.get("/operations/issues/{issue_id}")
async def get_operational_issue(issue_id: int, db: Session = Depends(get_db)):
    issue = db.get(OperationalIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="operational issue not found")
    return _operational_issue_summary(issue)


def _reconciliation_break_summary(row: ReconciliationBreak) -> dict:
    return {
        "id": row.id,
        "tenant_bank_id": row.tenant_bank_id,
        "transaction_id": row.transaction_id,
        "rail_type": row.rail_type,
        "detection_type": row.detection_type,
        "source_reconciliation_status": row.source_reconciliation_status,
        "variance_amount": row.variance_amount,
        "amount": row.amount,
        "details": row.details,
        "detected_at": row.detected_at,
    }


@router.get("/reconciliation/breaks")
async def list_reconciliation_breaks(
    tenant_bank_id: str,
    detection_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(ReconciliationBreak).filter(ReconciliationBreak.tenant_bank_id == tenant_bank_id)
    if detection_type:
        base_query = base_query.filter(ReconciliationBreak.detection_type == detection_type.upper())
    total = base_query.count()
    rows = (
        base_query.order_by(ReconciliationBreak.detected_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "breaks": [_reconciliation_break_summary(r) for r in rows],
    }


@router.get("/reconciliation/breaks/{break_id}")
async def get_reconciliation_break(break_id: int, db: Session = Depends(get_db)):
    brk = db.get(ReconciliationBreak, break_id)
    if brk is None:
        raise HTTPException(status_code=404, detail="reconciliation break not found")
    return _reconciliation_break_summary(brk)


@router.get("/dashboard/overview")
async def dashboard_overview(tenant_bank_id: str, db: Session = Depends(get_db)):
    return get_overview(db, tenant_bank_id)


@router.get("/dashboard/rails")
async def dashboard_rails(tenant_bank_id: str, db: Session = Depends(get_db)):
    return get_rail_stats(db, tenant_bank_id)


@router.get("/dashboard/detection-performance")
async def dashboard_detection_performance(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Real detection-performance metrics -- see get_detection_performance's
    docstring for why confirmation/false-positive rate are grounded in the
    analyst review workflow (real, but grows more meaningful as review
    activity accumulates) while coverage and exposure-identified-early are
    fully real right now.
    """
    return get_detection_performance(db, tenant_bank_id)


@router.get("/dashboard/exposure")
async def dashboard_exposure(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Real-dollar exposure views -- see app/exposure.py for exactly what
    "exposure" and "mitigated" mean here and why. Combined into one call
    since the Overview and Anomalies pages each read a subset of the same
    underlying real claims (see app/exposure.py's _all_claims()).
    """
    return {
        "by_domain": get_exposure_by_domain(db, tenant_bank_id),
        "trend": get_exposure_trend(db, tenant_bank_id),
        "mitigation": get_mitigation_progress(db, tenant_bank_id),
        "mitigation_by_domain": get_mitigation_progress_by_domain(db, tenant_bank_id),
        "payment_value_by_rail": get_payment_value_by_rail(db, tenant_bank_id),
        "normalcy": get_payment_normalcy(db, tenant_bank_id),
    }


@router.get("/dashboard/exposure-by-rail")
async def dashboard_exposure_by_rail(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Real-dollar exposure broken down by rail instead of by domain --
    see get_exposure_by_rail()'s docstring for why Fraud/Anomaly claims
    are excluded (no single rail to attribute them to).
    """
    return get_exposure_by_rail(db, tenant_bank_id)


@router.get("/dashboard/priority-distribution")
async def dashboard_priority_distribution(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Critical/High/Medium/Low counts across all three engines -- see
    get_priority_distribution()'s docstring for the exact bucketing rule
    (a heuristic, since no engine stores a "priority" field itself).
    """
    return get_priority_distribution(db, tenant_bank_id)


@router.get("/dashboard/anomaly-heatmap")
async def dashboard_anomaly_heatmap(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Rail x category cross-tab -- serves both Overview's heatmap and
    Payment Rails' "Anomalies by Rail" breakdown, same underlying data.
    """
    return get_anomaly_heatmap(db, tenant_bank_id)


@router.get("/dashboard/quality-trend-daily")
async def dashboard_quality_trend_daily(tenant_bank_id: str, days: int = 7, db: Session = Depends(get_db)):
    """Confirmed/dismissed counts per calendar day over this tenant's own
    recent review activity -- see get_review_quality_trend_daily()'s
    docstring for why "recent" is relative to the data's own timeline,
    not wall-clock now().
    """
    return {"days": get_review_quality_trend_daily(db, tenant_bank_id, days=days)}


@router.get("/dashboard/detection-attention")
async def dashboard_detection_attention(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Rails whose success_rate sits below this tenant's own cross-rail
    average -- see get_detection_attention()'s docstring.
    """
    return get_detection_attention(db, tenant_bank_id)


_FACT_BUILDERS = {
    "operational_issue": (OperationalIssue, facts_for_operational_issue),
    "reconciliation_break": (ReconciliationBreak, facts_for_reconciliation_break),
    "fraud_anomaly": (EntitySnapshot, facts_for_entity_snapshot),
    "funnel_account": (BeneficiarySnapshot, facts_for_beneficiary_snapshot),
}


class NarrateRequest(BaseModel):
    signal_type: str  # "operational_issue" | "reconciliation_break" | "fraud_anomaly" | "funnel_account"
    signal_id: int  # the primary key of the OperationalIssue/ReconciliationBreak/EntitySnapshot/BeneficiarySnapshot row
    tenant_bank_id: str
    force: bool = False  # regenerate even if a cached narrative already exists


def _narrative_summary(n: AgentNarrative) -> dict:
    return {
        "id": n.id,
        "signal_type": n.signal_type,
        "reference_id": n.reference_id,
        "tenant_bank_id": n.tenant_bank_id,
        "title": n.title,
        "description": n.description,
        "recommended_action": {
            "title": n.recommended_action_title,
            "description": n.recommended_action_description,
        },
        "model": n.model,
        "generated_at": n.generated_at,
    }


@router.post("/agent/narrate")
async def narrate_endpoint(body: NarrateRequest, db: Session = Depends(get_db)):
    builder = _FACT_BUILDERS.get(body.signal_type)
    if builder is None:
        raise HTTPException(status_code=400, detail=f"signal_type must be one of {sorted(_FACT_BUILDERS)}")
    model_cls, facts_fn = builder

    row = (
        db.query(model_cls)
        .filter_by(id=body.signal_id, tenant_bank_id=body.tenant_bank_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No {body.signal_type} row with id={body.signal_id} for this tenant")

    try:
        narrative = await get_or_create_narrative(
            db, body.signal_type, str(body.signal_id), body.tenant_bank_id, facts_fn(row), force=body.force,
        )
    except RuntimeError as exc:  # MISTRAL_API_KEY not set
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # Mistral API error, malformed response, etc.
        raise HTTPException(status_code=502, detail=f"Narration failed: {exc}") from exc

    return _narrative_summary(narrative)


@router.get("/agent/narratives")
async def list_narratives(
    tenant_bank_id: str,
    signal_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(AgentNarrative).filter(AgentNarrative.tenant_bank_id == tenant_bank_id)
    if signal_type:
        base_query = base_query.filter(AgentNarrative.signal_type == signal_type)
    total = base_query.count()
    rows = (
        base_query.order_by(AgentNarrative.generated_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "narratives": [_narrative_summary(n) for n in rows],
    }


# --- Investigation Cases (Investigation Queue) ---
# Groups existing OperationalIssue/ReconciliationBreak/EntitySnapshot rows
# into cases -- see app/investigation/cases.py for the clustering rule.
# validation_status below is display-only: it never touches
# analyst_reviews or any Detection Performance metric (no feedback loop
# yet, by design -- see app/investigation package docstring).

class ComputeCasesRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/investigation/cases/compute")
async def compute_cases_endpoint(
    body: ComputeCasesRequest = ComputeCasesRequest(),
    db: Session = Depends(get_db),
):
    return compute_investigation_cases(db, tenant_bank_id=body.tenant_bank_id)


@router.get("/investigation/anomaly-types")
async def list_anomaly_type_counts(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Ranked real named types ("Failure-rate spike," "Batch never
    settles," etc.) from InvestigationCaseAlert -- see
    get_anomaly_type_counts()'s docstring.
    """
    return get_anomaly_type_counts(db, tenant_bank_id=tenant_bank_id)


def _investigation_case_summary(case: InvestigationCase) -> dict:
    return {
        "id": case.id,
        "case_code": case.case_code,
        "tenant_bank_id": case.tenant_bank_id,
        "category": case.category,
        "payment_rail": case.payment_rail,
        "title": case.title,
        "current_exposure": case.current_exposure,
        "transactions_affected": case.transactions_affected,
        "contributing_alerts_count": case.contributing_alerts_count,
        "validation_status": case.validation_status,
        "opened_at": case.opened_at,
        "updated_at": case.updated_at,
    }


def _investigation_case_alert_summary(alert: InvestigationCaseAlert) -> dict:
    return {
        "id": alert.id,
        "alert_code": alert.alert_code,
        "source_type": alert.source_type,
        "source_id": alert.source_id,
        "transaction_id": alert.transaction_id,
        "payment_rail": alert.payment_rail,
        "anomaly_category": alert.anomaly_category,
        "anomaly_type": alert.anomaly_type,
        "description": alert.description,
        "detected_at": alert.detected_at,
    }


@router.get("/investigation/cases")
async def list_investigation_cases(
    tenant_bank_id: str,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(InvestigationCase).filter(InvestigationCase.tenant_bank_id == tenant_bank_id)
    total = base_query.count()
    rows = (
        base_query.order_by(InvestigationCase.opened_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "cases": [_investigation_case_summary(c) for c in rows],
    }


@router.get("/investigation/cases/{case_id}")
async def get_investigation_case(case_id: int, db: Session = Depends(get_db)):
    case = db.get(InvestigationCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="investigation case not found")

    alerts = (
        db.query(InvestigationCaseAlert)
        .filter_by(case_id=case_id)
        .order_by(InvestigationCaseAlert.detected_at)
        .all()
    )
    return {
        **_investigation_case_summary(case),
        "alerts": [_investigation_case_alert_summary(a) for a in alerts],
    }


_CASE_VALIDATION_STATUSES = ("PENDING", "VALID", "INVALID")


class ValidateCaseRequest(BaseModel):
    validation_status: str  # "PENDING" | "VALID" | "INVALID"


@router.post("/investigation/cases/{case_id}/validate")
async def validate_investigation_case(case_id: int, body: ValidateCaseRequest, db: Session = Depends(get_db)):
    if body.validation_status not in _CASE_VALIDATION_STATUSES:
        raise HTTPException(status_code=400, detail=f"validation_status must be one of {_CASE_VALIDATION_STATUSES}")

    case = db.get(InvestigationCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="investigation case not found")

    case.validation_status = body.validation_status
    db.commit()
    db.refresh(case)
    return _investigation_case_summary(case)
