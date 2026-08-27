"""Funnel Account detection input: behavioral snapshots grouped by
BENEFICIARY (receiver) instead of by sender, per
unsupervised-anomaly-detection-knowledge.md Section 10 ("beneficiary/
account relationship features (unique senders, new-sender ratio)").

Same hard rule as features.py: every value here comes from raw
canonical_events facts (payee identity, sender identity, amount,
transaction_occurred_at) -- never from the source's own
funnel_account_flag or fraud_risk_details.distinct_originating_accounts_24h/7d.
Those are the source's own pre-computed verdict for exactly the pattern
this table exists to (re)discover independently.

Why this can't just be a view over EntitySnapshot: EntitySnapshot is
grouped by sender and answers "how many different people did this entity
pay" (unique_counterparties). Funnel detection needs the reverse
question -- "how many different senders paid THIS beneficiary, and how
many of them are new" -- which requires grouping the same raw
transactions by counterparty instead of by resolved party. No amount of
modeling on top of EntitySnapshot can produce that; the grouping itself
has to be different.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .features import _parse_ts, _week_start
from .models import BeneficiarySnapshot


def _beneficiary_key(event: CanonicalEvent) -> str | None:
    return event.payee_account_ref or event.payee_name


def _sender_key(event: CanonicalEvent) -> str | None:
    return event.merchant_id or event.individual_id or event.payer_name


def _sender_party_type(event: CanonicalEvent) -> str | None:
    if event.merchant_id:
        return "MERCHANT"
    if event.individual_id:
        return "INDIVIDUAL"
    return None


def compute_beneficiary_snapshots(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds anomaly_beneficiary_snapshots from canonical_events. Fully
    derived -- each run replaces every snapshot row for the beneficiaries
    it covers, same pattern as compute_snapshots() (Track A, sender-side).

    One row per (beneficiary, week) they received at least one payment in
    -- unlike EntitySnapshot there's no low-volume/TO_DATE split here,
    since a beneficiary's transaction count isn't bounded by one entity's
    own activity the way a single merchant's or individual's is.
    """
    query = db.query(CanonicalEvent).filter(
        CanonicalEvent.payee_name.isnot(None),
        CanonicalEvent.transaction_occurred_at.isnot(None),
    )
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    # Grouped by (tenant, beneficiary_key), not beneficiary_key alone.
    # Unlike merchant_id/individual_id (resolved party ids, already unique
    # per tenant), beneficiary_key is a raw source-provided account ref or
    # display name -- two different tenants can easily share one (e.g. the
    # same routing/account number format, or "Amazon" as a payee name).
    # Grouping by key alone would silently merge their transactions into a
    # single snapshot under whichever tenant happened to sort first.
    groups: dict[tuple[str, str], list[tuple[CanonicalEvent, datetime]]] = defaultdict(list)
    skipped_no_beneficiary_key = 0
    for event in query.all():
        key = _beneficiary_key(event)
        ts = _parse_ts(event.transaction_occurred_at)
        if key is None or ts is None:
            skipped_no_beneficiary_key += 1
            continue
        groups[(event.tenant_bank_id, key)].append((event, ts))

    if groups:
        db.query(BeneficiarySnapshot).filter(
            or_(*(
                and_(BeneficiarySnapshot.tenant_bank_id == tenant, BeneficiarySnapshot.beneficiary_key == key)
                for tenant, key in groups.keys()
            ))
        ).delete(synchronize_session=False)

    snapshots_created = 0
    errors: list[dict[str, Any]] = []

    for (tenant, beneficiary_key), dated_events in groups.items():
        try:
            dated_events.sort(key=lambda pair: pair[1])

            weeks: dict[datetime, list[tuple[CanonicalEvent, datetime]]] = defaultdict(list)
            for event, ts in dated_events:
                weeks[_week_start(ts)].append((event, ts))

            senders_seen_before: set[str] = set()
            for week_start in sorted(weeks.keys()):
                week_events = [e for e, _ts in weeks[week_start]]
                sender_keys = {_sender_key(e) for e in week_events if _sender_key(e) is not None}

                new_senders = sender_keys - senders_seen_before
                distinct_senders = len(sender_keys)
                distinct_new_senders = len(new_senders)
                new_sender_ratio = (distinct_new_senders / distinct_senders) if distinct_senders else None

                amounts = [e.amount for e in week_events if e.amount is not None]
                party_types = sorted({t for e in week_events if (t := _sender_party_type(e)) is not None})
                last_name = next((e.payee_name for e in reversed(week_events) if e.payee_name), None)

                db.add(BeneficiarySnapshot(
                    beneficiary_key=beneficiary_key,
                    beneficiary_name=last_name,
                    tenant_bank_id=tenant,
                    window_start=week_start,
                    window_end=week_start + timedelta(days=7),
                    transaction_count=len(week_events),
                    amount_total=sum(amounts) if amounts else None,
                    distinct_senders=distinct_senders,
                    distinct_new_senders=distinct_new_senders,
                    new_sender_ratio=new_sender_ratio,
                    sender_party_types=party_types,
                ))
                snapshots_created += 1
                senders_seen_before |= sender_keys
        except Exception as exc:
            errors.append({"type": "beneficiary_error", "beneficiary_key": beneficiary_key, "error": str(exc)})

    db.commit()

    return {
        "beneficiaries_processed": len(groups),
        "snapshots_created": snapshots_created,
        "skipped_no_beneficiary_key": skipped_no_beneficiary_key,
        "errors": errors,
    }
