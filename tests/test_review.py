from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.anomaly.models import EntitySnapshot
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak
from app.review.models import AnalystReview
from app.review.service import get_review, get_review_quality_trend, get_review_summary, set_review


def test_get_review_returns_none_when_never_reviewed(db_session):
    assert get_review(db_session, "operational_issue", "42", "KEYBANK") is None


def test_set_review_creates_row_with_reviewer_and_timestamp(db_session):
    row = set_review(db_session, "operational_issue", "42", "KEYBANK", "CONFIRMED", reviewed_by="analyst@bank.com")

    assert row.status == "CONFIRMED"
    assert row.reviewed_by == "analyst@bank.com"
    assert row.reviewed_at is not None
    assert row.id == "operational_issue:KEYBANK:42"


def test_set_review_twice_updates_same_row_not_duplicate(db_session):
    set_review(db_session, "operational_issue", "42", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "42", "KEYBANK", "DISMISSED")

    assert db_session.query(AnalystReview).count() == 1
    row = get_review(db_session, "operational_issue", "42", "KEYBANK")
    assert row.status == "DISMISSED"


def test_set_review_back_to_pending_clears_reviewed_at(db_session):
    set_review(db_session, "operational_issue", "42", "KEYBANK", "CONFIRMED", reviewed_by="a@b.com")
    row = set_review(db_session, "operational_issue", "42", "KEYBANK", "PENDING")

    assert row.reviewed_at is None


def test_set_review_rejects_unknown_status(db_session):
    with pytest.raises(ValueError, match="status must be one of"):
        set_review(db_session, "operational_issue", "42", "KEYBANK", "APPROVED_BY_NOBODY")


def test_review_tenant_isolation_in_key(db_session):
    set_review(db_session, "operational_issue", "42", "KEYBANK", "CONFIRMED")

    assert get_review(db_session, "operational_issue", "42", "MTB") is None


def test_review_summary_counts_claims_across_all_three_engines(db_session):
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1"))
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical",
    ))
    db_session.commit()

    summary = get_review_summary(db_session, "KEYBANK")

    assert summary["total_claims"] == 3
    assert summary["pending"] == 3
    assert summary["confirmed"] == 0
    assert summary["review_rate"] == 0.0


def test_review_summary_excludes_normal_and_low_medium_anomaly_bands(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Normal",
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-2", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Low-Medium",
    ))
    db_session.commit()

    summary = get_review_summary(db_session, "KEYBANK")

    assert summary["total_claims"] == 0  # neither band is a "material" claim


def test_review_summary_reflects_confirmed_and_dismissed(db_session):
    """AnalystReview.reference_id is the claim row's PRIMARY KEY, not its
    business reference_id -- the convention /review/set, /review/status
    and app/exposure.py's claims all use. get_review_summary now matches
    each review back to a real claim by that key, so seeding a review
    against a transaction id (as this test used to) correctly counts as
    zero reviewed.
    """
    issue_a = OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1")
    issue_b = OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-2")
    db_session.add_all([issue_a, issue_b])
    db_session.commit()

    set_review(db_session, "operational_issue", str(issue_a.id), "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", str(issue_b.id), "KEYBANK", "DISMISSED")

    summary = get_review_summary(db_session, "KEYBANK")

    assert summary["confirmed"] == 1
    assert summary["dismissed"] == 1
    assert summary["pending"] == 0
    assert summary["review_rate"] == 1.0
    assert summary["by_signal_type"]["operational_issue"] == {"total_claims": 2, "reviewed": 2, "pending": 0}


def test_review_summary_tenant_isolation(db_session):
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1"))
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="MTB", reference_type="TRANSACTION", reference_id="TXN-2"))
    db_session.commit()

    summary = get_review_summary(db_session, "KEYBANK")

    assert summary["total_claims"] == 1


def test_review_summary_with_no_claims_has_null_review_rate(db_session):
    summary = get_review_summary(db_session, "KEYBANK")

    assert summary["total_claims"] == 0
    assert summary["review_rate"] is None


def test_quality_trend_one_point_per_real_review_in_chronological_order(db_session):
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "2", "KEYBANK", "DISMISSED")
    set_review(db_session, "operational_issue", "3", "KEYBANK", "CONFIRMED")

    points = get_review_quality_trend(db_session, "KEYBANK")

    assert len(points) == 3
    assert points[0]["reviewed_count"] == 1
    assert points[0]["confirmation_rate"] == 1.0
    assert points[1]["reviewed_count"] == 2
    assert points[1]["confirmation_rate"] == 0.5
    assert points[1]["false_positive_rate"] == 0.5
    assert points[2]["reviewed_count"] == 3
    assert points[2]["confirmation_rate"] == pytest.approx(2 / 3)


def test_quality_trend_excludes_pending_reviews(db_session):
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "2", "KEYBANK", "PENDING")

    points = get_review_quality_trend(db_session, "KEYBANK")

    assert len(points) == 1


def test_quality_trend_empty_when_nothing_reviewed_yet(db_session):
    points = get_review_quality_trend(db_session, "KEYBANK")

    assert points == []


def test_quality_trend_tenant_isolation(db_session):
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "2", "MTB", "CONFIRMED")

    points = get_review_quality_trend(db_session, "KEYBANK")

    assert len(points) == 1
