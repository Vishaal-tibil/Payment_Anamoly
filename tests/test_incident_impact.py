from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.models import BeneficiarySnapshot, EntitySnapshot
from app.incident_impact import get_incident_enterprise_impact
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak

_TENANT = "KEYBANK"
_WINDOW_END = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _event(db, **overrides):
    defaults = dict(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-1", amount=100.0)
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


def _snapshot(**overrides):
    defaults = dict(
        party_type="MERCHANT", tenant_bank_id=_TENANT, segment="MERCHANT",
        window_type="TO_DATE", window_end=_WINDOW_END, anomaly_band="Critical",
    )
    defaults.update(overrides)
    return EntitySnapshot(**defaults)


def _beneficiary_snapshot(**overrides):
    defaults = dict(tenant_bank_id=_TENANT, window_start=_WINDOW_END, window_end=_WINDOW_END)
    defaults.update(overrides)
    return BeneficiarySnapshot(**defaults)


def test_returns_none_for_missing_signal(db_session):
    assert get_incident_enterprise_impact(db_session, _TENANT, "operational_issue", 999) is None


def test_reconciliation_break_impact_cross_references_real_fraud_and_reconciliation(db_session):
    # The break's own transaction resolves to merchant MER-1.
    _event(db_session, transaction_id="TXN-1", merchant_id="MER-1", amount=500.0)
    brk = ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0, variance_amount=-50.0)
    db_session.add(brk)
    # A second, unrelated break for the SAME merchant -- must be included
    # in reconciliation_exposure (real cross-reference), not just this one.
    _event(db_session, transaction_id="TXN-2", merchant_id="MER-1", rail_type="WIRE", amount=200.0)
    db_session.add(ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id="TXN-2", rail_type="WIRE", detection_type="CONFIRMED_BREAK", amount=200.0, variance_amount=200.0))
    # A real fraud snapshot for the SAME merchant.
    db_session.add(_snapshot(party_id="MER-1", amount_total=9000.0))
    db_session.commit()
    brk_id = brk.id

    result = get_incident_enterprise_impact(db_session, _TENANT, "reconciliation_break", brk_id)

    assert result["payments_affected"] == 1
    assert result["payment_value"] == 50.0  # abs(variance_amount)
    assert result["reconciliation_exposure"] == 250.0  # both breaks for MER-1: 50 + 200
    assert result["fraud_exposure"] == 9000.0


def test_fraud_anomaly_impact_includes_its_own_amount_and_avoids_double_count(db_session):
    snap = _snapshot(party_id="MER-2", transaction_count=10, amount_total=5000.0)
    db_session.add(snap)
    db_session.commit()

    result = get_incident_enterprise_impact(db_session, _TENANT, "fraud_anomaly", snap.id)

    assert result["payments_affected"] == 10
    assert result["payment_value"] == 5000.0
    assert result["fraud_exposure"] == 5000.0  # its own amount, not doubled by re-querying itself
    assert result["reconciliation_exposure"] == 0.0  # honestly none found


def test_party_level_operational_issue_uses_reference_id_as_party_directly(db_session):
    issue = OperationalIssue(
        issue_type="NETWORK_TIMEOUT_SPIKE", tenant_bank_id=_TENANT,
        reference_type="PARTY", reference_id="MER-3", severity_score=80.0,
    )
    db_session.add(issue)
    db_session.add(_snapshot(party_id="MER-3", amount_total=1234.0, anomaly_band="High"))
    db_session.commit()

    result = get_incident_enterprise_impact(db_session, _TENANT, "operational_issue", issue.id)

    assert result["payments_affected"] == 0  # no single transaction for a rate-based issue
    assert result["payment_value"] == 0.0
    assert result["fraud_exposure"] == 1234.0  # cross-referenced via reference_id as the real party


def test_funnel_account_has_no_cross_reference_party(db_session):
    snap = _beneficiary_snapshot(beneficiary_key="PAYEE-1", transaction_count=3, amount_total=300.0)
    db_session.add(snap)
    db_session.commit()

    result = get_incident_enterprise_impact(db_session, _TENANT, "funnel_account", snap.id)

    assert result["payments_affected"] == 3
    assert result["payment_value"] == 300.0
    assert result["reconciliation_exposure"] == 0.0
    assert result["fraud_exposure"] == 0.0


def test_percent_is_real_share_of_tenant_wide_total(db_session):
    _event(db_session, transaction_id="TXN-1", merchant_id="MER-1", amount=100.0)
    _event(db_session, transaction_id="TXN-2", merchant_id="MER-2", amount=300.0)  # unrelated -- part of the total
    brk = ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=100.0)
    db_session.add(brk)
    db_session.commit()

    result = get_incident_enterprise_impact(db_session, _TENANT, "reconciliation_break", brk.id)

    # payment_value=100 out of a real tenant-wide total_amount of 400 -> 25%.
    assert result["payment_value_percent"] == 25.0
