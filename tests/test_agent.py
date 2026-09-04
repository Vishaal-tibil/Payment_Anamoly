from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.models import AgentNarrative
from app.agent.narration import (
    facts_for_entity_snapshot,
    facts_for_investigation_case,
    facts_for_operational_issue,
    facts_for_reconciliation_break,
    get_or_create_narrative,
    narrate,
)
from app.anomaly.models import EntitySnapshot
from app.investigation.models import InvestigationCase, InvestigationCaseAlert
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak


def _mock_mistral_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return response


_VALID_ACTION = {"title": "Review batch", "description": "Check the batch manually.", "why": "It is overdue."}
_VALID_PAYLOAD = {
    "title": "Batch overdue",
    "description": "This batch has not settled.",
    "recommended_actions": [_VALID_ACTION],
}


def test_facts_for_operational_issue_uses_only_real_fields():
    issue = OperationalIssue(
        id=1, issue_type="BATCH_NOT_SETTLED", tenant_bank_id="KEYBANK",
        reference_type="BATCH", reference_id="BATCH-1", severity_score=None,
        details={"days_overdue": 4}, detected_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    facts = facts_for_operational_issue(issue)
    assert facts == {
        "signal_type": "operational_issue",
        "issue_type": "BATCH_NOT_SETTLED",
        "reference_type": "BATCH",
        "reference_id": "BATCH-1",
        "severity_score": None,
        "details": {"days_overdue": 4},
        "detected_at": "2026-05-01T00:00:00+00:00",
    }


def test_facts_for_reconciliation_break():
    brk = ReconciliationBreak(
        id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH",
        detection_type="CONFIRMED_BREAK", source_reconciliation_status="BREAK",
        variance_amount=-19.4, amount=2095.7, detected_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    facts = facts_for_reconciliation_break(brk)
    assert facts["detection_type"] == "CONFIRMED_BREAK"
    assert facts["variance_amount"] == -19.4
    assert facts["transaction_id"] == "TXN-1"


def test_facts_for_entity_snapshot():
    snapshot = EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 11, tzinfo=timezone.utc), transaction_count=10,
        amount_total=5000.0, isolation_forest_score=85.0, cluster_changed=True,
        timeseries_drift_score=70.0, final_anomaly_score=84.0, anomaly_band="Critical",
    )
    facts = facts_for_entity_snapshot(snapshot)
    assert facts["anomaly_band"] == "Critical"
    assert facts["isolation_forest_score"] == 85.0
    assert facts["cluster_changed"] is True


def test_facts_for_investigation_case_summarizes_alerts_without_per_alert_ids(db_session):
    case = InvestigationCase(
        id=1, case_code="CNO-ABC123", tenant_bank_id="KEYBANK", category="CONFIRMED_BREAK",
        payment_rail="CHEQUE", title="CHEQUE Confirmed reconciliation break Cluster",
        current_exposure=78.08, transactions_affected=6, contributing_alerts_count=6,
        validation_status="PENDING", opened_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    alerts = [
        InvestigationCaseAlert(
            id=i, case_id=1, tenant_bank_id="KEYBANK", alert_code=f"ALT-{i}",
            source_type="RECONCILIATION_BREAK", source_id=i, transaction_id=f"CHK-MTB-10000{i}",
            payment_rail="CHEQUE", anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break",
            description=f"Confirmed reconciliation break on transaction CHK-MTB-10000{i} (CHEQUE)",
            detected_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        for i in range(1, 4)
    ]

    facts = facts_for_investigation_case(db_session, case, alerts)

    assert facts["signal_type"] == "investigation_case"
    assert facts["case_code"] == "CNO-ABC123"
    assert facts["current_exposure"] == 78.08
    assert facts["contributing_alerts_count"] == 6
    assert len(facts["alerts"]) == 3
    # Per-alert facts summarize type/category/rail, not individual transaction_ids --
    # the prompt explicitly doesn't require reproducing every alert's identifier.
    assert "transaction_id" not in facts["alerts"][0]
    assert facts["alerts"][0]["anomaly_type"] == "Confirmed reconciliation break"
    # No ReconciliationBreak/CanonicalEvent rows seeded in db_session for this
    # (category, rail) -- honestly None, not a fabricated trend.
    assert facts["category_trend"] is None


@patch("app.agent.narration.get_clients")
def test_narrate_passes_when_case_code_present_verbatim(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "Case CNO-ABC123: reconciliation break cluster",
        "description": "Case CNO-ABC123 clusters 6 confirmed reconciliation breaks on CHEQUE.",
        "recommended_actions": [
            {"title": "Review cheque breaks", "description": "Review the clustered cheque transactions for a common cause.", "why": "6 confirmed breaks share this cause."},
        ],
    }))
    mock_get_clients.return_value = [mock_client]

    result = asyncio.run(narrate({"signal_type": "investigation_case", "case_code": "CNO-ABC123"}))

    assert "CNO-ABC123" in result["title"]


# narrate()/get_or_create_narrative() are async (a real Mistral call from
# this environment was observed taking on the order of minutes -- calling
# it synchronously inside main.py's `async def` endpoint would block the
# whole FastAPI event loop for that entire duration). No pytest-asyncio
# dependency added just for this handful of tests -- asyncio.run() inside
# a plain sync test function is sufficient.


@patch("app.agent.narration.get_clients")
def test_narrate_returns_parsed_fields_on_valid_response(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_clients.return_value = [mock_client]

    result = asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert result["title"] == "Batch overdue"
    assert result["recommended_actions"] == [_VALID_ACTION]
    mock_client.chat.complete_async.assert_awaited_once()
    call_kwargs = mock_client.chat.complete_async.call_args.kwargs
    assert call_kwargs["response_format"] == {"type": "json_object"}
    assert call_kwargs["messages"][0]["role"] == "system"


# Retry-on-transient-API-error tests -- confirmed live that identical
# /agent/narrate calls sometimes fail with "403 tier_not_allowed" and
# sometimes succeed seconds apart (3 of 5 back-to-back real calls
# succeeded), meaning it's the API key's rate limit, not a real
# subscription restriction. asyncio.sleep is patched so these don't
# actually wait out the real backoff delay.

@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_retries_then_succeeds_on_transient_error(mock_get_clients, mock_sleep):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(side_effect=[
        Exception("403 tier_not_allowed"),
        Exception("403 tier_not_allowed"),
        _mock_mistral_response(_VALID_PAYLOAD),
    ])
    mock_get_clients.return_value = [mock_client]

    result = asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert result["title"] == "Batch overdue"
    assert mock_client.chat.complete_async.await_count == 3
    assert mock_sleep.await_count == 2  # slept between attempt 1->2 and 2->3, not after the final success


@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_raises_after_exhausting_all_retries(mock_get_clients, mock_sleep):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(side_effect=Exception("403 tier_not_allowed"))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(Exception, match="tier_not_allowed"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert mock_client.chat.complete_async.await_count == 3  # _MAX_ATTEMPTS, no more


@patch("app.agent.narration.get_clients")
def test_narrate_raises_on_missing_required_keys(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({"title": "Only a title"}))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(ValueError, match="missing required keys"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))


@patch("app.agent.narration.get_clients")
def test_narrate_raises_when_recommended_actions_not_a_list(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "T", "description": "D", "recommended_actions": "not a list",
    }))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(ValueError, match="recommended_actions must be a list"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))


@patch("app.agent.narration.get_clients")
def test_narrate_raises_when_recommended_actions_exceeds_three(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "T", "description": "D", "recommended_actions": [_VALID_ACTION] * 4,
    }))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(ValueError, match="recommended_actions must be a list of 1-3"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))


@patch("app.agent.narration.get_clients")
def test_narrate_raises_when_action_missing_why(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "T", "description": "D",
        "recommended_actions": [{"title": "Review", "description": "Review it."}],
    }))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(ValueError, match="must have"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))


@patch("app.agent.narration.get_clients")
def test_narrate_accepts_three_ranked_actions(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "T", "description": "D",
        "recommended_actions": [
            {"title": "First", "description": "Do first.", "why": "Most urgent."},
            {"title": "Second", "description": "Do second.", "why": "Also real."},
            {"title": "Third", "description": "Do third.", "why": "Still grounded."},
        ],
    }))
    mock_get_clients.return_value = [mock_client]

    result = asyncio.run(narrate({"signal_type": "investigation_case"}))

    assert len(result["recommended_actions"]) == 3
    assert result["recommended_actions"][0]["title"] == "First"


# Grounding check regression tests -- a real live call against real data
# (party_id "MER-20A71A0D") came back with the identifier truncated to
# "MER-20A71A" and vague filler ("totaling the given amount") in place of
# the real $243,864.42 it was given. These confirm that specific failure
# mode is now caught rather than silently cached.

@patch("app.agent.narration.get_clients")
def test_narrate_passes_when_identifier_present_verbatim(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "High anomaly for merchant MER-20A71A0D",
        "description": "Merchant MER-20A71A0D triggered a high anomaly score.",
        "recommended_actions": [
            {"title": "Review merchant", "description": "Review the merchant's recent activity.", "why": "Score is high."},
        ],
    }))
    mock_get_clients.return_value = [mock_client]

    result = asyncio.run(narrate({"signal_type": "fraud_anomaly", "party_id": "MER-20A71A0D"}))

    assert "MER-20A71A0D" in result["title"]


@patch("app.agent.narration.get_clients")
def test_narrate_raises_when_identifier_truncated(mock_get_clients):
    # The exact real bug: the trailing "0D" silently dropped.
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "High Anomaly in Merchant MER-20A71A Transactions",
        "description": "Four transactions totaling the given amount for merchant MER-20A71A triggered a high anomaly score in the specified time window.",
        "recommended_actions": [
            {"title": "Review merchant transactions urgently", "description": "Examine the four flagged transactions for potential fraud.", "why": "High anomaly score."},
        ],
    }))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(ValueError, match="grounding check"):
        asyncio.run(narrate({"signal_type": "fraud_anomaly", "party_id": "MER-20A71A0D"}))


@patch("app.agent.narration.get_clients")
def test_narrate_raises_when_identifier_missing_entirely(mock_get_clients):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({
        "title": "Reconciliation break detected",
        "description": "A transaction failed to reconcile.",
        "recommended_actions": [
            {"title": "Investigate", "description": "Review the ledger entries.", "why": "A break was detected."},
        ],
    }))
    mock_get_clients.return_value = [mock_client]

    with pytest.raises(ValueError, match="grounding check"):
        asyncio.run(narrate({"signal_type": "reconciliation_break", "transaction_id": "CHK-MTB-100029"}))


@patch("app.agent.narration.get_clients")
def test_narrate_skips_grounding_check_when_no_identifier_in_facts(mock_get_clients):
    # No identifier fact given (e.g. an unexpected/unknown signal_type) --
    # nothing to check, must not crash on a None lookup.
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_clients.return_value = [mock_client]

    result = asyncio.run(narrate({"signal_type": "something_unrecognized"}))

    assert result["title"] == "Batch overdue"


@patch("app.agent.narration.narrate", new_callable=AsyncMock)
def test_get_or_create_narrative_calls_narrate_once_then_caches(mock_narrate, db_session):
    mock_narrate.return_value = {"title": "T", "description": "D", "recommended_actions": [_VALID_ACTION]}

    async def run():
        first = await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        second = await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        return first, second

    first, second = asyncio.run(run())

    assert mock_narrate.await_count == 1  # second call hit the cache, no new API call
    assert first.id == second.id == "operational_issue:KEYBANK:42"
    assert first.recommended_actions == [_VALID_ACTION]
    assert first.recommended_action_title == _VALID_ACTION["title"]
    assert db_session.query(AgentNarrative).count() == 1


@patch("app.agent.narration.narrate", new_callable=AsyncMock)
def test_get_or_create_narrative_force_regenerates(mock_narrate, db_session):
    mock_narrate.side_effect = [
        {"title": "First", "description": "D", "recommended_actions": [{"title": "A", "description": "AD", "why": "W"}]},
        {"title": "Second", "description": "D2", "recommended_actions": [{"title": "A2", "description": "AD2", "why": "W2"}]},
    ]

    async def run():
        first = await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        first_title = first.title  # captured before the second (force) call mutates the same identity-mapped row in place
        second = await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1}, force=True)
        return first_title, second

    first_title, second = asyncio.run(run())

    assert first_title == "First"
    assert mock_narrate.await_count == 2
    assert second.title == "Second"
    assert second.recommended_actions == [{"title": "A2", "description": "AD2", "why": "W2"}]
    assert db_session.query(AgentNarrative).count() == 1  # updated in place, not duplicated


@patch("app.agent.narration.narrate", new_callable=AsyncMock)
def test_get_or_create_narrative_tenant_isolation_in_cache_key(mock_narrate, db_session):
    mock_narrate.return_value = {"title": "T", "description": "D", "recommended_actions": [_VALID_ACTION]}

    async def run():
        await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        await get_or_create_narrative(db_session, "operational_issue", "42", "MTB", {"x": 1})

    asyncio.run(run())

    assert mock_narrate.await_count == 2  # same reference_id, different tenant -- not a cache hit
    assert db_session.query(AgentNarrative).count() == 2


# -- Multi-key quota failover ------------------------------------------------
# The shared Mistral tier exhausts mid-session. With more than one key
# configured, narration should move to the next key rather than going dark.
# asyncio.sleep is patched throughout so these don't wait out real backoff.


def _client_raising(*side_effects):
    client = MagicMock()
    client.chat.complete_async = AsyncMock(side_effect=list(side_effects))
    return client


@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_fails_over_to_second_key_when_first_is_exhausted(mock_get_clients, mock_sleep):
    """First key is out of quota on every attempt; the second answers."""
    spent = _client_raising(*[Exception("429 rate limit exceeded")] * 3)
    healthy = _client_raising(_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_clients.return_value = [spent, healthy]

    result = asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert result["title"] == "Batch overdue"
    assert spent.chat.complete_async.await_count == 3    # exhausted its own retries first
    assert healthy.chat.complete_async.await_count == 1  # then the next key answered


@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_skips_invalid_key_immediately_without_backoff(mock_get_clients, mock_sleep):
    """A rejected credential can never work -- don't burn ~9s of backoff on
    it before trying the key that does.
    """
    bad = _client_raising(Exception("401 Unauthorized: invalid api key"))
    healthy = _client_raising(_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_clients.return_value = [bad, healthy]

    result = asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert result["title"] == "Batch overdue"
    assert bad.chat.complete_async.await_count == 1  # one attempt, then straight to the next key
    assert mock_sleep.await_count == 0               # no backoff burned on a dead credential


@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_raises_only_after_every_key_is_exhausted(mock_get_clients, mock_sleep):
    """All keys spent -> the caller still gets a real error, so the UI falls
    back to the plain-facts display instead of showing anything ungrounded.
    """
    first = _client_raising(*[Exception("429 rate limit")] * 3)
    second = _client_raising(*[Exception("429 rate limit")] * 3)
    mock_get_clients.return_value = [first, second]

    with pytest.raises(Exception, match="rate limit"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert first.chat.complete_async.await_count == 3
    assert second.chat.complete_async.await_count == 3


@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_does_not_touch_second_key_when_first_succeeds(mock_get_clients, mock_sleep):
    """Failover, not load balancing: the primary key is always preferred, so
    a spare key stays untouched while the first one works.
    """
    primary = _client_raising(_mock_mistral_response(_VALID_PAYLOAD))
    spare = _client_raising(_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_clients.return_value = [primary, spare]

    asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert primary.chat.complete_async.await_count == 1
    assert spare.chat.complete_async.await_count == 0


@patch("app.agent.narration.asyncio.sleep", new_callable=AsyncMock)
@patch("app.agent.narration.get_clients")
def test_narrate_recovers_on_a_later_attempt_of_the_same_key(mock_get_clients, mock_sleep):
    """The common real shape of "403 tier_not_allowed" here is a transient
    burst limit, so a key gets its retries before being abandoned -- a spare
    key is not consumed for something that resolves on its own.
    """
    flaky = _client_raising(
        Exception("403 tier_not_allowed"),
        _mock_mistral_response(_VALID_PAYLOAD),
    )
    spare = _client_raising(_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_clients.return_value = [flaky, spare]

    result = asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert result["title"] == "Batch overdue"
    assert flaky.chat.complete_async.await_count == 2
    assert spare.chat.complete_async.await_count == 0
