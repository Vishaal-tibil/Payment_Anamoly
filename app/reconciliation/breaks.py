"""Reconciliation Break detection.

Deterministic -- no statistics needed, unlike the two rate-based
Operational Issues checks. reconciliation_status and
reconciliation_variance_amount are the source's own completed
comparison of network-settled amount vs ledger-posted amount; the only
work here is surfacing every transaction where that comparison came
back bad, via two independent checks (see models.py's docstring for
why neither check alone is sufficient):

1. The source already called it a break (reconciliation_status="BREAK").
2. The source hasn't called it a break yet, but the variance amount is
   already nonzero -- catches a real mismatch before the source's own
   pipeline has caught up to it.

Confirmed against real Meridian data before writing this: of 30
BREAK-status transactions, only 18 carry a nonzero variance_amount (the
other 12 are flagged for reasons the amount alone doesn't capture) --
so CONFIRMED_BREAK must never be inferred from variance alone. And 3
NOT_YET_RECONCILED transactions already show nonzero variance --
genuine early-warning signal, not a hypothetical.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import ReconciliationBreak

_CONFIRMED_STATUS = "BREAK"


def detect_reconciliation_breaks(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds ReconciliationBreak rows. Fully derived -- each run
    deletes and rebuilds every row within the requested scope (whole
    tenant, or everything if no tenant given), same idempotent
    full-replace pattern as detect_duplicate_payments(): the delete
    scope is the requested scope itself, not just whichever
    transactions matched this run, so a run that finds nothing no
    longer flagged still clears stale rows from a previous run.
    """
    query = db.query(CanonicalEvent).filter(CanonicalEvent.reconciliation_status.isnot(None))
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    events = query.all()

    delete_query = db.query(ReconciliationBreak)
    if tenant_bank_id:
        delete_query = delete_query.filter(ReconciliationBreak.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    transactions_checked = len(events)
    confirmed_breaks = 0
    provisional_variances = 0

    for event in events:
        variance = event.reconciliation_variance_amount
        has_nonzero_variance = variance is not None and variance != 0.0

        if event.reconciliation_status == _CONFIRMED_STATUS:
            detection_type = "CONFIRMED_BREAK"
            confirmed_breaks += 1
        elif has_nonzero_variance:
            detection_type = "PROVISIONAL_VARIANCE"
            provisional_variances += 1
        else:
            continue

        db.add(ReconciliationBreak(
            tenant_bank_id=event.tenant_bank_id,
            transaction_id=event.transaction_id,
            rail_type=event.rail_type,
            detection_type=detection_type,
            source_reconciliation_status=event.reconciliation_status,
            variance_amount=variance,
            amount=event.amount,
            details=event.reconciliation_details,
        ))

    db.commit()

    return {
        "transactions_checked": transactions_checked,
        "confirmed_breaks": confirmed_breaks,
        "provisional_variances": provisional_variances,
    }
