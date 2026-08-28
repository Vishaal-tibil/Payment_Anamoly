"""Duplicate Payment Sent Due to Retry detection.

Deterministic match + exact-key join, no ML: idempotency_key and
original_transaction_id already link a retry to its original attempt --
this is a lookup problem, not a prediction problem. Grouping by these
keys (not amount/time-proximity heuristics) is what the doc's own
mapping calls for. Reading duplicate_check_status/is_retry/
idempotency_key directly is the entire point of this engine -- see the
package docstring's reversed-exclusion-rule note.

A genuine retry should only ever have one winner reach a settled status;
more than one settled row sharing the same key means a duplicate payment
was actually sent, not just retried and superseded.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import OperationalIssue

_SETTLED_STATUSES = {"SETTLED", "COMPLETED"}


def _group_and_flag(
    db: Session, tenant_bank_id: str | None, key_getter: Callable[[CanonicalEvent], Any], match_key_type: str,
) -> tuple[int, int]:
    query = db.query(CanonicalEvent)
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    groups: dict[tuple[str, Any], list[CanonicalEvent]] = defaultdict(list)
    for event in query.all():
        key = key_getter(event)
        if key:
            groups[(event.tenant_bank_id, key)].append(event)

    groups_checked = 0
    flagged = 0
    for (tenant, key), events in groups.items():
        if len(events) < 2:
            continue
        groups_checked += 1
        settled = [e for e in events if e.status in _SETTLED_STATUSES]
        if len(settled) > 1:
            db.add(OperationalIssue(
                issue_type="DUPLICATE_PAYMENT",
                tenant_bank_id=tenant,
                reference_type="TRANSACTION",
                reference_id=settled[0].transaction_id,
                severity_score=None,
                details={
                    "match_key_type": match_key_type,
                    "match_key": key,
                    "settled_transaction_ids": [e.transaction_id for e in settled],
                    "settled_count": len(settled),
                },
            ))
            flagged += 1

    return groups_checked, flagged


def detect_duplicate_payments(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds this tenant's DUPLICATE_PAYMENT rows in operational_issues.
    Two independent grouping passes -- by idempotency_key (all rails) and
    by original_transaction_id (retries only) -- since either key alone
    can link a duplicate depending on what the source rail populated.
    """
    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "DUPLICATE_PAYMENT")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    def _original_transaction_key(event: CanonicalEvent) -> str:
        # A retry's original_transaction_id points at the ORIGINAL row's
        # transaction_id -- that original row itself has is_retry=False
        # and no original_transaction_id of its own, so both sides must
        # be normalized to the SAME key (the original's transaction_id)
        # for them to land in the same group. Every non-retry event keys
        # to itself, which just means unrelated transactions form
        # harmless singleton groups that _group_and_flag skips (< 2 members).
        if event.is_retry and event.original_transaction_id:
            return event.original_transaction_id
        return event.transaction_id

    idempotency_checked, idempotency_flagged = _group_and_flag(
        db, tenant_bank_id, lambda e: e.idempotency_key, "idempotency_key",
    )
    retry_checked, retry_flagged = _group_and_flag(
        db, tenant_bank_id, _original_transaction_key, "original_transaction_id",
    )

    db.commit()

    return {
        "groups_checked": idempotency_checked + retry_checked,
        "duplicates_flagged": idempotency_flagged + retry_flagged,
    }
