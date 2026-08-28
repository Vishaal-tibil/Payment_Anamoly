"""Fraud category: Funnel Account -- multiple distinct senders suddenly
paying the same beneficiary, a classic mule-account collection pattern.

v1 is a simple threshold rule (Section 7: "before ML"), not a trained
model -- flagging is deterministic off distinct_senders/new_sender_ratio.
An ML-scored version would mean training a third segment-model on
BeneficiarySnapshot rows, genuinely new scope, not built here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import BeneficiarySnapshot

# A beneficiary's "recent" window, relative to its own last-seen
# transaction -- senders whose first-ever payment to this beneficiary
# falls inside it count as "new". Chosen to match Track A's own weekly
# windowing cadence.
_RECENT_WINDOW = timedelta(days=7)

# Threshold rule (Section 7, v1/pre-ML): flag a beneficiary only when both
# conditions hold -- enough distinct senders to look like fan-in at all,
# AND most of them are recent arrivals, not a beneficiary that's simply
# always had many payers (e.g. a legitimate biller).
_MIN_DISTINCT_SENDERS_FOR_FUNNEL_FLAG = 3
_MIN_NEW_SENDER_RATIO_FOR_FUNNEL_FLAG = 0.6

# Near-miss band: beneficiaries below the flag threshold but not
# trivially quiet either -- surfaced (not flagged) so the thresholds
# above can eventually be recalibrated against where real cases actually
# fall, instead of staying permanently arbitrary.
_NEAR_MISS_MIN_DISTINCT_SENDERS = 2
_NEAR_MISS_MIN_NEW_SENDER_RATIO = 0.4


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


def _beneficiary_key(event: CanonicalEvent) -> str | None:
    return event.payee_name or event.payee_account_ref


def _sender_key(event: CanonicalEvent) -> str | None:
    # Prefer the resolved party id when available (stable across name
    # variants); fall back to the raw payer_name for unresolved rows.
    return event.merchant_id or event.individual_id or event.payer_name


def compute_beneficiary_snapshots(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds anomaly_beneficiary_snapshots from canonical_events, one
    to-date row per beneficiary. Fully derived -- each run deletes and
    rebuilds the rows it covers, same idempotent shape as
    compute_snapshots() (Track A).
    """
    query = db.query(CanonicalEvent).filter(CanonicalEvent.transaction_occurred_at.isnot(None))
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    groups: dict[tuple[str, str], list[tuple[CanonicalEvent, datetime]]] = defaultdict(list)
    skipped_no_beneficiary = 0
    for event in query.all():
        key = _beneficiary_key(event)
        if not key:
            skipped_no_beneficiary += 1
            continue
        ts = _parse_ts(event.transaction_occurred_at)
        if ts is None:
            continue
        groups[(event.tenant_bank_id, key)].append((event, ts))

    if groups:
        tenant_ids = {tenant for (tenant, _key) in groups.keys()}
        delete_query = db.query(BeneficiarySnapshot).filter(BeneficiarySnapshot.tenant_bank_id.in_(tenant_ids))
        if tenant_bank_id:
            delete_query = delete_query.filter(BeneficiarySnapshot.tenant_bank_id == tenant_bank_id)
        delete_query.delete(synchronize_session=False)

    processed = 0
    flagged_count = 0
    near_misses: list[dict[str, Any]] = []
    for (tenant, beneficiary_key), events_with_ts in groups.items():
        events_with_ts.sort(key=lambda pair: pair[1])
        last_seen = events_with_ts[-1][1]
        recent_cutoff = last_seen - _RECENT_WINDOW

        first_payment_by_sender: dict[str, datetime] = {}
        for event, ts in events_with_ts:
            sender = _sender_key(event)
            if sender and sender not in first_payment_by_sender:
                first_payment_by_sender[sender] = ts

        distinct_senders = len(first_payment_by_sender)
        new_sender_count = sum(1 for ts in first_payment_by_sender.values() if ts >= recent_cutoff)
        new_sender_ratio = (new_sender_count / distinct_senders) if distinct_senders else None

        amounts = [e.amount for e, _ts in events_with_ts if e.amount is not None]
        amount_total = sum(amounts) if amounts else None

        funnel_flag = (
            distinct_senders >= _MIN_DISTINCT_SENDERS_FOR_FUNNEL_FLAG
            and (new_sender_ratio or 0) >= _MIN_NEW_SENDER_RATIO_FOR_FUNNEL_FLAG
        )
        reason = (
            f"{distinct_senders} distinct senders, {new_sender_count} new in the last "
            f"{_RECENT_WINDOW.days} days -- possible mule/funnel account"
            if funnel_flag else None
        )

        db.add(BeneficiarySnapshot(
            beneficiary_key=beneficiary_key,
            tenant_bank_id=tenant,
            window_end=last_seen,
            transaction_count=len(events_with_ts),
            distinct_senders=distinct_senders,
            new_sender_count=new_sender_count,
            new_sender_ratio=new_sender_ratio,
            amount_total=amount_total,
            funnel_flag=funnel_flag,
            funnel_reason=reason,
        ))
        processed += 1
        if funnel_flag:
            flagged_count += 1
        elif (
            distinct_senders >= _NEAR_MISS_MIN_DISTINCT_SENDERS
            and (new_sender_ratio or 0) >= _NEAR_MISS_MIN_NEW_SENDER_RATIO
        ):
            near_misses.append({
                "beneficiary_key": beneficiary_key,
                "tenant_bank_id": tenant,
                "distinct_senders": distinct_senders,
                "new_sender_ratio": new_sender_ratio,
            })

    db.commit()

    return {
        "beneficiaries_processed": processed,
        "funnel_flagged": flagged_count,
        "skipped_no_beneficiary_identifier": skipped_no_beneficiary,
        "near_miss_count": len(near_misses),
        "near_misses": near_misses,
    }
