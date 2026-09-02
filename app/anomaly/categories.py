"""Maps the platform's named fraud patterns (New Payee Risk, Structuring,
Velocity Checks -- see app/dashboard.py's get_anomaly_detection_categories
for the fourth, Funnel Account, which lives on BeneficiarySnapshot instead)
onto EntitySnapshot rows that are already flagged.

Not a new detector -- every value read here is already computed by Track A/B
(new_counterparty_ratio, near_threshold_ratio, timeseries_drift_score). This
only tags which of a row's own real feature values are elevated relative to
its own segment, so a viewer sees *why* a row was flagged in the platform's
own named vocabulary, not just an opaque isolation_forest_score.

Thresholds are each segment's own 75th percentile for that feature,
computed fresh from real data on every call -- never a fixed number.
Confirmed against real Meridian data before picking p75 specifically:
merchant new_counterparty_ratio sits high across almost the whole segment
(median 0.75, p75 1.0) since most merchants see mostly first-time payees
each week, so a fixed threshold like ">= 0.5" would tag nearly everything;
p75 keeps the tag meaningful (only the row's own genuine outliers within
its peer group qualify) regardless of how a given tenant's data happens
to be shaped.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..query_filters import parse_date_bound
from .models import BeneficiarySnapshot, EntitySnapshot

_MATERIAL_ANOMALY_BANDS = ("High", "Critical")

NEW_PAYEE_RISK = "New Payee Risk"
STRUCTURING = "Structuring"
VELOCITY_CHECKS = "Velocity Checks"

# (category label, EntitySnapshot column name)
_CATEGORY_FEATURES = (
    (NEW_PAYEE_RISK, "new_counterparty_ratio"),
    (STRUCTURING, "near_threshold_ratio"),
    # Velocity Checks is an approximation -- timeseries_drift_score blends
    # amount_total/transaction_count/new_counterparty_ratio (see
    # timeseries.py), it isn't isolated to transaction_count alone. Using
    # it as the velocity proxy matches what Settings > Anomaly Detection
    # already documents this category as ("time-series z-score drift on
    # transaction_count") -- an honest simplification, not a separate signal.
    (VELOCITY_CHECKS, "timeseries_drift_score"),
)

_PERCENTILE = 0.75

# Funnel Account lives on BeneficiarySnapshot, not EntitySnapshot -- a
# different table/tagging shape from the three categories above (a fixed
# score cutoff, not a segment percentile, since there's no natural
# "segment" to be relative to here). Kept in this module anyway since
# it's the platform's fourth named fraud category and this is the single
# place both main.py (API response tagging) and review/service.py (claim
# counting) import category logic from -- one definition, not two kept
# manually in sync. Documented v1 cutoff, same status as Format Rejection
# Spike's "score crosses 60": confirmed against real Meridian data before
# picking this -- of 69 real scored windows, scores cluster at 100.0 (1),
# 50.0 (5), then drop to ~28 and below. 50 catches that real top tier (6
# of 69, ~9%) without over-flagging the long tail below it.
FUNNEL_ACCOUNT_THRESHOLD = 50.0


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def categories_for_snapshots(db: Session, tenant_bank_id: str, segment: str) -> dict[int, list[str]]:
    """Returns {snapshot_id: [matched category labels]} for every row in
    this tenant+segment. Computed against the whole segment population,
    independent of whatever page/limit the caller applied -- a flagged
    row's tags don't change depending on how the list was paginated.
    """
    rows = db.query(EntitySnapshot).filter_by(tenant_bank_id=tenant_bank_id, segment=segment).all()

    thresholds = {}
    for label, column in _CATEGORY_FEATURES:
        values = [getattr(r, column) for r in rows if getattr(r, column) is not None]
        thresholds[label] = _percentile(values, _PERCENTILE)

    result: dict[int, list[str]] = {}
    for row in rows:
        tags = []
        for label, column in _CATEGORY_FEATURES:
            value = getattr(row, column)
            threshold = thresholds[label]
            # threshold > 0 guard: a segment where p75 is 0 (most rows at
            # zero) would otherwise tag every nonzero row as an "outlier"
            # off a threshold that isn't really discriminating anything.
            if value is not None and threshold is not None and threshold > 0 and value >= threshold:
                tags.append(label)
        result[row.id] = tags
    return result


def get_pattern_mix(
    db: Session, tenant_bank_id: str, start_date: str | None = None, end_date: str | None = None,
) -> dict[str, Any]:
    """"Known Patterns" vs "Newly Discovered" -- every flagged entity/
    beneficiary across both fraud tables, split by whether it matches at
    least one of the platform's 4 named categories. A row with zero
    matched categories is a real, honest outcome (Isolation Forest can
    flag a row for a feature *combination* that isn't any single feature
    being a peer-relative outlier) -- "newly discovered" here means
    exactly that: flagged by the model, not attributable to a named
    pattern yet, not a fabricated novelty-detection signal.

    start_date/end_date narrow to snapshots whose window_end falls in
    that range -- see query_filters.py's module docstring.
    """
    start_dt, end_dt = parse_date_bound(start_date), parse_date_bound(end_date, end_of_day=True)
    known = 0
    newly_discovered = 0

    for segment in ("MERCHANT", "INDIVIDUAL"):
        tags_by_id = categories_for_snapshots(db, tenant_bank_id, segment)
        flagged_query = db.query(EntitySnapshot.id).filter(
            EntitySnapshot.tenant_bank_id == tenant_bank_id,
            EntitySnapshot.segment == segment,
            EntitySnapshot.anomaly_band.in_(_MATERIAL_ANOMALY_BANDS),
        )
        if start_dt:
            flagged_query = flagged_query.filter(EntitySnapshot.window_end >= start_dt)
        if end_dt:
            flagged_query = flagged_query.filter(EntitySnapshot.window_end <= end_dt)
        flagged_ids = {row.id for row in flagged_query.all()}
        for snapshot_id in flagged_ids:
            if tags_by_id.get(snapshot_id):
                known += 1
            else:
                newly_discovered += 1

    # Funnel Account is always its own single tag by construction (see
    # main.py's _beneficiary_snapshot_summary) -- every funnel-flagged
    # beneficiary is "known" by definition, never "newly discovered".
    funnel_query = db.query(BeneficiarySnapshot).filter(
        BeneficiarySnapshot.tenant_bank_id == tenant_bank_id, BeneficiarySnapshot.funnel_drift_score >= FUNNEL_ACCOUNT_THRESHOLD,
    )
    if start_dt:
        funnel_query = funnel_query.filter(BeneficiarySnapshot.window_end >= start_dt)
    if end_dt:
        funnel_query = funnel_query.filter(BeneficiarySnapshot.window_end <= end_dt)
    funnel_count = funnel_query.count()
    known += funnel_count

    total = known + newly_discovered
    return {
        "known_count": known,
        "newly_discovered_count": newly_discovered,
        "total": total,
        "known_rate": (known / total) if total else None,
        "newly_discovered_rate": (newly_discovered / total) if total else None,
    }
