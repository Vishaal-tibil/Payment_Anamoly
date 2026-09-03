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

from sqlalchemy.orm import Session, defer

from .models import CanonicalEvent

# CanonicalEvent has 46 columns, 9 of them JSON. Deserializing those 9 was
# ~97% of the cost of building this lookup (measured: 164ms -> 29ms per
# build, 5.6x, from ~28.6k JSON-decode calls per request), and nothing
# that reads through this lookup touches any of them -- the only JSON
# consumers (resolution.py, anomaly/features.py, operations/
# format_rejection.py, reconciliation/breaks.py, canonical_store.py) all
# run their own queries. Deferring rather than selecting an explicit
# column list keeps every scalar attribute eagerly loaded, so callers
# need no changes and no deferred-column access can turn into an N+1.
#
# If a future caller genuinely needs a JSON column off this lookup, load
# it with its own query rather than removing this defer -- that would
# silently put the 5.6x back on every dashboard endpoint.
_JSON_COLUMNS = tuple(
    c.name for c in CanonicalEvent.__table__.columns if "JSON" in str(c.type).upper()
)


def JSON_COLUMN_DEFERRALS() -> list:
    """Shared with any other read path that loads whole CanonicalEvent
    rows but never reads their JSON columns (app/dashboard.py's
    get_rail_stats). Built fresh per call -- a SQLAlchemy loader option
    is bound to the query it's applied to, so a module-level list would
    be reused across queries.
    """
    return [defer(getattr(CanonicalEvent, name)) for name in _JSON_COLUMNS]


class CanonicalEventLookup:
    def __init__(self, db: Session, tenant_bank_id: str):
        self._by_transaction_id: dict[str, list[CanonicalEvent]] = defaultdict(list)
        self._by_batch_id: dict[str, list[CanonicalEvent]] = defaultdict(list)
        self._by_rail_transaction: dict[tuple[str, str], CanonicalEvent] = {}

        events = (
            db.query(CanonicalEvent)
            .options(*JSON_COLUMN_DEFERRALS())
            .filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
            .all()
        )
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
