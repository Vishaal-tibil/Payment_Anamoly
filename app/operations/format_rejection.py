"""File or Message Rejected Due to Incorrect Formatting -- listing half.

format_validation_status is a literal per-transaction fact (did this
specific message pass validation), not a statistical question -- no
model needed, just a filter. (The doc's "statistical monitoring" half --
is the reject RATE spiking -- is a separate future enhancement building
on EntitySnapshot.format_reject_ratio the same way drift.py does for
timeout_ratio; not built here, this module is the listing half only.)
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import OperationalIssue

_REJECTED_STATUSES = {"FAILED", "REJECTED"}


def detect_format_rejections(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds this tenant's FORMAT_REJECTION rows in operational_issues,
    one row per rejected transaction. Fully derived -- each run deletes
    and rebuilds the rows it covers.
    """
    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "FORMAT_REJECTION")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    query = db.query(CanonicalEvent).filter(CanonicalEvent.format_validation_status.in_(_REJECTED_STATUSES))
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    flagged = 0
    for event in query.all():
        db.add(OperationalIssue(
            issue_type="FORMAT_REJECTION",
            tenant_bank_id=event.tenant_bank_id,
            reference_type="TRANSACTION",
            reference_id=event.transaction_id,
            severity_score=None,
            details={"rail_type": event.rail_type, "errors": event.format_validation_errors},
        ))
        flagged += 1

    db.commit()

    return {"rejected_transactions_flagged": flagged}
