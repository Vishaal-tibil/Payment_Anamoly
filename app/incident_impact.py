"""Real "Enterprise Impact" cross-reference for one incident (Head of
Operations' Incident Details page). Two of this panel's original 6 rows
(payments affected, payment value) are the incident's own real
underlying transaction(s); two more (reconciliation exposure, fraud
exposure) are real sums from OTHER engines' already-computed output for
the SAME real transaction/party this incident touches -- not a forecast,
a cross-reference. The remaining two rows from the original mockup
(chargeback rate, dispute resolution time) have no real concept
anywhere in this schema and are never returned here -- honestly absent,
not fabricated.

Each row's "percent" (bar fill) is a real share of a real tenant-wide
total (e.g. payment_value / total transaction dollar volume), never an
invented baseline or a forecasted trajectory -- see the separate
"Exposure With vs. Without Intervention" chart (still blocked; that one
needs actual projection/extrapolation) for the one thing this page
still can't honestly show.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .anomaly.models import BeneficiarySnapshot, EntitySnapshot
from .canonical_event_lookup import CanonicalEventLookup
from .models import CanonicalEvent
from .operations.models import OperationalIssue
from .reconciliation.models import ReconciliationBreak

_MATERIAL_ANOMALY_BANDS = ("Critical", "High")
_PARTY_LEVEL_ISSUE_TYPES = {"NETWORK_TIMEOUT_SPIKE", "FORMAT_REJECTION_SPIKE"}


def _resolve_incident(
    db: Session, tenant_bank_id: str, signal_type: str, signal_id: int, lookup: CanonicalEventLookup,
) -> tuple[int, float, set[str]] | None:
    """(payments_affected, payment_value, involved real party_ids) for
    this one real incident. Returns None if the row doesn't exist for
    this tenant.
    """
    if signal_type == "operational_issue":
        issue = db.query(OperationalIssue).filter_by(id=signal_id, tenant_bank_id=tenant_bank_id).one_or_none()
        if issue is None:
            return None
        if issue.issue_type in _PARTY_LEVEL_ISSUE_TYPES:
            return 0, 0.0, {issue.reference_id}
        events = (
            lookup.all_by_transaction_id(issue.reference_id) if issue.reference_type == "TRANSACTION"
            else lookup.all_by_batch_id(issue.reference_id)
        )
        parties = {e.merchant_id or e.individual_id for e in events if e.merchant_id or e.individual_id}
        payment_value = sum(abs(e.amount) for e in events if e.amount is not None)
        return len(events), payment_value, parties

    if signal_type == "reconciliation_break":
        brk = db.query(ReconciliationBreak).filter_by(id=signal_id, tenant_bank_id=tenant_bank_id).one_or_none()
        if brk is None:
            return None
        event = lookup.by_rail_and_transaction_id(brk.rail_type, brk.transaction_id)
        parties = {event.merchant_id or event.individual_id} if event and (event.merchant_id or event.individual_id) else set()
        amount = brk.variance_amount if brk.variance_amount is not None else brk.amount
        return 1, (abs(amount) if amount is not None else 0.0), parties

    if signal_type == "fraud_anomaly":
        snap = db.query(EntitySnapshot).filter_by(id=signal_id, tenant_bank_id=tenant_bank_id).one_or_none()
        if snap is None:
            return None
        return snap.transaction_count or 0, snap.amount_total or 0.0, {snap.party_id}

    if signal_type == "funnel_account":
        snap = db.query(BeneficiarySnapshot).filter_by(id=signal_id, tenant_bank_id=tenant_bank_id).one_or_none()
        if snap is None:
            return None
        # beneficiary_key is the payee side -- doesn't map onto
        # CanonicalEvent.merchant_id/individual_id (this schema's sender-
        # side resolved identity), so no real cross-reference party to
        # join reconciliation/fraud exposure against for this signal type.
        return snap.transaction_count or 0, snap.amount_total or 0.0, set()

    return None


def get_incident_enterprise_impact(
    db: Session, tenant_bank_id: str, signal_type: str, signal_id: int,
) -> dict[str, Any] | None:
    lookup = CanonicalEventLookup(db, tenant_bank_id)
    resolved = _resolve_incident(db, tenant_bank_id, signal_type, signal_id, lookup)
    if resolved is None:
        return None
    payments_affected, payment_value, parties = resolved

    reconciliation_exposure = 0.0
    fraud_exposure = payment_value if signal_type == "fraud_anomaly" else 0.0
    if parties:
        for brk in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
            event = lookup.by_rail_and_transaction_id(brk.rail_type, brk.transaction_id)
            if event and (event.merchant_id or event.individual_id) in parties:
                amount = brk.variance_amount if brk.variance_amount is not None else brk.amount
                if amount is not None:
                    reconciliation_exposure += abs(amount)

        fraud_query = db.query(EntitySnapshot).filter(
            EntitySnapshot.tenant_bank_id == tenant_bank_id,
            EntitySnapshot.party_id.in_(parties),
            EntitySnapshot.anomaly_band.in_(_MATERIAL_ANOMALY_BANDS),
        )
        if signal_type == "fraud_anomaly":
            fraud_query = fraud_query.filter(EntitySnapshot.id != signal_id)  # don't double-count the incident itself
        fraud_exposure += sum(s.amount_total or 0.0 for s in fraud_query.all())

    # Real tenant-wide totals -- the denominator for each row's "percent"
    # bar. A real share of the whole, never an invented baseline.
    total_transactions = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id).count()
    total_amount = (
        db.query(func.sum(CanonicalEvent.amount))
        .filter(CanonicalEvent.tenant_bank_id == tenant_bank_id, CanonicalEvent.amount.isnot(None))
        .scalar() or 0.0
    )
    total_reconciliation_exposure = 0.0
    for b in db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all():
        amount = b.variance_amount if b.variance_amount is not None else b.amount
        if amount is not None:
            total_reconciliation_exposure += abs(amount)
    total_fraud_exposure = sum(
        s.amount_total or 0.0
        for s in db.query(EntitySnapshot)
        .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.anomaly_band.in_(_MATERIAL_ANOMALY_BANDS))
        .all()
    )

    def _pct(value: float, total: float) -> float | None:
        return round(min(value / total, 1.0) * 100, 1) if total else None

    return {
        "payments_affected": payments_affected,
        "payments_affected_percent": _pct(payments_affected, total_transactions),
        "payment_value": round(payment_value, 2),
        "payment_value_percent": _pct(payment_value, total_amount),
        "reconciliation_exposure": round(reconciliation_exposure, 2),
        "reconciliation_exposure_percent": _pct(reconciliation_exposure, total_reconciliation_exposure),
        "fraud_exposure": round(fraud_exposure, 2),
        "fraud_exposure_percent": _pct(fraud_exposure, total_fraud_exposure),
    }
