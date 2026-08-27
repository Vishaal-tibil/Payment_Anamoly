from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.models import AgentNarrative
from app.agent.narration import (
    facts_for_entity_snapshot,
    facts_for_operational_issue,
    facts_for_reconciliation_break,
    get_or_create_narrative,
    narrate,
)
from app.anomaly.models import EntitySnapshot
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak


def _mock_mistral_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return response


_VALID_PAYLOAD = {
    "title": "Batch overdue",
    "description": "This batch has not settled.",
    "recommended_action_title": "Review batch",
    "recommended_action_description": "Check the batch manually.",
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


# narrate()/get_or_create_narrative() are async (a real Mistral call from
# this environment was observed taking on the order of minutes -- calling
# it synchronously inside main.py's `async def` endpoint would block the
# whole FastAPI event loop for that entire duration). No pytest-asyncio
# dependency added just for this handful of tests -- asyncio.run() inside
# a plain sync test function is sufficient.


@patch("app.agent.narration.get_client")
def test_narrate_returns_parsed_fields_on_valid_response(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response(_VALID_PAYLOAD))
    mock_get_client.return_value = mock_client

    result = asyncio.run(narrate({"signal_type": "operational_issue"}))

    assert result["title"] == "Batch overdue"
    assert result["recommended_action_title"] == "Review batch"
    mock_client.chat.complete_async.assert_awaited_once()
    call_kwargs = mock_client.chat.complete_async.call_args.kwargs
    assert call_kwargs["response_format"] == {"type": "json_object"}
    assert call_kwargs["messages"][0]["role"] == "system"


@patch("app.agent.narration.get_client")
def test_narrate_raises_on_missing_required_keys(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.complete_async = AsyncMock(return_value=_mock_mistral_response({"title": "Only a title"}))
    mock_get_client.return_value = mock_client

    with pytest.raises(ValueError, match="missing required keys"):
        asyncio.run(narrate({"signal_type": "operational_issue"}))


@patch("app.agent.narration.narrate", new_callable=AsyncMock)
def test_get_or_create_narrative_calls_narrate_once_then_caches(mock_narrate, db_session):
    mock_narrate.return_value = {
        "title": "T", "description": "D",
        "recommended_action_title": "A", "recommended_action_description": "AD",
    }

    async def run():
        first = await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        second = await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        return first, second

    first, second = asyncio.run(run())

    assert mock_narrate.await_count == 1  # second call hit the cache, no new API call
    assert first.id == second.id == "operational_issue:KEYBANK:42"
    assert db_session.query(AgentNarrative).count() == 1


@patch("app.agent.narration.narrate", new_callable=AsyncMock)
def test_get_or_create_narrative_force_regenerates(mock_narrate, db_session):
    mock_narrate.side_effect = [
        {"title": "First", "description": "D", "recommended_action_title": "A", "recommended_action_description": "AD"},
        {"title": "Second", "description": "D2", "recommended_action_title": "A2", "recommended_action_description": "AD2"},
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
    assert db_session.query(AgentNarrative).count() == 1  # updated in place, not duplicated


@patch("app.agent.narration.narrate", new_callable=AsyncMock)
def test_get_or_create_narrative_tenant_isolation_in_cache_key(mock_narrate, db_session):
    mock_narrate.return_value = {
        "title": "T", "description": "D",
        "recommended_action_title": "A", "recommended_action_description": "AD",
    }

    async def run():
        await get_or_create_narrative(db_session, "operational_issue", "42", "KEYBANK", {"x": 1})
        await get_or_create_narrative(db_session, "operational_issue", "42", "MTB", {"x": 1})

    asyncio.run(run())

    assert mock_narrate.await_count == 2  # same reference_id, different tenant -- not a cache hit
    assert db_session.query(AgentNarrative).count() == 2
