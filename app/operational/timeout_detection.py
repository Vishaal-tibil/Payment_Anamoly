"""Network/Processor Timeout (No Response) detection.

Per Section 10 ("response-time time series"), but implemented here at the
transaction level: canonical_events.network_response_details already
carries response_time_ms and expected_response_sla_ms per transaction
(the same raw fields Track A's avg_response_time_ms/timeout_ratio are
computed from) -- flagging is a direct comparison, no model needed.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent


def _extract_json_value(details: dict[str, Any] | None, suffix: str) -> Any:
    """Same suffix-match as features.py's _extract_json_value -- keys are
    the full dotted source column name and vary per rail
    (network_response_control.* vs. clearing_network_response.* for Cheque).
    """
    if not isinstance(details, dict):
        return None
    for key, value in details.items():
        if key.endswith(suffix):
            return value
    return None


def detect_timeouts(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    query = db.query(CanonicalEvent).filter(CanonicalEvent.network_response_details.isnot(None))
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    checked = 0
    flagged: list[dict[str, Any]] = []
    for event in query.all():
        response_time = _extract_json_value(event.network_response_details, ".response_time_ms")
        sla = _extract_json_value(event.network_response_details, ".expected_response_sla_ms")
        if response_time is None or sla is None:
            continue
        try:
            response_time, sla = float(response_time), float(sla)
        except (TypeError, ValueError):
            continue

        checked += 1
        if response_time > sla:
            flagged.append({
                "transaction_id": event.transaction_id,
                "tenant_bank_id": event.tenant_bank_id,
                "rail_type": event.rail_type,
                "party_id": event.merchant_id or event.individual_id,
                "response_time_ms": response_time,
                "expected_sla_ms": sla,
                "overage_ms": response_time - sla,
            })

    return {
        "transactions_checked": checked,
        "timeouts_flagged": len(flagged),
        "flagged": sorted(flagged, key=lambda f: f["overage_ms"], reverse=True),
    }
