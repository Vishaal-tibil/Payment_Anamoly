# Analyst Screen — API Reference

Scope: only the endpoints the **Analyst persona** (Investigation Queue,
Insights > Overview/Anomalies/Payment Rails/Detection Performance) needs.
Head-of-Operations-only endpoints (`/health/*`, `/review/*`,
`/dashboard/senior-overview`, `/dashboard/anomaly-detection-categories`,
`/merchants*`, `/individuals*`, `/features*`, `/ingest/file`,
`/resolve/parties`) are intentionally excluded.

Two sections below:
- **Existing** — already implemented, live on the backend today, response
  shapes pulled directly from the current code (not guessed) or, where
  marked, real values from an actual run against Meridian data.
- **Genuinely blocked** — gaps identified against the frontend mockups
  that no new endpoint can fix, because the underlying data/concept
  doesn't exist anywhere in the schema yet.

All endpoints are scoped to one tenant via a required `tenant_bank_id`
query parameter (GET) or body field (POST), same convention throughout.

---

## Existing endpoints

### `GET /dashboard/overview`
Real-time aggregate counts for the whole tenant.

**Query params**
| Name | Type | Required | Description |
|---|---|---|---|
| `tenant_bank_id` | string | yes | Tenant to scope to |

**Response 200**
```json
{
  "total_transactions": 520,
  "settled_transactions": 478,
  "settlement_rate": 0.9192307692307692,
  "total_merchants": 10,
  "total_individuals": 91,
  "date_range_start": "2026-06-25 00:00:00",
  "date_range_end": "2026-08-23T18:23:19Z",
  "anomaly_band_counts": { "Normal": 98, "Low-Medium": 68, "Critical": 2, "High": 6 },
  "operational_issue_counts": { "DUPLICATE_PAYMENT": 18, "FORMAT_REJECTION": 19, "FORMAT_REJECTION_SPIKE": 7, "BATCH_NOT_SETTLED": 17 },
  "reconciliation_break_counts": { "CONFIRMED_BREAK": 30, "PROVISIONAL_VARIANCE": 3 }
}
```
| Field | Type | Description |
|---|---|---|
| `total_transactions` | int | All canonical events for this tenant |
| `settled_transactions` | int | Count with `status == "SETTLED"` |
| `settlement_rate` | float\|null | `settled_transactions / total_transactions` |
| `total_merchants` / `total_individuals` | int | Resolved party counts |
| `date_range_start` / `date_range_end` | string (ISO 8601) | Min/max `transaction_occurred_at` |
| `anomaly_band_counts` | object\<string,int\> | Count of `EntitySnapshot` rows per `anomaly_band` |
| `operational_issue_counts` | object\<string,int\> | Count of `OperationalIssue` rows per `issue_type` |
| `reconciliation_break_counts` | object\<string,int\> | Count of `ReconciliationBreak` rows per `detection_type` |

---

### `GET /dashboard/rails`
Per-rail transaction/settlement/reconciliation stats.

**Query params**: `tenant_bank_id` (string, required)

**Response 200**
```json
{
  "rails": [
    {
      "rail_type": "ACH",
      "transaction_count": 140,
      "settled_count": 128,
      "settlement_rate": 0.9142857142857143,
      "total_amount": 512340.22,
      "reconciliation_break_count": 4
    }
  ]
}
```

---

### `GET /dashboard/exposure`
Real-dollar exposure/mitigation views, combined in one call.

**Query params**: `tenant_bank_id` (string, required)

**Response 200** — object with six sub-sections:
```json
{
  "by_domain": [ { "domain": "Fraud", "exposure": 1700000.0 } ],
  "trend": [ { "timestamp": "2026-08-23T10:00:00Z", "exposure": 3110000.0 } ],
  "mitigation": { "mitigated_amount": 380000.0, "total_exposure": 3110000.0, "mitigation_rate": 0.122 },
  "mitigation_by_domain": [ { "domain": "Fraud", "residual": 1200000.0, "mitigated": 500000.0 } ],
  "payment_value_by_rail": [ { "rail_type": "RTP", "value": 14200000.0 } ],
  "normalcy": { "expected_volume": 20000, "actual_volume": 21300 }
}
```
Exact field names per sub-section should be pulled from `app/exposure.py` when scaffolding — six independently-computed functions (`get_exposure_by_domain`, `get_exposure_trend`, `get_mitigation_progress`, `get_mitigation_progress_by_domain`, `get_payment_value_by_rail`, `get_payment_normalcy`), combined only at the endpoint layer.

**Note**: `trend` is weekly totals, not hourly/intraday — see "Genuinely blocked" below for why an intraday version isn't buildable yet. Exposure is broken down by *domain* (Fraud/Operational/Reconciliation) here, not by *rail* — see the Planned section's `/dashboard/exposure-by-rail`.

---

### `GET /dashboard/detection-performance`
Review-grounded detection quality metrics, **including the four fields below that were previously listed as "planned" — all four are now live**, verified against real data.

**Query params**: `tenant_bank_id` (string, required)

**Response 200**
```json
{
  "coverage_rate": 0.98,
  "covered_transactions": 510,
  "total_transactions": 520,
  "coverage_by_rail": [ { "rail_type": "ACH", "coverage_rate": 0.97 } ],
  "exposure_identified_early": 4552.13,
  "provisional_variance_count": 3,
  "confirmation_rate": 0.947,
  "false_positive_rate": 0.053,
  "reviewed_count": 38,
  "pending_count": 59,
  "confirmed_count": 36,
  "dismissed_count": 2,
  "median_detection_time_seconds": 3623866.557597,
  "detection_volume_by_category": [
    { "category": "Operational", "count": 61, "percentage": 0.2276 },
    { "category": "Fraud", "count": 174, "percentage": 0.6493 },
    { "category": "Reconciliation", "count": 33, "percentage": 0.1231 }
  ],
  "detection_performance_by_rail": [
    { "rail_type": "ACH", "success_rate": 0.9029, "median_detection_latency_seconds": 4178559.1 }
  ],
  "new_patterns_detected": 6,
  "quality_trend": [ { "reviewed_at": "2026-08-01T09:00:00Z", "cumulative_confirmation_rate": 0.9, "cumulative_false_positive_rate": 0.1 } ],
  "pattern_mix": [ { "category": "Fraud", "count": 29 } ]
}
```
| Field | Type | Description |
|---|---|---|
| `coverage_rate` | float\|null | Fraction of transactions belonging to a resolved party |
| `exposure_identified_early` | float | Real $ sum of `PROVISIONAL_VARIANCE` breaks caught ahead of the source's own verdict |
| `confirmation_rate` / `false_positive_rate` | float\|null | Real, but only over `reviewed_count` — grows more meaningful over time |
| `median_detection_time_seconds` | float\|null | Median delta between transaction occurrence and `detected_at`, over signals with a resolvable transaction timestamp only. **Caveat**: `detected_at` reflects whenever the compute batch last ran, not a live production cadence — large/odd-looking values are a pilot-data artifact, not a bug |
| `detection_volume_by_category` | array | % split across Operational/Fraud/Reconciliation — reclassifies existing counts, not new data |
| `detection_performance_by_rail` | array | Per rail: fraction of that rail's transactions NOT referenced by any issue/break, + median detection latency |
| `new_patterns_detected` | int | Count of distinct categories whose earliest `detected_at` falls in the most recent 7 days of *this dataset's own* timeline (not wall-clock) |
| `quality_trend` | array | One point per real review action, chronological |
| `pattern_mix` | array | Category breakdown of detected patterns |

---

### `GET /anomaly/snapshots`
Fraud/anomaly engine output — one row per merchant/individual per window.

**Query params**
| Name | Type | Required | Description |
|---|---|---|---|
| `tenant_bank_id` | string | yes | |
| `party_id` | string | no | Filter to one party |
| `segment` | string | no | `MERCHANT` \| `INDIVIDUAL` |
| `split` | string | no | train/test split filter |
| `limit` | int | no (default 50) | |
| `offset` | int | no (default 0) | |

**Response 200**
```json
{
  "total": 174,
  "snapshots": [
    {
      "id": 101, "matched_categories": ["New Payee Risk"], "party_id": "MER-...", "party_type": "MERCHANT",
      "tenant_bank_id": "MERIDIAN_TRUST_BANK", "segment": "MERCHANT", "window_type": "WEEKLY",
      "window_start": "2026-07-13T00:00:00Z", "window_end": "2026-07-20T00:00:00Z",
      "transaction_count": 4, "amount_total": 243864.42, "amount_avg": 60966.1, "amount_median": 58000.0,
      "amount_std": 12000.0, "unique_counterparties": 3, "new_counterparty_ratio": 0.75, "retry_ratio": 0.0,
      "avg_response_time_ms": 340.0, "timeout_ratio": 0.0, "format_reject_ratio": 0.0,
      "rails_used": ["RTP"], "account_age_days": 210.5, "split": "train", "computed_at": "2026-08-24T00:00:00Z",
      "isolation_forest_score": 62.1, "cluster_id": 2, "cluster_changed": true, "timeseries_drift_score": 100.0,
      "final_anomaly_score": 81.4, "anomaly_band": "Critical"
    }
  ]
}
```

### `GET /anomaly/snapshots/{snapshot_id}`
Single-row detail, same shape as one item in the list above (`matched_categories` computed the same way, against this row's segment population). `404` if `snapshot_id` doesn't exist.

---

### `GET /anomaly/beneficiary-snapshots`
Funnel Account signal — grouped by **receiver**, not sender.

**Query params**: `tenant_bank_id` (required), `beneficiary_key` (optional), `limit`, `offset`

**Response 200**
```json
{
  "total": 6,
  "snapshots": [
    {
      "id": 55, "beneficiary_key": "ACCT-1", "beneficiary_name": "J. Smith", "tenant_bank_id": "MERIDIAN_TRUST_BANK",
      "window_start": "2026-08-01T00:00:00Z", "window_end": "2026-08-08T00:00:00Z",
      "transaction_count": 20, "amount_total": 45000.0, "distinct_senders": 20, "distinct_new_senders": 19,
      "new_sender_ratio": 0.95, "sender_party_types": ["INDIVIDUAL", "MERCHANT"],
      "funnel_drift_score": 100.0, "matched_categories": ["Funnel Account"], "computed_at": "2026-08-24T00:00:00Z"
    }
  ]
}
```

### `GET /anomaly/beneficiary-snapshots/{snapshot_id}`
Single-row detail, same shape as one list item. `404` if not found.

---

### `GET /operations/issues`
Operational Issues engine — all 5 issue types in one flat feed.

**Query params**: `tenant_bank_id` (required), `issue_type` (optional filter: `NETWORK_TIMEOUT_SPIKE` \| `BATCH_NOT_SETTLED` \| `DUPLICATE_PAYMENT` \| `FORMAT_REJECTION` \| `FORMAT_REJECTION_SPIKE`), `limit`, `offset`

**Response 200**
```json
{
  "total": 61,
  "issues": [
    {
      "id": 12, "issue_type": "BATCH_NOT_SETTLED", "tenant_bank_id": "MERIDIAN_TRUST_BANK",
      "reference_type": "BATCH", "reference_id": "CL-MTB-20260807-4",
      "window_start": null, "window_end": null, "severity_score": null,
      "details": { "rail_type": "CHEQUE", "expected_settlement_at": "2026-08-08T00:33:54+00:00", "total_transactions": 1, "unsettled_transactions": 1, "days_overdue": 20 },
      "detected_at": "2026-08-28T00:00:00Z"
    }
  ]
}
```
Note: `reference_id` means different things per `issue_type` — `transaction_id` for duplicates/format rejections, `batch_id` for stuck batches, `party_id` for rate-spike issues. `severity_score` (0-100) is only populated for the two z-score-based types; null for the deterministic ones.

### `GET /operations/issues/{issue_id}`
Single-row detail, same shape as one list item. `404` if not found.

---

### `GET /reconciliation/breaks`
Reconciliation engine — one row per transaction with a detected break.

**Query params**: `tenant_bank_id` (required), `detection_type` (optional: `CONFIRMED_BREAK` \| `PROVISIONAL_VARIANCE`), `limit`, `offset`

**Response 200**
```json
{
  "total": 33,
  "breaks": [
    {
      "id": 7, "tenant_bank_id": "MERIDIAN_TRUST_BANK", "transaction_id": "FN-MTB-100323", "rail_type": "FEDNOW",
      "detection_type": "CONFIRMED_BREAK", "source_reconciliation_status": "BREAK", "variance_amount": 24.63,
      "amount": 15000.0, "details": {}, "detected_at": "2026-08-27T00:00:00Z"
    }
  ]
}
```

### `GET /reconciliation/breaks/{break_id}`
Single-row detail, same shape as one list item. `404` if not found.

---

### `POST /agent/narrate`
LLM-generated narrative + single recommended action for one signal. **~2 minutes real latency** — user-triggered only, never on page load. Cached: repeat calls with `force: false` return the same row.

**Request body**
```json
{ "signal_type": "operational_issue", "signal_id": 12, "tenant_bank_id": "MERIDIAN_TRUST_BANK", "force": false }
```
`signal_type` ∈ `operational_issue` \| `reconciliation_break` \| `fraud_anomaly` \| `funnel_account`

**Response 200**
```json
{
  "id": 3, "signal_type": "operational_issue", "reference_id": "12", "tenant_bank_id": "MERIDIAN_TRUST_BANK",
  "title": "Batch CL-MTB-20260807-4 overdue", "description": "...",
  "recommended_action": { "title": "Escalate to settlement ops", "description": "..." },
  "model": "mistral-large-latest", "generated_at": "2026-08-28T00:05:00Z"
}
```
**Note**: currently returns exactly **one** recommendation, not the ranked multi-action list shown in the Case Details mockups — multi-action support is explicitly out of scope for this pass. Wire the analyst screen's "Recommended Actions" panel to this single action for now.

**Errors**: `404` if the signal doesn't exist for that tenant; `503` if `MISTRAL_API_KEY` isn't configured; `502` on a narration/API failure.

---

### `POST /investigation/cases/compute`
Rebuilds all `InvestigationCase` rows for a tenant by clustering existing `OperationalIssue`/`ReconciliationBreak`/`EntitySnapshot` rows. Fully derived — deletes and rebuilds every run.

**Request body**: `{ "tenant_bank_id": "MERIDIAN_TRUST_BANK" }` (nullable — omit to run for every tenant)

**Response 200**: `{ "cases_created": 26, "alerts_grouped": 102 }` (real numbers, Meridian data)

**Clustering rule**: same issue category + same payment rail + detected within a rolling time window (48h). Party-level rate-spike issue types (`NETWORK_TIMEOUT_SPIKE`, `FORMAT_REJECTION_SPIKE`) and fraud snapshots with >1 `rails_used` group by category + time window only (no rail split).

---

### `GET /investigation/cases`
List view for the Investigation Queue page.

**Query params**: `tenant_bank_id` (required), `limit`, `offset`

**Response 200**
```json
{
  "total": 26,
  "cases": [
    {
      "id": 1, "case_code": "CNO-F7DE97", "tenant_bank_id": "MERIDIAN_TRUST_BANK",
      "category": "CONFIRMED_BREAK", "payment_rail": "CHEQUE",
      "title": "CHEQUE Confirmed reconciliation break Cluster",
      "current_exposure": 78.08, "transactions_affected": 6, "contributing_alerts_count": 6,
      "validation_status": "PENDING",
      "opened_at": "2026-08-05T00:00:00Z", "updated_at": "2026-08-28T00:00:00Z"
    }
  ]
}
```
Stat-card aggregates on the Investigation Queue page (High & Critical cases count, transactions requiring attention, payment value at risk) are computed **client-side** from this same list — no separate endpoint for those. Note: there's currently no `priority`/severity field on a case to filter or sort by — see Planned section.

---

### `GET /investigation/cases/{case_id}`
Case detail — powers the Case Details + Alerts tabs.

**Path params**: `case_id` (int)

**Response 200**
```json
{
  "id": 1, "case_code": "CNO-F7DE97", "category": "CONFIRMED_BREAK", "payment_rail": "CHEQUE",
  "title": "CHEQUE Confirmed reconciliation break Cluster",
  "current_exposure": 78.08, "transactions_affected": 6, "contributing_alerts_count": 6,
  "validation_status": "PENDING", "opened_at": "2026-08-05T00:00:00Z",
  "alerts": [
    {
      "id": 501, "alert_code": "ALT-CHEQUE-102", "source_type": "RECONCILIATION_BREAK", "source_id": 12,
      "transaction_id": "CHK-MTB-100038", "payment_rail": "CHEQUE", "anomaly_category": "Reconciliation",
      "anomaly_type": "Confirmed reconciliation break", "description": "Confirmed reconciliation break on transaction CHK-MTB-100038 (CHEQUE)",
      "detected_at": "2026-08-05T00:00:00Z"
    }
  ]
}
```
**Errors**: `404` if `case_id` doesn't exist.

---

### `POST /investigation/cases/{case_id}/validate`
Sets the case's display-only validation status. **Writes only to `InvestigationCase.validation_status`** — does not touch `analyst_reviews`, does not affect `confirmation_rate`/`false_positive_rate`/`quality_trend` anywhere. No feedback loop.

**Path params**: `case_id` (int)

**Request body**: `{ "validation_status": "VALID" }` — one of `PENDING` \| `VALID` \| `INVALID`

**Response 200**: the updated case object (same shape as the detail endpoint, minus `alerts`).

**Errors**: `400` if `validation_status` isn't one of the three; `404` if `case_id` doesn't exist.

---

### `GET /dashboard/exposure-by-rail`
**Serves**: Overview's "Exposure by Payment Rail" panel.
**Backing data**: reuses `app/exposure.py`'s `_all_claims()` — the same claims `get_exposure_by_domain`/`get_payment_value_by_rail` already use, just grouped by `rail_type`. Fraud/Anomaly claims carry `rail_type=None` (an entity, not a single-rail transaction) and are excluded, same limitation `get_payment_value_by_rail` already has.

**Response 200** (real, Meridian data):
```json
{
  "rails": [
    { "rail_type": "ACH", "exposure": 83326.47 },
    { "rail_type": "CARD", "exposure": 47834.47 },
    { "rail_type": "CHEQUE", "exposure": 118931.97 },
    { "rail_type": "FEDNOW", "exposure": 82137.15 }
  ],
  "total": 332229.06
}
```

---

### `GET /dashboard/priority-distribution`
**Serves**: Overview's "Action Priority Distribution" (Critical/High/Medium/Low).
**Backing data**: heuristic bucketing across all three engines — see `get_priority_distribution()`'s docstring in `app/dashboard.py` for the exact rule per signal type (no engine stores a "priority" field itself, this is a documented mapping, not raw data).

**Response 200** (real, Meridian data): `{ "critical": 54, "high": 45, "medium": 71, "low": 0 }`

---

### `GET /dashboard/anomaly-heatmap`
**Serves**: *both* Overview's "Anomaly Heatmap — Payment Rail × Category" and Payment Rails' "Anomalies by Rail — Category Breakdown" — same cross-tab, two chart treatments, one endpoint.
**Backing data**: same `_all_claims()` reuse as `exposure-by-rail` above; Fraud/Anomaly excluded for the same no-single-rail reason.

**Response 200** (real, Meridian data):
```json
{ "cells": [ { "rail_type": "ACH", "category": "Operational", "count": 13 }, { "rail_type": "ACH", "category": "Reconciliation", "count": 7 } ] }
```

---

### `GET /investigation/anomaly-types`
**Serves**: Anomalies page's "Top Anomaly Types" (named types — "Failure-rate spike," "Batch never settles," etc.).
**Backing data**: `InvestigationCaseAlert.anomaly_type`, ranked by count.

**Response 200** (real, Meridian data):
```json
{
  "types": [
    { "anomaly_type": "Confirmed reconciliation break", "count": 30 },
    { "anomaly_type": "Formatting rejection", "count": 19 },
    { "anomaly_type": "Duplicate payment", "count": 18 },
    { "anomaly_type": "Batch never settles", "count": 17 }
  ]
}
```

---

### `GET /dashboard/quality-trend-daily`
**Serves**: Detection Performance's "Valid Alerts vs False Positives — 7 Days".
**Query params**: `tenant_bank_id` (required), `days` (optional, default 7)
**Backing data**: same `AnalystReview.reviewed_at` timestamps `get_review_quality_trend` already uses, bucketed by calendar day. "Recent" is relative to this tenant's own latest review action, not wall-clock now — same reasoning `new_patterns_detected` uses.

**Response 200** (real, from actual review actions taken this session):
```json
{ "days": [ { "date": "2026-08-28", "confirmed": 1, "dismissed": 0 }, { "date": "2026-08-31", "confirmed": 1, "dismissed": 1 } ] }
```

---

### `GET /dashboard/detection-attention`
**Serves**: Detection Performance's "Detection Areas Requiring Attention" panel.
**Backing data**: reuses `detection_performance_by_rail` (already built) — flags rails whose `success_rate` sits below this tenant's own cross-rail average. Relative comparison, not an arbitrary absolute threshold.

**Response 200** (real, Meridian data):
```json
{
  "areas": [
    { "rail_type": "CARD", "reason": "success_rate 86.7% -- below this tenant's 88.1% average across rails", "severity": "medium" },
    { "rail_type": "WIRE", "reason": "success_rate 85.8% -- below this tenant's 88.1% average across rails", "severity": "medium" }
  ]
}
```

---

## Genuinely blocked — no endpoint fixes these without new underlying data/schema

- **Payment Processing Funnel** (Received→Validated→Processed→Posted→Settled) — the schema only has a settled/not-settled binary; no pipeline-stage concept exists to report on.
- **Cases Approaching SLA** — `InvestigationCase` has no deadline field; needs a schema addition, not just an endpoint.
- **"Reclassified" review outcome** — needs a new status value in `review/models.py`'s `STATUSES` (currently `PENDING`/`CONFIRMED`/`DISMISSED` only).
- **True intraday/hourly charts** (Active Anomalies chart, Detected vs Resolved by hour, Successful vs Failed over time, Transaction Volume by Rail over time) — `detected_at` reflects whenever the compute batch last ran, not a live production cadence; bucketing it hourly would chart *when we happened to run the job*, not a real intraday pattern.
- **AI Identified Patterns bulk list** — blocked by the deliberate cost/latency design of `/agent/narrate` (~2 min, real API cost per call) — a bulk version reverses that intentional constraint.
