"""Batched replacement for the "one CanonicalEvent query per
OperationalIssue/ReconciliationBreak row" pattern duplicated across this
codebase (app/dashboard.py, app/exposure.py, app/priority.py,
app/investigation/trend.py, app/investigation/sla.py,
app/investigation/cases.py) -- confirmed via direct latency measurement
to be the dominant real cost behind slow page loads, once WAL mode
(app/database.py) ruled out SQLite lock contention as the bottleneck.

One query per CanonicalEventLookup instance (built once per request/
top-level call), reused for every row that instance's caller needs to
join against, instead of a query per row.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from .models import CanonicalEvent


class CanonicalEventLookup:
    def __init__(self, db: Session, tenant_bank_id: str):
        self._by_transaction_id: dict[str, list[CanonicalEvent]] = defaultdict(list)
        self._by_batch_id: dict[str, list[CanonicalEvent]] = defaultdict(list)
        self._by_rail_transaction: dict[tuple[str, str], CanonicalEvent] = {}

        events = db.query(CanonicalEvent).filter(CanonicalEvent.tenant_bank_id == tenant_bank_id).all()
        for event in events:
            if event.transaction_id:
                self._by_transaction_id[event.transaction_id].append(event)
                if event.rail_type:
                    self._by_rail_transaction.setdefault((event.rail_type, event.transaction_id), event)
            if event.batch_id:
                self._by_batch_id[event.batch_id].append(event)

    def all_by_transaction_id(self, transaction_id: str | None) -> list[CanonicalEvent]:
        return self._by_transaction_id.get(transaction_id, []) if transaction_id else []

    def first_by_transaction_id(self, transaction_id: str | None) -> CanonicalEvent | None:
        matches = self.all_by_transaction_id(transaction_id)
        return matches[0] if matches else None

    def all_by_batch_id(self, batch_id: str | None) -> list[CanonicalEvent]:
        return self._by_batch_id.get(batch_id, []) if batch_id else []

    def first_by_batch_id(self, batch_id: str | None) -> CanonicalEvent | None:
        matches = self.all_by_batch_id(batch_id)
        return matches[0] if matches else None

    def by_rail_and_transaction_id(self, rail_type: str | None, transaction_id: str | None) -> CanonicalEvent | None:
        if not rail_type or not transaction_id:
            return None
        return self._by_rail_transaction.get((rail_type, transaction_id))
