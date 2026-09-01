"""Pipeline component -- ingestion, resolution, feature computation, and
every engine's *_compute trigger. Not consumed directly by either
persona's UI; these are the batch/admin operations that produce the data
the Analyst and Head of Operations routers read.
"""
from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..anomaly.beneficiary_features import compute_beneficiary_snapshots
from ..anomaly.clustering import cluster_and_score
from ..anomaly.features import compute_snapshots
from ..anomaly.isolation_forest import compute_final_score, train_and_score
from ..anomaly.timeseries import score_drift, score_funnel_drift
from ..database import get_db
from ..feature_store import compute_features
from ..health.scoring import compute_health_scores
from ..ingestion import process_file
from ..models import CanonicalEvent, Individual, Merchant, PartyFeatures
from ..operations.drift import detect_timeout_spikes
from ..operations.duplicate_payment import detect_duplicate_payments
from ..operations.format_rejection import list_format_rejections, score_format_rejection_drift
from ..operations.rules import detect_unsettled_batches
from ..reconciliation.breaks import detect_reconciliation_breaks
from ..resolution import resolve_parties

router = APIRouter()


class SettlementStage(str, Enum):
    """Structural to the pipeline (drives snapshot_pre/snapshot_post and
    is_pre_settlement), unlike rail_type/tenant_bank_id which are free-form
    config values so a new rail or bank never requires a code change."""

    PRE = "PRE"
    POST = "POST"


@router.post("/ingest/file")
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


@router.post("/resolve/parties")
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


@router.get("/merchants")
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


@router.get("/merchants/{merchant_id}")
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


@router.get("/individuals")
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


@router.get("/individuals/{individual_id}")
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


@router.post("/features/compute")
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


@router.get("/features")
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


@router.get("/features/{party_id}")
async def get_features(party_id: str, db: Session = Depends(get_db)):
    features = db.query(PartyFeatures).filter_by(party_id=party_id).one_or_none()
    if features is None:
        raise HTTPException(status_code=404, detail="no features computed for this party_id")
    return _features_summary(features)


class ComputeSnapshotsRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/anomaly/snapshots/compute")
async def compute_snapshots_endpoint(
    body: ComputeSnapshotsRequest = ComputeSnapshotsRequest(),
    db: Session = Depends(get_db),
):
    return compute_snapshots(db, tenant_bank_id=body.tenant_bank_id)


class TrainIsolationForestRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/anomaly/isolation-forest/train")
async def train_isolation_forest_endpoint(
    body: TrainIsolationForestRequest = TrainIsolationForestRequest(),
    db: Session = Depends(get_db),
):
    return train_and_score(db, tenant_bank_id=body.tenant_bank_id)


class ClusterAndScoreRequest(BaseModel):
    tenant_bank_id: str | None = None
    include_structuring: bool = False  # opt-in: adds near_threshold_ratio via PCA -- see clustering.py


@router.post("/anomaly/clustering/compute")
async def cluster_and_score_endpoint(
    body: ClusterAndScoreRequest = ClusterAndScoreRequest(),
    db: Session = Depends(get_db),
):
    return cluster_and_score(db, tenant_bank_id=body.tenant_bank_id, include_structuring=body.include_structuring)


class ComputeFinalScoreRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/anomaly/final-score/compute")
async def compute_final_score_endpoint(
    body: ComputeFinalScoreRequest = ComputeFinalScoreRequest(),
    db: Session = Depends(get_db),
):
    try:
        return compute_final_score(db, tenant_bank_id=body.tenant_bank_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ScoreDriftRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/anomaly/timeseries/compute")
async def score_drift_endpoint(
    body: ScoreDriftRequest = ScoreDriftRequest(),
    db: Session = Depends(get_db),
):
    return score_drift(db, tenant_bank_id=body.tenant_bank_id)


class ComputeBeneficiarySnapshotsRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/anomaly/beneficiary-snapshots/compute")
async def compute_beneficiary_snapshots_endpoint(
    body: ComputeBeneficiarySnapshotsRequest = ComputeBeneficiarySnapshotsRequest(),
    db: Session = Depends(get_db),
):
    return compute_beneficiary_snapshots(db, tenant_bank_id=body.tenant_bank_id)


class ScoreFunnelDriftRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/anomaly/funnel/compute")
async def score_funnel_drift_endpoint(
    body: ScoreFunnelDriftRequest = ScoreFunnelDriftRequest(),
    db: Session = Depends(get_db),
):
    return score_funnel_drift(db, tenant_bank_id=body.tenant_bank_id)


class DetectDuplicatePaymentsRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/operations/duplicate-payments/compute")
async def detect_duplicate_payments_endpoint(
    body: DetectDuplicatePaymentsRequest = DetectDuplicatePaymentsRequest(),
    db: Session = Depends(get_db),
):
    return detect_duplicate_payments(db, tenant_bank_id=body.tenant_bank_id)


class ListFormatRejectionsRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/operations/format-rejections/compute")
async def list_format_rejections_endpoint(
    body: ListFormatRejectionsRequest = ListFormatRejectionsRequest(),
    db: Session = Depends(get_db),
):
    return list_format_rejections(db, tenant_bank_id=body.tenant_bank_id)


class ScoreFormatRejectionDriftRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/operations/format-rejections/spikes/compute")
async def score_format_rejection_drift_endpoint(
    body: ScoreFormatRejectionDriftRequest = ScoreFormatRejectionDriftRequest(),
    db: Session = Depends(get_db),
):
    return score_format_rejection_drift(db, tenant_bank_id=body.tenant_bank_id)


class DetectUnsettledBatchesRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/operations/batches/compute")
async def detect_unsettled_batches_endpoint(
    body: DetectUnsettledBatchesRequest = DetectUnsettledBatchesRequest(),
    db: Session = Depends(get_db),
):
    return detect_unsettled_batches(db, tenant_bank_id=body.tenant_bank_id)


class DetectTimeoutSpikesRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/operations/timeout/compute")
async def detect_timeout_spikes_endpoint(
    body: DetectTimeoutSpikesRequest = DetectTimeoutSpikesRequest(),
    db: Session = Depends(get_db),
):
    return detect_timeout_spikes(db, tenant_bank_id=body.tenant_bank_id)


class DetectReconciliationBreaksRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/reconciliation/breaks/compute")
async def detect_reconciliation_breaks_endpoint(
    body: DetectReconciliationBreaksRequest = DetectReconciliationBreaksRequest(),
    db: Session = Depends(get_db),
):
    return detect_reconciliation_breaks(db, tenant_bank_id=body.tenant_bank_id)


class ComputeHealthRequest(BaseModel):
    tenant_bank_id: str | None = None


@router.post("/health/compute")
async def compute_health_endpoint(
    body: ComputeHealthRequest = ComputeHealthRequest(),
    db: Session = Depends(get_db),
):
    return compute_health_scores(db, tenant_bank_id=body.tenant_bank_id)
