"""AI Identified Patterns -- real, ranked category+rail patterns for the
Analyst Anomalies page. Not new ML pattern discovery: reuses the same
real category+rail clustering compute_cases() already groups
InvestigationCase rows by, the same category_weekly_trend() built for
the SLA/Case-Summary trend features, and the same real
current_exposure/priority_level every case already carries. Packaging
already-real numbers together, not inventing new ones.

A pattern only appears here if category_weekly_trend() found 2+ real
weeks of history for it -- no real baseline means no fabricated
"vs baseline" percentage, same honesty rule trend.py itself follows.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from ..canonical_event_lookup import CanonicalEventLookup
from .models import InvestigationCase
from .trend import category_weekly_trend

_CATEGORY_LABELS = {
    "NETWORK_TIMEOUT_SPIKE": "Failure-rate spike",
    "BATCH_NOT_SETTLED": "Batch never settles",
    "DUPLICATE_PAYMENT": "Duplicate payment",
    "FORMAT_REJECTION": "Formatting rejection",
    "FORMAT_REJECTION_SPIKE": "Formatting rejection spike",
    "CONFIRMED_BREAK": "Confirmed reconciliation break",
    "PROVISIONAL_VARIANCE": "Provisional reconciliation variance",
}

_PRIORITY_RANK = {"Critical": 3, "High": 2, "Medium": 1, "Low": 0}


def _pattern_title(category: str, rail: str | None) -> str:
    if category.startswith("FRAUD_"):
        band = category.removeprefix("FRAUD_").capitalize()
        label = f"{band} anomaly cluster"
    else:
        label = _CATEGORY_LABELS.get(category, category.replace("_", " ").title())
    return f"{rail} {label}" if rail else label


def get_ai_identified_patterns(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    cases = db.query(InvestigationCase).filter_by(tenant_bank_id=tenant_bank_id).all()

    grouped: dict[tuple[str, str | None], list[InvestigationCase]] = defaultdict(list)
    for case in cases:
        grouped[(case.category, case.payment_rail)].append(case)

    # One shared CanonicalEvent scan for every (category, rail) pair below.
    # category_weekly_trend used to build its own per call, so this loop
    # scanned every CanonicalEvent row once per pair -- 20 scans / 10,400
    # rows for one real request, and by far this endpoint's dominant cost.
    lookup = CanonicalEventLookup(db, tenant_bank_id)

    patterns = []
    for (category, rail), group in grouped.items():
        trend = category_weekly_trend(db, tenant_bank_id, category, rail, lookup)
        if trend is None:
            continue  # no real 2+ week baseline -- never show a fabricated vs-baseline number

        vs_baseline_percent = round((trend["latest_week_count"] / trend["prior_weeks_average_count"] - 1) * 100)
        exposure = sum(c.current_exposure for c in group if c.current_exposure is not None)
        worst = max(group, key=lambda c: _PRIORITY_RANK.get(c.priority_level, -1))

        patterns.append({
            "title": _pattern_title(category, rail),
            "category": category,
            "payment_rail": rail,
            "direction": trend["direction"],
            "vs_baseline_percent": vs_baseline_percent,
            "exposure": round(exposure, 2),
            "severity": worst.priority_level,
            "case_count": len(group),
            "representative_case_id": worst.id,
        })

    patterns.sort(key=lambda p: abs(p["vs_baseline_percent"]), reverse=True)
    return {"patterns": patterns, "total": len(patterns)}
