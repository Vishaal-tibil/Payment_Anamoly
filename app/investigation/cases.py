"""Case clustering: groups existing OperationalIssue/ReconciliationBreak/
EntitySnapshot rows into InvestigationCase rows for the Analyst
persona's Investigation Queue.

Clustering rule: same category (issue_type / detection_type / fraud
anomaly_band) + same payment rail + detected within a rolling window of
the previous member (_CLUSTER_WINDOW). Party-level rate-spike issue
types (NETWORK_TIMEOUT_SPIKE, FORMAT_REJECTION_SPIKE) have no single
rail -- a party can transact on several -- so those group by category +
time window only, rail left null on those cases.

Fully derived -- each run deletes and rebuilds every case/alert for the
requested scope, same idempotent shape as every other engine here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple
from uuid import uuid4

from sqlalchemy.orm import Session

from ..anomaly.models import EntitySnapshot
from ..models import CanonicalEvent
from ..operations.models import OperationalIssue
from ..reconciliation.models import ReconciliationBreak
from .models import InvestigationCase, InvestigationCaseAlert

# Signals more than this long after the previous member (by detected_at)
# start a new case instead of joining the current one.
_CLUSTER_WINDOW = timedelta(hours=48)

# issue_type values with no single rail to group by -- a merchant/
# individual can transact on several rails, so "same rail" doesn't apply.
_PARTY_LEVEL_ISSUE_TYPES = {"NETWORK_TIMEOUT_SPIKE", "FORMAT_REJECTION_SPIKE"}

_OPERATIONAL_ISSUE_LABELS = {
    "NETWORK_TIMEOUT_SPIKE": "Failure-rate spike",
    "BATCH_NOT_SETTLED": "Batch never settles",
    "DUPLICATE_PAYMENT": "Duplicate payment",
    "FORMAT_REJECTION": "Formatting rejection",
    "FORMAT_REJECTION_SPIKE": "Formatting rejection spike",
}

_RECONCILIATION_LABELS = {
    "CONFIRMED_BREAK": "Confirmed reconciliation break",
    "PROVISIONAL_VARIANCE": "Provisional reconciliation variance",
}


class _Signal(NamedTuple):
    tenant_bank_id: str
    category: str
    payment_rail: str | None
    detected_at: datetime
    source_type: str
    source_id: int
    transaction_id: str | None
    anomaly_category: str
    anomaly_type: str
    description: str
    exposure: float | None
    party_id: str | None = None


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rail_for_operational_issue(db: Session, issue: OperationalIssue) -> str | None:
    if issue.issue_type in _PARTY_LEVEL_ISSUE_TYPES:
        return None
    if issue.reference_type in ("TRANSACTION", "BATCH"):
        filters = {"tenant_bank_id": issue.tenant_bank_id}
        filters["transaction_id" if issue.reference_type == "TRANSACTION" else "batch_id"] = issue.reference_id
        event = db.query(CanonicalEvent).filter_by(**filters).first()
        return event.rail_type if event else None
    return None


def _collect_operational_signals(db: Session, tenant_bank_id: str | None) -> list[_Signal]:
    query = db.query(OperationalIssue)
    if tenant_bank_id:
        query = query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)

    signals = []
    for issue in query.all():
        label = _OPERATIONAL_ISSUE_LABELS.get(issue.issue_type, issue.issue_type)
        signals.append(_Signal(
            tenant_bank_id=issue.tenant_bank_id,
            category=issue.issue_type,
            payment_rail=_rail_for_operational_issue(db, issue),
            detected_at=_as_utc(issue.detected_at),
            source_type="OPERATIONAL_ISSUE",
            source_id=issue.id,
            transaction_id=issue.reference_id if issue.reference_type == "TRANSACTION" else None,
            anomaly_category="Operational",
            anomaly_type=label,
            description=f"{label} detected on {issue.reference_type.lower()} {issue.reference_id}",
            exposure=None,  # OperationalIssue carries no dollar amount directly -- not invented
            party_id=issue.reference_id if issue.reference_type == "PARTY" else None,
        ))
    return signals


def _collect_reconciliation_signals(db: Session, tenant_bank_id: str | None) -> list[_Signal]:
    query = db.query(ReconciliationBreak)
    if tenant_bank_id:
        query = query.filter(ReconciliationBreak.tenant_bank_id == tenant_bank_id)

    signals = []
    for brk in query.all():
        label = _RECONCILIATION_LABELS.get(brk.detection_type, brk.detection_type)
        amount = brk.variance_amount if brk.variance_amount is not None else brk.amount
        signals.append(_Signal(
            tenant_bank_id=brk.tenant_bank_id,
            category=brk.detection_type,
            payment_rail=brk.rail_type,
            detected_at=_as_utc(brk.detected_at),
            source_type="RECONCILIATION_BREAK",
            source_id=brk.id,
            transaction_id=brk.transaction_id,
            anomaly_category="Reconciliation",
            anomaly_type=label,
            description=f"{label} on transaction {brk.transaction_id} ({brk.rail_type})",
            exposure=abs(amount) if amount is not None else None,
        ))
    return signals


def _collect_fraud_signals(db: Session, tenant_bank_id: str | None) -> list[_Signal]:
    query = db.query(EntitySnapshot).filter(EntitySnapshot.anomaly_band.in_(("Critical", "High")))
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)

    signals = []
    for snap in query.all():
        if snap.window_end is None:
            continue
        rails = snap.rails_used or []
        rail = rails[0] if len(rails) == 1 else None  # >1 rail -- no single-rail split, same reasoning as party-level ops issues
        signals.append(_Signal(
            tenant_bank_id=snap.tenant_bank_id,
            category=f"FRAUD_{snap.anomaly_band.upper()}",
            payment_rail=rail,
            detected_at=_as_utc(snap.window_end),
            source_type="ANOMALY_SNAPSHOT",
            source_id=snap.id,
            transaction_id=None,
            anomaly_category="Fraud",
            anomaly_type=f"{snap.anomaly_band} anomaly",
            description=f"{snap.party_id} scored {snap.anomaly_band} (final score {snap.final_anomaly_score})",
            exposure=snap.amount_total,
            party_id=snap.party_id,
        ))
    return signals


def _case_title(category: str, payment_rail: str | None) -> str:
    label = (
        _OPERATIONAL_ISSUE_LABELS.get(category)
        or _RECONCILIATION_LABELS.get(category)
        or category.replace("_", " ").title()
    )
    return f"{payment_rail} {label} Cluster" if payment_rail else f"{label} Cluster"


def _create_case(db: Session, tenant: str, category: str, rail: str | None, cluster: list[_Signal]) -> None:
    transaction_ids = {s.transaction_id for s in cluster if s.transaction_id}
    party_ids = {s.party_id for s in cluster if s.party_id}
    transactions_affected = len(transaction_ids) if transaction_ids else max(len(party_ids), len(cluster))
    exposure_values = [s.exposure for s in cluster if s.exposure is not None]

    case = InvestigationCase(
        case_code=f"CNO-{uuid4().hex[:6].upper()}",
        tenant_bank_id=tenant,
        category=category,
        payment_rail=rail,
        title=_case_title(category, rail),
        current_exposure=sum(exposure_values) if exposure_values else None,
        transactions_affected=transactions_affected,
        contributing_alerts_count=len(cluster),
        validation_status="PENDING",
        opened_at=cluster[0].detected_at,
    )
    db.add(case)
    db.flush()  # populate case.id for the alerts below

    for i, s in enumerate(cluster):
        db.add(InvestigationCaseAlert(
            case_id=case.id,
            tenant_bank_id=tenant,
            alert_code=f"ALT-{rail or 'GEN'}-{case.id}{i:02d}",
            source_type=s.source_type,
            source_id=s.source_id,
            transaction_id=s.transaction_id,
            payment_rail=s.payment_rail,
            anomaly_category=s.anomaly_category,
            anomaly_type=s.anomaly_type,
            description=s.description,
            detected_at=s.detected_at,
        ))


def compute_cases(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds InvestigationCase/InvestigationCaseAlert rows for the
    requested scope. Fully derived -- deletes and rebuilds every run.
    """
    all_signals = (
        _collect_operational_signals(db, tenant_bank_id)
        + _collect_reconciliation_signals(db, tenant_bank_id)
        + _collect_fraud_signals(db, tenant_bank_id)
    )

    alert_delete = db.query(InvestigationCaseAlert)
    case_delete = db.query(InvestigationCase)
    if tenant_bank_id:
        alert_delete = alert_delete.filter(InvestigationCaseAlert.tenant_bank_id == tenant_bank_id)
        case_delete = case_delete.filter(InvestigationCase.tenant_bank_id == tenant_bank_id)
    alert_delete.delete(synchronize_session=False)
    case_delete.delete(synchronize_session=False)

    grouped: dict[tuple[str, str, str | None], list[_Signal]] = defaultdict(list)
    for s in all_signals:
        grouped[(s.tenant_bank_id, s.category, s.payment_rail)].append(s)

    cases_created = 0
    alerts_grouped = 0
    for (tenant, category, rail), signals in grouped.items():
        signals.sort(key=lambda s: s.detected_at)
        cluster: list[_Signal] = []
        for s in signals:
            if cluster and (s.detected_at - cluster[-1].detected_at) > _CLUSTER_WINDOW:
                _create_case(db, tenant, category, rail, cluster)
                cases_created += 1
                alerts_grouped += len(cluster)
                cluster = []
            cluster.append(s)
        if cluster:
            _create_case(db, tenant, category, rail, cluster)
            cases_created += 1
            alerts_grouped += len(cluster)

    db.commit()

    return {"cases_created": cases_created, "alerts_grouped": alerts_grouped}
