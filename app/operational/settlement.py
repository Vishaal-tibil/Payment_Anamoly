"""Batch/File Not Reaching Settlement detection.

Purely deterministic lifecycle check: canonical_events already carries
batch_id, file_reached_settlement, and expected_settlement_at per
transaction -- a batch is "overdue and unsettled" if its expected
settlement time has passed and any of its transactions haven't reached
settlement. No model, no per-entity baseline -- batches aren't parties,
so this deliberately doesn't go through EntitySnapshot.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def detect_unsettled_batches(
    db: Session, tenant_bank_id: str | None = None, as_of: datetime | None = None
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)

    query = db.query(CanonicalEvent).filter(CanonicalEvent.batch_id.isnot(None))
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    batches: dict[str, list[CanonicalEvent]] = defaultdict(list)
    for event in query.all():
        batches[event.batch_id].append(event)

    flagged: list[dict[str, Any]] = []
    for batch_id, events in batches.items():
        expected = next((ts for e in events if (ts := _parse_ts(e.expected_settlement_at)) is not None), None)
        if expected is None or expected > as_of:
            continue  # no known deadline, or not yet due

        unsettled = [e for e in events if e.file_reached_settlement is not True]
        if unsettled:
            flagged.append({
                "batch_id": batch_id,
                "tenant_bank_id": events[0].tenant_bank_id,
                "rail_type": events[0].rail_type,
                "expected_settlement_at": expected.isoformat(),
                "total_transactions": len(events),
                "unsettled_transactions": len(unsettled),
            })

    return {
        "batches_checked": len(batches),
        "batches_overdue_unsettled": len(flagged),
        "flagged": sorted(flagged, key=lambda f: f["unsettled_transactions"], reverse=True),
    }
