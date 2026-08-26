from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import CanonicalEvent, PartyFeatures


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rate(events: list[CanonicalEvent], getter) -> float | None:
    """Fraction of True among transactions where this flag was actually
    set (not None). None (not 0.0) when the flag never applied to any of
    this party's transactions -- "never evaluated" is not "never flagged".
    """
    applicable = [getter(e) for e in events if getter(e) is not None]
    if not applicable:
        return None
    return sum(1 for v in applicable if v) / len(applicable)


def _bad_status_rate(events: list[CanonicalEvent], getter, bad_values: set[str]) -> float | None:
    applicable = [getter(e) for e in events if getter(e) is not None]
    if not applicable:
        return None
    return sum(1 for v in applicable if v in bad_values) / len(applicable)


def _compute_one(party_id: str, party_type: str, events: list[CanonicalEvent]) -> PartyFeatures:
    tenant_bank_id = events[0].tenant_bank_id
    amounts = [e.amount for e in events if e.amount is not None]

    # Convention: the party IS the entity, and entity always maps to
    # payer_name (see resolution.py) -- so payee_name is always "the
    # other side" this party transacted with, regardless of rail.
    counterparties = {e.payee_name for e in events if e.payee_name}

    return PartyFeatures(
        party_id=party_id,
        party_type=party_type,
        tenant_bank_id=tenant_bank_id,
        transaction_count=len(events),
        total_amount=sum(amounts) if amounts else None,
        avg_amount=(sum(amounts) / len(amounts)) if amounts else None,
        rails_active=sorted({e.rail_type for e in events}),
        distinct_counterparties=len(counterparties) if counterparties else None,
        new_payee_risk_rate=_rate(events, lambda e: e.new_payee_risk_flag),
        funnel_account_rate=_rate(events, lambda e: e.funnel_account_flag),
        velocity_breach_rate=_rate(events, lambda e: e.velocity_threshold_breached),
        structuring_rate=_rate(events, lambda e: e.structuring_flag),
        network_timeout_rate=_rate(events, lambda e: e.network_timeout_flag),
        is_retry_rate=_rate(events, lambda e: e.is_retry),
        format_reject_rate=_bad_status_rate(events, lambda e: e.format_validation_status, {"FAILED"}),
        reconciliation_break_rate=_bad_status_rate(events, lambda e: e.reconciliation_status, {"BREAK"}),
        first_seen_at=min(e.first_seen_at for e in events),
        last_seen_at=max(e.last_updated_at for e in events),
        computed_at=_utcnow(),
    )


def compute_features(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Aggregates canonical_events per resolved party (merchant or
    individual) into party_features.

    Fully derived, so each run replaces (not merges) the feature rows it
    covers -- unlike canonical_events' merge-only rule, there is no
    "earlier arrival" to protect here, only a fresh recomputation from
    whatever canonical_events currently holds. Only rows with a resolved
    merchant_id/individual_id are considered; unresolved rows (Step 4
    hasn't run, or the row is genuinely unresolvable) are silently
    excluded, same as they'd be invisible to any downstream engine.
    """
    query = db.query(CanonicalEvent).filter(
        or_(CanonicalEvent.merchant_id.isnot(None), CanonicalEvent.individual_id.isnot(None))
    )
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    groups: dict[tuple[str, str], list[CanonicalEvent]] = defaultdict(list)
    for event in query.all():
        if event.merchant_id:
            groups[(event.merchant_id, "MERCHANT")].append(event)
        else:
            groups[(event.individual_id, "INDIVIDUAL")].append(event)

    errors: list[dict[str, Any]] = []
    merchants_computed = 0
    individuals_computed = 0

    for (party_id, party_type), events in groups.items():
        try:
            db.query(PartyFeatures).filter_by(party_id=party_id).delete()
            db.add(_compute_one(party_id, party_type, events))
            if party_type == "MERCHANT":
                merchants_computed += 1
            else:
                individuals_computed += 1
        except Exception as exc:
            errors.append({"type": "party_error", "party_id": party_id, "party_type": party_type, "error": str(exc)})

    db.commit()

    return {
        "parties_computed": merchants_computed + individuals_computed,
        "merchants_computed": merchants_computed,
        "individuals_computed": individuals_computed,
        "errors": errors,
    }
