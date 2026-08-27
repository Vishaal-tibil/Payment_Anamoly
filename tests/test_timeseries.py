from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.models import EntitySnapshot
from app.anomaly.timeseries import score_drift


def _make_weekly_snapshot(db, **overrides):
    defaults = dict(
        party_id="MER-1",
        party_type="MERCHANT",
        tenant_bank_id="TESTBANK",
        segment="MERCHANT",
        window_type="WEEKLY",
        window_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        transaction_count=10,
        amount_total=1000.0,
        new_counterparty_ratio=0.2,
        split="train",
    )
    defaults.update(overrides)
    snap = EntitySnapshot(**defaults)
    db.add(snap)
    db.commit()
    return snap


def _weeks(start_week: int, count: int):
    # window_start values one week apart, starting from an arbitrary Monday.
    base = datetime(2026, 5, 4, tzinfo=timezone.utc)
    from datetime import timedelta
    return [base + timedelta(weeks=start_week + i) for i in range(count)]


def test_stable_merchant_scores_lower_than_a_spiking_one_in_the_same_run(db_session):
    # min-max rescaling is relative to the whole batch it's run over, so a
    # stable merchant tested in isolation would always show a 100.0
    # somewhere -- scoring it alongside a genuinely spiking merchant in the
    # same run is what actually shows the rescale is meaningful.
    for i, week_start in enumerate(_weeks(0, 6)):
        _make_weekly_snapshot(
            db_session, party_id="MER-STABLE", window_start=week_start,
            window_end=week_start, transaction_count=10, amount_total=1000.0 + i,  # tiny jitter only
            new_counterparty_ratio=0.2,
        )
    spike_weeks = _weeks(0, 6)
    for week_start in spike_weeks[:5]:
        _make_weekly_snapshot(
            db_session, party_id="MER-SPIKE-CONTROL", window_start=week_start, window_end=week_start,
            transaction_count=10, amount_total=1000.0, new_counterparty_ratio=0.2,
        )
    _make_weekly_snapshot(
        db_session, party_id="MER-SPIKE-CONTROL", window_start=spike_weeks[5], window_end=spike_weeks[5],
        transaction_count=200, amount_total=500_000.0, new_counterparty_ratio=0.9,
    )

    result = score_drift(db_session, tenant_bank_id="TESTBANK")

    assert result["merchant_weekly_rows"] == 12
    stable_rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-STABLE").all()
    spike_rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-SPIKE-CONTROL").all()
    stable_scored = [r.timeseries_drift_score for r in stable_rows if r.timeseries_drift_score is not None]
    spike_scored = [r.timeseries_drift_score for r in spike_rows if r.timeseries_drift_score is not None]
    assert max(spike_scored) == 100.0
    assert max(stable_scored) < max(spike_scored)


def test_sudden_spike_scores_high_drift(db_session):
    weeks = _weeks(0, 6)
    for week_start in weeks[:5]:
        _make_weekly_snapshot(
            db_session, party_id="MER-SPIKE", window_start=week_start, window_end=week_start,
            transaction_count=10, amount_total=1000.0, new_counterparty_ratio=0.2,
        )
    # Week 6: transaction_count and amount both spike far outside the
    # stable history established by weeks 1-5.
    _make_weekly_snapshot(
        db_session, party_id="MER-SPIKE", window_start=weeks[5], window_end=weeks[5],
        transaction_count=200, amount_total=500_000.0, new_counterparty_ratio=0.9,
    )

    score_drift(db_session, tenant_bank_id="TESTBANK")

    rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-SPIKE").order_by(EntitySnapshot.window_start).all()
    last_week = rows[-1]
    earlier_scored = [r.timeseries_drift_score for r in rows[2:5] if r.timeseries_drift_score is not None]
    assert last_week.timeseries_drift_score is not None
    assert all(last_week.timeseries_drift_score > s for s in earlier_scored)
    assert last_week.timeseries_drift_score == 100.0  # max of this segment's min-max rescale


def test_first_weeks_get_null_insufficient_history(db_session):
    weeks = _weeks(0, 3)
    for week_start in weeks:
        _make_weekly_snapshot(db_session, party_id="MER-NEW", window_start=week_start, window_end=week_start)

    score_drift(db_session, tenant_bank_id="TESTBANK")

    rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-NEW").order_by(EntitySnapshot.window_start).all()
    # _MIN_PRIOR_WEEKS = 2 -> the first 2 rows (0 and 1 prior weeks) can't be scored.
    assert rows[0].timeseries_drift_score is None
    assert rows[1].timeseries_drift_score is None


def test_individuals_get_null_drift_score(db_session):
    snap = EntitySnapshot(
        party_id="IND-1", party_type="INDIVIDUAL", tenant_bank_id="TESTBANK", segment="INDIVIDUAL",
        window_type="TO_DATE", window_start=None, window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        transaction_count=2, amount_total=500.0, split="train",
    )
    db_session.add(snap)
    db_session.commit()

    score_drift(db_session, tenant_bank_id="TESTBANK")

    row = db_session.query(EntitySnapshot).filter_by(party_id="IND-1").one()
    assert row.timeseries_drift_score is None


def test_recompute_replaces_not_duplicates(db_session):
    for week_start in _weeks(0, 4):
        _make_weekly_snapshot(db_session, window_start=week_start, window_end=week_start)

    score_drift(db_session, tenant_bank_id="TESTBANK")
    score_drift(db_session, tenant_bank_id="TESTBANK")

    assert db_session.query(EntitySnapshot).filter_by(tenant_bank_id="TESTBANK").count() == 4
