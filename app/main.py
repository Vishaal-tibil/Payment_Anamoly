from __future__ import annotations

from contextlib import asynccontextmanager
from enum import Enum

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models  # noqa: F401  -- registers tables on Base.metadata
from .agent import models as agent_models  # noqa: F401  -- registers agent_narratives
from .agent.models import AgentNarrative
from .agent.narration import (
    facts_for_beneficiary_snapshot,
    facts_for_entity_snapshot,
    facts_for_operational_issue,
    facts_for_reconciliation_break,
    get_or_create_narrative,
)
from .anomaly import models as anomaly_models  # noqa: F401  -- registers anomaly_entity_snapshots
from .anomaly.beneficiary_features import compute_beneficiary_snapshots
from .anomaly.categories import FUNNEL_ACCOUNT_THRESHOLD, categories_for_snapshots
from .anomaly.clustering import cluster_and_score
from .anomaly.features import compute_snapshots
from .anomaly.isolation_forest import compute_final_score, train_and_score
from .anomaly.models import BeneficiarySnapshot, EntitySnapshot
from .anomaly.timeseries import score_drift, score_funnel_drift
from .dashboard import get_anomaly_detection_categories, get_detection_performance, get_overview, get_rail_stats, get_senior_overview
from .database import Base, SessionLocal, engine, get_db
from .exposure import (
    get_exposure_by_domain,
    get_exposure_trend,
    get_mitigation_progress,
    get_mitigation_progress_by_domain,
    get_payment_normalcy,
    get_payment_value_by_rail,
)
from .feature_store import compute_features
from .health import models as health_models  # noqa: F401  -- registers payment_health_scores
from .health.models import PaymentHealthScore
from .health.scoring import compute_health_scores, get_health_history
from .ingestion import process_file
from .models import CanonicalEvent, Individual, Merchant, PartyFeatures
from .operations import models as operations_models  # noqa: F401  -- registers operational_issues
from .operations.drift import detect_timeout_spikes
from .operations.duplicate_payment import detect_duplicate_payments
from .operations.format_rejection import list_format_rejections, score_format_rejection_drift
from .operations.models import OperationalIssue
from .operations.rules import detect_unsettled_batches
from .reconciliation import models as reconciliation_models  # noqa: F401  -- registers reconciliation_breaks
from .reconciliation.breaks import detect_reconciliation_breaks
from .reconciliation.models import ReconciliationBreak
from .resolution import resolve_parties
from .review import models as review_models  # noqa: F401  -- registers analyst_reviews
from .review.models import STATUSES as REVIEW_STATUSES
from .review.service import get_review, get_review_quality_trend, get_review_summary, set_review
from .seed import seed_sample_mappings_if_empty


class SettlementStage(str, Enum):
    """Structural to the pipeline (drives snapshot_pre/snapshot_post and
    is_pre_settlement), unlike rail_type/tenant_bank_id which are free-form
    config values so a new rail or bank never requires a code change."""

    PRE = "PRE"
    POST = "POST"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_sample_mappings_if_empty(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Merchant Payment Intelligence Platform", lifespan=lifespan)

# Local-dev frontend (Vite) origins. Regex (not a fixed port list)
# because Vite auto-increments past 5173 whenever that port's already
# taken -- still scoped to localhost only. Pilot system, no auth yet --
# tighten this to a real allowlist before any non-local deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/ingest/file")
async def ingest_file(
    tenant_bank_id: str = Form(...),
    rail_type: str = Form(...),
    settlement_stage: SettlementStage = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="file must be .csv, .xlsx, or .xls")

    content = await file.read()
    log = process_file(
        db=db,
        tenant_bank_id=tenant_bank_id.strip(),
        rail_type=rail_type.strip().upper(),
        settlement_stage=settlement_stage.value,
        filename=file.filename,
        content=content,
    )
    return {
        "ingestion_log_id": log.id,
        "file_name": log.file_name,
        "tenant_bank_id": log.tenant_bank_id,
        "rail_type": log.rail_type,
        "settlement_stage": log.settlement_stage,
        "row_count": log.row_count,
        "rows_mapped": log.rows_mapped,
        "rows_failed": log.rows_failed,
        "errors": log.errors,
    }


class ResolvePartiesRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/resolve/parties")
async def resolve_parties_endpoint(
    body: ResolvePartiesRequest = ResolvePartiesRequest(),
    db: Session = Depends(get_db),
):
    return resolve_parties(db, tenant_bank_id=body.tenant_bank_id)


def _merchant_summary(db: Session, merchant: Merchant) -> dict:
    transaction_count = (
        db.query(func.count(CanonicalEvent.id))
        .filter(CanonicalEvent.merchant_id == merchant.merchant_id)
        .scalar()
    )
    return {
        "merchant_id": merchant.merchant_id,
        "source_merchant_id": merchant.source_merchant_id,
        "tenant_bank_id": merchant.tenant_bank_id,
        "legal_name": merchant.legal_name,
        "processor_name": merchant.processor_name,
        "onboarded_by": merchant.onboarded_by,
        "transaction_count": transaction_count,
    }


@app.get("/merchants")
async def list_merchants(
    tenant_bank_id: str,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(Merchant).filter(Merchant.tenant_bank_id == tenant_bank_id)
    total = base_query.count()
    merchants = (
        base_query.order_by(Merchant.created_at)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "merchants": [_merchant_summary(db, m) for m in merchants],
    }


@app.get("/merchants/{merchant_id}")
async def get_merchant(merchant_id: str, db: Session = Depends(get_db)):
    merchant = db.query(Merchant).filter_by(merchant_id=merchant_id).one_or_none()
    if merchant is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    events = db.query(CanonicalEvent).filter(CanonicalEvent.merchant_id == merchant_id).all()
    rails_active = sorted({e.rail_type for e in events})

    return {
        "merchant_id": merchant.merchant_id,
        "source_merchant_id": merchant.source_merchant_id,
        "tenant_bank_id": merchant.tenant_bank_id,
        "legal_name": merchant.legal_name,
        "trade_name": merchant.trade_name,
        "merchant_location": merchant.merchant_location,
        "merchant_account": merchant.merchant_account,
        "processor_name": merchant.processor_name,
        "onboarded_by": merchant.onboarded_by,
        "created_at": merchant.created_at,
        "transaction_count": len(events),
        "rails_active": rails_active,
    }


def _individual_summary(db: Session, individual: Individual) -> dict:
    transaction_count = (
        db.query(func.count(CanonicalEvent.id))
        .filter(CanonicalEvent.individual_id == individual.individual_id)
        .scalar()
    )
    return {
        "individual_id": individual.individual_id,
        "source_individual_id": individual.source_individual_id,
        "tenant_bank_id": individual.tenant_bank_id,
        "full_name": individual.full_name,
        "account_type": individual.account_type,
        "onboarded_by": individual.onboarded_by,
        "transaction_count": transaction_count,
    }


@app.get("/individuals")
async def list_individuals(
    tenant_bank_id: str,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(Individual).filter(Individual.tenant_bank_id == tenant_bank_id)
    total = base_query.count()
    individuals = (
        base_query.order_by(Individual.created_at)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "individuals": [_individual_summary(db, i) for i in individuals],
    }


@app.get("/individuals/{individual_id}")
async def get_individual(individual_id: str, db: Session = Depends(get_db)):
    individual = db.query(Individual).filter_by(individual_id=individual_id).one_or_none()
    if individual is None:
        raise HTTPException(status_code=404, detail="individual not found")

    events = db.query(CanonicalEvent).filter(CanonicalEvent.individual_id == individual_id).all()
    rails_active = sorted({e.rail_type for e in events})

    return {
        "individual_id": individual.individual_id,
        "source_individual_id": individual.source_individual_id,
        "tenant_bank_id": individual.tenant_bank_id,
        "full_name": individual.full_name,
        "account_ref": individual.account_ref,
        "account_type": individual.account_type,
        "onboarded_by": individual.onboarded_by,
        "created_at": individual.created_at,
        "transaction_count": len(events),
        "rails_active": rails_active,
    }


class ComputeFeaturesRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/features/compute")
async def compute_features_endpoint(
    body: ComputeFeaturesRequest = ComputeFeaturesRequest(),
    db: Session = Depends(get_db),
):
    return compute_features(db, tenant_bank_id=body.tenant_bank_id)


def _features_summary(f: PartyFeatures) -> dict:
    return {
        "party_id": f.party_id,
        "party_type": f.party_type,
        "tenant_bank_id": f.tenant_bank_id,
        "transaction_count": f.transaction_count,
        "total_amount": f.total_amount,
        "avg_amount": f.avg_amount,
        "rails_active": f.rails_active,
        "distinct_counterparties": f.distinct_counterparties,
        "new_payee_risk_rate": f.new_payee_risk_rate,
        "funnel_account_rate": f.funnel_account_rate,
        "velocity_breach_rate": f.velocity_breach_rate,
        "structuring_rate": f.structuring_rate,
        "network_timeout_rate": f.network_timeout_rate,
        "is_retry_rate": f.is_retry_rate,
        "format_reject_rate": f.format_reject_rate,
        "reconciliation_break_rate": f.reconciliation_break_rate,
        "first_seen_at": f.first_seen_at,
        "last_seen_at": f.last_seen_at,
        "computed_at": f.computed_at,
    }


@app.get("/features")
async def list_features(
    tenant_bank_id: str,
    party_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    base_query = db.query(PartyFeatures).filter(PartyFeatures.tenant_bank_id == tenant_bank_id)
    if party_type:
        base_query = base_query.filter(PartyFeatures.party_type == party_type.upper())
    total = base_query.count()
    rows = base_query.order_by(PartyFeatures.party_id).offset(offset).limit(limit).all()
    return {
        "total": total,
        "features": [_features_summary(f) for f in rows],
    }


@app.get("/features/{party_id}")
async def get_features(party_id: str, db: Session = Depends(get_db)):
    features = db.query(PartyFeatures).filter_by(party_id=party_id).one_or_none()
    if features is None:
        raise HTTPException(status_code=404, detail="no features computed for this party_id")
    return _features_summary(features)


# --- Anomaly detection engine: Track A (behavioral snapshots) ---
# See app/anomaly/features.py for what this is and why it's separate from
# /features above (Step 5's party_features would leak the pre-computed
# risk flags into model training).

class ComputeSnapshotsRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/anomaly/snapshots/compute")
async def compute_snapshots_endpoint(
    body: ComputeSnapshotsRequest = ComputeSnapshotsRequest(),
    db: Session = Depends(get_db),
):
    return compute_snapshots(db, tenant_bank_id=body.tenant_bank_id)


# --- Anomaly detection engine: Track B (Isolation Forest) ---
# Reads/writes the same EntitySnapshot rows as Track A -- see
# app/anomaly/isolation_forest.py for the model itself. Run
# /anomaly/snapshots/compute first if snapshots are stale; this only
# (re)scores whatever EntitySnapshot rows already exist.

class TrainIsolationForestRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/anomaly/isolation-forest/train")
async def train_isolation_forest_endpoint(
    body: TrainIsolationForestRequest = TrainIsolationForestRequest(),
    db: Session = Depends(get_db),
):
    return train_and_score(db, tenant_bank_id=body.tenant_bank_id)


# --- Anomaly detection engine: Track D (HDBSCAN clustering) ---
# Also reads/writes EntitySnapshot -- see app/anomaly/clustering.py.

class ClusterAndScoreRequest(BaseModel):
    tenant_bank_id: str | None = None
    include_structuring: bool = False  # opt-in: adds near_threshold_ratio via PCA -- see clustering.py


@app.post("/anomaly/clustering/compute")
async def cluster_and_score_endpoint(
    body: ClusterAndScoreRequest = ClusterAndScoreRequest(),
    db: Session = Depends(get_db),
):
    return cluster_and_score(db, tenant_bank_id=body.tenant_bank_id, include_structuring=body.include_structuring)


# --- Anomaly detection engine: Section 8 final aggregation ---
# Combines isolation_forest_score (Track B) + cluster_changed (Track D) +
# timeseries_drift_score (Track C) into final_anomaly_score/anomaly_band.
# Run /anomaly/isolation-forest/train first -- this raises if any targeted
# row is missing isolation_forest_score. Clustering/timeseries are NOT
# required to be populated first (null there just means 0 contribution
# from that signal, not a hard dependency) but should be, for a real score.

class ComputeFinalScoreRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/anomaly/final-score/compute")
async def compute_final_score_endpoint(
    body: ComputeFinalScoreRequest = ComputeFinalScoreRequest(),
    db: Session = Depends(get_db),
):
    try:
        return compute_final_score(db, tenant_bank_id=body.tenant_bank_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.get("/anomaly/snapshots")
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


# --- Anomaly detection engine: Track C (time-series drift) ---

class ScoreDriftRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/anomaly/timeseries/compute")
async def score_drift_endpoint(
    body: ScoreDriftRequest = ScoreDriftRequest(),
    db: Session = Depends(get_db),
):
    return score_drift(db, tenant_bank_id=body.tenant_bank_id)


# --- Anomaly detection engine: Funnel Account (Track A input + Track C scoring) ---
# Deliberately separate from /anomaly/snapshots above -- BeneficiarySnapshot
# is grouped by receiver, not sender. See app/anomaly/beneficiary_features.py.

class ComputeBeneficiarySnapshotsRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/anomaly/beneficiary-snapshots/compute")
async def compute_beneficiary_snapshots_endpoint(
    body: ComputeBeneficiarySnapshotsRequest = ComputeBeneficiarySnapshotsRequest(),
    db: Session = Depends(get_db),
):
    return compute_beneficiary_snapshots(db, tenant_bank_id=body.tenant_bank_id)


class ScoreFunnelDriftRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/anomaly/funnel/compute")
async def score_funnel_drift_endpoint(
    body: ScoreFunnelDriftRequest = ScoreFunnelDriftRequest(),
    db: Session = Depends(get_db),
):
    return score_funnel_drift(db, tenant_bank_id=body.tenant_bank_id)


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


@app.get("/anomaly/beneficiary-snapshots")
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


# --- Operational Issues engine (Step 6b) ---
# A separate engine from the fraud/anomaly one above -- writes to its own
# operational_issues table, never to EntitySnapshot/BeneficiarySnapshot.
# Reading the source's pre-computed operational flags directly (e.g.
# format_validation_status) is fine here, unlike the fraud engine's rule
# against reading its pre-computed risk flags -- see README.

class DetectDuplicatePaymentsRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/operations/duplicate-payments/compute")
async def detect_duplicate_payments_endpoint(
    body: DetectDuplicatePaymentsRequest = DetectDuplicatePaymentsRequest(),
    db: Session = Depends(get_db),
):
    return detect_duplicate_payments(db, tenant_bank_id=body.tenant_bank_id)


class ListFormatRejectionsRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/operations/format-rejections/compute")
async def list_format_rejections_endpoint(
    body: ListFormatRejectionsRequest = ListFormatRejectionsRequest(),
    db: Session = Depends(get_db),
):
    return list_format_rejections(db, tenant_bank_id=body.tenant_bank_id)


class ScoreFormatRejectionDriftRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/operations/format-rejections/spikes/compute")
async def score_format_rejection_drift_endpoint(
    body: ScoreFormatRejectionDriftRequest = ScoreFormatRejectionDriftRequest(),
    db: Session = Depends(get_db),
):
    return score_format_rejection_drift(db, tenant_bank_id=body.tenant_bank_id)


class DetectUnsettledBatchesRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/operations/batches/compute")
async def detect_unsettled_batches_endpoint(
    body: DetectUnsettledBatchesRequest = DetectUnsettledBatchesRequest(),
    db: Session = Depends(get_db),
):
    return detect_unsettled_batches(db, tenant_bank_id=body.tenant_bank_id)


class DetectTimeoutSpikesRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/operations/timeout/compute")
async def detect_timeout_spikes_endpoint(
    body: DetectTimeoutSpikesRequest = DetectTimeoutSpikesRequest(),
    db: Session = Depends(get_db),
):
    return detect_timeout_spikes(db, tenant_bank_id=body.tenant_bank_id)


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


@app.get("/operations/issues")
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


# --- Reconciliation engine (Step 6c) ---
# A separate engine from both the fraud/anomaly one and Operational
# Issues -- writes to its own reconciliation_breaks table. Reads
# reconciliation_status/reconciliation_variance_amount directly: these
# are the source's own completed network-vs-ledger comparison, not a
# fraud verdict, so there's no leakage concern (same reasoning as
# Operational Issues -- see README).

class DetectReconciliationBreaksRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/reconciliation/breaks/compute")
async def detect_reconciliation_breaks_endpoint(
    body: DetectReconciliationBreaksRequest = DetectReconciliationBreaksRequest(),
    db: Session = Depends(get_db),
):
    return detect_reconciliation_breaks(db, tenant_bank_id=body.tenant_bank_id)


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


@app.get("/reconciliation/breaks")
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


# --- Payment Health engine (Step 6d) ---
# The culmination engine -- detects nothing new, rolls up the other
# three engines' already-real output into one bank-wide 0-100 score.
# See app/health/scoring.py for the exact formula and why each weight
# is what it is.

class ComputeHealthRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/health/compute")
async def compute_health_endpoint(
    body: ComputeHealthRequest = ComputeHealthRequest(),
    db: Session = Depends(get_db),
):
    return compute_health_scores(db, tenant_bank_id=body.tenant_bank_id)


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


@app.get("/health/scores")
async def get_health_score(tenant_bank_id: str, db: Session = Depends(get_db)):
    row = db.get(PaymentHealthScore, tenant_bank_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Payment Health score computed yet for {tenant_bank_id} -- call POST /health/compute first",
        )
    return _health_score_summary(row)


@app.get("/health/history")
async def get_health_history_endpoint(tenant_bank_id: str, limit: int = 90, db: Session = Depends(get_db)):
    """Real health-score history for this tenant -- see
    get_health_history()'s docstring for why this can honestly be a
    1-point list today and grow over real time, never backfilled.
    """
    return {"points": get_health_history(db, tenant_bank_id, limit=limit)}


# --- Analyst review workflow ---
# Tracks whether a human has actually looked at a detected claim --
# PENDING until reviewed, then CONFIRMED or DISMISSED. See
# app/review/service.py. This is what makes the analyst view a review
# queue rather than just a read-only list, and what the senior view's
# review-completion numbers are computed from.

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


@app.post("/review/set")
async def set_review_endpoint(body: SetReviewRequest, db: Session = Depends(get_db)):
    if body.status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {REVIEW_STATUSES}")
    row = set_review(
        db, body.signal_type, str(body.signal_id), body.tenant_bank_id,
        body.status, reviewed_by=body.reviewed_by, notes=body.notes,
    )
    return _review_summary(row)


@app.get("/review/status")
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


@app.get("/review/summary")
async def get_review_summary_endpoint(tenant_bank_id: str, db: Session = Depends(get_db)):
    return get_review_summary(db, tenant_bank_id)


# --- Dashboard (frontend) ---
# Read-only aggregation views over the three engines above -- see
# app/dashboard.py. No output table of its own; nothing here computes
# anything new, it only reshapes what's already been computed.

@app.get("/dashboard/overview")
async def dashboard_overview(tenant_bank_id: str, db: Session = Depends(get_db)):
    return get_overview(db, tenant_bank_id)


@app.get("/dashboard/rails")
async def dashboard_rails(tenant_bank_id: str, db: Session = Depends(get_db)):
    return get_rail_stats(db, tenant_bank_id)


@app.get("/dashboard/anomaly-detection-categories")
async def dashboard_anomaly_detection_categories():
    return get_anomaly_detection_categories()


@app.get("/dashboard/detection-performance")
async def dashboard_detection_performance(tenant_bank_id: str, db: Session = Depends(get_db)):
    """Real detection-performance metrics -- see get_detection_performance's
    docstring for why confirmation/false-positive rate are grounded in the
    analyst review workflow (real, but grows more meaningful as review
    activity accumulates) while coverage and exposure-identified-early are
    fully real right now.
    """
    return get_detection_performance(db, tenant_bank_id)


@app.get("/dashboard/senior-overview")
async def dashboard_senior_overview(tenant_bank_id: str, db: Session = Depends(get_db)):
    """The executive rollup: one health score + review completion +
    per-engine finding totals. No per-item detail -- that's the analyst
    view's job (GET /operations/issues, /reconciliation/breaks,
    /anomaly/snapshots, each with its own review status).
    """
    return get_senior_overview(db, tenant_bank_id)


@app.get("/dashboard/exposure")
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


# --- Agent (Step 7, LLM Agent Layer) ---
# Never computes a new signal -- only narrates what the three engines
# above already computed. See app/agent/narration.py's system prompt
# for the rules against fabricating facts not present in the input.

_FACT_BUILDERS = {
    "operational_issue": (OperationalIssue, facts_for_operational_issue),
    "reconciliation_break": (ReconciliationBreak, facts_for_reconciliation_break),
    "fraud_anomaly": (EntitySnapshot, facts_for_entity_snapshot),
    "funnel_account": (BeneficiarySnapshot, facts_for_beneficiary_snapshot),
}


class NarrateRequest(BaseModel):
    signal_type: str  # "operational_issue" | "reconciliation_break" | "fraud_anomaly"
    signal_id: int  # the primary key of the OperationalIssue/ReconciliationBreak/EntitySnapshot row
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


@app.post("/agent/narrate")
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


@app.get("/agent/narratives")
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
