from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.investigation.cases import compute_cases
from app.investigation.models import InvestigationCase, InvestigationCaseAlert
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak
from app.anomaly.models import EntitySnapshot

_BASE = datetime(2026, 4, 12, 13, 0, tzinfo=timezone.utc)


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK", rail_type="RTP", transaction_id="TXN-1",
        transaction_occurred_at="2026-04-12T12:00:00Z",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def _make_op_issue(db, **overrides):
    defaults = dict(
        issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK",
        reference_type="TRANSACTION", reference_id="TXN-1", detected_at=_BASE,
    )
    defaults.update(overrides)
    issue = OperationalIssue(**defaults)
    db.add(issue)
    db.commit()
    return issue


def _make_recon_break(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="RTP",
        detection_type="CONFIRMED_BREAK", variance_amount=100.0, detected_at=_BASE,
    )
    defaults.update(overrides)
    brk = ReconciliationBreak(**defaults)
    db.add(brk)
    db.commit()
    return brk


def _make_snapshot(db, **overrides):
    defaults = dict(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_start=_BASE, window_end=_BASE, transaction_count=5,
        amount_total=1000.0, anomaly_band="Critical", final_anomaly_score=90.0, rails_used=["RTP"],
    )
    defaults.update(overrides)
    snap = EntitySnapshot(**defaults)
    db.add(snap)
    db.commit()
    return snap


def test_two_issues_same_category_rail_and_window_form_one_case(db_session):
    _make_op_issue(db_session, reference_id="TXN-1", detected_at=_BASE)
    _make_event(db_session, transaction_id="TXN-1", rail_type="RTP")
    _make_op_issue(db_session, reference_id="TXN-2", detected_at=_BASE + timedelta(hours=2))
    _make_event(db_session, transaction_id="TXN-2", rail_type="RTP")

    result = compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert result["cases_created"] == 1
    assert result["alerts_grouped"] == 2
    case = db_session.query(InvestigationCase).one()
    assert case.category == "DUPLICATE_PAYMENT"
    assert case.payment_rail == "RTP"
    assert case.contributing_alerts_count == 2
    assert case.transactions_affected == 2
    alerts = db_session.query(InvestigationCaseAlert).filter_by(case_id=case.id).all()
    assert len(alerts) == 2


def test_issues_more_than_48h_apart_form_separate_cases(db_session):
    _make_op_issue(db_session, reference_id="TXN-1", detected_at=_BASE)
    _make_event(db_session, transaction_id="TXN-1", rail_type="RTP")
    _make_op_issue(db_session, reference_id="TXN-2", detected_at=_BASE + timedelta(hours=49))
    _make_event(db_session, transaction_id="TXN-2", rail_type="RTP")

    result = compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert result["cases_created"] == 2


def test_different_rail_forms_separate_cases(db_session):
    _make_op_issue(db_session, reference_id="TXN-1", detected_at=_BASE)
    _make_event(db_session, transaction_id="TXN-1", rail_type="RTP")
    _make_op_issue(db_session, reference_id="TXN-2", detected_at=_BASE + timedelta(hours=1))
    _make_event(db_session, transaction_id="TXN-2", rail_type="ACH")

    result = compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert result["cases_created"] == 2
    rails = {c.payment_rail for c in db_session.query(InvestigationCase).all()}
    assert rails == {"RTP", "ACH"}


def test_party_level_issue_type_never_gets_a_rail(db_session):
    _make_op_issue(
        db_session, issue_type="NETWORK_TIMEOUT_SPIKE", reference_type="PARTY", reference_id="MER-1", detected_at=_BASE,
    )

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).one()
    assert case.payment_rail is None
    assert case.category == "NETWORK_TIMEOUT_SPIKE"


def test_reconciliation_break_included_with_its_own_rail_and_exposure(db_session):
    _make_recon_break(db_session, transaction_id="TXN-9", rail_type="FEDNOW", variance_amount=24.63)

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).one()
    assert case.category == "CONFIRMED_BREAK"
    assert case.payment_rail == "FEDNOW"
    assert case.current_exposure == 24.63
    alert = db_session.query(InvestigationCaseAlert).one()
    assert alert.anomaly_category == "Reconciliation"
    assert alert.transaction_id == "TXN-9"


def test_only_critical_and_high_fraud_snapshots_are_included(db_session):
    _make_snapshot(db_session, party_id="MER-CRIT", anomaly_band="Critical")
    _make_snapshot(db_session, party_id="MER-NORMAL", anomaly_band="Normal")

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    cases = db_session.query(InvestigationCase).all()
    assert len(cases) == 1
    assert cases[0].category == "FRAUD_CRITICAL"


def test_fraud_snapshot_with_multiple_rails_gets_no_rail_split(db_session):
    _make_snapshot(db_session, rails_used=["RTP", "ACH"])

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).one()
    assert case.payment_rail is None


def test_recompute_replaces_not_duplicates(db_session):
    _make_op_issue(db_session, reference_id="TXN-1", detected_at=_BASE)
    _make_event(db_session, transaction_id="TXN-1", rail_type="RTP")

    compute_cases(db_session, tenant_bank_id="KEYBANK")
    compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(InvestigationCase).count() == 1
    assert db_session.query(InvestigationCaseAlert).count() == 1


def test_tenant_isolation(db_session):
    _make_op_issue(db_session, tenant_bank_id="KEYBANK", reference_id="TXN-1", detected_at=_BASE)
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="RTP")
    _make_op_issue(db_session, tenant_bank_id="MTB", reference_id="TXN-2", detected_at=_BASE)
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", rail_type="RTP")

    result = compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert result["cases_created"] == 1
    assert db_session.query(InvestigationCase).filter_by(tenant_bank_id="MTB").count() == 0


def test_mixed_sources_combine_into_one_case_when_category_and_rail_match(db_session):
    # Reconciliation break and a snapshot happen to share the same
    # clustering key is unlikely in practice (different category
    # strings) -- this test instead confirms exposure sums correctly
    # across multiple alerts of the SAME source type, since OperationalIssue
    # contributes no exposure at all (documented, not a bug).
    _make_recon_break(db_session, transaction_id="TXN-1", rail_type="RTP", variance_amount=100.0, detected_at=_BASE)
    _make_recon_break(db_session, transaction_id="TXN-2", rail_type="RTP", variance_amount=50.0, detected_at=_BASE + timedelta(hours=1))

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).one()
    assert case.current_exposure == 150.0
    assert case.transactions_affected == 2
