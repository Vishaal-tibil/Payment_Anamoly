"""Track C: time-series drift detection, per
unsupervised-anomaly-detection-knowledge.md Section 7.

Starts simple, per the doc's own guidance ("rolling mean/std, z-score, EWMA,
CUSUM before LSTM/Transformer"): for each entity (merchant or beneficiary),
each week, z-score that week's values against a rolling baseline built from
that SAME entity's own PRIOR weeks only -- never including the current week
in its own baseline, or drift would be artificially damped.

Two scoring functions sharing one core algorithm (_score_sequence):
- score_drift(): merchants' own spending/activity drift, from
  EntitySnapshot -> EntitySnapshot.timeseries_drift_score. Individuals
  currently have exactly one TO_DATE row each (median 2 transactions
  total) -- there's no sequence to speak of, so they're excluded rather
  than forced to a number that would just be noise.
- score_funnel_drift(): a BENEFICIARY's own history of how many distinct
  senders pay them -- from BeneficiarySnapshot ->
  BeneficiarySnapshot.funnel_drift_score. This is the Funnel Account
  signal: a beneficiary who normally has 1-2 senders a week suddenly
  drawing 6+ new senders is exactly the mule-account pattern, caught the
  same profile-based way as everything else here, not a global threshold.

Same input rule as Track A: reads only EntitySnapshot's/
BeneficiarySnapshot's raw-derived columns, never party_features or the
source's pre-computed risk flags.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from .models import BeneficiarySnapshot, EntitySnapshot

# Need at least this many prior weeks before a rolling mean/std means
# anything -- with 1 prior point, std is undefined (or 0, which blows up
# a z-score for any deviation at all).
_MIN_PRIOR_WEEKS = 2

# Merchant drift features. Picked because they're available on every rail
# (unlike e.g. avg_response_time_ms, which is null whenever a rail
# doesn't report timing) and behaviorally distinct: volume, frequency,
# and counterparty-novelty drift are different signals.
_DRIFT_FEATURES = ("amount_total", "transaction_count", "new_counterparty_ratio")

# Funnel drift features: how many distinct senders paid this beneficiary,
# and what fraction were new -- the two numbers that directly define the
# mule-account pattern from the knowledge doc's Section 10.
_FUNNEL_DRIFT_FEATURES = ("distinct_senders", "new_sender_ratio")

# Caps the combined |z| before rescaling to 0-100 -- z-scores are
# theoretically unbounded but in practice a mean-|z| beyond ~5 across
# these features already means "clearly unusual for this entity";
# capping keeps one outlier week from swamping the 0-100 scale.
_Z_CAP = 5.0


def _zscore(value: float, prior_values: list[float]) -> float | None:
    if len(prior_values) < _MIN_PRIOR_WEEKS:
        return None
    mean = statistics.mean(prior_values)
    std = statistics.stdev(prior_values)
    if std == 0:
        return 0.0 if value == mean else _Z_CAP
    return (value - mean) / std


def _rescale(mean_abs_z: float) -> float:
    return min(mean_abs_z, _Z_CAP) / _Z_CAP * 100


def _score_sequence(rows_sorted: list[Any], features: tuple[str, ...]) -> list[float | None]:
    """Core drift algorithm, shared by score_drift and score_funnel_drift:
    given one entity's own rows in chronological order, returns a
    same-length list of drift scores (0-100), or None where there isn't
    yet enough prior history to judge (first two weeks of any sequence).
    """
    scores: list[float | None] = []
    for i, current in enumerate(rows_sorted):
        prior = rows_sorted[:i]
        z_scores: list[float] = []
        for feature in features:
            current_value = getattr(current, feature)
            if current_value is None:
                continue
            prior_values = [v for v in (getattr(r, feature) for r in prior) if v is not None]
            z = _zscore(current_value, prior_values)
            if z is not None:
                z_scores.append(abs(z))

        if not z_scores:
            scores.append(None)
        else:
            mean_abs_z = sum(z_scores) / len(z_scores)
            scores.append(_rescale(mean_abs_z))
    return scores


def score_drift(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Scores every merchant WEEKLY snapshot's drift from that merchant's
    own prior-weeks baseline. Fully derived -- safe to re-run; each call
    recomputes and overwrites timeseries_drift_score on the rows it
    covers, same as Track A/B/D's compute functions.
    """
    query = db.query(EntitySnapshot).filter(
        EntitySnapshot.party_type == "MERCHANT",
        EntitySnapshot.window_type == "WEEKLY",
    )
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)

    by_party: dict[str, list[EntitySnapshot]] = defaultdict(list)
    for row in query.all():
        by_party[row.party_id].append(row)

    scored = 0
    skipped_insufficient_history = 0
    errors: list[dict[str, Any]] = []

    for party_id, rows in by_party.items():
        try:
            rows_sorted = sorted(rows, key=lambda r: r.window_start)
            scores = _score_sequence(rows_sorted, _DRIFT_FEATURES)
            for row, score in zip(rows_sorted, scores):
                row.timeseries_drift_score = score
                if score is None:
                    skipped_insufficient_history += 1
                else:
                    scored += 1
        except Exception as exc:
            errors.append({"type": "party_error", "party_id": party_id, "error": str(exc)})

    db.commit()

    return {
        "scored": scored,
        "skipped_insufficient_history": skipped_insufficient_history,
        "parties_processed": len(by_party),
        "errors": errors,
    }


def score_funnel_drift(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Scores every beneficiary's weekly snapshot's drift from that SAME
    beneficiary's own prior-weeks baseline of distinct_senders/
    new_sender_ratio. A beneficiary who normally draws 1-2 senders a week
    and suddenly draws 6+, mostly new, scores high here -- the classic
    funnel/mule-account signature, detected against that beneficiary's own
    history rather than a fixed "N senders" threshold.
    """
    query = db.query(BeneficiarySnapshot)
    if tenant_bank_id:
        query = query.filter(BeneficiarySnapshot.tenant_bank_id == tenant_bank_id)

    by_beneficiary: dict[str, list[BeneficiarySnapshot]] = defaultdict(list)
    for row in query.all():
        by_beneficiary[row.beneficiary_key].append(row)

    scored = 0
    skipped_insufficient_history = 0
    errors: list[dict[str, Any]] = []

    for beneficiary_key, rows in by_beneficiary.items():
        try:
            rows_sorted = sorted(rows, key=lambda r: r.window_start)
            scores = _score_sequence(rows_sorted, _FUNNEL_DRIFT_FEATURES)
            for row, score in zip(rows_sorted, scores):
                row.funnel_drift_score = score
                if score is None:
                    skipped_insufficient_history += 1
                else:
                    scored += 1
        except Exception as exc:
            errors.append({"type": "beneficiary_error", "beneficiary_key": beneficiary_key, "error": str(exc)})

    db.commit()

    return {
        "scored": scored,
        "skipped_insufficient_history": skipped_insufficient_history,
        "beneficiaries_processed": len(by_beneficiary),
        "errors": errors,
    }
