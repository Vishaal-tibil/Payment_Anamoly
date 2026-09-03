"""Cases Approaching SLA -- real per-case age against a documented v1
policy window per priority_level. Replaces what was previously a fully
fabricated frontend fixture (data/analyst/insightsOverview.js's
slaCases -- literal placeholder case ids like "INV-4421" with invented
minute-level countdowns).

Real case age is resolved from each case's own alerts' real underlying
event time -- NOT InvestigationCase.opened_at directly. Confirmed
opened_at is a batch-compute-run timestamp for non-fraud cases (recomputed
cases showed opened_at values from whenever compute_cases() was last run,
not from any real transaction date) -- the exact same contamination
app/investigation/trend.py's docstring found in detected_at. Real anchor
instead:
- Fraud-sourced alerts (source_type="ANOMALY_SNAPSHOT", party-level, no
  transaction_id): their own detected_at IS real -- set from
  EntitySnapshot.window_end at case-creation time.
- Transaction-referenced alerts (a real transaction_id stored on the
  alert): the underlying CanonicalEvent's real transaction_occurred_at.
- Party-level rate-spike issue alerts (NETWORK_TIMEOUT_SPIKE /
  FORMAT_REJECTION_SPIKE -- no transaction_id, no real per-alert anchor
  available on this table): falls back to the case's own opened_at, a
  documented limitation rather than a silent wrong answer.

The SLA window itself is a real, documented v1 policy applied to those
real timestamps -- same "documented cutoff, revisit later" status as
priority.py's CRITICAL_MIN/HIGH_MIN/MEDIUM_MIN. Not fabricated data: a
business rule, not an invented number.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..canonical_event_lookup import CanonicalEventLookup
from .models import InvestigationCase, InvestigationCaseAlert

_SLA_WINDOW_DAYS = {"Critical": 2.0, "High": 5.0, "Medium": 10.0, "Low": 20.0}
_DEFAULT_WINDOW_DAYS = 10.0

# "Approaching" = within this fraction of the window still remaining
# (and not yet overdue) -- the last 30% of the window.
_APPROACHING_FRACTION = 0.3


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_real_case_anchor(
    case: InvestigationCase, alerts: list[InvestigationCaseAlert], lookup: CanonicalEventLookup,
) -> datetime:
    """Public since GET /investigation/cases' start_date/end_date filter
    reuses this same real per-case anchor (see cases.py's list endpoint)
    -- not just this module's own SLA aging.
    """
    anchors: list[datetime] = []
    for alert in alerts:
        if alert.source_type == "ANOMALY_SNAPSHOT":
            anchors.append(_as_utc(alert.detected_at))
        elif alert.transaction_id:
            # Rail-scoped (not just transaction_id) -- a transaction_id
            # alone isn't guaranteed unique across rails for one tenant
            # (only (tenant, rail, transaction_id) is), same lookup key
            # app/dashboard.py's _reconciliation_break_date already uses.
            event = lookup.by_rail_and_transaction_id(alert.payment_rail, alert.transaction_id)
            ts = _parse_ts(event.transaction_occurred_at) if event else None
            if ts:
                anchors.append(ts)
    return min(anchors) if anchors else _as_utc(case.opened_at)


def get_cases_approaching_sla(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    """Real PENDING cases either overdue or nearing their priority's real
    v1 SLA window, aged from each case's own real event time (see module
    docstring -- never opened_at directly for non-fraud cases). Sorted
    most-urgent (most overdue, then soonest-due) first.
    """
    cases = (
        db.query(InvestigationCase)
        .filter_by(tenant_bank_id=tenant_bank_id, validation_status="PENDING")
        .all()
    )
    if not cases:
        return {"cases": [], "urgent_count": 0}

    # One batched query for every case's alerts, and one batched
    # CanonicalEvent lookup for the whole call, instead of a query per
    # case (for its alerts) plus a query per alert (for its real anchor)
    # -- confirmed via direct latency measurement that this was the
    # dominant real cost behind slow page loads.
    case_ids = [case.id for case in cases]
    alerts_by_case_id: dict[int, list[InvestigationCaseAlert]] = defaultdict(list)
    for alert in db.query(InvestigationCaseAlert).filter(InvestigationCaseAlert.case_id.in_(case_ids)).all():
        alerts_by_case_id[alert.case_id].append(alert)
    lookup = CanonicalEventLookup(db, tenant_bank_id)

    anchors: dict[int, datetime] = {}
    for case in cases:
        anchors[case.id] = resolve_real_case_anchor(case, alerts_by_case_id.get(case.id, []), lookup)

    reference_now = max(anchors.values())

    results = []
    for case in cases:
        window_days = _SLA_WINDOW_DAYS.get(case.priority_level, _DEFAULT_WINDOW_DAYS)
        age_days = (reference_now - anchors[case.id]).total_seconds() / 86400
        remaining_days = window_days - age_days
        if remaining_days > window_days * _APPROACHING_FRACTION:
            continue  # plenty of real time left -- not shown

        results.append({
            "case_id": case.id,
            "case_code": case.case_code,
            "title": case.title,
            "priority_level": case.priority_level,
            "current_exposure": case.current_exposure,
            "sla_window_days": window_days,
            "age_days": round(age_days, 1),
            "remaining_days": round(remaining_days, 1),  # negative == overdue by that many days
            "status": "overdue" if remaining_days <= 0 else "approaching",
        })

    results.sort(key=lambda r: r["remaining_days"])
    urgent_count = sum(1 for r in results if r["status"] == "overdue")
    return {"cases": results, "urgent_count": urgent_count}
