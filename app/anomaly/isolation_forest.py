"""Track B: Isolation Forest anomaly scoring, per
unsupervised-anomaly-detection-knowledge.md Section 7.

Reads only the Input contract columns off EntitySnapshot (see README's
"Input contract" section) -- never party_features, never the pre-computed
risk flags on canonical_events. Writes only isolation_forest_score, by
row id, on the existing EntitySnapshot rows.

Model granularity (knowledge doc Section 9): every resolved party today
has under 50 total transactions (merchants: 19-45, individuals: 1-6,
confirmed against real data), which puts 100% of entities in the
"< 50 observations -> global/segment baseline" tier. So this trains one
IsolationForest per segment (MERCHANT, INDIVIDUAL), pooling every entity's
snapshot rows in that segment, not one model per entity. _observation_tier
below is a real, checked branch on each entity's total transaction count
-- the ENTITY_SPECIFIC/FULL_MODEL branches are intentionally not
implemented (they raise) since no entity qualifies yet; this lets the
segment-baseline path run today and upgrade later without a rewrite,
rather than silently mis-scoring entities that do qualify once history
grows.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from sklearn.ensemble import IsolationForest
from sqlalchemy.orm import Session

from .models import EntitySnapshot

# The 12 Input-contract feature columns models train/score on.
FEATURE_COLUMNS = [
    "transaction_count",
    "amount_total",
    "amount_avg",
    "amount_median",
    "amount_std",
    "unique_counterparties",
    "new_counterparty_ratio",
    "retry_ratio",
    "avg_response_time_ms",
    "timeout_ratio",
    "format_reject_ratio",
    "account_age_days",
]

# Section 9 fallback tiers, keyed off each entity's total transaction count
# (summed across its own snapshot rows in the segment). Only the < 50
# branch is reachable with today's data.
_ENTITY_SPECIFIC_MIN_OBS = 50
_FULL_MODEL_MIN_OBS = 200

_RANDOM_STATE = 42


def _observation_tier(total_txns: int) -> str:
    if total_txns > _FULL_MODEL_MIN_OBS:
        return "FULL_MODEL"
    if total_txns >= _ENTITY_SPECIFIC_MIN_OBS:
        return "ENTITY_SPECIFIC"
    return "SEGMENT_BASELINE"


def _segment_medians(rows: list[EntitySnapshot]) -> dict[str, float]:
    """Per-feature median across a segment's train rows, used to impute
    the rare null (e.g. avg_response_time_ms when a rail never reports
    response timing) -- "assume average on the thing we don't know" is a
    defensible default at this data volume; falls back to 0.0 only if a
    column is null for every single row (not observed in real data today).
    """
    medians: dict[str, float] = {}
    for col in FEATURE_COLUMNS:
        values = [getattr(r, col) for r in rows if getattr(r, col) is not None]
        medians[col] = float(statistics.median(values)) if values else 0.0
    return medians


def _row_to_vector(row: EntitySnapshot, medians: dict[str, float]) -> list[float]:
    return [
        float(getattr(row, col)) if getattr(row, col) is not None else medians[col]
        for col in FEATURE_COLUMNS
    ]


def _rescale_to_percentile(raw_scores: list[float]) -> list[float]:
    """IsolationForest.score_samples: lower raw score = more anomalous.
    Rescale to 0-100 where 100 = most anomalous, via percentile rank
    against this segment's own score distribution (v1 choice per README --
    rank-based, so it's stable regardless of the raw score's absolute
    range, and comparable across segments of different sizes).
    """
    n = len(raw_scores)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    order = sorted(range(n), key=lambda i: raw_scores[i])  # ascending: most anomalous first
    percentiles = [0.0] * n
    for rank, idx in enumerate(order):
        percentiles[idx] = 100.0 * (n - 1 - rank) / (n - 1)
    return percentiles


def _score_segment(db: Session, tenant_bank_id: str | None, segment: str) -> dict[str, Any]:
    query = db.query(EntitySnapshot).filter(EntitySnapshot.segment == segment)
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    rows = query.all()
    if not rows:
        return {"segment": segment, "rows_scored": 0, "tier_counts": {}}

    totals_by_party: dict[str, int] = defaultdict(int)
    for r in rows:
        totals_by_party[r.party_id] += r.transaction_count
    tiers = {party_id: _observation_tier(total) for party_id, total in totals_by_party.items()}
    tier_counts = {
        tier: sum(1 for t in tiers.values() if t == tier)
        for tier in ("SEGMENT_BASELINE", "ENTITY_SPECIFIC", "FULL_MODEL")
    }

    upgraded_parties = [pid for pid, tier in tiers.items() if tier != "SEGMENT_BASELINE"]
    if upgraded_parties:
        # Not reachable with today's real data (every party < 50 total
        # txns) -- once an entity crosses the threshold it needs its own
        # per-entity model per Section 9, which isn't built yet. Fail
        # loudly rather than silently scoring it against the pooled
        # segment baseline as if nothing changed.
        raise NotImplementedError(
            f"{segment}: {len(upgraded_parties)} part(ies) now qualify for a "
            f"per-entity model (>= {_ENTITY_SPECIFIC_MIN_OBS} observations) -- "
            "not implemented, only the segment-baseline tier is built: "
            f"{upgraded_parties}"
        )

    train_rows = [r for r in rows if r.split == "train"] or rows
    medians = _segment_medians(train_rows)

    model = IsolationForest(random_state=_RANDOM_STATE)
    model.fit([_row_to_vector(r, medians) for r in train_rows])

    raw_scores = model.score_samples([_row_to_vector(r, medians) for r in rows]).tolist()
    rescaled = _rescale_to_percentile(raw_scores)

    for row, score in zip(rows, rescaled):
        db.query(EntitySnapshot).filter_by(id=row.id).update({"isolation_forest_score": score})

    return {
        "segment": segment,
        "tier_counts": tier_counts,
        "parties": len(totals_by_party),
        "train_rows": len(train_rows),
        "rows_scored": len(rows),
        "score_min": min(rescaled),
        "score_median": statistics.median(rescaled),
        "score_max": max(rescaled),
    }


def train_and_score(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Trains one IsolationForest per segment (MERCHANT, INDIVIDUAL) on that
    segment's split="train" EntitySnapshot rows, scores every row in the
    segment (train + test), rescales to 0-100 (percentile rank within the
    segment), and writes EntitySnapshot.isolation_forest_score back onto
    each row by id. Callable standalone (scripts/tests) or from an
    endpoint later, same shape as resolve_parties()/compute_features().
    """
    results = {segment: _score_segment(db, tenant_bank_id, segment) for segment in ("MERCHANT", "INDIVIDUAL")}
    db.commit()
    return results


# --- Section 8: final aggregation -----------------------------------------
# NOT wired to a script/endpoint yet, and must not be run against real data
# until Track C (timeseries_drift_score) and Track D (cluster_id /
# cluster_changed) are both merged into main. Every EntitySnapshot row's
# timeseries_drift_score and cluster_changed are still None on real data
# today -- calling this now would silently produce a "final" score that's
# really just 0.40 * isolation_forest_score with the other two signals
# zeroed out, which is worse than no final score at all if it ends up on a
# dashboard looking authoritative. Built and tested against synthetic data
# now so it's ready to run for real the moment both tracks land -- that's
# the second PR per the README, not this one.

_AGGREGATION_WEIGHTS = {"isolation_forest": 0.40, "clustering": 0.25, "timeseries": 0.35}

# KNOWN SCALE MISMATCH -- flag to the team before relying on this for real:
# isolation_forest_score and timeseries_drift_score are both 0-100, but the
# README specifies clustering_signal as a literal 0/1 ("treat a True/changed
# cluster as a 0/1 signal"). Plugged into the weighted sum as-is, the
# maximum possible final_score is 0.40*100 + 0.25*1 + 0.35*100 = 75.25, so
# the "Critical" band (80-100) is mathematically unreachable. Implemented
# exactly per the literal spec below rather than silently rescaling
# clustering_signal to 0/100 on my own judgment -- that's a real decision
# for whoever owns the final weights to make once Track D's actual output
# is visible, not something to guess at now.


def _clustering_signal(cluster_changed: bool | None) -> float:
    # True/changed -> 1.0, False -> 0.0, None (not applicable yet, e.g. an
    # individual with no prior snapshot to compare against) -> 0.0, per
    # the README's explicit call: treat "not applicable" as "no evidence
    # of change" rather than dropping the entity or crashing.
    return 1.0 if cluster_changed else 0.0


def _timeseries_signal(timeseries_drift_score: float | None) -> float:
    # Provisional, same reasoning as clustering: None (individuals have no
    # weekly sequence to drift-score, per Track C) contributes 0 rather
    # than being treated as missing data. Revisit once real Track C
    # output is available -- a null timeseries score for every INDIVIDUAL
    # row means their final_score is only ever isolation_forest_score
    # weighted, which is a real limitation worth flagging to the team, not
    # silently smoothing over.
    return 0.0 if timeseries_drift_score is None else float(timeseries_drift_score)


def _anomaly_band(score: float) -> str:
    if score < 30.0:
        return "Normal"
    if score < 60.0:
        return "Low-Medium"
    if score < 80.0:
        return "High"
    return "Critical"


def compute_final_score(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Section 8: final_score = 0.40*IF + 0.25*clustering + 0.35*timeseries,
    written to EntitySnapshot.final_anomaly_score / anomaly_band by row id.

    Requires isolation_forest_score already populated (raises if any
    targeted row is missing it -- run train_and_score() first). Does NOT
    require timeseries_drift_score/cluster_changed to be non-null -- see
    the module-level note above for why null there means "0 contribution"
    rather than "skip this row", and why that's provisional.
    """
    query = db.query(EntitySnapshot)
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    rows = query.all()

    missing_if = [r.id for r in rows if r.isolation_forest_score is None]
    if missing_if:
        raise ValueError(
            f"{len(missing_if)} row(s) missing isolation_forest_score -- run train_and_score() first: {missing_if[:10]}"
        )

    band_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        final = (
            _AGGREGATION_WEIGHTS["isolation_forest"] * row.isolation_forest_score
            + _AGGREGATION_WEIGHTS["clustering"] * _clustering_signal(row.cluster_changed)
            + _AGGREGATION_WEIGHTS["timeseries"] * _timeseries_signal(row.timeseries_drift_score)
        )
        band = _anomaly_band(final)
        band_counts[band] += 1
        db.query(EntitySnapshot).filter_by(id=row.id).update({"final_anomaly_score": final, "anomaly_band": band})

    db.commit()
    return {"rows_scored": len(rows), "band_counts": dict(band_counts)}
