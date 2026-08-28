"""Track C: time-series drift detection, per
unsupervised-anomaly-detection-knowledge.md Section 7 ("start with rolling
mean/std, z-score, EWMA, CUSUM before LSTM/Transformer"). This is also the
primary detector for Section 10's Velocity Checks category -- "unusual
increase in frequency/rate of transactions" is exactly what a rolling
z-score on transaction_count over a merchant's own history measures.

Meaningful only for merchants' WEEKLY rows, where an actual chronological
sequence exists. Individuals have one TO_DATE row each -- no sequence to
drift against -- so timeseries_drift_score is left null for them, same
reasoning clustering.py uses for cluster_changed.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from .models import EntitySnapshot

# Section 5's suggested starting features for drift detection -- amount,
# volume, and new-counterparty behavior are the three most likely to
# reflect a genuine behavioral shift rather than routine week-to-week noise.
DRIFT_FEATURES = ["transaction_count", "amount_total", "new_counterparty_ratio"]

# Relative importance of each feature in the combined score -- flat (1.0
# each) today, but a real, named lever rather than an implicit assumption
# buried in an unweighted average, so it can be tuned later (e.g. once
# analyst feedback says amount spikes matter more than count spikes)
# without restructuring _combined_drift().
DRIFT_FEATURE_WEIGHTS: dict[str, float] = {
    "transaction_count": 1.0,
    "amount_total": 1.0,
    "new_counterparty_ratio": 1.0,
}

# Need at least this many prior weeks before a rolling mean/std means
# anything -- below this, a "drift score" would just be noise dressed up
# as a number.
_MIN_PRIOR_WEEKS = 2

# When a feature has had zero variance across all prior weeks, an ordinary
# z-score is undefined (division by zero). Any change at all from a
# perfectly flat history is a real signal, not noise -- scored as this
# fixed, deliberately large magnitude rather than left null, so it isn't
# silently dropped from the combined score.
_ZERO_VARIANCE_DRIFT_MAGNITUDE = 5.0

# EWMA gives more weight to recent weeks than a flat mean does -- catches
# a creeping drift that's still within the flat mean/std's normal range
# because older, pre-drift weeks are diluting the average.
_EWMA_ALPHA = 0.3

# CUSUM's allowance (in std units): drift smaller than this per week is
# treated as noise and doesn't accumulate. Catches a persistent SMALL
# drift that never trips a single week's z-score threshold but adds up
# over several weeks -- the knowledge doc's own next-step-after-z-score.
_CUSUM_ALLOWANCE = 0.5


def _zscore(value: float, prior_values: list[float]) -> float | None:
    if len(prior_values) < _MIN_PRIOR_WEEKS:
        return None
    mean = statistics.mean(prior_values)
    std = statistics.stdev(prior_values) if len(prior_values) > 1 else 0.0
    if std == 0:
        return 0.0 if value == mean else _ZERO_VARIANCE_DRIFT_MAGNITUDE
    return (value - mean) / std


def _ewma_zscore(value: float, prior_values: list[float]) -> float | None:
    """Same as _zscore, but against an EWMA-smoothed baseline instead of
    the flat mean -- recent prior weeks count more than older ones.
    """
    if len(prior_values) < _MIN_PRIOR_WEEKS:
        return None
    baseline = prior_values[0]
    for v in prior_values[1:]:
        baseline = _EWMA_ALPHA * v + (1 - _EWMA_ALPHA) * baseline
    std = statistics.stdev(prior_values) if len(prior_values) > 1 else 0.0
    if std == 0:
        return 0.0 if value == baseline else _ZERO_VARIANCE_DRIFT_MAGNITUDE
    return (value - baseline) / std


def _cusum_signal(prior_values: list[float], value: float) -> float:
    """Standardized two-sided CUSUM over prior_values + [value]; returns
    the largest accumulated deviation reached at any point in the
    sequence (in std units, so directly comparable to a z-score).
    """
    sequence = prior_values + [value]
    if len(sequence) < 2:
        return 0.0
    mean = statistics.mean(sequence)
    std = statistics.stdev(sequence)
    if std == 0:
        return 0.0
    standardized = [(v - mean) / std for v in sequence]
    s_pos = s_neg = 0.0
    peak = 0.0
    for x in standardized:
        s_pos = max(0.0, s_pos + x - _CUSUM_ALLOWANCE)
        s_neg = min(0.0, s_neg + x + _CUSUM_ALLOWANCE)
        peak = max(peak, s_pos, -s_neg)
    return peak


def _feature_drift_signal(value: float, prior_values: list[float]) -> float | None:
    """Combines three complementary signals for one feature -- an ordinary
    z-score, an EWMA-baselined z-score (catches creeping drift a flat
    mean would dilute), and a CUSUM statistic (catches a persistent small
    drift no single week's z-score alone would flag). All three are
    already in std-normalized units, so averaging them needs no further
    rescaling.
    """
    z = _zscore(value, prior_values)
    if z is None:
        return None
    ewma_z = _ewma_zscore(value, prior_values)
    cusum = _cusum_signal(prior_values, value)
    signals = [abs(z), abs(ewma_z) if ewma_z is not None else abs(z), cusum]
    return sum(signals) / len(signals)


def _combined_drift(row: EntitySnapshot, prior_rows: list[EntitySnapshot]) -> float | None:
    """Weighted average of _feature_drift_signal across whichever
    DRIFT_FEATURES are non-null for this row -- combining multiple
    features into the "one drift score per row" Section 7 calls for.
    """
    weighted_sum = 0.0
    weight_total = 0.0
    for feature in DRIFT_FEATURES:
        value = getattr(row, feature)
        if value is None:
            continue
        prior_values = [getattr(r, feature) for r in prior_rows if getattr(r, feature) is not None]
        signal = _feature_drift_signal(value, prior_values)
        if signal is not None:
            weight = DRIFT_FEATURE_WEIGHTS.get(feature, 1.0)
            weighted_sum += signal * weight
            weight_total += weight
    if weight_total == 0:
        return None
    return weighted_sum / weight_total


def _rescale_to_0_100(raw_scores: dict[int, float]) -> dict[int, float]:
    """Min-max rescale within the set of rows that got a raw score, so the
    output is comparable to the other tracks' 0-100 scores (same v1
    approach Track B's isolation_forest_score uses).
    """
    if not raw_scores:
        return {}
    values = list(raw_scores.values())
    lo, hi = min(values), max(values)
    if hi == lo:
        return {row_id: 0.0 for row_id in raw_scores}
    return {row_id: (v - lo) / (hi - lo) * 100 for row_id, v in raw_scores.items()}


def score_drift(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Writes timeseries_drift_score onto EntitySnapshot's merchant WEEKLY
    rows. Fully derived, so each run recomputes and overwrites every row
    it covers -- same idempotency shape as clustering.py/compute_snapshots().

    Rescaling is done per-tenant, not globally: party_id is already
    tenant-unique so grouping-by-party never mixes tenants, but a global
    min-max rescale across a tenant_bank_id=None call would still let one
    tenant's biggest spike dictate the whole 0-100 scale for every other
    tenant's rows -- diluting a real but smaller-magnitude spike at a
    quieter bank down near 0.
    """
    weekly_query = db.query(EntitySnapshot).filter(
        EntitySnapshot.segment == "MERCHANT", EntitySnapshot.window_type == "WEEKLY",
    )
    if tenant_bank_id:
        weekly_query = weekly_query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    weekly_rows = weekly_query.all()

    by_party: dict[str, list[EntitySnapshot]] = defaultdict(list)
    for row in weekly_rows:
        by_party[row.party_id].append(row)

    raw_scores: dict[int, float] = {}
    row_by_id: dict[int, EntitySnapshot] = {}
    for party_rows in by_party.values():
        party_rows.sort(key=lambda r: r.window_start)
        for i, row in enumerate(party_rows):
            row_by_id[row.id] = row
            drift = _combined_drift(row, party_rows[:i])
            if drift is not None:
                raw_scores[row.id] = drift

    scores_by_tenant: dict[str, dict[int, float]] = defaultdict(dict)
    for row_id, score in raw_scores.items():
        scores_by_tenant[row_by_id[row_id].tenant_bank_id][row_id] = score

    rescaled: dict[int, float] = {}
    for tenant_scores in scores_by_tenant.values():
        rescaled.update(_rescale_to_0_100(tenant_scores))

    for row in weekly_rows:
        row.timeseries_drift_score = rescaled.get(row.id)  # None if not enough prior history yet

    individual_query = db.query(EntitySnapshot).filter(EntitySnapshot.segment == "INDIVIDUAL")
    if tenant_bank_id:
        individual_query = individual_query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    for row in individual_query.all():
        row.timeseries_drift_score = None

    db.commit()

    return {
        "merchant_weekly_rows": len(weekly_rows),
        "rows_scored": len(rescaled),
        "rows_insufficient_history": len(weekly_rows) - len(rescaled),
    }
