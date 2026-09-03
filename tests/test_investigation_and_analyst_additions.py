from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.anomaly.models import EntitySnapshot
from app.dashboard import get_detection_attention, get_priority_distribution
from app.database import SessionLocal
from app.exposure import get_anomaly_heatmap, get_exposure_by_rail
from app.agent.models import AgentNarrative
from app.investigation.cases import compute_cases, get_anomaly_type_counts
from app.investigation.failure_rate_trend import get_case_failure_rate_trend
from app.investigation.models import InvestigationCase, InvestigationCaseAlert
from app.main import app
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak
from app.review.service import get_review_quality_trend_daily, set_review

# Distinct tenant so this doesn't collide with real KeyBank/Meridian data
# in the shared file-backed demo db -- same pattern every other endpoint
# test file already uses.
_TENANT = "INVESTIGATION-TEST-BANK"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _event(db, **overrides):
    defaults = dict(tenant_bank_id="KEYBANK", rail_type="ACH", status="SETTLED", amount=100.0)
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


# -- app/investigation/cases.py ------------------------------------------


def test_compute_cases_clusters_same_category_and_rail(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    _event(db_session, transaction_id="TXN-2", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=2, tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=300.0))
    db_session.commit()

    result = compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert result["cases_created"] == 1  # same category + rail + close in time -> one cluster
    assert result["alerts_grouped"] == 2
    case = db_session.query(InvestigationCase).filter_by(tenant_bank_id="KEYBANK").one()
    assert case.contributing_alerts_count == 2
    assert case.current_exposure == 800.0
    assert case.case_code.startswith("CNO-")


def test_compute_cases_splits_clusters_outside_window(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0, detected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    _event(db_session, transaction_id="TXN-2", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=2, tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=300.0, detected_at=datetime(2026, 2, 1, tzinfo=timezone.utc)))
    db_session.commit()

    result = compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert result["cases_created"] == 2  # more than 48h apart -> separate cases


def test_compute_cases_is_idempotent_rebuild(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.commit()

    compute_cases(db_session, tenant_bank_id="KEYBANK")
    compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(InvestigationCase).filter_by(tenant_bank_id="KEYBANK").count() == 1


def test_compute_cases_purges_stale_investigation_case_narratives(db_session):
    """A rebuild reassigns both case.id (autoincrement) and case_code
    (fresh uuid4()) -- any AgentNarrative cached under the OLD id would
    otherwise get silently served against whatever unrelated case
    inherits that same numeric id next. Confirmed live: a stale
    "CNO-36F8D5" narrative rendered on a since-renumbered case that was
    actually a different one entirely.
    """
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.commit()
    compute_cases(db_session, tenant_bank_id="KEYBANK")
    first_case_id = db_session.query(InvestigationCase).filter_by(tenant_bank_id="KEYBANK").one().id

    db_session.add(AgentNarrative(
        id=f"investigation_case:KEYBANK:{first_case_id}", signal_type="investigation_case",
        reference_id=str(first_case_id), tenant_bank_id="KEYBANK",
        title="Stale title", description="Stale description",
        recommended_action_title="Stale action", recommended_action_description="Stale.",
        model="mistral-test",
    ))
    db_session.commit()

    compute_cases(db_session, tenant_bank_id="KEYBANK")  # rebuild -- ids/case_codes reassigned

    assert db_session.query(AgentNarrative).filter_by(signal_type="investigation_case", tenant_bank_id="KEYBANK").count() == 0


def test_compute_cases_severity_sourced_from_priority_module(db_session):
    """A case's priority_level must equal its most severe contributing
    alert's real priority.py priority_level -- never a separate heuristic.
    """
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.commit()

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).filter_by(tenant_bank_id="KEYBANK").one()
    # A lone CONFIRMED_BREAK always floors at least Medium (priority.py's
    # _CONFIRMED_BREAK_SCORE_FLOOR) -- never null/unset.
    assert case.priority_level in ("Critical", "High", "Medium")
    assert case.severity_score is not None


def test_compute_cases_fraud_signal_uses_band_ceiling_score(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical", amount_total=1000.0,
    ))
    db_session.commit()

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).filter_by(tenant_bank_id="KEYBANK").one()
    assert case.priority_level == "Critical"
    assert case.severity_score == 90.0


def test_compute_cases_party_level_issue_has_no_rail(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="NETWORK_TIMEOUT_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id="MER-1"))
    db_session.commit()

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    case = db_session.query(InvestigationCase).filter_by(tenant_bank_id="KEYBANK").one()
    assert case.payment_rail is None


def test_compute_cases_tenant_isolation(db_session):
    _event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    _event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=2, tenant_bank_id="MTB", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=999.0))
    db_session.commit()

    compute_cases(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(InvestigationCase).filter_by(tenant_bank_id="MTB").count() == 0


def test_get_anomaly_type_counts_ranked(db_session):
    db_session.add(InvestigationCaseAlert(case_id=1, tenant_bank_id="KEYBANK", alert_code="A1", source_type="RECONCILIATION_BREAK", source_id=1, anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break", description="x", detected_at=datetime.now(timezone.utc)))
    db_session.add(InvestigationCaseAlert(case_id=1, tenant_bank_id="KEYBANK", alert_code="A2", source_type="RECONCILIATION_BREAK", source_id=2, anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break", description="x", detected_at=datetime.now(timezone.utc)))
    db_session.add(InvestigationCaseAlert(case_id=1, tenant_bank_id="KEYBANK", alert_code="A3", source_type="OPERATIONAL_ISSUE", source_id=1, anomaly_category="Operational", anomaly_type="Batch never settles", description="x", detected_at=datetime.now(timezone.utc)))
    db_session.commit()

    result = get_anomaly_type_counts(db_session, tenant_bank_id="KEYBANK")

    assert result["types"][0] == {"anomaly_type": "Confirmed reconciliation break", "count": 2}


# -- Tenant-scoping fix on investigation case detail/validate endpoints ----


def _seed_case(case_code: str, **overrides) -> int:
    """Idempotent against the shared file-backed demo db -- re-running the
    suite must not hit case_code's UNIQUE constraint, same reasoning
    test_detail_and_search_endpoints.py's _seed_merchant/_seed_individual
    already establish for this file-backed db.
    """
    defaults = dict(
        tenant_bank_id=_TENANT, category="CONFIRMED_BREAK", title="Test Case",
        transactions_affected=1, contributing_alerts_count=1, opened_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    db = SessionLocal()
    try:
        existing = db.query(InvestigationCase).filter_by(case_code=case_code).one_or_none()
        if existing is None:
            case = InvestigationCase(case_code=case_code, **defaults)
            db.add(case)
            db.commit()
            db.refresh(case)
            return case.id
        return existing.id
    finally:
        db.close()


def test_investigation_case_detail_404_for_wrong_tenant(client):
    case_id = _seed_case("CNO-TEST01")

    resp = client.get(f"/investigation/cases/{case_id}", params={"tenant_bank_id": "SOME-OTHER-TENANT"})
    assert resp.status_code == 404

    resp = client.get(f"/investigation/cases/{case_id}", params={"tenant_bank_id": _TENANT})
    assert resp.status_code == 200


def test_investigation_case_validate_404_for_wrong_tenant(client):
    case_id = _seed_case("CNO-TEST02")

    resp = client.post(f"/investigation/cases/{case_id}/validate", json={"tenant_bank_id": "SOME-OTHER-TENANT", "validation_status": "VALID"})
    assert resp.status_code == 404

    resp = client.post(f"/investigation/cases/{case_id}/validate", json={"tenant_bank_id": _TENANT, "validation_status": "VALID"})
    assert resp.status_code == 200
    assert resp.json()["validation_status"] == "VALID"


def test_investigation_cases_priority_level_filter(client):
    _seed_case("CNO-CRIT01", category="CONFIRMED_BREAK", title="Critical Case", severity_score=95.0, priority_level="Critical")
    _seed_case("CNO-LOW01", category="PROVISIONAL_VARIANCE", title="Low Case", severity_score=10.0, priority_level="Low")

    resp = client.get("/investigation/cases", params={"tenant_bank_id": _TENANT, "priority_level": "critical"})
    assert resp.status_code == 200
    cases = resp.json()["cases"]
    assert all(c["priority_level"] == "Critical" for c in cases)
    assert any(c["case_code"] == "CNO-CRIT01" for c in cases)


def test_investigation_cases_date_filter_uses_real_anchor(client):
    """No alerts seeded for either case -> resolve_real_case_anchor falls
    back to opened_at (documented behavior, see sla.py) -- a real,
    meaningful date filter, not the previous no-op (start_date/end_date
    were silently ignored before this endpoint accepted them at all).
    """
    _seed_case("CNO-DATEOLD01", opened_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    _seed_case("CNO-DATENEW01", opened_at=datetime.now(timezone.utc))

    resp = client.get("/investigation/cases", params={
        "tenant_bank_id": _TENANT, "start_date": "2020-01-01", "end_date": "2020-01-02",
    })
    assert resp.status_code == 200
    codes = {c["case_code"] for c in resp.json()["cases"]}
    assert "CNO-DATEOLD01" in codes
    assert "CNO-DATENEW01" not in codes


# -- app/dashboard.py: get_priority_distribution reconciled with priority.py --


def test_priority_distribution_sourced_from_priority_module(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.commit()

    result = get_priority_distribution(db_session, "KEYBANK")

    # A lone CONFIRMED_BREAK floors at Medium in priority.py -- never
    # counted as Low, matching priority_levels_for_breaks' own floor rule.
    assert result["low"] == 0
    assert result["critical"] + result["high"] + result["medium"] == 1


def test_priority_distribution_narrows_by_date(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", transaction_occurred_at="2026-08-03T00:00:00Z")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", transaction_occurred_at="2026-09-01T00:00:00Z")
    db_session.add(ReconciliationBreak(id=2, tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=999.0))
    db_session.commit()

    from datetime import date
    in_range = get_priority_distribution(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))
    total_in_range = sum(in_range.values())
    all_time = get_priority_distribution(db_session, "KEYBANK")
    total_all_time = sum(all_time.values())

    assert total_in_range == 1
    assert total_all_time == 2


def test_priority_distribution_fraud_uses_real_anomaly_band(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical",
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-2", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Normal",
    ))
    db_session.commit()

    result = get_priority_distribution(db_session, "KEYBANK")

    assert result["critical"] == 1  # Normal band excluded, not a detected issue


# -- app/exposure.py: get_exposure_by_rail / get_anomaly_heatmap ----------


def test_exposure_by_rail_excludes_fraud_no_single_rail(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=500.0)
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical", amount_total=99999.0,
    ))
    db_session.commit()

    result = get_exposure_by_rail(db_session, "KEYBANK")

    assert result["rails"] == [{"rail_type": "ACH", "exposure": 500.0}]
    assert result["total"] == 500.0  # fraud's 99999 not included -- no single rail


def test_anomaly_heatmap_cross_tab(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=100.0))
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1"))
    db_session.commit()

    result = get_anomaly_heatmap(db_session, "KEYBANK")

    cells = {(c["rail_type"], c["category"]): c["count"] for c in result["cells"]}
    assert cells[("ACH", "Reconciliation")] == 1
    assert cells[("ACH", "Operational")] == 1


# -- app/dashboard.py: get_detection_attention -----------------------------


def test_detection_attention_flags_below_average_rail(db_session):
    for i in range(10):
        _event(db_session, transaction_id=f"ACH-{i}", rail_type="ACH")
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="ACH-0"))
    for i in range(10):
        _event(db_session, transaction_id=f"WIRE-{i}", rail_type="WIRE")
    db_session.commit()

    result = get_detection_attention(db_session, "KEYBANK")

    rails_flagged = {a["rail_type"] for a in result["areas"]}
    assert "ACH" in rails_flagged  # below the cross-rail average (WIRE has 0 flagged)
    assert "WIRE" not in rails_flagged


def test_detection_attention_empty_with_no_rail_data(db_session):
    result = get_detection_attention(db_session, "KEYBANK")
    assert result == {"areas": []}


# -- app/review/service.py: get_review_quality_trend_daily -----------------


def test_review_quality_trend_daily_buckets_by_day(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-A"))
    db_session.add(OperationalIssue(id=2, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-B"))
    db_session.commit()
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "2", "KEYBANK", "DISMISSED")

    result = get_review_quality_trend_daily(db_session, "KEYBANK", days=7)

    assert len(result) == 1  # both reviewed today (test runs instantaneously)
    assert result[0]["confirmed"] == 1
    assert result[0]["dismissed"] == 1


def test_review_quality_trend_daily_empty_with_no_reviews(db_session):
    assert get_review_quality_trend_daily(db_session, "KEYBANK") == []


# -- app/investigation/failure_rate_trend.py --------------------------------


def _weekly_snapshot(db, party_id, window_start, ratio_field, ratio_value, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK", party_id=party_id, party_type="MERCHANT", segment="MERCHANT",
        window_type="WEEKLY", window_start=window_start, window_end=window_start + timedelta(days=6),
    )
    defaults.update(overrides)
    defaults[ratio_field] = ratio_value
    snap = EntitySnapshot(**defaults)
    db.add(snap)
    return snap


def test_failure_rate_trend_available_with_real_baseline_and_spike(db_session):
    party_id = "MERCH-TREND-01"
    weeks = [datetime(2026, 1, 5 + 7 * i, tzinfo=timezone.utc) for i in range(4)]
    ratios = [0.03, 0.08, 0.04, 0.30]  # last week is the real spike
    for week_start, ratio in zip(weeks, ratios):
        _weekly_snapshot(db_session, party_id, week_start, "timeout_ratio", ratio)

    issue = OperationalIssue(
        issue_type="NETWORK_TIMEOUT_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id=party_id,
        window_start=weeks[3], window_end=weeks[3] + timedelta(days=6), severity_score=100.0,
    )
    db_session.add(issue)
    db_session.flush()

    case = InvestigationCase(
        case_code="CNO-TRENDTEST1", tenant_bank_id="KEYBANK", category="NETWORK_TIMEOUT_SPIKE", title="Timeout Cluster",
        transactions_affected=1, contributing_alerts_count=1, opened_at=weeks[3], severity_score=100.0, priority_level="Critical",
    )
    db_session.add(case)
    db_session.flush()
    db_session.add(InvestigationCaseAlert(
        case_id=case.id, tenant_bank_id="KEYBANK", alert_code="ALT-GEN-0100", source_type="OPERATIONAL_ISSUE",
        source_id=issue.id, anomaly_category="Operational", anomaly_type="Failure-rate spike",
        description="test", detected_at=weeks[3] + timedelta(days=6),
    ))
    db_session.commit()

    result = get_case_failure_rate_trend(db_session, "KEYBANK", case.id)

    assert result["available"] is True
    assert result["title"] == "Network Timeout Rate vs. Expected Baseline"
    assert result["unit"] == "%"
    assert [p["value"] for p in result["points"]] == [3.0, 8.0, 4.0, 30.0]
    assert result["baseline_value"] == pytest.approx(5.0, abs=0.01)
    assert result["threshold_value"] == pytest.approx(10.29, abs=0.01)
    assert result["max_value"] >= 30.0
    # Only the real triggering week (|z|=9.4, way past the 2.0 cutoff)
    # gets an annotation -- the earlier weeks' modest wobble stays quiet.
    assert len(result["annotations"]) == 1
    assert result["annotations"][0]["time"] == weeks[3].date().isoformat()


def test_failure_rate_trend_unavailable_for_non_rate_category(db_session):
    case = InvestigationCase(
        case_code="CNO-TRENDTEST2", tenant_bank_id="KEYBANK", category="CONFIRMED_BREAK", title="Break Cluster",
        transactions_affected=1, contributing_alerts_count=1, opened_at=datetime.now(timezone.utc),
    )
    db_session.add(case)
    db_session.commit()

    result = get_case_failure_rate_trend(db_session, "KEYBANK", case.id)

    assert result["available"] is False
    assert result["points"] == []
    assert "No real per-week rate" in result["reason"]


def test_failure_rate_trend_unavailable_with_insufficient_history(db_session):
    party_id = "MERCH-TREND-02"
    weeks = [datetime(2026, 1, 5 + 7 * i, tzinfo=timezone.utc) for i in range(2)]  # only 2 real weeks
    for week_start, ratio in zip(weeks, [0.05, 0.30]):
        _weekly_snapshot(db_session, party_id, week_start, "format_reject_ratio", ratio)

    issue = OperationalIssue(
        issue_type="FORMAT_REJECTION_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id=party_id,
        window_start=weeks[1], window_end=weeks[1] + timedelta(days=6), severity_score=80.0,
    )
    db_session.add(issue)
    db_session.flush()

    case = InvestigationCase(
        case_code="CNO-TRENDTEST3", tenant_bank_id="KEYBANK", category="FORMAT_REJECTION_SPIKE", title="Rejection Cluster",
        transactions_affected=1, contributing_alerts_count=1, opened_at=weeks[1],
    )
    db_session.add(case)
    db_session.flush()
    db_session.add(InvestigationCaseAlert(
        case_id=case.id, tenant_bank_id="KEYBANK", alert_code="ALT-GEN-0200", source_type="OPERATIONAL_ISSUE",
        source_id=issue.id, anomaly_category="Operational", anomaly_type="Formatting rejection spike",
        description="test", detected_at=weeks[1] + timedelta(days=6),
    ))
    db_session.commit()

    result = get_case_failure_rate_trend(db_session, "KEYBANK", case.id)

    assert result["available"] is False
    assert "Fewer than 3" in result["reason"]


def test_failure_rate_trend_404_for_wrong_tenant(client):
    case_id = _seed_case("CNO-TRENDTEST4", category="CONFIRMED_BREAK")

    resp = client.get(f"/investigation/cases/{case_id}/failure-rate-trend", params={"tenant_bank_id": "SOME-OTHER-TENANT"})
    assert resp.status_code == 404

    resp = client.get(f"/investigation/cases/{case_id}/failure-rate-trend", params={"tenant_bank_id": _TENANT})
    assert resp.status_code == 200
    assert resp.json()["available"] is False
