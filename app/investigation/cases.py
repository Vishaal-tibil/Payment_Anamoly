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

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..agent.models import AgentNarrative
from ..anomaly.models import EntitySnapshot
from ..canonical_event_lookup import CanonicalEventLookup
from ..operations.models import OperationalIssue
from ..priority import (
    CRITICAL,
    CRITICAL_MIN,
    HIGH,
    HIGH_MIN,
    LOW,
    MEDIUM,
    MEDIUM_MIN,
    priority_levels_for_breaks,
    priority_levels_for_issues,
)
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

# Representative mid-band severity_score for fraud-sourced alerts --
# EntitySnapshot has no peer-relative severity_score of its own in
# priority.py's 0-100 scale, only its real anomaly_band (Critical/High,
# the only two bands compute_cases() ever clusters -- see
# _collect_fraud_signals). Matches priority.py's own band cutoffs
# (CRITICAL_MIN=85, HIGH_MIN=60) rather than inventing new ones.
_FRAUD_BAND_SEVERITY_SCORE = {"Critical": 90.0, "High": 70.0}


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
    severity_score: float | None
    priority_level: str | None
    party_id: str | None = None


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rail_for_operational_issue(issue: OperationalIssue, lookup: CanonicalEventLookup) -> str | None:
    if issue.issue_type in _PARTY_LEVEL_ISSUE_TYPES:
        return None
    if issue.reference_type == "TRANSACTION":
        event = lookup.first_by_transaction_id(issue.reference_id)
    elif issue.reference_type == "BATCH":
        event = lookup.first_by_batch_id(issue.reference_id)
    else:
        return None
    return event.rail_type if event else None


class _PriorityCache:
    """Real per-item severity_score/priority_level from app/priority.py,
    computed once per tenant (peer-relative scoring only means something
    within one tenant's own population) and reused across every signal
    from that tenant -- not a separate heuristic, so a case's priority can
    never disagree with the same issue/break's own entry on
    GET /operations/issues or /reconciliation/breaks.

    Also caches a CanonicalEventLookup per tenant (same lazy-per-tenant
    shape) -- compute_cases() may run across all tenants at once
    (tenant_bank_id=None), so this can't just build one lookup up front;
    it's built the first time a given tenant is actually seen, then reused
    for every signal from that tenant instead of a query per row.
    """

    def __init__(self, db: Session):
        self._db = db
        self._issues: dict[str, dict[int, dict]] = {}
        self._breaks: dict[str, dict[int, dict]] = {}
        self._event_lookups: dict[str, CanonicalEventLookup] = {}

    def for_issue(self, tenant_bank_id: str, issue_id: int) -> tuple[float | None, str | None]:
        if tenant_bank_id not in self._issues:
            self._issues[tenant_bank_id] = priority_levels_for_issues(self._db, tenant_bank_id)
        entry = self._issues[tenant_bank_id].get(issue_id)
        return (entry["severity_score"], entry["priority_level"]) if entry else (None, None)

    def for_break(self, tenant_bank_id: str, break_id: int) -> tuple[float | None, str | None]:
        if tenant_bank_id not in self._breaks:
            self._breaks[tenant_bank_id] = priority_levels_for_breaks(self._db, tenant_bank_id)
        entry = self._breaks[tenant_bank_id].get(break_id)
        return (entry["severity_score"], entry["priority_level"]) if entry else (None, None)

    def event_lookup(self, tenant_bank_id: str) -> CanonicalEventLookup:
        if tenant_bank_id not in self._event_lookups:
            self._event_lookups[tenant_bank_id] = CanonicalEventLookup(self._db, tenant_bank_id)
        return self._event_lookups[tenant_bank_id]


def _collect_operational_signals(db: Session, tenant_bank_id: str | None, priorities: _PriorityCache) -> list[_Signal]:
    query = db.query(OperationalIssue)
    if tenant_bank_id:
        query = query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)

    signals = []
    for issue in query.all():
        label = _OPERATIONAL_ISSUE_LABELS.get(issue.issue_type, issue.issue_type)
        severity_score, priority_level = priorities.for_issue(issue.tenant_bank_id, issue.id)
        signals.append(_Signal(
            tenant_bank_id=issue.tenant_bank_id,
            category=issue.issue_type,
            payment_rail=_rail_for_operational_issue(issue, priorities.event_lookup(issue.tenant_bank_id)),
            detected_at=_as_utc(issue.detected_at),
            source_type="OPERATIONAL_ISSUE",
            source_id=issue.id,
            transaction_id=issue.reference_id if issue.reference_type == "TRANSACTION" else None,
            anomaly_category="Operational",
            anomaly_type=label,
            description=f"{label} detected on {issue.reference_type.lower()} {issue.reference_id}",
            exposure=None,  # OperationalIssue carries no dollar amount directly -- not invented
            severity_score=severity_score,
            priority_level=priority_level,
            party_id=issue.reference_id if issue.reference_type == "PARTY" else None,
        ))
    return signals


def _collect_reconciliation_signals(db: Session, tenant_bank_id: str | None, priorities: _PriorityCache) -> list[_Signal]:
    query = db.query(ReconciliationBreak)
    if tenant_bank_id:
        query = query.filter(ReconciliationBreak.tenant_bank_id == tenant_bank_id)

    signals = []
    for brk in query.all():
        label = _RECONCILIATION_LABELS.get(brk.detection_type, brk.detection_type)
        amount = brk.variance_amount if brk.variance_amount is not None else brk.amount
        severity_score, priority_level = priorities.for_break(brk.tenant_bank_id, brk.id)
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
            severity_score=severity_score,
            priority_level=priority_level,
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
            severity_score=_FRAUD_BAND_SEVERITY_SCORE.get(snap.anomaly_band),
            priority_level=snap.anomaly_band,
            party_id=snap.party_id,
        ))
    return signals


def _priority_reason(worst: _Signal | None, cluster_size: int) -> str | None:
    """Plain-language explanation of the case's already-computed
    priority_level/severity_score -- which contributing alert drove it
    and the real band cutoff it crossed, quoted from priority.py rather
    than restated as new numbers. Nothing here is a new computation;
    `worst` (the highest-severity_score signal in the cluster) is the
    same one _create_case already uses for the case's own
    severity_score/priority_level.
    """
    if worst is None or worst.severity_score is None or worst.priority_level is None:
        return None

    band = worst.priority_level
    cutoff = {CRITICAL: CRITICAL_MIN, HIGH: HIGH_MIN, MEDIUM: MEDIUM_MIN, LOW: 0.0}.get(band, 0.0)
    driver = (
        f"its own alert ({worst.anomaly_type.lower()})" if cluster_size == 1
        else f"its most severe contributing alert ({worst.anomaly_type.lower()}, of {cluster_size} in this case)"
    )
    return (
        f"Rated {band} priority because of {driver}, which scored {worst.severity_score:.0f}/100 on real "
        f"peer-relative severity -- at or above the {band} threshold of {cutoff:.0f}."
    )


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

    # A case is at least as urgent as its single most severe contributing
    # alert -- not averaged, since one Critical alert buried among several
    # Low ones still deserves Critical attention.
    scored = [s for s in cluster if s.severity_score is not None]
    worst = max(scored, key=lambda s: s.severity_score) if scored else None

    case = InvestigationCase(
        case_code=f"CNO-{uuid4().hex[:6].upper()}",
        tenant_bank_id=tenant,
        category=category,
        payment_rail=rail,
        title=_case_title(category, rail),
        current_exposure=sum(exposure_values) if exposure_values else None,
        transactions_affected=transactions_affected,
        contributing_alerts_count=len(cluster),
        severity_score=worst.severity_score if worst else None,
        priority_level=worst.priority_level if worst else None,
        priority_reason=_priority_reason(worst, len(cluster)),
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

    Also purges any cached investigation_case AgentNarrative rows for
    this scope. Necessary, not just tidiness: case.id is autoincrement
    and case_code is freshly random (uuid4()) on every rebuild, so a
    narrative cached under the OLD id/case_code would otherwise get
    silently served against whatever unrelated case inherits that same
    numeric id next -- confirmed live (a stale "CNO-36F8D5" narrative
    rendered on a since-renumbered case that was actually CNO-7EA196).
    """
    priorities = _PriorityCache(db)
    all_signals = (
        _collect_operational_signals(db, tenant_bank_id, priorities)
        + _collect_reconciliation_signals(db, tenant_bank_id, priorities)
        + _collect_fraud_signals(db, tenant_bank_id)
    )

    alert_delete = db.query(InvestigationCaseAlert)
    case_delete = db.query(InvestigationCase)
    narrative_delete = db.query(AgentNarrative).filter(AgentNarrative.signal_type == "investigation_case")
    if tenant_bank_id:
        alert_delete = alert_delete.filter(InvestigationCaseAlert.tenant_bank_id == tenant_bank_id)
        case_delete = case_delete.filter(InvestigationCase.tenant_bank_id == tenant_bank_id)
        narrative_delete = narrative_delete.filter(AgentNarrative.tenant_bank_id == tenant_bank_id)
    alert_delete.delete(synchronize_session=False)
    case_delete.delete(synchronize_session=False)
    narrative_delete.delete(synchronize_session=False)

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


def get_anomaly_type_counts(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Ranked count of InvestigationCaseAlert.anomaly_type -- real named
    types ("Failure-rate spike," "Batch never settles," etc.) already
    computed by compute_cases(), just never surfaced as their own list
    before this. Read-only reshape, same "reshape, don't recompute" rule
    dashboard.py follows -- run POST /investigation/cases/compute first
    if this looks stale.
    """
    query = db.query(InvestigationCaseAlert.anomaly_type, func.count(InvestigationCaseAlert.id))
    if tenant_bank_id:
        query = query.filter(InvestigationCaseAlert.tenant_bank_id == tenant_bank_id)
    rows = query.group_by(InvestigationCaseAlert.anomaly_type).all()

    types = sorted(
        ({"anomaly_type": anomaly_type, "count": count} for anomaly_type, count in rows),
        key=lambda t: t["count"],
        reverse=True,
    )
    return {"types": types}
