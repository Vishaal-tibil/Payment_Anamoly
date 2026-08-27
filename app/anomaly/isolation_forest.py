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
