from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.clustering import _compute_cluster_changed, _tier_for_observation_count, cluster_and_score
from app.anomaly.models import EntitySnapshot


def _make_snapshot(db, **overrides):
    defaults = dict(
        party_id="MER-1",
        party_type="MERCHANT",
        tenant_bank_id="TESTBANK",
        segment="MERCHANT",
        window_type="WEEKLY",
        window_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        transaction_count=10,
        amount_total=100.0,
        amount_avg=50.0,
        amount_median=50.0,
        amount_std=5.0,
        unique_counterparties=2,
        new_counterparty_ratio=0.2,
        retry_ratio=0.0,
        near_threshold_ratio=0.0,
        avg_response_time_ms=300.0,
        timeout_ratio=0.0,
        format_reject_ratio=0.0,
        rails_used=["WIRE"],
        account_age_days=15.0,
        split="train",
    )
    defaults.update(overrides)
    snap = EntitySnapshot(**defaults)
    db.add(snap)
    db.commit()
    return snap


# "quiet" and "active" are two deliberately far-apart behavioral profiles --
# every feature differs by 1-2 orders of magnitude, so StandardScaler +
# HDBSCAN should cleanly separate a group of "quiet" rows from a group of
# "active" rows rather than lumping them into one cluster or scattering
# them as noise.
_QUIET = dict(
    amount_total=100.0, amount_avg=50.0, amount_median=50.0, amount_std=5.0,
    unique_counterparties=1, new_counterparty_ratio=0.1, retry_ratio=0.0,
    avg_response_time_ms=200.0, format_reject_ratio=0.0, account_age_days=10.0,
    transaction_count=2,
)
_ACTIVE = dict(
    amount_total=1_000_000.0, amount_avg=50_000.0, amount_median=48_000.0, amount_std=9_000.0,
    unique_counterparties=10, new_counterparty_ratio=0.9, retry_ratio=0.5,
    avg_response_time_ms=4_000.0, format_reject_ratio=0.4, account_age_days=50.0,
    transaction_count=20,
)


def test_tier_for_observation_count_boundaries():
    assert _tier_for_observation_count(0) == "SEGMENT_BASELINE"
    assert _tier_for_observation_count(49) == "SEGMENT_BASELINE"
    assert _tier_for_observation_count(50) == "ENTITY_BASELINE"
    assert _tier_for_observation_count(200) == "ENTITY_BASELINE"
    assert _tier_for_observation_count(201) == "ENTITY_FULL_MODEL"


def test_two_well_separated_groups_get_different_cluster_ids(db_session):
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-QUIET-{i}", window_type="TO_DATE", window_start=None, **_QUIET)
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-ACTIVE-{i}", window_type="TO_DATE", window_start=None, **_ACTIVE)

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    assert result["errors"] == []
    quiet_rows = db_session.query(EntitySnapshot).filter(EntitySnapshot.party_id.like("MER-QUIET-%")).all()
    active_rows = db_session.query(EntitySnapshot).filter(EntitySnapshot.party_id.like("MER-ACTIVE-%")).all()

    quiet_clusters = {r.cluster_id for r in quiet_rows}
    active_clusters = {r.cluster_id for r in active_rows}
    assert len(quiet_clusters) == 1, f"quiet group split across clusters: {quiet_clusters}"
    assert len(active_clusters) == 1, f"active group split across clusters: {active_clusters}"
    assert quiet_clusters != active_clusters
    assert -1 not in quiet_clusters | active_clusters, "well-separated groups of 6 should not be noise"


def test_single_row_segment_gets_noise_cluster_without_crashing(db_session):
    _make_snapshot(db_session, party_id="MER-LONELY", window_type="TO_DATE", window_start=None)

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    assert result["errors"] == []
    row = db_session.query(EntitySnapshot).filter_by(party_id="MER-LONELY").one()
    assert row.cluster_id == -1


def test_cluster_changed_tracks_transition_across_a_merchants_weekly_rows(db_session):
    # 5 filler merchants seed a real "quiet" cluster, 5 more seed a real
    # "active" cluster -- each comfortably clears HDBSCAN's min_cluster_size.
    for i in range(5):
        _make_snapshot(db_session, party_id=f"MER-QFILL-{i}", window_type="TO_DATE", window_start=None, **_QUIET)
    for i in range(5):
        _make_snapshot(db_session, party_id=f"MER-AFILL-{i}", window_type="TO_DATE", window_start=None, **_ACTIVE)

    # Target merchant: quiet in week 1 and 2, then switches to the active
    # profile in week 3.
    _make_snapshot(
        db_session, party_id="MER-TARGET",
        window_start=datetime(2026, 5, 4, tzinfo=timezone.utc), window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        **_QUIET,
    )
    _make_snapshot(
        db_session, party_id="MER-TARGET",
        window_start=datetime(2026, 5, 11, tzinfo=timezone.utc), window_end=datetime(2026, 5, 18, tzinfo=timezone.utc),
        **_QUIET,
    )
    _make_snapshot(
        db_session, party_id="MER-TARGET",
        window_start=datetime(2026, 5, 18, tzinfo=timezone.utc), window_end=datetime(2026, 5, 25, tzinfo=timezone.utc),
        **_ACTIVE,
    )

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    assert result["errors"] == []
    weeks = (
        db_session.query(EntitySnapshot)
        .filter_by(party_id="MER-TARGET")
        .order_by(EntitySnapshot.window_start)
        .all()
    )
    assert len(weeks) == 3
    week1, week2, week3 = weeks

    assert week1.cluster_changed is None  # no prior snapshot to compare to
    assert week1.cluster_id == week2.cluster_id
    assert week2.cluster_changed is False  # same cluster as week1
    assert week3.cluster_id != week2.cluster_id
    assert week3.cluster_changed is True  # switched clusters


def test_individuals_always_get_null_cluster_changed(db_session):
    for i in range(6):
        _make_snapshot(
            db_session, party_id=f"IND-{i}", party_type="INDIVIDUAL", segment="INDIVIDUAL",
            window_type="TO_DATE", window_start=None, **(_QUIET if i % 2 == 0 else _ACTIVE),
        )

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    assert result["errors"] == []
    rows = db_session.query(EntitySnapshot).filter_by(segment="INDIVIDUAL").all()
    assert len(rows) == 6
    assert all(r.cluster_changed is None for r in rows)
    assert all(r.cluster_id is not None for r in rows)


def test_recompute_replaces_not_duplicates(db_session):
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-{i}", window_type="TO_DATE", window_start=None, **_QUIET)

    cluster_and_score(db_session, tenant_bank_id="TESTBANK")
    cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    assert db_session.query(EntitySnapshot).filter_by(tenant_bank_id="TESTBANK").count() == 6


def test_null_features_are_imputed_without_crashing(db_session):
    for i in range(5):
        _make_snapshot(db_session, party_id=f"MER-{i}", window_type="TO_DATE", window_start=None, **_QUIET)
    # A row missing some feature values -- avg_response_time_ms/timeout_ratio
    # can genuinely be null (e.g. a rail that never reports response
    # timing), same as real Meridian data shows.
    _make_snapshot(
        db_session, party_id="MER-NULLS", window_type="TO_DATE", window_start=None,
        **{**_QUIET, "avg_response_time_ms": None, "unique_counterparties": None, "new_counterparty_ratio": None},
    )

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    assert result["errors"] == []
    row = db_session.query(EntitySnapshot).filter_by(party_id="MER-NULLS").one()
    assert row.cluster_id is not None


def test_party_exceeding_baseline_tier_is_flagged_but_still_clustered(db_session):
    for i in range(5):
        _make_snapshot(db_session, party_id=f"MER-{i}", window_type="TO_DATE", window_start=None, **_QUIET)
    # 60 total transactions on one TO_DATE row -- crosses the 50-observation
    # SEGMENT_BASELINE -> ENTITY_BASELINE line from Section 9.
    _make_snapshot(
        db_session, party_id="MER-HIVOLUME", window_type="TO_DATE", window_start=None,
        transaction_count=60, **{k: v for k, v in _QUIET.items() if k != "transaction_count"},
    )

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK")

    tier_warnings = [e for e in result["errors"] if e["type"] == "tier_not_implemented"]
    assert len(tier_warnings) == 1
    assert "MER-HIVOLUME" in tier_warnings[0]["party_ids"]

    row = db_session.query(EntitySnapshot).filter_by(party_id="MER-HIVOLUME").one()
    assert row.cluster_id is not None  # still clustered at segment level, not skipped


def test_compute_cluster_changed_unit():
    rows = [
        EntitySnapshot(id=1, party_id="A", window_start=datetime(2026, 1, 5, tzinfo=timezone.utc)),
        EntitySnapshot(id=2, party_id="A", window_start=datetime(2026, 1, 12, tzinfo=timezone.utc)),
        EntitySnapshot(id=3, party_id="A", window_start=datetime(2026, 1, 19, tzinfo=timezone.utc)),
    ]
    cluster_ids = {1: 0, 2: 0, 3: 1}

    result = _compute_cluster_changed(rows, cluster_ids)

    assert result[1] is None
    assert result[2] is False
    assert result[3] is True


def test_two_tenants_are_never_pooled_together(db_session):
    # Same segment name, same party_id convention, deliberately DIFFERENT
    # tenants -- if grouping ever pooled by segment alone (not
    # (tenant, segment)), these two tenants' rows would get clustered
    # together and each tenant's row count below would be wrong.
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-A{i}", tenant_bank_id="TENANT_A", window_type="TO_DATE", window_start=None, **_QUIET)
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-B{i}", tenant_bank_id="TENANT_B", window_type="TO_DATE", window_start=None, **_QUIET)

    result = cluster_and_score(db_session)  # no tenant_bank_id -- both tenants in one call

    assert "TENANT_A/MERCHANT" in result["segments"]
    assert "TENANT_B/MERCHANT" in result["segments"]
    assert result["segments"]["TENANT_A/MERCHANT"]["rows_clustered"] == 6
    assert result["segments"]["TENANT_B/MERCHANT"]["rows_clustered"] == 6

    a_rows = db_session.query(EntitySnapshot).filter_by(tenant_bank_id="TENANT_A").all()
    b_rows = db_session.query(EntitySnapshot).filter_by(tenant_bank_id="TENANT_B").all()
    assert all(r.cluster_id is not None for r in a_rows + b_rows)


def test_include_structuring_with_pca_still_separates_distinct_groups(db_session):
    # Same two-group separability test as above, but with
    # include_structuring=True (adds near_threshold_ratio + PCA) -- proves
    # PCA compression is what makes the 12th dimension viable, rather than
    # collapsing everything to noise the way plain inclusion did.
    quiet_with_structuring = {**_QUIET, "near_threshold_ratio": 0.05}
    active_with_structuring = {**_ACTIVE, "near_threshold_ratio": 0.95}
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-QUIET-{i}", window_type="TO_DATE", window_start=None, **quiet_with_structuring)
    for i in range(6):
        _make_snapshot(db_session, party_id=f"MER-ACTIVE-{i}", window_type="TO_DATE", window_start=None, **active_with_structuring)

    result = cluster_and_score(db_session, tenant_bank_id="TESTBANK", include_structuring=True)

    assert result["errors"] == []
    quiet_rows = db_session.query(EntitySnapshot).filter(EntitySnapshot.party_id.like("MER-QUIET-%")).all()
    active_rows = db_session.query(EntitySnapshot).filter(EntitySnapshot.party_id.like("MER-ACTIVE-%")).all()
    quiet_clusters = {r.cluster_id for r in quiet_rows}
    active_clusters = {r.cluster_id for r in active_rows}
    assert len(quiet_clusters) == 1
    assert len(active_clusters) == 1
    assert quiet_clusters != active_clusters
