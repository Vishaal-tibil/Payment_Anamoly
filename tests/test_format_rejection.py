from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.anomaly.models import EntitySnapshot
from app.models import CanonicalEvent
from app.operations.format_rejection import list_format_rejections, score_format_rejection_drift
from app.operations.models import OperationalIssue


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="ACH",
        transaction_id="TXN-1",
        amount=500.0,
        format_validation_status="PASSED",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_list_format_rejections_lists_only_failed_rows(db_session):
    _make_event(db_session, transaction_id="TXN-PASS", format_validation_status="PASSED")
    _make_event(db_session, transaction_id="TXN-FAIL", format_validation_status="FAILED",
                format_validation_errors={"rejection_code": "E101"})

    result = list_format_rejections(db_session)

    assert result["rejections_listed"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="FORMAT_REJECTION").one()
    assert issue.reference_id == "TXN-FAIL"
    assert issue.details == {"format_validation_errors": {"rejection_code": "E101"}}
    assert issue.severity_score is None


def test_list_format_rejections_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", format_validation_status="FAILED")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", format_validation_status="FAILED")

    result = list_format_rejections(db_session, tenant_bank_id="KEYBANK")

    assert result["rejections_listed"] == 1
    assert db_session.query(OperationalIssue).filter_by(tenant_bank_id="KEYBANK").count() == 1


def test_list_format_rejections_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-FAIL", format_validation_status="FAILED")

    list_format_rejections(db_session)
    list_format_rejections(db_session)

    assert db_session.query(OperationalIssue).filter_by(issue_type="FORMAT_REJECTION").count() == 1


def _week(db, party_id, week_index, format_reject_ratio, **overrides):
    start = datetime(2026, 5, 4, tzinfo=timezone.utc) + timedelta(weeks=week_index)  # 2026-05-04 is a Monday
    defaults = dict(
        party_id=party_id,
        party_type="MERCHANT",
        tenant_bank_id="KEYBANK",
        segment="MERCHANT",
        window_type="WEEKLY",
        window_start=start,
        window_end=start + timedelta(days=7),
        transaction_count=5,
        format_reject_ratio=format_reject_ratio,
        split="train",
    )
    defaults.update(overrides)
    row = EntitySnapshot(**defaults)
    db.add(row)
    db.commit()
    return row


def test_reject_rate_spike_above_threshold_is_flagged(db_session):
    for i in range(4):
        _week(db_session, "MER-1", i, format_reject_ratio=0.0)
    _week(db_session, "MER-1", 4, format_reject_ratio=0.5)  # sudden spike off an all-zero baseline

    result = score_format_rejection_drift(db_session)

    assert result["spikes_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="FORMAT_REJECTION_SPIKE").one()
    assert issue.reference_id == "MER-1"
    assert issue.severity_score == 100.0
    assert issue.details == {"format_reject_ratio": 0.5}


def test_stable_zero_reject_rate_is_not_flagged(db_session):
    for i in range(5):
        _week(db_session, "MER-STABLE", i, format_reject_ratio=0.0)

    result = score_format_rejection_drift(db_session)

    assert result["spikes_flagged"] == 0
    assert result["weeks_scored"] > 0  # scored, just not above the severity threshold


def test_first_two_weeks_have_no_baseline_and_are_not_flagged(db_session):
    _week(db_session, "MER-1", 0, format_reject_ratio=0.9)
    _week(db_session, "MER-1", 1, format_reject_ratio=0.9)

    result = score_format_rejection_drift(db_session)

    assert result["weeks_scored"] == 0
    assert result["spikes_flagged"] == 0


def test_individuals_are_never_scored(db_session):
    row = EntitySnapshot(
        party_id="IND-1", party_type="INDIVIDUAL", tenant_bank_id="KEYBANK", segment="INDIVIDUAL",
        window_type="TO_DATE", window_start=None,
        window_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        transaction_count=2, format_reject_ratio=1.0,
    )
    db_session.add(row)
    db_session.commit()

    result = score_format_rejection_drift(db_session)

    assert result["weeks_scored"] == 0
    assert db_session.query(OperationalIssue).count() == 0


def test_drift_tenant_isolation(db_session):
    for i in range(4):
        _week(db_session, "MER-A", i, format_reject_ratio=0.0, tenant_bank_id="KEYBANK")
    _week(db_session, "MER-A", 4, format_reject_ratio=0.5, tenant_bank_id="KEYBANK")
    for i in range(4):
        _week(db_session, "MER-B", i, format_reject_ratio=0.0, tenant_bank_id="MTB")
    _week(db_session, "MER-B", 4, format_reject_ratio=0.5, tenant_bank_id="MTB")

    result = score_format_rejection_drift(db_session, tenant_bank_id="KEYBANK")

    assert result["spikes_flagged"] == 1
    assert db_session.query(OperationalIssue).filter_by(tenant_bank_id="MTB").count() == 0


def test_drift_recompute_replaces_not_duplicates(db_session):
    for i in range(4):
        _week(db_session, "MER-1", i, format_reject_ratio=0.0)
    _week(db_session, "MER-1", 4, format_reject_ratio=0.5)

    score_format_rejection_drift(db_session)
    score_format_rejection_drift(db_session)

    assert db_session.query(OperationalIssue).filter_by(issue_type="FORMAT_REJECTION_SPIKE").count() == 1
