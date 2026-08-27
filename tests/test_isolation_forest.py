from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

import pytest

from app.anomaly.isolation_forest import train_and_score
from app.anomaly.models import EntitySnapshot
from app.database import SessionLocal


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
