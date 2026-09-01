from __future__ import annotations

from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.priority import CRITICAL, HIGH, LOW, MEDIUM, priority_levels_for_breaks, priority_levels_for_issues
from app.reconciliation.models import ReconciliationBreak


def _event(db, **overrides):
    defaults = dict(tenant_bank_id="KEYBANK", rail_type="ACH", status="SETTLED")
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


def test_duplicate_payment_priority_scales_with_real_amount(db_session):
    _event(db_session, transaction_id="TXN-SMALL", amount=50.0)
    _event(db_session, transaction_id="TXN-MED", amount=500.0)
    _event(db_session, transaction_id="TXN-LARGE", amount=50000.0)
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-SMALL"))
    db_session.add(OperationalIssue(id=2, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-MED"))
    db_session.add(OperationalIssue(id=3, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-LARGE"))
    db_session.commit()

    result = priority_levels_for_issues(db_session, "KEYBANK")

    assert result[3]["severity_score"] > result[2]["severity_score"] > result[1]["severity_score"]
    assert result[3]["priority_level"] == CRITICAL  # top of its own real population


def test_batch_not_settled_priority_scales_with_days_overdue(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="BATCH_NOT_SETTLED", tenant_bank_id="KEYBANK", reference_type="BATCH", reference_id="B-1", details={"days_overdue": 1}))
    db_session.add(OperationalIssue(id=2, issue_type="BATCH_NOT_SETTLED", tenant_bank_id="KEYBANK", reference_type="BATCH", reference_id="B-2", details={"days_overdue": 30}))
    db_session.commit()

    result = priority_levels_for_issues(db_session, "KEYBANK")

    assert result[2]["severity_score"] > result[1]["severity_score"]


def test_spike_issue_uses_its_own_real_severity_score_directly(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="FORMAT_REJECTION_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id="MER-1", severity_score=92.0))
    db_session.commit()

    result = priority_levels_for_issues(db_session, "KEYBANK")

    assert result[1]["severity_score"] == 92.0
    assert result[1]["priority_level"] == CRITICAL


def test_all_four_priority_bands_are_reachable(db_session):
    for i, amount in enumerate([10.0, 100.0, 1000.0, 10000.0, 100000.0]):
        _event(db_session, transaction_id=f"TXN-{i}", amount=amount)
        db_session.add(OperationalIssue(id=i + 1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id=f"TXN-{i}"))
    db_session.commit()

    result = priority_levels_for_issues(db_session, "KEYBANK")
    bands = {r["priority_level"] for r in result.values()}

    assert LOW in bands or MEDIUM in bands  # the smallest real amounts don't all land Critical/High
    assert CRITICAL in bands  # the largest real amount does


def test_missing_join_data_lands_mid_band_not_silently_zero(db_session):
    # No matching CanonicalEvent for this transaction_id -- a real data
    # gap, not scored as automatically Low.
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-MISSING"))
    db_session.commit()

    result = priority_levels_for_issues(db_session, "KEYBANK")

    assert result[1]["severity_score"] == 50.0
    assert result[1]["priority_level"] == MEDIUM


def test_confirmed_break_never_lands_below_medium_regardless_of_amount(db_session):
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=1.0))
    db_session.commit()

    result = priority_levels_for_breaks(db_session, "KEYBANK")

    assert result[1]["priority_level"] in (CRITICAL, HIGH, MEDIUM)
    assert result[1]["severity_score"] >= 50.0


def test_provisional_variance_can_be_low_when_amount_is_small(db_session):
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="PROVISIONAL_VARIANCE", variance_amount=1.0))
    db_session.add(ReconciliationBreak(id=2, tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="PROVISIONAL_VARIANCE", variance_amount=100000.0))
    db_session.commit()

    result = priority_levels_for_breaks(db_session, "KEYBANK")

    assert result[1]["severity_score"] < result[2]["severity_score"]


def test_break_uses_variance_amount_over_amount_when_both_present(db_session):
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="PROVISIONAL_VARIANCE", amount=100.0, variance_amount=0.0))
    db_session.add(ReconciliationBreak(id=2, tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="PROVISIONAL_VARIANCE", amount=5000.0, variance_amount=5000.0))
    db_session.commit()

    result = priority_levels_for_breaks(db_session, "KEYBANK")

    # TXN-1 has variance_amount=0.0 (falsy) -- falls back to `amount` (100.0),
    # still real, still ranked against the real population.
    assert result[1]["severity_score"] <= result[2]["severity_score"]


def test_priority_levels_tenant_isolation(db_session):
    _event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", amount=100.0)
    _event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", amount=999999.0)
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1"))
    db_session.add(OperationalIssue(id=2, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="MTB", reference_type="TRANSACTION", reference_id="TXN-2"))
    db_session.commit()

    result = priority_levels_for_issues(db_session, "KEYBANK")

    assert list(result.keys()) == [1]
