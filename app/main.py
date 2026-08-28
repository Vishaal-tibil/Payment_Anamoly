from __future__ import annotations

from contextlib import asynccontextmanager
from enum import Enum

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models  # noqa: F401  -- registers tables on Base.metadata
from .anomaly import models as anomaly_models  # noqa: F401  -- registers anomaly_entity_snapshots
from .anomaly.features import compute_snapshots
from .anomaly.models import EntitySnapshot
from .database import Base, SessionLocal, engine, get_db
from .feature_store import compute_features
from .ingestion import process_file
from .models import CanonicalEvent, Individual, Merchant, PartyFeatures
from .operations import models as operations_models  # noqa: F401  -- registers operational_issues
from .operations.drift import detect_timeout_spikes
from .operations.duplicate_payment import detect_duplicate_payments
from .operations.format_rejection import detect_format_rejections
from .operations.models import OperationalIssue
from .operations.rules import detect_unsettled_batches
from .resolution import resolve_parties
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


app = FastAPI(title="Merchant Payment Intelligence - Ingestion Foundation", lifespan=lifespan)


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


def _snapshot_summary(s: EntitySnapshot) -> dict:
    return {
        "id": s.id,
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
    return {
        "total": total,
        "snapshots": [_snapshot_summary(s) for s in rows],
    }


# --- Operational Issues engine (Step 6b) ---
# Separate from the anomaly engine above -- see app/operations/__init__.py
# for why file_reached_settlement/timeout_ratio/idempotency_key/
# format_validation_status are read directly here rather than treated as
# leakage. Four issue types implemented: BATCH_NOT_SETTLED
# (deterministic), NETWORK_TIMEOUT_SPIKE (rolling z-score),
# DUPLICATE_PAYMENT (exact-key join), FORMAT_REJECTION (listing half only).

class ComputeOperationalIssuesRequest(BaseModel):
    tenant_bank_id: str | None = None


@app.post("/operations/issues/compute")
async def compute_operational_issues_endpoint(
    body: ComputeOperationalIssuesRequest = ComputeOperationalIssuesRequest(),
    db: Session = Depends(get_db),
):
    batch_result = detect_unsettled_batches(db, tenant_bank_id=body.tenant_bank_id)
    timeout_result = detect_timeout_spikes(db, tenant_bank_id=body.tenant_bank_id)
    duplicate_result = detect_duplicate_payments(db, tenant_bank_id=body.tenant_bank_id)
    format_result = detect_format_rejections(db, tenant_bank_id=body.tenant_bank_id)
    return {
        "batch_not_settled": batch_result,
        "network_timeout_spike": timeout_result,
        "duplicate_payment": duplicate_result,
        "format_rejection": format_result,
    }


def _operational_issue_summary(i: OperationalIssue) -> dict:
    return {
        "id": i.id,
        "issue_type": i.issue_type,
        "tenant_bank_id": i.tenant_bank_id,
        "reference_type": i.reference_type,
        "reference_id": i.reference_id,
        "window_start": i.window_start,
        "window_end": i.window_end,
        "severity_score": i.severity_score,
        "details": i.details,
        "detected_at": i.detected_at,
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
