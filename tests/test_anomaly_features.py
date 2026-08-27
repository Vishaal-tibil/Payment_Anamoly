from __future__ import annotations

from app.anomaly.features import compute_snapshots
from app.anomaly.models import EntitySnapshot
from app.models import CanonicalEvent


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="CARD",
        transaction_id="TXN-1",
        merchant_id="MER-1",
        payee_name="Counterparty A",
        amount=100.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_low_volume_party_gets_single_to_date_snapshot(db_session):
    _make_event(db_session, transaction_id="TXN-1", amount=100.0, transaction_occurred_at="2026-05-01T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-2", amount=200.0, transaction_occurred_at="2026-05-08T10:00:00Z")

    result = compute_snapshots(db_session)

    assert result["errors"] == []
    snapshots = db_session.query(EntitySnapshot).filter_by(party_id="MER-1").all()
    assert len(snapshots) == 1
    assert snapshots[0].window_type == "TO_DATE"
    assert snapshots[0].transaction_count == 2
    assert snapshots[0].amount_total == 300.0
    assert snapshots[0].split == "train"


def test_high_volume_merchant_gets_weekly_windows(db_session):
    # 16 transactions across 3 distinct ISO (Monday-start) weeks -- above
    # the windowing threshold. 2026-05-11 is a Monday.
    dates = [
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15", "2026-05-16",
        "2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22",
        "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29",
    ]
    for i, d in enumerate(dates):
        _make_event(db_session, transaction_id=f"TXN-{i}", transaction_occurred_at=f"{d}T10:00:00Z", amount=50.0 + i)

    result = compute_snapshots(db_session)

    assert result["errors"] == []
    snapshots = db_session.query(EntitySnapshot).filter_by(party_id="MER-1").order_by(EntitySnapshot.window_start).all()
    assert all(s.window_type == "WEEKLY" for s in snapshots)
    assert len(snapshots) == 3  # 3 distinct ISO weeks
    assert sum(s.transaction_count for s in snapshots) == 16


def test_new_counterparty_ratio_tracks_chronological_first_sightings(db_session):
    _make_event(db_session, transaction_id="TXN-1", payee_name="Alpha", transaction_occurred_at="2026-05-01T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-2", payee_name="Alpha", transaction_occurred_at="2026-05-02T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-3", payee_name="Beta", transaction_occurred_at="2026-05-03T10:00:00Z")

    compute_snapshots(db_session)

    snapshot = db_session.query(EntitySnapshot).filter_by(party_id="MER-1").one()
    # Alpha (1st sighting), Alpha (repeat), Beta (1st sighting) -> 2 first-sightings / 3 = 0.667
    assert abs(snapshot.new_counterparty_ratio - (2 / 3)) < 1e-9


def test_snapshot_values_are_unaffected_by_the_flags_it_must_not_use(db_session):
    # Same underlying transactions, but one version has every leakage-risk
    # flag set to a "high risk" value and the other doesn't. The computed
    # snapshot must be byte-for-byte identical either way -- if it isn't,
    # something in features.py is reading a flag it shouldn't.
    common = dict(
        transaction_id="TXN-1", amount=100.0, payee_name="Alpha",
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    _make_event(db_session, tenant_bank_id="KEYBANK", merchant_id="MER-CLEAN", **common)
    _make_event(
        db_session, tenant_bank_id="MTB", merchant_id="MER-FLAGGED", **common,
        new_payee_risk_flag=True, funnel_account_flag=True, velocity_threshold_breached=True,
        structuring_flag=True, network_timeout_flag=True,
        fraud_risk_details={"velocity_score": 99, "distinct_originating_accounts_24h": 50},
    )

    compute_snapshots(db_session)

    clean = db_session.query(EntitySnapshot).filter_by(party_id="MER-CLEAN").one()
    flagged = db_session.query(EntitySnapshot).filter_by(party_id="MER-FLAGGED").one()
    for field in (
        "transaction_count", "amount_total", "amount_avg", "amount_median", "amount_std",
        "unique_counterparties", "new_counterparty_ratio", "retry_ratio",
        "avg_response_time_ms", "timeout_ratio", "format_reject_ratio", "near_threshold_ratio",
    ):
        assert getattr(clean, field) == getattr(flagged, field), f"{field} differs -- possible leakage"


def test_rows_without_a_timestamp_are_excluded_and_counted(db_session):
    _make_event(db_session, transaction_id="TXN-1", transaction_occurred_at=None)

    result = compute_snapshots(db_session)

    assert result["skipped_no_timestamp"] == 1
    assert db_session.query(EntitySnapshot).count() == 0


def test_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-1")
    compute_snapshots(db_session)
    compute_snapshots(db_session)

    assert db_session.query(EntitySnapshot).filter_by(party_id="MER-1").count() == 1


def test_near_threshold_ratio_flags_amounts_just_under_10k(db_session):
    _make_event(db_session, transaction_id="TXN-1", amount=9500.0, transaction_occurred_at="2026-05-01T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-2", amount=9999.99, transaction_occurred_at="2026-05-02T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-3", amount=100.0, transaction_occurred_at="2026-05-03T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-4", amount=10000.0, transaction_occurred_at="2026-05-04T10:00:00Z")  # at threshold, not under it

    compute_snapshots(db_session)

    snapshot = db_session.query(EntitySnapshot).filter_by(party_id="MER-1").one()
    assert snapshot.near_threshold_ratio == 0.5  # TXN-1, TXN-2 out of 4


def test_timeout_and_retry_ratios_from_raw_fields(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", is_retry=True,
        network_response_details={"network_response_control.response_time_ms": 9000, "network_response_control.expected_response_sla_ms": 8000},
    )
    _make_event(
        db_session, transaction_id="TXN-2", is_retry=False,
        network_response_details={"network_response_control.response_time_ms": 100, "network_response_control.expected_response_sla_ms": 8000},
    )

    compute_snapshots(db_session)

    snapshot = db_session.query(EntitySnapshot).filter_by(party_id="MER-1").one()
    assert snapshot.retry_ratio == 0.5
    assert snapshot.timeout_ratio == 0.5  # only TXN-1 exceeded its SLA
    assert snapshot.avg_response_time_ms == (9000 + 100) / 2
