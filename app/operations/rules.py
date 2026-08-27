"""Deterministic rule: Batch/File Not Reaching Settlement.

canonical_events already carries batch_id, file_reached_settlement, and
expected_settlement_at per transaction -- a batch is "overdue and
unsettled" if its expected settlement time has passed and any of its
transactions haven't reached settlement. file_reached_settlement is a
literal fact, not a risk judgment, so reading it directly is exactly what
this engine is for (see the package docstring's reversed-exclusion-rule
note) -- no model, no baseline needed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import OperationalIssue


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
    """Rebuilds this tenant's BATCH_NOT_SETTLED rows in operational_issues.
    Fully derived -- each run deletes and rebuilds the rows it covers,
    same idempotent shape as the fraud engine's compute_snapshots().
    """
    as_of = as_of or datetime.now(timezone.utc)

    query = db.query(CanonicalEvent).filter(CanonicalEvent.batch_id.isnot(None))
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    batches: dict[str, list[CanonicalEvent]] = defaultdict(list)
    for event in query.all():
        batches[event.batch_id].append(event)

    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "BATCH_NOT_SETTLED")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    flagged = 0
    for batch_id, events in batches.items():
        expected = next((ts for e in events if (ts := _parse_ts(e.expected_settlement_at)) is not None), None)
        if expected is None or expected > as_of:
            continue  # no known deadline, or not yet due

        unsettled = [e for e in events if e.file_reached_settlement is not True]
        if unsettled:
            db.add(OperationalIssue(
                issue_type="BATCH_NOT_SETTLED",
                tenant_bank_id=events[0].tenant_bank_id,
                reference_type="BATCH",
                reference_id=batch_id,
                severity_score=None,  # binary condition, not scored
                details={
                    "rail_type": events[0].rail_type,
                    "expected_settlement_at": expected.isoformat(),
                    "total_transactions": len(events),
                    "unsettled_transactions": len(unsettled),
                    "days_overdue": (as_of - expected).days,
                },
            ))
            flagged += 1

    db.commit()

    return {"batches_checked": len(batches), "batches_flagged": flagged}
