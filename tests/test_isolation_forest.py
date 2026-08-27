from __future__ import annotations

import random
import statistics
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.anomaly.isolation_forest import compute_final_score, train_and_score
from app.anomaly.models import EntitySnapshot
from app.database import SessionLocal
from app.main import app


def _make_snapshot(db, **overrides):
    defaults = dict(
        party_id="MER-1",
        party_type="MERCHANT",
        tenant_bank_id="KEYBANK",
        segment="MERCHANT",
        window_type="WEEKLY",
        window_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        transaction_count=10,
        amount_total=1000.0,
        amount_avg=100.0,
        amount_median=100.0,
        amount_std=10.0,
        unique_counterparties=5,
        new_counterparty_ratio=0.2,
        retry_ratio=0.0,
        avg_response_time_ms=200.0,
        timeout_ratio=0.0,
        format_reject_ratio=0.0,
        account_age_days=90.0,
        split="train",
    )
    defaults.update(overrides)
    snapshot = EntitySnapshot(**defaults)
    db.add(snapshot)
    db.commit()
    return snapshot


def test_obvious_outlier_scores_higher_than_normal_rows(db_session):
    # 9 "normal" merchants clustered around the same behavior, plus one
    # obvious outlier at ~50x the amount of everything else -- proves the
    # model actually separates the two, not just that it runs.
    for i in range(9):
        _make_snapshot(
            db_session, party_id=f"MER-NORMAL-{i}",
            amount_total=1000.0 + i * 10, amount_avg=100.0 + i, amount_median=100.0 + i,
        )
    _make_snapshot(
        db_session, party_id="MER-OUTLIER",
        amount_total=50000.0, amount_avg=5000.0, amount_median=5000.0, amount_std=800.0,
    )

    train_and_score(db_session)

    rows = {r.party_id: r.isolation_forest_score for r in db_session.query(EntitySnapshot).all()}
    outlier_score = rows.pop("MER-OUTLIER")
    normal_scores = list(rows.values())

    assert outlier_score is not None
    assert all(s is not None for s in normal_scores)
    assert outlier_score > max(normal_scores)


def test_scores_are_rescaled_into_0_to_100(db_session):
    for i in range(5):
        _make_snapshot(db_session, party_id=f"MER-{i}", amount_total=1000.0 + i * 500)

    train_and_score(db_session)

    scores = [r.isolation_forest_score for r in db_session.query(EntitySnapshot).all()]
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_train_and_score_only_writes_its_own_column(db_session):
    _make_snapshot(db_session, party_id="MER-1", cluster_id=None, timeseries_drift_score=None)
    _make_snapshot(db_session, party_id="MER-2")

    train_and_score(db_session)

    for row in db_session.query(EntitySnapshot).all():
        assert row.cluster_id is None
        assert row.cluster_changed is None
        assert row.timeseries_drift_score is None
        assert row.final_anomaly_score is None
        assert row.anomaly_band is None


def test_null_features_are_imputed_not_left_to_crash(db_session):
    # avg_response_time_ms/timeout_ratio null for one row, same as real
    # individuals where a rail never reported response timing.
    _make_snapshot(db_session, party_id="MER-1", avg_response_time_ms=None, timeout_ratio=None)
    _make_snapshot(db_session, party_id="MER-2", avg_response_time_ms=150.0, timeout_ratio=0.1)
    _make_snapshot(db_session, party_id="MER-3", avg_response_time_ms=250.0, timeout_ratio=0.0)

    result = train_and_score(db_session)

    assert result["MERCHANT"]["rows_scored"] == 3
    scores = [r.isolation_forest_score for r in db_session.query(EntitySnapshot).all()]
    assert all(s is not None for s in scores)


def test_segments_are_modeled_independently(db_session):
    for i in range(5):
        _make_snapshot(db_session, party_id=f"MER-{i}", segment="MERCHANT", party_type="MERCHANT")
    for i in range(5):
        _make_snapshot(
            db_session, party_id=f"IND-{i}", segment="INDIVIDUAL", party_type="INDIVIDUAL",
            window_type="TO_DATE", window_start=None,
        )

    result = train_and_score(db_session)

    assert result["MERCHANT"]["rows_scored"] == 5
    assert result["INDIVIDUAL"]["rows_scored"] == 5


def test_entity_above_segment_baseline_threshold_is_not_silently_scored(db_session):
    # Section 9: >= 50 total observations for one party should route to a
    # per-entity model, which isn't built yet -- must fail loudly, not
    # silently fall back to the pooled segment baseline as if nothing
    # changed.
    for week in range(6):
        _make_snapshot(
            db_session, party_id="MER-HIGH-VOLUME",
            window_start=datetime(2026, 5, 4, tzinfo=timezone.utc) + timedelta(weeks=week),
            transaction_count=10,
        )
    _make_snapshot(db_session, party_id="MER-LOW-VOLUME", transaction_count=5)

    with pytest.raises(NotImplementedError):
        train_and_score(db_session)


def test_synthetic_injection_detection_rate(db_session):
    """Not "model accuracy" -- that requires real fraud labels, which don't
    exist anywhere in this pipeline (unsupervised by design). This is a
    narrower, honest question: if we inject N *known* obvious outliers into
    a realistic-looking normal population, what fraction does the model
    actually rank near the top? Answers "does the mechanism work at all,"
    not "how well would it work on real fraud" -- real fraud won't
    necessarily look like these injections.

    Recall@K here means: of the K rows actually injected as anomalies, how
    many appear in the model's own top-K highest isolation_forest_score
    rows. Threshold-free by construction (K = number of true positives, so
    precision@K == recall@K) -- avoids picking an arbitrary score cutoff.
    """
    rng = random.Random(7)
    n_normal = 90
    n_injected = 10

    normal_ids = [f"MER-NORMAL-{i}" for i in range(n_normal)]
    for party_id in normal_ids:
        txn_count = rng.randint(15, 40)
        amount_avg = rng.gauss(3000, 400)
        _make_snapshot(
            db_session, party_id=party_id,
            transaction_count=txn_count,
            amount_total=amount_avg * txn_count,
            amount_avg=amount_avg,
            amount_median=amount_avg * rng.uniform(0.9, 1.1),
            amount_std=amount_avg * rng.uniform(0.1, 0.3),
            unique_counterparties=rng.randint(3, 10),
            new_counterparty_ratio=rng.uniform(0.1, 0.4),
            retry_ratio=rng.uniform(0.0, 0.05),
            avg_response_time_ms=rng.gauss(300, 50),
            timeout_ratio=rng.uniform(0.0, 0.03),
            format_reject_ratio=rng.uniform(0.0, 0.02),
            account_age_days=rng.uniform(60, 400),
        )

    injected_ids = [f"MER-INJECTED-{i}" for i in range(n_injected)]
    for party_id in injected_ids:
        # 20-50x the normal amount scale, plus every counterparty new and
        # much higher retries -- an obvious, multi-feature outlier, not
        # just one axis nudged slightly.
        scale = rng.uniform(20, 50)
        amount_avg = 3000 * scale
        txn_count = rng.randint(15, 40)
        _make_snapshot(
            db_session, party_id=party_id,
            transaction_count=txn_count,
            amount_total=amount_avg * txn_count,
            amount_avg=amount_avg,
            amount_median=amount_avg * rng.uniform(0.9, 1.1),
            amount_std=amount_avg * rng.uniform(0.3, 0.6),
            unique_counterparties=rng.randint(3, 10),
            new_counterparty_ratio=1.0,
            retry_ratio=rng.uniform(0.3, 0.6),
            avg_response_time_ms=rng.gauss(300, 50),
            timeout_ratio=rng.uniform(0.0, 0.03),
            format_reject_ratio=rng.uniform(0.0, 0.02),
            account_age_days=rng.uniform(60, 400),
        )

    train_and_score(db_session)

    all_rows = db_session.query(EntitySnapshot).all()
    ranked = sorted(all_rows, key=lambda r: r.isolation_forest_score, reverse=True)

    def recall_at_k(k: int) -> float:
        top_k_ids = {r.party_id for r in ranked[:k]}
        caught = sum(1 for pid in injected_ids if pid in top_k_ids)
        return caught / n_injected

    recall_at_10 = recall_at_k(n_injected)
    recall_at_20 = recall_at_k(n_injected * 2)

    injected_scores = [r.isolation_forest_score for r in all_rows if r.party_id in injected_ids]
    normal_scores = [r.isolation_forest_score for r in all_rows if r.party_id in normal_ids]

    print(f"\nSynthetic injection check: {n_injected} injected / {n_normal} normal")
    print(f"  recall@{n_injected} (precision@K, K=true positives): {recall_at_10:.0%}")
    print(f"  recall@{n_injected * 2}: {recall_at_20:.0%}")
    print(f"  injected scores: min={min(injected_scores):.1f} median={statistics.median(injected_scores):.1f}")
    print(f"  normal scores:   min={min(normal_scores):.1f} median={statistics.median(normal_scores):.1f}")

    # Not a claim about real-world fraud detection -- just proves the
    # mechanism reliably surfaces obvious, multi-feature outliers near the
    # top of the ranking on a population it wasn't tuned to.
    assert recall_at_10 >= 0.7
    assert statistics.median(injected_scores) > statistics.median(normal_scores)


def test_train_endpoint_scores_real_meridian_data():
    """Non-destructive against the shared file-backed db -- unlike the
    ingestion endpoints, this only rescores existing EntitySnapshot rows
    by id, never inserts/deletes, so it's safe to hit through the real
    TestClient(app) the same way test_api_integration.py does.
    """
    with TestClient(app) as client:
        resp = client.post("/anomaly/isolation-forest/train", json={"tenant_bank_id": "MERIDIAN_TRUST_BANK"})
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert set(body.keys()) == {"MERCHANT", "INDIVIDUAL"}
        for segment_result in body.values():
            assert segment_result["rows_scored"] > 0
            assert 0.0 <= segment_result["score_min"]
            assert segment_result["score_max"] <= 100.0

    db = SessionLocal()
    try:
        rows = db.query(EntitySnapshot).filter_by(tenant_bank_id="MERIDIAN_TRUST_BANK").all()
        assert all(r.isolation_forest_score is not None for r in rows)
    finally:
        db.close()


def test_compute_final_score_weights_the_three_signals(db_session):
    # isolation_forest_score set directly (bypassing train_and_score) so
    # the weighted-sum arithmetic can be checked exactly, independent of
    # what IsolationForest itself would produce.
    row = _make_snapshot(db_session, party_id="MER-1", isolation_forest_score=80.0, cluster_changed=True, timeseries_drift_score=60.0)

    compute_final_score(db_session, tenant_bank_id="KEYBANK")

    db_session.refresh(row)
    # Literal README formula: IF and timeseries are 0-100, but
    # clustering_signal is the literal 0/1 the README specifies -- NOT
    # rescaled to 0/100 here, since that would be inventing a design
    # decision rather than following the spec. Flagged separately: this
    # means max possible final_score is 0.40*100 + 0.25*1 + 0.35*100 =
    # 75.25, so "Critical" (80-100) is unreachable as currently specified.
    expected = 0.40 * 80.0 + 0.25 * 1.0 + 0.35 * 60.0  # = 53.25
    assert row.final_anomaly_score == pytest.approx(expected)
    assert row.anomaly_band == "Low-Medium"


def test_null_clustering_and_timeseries_treated_as_zero_contribution(db_session):
    # Real data today: every row's cluster_changed/timeseries_drift_score
    # is None (Track C/D not merged). This must not crash or skip the
    # row -- it should fall back to isolation_forest_score alone.
    row = _make_snapshot(db_session, party_id="MER-1", isolation_forest_score=50.0, cluster_changed=None, timeseries_drift_score=None)

    compute_final_score(db_session)

    db_session.refresh(row)
    assert row.final_anomaly_score == pytest.approx(0.40 * 50.0)
    assert row.anomaly_band == "Normal"


def test_anomaly_band_cutoffs():
    from app.anomaly.isolation_forest import _anomaly_band

    assert _anomaly_band(0.0) == "Normal"
    assert _anomaly_band(29.9) == "Normal"
    assert _anomaly_band(30.0) == "Low-Medium"
    assert _anomaly_band(59.9) == "Low-Medium"
    assert _anomaly_band(60.0) == "High"
    assert _anomaly_band(79.9) == "High"
    assert _anomaly_band(80.0) == "Critical"
    assert _anomaly_band(100.0) == "Critical"


def test_compute_final_score_requires_isolation_forest_score_first(db_session):
    _make_snapshot(db_session, party_id="MER-1", isolation_forest_score=None)

    with pytest.raises(ValueError):
        compute_final_score(db_session)


def test_real_data_score_distribution_sanity_check():
    """Not a correctness assertion -- a real-data run against the
    committed Meridian dataset, printing the score distribution per
    segment so a reviewer can eyeball it (README's Track B step 7).
    """
    db = SessionLocal()
    try:
        result = train_and_score(db, tenant_bank_id="MERIDIAN_TRUST_BANK")
        for segment, stats in result.items():
            if stats["rows_scored"] == 0:
                continue
            print(f"\n{segment}: {stats['rows_scored']} rows scored across {stats['parties']} parties")
            print(f"  score min={stats['score_min']:.1f} median={stats['score_median']:.1f} max={stats['score_max']:.1f}")
            assert 0.0 <= stats["score_min"]
            assert stats["score_max"] <= 100.0
            assert stats["score_min"] <= stats["score_median"] <= stats["score_max"]
    finally:
        db.close()
