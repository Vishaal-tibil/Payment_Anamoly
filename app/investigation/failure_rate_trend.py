"""Real per-case Failure Rate trend -- Case Details' "Supporting
Evidence" panel. Two different real charts, depending on what this
case's category actually has:

- NETWORK_TIMEOUT_SPIKE / FORMAT_REJECTION_SPIKE: a real per-party
  weekly RATE (EntitySnapshot.timeout_ratio / format_reject_ratio) vs. a
  rolling baseline -- the same rate app/operations/drift.py and
  app/operations/format_rejection.py already z-score against that same
  party's own prior weeks. This case's own severity, at party
  granularity.
- Every other category (CONFIRMED_BREAK, PROVISIONAL_VARIANCE,
  DUPLICATE_PAYMENT, FORMAT_REJECTION, BATCH_NOT_SETTLED, FRAUD_HIGH/
  CRITICAL): no per-case rate concept exists (these are discrete
  transaction events or fraud scores, not a rate), so instead falls back
  to app/investigation/trend.py's real tenant-wide weekly COUNT trend
  for this case's (category, rail) -- how often this KIND of issue is
  happening, not this case's own rate. Different question, still real,
  never a fabricated line. available=False only when even that has
  fewer than 2 real weeks of history.

Granularity is weekly, not hourly -- EntitySnapshot's real windowing,
not an invented finer resolution. There is also no real infrastructure-
event concept in this schema (e.g. "Posting Service 03 latency
degradation") to caption a point with, so annotations are limited to
real detected spikes / real volume-direction changes.

Rate-branch baseline_value/threshold_value are reconstructed from the
exact same z-score inputs (mean/stdev of that party's own prior weeks)
the real detector used for the triggering week, at the exact same flag
threshold each detector enforces (|z|>=2.0 for timeout, |z|>=3.0 --
equivalent to format_rejection.py's 60.0/100 rescaled cutoff, see its
_rescale -- for format rejection). Frequency-branch baseline_value/
threshold_value reuse trend.py's own real _DIRECTION_BAND cutoff. Not a
new judgment call in either branch, a re-derivation of a real one
already made elsewhere.
"""
from __future__ import annotations

import statistics
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import EntitySnapshot
from ..anomaly.timeseries import _zscore
from ..operations.models import OperationalIssue
from .models import InvestigationCase, InvestigationCaseAlert
from .patterns import _pattern_title
from .trend import _DIRECTION_BAND, category_weekly_trend

_RATE_FIELD = {
    "NETWORK_TIMEOUT_SPIKE": "timeout_ratio",
    "FORMAT_REJECTION_SPIKE": "format_reject_ratio",
}
_TITLE = {
    "NETWORK_TIMEOUT_SPIKE": "Network Timeout Rate vs. Expected Baseline",
    "FORMAT_REJECTION_SPIKE": "Format Rejection Rate vs. Expected Baseline",
}
# Same real flag thresholds app/operations/drift.py (|z|>=2.0) and
# app/operations/format_rejection.py (rescaled score>=60.0, which is
# |z|>=3.0 on this single-feature series -- see that module's
# _SPIKE_SEVERITY_THRESHOLD/_rescale) already enforce -- not a new number.
_Z_THRESHOLD = {"NETWORK_TIMEOUT_SPIKE": 2.0, "FORMAT_REJECTION_SPIKE": 3.0}

_UNAVAILABLE: dict[str, Any] = {
    "available": False, "reason": None,
    "title": None, "subtitle": None, "unit": None,
    "max_value": None, "baseline_value": None, "threshold_value": None,
    "points": [], "annotations": [],
}


def _unavailable(reason: str) -> dict[str, Any]:
    return {**_UNAVAILABLE, "reason": reason}


def _category_frequency_trend(db: Session, tenant_bank_id: str, case: InvestigationCase) -> dict[str, Any]:
    """Fallback for the 5 categories with no per-case rate concept -- see
    module docstring. Reshapes trend.py's real weekly count series into
    the same chart contract the rate branch returns, so the frontend
    doesn't need a second chart component.
    """
    label = _pattern_title(case.category, case.payment_rail)
    trend = category_weekly_trend(db, tenant_bank_id, case.category, case.payment_rail)
    if trend is None:
        return _unavailable(f"Not enough real weekly history yet for {label.lower()} to show a frequency trend.")

    points = [{"time": w["week_start"], "value": w["count"]} for w in trend["counts_by_week"]]
    baseline = trend["prior_weeks_average_count"]
    # Same real "meaningfully elevated" cutoff trend.py itself uses to
    # call a week's direction "increasing" -- not a new number.
    threshold = baseline * (1 + _DIRECTION_BAND)

    annotations = []
    if trend["direction"] in ("increasing", "decreasing"):
        latest = trend["counts_by_week"][-1]
        annotations.append({
            "time": latest["week_start"],
            "label": f"Volume {trend['direction']} (vs. {baseline:.1f}/week avg)",
        })

    max_value = round(max([p["value"] for p in points] + [threshold]) * 1.25, 1)

    return {
        "available": True,
        "reason": None,
        "title": f"{label} -- Weekly Frequency",
        "subtitle": (
            f"Tenant-wide weekly count for this issue type"
            f"{f' on {case.payment_rail}' if case.payment_rail else ''} -- not this case's own rate "
            f"({trend['weeks_observed']} real weeks observed)."
        ),
        "unit": "count",
        "max_value": max_value,
        "baseline_value": round(baseline, 2),
        "threshold_value": round(threshold, 2),
        "points": points,
        "annotations": annotations,
    }


def get_case_failure_rate_trend(db: Session, tenant_bank_id: str, case_id: int) -> dict[str, Any] | None:
    """None only if the case itself doesn't exist for this tenant (404
    territory). Otherwise always a dict; available=False (points=[]) only
    if even the category-wide frequency fallback has under 2 real weeks
    of history for this (category, rail).
    """
    case = db.query(InvestigationCase).filter_by(id=case_id, tenant_bank_id=tenant_bank_id).one_or_none()
    if case is None:
        return None

    rate_field = _RATE_FIELD.get(case.category)
    if rate_field is None:
        return _category_frequency_trend(db, tenant_bank_id, case)

    alerts = db.query(InvestigationCaseAlert).filter_by(case_id=case.id, tenant_bank_id=tenant_bank_id).all()
    issue_ids = [a.source_id for a in alerts if a.source_type == "OPERATIONAL_ISSUE"]
    issues = db.query(OperationalIssue).filter(OperationalIssue.id.in_(issue_ids)).all() if issue_ids else []
    if not issues:
        return _unavailable("This case's underlying alert no longer exists -- rebuild investigation cases.")

    # Case is at least as urgent as its worst alert (same rule
    # InvestigationCase.severity_score already used) -- that alert's
    # party is this chart's real subject.
    worst_issue = max(issues, key=lambda i: i.severity_score if i.severity_score is not None else -1.0)
    party_id = worst_issue.reference_id

    rows = (
        db.query(EntitySnapshot)
        .filter_by(tenant_bank_id=tenant_bank_id, party_id=party_id, window_type="WEEKLY")
        .order_by(EntitySnapshot.window_start)
        .all()
    )
    rows = [r for r in rows if getattr(r, rate_field) is not None]
    if len(rows) < 3:  # need >=2 real prior weeks plus the triggering week itself
        return _unavailable(f"Fewer than 3 real weekly snapshots for {party_id} -- not enough history for a baseline.")

    z_threshold = _Z_THRESHOLD[case.category]
    points = []
    annotations = []
    triggering_baseline = None
    triggering_threshold = None

    for i, row in enumerate(rows):
        value_percent = getattr(row, rate_field) * 100
        points.append({"time": row.window_start.date().isoformat(), "value": round(value_percent, 2)})

        prior = [getattr(r, rate_field) for r in rows[:i]]
        z = _zscore(getattr(row, rate_field), prior)
        if z is not None and abs(z) >= z_threshold:
            metric_label = "timeout" if rate_field == "timeout_ratio" else "rejection"
            annotations.append({
                "time": row.window_start.date().isoformat(),
                "label": f"Spike flagged ({metric_label} rate, |z|={abs(z):.1f})",
            })

        if prior and row.window_end == worst_issue.window_end:
            mean = statistics.mean(prior)
            std = statistics.stdev(prior) if len(prior) >= 2 else 0.0
            triggering_baseline = mean * 100
            triggering_threshold = (mean + z_threshold * std) * 100

    if triggering_baseline is None:
        # Fallback -- shouldn't happen (the detector requires >=2 real
        # prior weeks to have flagged this case's own triggering alert at
        # all), but avoids a crash if the case's snapshot data has since
        # changed underneath it. Uses the most recent scored week instead.
        prior = [getattr(r, rate_field) for r in rows[:-1]]
        mean = statistics.mean(prior)
        std = statistics.stdev(prior) if len(prior) >= 2 else 0.0
        triggering_baseline = mean * 100
        triggering_threshold = (mean + z_threshold * std) * 100

    all_values = [p["value"] for p in points]
    max_value = round(max(all_values + [triggering_threshold]) * 1.25, 1)

    return {
        "available": True,
        "reason": None,
        "title": _TITLE[case.category],
        "subtitle": f"{party_id} -- {len(rows)} real weekly snapshots",
        "unit": "%",
        "max_value": max_value,
        "baseline_value": round(triggering_baseline, 2),
        "threshold_value": round(triggering_threshold, 2),
        "points": points,
        "annotations": annotations,
    }
