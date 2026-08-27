"""Rolling z-score: Network/Processor Timeout spike detection.

Reads EntitySnapshot.timeout_ratio -- Track A's existing per-merchant-
per-week rate (app/anomaly/models.py) -- read-only; this package owns
operational_issues, never EntitySnapshot. z-scores each merchant's
current week against that same merchant's own prior weeks, reusing
timeseries.py's _zscore() rather than reimplementing it.

"Is this rate normal" isn't a fact the way file_reached_settlement is --
it needs a baseline to compare against, hence the (small, untrained)
statistics here, unlike rules.py's deterministic check.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import EntitySnapshot
from ..anomaly.timeseries import _zscore
from .models import OperationalIssue

# |z-score| beyond this counts as a genuine spike worth a human's
# attention, not routine week-to-week noise.
_SEVERITY_FLAG_THRESHOLD = 2.0


def _severity_score(z: float) -> float:
    """0-100 rescale: the flag threshold itself lands at 40, scaling up
    linearly and capping at 100 -- a simple, documented v1 choice, same
    spirit as Track C's own min-max rescale.
    """
    return min(100.0, abs(z) * 20.0)


def detect_timeout_spikes(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds this tenant's NETWORK_TIMEOUT_SPIKE rows in
    operational_issues. Fully derived -- each run deletes and rebuilds
    the rows it covers.
    """
    query = db.query(EntitySnapshot).filter(
        EntitySnapshot.segment == "MERCHANT", EntitySnapshot.window_type == "WEEKLY",
    )
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    weekly_rows = query.all()

    by_party: dict[str, list[EntitySnapshot]] = defaultdict(list)
    for row in weekly_rows:
        by_party[row.party_id].append(row)

    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "NETWORK_TIMEOUT_SPIKE")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    weeks_checked = 0
    flagged = 0
    for party_rows in by_party.values():
        party_rows.sort(key=lambda r: r.window_start)
        for i, row in enumerate(party_rows):
            if row.timeout_ratio is None:
                continue
            prior_values = [r.timeout_ratio for r in party_rows[:i] if r.timeout_ratio is not None]
            z = _zscore(row.timeout_ratio, prior_values)
            if z is None:
                continue
            weeks_checked += 1
            if abs(z) >= _SEVERITY_FLAG_THRESHOLD:
                db.add(OperationalIssue(
                    issue_type="NETWORK_TIMEOUT_SPIKE",
                    tenant_bank_id=row.tenant_bank_id,
                    reference_type="PARTY",
                    reference_id=row.party_id,
                    window_start=row.window_start,
                    window_end=row.window_end,
                    severity_score=_severity_score(z),
                    details={"timeout_ratio": row.timeout_ratio, "z_score": z, "prior_weeks_used": len(prior_values)},
                ))
                flagged += 1

    db.commit()

    return {"weeks_checked": weeks_checked, "weeks_flagged": flagged}
