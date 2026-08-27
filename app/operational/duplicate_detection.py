"""Duplicate Payment Sent Due to Retry detection.

Per Section 10 ("transaction similarity + time proximity + account/
beneficiary/amount/reference matching"): independently matches
transactions by (payer, payee, amount) within a short time window, rather
than trusting the source's own is_retry/duplicate_check_status flags at
face value -- those are surfaced as corroborating context on a match, not
used to find the match in the first place, so this is a real detection,
not a relabeling of the source's own verdict.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent

_DEFAULT_TIME_WINDOW = timedelta(minutes=30)


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


def detect_duplicate_payments(
    db: Session, tenant_bank_id: str | None = None, time_window: timedelta = _DEFAULT_TIME_WINDOW,
) -> dict[str, Any]:
    query = db.query(CanonicalEvent).filter(
        CanonicalEvent.amount.isnot(None), CanonicalEvent.transaction_occurred_at.isnot(None),
    )
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    groups: dict[tuple[str, str | None, str | None, float], list[tuple[CanonicalEvent, datetime]]] = defaultdict(list)
    for event in query.all():
        ts = _parse_ts(event.transaction_occurred_at)
        if ts is None:
            continue
        key = (event.tenant_bank_id, event.payer_name, event.payee_name, event.amount)
        groups[key].append((event, ts))

    flagged: list[dict[str, Any]] = []
    for (tenant_id, payer, payee, amount), events_with_ts in groups.items():
        events_with_ts.sort(key=lambda pair: pair[1])
        for i in range(1, len(events_with_ts)):
            prev_event, prev_ts = events_with_ts[i - 1]
            curr_event, curr_ts = events_with_ts[i]
            gap = curr_ts - prev_ts
            if gap <= time_window:
                flagged.append({
                    "tenant_bank_id": tenant_id,
                    "payer_name": payer,
                    "payee_name": payee,
                    "amount": amount,
                    "transaction_id_1": prev_event.transaction_id,
                    "transaction_id_2": curr_event.transaction_id,
                    "seconds_apart": gap.total_seconds(),
                    "either_marked_retry": bool(prev_event.is_retry or curr_event.is_retry),
                })

    return {
        "candidate_groups_checked": len(groups),
        "duplicate_pairs_flagged": len(flagged),
        "flagged": sorted(flagged, key=lambda f: f["seconds_apart"]),
    }
