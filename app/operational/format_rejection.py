"""File or Message Rejected Due to Incorrect Formatting detection.

format_reject_ratio already exists on EntitySnapshot (Track A) and is
already part of clustering's feature set -- the gap this module fills is
surfacing the SPECIFIC rejected transactions and their validation errors
(format_validation_errors), not a new aggregate statistic.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent


def detect_format_rejections(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    query = db.query(CanonicalEvent).filter(CanonicalEvent.format_validation_status == "FAILED")
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    flagged = [
        {
            "transaction_id": e.transaction_id,
            "tenant_bank_id": e.tenant_bank_id,
            "rail_type": e.rail_type,
            "party_id": e.merchant_id or e.individual_id,
            "errors": e.format_validation_errors,
        }
        for e in query.all()
    ]
    return {"rejected_transactions": len(flagged), "flagged": flagged}
