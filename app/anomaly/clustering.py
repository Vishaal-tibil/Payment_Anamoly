"""Track D: HDBSCAN clustering for the unsupervised anomaly detection
engine, per unsupervised-anomaly-detection-knowledge.md Section 7.

Reads only the feature columns EntitySnapshot already guarantees are
leakage-free (see features.py's module docstring) -- nothing here reads a
pre-computed risk flag.

At today's real data volume (10 merchants, 91 individuals -- every party
under 50 total observations), Section 9's fallback hierarchy puts
everyone in the "< 50 -> segment/global baseline" tier: cluster each
segment's pooled snapshot rows together (all MERCHANT weekly rows in one
model, all INDIVIDUAL to-date rows in another), not one model per entity.
_tier_for_observation_count() is a real, checked branch for the
50-200/>200 tiers so this upgrades cleanly once there's more history --
today it will only ever return SEGMENT_BASELINE, and cluster_and_score()
logs (not silently ignores) any party that would otherwise qualify for a
per-entity tier.

A cluster id alone isn't the signal (Section 7): cluster_id can shift
between runs/parameter choices and carries no inherent meaning. The
useful signal is cluster_changed -- whether a given merchant's cluster
differs from that same merchant's immediately preceding WEEKLY snapshot.
Individuals have only one TO_DATE row each, so there's no "previous" to
compare to -- cluster_changed is left null for them, same reasoning
timeseries.py uses for timeseries_drift_score.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.preprocessing import StandardScaler
from sqlalchemy.orm import Session

from .models import EntitySnapshot

# The feature set clustered on -- every column here is one EntitySnapshot
# already guarantees is derived from raw canonical_events facts, never a
# pre-computed risk flag (see Input contract in README / features.py).
#
# timeout_ratio is deliberately excluded: across every real snapshot row
# today it is a constant 0.0 (no rail in our data has reported a timeout
# yet), so it has zero variance -- it would contribute nothing to the
# distance metric and risks a divide-by-zero in StandardScaler. Add it
# back once real data shows any variation in it.
#
# near_threshold_ratio (fraction of an entity's amounts sitting just under
# a reporting threshold -- Section 10's Structuring signal) is computed on
# EntitySnapshot but deliberately left OUT of clustering's feature set for
# now: tested against real data, adding it as a 12th dimension collapses
# the merchant segment from 2 clusters to 100% noise -- at only 10
# merchants/83 rows, one more dimension is enough to break HDBSCAN's
# density requirement entirely, which would make cluster_changed flat
# (always False) and useless as a signal. Revisit inclusion once there's
# enough history for it to help rather than hurt; the column itself is
# already available to other tracks (e.g. Isolation Forest) today.
FEATURE_COLUMNS = [
    "transaction_count", "amount_total", "amount_avg", "amount_median", "amount_std",
    "unique_counterparties", "new_counterparty_ratio", "retry_ratio",
    "avg_response_time_ms", "format_reject_ratio", "account_age_days",
]

# Section 9 fallback tiers, keyed off each PARTY's total observed
# transactions (summed across every snapshot row it has in this segment --
# not the row count, since one merchant contributes many WEEKLY rows).
# Every real party today sits under 50, so only the SEGMENT_BASELINE
# branch in cluster_and_score() actually executes.
_ENTITY_BASELINE_MIN_OBSERVATIONS = 50
_ENTITY_FULL_MODEL_MIN_OBSERVATIONS = 200

# sklearn's HDBSCAN default. Real segments (83 merchant-weeks, 91
# individuals) comfortably clear this; clamped down for small segments
# (e.g. a sparse tenant, or a test fixture) so HDBSCAN doesn't choke on
# fewer rows than its own minimum cluster size.
_DEFAULT_MIN_CLUSTER_SIZE = 5


def _tier_for_observation_count(count: int) -> str:
    if count > _ENTITY_FULL_MODEL_MIN_OBSERVATIONS:
        return "ENTITY_FULL_MODEL"
    if count >= _ENTITY_BASELINE_MIN_OBSERVATIONS:
        return "ENTITY_BASELINE"
    return "SEGMENT_BASELINE"


def _build_feature_matrix(rows: list[EntitySnapshot]) -> np.ndarray:
    """Median-impute (per column, within this segment's rows) then
    standardize. HDBSCAN is distance-based -- amount_total (ranges into
    the hundreds of thousands) would otherwise swamp 0-1 ratio features
    like new_counterparty_ratio in the distance metric.
    """
    raw = np.array([[getattr(r, col) for col in FEATURE_COLUMNS] for r in rows], dtype=float)
    for j in range(raw.shape[1]):
        col = raw[:, j]
        missing = np.isnan(col)
        if missing.any():
            median = 0.0 if missing.all() else float(np.nanmedian(col))
            col[missing] = median
    return StandardScaler().fit_transform(raw)


def _cluster_segment(rows: list[EntitySnapshot]) -> dict[int, int]:
    """Returns {EntitySnapshot.id: cluster_id}. -1 is HDBSCAN's "noise" /
    unclustered label -- expected and fine, not an error condition.
    """
    if len(rows) < 2:
        return {r.id: -1 for r in rows}
    matrix = _build_feature_matrix(rows)
    min_cluster_size = min(_DEFAULT_MIN_CLUSTER_SIZE, len(rows))
    labels = HDBSCAN(min_cluster_size=min_cluster_size).fit_predict(matrix)
    return {row.id: int(label) for row, label in zip(rows, labels)}


def _compute_cluster_changed(
    merchant_weekly_rows: list[EntitySnapshot], cluster_ids: dict[int, int]
) -> dict[int, bool | None]:
    """True where a merchant's cluster differs from that same merchant's
    immediately preceding WEEKLY row, chronologically. The first row for
    a given merchant has no prior snapshot to compare against -> None.
    """
    result: dict[int, bool | None] = {}
    by_party: dict[str, list[EntitySnapshot]] = defaultdict(list)
    for row in merchant_weekly_rows:
        by_party[row.party_id].append(row)

    for party_rows in by_party.values():
        party_rows.sort(key=lambda r: r.window_start)
        previous_cluster: int | None = None
        for row in party_rows:
            current_cluster = cluster_ids[row.id]
            result[row.id] = None if previous_cluster is None else (current_cluster != previous_cluster)
            previous_cluster = current_cluster
    return result


def cluster_and_score(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Clusters each segment's pooled EntitySnapshot rows with HDBSCAN and
    writes cluster_id / cluster_changed back onto the existing rows (never
    a new table/row -- same output contract as every other track).

    Fully derived, so each run recomputes and overwrites cluster_id /
    cluster_changed for every row it covers -- same idempotency shape as
    compute_snapshots() (Track A) and compute_features() (Step 5).
    """
    query = db.query(EntitySnapshot)
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    all_rows = query.all()

    by_segment: dict[str, list[EntitySnapshot]] = defaultdict(list)
    for row in all_rows:
        by_segment[row.segment].append(row)

    errors: list[dict[str, Any]] = []
    segment_summaries: dict[str, Any] = {}

    for segment, rows in by_segment.items():
        observation_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            observation_counts[row.party_id] += row.transaction_count
        non_baseline_parties = {
            party_id: _tier_for_observation_count(count)
            for party_id, count in observation_counts.items()
            if _tier_for_observation_count(count) != "SEGMENT_BASELINE"
        }
        if non_baseline_parties:
            errors.append({
                "type": "tier_not_implemented",
                "segment": segment,
                "detail": (
                    f"{len(non_baseline_parties)} part(ies) now qualify for a "
                    "per-entity tier (ENTITY_BASELINE/ENTITY_FULL_MODEL) but only "
                    "SEGMENT_BASELINE clustering is implemented -- they were "
                    "still clustered at the segment level below."
                ),
                "party_ids": sorted(non_baseline_parties.keys()),
            })

        cluster_ids = _cluster_segment(rows)
        for row in rows:
            row.cluster_id = cluster_ids[row.id]

        weekly_rows = [row for row in rows if row.window_type == "WEEKLY"]
        changed_by_id = _compute_cluster_changed(weekly_rows, cluster_ids) if weekly_rows else {}
        for row in rows:
            row.cluster_changed = changed_by_id.get(row.id) if row.window_type == "WEEKLY" else None

        labels = list(cluster_ids.values())
        unique_labels = sorted(set(labels))
        segment_summaries[segment] = {
            "rows_clustered": len(rows),
            "distinct_parties": len(observation_counts),
            "cluster_sizes": {label: labels.count(label) for label in unique_labels},
            "n_clusters": sum(1 for label in unique_labels if label != -1),
            "noise_count": labels.count(-1),
        }

    db.commit()

    return {"segments": segment_summaries, "errors": errors}
