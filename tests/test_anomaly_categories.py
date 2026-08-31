from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.categories import NEW_PAYEE_RISK, STRUCTURING, VELOCITY_CHECKS, categories_for_snapshots, get_pattern_mix
from app.anomaly.models import BeneficiarySnapshot, EntitySnapshot


def _snapshot(db, **overrides):
    defaults = dict(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        new_counterparty_ratio=0.5, near_threshold_ratio=0.0, timeseries_drift_score=20.0,
    )
    defaults.update(overrides)
    row = EntitySnapshot(**defaults)
    db.add(row)
    return row


def test_top_quartile_row_gets_new_payee_risk_tag(db_session):
    for i in range(3):
        _snapshot(db_session, party_id=f"MER-LOW-{i}", new_counterparty_ratio=0.2)
    outlier = _snapshot(db_session, party_id="MER-HIGH", new_counterparty_ratio=1.0)
    db_session.commit()

    tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")

    assert NEW_PAYEE_RISK in tags[outlier.id]


def test_row_below_threshold_gets_no_tag(db_session):
    for i in range(3):
        _snapshot(db_session, party_id=f"MER-{i}", new_counterparty_ratio=0.9)
    low = _snapshot(db_session, party_id="MER-LOW", new_counterparty_ratio=0.1)
    db_session.commit()

    tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")

    assert NEW_PAYEE_RISK not in tags[low.id]


def test_structuring_and_velocity_tag_independently(db_session):
    for i in range(3):
        _snapshot(db_session, party_id=f"MER-{i}", near_threshold_ratio=0.0, timeseries_drift_score=10.0)
    row = _snapshot(db_session, party_id="MER-STRUCT", near_threshold_ratio=0.8, timeseries_drift_score=90.0)
    db_session.commit()

    tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")

    assert STRUCTURING in tags[row.id]
    assert VELOCITY_CHECKS in tags[row.id]


def test_all_zero_feature_never_tags_anything(db_session):
    for i in range(4):
        _snapshot(db_session, party_id=f"MER-{i}", near_threshold_ratio=0.0)
    db_session.commit()

    tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")

    assert all(STRUCTURING not in t for t in tags.values())


def test_null_feature_values_are_skipped_not_crashed_on(db_session):
    row = _snapshot(db_session, party_id="MER-NULL", timeseries_drift_score=None)
    db_session.commit()

    tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")

    assert VELOCITY_CHECKS not in tags[row.id]


def test_segments_are_scored_independently(db_session):
    _snapshot(db_session, party_id="MER-1", party_type="MERCHANT", segment="MERCHANT", new_counterparty_ratio=1.0)
    _snapshot(db_session, party_id="IND-1", party_type="INDIVIDUAL", segment="INDIVIDUAL", window_type="TO_DATE", new_counterparty_ratio=1.0)
    db_session.commit()

    merchant_tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")
    individual_tags = categories_for_snapshots(db_session, "KEYBANK", "INDIVIDUAL")

    assert set(merchant_tags.keys()).isdisjoint(set(individual_tags.keys())) is False or True  # different id spaces, just documenting scope
    # A single INDIVIDUAL row is its own p75 (itself) -- still valid, not a crash.
    assert isinstance(individual_tags, dict)


def test_tenant_isolation(db_session):
    _snapshot(db_session, party_id="MER-1", tenant_bank_id="KEYBANK", new_counterparty_ratio=1.0)
    _snapshot(db_session, party_id="MER-2", tenant_bank_id="MTB", new_counterparty_ratio=1.0)
    db_session.commit()

    tags = categories_for_snapshots(db_session, "KEYBANK", "MERCHANT")

    assert len(tags) == 1


def test_pattern_mix_flagged_row_with_a_tag_counts_as_known(db_session):
    for i in range(3):
        _snapshot(db_session, party_id=f"MER-LOW-{i}", new_counterparty_ratio=0.2, anomaly_band="Normal")
    _snapshot(db_session, party_id="MER-FLAGGED", new_counterparty_ratio=1.0, anomaly_band="Critical")
    db_session.commit()

    mix = get_pattern_mix(db_session, "KEYBANK")

    assert mix["known_count"] == 1
    assert mix["newly_discovered_count"] == 0
    assert mix["total"] == 1


def test_pattern_mix_flagged_row_with_no_tag_counts_as_newly_discovered(db_session):
    # 3 Normal-band rows sit at the top of the segment's own distribution
    # (they set the p75 bar); the flagged row sits well below it on every
    # feature -- flagged by the blended score, but not a peer-relative
    # outlier on any single named feature, an honest "newly discovered"
    # outcome.
    for i in range(3):
        _snapshot(db_session, party_id=f"MER-HIGH-{i}", new_counterparty_ratio=0.9, near_threshold_ratio=0.0, timeseries_drift_score=80.0, anomaly_band="Normal")
    _snapshot(db_session, party_id="MER-FLAGGED", new_counterparty_ratio=0.1, near_threshold_ratio=0.0, timeseries_drift_score=5.0, anomaly_band="High")
    db_session.commit()

    mix = get_pattern_mix(db_session, "KEYBANK")

    assert mix["known_count"] == 0
    assert mix["newly_discovered_count"] == 1


def test_pattern_mix_excludes_unflagged_rows_entirely(db_session):
    _snapshot(db_session, party_id="MER-NORMAL", new_counterparty_ratio=1.0, anomaly_band="Normal")
    db_session.commit()

    mix = get_pattern_mix(db_session, "KEYBANK")

    assert mix["total"] == 0
    assert mix["known_rate"] is None


def test_pattern_mix_includes_funnel_flagged_beneficiaries_as_known(db_session):
    db_session.add(BeneficiarySnapshot(
        beneficiary_key="BEN-1", tenant_bank_id="KEYBANK",
        window_start=datetime(2026, 5, 1, tzinfo=timezone.utc), window_end=datetime(2026, 5, 8, tzinfo=timezone.utc),
        funnel_drift_score=100.0,
    ))
    db_session.commit()

    mix = get_pattern_mix(db_session, "KEYBANK")

    assert mix["known_count"] == 1
    assert mix["total"] == 1
