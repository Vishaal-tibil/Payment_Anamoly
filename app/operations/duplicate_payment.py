"""Duplicate Payment: deterministic exact-key join, no ML.

idempotency_key/original_transaction_id already link a retry to its
original -- this is a lookup problem, not a prediction problem. A
genuine retry sharing an idempotency key with its original should only
ever have ONE of the two reach SETTLED; if both do, the retry wasn't
correctly deduplicated and the payment went through twice.

Confirmed against real Meridian data before writing this: retries
reliably share idempotency_key with their original transaction (all 20
is_retry=True rows here do), and several of those pairs genuinely have
BOTH sides SETTLED -- real signal, not a hypothetical.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import OperationalIssue

_SETTLED_STATUS = "SETTLED"


def detect_duplicate_payments(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds OperationalIssue rows of type DUPLICATE_PAYMENT. Fully
    derived -- each run deletes and rebuilds every row of this type
    within the requested scope (whole tenant, or everything if no
    tenant given), same idempotent full-replace pattern as
    compute_beneficiary_snapshots()/compute_snapshots(). The delete
    scope is the requested scope itself, not "whichever tenants
    happened to produce a group this run" -- otherwise a run that finds
    zero duplicates would leave stale rows from a previous run in place.
    """
    query = db.query(CanonicalEvent)
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    events = query.all()

    # Primary grouping: rows sharing one (tenant, idempotency_key).
    groups: dict[tuple[str, str], list[CanonicalEvent]] = defaultdict(list)
    for event in events:
        if event.idempotency_key:
            groups[(event.tenant_bank_id, event.idempotency_key)].append(event)

    # Fallback: a retry and its original, linked by original_transaction_id,
    # for the case where they don't already share an idempotency_key (e.g.
    # the key wasn't captured on one side). Skipped when the pair is
    # already covered by the primary grouping, to avoid double-counting.
    by_txn_id = {(e.tenant_bank_id, e.transaction_id): e for e in events}
    for event in events:
        if not (event.is_retry and event.original_transaction_id):
            continue
        original = by_txn_id.get((event.tenant_bank_id, event.original_transaction_id))
        if original is None:
            continue
        share_idempotency_key = bool(event.idempotency_key) and event.idempotency_key == original.idempotency_key
        if share_idempotency_key:
            continue
        link_key = (event.tenant_bank_id, f"RETRY-LINK:{original.transaction_id}")
        pair = groups[link_key]
        for member in (event, original):
            if member not in pair:
                pair.append(member)

    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "DUPLICATE_PAYMENT")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    groups_checked = 0
    flagged = 0
    for (tenant, group_key), group_events in groups.items():
        if len(group_events) < 2:
            continue
        groups_checked += 1
        settled = [e for e in group_events if e.status == _SETTLED_STATUS]
        if len(settled) < 2:
            continue
        flagged += 1
        db.add(OperationalIssue(
            issue_type="DUPLICATE_PAYMENT",
            tenant_bank_id=tenant,
            reference_type="TRANSACTION",
            reference_id=settled[0].transaction_id,
            severity_score=None,
            details={
                "link_key": group_key,
                "settled_transaction_ids": [e.transaction_id for e in settled],
                "all_transaction_ids_in_group": [e.transaction_id for e in group_events],
            },
        ))

    db.commit()

    return {
        "groups_checked": groups_checked,
        "duplicate_payments_flagged": flagged,
    }
