"""Manual precision/accuracy spot-check: for each Fraud category (Section
2.1 -- New Payee Risk, Funnel Account, Velocity, Structuring) and each
Operational category (Section 2.2 -- Network Timeout, Batch Never
Settles, Duplicate Payment, Format Rejection), pulls the single
top-flagged item and prints the RAW canonical_events rows behind it, so a
human can judge by eye whether the flag actually makes sense.

This is not a metric -- there's no ground-truth fraud label anywhere in
this dataset (the whole point of this engine is unsupervised detection),
so there's no accuracy/precision number to compute. This is face-validity
checking: does the raw evidence actually support what the detector claims?

Usage: python -m scripts.spot_check_findings [tenant_bank_id]
       (defaults to MERIDIAN_TRUST_BANK if omitted; run from the repo root)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.anomaly.funnel import compute_beneficiary_snapshots
from app.anomaly.models import BeneficiarySnapshot, EntitySnapshot
from app.anomaly.timeseries import score_drift
from app.database import SessionLocal
from app.models import CanonicalEvent
from app.operations.drift import detect_timeout_spikes
from app.operations.duplicate_payment import detect_duplicate_payments
from app.operations.format_rejection import detect_format_rejections
from app.operations.models import OperationalIssue
from app.operations.rules import detect_unsettled_batches

DEFAULT_TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"


def _header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _print_event(e: CanonicalEvent) -> None:
    print(
        f"    {e.transaction_id} | {e.transaction_occurred_at} | amount={e.amount} | "
        f"payer={e.payer_name} | payee={e.payee_name} | rail={e.rail_type}"
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _events_for_snapshot_row(db, row: EntitySnapshot) -> list[CanonicalEvent]:
    """Raw events actually behind one EntitySnapshot row -- scoped to its
    window when it's a WEEKLY row (a merchant has many rows, one per
    week; pulling the party's whole history would show transactions that
    have nothing to do with the specific ratio being checked). TO_DATE
    rows have no window, so party_id alone is the full scope already.
    """
    query = db.query(CanonicalEvent).filter(
        (CanonicalEvent.merchant_id == row.party_id) | (CanonicalEvent.individual_id == row.party_id)
    )
    events = query.all()
    if row.window_type == "WEEKLY" and row.window_start is not None:
        # SQLite doesn't actually persist tzinfo even for a
        # DateTime(timezone=True) column -- what comes back is naive, so
        # normalize to UTC (same convention _parse_ts already applies)
        # before comparing.
        window_start = row.window_start if row.window_start.tzinfo else row.window_start.replace(tzinfo=timezone.utc)
        window_end = row.window_end if row.window_end.tzinfo else row.window_end.replace(tzinfo=timezone.utc)
        events = [
            e for e in events
            if (ts := _parse_ts(e.transaction_occurred_at)) is not None and window_start <= ts < window_end
        ]
    return events


def main() -> None:
    tenant_bank_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TENANT_BANK_ID
    db = SessionLocal()
    try:
        # --- Structuring ---
        _header("STRUCTURING -- top near_threshold_ratio entity")
        rows = db.query(EntitySnapshot).filter_by(tenant_bank_id=tenant_bank_id).all()
        top = max((r for r in rows if r.near_threshold_ratio), key=lambda r: r.near_threshold_ratio, default=None)
        if top:
            window_desc = f"week {top.window_start.date()}-{top.window_end.date()}" if top.window_type == "WEEKLY" else "to-date"
            print(f"  {top.party_id} near_threshold_ratio={top.near_threshold_ratio:.3f} ({window_desc}, {top.transaction_count} txns in this row)")
            events = _events_for_snapshot_row(db, top)
            print(f"  raw transaction amounts THIS ROW ACTUALLY COVERS (check how many sit just under $10,000/$3,000):")
            for e in sorted(events, key=lambda e: e.transaction_occurred_at or ""):
                print(f"    {e.transaction_id} | {e.transaction_occurred_at} | amount={e.amount}")

        # --- New Payee Risk ---
        _header("NEW PAYEE RISK -- top new_counterparty_ratio entity")
        top = max((r for r in rows if r.new_counterparty_ratio), key=lambda r: r.new_counterparty_ratio, default=None)
        if top:
            window_desc = f"week {top.window_start.date()}-{top.window_end.date()}" if top.window_type == "WEEKLY" else "to-date"
            print(f"  {top.party_id} new_counterparty_ratio={top.new_counterparty_ratio:.3f} ({window_desc})")
            events = _events_for_snapshot_row(db, top)
            print("  chronological payee sequence THIS ROW ACTUALLY COVERS (check how many are first-time names):")
            for e in sorted(events, key=lambda e: e.transaction_occurred_at or ""):
                print(f"    {e.transaction_occurred_at} | payee={e.payee_name}")

        # --- Funnel Account ---
        _header("FUNNEL ACCOUNT -- top flagged beneficiary")
        compute_beneficiary_snapshots(db, tenant_bank_id=tenant_bank_id)
        top_b = (
            db.query(BeneficiarySnapshot)
            .filter_by(tenant_bank_id=tenant_bank_id, funnel_flag=True)
            .order_by(BeneficiarySnapshot.distinct_senders.desc())
            .first()
        )
        if top_b:
            print(f"  {top_b.beneficiary_key}: {top_b.funnel_reason}")
            events = db.query(CanonicalEvent).filter(CanonicalEvent.payee_name == top_b.beneficiary_key).all()
            print("  raw payments to this beneficiary (check: multiple different senders?):")
            for e in sorted(events, key=lambda e: e.transaction_occurred_at or ""):
                _print_event(e)
        else:
            print("  (no beneficiary currently flagged)")

        # --- Velocity ---
        _header("VELOCITY -- top timeseries_drift_score row")
        score_drift(db, tenant_bank_id=tenant_bank_id)
        top_drift = (
            db.query(EntitySnapshot)
            .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.timeseries_drift_score.isnot(None))
            .order_by(EntitySnapshot.timeseries_drift_score.desc())
            .first()
        )
        if top_drift:
            print(f"  {top_drift.party_id} week={top_drift.window_start.date()} drift_score={top_drift.timeseries_drift_score:.1f}")
            all_weeks = (
                db.query(EntitySnapshot)
                .filter_by(party_id=top_drift.party_id, tenant_bank_id=tenant_bank_id)
                .order_by(EntitySnapshot.window_start)
                .all()
            )
            print("  this merchant's full weekly sequence (check: does the flagged week actually stand out?):")
            for w in all_weeks:
                marker = " <-- FLAGGED" if w.id == top_drift.id else ""
                print(f"    week={w.window_start.date()} txn_count={w.transaction_count} amount_total={w.amount_total:.2f}{marker}")

        # --- Batch Never Settles ---
        _header("BATCH NEVER SETTLES -- top flagged batch")
        detect_unsettled_batches(db, tenant_bank_id=tenant_bank_id)
        top_batch = (
            db.query(OperationalIssue)
            .filter_by(tenant_bank_id=tenant_bank_id, issue_type="BATCH_NOT_SETTLED")
            .order_by(OperationalIssue.detected_at.desc())
            .first()
        )
        if top_batch:
            print(f"  {top_batch.reference_id}: {top_batch.details}")
            events = db.query(CanonicalEvent).filter(CanonicalEvent.batch_id == top_batch.reference_id).all()
            print("  raw transactions in this batch (check: file_reached_settlement/expected_settlement_at):")
            for e in events:
                print(f"    {e.transaction_id} | file_reached_settlement={e.file_reached_settlement} | expected={e.expected_settlement_at}")
        else:
            print("  (no batch currently flagged)")

        # --- Network/Processor Timeout ---
        _header("NETWORK TIMEOUT -- top flagged week")
        detect_timeout_spikes(db, tenant_bank_id=tenant_bank_id)
        top_timeout = (
            db.query(OperationalIssue)
            .filter_by(tenant_bank_id=tenant_bank_id, issue_type="NETWORK_TIMEOUT_SPIKE")
            .order_by(OperationalIssue.severity_score.desc())
            .first()
        )
        if top_timeout:
            print(f"  {top_timeout.reference_id} week={top_timeout.window_start.date()} severity={top_timeout.severity_score:.1f}")
            all_weeks = (
                db.query(EntitySnapshot)
                .filter_by(party_id=top_timeout.reference_id, tenant_bank_id=tenant_bank_id)
                .order_by(EntitySnapshot.window_start)
                .all()
            )
            print("  this merchant's full weekly timeout_ratio sequence (check: does the flagged week stand out?):")
            for w in all_weeks:
                marker = " <-- FLAGGED" if w.window_start == top_timeout.window_start else ""
                print(f"    week={w.window_start.date()} timeout_ratio={w.timeout_ratio}{marker}")
        else:
            print("  (none flagged -- timeout_ratio may be constant/zero in this dataset)")

        # --- Duplicate Payment ---
        _header("DUPLICATE PAYMENT -- top flagged pair")
        detect_duplicate_payments(db, tenant_bank_id=tenant_bank_id)
        top_dup = (
            db.query(OperationalIssue)
            .filter_by(tenant_bank_id=tenant_bank_id, issue_type="DUPLICATE_PAYMENT")
            .order_by(OperationalIssue.detected_at.desc())
            .first()
        )
        if top_dup:
            print(f"  {top_dup.details}")
            for txn_id in top_dup.details.get("settled_transaction_ids", []):
                e = db.query(CanonicalEvent).filter_by(transaction_id=txn_id).first()
                if e:
                    _print_event(e)
        else:
            print("  (none flagged)")

        # --- Format Rejection ---
        _header("FORMAT REJECTION -- top flagged transaction")
        detect_format_rejections(db, tenant_bank_id=tenant_bank_id)
        top_reject = (
            db.query(OperationalIssue)
            .filter_by(tenant_bank_id=tenant_bank_id, issue_type="FORMAT_REJECTION")
            .order_by(OperationalIssue.detected_at.desc())
            .first()
        )
        if top_reject:
            print(f"  {top_reject.reference_id}: {top_reject.details}")
        else:
            print("  (none flagged)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
