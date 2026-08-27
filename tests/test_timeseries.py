from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.anomaly.models import EntitySnapshot
from app.anomaly.timeseries import score_drift


def _week(db, party_id, week_index, amount_total, transaction_count=5, new_counterparty_ratio=0.2, **overrides):
    start = datetime(2026, 5, 4, tzinfo=timezone.utc) + timedelta(weeks=week_index)  # 2026-05-04 is a Monday
    defaults = dict(
        party_id=party_id,
        party_type="MERCHANT",
        tenant_bank_id="KEYBANK",
        segment="MERCHANT",
        window_type="WEEKLY",
        window_start=start,
        window_end=start + timedelta(days=7),
        transaction_count=transaction_count,
        amount_total=amount_total,
        new_counterparty_ratio=new_counterparty_ratio,
        split="train",
    )
    defaults.update(overrides)
    row = EntitySnapshot(**defaults)
    db.add(row)
    db.commit()
    return row


def test_first_two_weeks_have_no_baseline_yet(db_session):
    _week(db_session, "MER-1", 0, amount_total=1000.0)
    _week(db_session, "MER-1", 1, amount_total=1050.0)

    result = score_drift(db_session)

    rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-1").order_by(EntitySnapshot.window_start).all()
    assert rows[0].timeseries_drift_score is None  # 0 prior weeks
    assert rows[1].timeseries_drift_score is None  # 1 prior week -- still not enough
    assert result["skipped_insufficient_history"] == 2


def test_stable_merchant_gets_low_drift_once_baseline_exists(db_session):
    for i in range(5):
        _week(db_session, "MER-STABLE", i, amount_total=1000.0, transaction_count=5, new_counterparty_ratio=0.2)

    score_drift(db_session)

    rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-STABLE").order_by(EntitySnapshot.window_start).all()
    # weeks 2-4 have >=2 prior weeks; identical values every week -> std=0, value==mean -> z=0
    for row in rows[2:]:
        assert row.timeseries_drift_score == 0.0


def test_spike_week_scores_higher_than_stable_weeks(db_session):
    for i in range(4):
        _week(db_session, "MER-SPIKE", i, amount_total=1000.0, transaction_count=5, new_counterparty_ratio=0.2)
    # All three tracked features spike together in week 5 -- a genuine
    # multi-signal anomaly, not just one metric moving.
    _week(db_session, "MER-SPIKE", 4, amount_total=50000.0, transaction_count=40, new_counterparty_ratio=0.9)

    score_drift(db_session)

    rows = db_session.query(EntitySnapshot).filter_by(party_id="MER-SPIKE").order_by(EntitySnapshot.window_start).all()
    spike_week = rows[4]
    assert spike_week.timeseries_drift_score == 100.0  # every tracked feature capped at _Z_CAP -> mean is also the cap
    assert rows[2].timeseries_drift_score < spike_week.timeseries_drift_score


def test_individuals_are_never_scored(db_session):
    row = EntitySnapshot(
        party_id="IND-1", party_type="INDIVIDUAL", tenant_bank_id="KEYBANK", segment="INDIVIDUAL",
        window_type="TO_DATE", window_start=None,
        window_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        transaction_count=2, amount_total=500.0,
    )
    db_session.add(row)
    db_session.commit()

    result = score_drift(db_session)

    db_session.refresh(row)
    assert row.timeseries_drift_score is None
    assert result["parties_processed"] == 0  # individuals never enter the query at all


def test_rerun_is_idempotent(db_session):
    for i in range(4):
        _week(db_session, "MER-1", i, amount_total=1000.0 + i * 10)

    first = score_drift(db_session)
    second = score_drift(db_session)

    assert first["scored"] == second["scored"]
    rows_after_first = [
        r.timeseries_drift_score
        for r in db_session.query(EntitySnapshot).filter_by(party_id="MER-1").order_by(EntitySnapshot.window_start)
    ]
    score_drift(db_session)
    rows_after_third = [
        r.timeseries_drift_score
        for r in db_session.query(EntitySnapshot).filter_by(party_id="MER-1").order_by(EntitySnapshot.window_start)
    ]
    assert rows_after_first == rows_after_third


def test_tenant_isolation(db_session):
    _week(db_session, "MER-A", 0, amount_total=1000.0, tenant_bank_id="KEYBANK")
    _week(db_session, "MER-A", 1, amount_total=1000.0, tenant_bank_id="KEYBANK")
    _week(db_session, "MER-A", 2, amount_total=1000.0, tenant_bank_id="KEYBANK")
    _week(db_session, "MER-B", 0, amount_total=1000.0, tenant_bank_id="MTB")
    _week(db_session, "MER-B", 1, amount_total=1000.0, tenant_bank_id="MTB")
    _week(db_session, "MER-B", 2, amount_total=1000.0, tenant_bank_id="MTB")

    result = score_drift(db_session, tenant_bank_id="KEYBANK")

    assert result["parties_processed"] == 1  # only MER-A
