"""Track A: behavioral feature snapshots for the unsupervised anomaly
detection engine (Isolation Forest + HDBSCAN + time-series), per
unsupervised-anomaly-detection-knowledge.md.

HARD RULE -- read this before adding a field:
Every value computed here comes from raw canonical_events facts:
amount, transaction_occurred_at, payee_name, rail_type, is_retry,
network_response_details, format_validation_status.

Never read: new_payee_risk_flag, funnel_account_flag,
velocity_threshold_breached, structuring_flag, network_timeout_flag, or
anything inside fraud_risk_details (velocity_score,
distinct_originating_accounts_24h/7d, payee_relationship_age_days,
prior_transaction_count_with_payee, aggregate_amount_24h_same_originator,
amount_to_threshold_ratio, ...). Those are the source's own pre-computed
risk verdicts and aggregates -- feeding them in would mean training the
model partly on the answer it's supposed to find. Where this module
computes something that sounds similar (e.g. new_counterparty_ratio vs.
the source's new_payee_risk_flag), it is deliberately recomputed from
scratch off the raw transaction sequence, not copied from the source's
version.

is_retry / retry ratio and format_validation_status / format-reject ratio
ARE allowed: these are raw operational facts the source system recorded
(was this literally marked a retry, did the message pass structural
validation), not a risk judgment applied on top of the data -- the
knowledge doc's own Section 5 lists "retry ratio" as a legitimate feature.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import CanonicalEvent
from .models import EntitySnapshot

# Below this many total transactions, weekly windowing would produce
# mostly-empty rows -- individuals (median 2, max 6 in our real data) sit
# well under this; merchants (19-45) sit well over it. Segment on volume,
# not a hardcoded rail/party_type list, so this adapts if a future
# tenant's individuals happen to be far more active.
_MIN_TXNS_FOR_WEEKLY_WINDOWING = 15

# Chronological split (Section 6): the most recent slice of the overall
# date range is held out as test, not a random sample.
_TEST_HOLDOUT_FRACTION = 0.2


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _week_start(dt: datetime) -> datetime:
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _extract_json_value(details: dict[str, Any] | None, suffix: str) -> Any:
    """network_response_details' keys are the full dotted source column
    name (e.g. "network_response_control.response_time_ms" for most
    rails, "clearing_network_response.response_time_ms" for Cheque) --
    match by suffix so this works across rails without a per-rail branch.
    """
    if not isinstance(details, dict):
        return None
    for key, value in details.items():
        if key.endswith(suffix):
            return value
    return None


def _amount_stats(amounts: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
    if not amounts:
        return None, None, None, None
    total = sum(amounts)
    avg = total / len(amounts)
    median = statistics.median(amounts)
    std = statistics.stdev(amounts) if len(amounts) > 1 else 0.0
    return total, avg, median, std


def _response_stats(events: list[CanonicalEvent]) -> tuple[float | None, float | None]:
    response_times: list[float] = []
    timeout_flags: list[bool] = []
    for e in events:
        rt = _extract_json_value(e.network_response_details, ".response_time_ms")
        sla = _extract_json_value(e.network_response_details, ".expected_response_sla_ms")
        if rt is not None:
            try:
                response_times.append(float(rt))
            except (TypeError, ValueError):
                pass
        if rt is not None and sla is not None:
            try:
                timeout_flags.append(float(rt) > float(sla))
            except (TypeError, ValueError):
                pass
    avg_rt = sum(response_times) / len(response_times) if response_times else None
    timeout_ratio = sum(1 for t in timeout_flags if t) / len(timeout_flags) if timeout_flags else None
    return avg_rt, timeout_ratio


def _new_counterparty_ratio(events_sorted: list[CanonicalEvent]) -> float | None:
    """Fraction of transactions whose counterparty was never seen earlier
    in THIS entity's own chronological history -- computed fresh from the
    sequence, not read from the source's new_payee_risk_flag.
    """
    named = [e for e in events_sorted if e.payee_name]
    if not named:
        return None
    seen: set[str] = set()
    new_count = 0
    for e in named:
        if e.payee_name not in seen:
            new_count += 1
            seen.add(e.payee_name)
    return new_count / len(named)


def _format_reject_ratio(events: list[CanonicalEvent]) -> float | None:
    applicable = [e.format_validation_status for e in events if e.format_validation_status]
    if not applicable:
        return None
    return sum(1 for v in applicable if v == "FAILED") / len(applicable)


def _retry_ratio(events: list[CanonicalEvent]) -> float | None:
    applicable = [e.is_retry for e in events if e.is_retry is not None]
    if not applicable:
        return None
    return sum(1 for v in applicable if v) / len(applicable)


def _build_snapshot(
    party_id: str,
    party_type: str,
    tenant_bank_id: str,
    segment: str,
    window_type: str,
    window_start: datetime | None,
    window_end: datetime,
    events: list[CanonicalEvent],
    account_first_seen: datetime,
    split: str,
) -> EntitySnapshot:
    amounts = [e.amount for e in events if e.amount is not None]
    total, avg, median, std = _amount_stats(amounts)
    avg_rt, timeout_ratio = _response_stats(events)
    counterparties = {e.payee_name for e in events if e.payee_name}

    return EntitySnapshot(
        party_id=party_id,
        party_type=party_type,
        tenant_bank_id=tenant_bank_id,
        segment=segment,
        window_type=window_type,
        window_start=window_start,
        window_end=window_end,
        transaction_count=len(events),
        amount_total=total,
        amount_avg=avg,
        amount_median=median,
        amount_std=std,
        unique_counterparties=len(counterparties) if counterparties else None,
        new_counterparty_ratio=_new_counterparty_ratio(events),
        retry_ratio=_retry_ratio(events),
        avg_response_time_ms=avg_rt,
        timeout_ratio=timeout_ratio,
        format_reject_ratio=_format_reject_ratio(events),
        rails_used=sorted({e.rail_type for e in events}),
        account_age_days=(window_end - account_first_seen).total_seconds() / 86400,
        split=split,
    )


def compute_snapshots(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds anomaly_entity_snapshots from canonical_events. Fully
    derived -- each run replaces every snapshot row for the parties it
    covers, same as compute_features() (Step 5) does for party_features.
    """
    query = db.query(CanonicalEvent).filter(
        or_(CanonicalEvent.merchant_id.isnot(None), CanonicalEvent.individual_id.isnot(None)),
        CanonicalEvent.transaction_occurred_at.isnot(None),
    )
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)

    groups: dict[tuple[str, str], list[CanonicalEvent]] = defaultdict(list)
    skipped_no_timestamp = 0
    for event in query.all():
        party_id = event.merchant_id or event.individual_id
        party_type = "MERCHANT" if event.merchant_id else "INDIVIDUAL"
        groups[(party_id, party_type)].append(event)

    all_no_ts_query = db.query(CanonicalEvent).filter(
        or_(CanonicalEvent.merchant_id.isnot(None), CanonicalEvent.individual_id.isnot(None)),
        CanonicalEvent.transaction_occurred_at.is_(None),
    )
    if tenant_bank_id:
        all_no_ts_query = all_no_ts_query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    skipped_no_timestamp = all_no_ts_query.count()

    # Global chronological cutoff: the most recent slice of the whole
    # observed date range is test, not a per-entity slice -- keeps "test"
    # meaning the same instant in time for everyone.
    all_timestamps = sorted(
        ts for (_, events) in groups.items() for ts in (_parse_ts(e.transaction_occurred_at) for e in events) if ts
    )
    cutoff = None
    if len(all_timestamps) >= 5:
        cutoff_idx = int(len(all_timestamps) * (1 - _TEST_HOLDOUT_FRACTION))
        cutoff = all_timestamps[min(cutoff_idx, len(all_timestamps) - 1)]

    errors: list[dict[str, Any]] = []
    merchant_snapshots = 0
    individual_snapshots = 0
    parties_processed = 0

    party_ids = [pid for (pid, _ptype) in groups.keys()]
    if party_ids:
        db.query(EntitySnapshot).filter(EntitySnapshot.party_id.in_(party_ids)).delete(synchronize_session=False)

    for (party_id, party_type), raw_events in groups.items():
        try:
            tenant = raw_events[0].tenant_bank_id
            segment = party_type
            dated = [(e, _parse_ts(e.transaction_occurred_at)) for e in raw_events]
            dated = [(e, ts) for e, ts in dated if ts is not None]
            if not dated:
                continue
            dated.sort(key=lambda pair: pair[1])
            events_sorted = [e for e, _ts in dated]
            first_seen = dated[0][1]
            parties_processed += 1

            if party_type == "MERCHANT" and len(events_sorted) >= _MIN_TXNS_FOR_WEEKLY_WINDOWING:
                weeks: dict[datetime, list[CanonicalEvent]] = defaultdict(list)
                for e, ts in dated:
                    weeks[_week_start(ts)].append(e)
                for week_start, week_events in sorted(weeks.items()):
                    week_end = week_start + timedelta(days=7)
                    split = "test" if cutoff and week_start >= cutoff else "train"
                    db.add(_build_snapshot(
                        party_id, party_type, tenant, segment, "WEEKLY",
                        week_start, week_end, week_events, first_seen, split,
                    ))
                    merchant_snapshots += 1
            else:
                # Individuals (and low-volume merchants): one to-date
                # snapshot. Nothing to hold out for a single observation,
                # so it's always "train".
                last_seen = dated[-1][1]
                db.add(_build_snapshot(
                    party_id, party_type, tenant, segment, "TO_DATE",
                    None, last_seen, events_sorted, first_seen, "train",
                ))
                if party_type == "MERCHANT":
                    merchant_snapshots += 1
                else:
                    individual_snapshots += 1
        except Exception as exc:
            errors.append({"type": "party_error", "party_id": party_id, "party_type": party_type, "error": str(exc)})

    db.commit()

    return {
        "parties_processed": parties_processed,
        "merchant_snapshots_created": merchant_snapshots,
        "individual_snapshots_created": individual_snapshots,
        "skipped_no_timestamp": skipped_no_timestamp,
        "errors": errors,
    }
