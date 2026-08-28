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


def _rescale_to_0_100(raw_scores: dict[int, float]) -> dict[int, float]:
    """Min-max rescale within the set of weeks actually checked for ONE
    tenant -- not a fixed multiplier, and not pooled across tenants (same
    reasoning as timeseries.py's tenant-relative rescale: one tenant's
    biggest deviation shouldn't dictate another tenant's severity scale).
    Rescaled over every checked week, not just the flagged subset, so a
    just-barely-flagged week doesn't misleadingly land at severity 0.
    """
    if not raw_scores:
        return {}
    values = list(raw_scores.values())
    lo, hi = min(values), max(values)
    if hi == lo:
        return {row_id: 100.0 for row_id in raw_scores}
    return {row_id: (v - lo) / (hi - lo) * 100 for row_id, v in raw_scores.items()}


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

    raw_z: dict[int, float] = {}
    row_by_id: dict[int, EntitySnapshot] = {}
    for party_rows in by_party.values():
        party_rows.sort(key=lambda r: r.window_start)
        for i, row in enumerate(party_rows):
            if row.timeout_ratio is None:
                continue
            prior_values = [r.timeout_ratio for r in party_rows[:i] if r.timeout_ratio is not None]
            z = _zscore(row.timeout_ratio, prior_values)
            if z is None:
                continue
            raw_z[row.id] = z
            row_by_id[row.id] = row

    abs_z_by_tenant: dict[str, dict[int, float]] = defaultdict(dict)
    for row_id, z in raw_z.items():
        abs_z_by_tenant[row_by_id[row_id].tenant_bank_id][row_id] = abs(z)

    severity_by_id: dict[int, float] = {}
    for tenant_scores in abs_z_by_tenant.values():
        severity_by_id.update(_rescale_to_0_100(tenant_scores))

    flagged = 0
    for row_id, z in raw_z.items():
        if abs(z) < _SEVERITY_FLAG_THRESHOLD:
            continue
        row = row_by_id[row_id]
        prior_count = sum(
            1 for r in by_party[row.party_id] if r.window_start < row.window_start and r.timeout_ratio is not None
        )
        db.add(OperationalIssue(
            issue_type="NETWORK_TIMEOUT_SPIKE",
            tenant_bank_id=row.tenant_bank_id,
            reference_type="PARTY",
            reference_id=row.party_id,
            window_start=row.window_start,
            window_end=row.window_end,
            severity_score=severity_by_id[row_id],
            details={"timeout_ratio": row.timeout_ratio, "z_score": z, "prior_weeks_used": prior_count},
        ))
        flagged += 1

    db.commit()

    return {"weeks_checked": len(raw_z), "weeks_flagged": flagged}
