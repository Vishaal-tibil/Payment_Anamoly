"""Payment Health scoring -- Step 6d.

One 0-100 score per tenant bank, built from four components, each itself
a plain rate against real counts already produced elsewhere in this
platform. No new detection happens here; this only rolls up what the
other three engines already found.

    health_score = 100 - (
        settlement_penalty      * 0.30 +
        anomaly_penalty         * 0.30 +
        operational_penalty     * 0.20 +
        reconciliation_penalty  * 0.20
    )

Weights are a documented v1 choice, not a derived constant -- same
"easy to revisit, not final" framing this codebase already uses for the
fraud engine's 0.40/0.25/0.35 blend and the anomaly-band cutoffs.
Settlement and anomaly outcomes are weighted highest (0.30 each) because
they're the most direct read on "is money actually moving correctly and
does anyone's behavior look wrong" -- operational plumbing and
reconciliation timing (0.20 each) matter but are one level more
operational than financial/behavioral.

Each penalty is a plain rate (bad_count / total_count * 100), deliberately
NOT amplified by an extra multiplier to make it look more dramatic --
this platform's whole ethos is not overstating what the data shows, and a
handful of real Critical anomalies or confirmed breaks should show up as
exactly that (a real, visible penalty), not be inflated into a crisis the
underlying numbers don't support. The component sub-scores are stored
alongside the final number specifically so nothing is hidden behind one
aggregate figure -- see PaymentHealthScore's docstring.

Band cutoffs (example, same "revisit later" status as anomaly_band's):
Healthy >= 85, Watch 70-84, At Risk 50-69, Critical < 50.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import EntitySnapshot
from ..models import CanonicalEvent
from ..operations.models import OperationalIssue
from ..query_filters import parse_date_bound
from ..reconciliation.models import ReconciliationBreak
from .models import PaymentHealthScore, PaymentHealthScoreHistory

_SETTLEMENT_WEIGHT = 0.30
_ANOMALY_WEIGHT = 0.30
_OPERATIONAL_WEIGHT = 0.20
_RECONCILIATION_WEIGHT = 0.20

# Critical counts twice as much as High toward the anomaly component --
# both are real signal, but a Critical-band entity is a materially
# different situation than a Low-Medium/High one.
_CRITICAL_ANOMALY_WEIGHT = 2
_HIGH_ANOMALY_WEIGHT = 1


def _band_for_score(score: float) -> str:
    if score >= 85:
        return "Healthy"
    if score >= 70:
        return "Watch"
    if score >= 50:
        return "At Risk"
    return "Critical"


def _compute_for_tenant(db: Session, tenant_bank_id: str) -> PaymentHealthScore:
    total_transactions = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id).count()
    settled_transactions = (
        db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, status="SETTLED").count()
    )
    settlement_rate = (settled_transactions / total_transactions) if total_transactions else 1.0
    settlement_penalty = max(0.0, (1.0 - settlement_rate) * 100)

    band_rows = db.query(EntitySnapshot.anomaly_band).filter_by(tenant_bank_id=tenant_bank_id).all()
    total_scored_entities = len(band_rows)
    critical_count = sum(1 for (band,) in band_rows if band == "Critical")
    high_count = sum(1 for (band,) in band_rows if band == "High")
    anomaly_penalty = (
        (critical_count * _CRITICAL_ANOMALY_WEIGHT + high_count * _HIGH_ANOMALY_WEIGHT) / total_scored_entities * 100
        if total_scored_entities
        else 0.0
    )

    operational_issue_count = db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).count()
    operational_penalty = (
        (operational_issue_count / total_transactions * 100) if total_transactions else 0.0
    )

    reconciliation_break_count = db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).count()
    # Denominator is transactions that actually went through reconciliation
    # (reconciliation_status set) -- not every ingested transaction does --
    # same scope detect_reconciliation_breaks() itself checks against.
    reconciliation_checked = (
        db.query(CanonicalEvent)
        .filter(CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.reconciliation_status.isnot(None))
        .count()
    )
    reconciliation_penalty = (
        (reconciliation_break_count / reconciliation_checked * 100) if reconciliation_checked else 0.0
    )

    settlement_component = max(0.0, 100 - settlement_penalty)
    anomaly_component = max(0.0, 100 - anomaly_penalty)
    operational_component = max(0.0, 100 - operational_penalty)
    reconciliation_component = max(0.0, 100 - reconciliation_penalty)

    health_score = (
        settlement_component * _SETTLEMENT_WEIGHT
        + anomaly_component * _ANOMALY_WEIGHT
        + operational_component * _OPERATIONAL_WEIGHT
        + reconciliation_component * _RECONCILIATION_WEIGHT
    )
    health_score = round(max(0.0, min(100.0, health_score)), 1)

    existing = db.get(PaymentHealthScore, tenant_bank_id)
    row = existing or PaymentHealthScore(tenant_bank_id=tenant_bank_id)
    row.health_score = health_score
    row.health_band = _band_for_score(health_score)
    row.settlement_component = round(settlement_component, 1)
    row.anomaly_component = round(anomaly_component, 1)
    row.operational_component = round(operational_component, 1)
    row.reconciliation_component = round(reconciliation_component, 1)
    row.total_transactions = total_transactions
    row.settled_transactions = settled_transactions
    row.total_scored_entities = total_scored_entities
    row.critical_anomaly_count = critical_count
    row.high_anomaly_count = high_count
    row.operational_issue_count = operational_issue_count
    row.reconciliation_break_count = reconciliation_break_count
    if not existing:
        db.add(row)

    # Always a new row, never upserted -- this is the real history a trend
    # chart reads from. See PaymentHealthScoreHistory's own docstring.
    db.add(PaymentHealthScoreHistory(
        tenant_bank_id=tenant_bank_id, health_score=health_score, health_band=row.health_band,
    ))
    return row


def compute_health_scores(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Recomputes and upserts the PaymentHealthScore row for one tenant,
    or every tenant with any CanonicalEvent data if none is given.
    """
    if tenant_bank_id:
        tenants = [tenant_bank_id]
    else:
        tenants = [t for (t,) in db.query(CanonicalEvent.tenant_bank_id).distinct().all()]

    scored = [_compute_for_tenant(db, t) for t in tenants]
    db.commit()

    return {
        "tenants_scored": len(scored),
        "scores": [
            {
                "tenant_bank_id": row.tenant_bank_id,
                "health_score": row.health_score,
                "health_band": row.health_band,
            }
            for row in scored
        ],
    }


def get_health_history(
    db: Session,
    tenant_bank_id: str,
    limit: int = 90,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Real compute-run history for this tenant, oldest first -- however
    many points actually exist (often just 1, honestly, until
    POST /health/compute has run repeatedly over real time). Never
    backfilled with fabricated past points -- see PaymentHealthScoreHistory.
    """
    start_dt, end_dt = parse_date_bound(start_date), parse_date_bound(end_date, end_of_day=True)
    query = db.query(PaymentHealthScoreHistory).filter_by(tenant_bank_id=tenant_bank_id)
    if start_dt:
        query = query.filter(PaymentHealthScoreHistory.computed_at >= start_dt)
    if end_dt:
        query = query.filter(PaymentHealthScoreHistory.computed_at <= end_dt)
    rows = query.order_by(PaymentHealthScoreHistory.computed_at.desc()).limit(limit).all()
    rows.reverse()
    return [
        {"computed_at": row.computed_at, "health_score": row.health_score, "health_band": row.health_band}
        for row in rows
    ]
