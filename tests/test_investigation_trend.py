from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.models import EntitySnapshot
from app.investigation.trend import category_weekly_trend
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak

_TENANT = "KEYBANK"
# Deliberately identical -- proves the trend never uses detected_at
# (a real ReconciliationBreak batch-run artifact where every row from
# one compute_cases() run shares this exact value).
_SHARED_DETECTED_AT = datetime(2026, 8, 27, 16, 10, 39, tzinfo=timezone.utc)


def _event(db, transaction_id: str, occurred_at: str, rail_type: str = "CHEQUE"):
    event = CanonicalEvent(tenant_bank_id=_TENANT, rail_type=rail_type, transaction_id=transaction_id, transaction_occurred_at=occurred_at)
    db.add(event)
    return event


def test_returns_none_below_two_weeks_of_history(db_session):
    _event(db_session, "TXN-1", "2026-06-01T10:00:00Z")
    db_session.add(ReconciliationBreak(
        tenant_bank_id=_TENANT, transaction_id="TXN-1", rail_type="CHEQUE",
        detection_type="CONFIRMED_BREAK", detected_at=_SHARED_DETECTED_AT,
    ))
    db_session.commit()

    assert category_weekly_trend(db_session, _TENANT, "CONFIRMED_BREAK", "CHEQUE") is None


def test_reconciliation_break_trend_uses_real_event_time_not_detected_at(db_session):
    # Week 1: 2026-06-01 (Monday); Week 2: 2026-06-15; Week 3: 2026-06-22 x3
    # (latest week clearly above the prior average -> "increasing").
    _event(db_session, "TXN-1", "2026-06-01T10:00:00Z")
    _event(db_session, "TXN-2", "2026-06-15T10:00:00Z")
    _event(db_session, "TXN-3", "2026-06-22T09:00:00Z")
    _event(db_session, "TXN-4", "2026-06-23T09:00:00Z")
    _event(db_session, "TXN-5", "2026-06-24T09:00:00Z")
    for txn_id in ("TXN-1", "TXN-2", "TXN-3", "TXN-4", "TXN-5"):
        db_session.add(ReconciliationBreak(
            tenant_bank_id=_TENANT, transaction_id=txn_id, rail_type="CHEQUE",
            detection_type="CONFIRMED_BREAK", detected_at=_SHARED_DETECTED_AT,  # identical on every row
        ))
    db_session.commit()

    trend = category_weekly_trend(db_session, _TENANT, "CONFIRMED_BREAK", "CHEQUE")

    assert trend is not None
    assert trend["weeks_observed"] == 3
    assert trend["counts_by_week"] == [
        {"week_start": "2026-06-01", "count": 1},
        {"week_start": "2026-06-15", "count": 1},
        {"week_start": "2026-06-22", "count": 3},
    ]
    assert trend["latest_week_count"] == 3
    assert trend["prior_weeks_average_count"] == 1.0
    assert trend["direction"] == "increasing"


def test_rail_filter_excludes_other_rails(db_session):
    _event(db_session, "TXN-1", "2026-06-01T10:00:00Z", rail_type="CHEQUE")
    _event(db_session, "TXN-2", "2026-06-15T10:00:00Z", rail_type="ACH")
    db_session.add(ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id="TXN-1", rail_type="CHEQUE", detection_type="CONFIRMED_BREAK", detected_at=_SHARED_DETECTED_AT))
    db_session.add(ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", detected_at=_SHARED_DETECTED_AT))
    db_session.commit()

    # Only the CHEQUE break counts -> 1 week of real history -> insufficient
    assert category_weekly_trend(db_session, _TENANT, "CONFIRMED_BREAK", "CHEQUE") is None


def test_fraud_trend_uses_entity_snapshot_window_end(db_session):
    for i, window_end in enumerate(["2026-06-07", "2026-06-14", "2026-06-21"]):
        db_session.add(EntitySnapshot(
            party_id=f"MER-{i}", party_type="MERCHANT", tenant_bank_id=_TENANT, segment="MERCHANT",
            window_type="WEEKLY", window_end=datetime.fromisoformat(window_end + "T00:00:00+00:00"),
            anomaly_band="Critical", rails_used=["WIRE"],
        ))
    db_session.commit()

    trend = category_weekly_trend(db_session, _TENANT, "FRAUD_CRITICAL", "WIRE")

    assert trend is not None
    assert trend["weeks_observed"] == 3
    assert trend["direction"] == "stable"  # 1 per week throughout


def test_party_level_operational_issue_uses_window_end_not_detected_at(db_session):
    for window_end in ["2026-06-01", "2026-06-08", "2026-06-15"]:
        db_session.add(OperationalIssue(
            issue_type="NETWORK_TIMEOUT_SPIKE", tenant_bank_id=_TENANT,
            reference_type="PARTY", reference_id="MER-1",
            window_end=datetime.fromisoformat(window_end + "T00:00:00+00:00"),
            detected_at=_SHARED_DETECTED_AT,
        ))
    db_session.commit()

    # NETWORK_TIMEOUT_SPIKE is party-level -- no rail to filter by.
    trend = category_weekly_trend(db_session, _TENANT, "NETWORK_TIMEOUT_SPIKE", None)

    assert trend is not None
    assert trend["weeks_observed"] == 3


def test_transaction_referenced_issue_rail_filter_excludes_other_rails(db_session):
    """Regression: _issue_anchor_dates (the DUPLICATE_PAYMENT/
    FORMAT_REJECTION/BATCH_NOT_SETTLED path) used to accept `rail` but
    never apply it -- every rail's chart for these categories rendered
    the identical all-rails count. CHEQUE gets 3 real weeks; ACH gets
    only 1 -- if the filter isn't applied, CHEQUE's own query would see
    all 4 events and return weeks_observed=4 instead of 3.
    """
    _event(db_session, "TXN-1", "2026-06-01T10:00:00Z", rail_type="CHEQUE")
    _event(db_session, "TXN-2", "2026-06-15T10:00:00Z", rail_type="CHEQUE")
    _event(db_session, "TXN-3", "2026-06-22T10:00:00Z", rail_type="CHEQUE")
    _event(db_session, "TXN-4", "2026-06-08T10:00:00Z", rail_type="ACH")
    for txn_id in ("TXN-1", "TXN-2", "TXN-3", "TXN-4"):
        db_session.add(OperationalIssue(
            issue_type="DUPLICATE_PAYMENT", tenant_bank_id=_TENANT,
            reference_type="TRANSACTION", reference_id=txn_id, detected_at=_SHARED_DETECTED_AT,
        ))
    db_session.commit()

    cheque_trend = category_weekly_trend(db_session, _TENANT, "DUPLICATE_PAYMENT", "CHEQUE")
    assert cheque_trend is not None
    assert cheque_trend["weeks_observed"] == 3
    assert sum(w["count"] for w in cheque_trend["counts_by_week"]) == 3  # not 4

    # Only 1 real ACH week -> insufficient history on its own.
    assert category_weekly_trend(db_session, _TENANT, "DUPLICATE_PAYMENT", "ACH") is None
