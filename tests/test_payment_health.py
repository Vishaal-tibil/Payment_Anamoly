from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.models import EntitySnapshot
from app.health.models import PaymentHealthScore
from app.health.scoring import compute_health_scores
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak


def _event(db, **overrides):
    defaults = dict(tenant_bank_id="KEYBANK", rail_type="ACH", status="SETTLED")
    defaults.update(overrides)
    defaults.setdefault("transaction_id", f"TXN-{overrides.get('transaction_id', id(overrides))}")
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


def test_perfect_data_scores_100_healthy(db_session):
    for i in range(5):
        _event(db_session, transaction_id=f"TXN-{i}", status="SETTLED", reconciliation_status="MATCHED")
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    row = db_session.get(PaymentHealthScore, "KEYBANK")

    assert row.health_score == 100.0
    assert row.health_band == "Healthy"
    assert row.settlement_component == 100.0
    assert row.anomaly_component == 100.0
    assert row.operational_component == 100.0
    assert row.reconciliation_component == 100.0


def test_settlement_penalty_reflects_real_unsettled_rate(db_session):
    _event(db_session, transaction_id="TXN-1", status="SETTLED")
    _event(db_session, transaction_id="TXN-2", status="PENDING")
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    row = db_session.get(PaymentHealthScore, "KEYBANK")

    assert row.settlement_component == 50.0  # 1/2 settled -> 50% penalty -> 50 component
    assert row.health_score < 100.0


def test_critical_anomaly_weighted_twice_high(db_session):
    _event(db_session, transaction_id="TXN-1")
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical",
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-2", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Normal",
    ))
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    row = db_session.get(PaymentHealthScore, "KEYBANK")

    # 1 Critical (weight 2) out of 2 scored -> penalty = 2/2*100 = 100 -> component 0
    assert row.anomaly_component == 0.0
    assert row.critical_anomaly_count == 1
    assert row.high_anomaly_count == 0


def test_operational_issue_penalty(db_session):
    for i in range(4):
        _event(db_session, transaction_id=f"TXN-{i}")
    db_session.add(OperationalIssue(
        issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-0",
    ))
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    row = db_session.get(PaymentHealthScore, "KEYBANK")

    assert row.operational_issue_count == 1
    assert row.operational_component == 75.0  # 1/4 issues -> 25% penalty


def test_reconciliation_penalty_uses_checked_not_total_transactions(db_session):
    _event(db_session, transaction_id="TXN-1", reconciliation_status="BREAK")
    _event(db_session, transaction_id="TXN-2", reconciliation_status=None)  # never reconciliation-checked
    db_session.add(ReconciliationBreak(
        tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK",
    ))
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    row = db_session.get(PaymentHealthScore, "KEYBANK")

    # 1 break / 1 reconciliation-checked transaction (TXN-2 excluded from
    # the denominator, same scope detect_reconciliation_breaks() itself uses)
    assert row.reconciliation_component == 0.0


def test_recompute_upserts_not_duplicates(db_session):
    _event(db_session, transaction_id="TXN-1")
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    compute_health_scores(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(PaymentHealthScore).count() == 1


def test_no_tenant_given_scores_every_tenant_with_data(db_session):
    _event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1")
    _event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2")
    db_session.commit()

    result = compute_health_scores(db_session)

    assert result["tenants_scored"] == 2
    assert db_session.query(PaymentHealthScore).count() == 2


def test_tenant_isolation(db_session):
    _event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", status="SETTLED")
    _event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", status="PENDING")
    db_session.commit()

    compute_health_scores(db_session, tenant_bank_id="KEYBANK")

    row = db_session.get(PaymentHealthScore, "KEYBANK")
    assert row.settlement_component == 100.0  # unaffected by MTB's unsettled row
    assert db_session.query(PaymentHealthScore).filter_by(tenant_bank_id="MTB").count() == 0


def test_no_transactions_still_scores_a_row_at_100(db_session):
    compute_health_scores(db_session, tenant_bank_id="KEYBANK")
    row = db_session.get(PaymentHealthScore, "KEYBANK")

    assert row.health_score == 100.0  # nothing bad detected because nothing exists to be bad
    assert row.total_transactions == 0
