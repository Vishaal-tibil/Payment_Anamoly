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


def _zscore(value: float, prior_values: list[float]) -> float | None:
    if len(prior_values) < _MIN_PRIOR_WEEKS:
        return None
    mean = statistics.mean(prior_values)
    std = statistics.stdev(prior_values) if len(prior_values) > 1 else 0.0
    if std == 0:
        return 0.0 if value == mean else _ZERO_VARIANCE_DRIFT_MAGNITUDE
    return (value - mean) / std


def _combined_drift(row: EntitySnapshot, prior_rows: list[EntitySnapshot]) -> float | None:
    """Mean absolute z-score across whichever DRIFT_FEATURES are non-null
    for this row -- combining multiple features into the "one drift score
    per row" the knowledge doc's Section 7 calls for.
    """
    zscores = []
    for feature in DRIFT_FEATURES:
        value = getattr(row, feature)
        if value is None:
            continue
        prior_values = [getattr(r, feature) for r in prior_rows if getattr(r, feature) is not None]
        z = _zscore(value, prior_values)
        if z is not None:
            zscores.append(abs(z))
    if not zscores:
        return None
    return sum(zscores) / len(zscores)


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
    for party_rows in by_party.values():
        party_rows.sort(key=lambda r: r.window_start)
        for i, row in enumerate(party_rows):
            drift = _combined_drift(row, party_rows[:i])
            if drift is not None:
                raw_scores[row.id] = drift

    rescaled = _rescale_to_0_100(raw_scores)
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
