# Analyst Screen — API Reference

Scope: only the endpoints the **Analyst persona** (Investigation Queue,
Insights > Overview/Anomalies/Payment Rails/Detection Performance) needs.
Head-of-Operations-only endpoints (`/health/*`, `/review/*`,
`/dashboard/senior-overview`, `/dashboard/anomaly-detection-categories`,
`/merchants*`, `/individuals*`, `/features*`, `/ingest/file`,
`/resolve/parties`) are intentionally excluded.

Two sections below:
- **Existing** — already implemented, live on the backend today, response
  shapes pulled directly from the current code (not guessed).
- **New (planned)** — per the agreed blueprint, not built yet. Included
  here so the Swagger file can be scaffolded for both at once.

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

---

### `GET /dashboard/detection-performance`
Review-grounded detection quality metrics. **Will gain new fields** — see "New (planned)" section below.

**Query params**: `tenant_bank_id` (string, required)

**Response 200 (current)**
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
  "quality_trend": [ { "reviewed_at": "2026-08-01T09:00:00Z", "cumulative_confirmation_rate": 0.9, "cumulative_false_positive_rate": 0.1 } ],
  "pattern_mix": [ { "category": "Fraud", "count": 29 } ]
}
```
| Field | Type | Description |
|---|---|---|
| `coverage_rate` | float\|null | Fraction of transactions belonging to a resolved party |
| `exposure_identified_early` | float | Real $ sum of `PROVISIONAL_VARIANCE` breaks caught ahead of the source's own verdict |
| `confirmation_rate` / `false_positive_rate` | float\|null | Real, but only over `reviewed_count` — grows more meaningful over time |
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
**Note**: currently returns exactly **one** recommendation, not the ranked multi-action list shown in the Case Details mockups — multi-action support is explicitly out of scope for this pass (see project notes). Wire the analyst screen's "Recommended Actions" panel to this single action for now.

**Errors**: `404` if the signal doesn't exist for that tenant; `503` if `MISTRAL_API_KEY` isn't configured; `502` on a narration/API failure.

---

## New (planned) — not yet implemented

### `POST /investigation/cases/compute`
Rebuilds all `InvestigationCase` rows for a tenant by clustering existing `OperationalIssue`/`ReconciliationBreak`/`EntitySnapshot` rows. Fully derived — deletes and rebuilds every run, same idempotent shape as every other `*/compute` endpoint in this backend.

**Request body**: `{ "tenant_bank_id": "MERIDIAN_TRUST_BANK" }` (nullable — omit to run for every tenant)

**Response 200**: `{ "cases_created": 11, "alerts_grouped": 97 }` (exact shape TBD at build time)

**Clustering rule**: same issue category + same payment rail + detected within a rolling time window (24-48h). Party-level rate-spike issue types (`NETWORK_TIMEOUT_SPIKE`, `FORMAT_REJECTION_SPIKE`) group by category + time window only (no rail split — a party can transact on multiple rails).

---

### `GET /investigation/cases`
List view for the Investigation Queue page.

**Query params**: `tenant_bank_id` (required), `priority` (optional: `Critical`\|`High`\|`Medium`\|`Low`), `limit`, `offset`

**Response 200 (proposed shape)**
```json
{
  "total": 11,
  "cases": [
    {
      "id": 1, "case_code": "CNO-123", "tenant_bank_id": "MERIDIAN_TRUST_BANK",
      "category": "NETWORK_TIMEOUT_SPIKE", "payment_rail": "RTP",
      "title": "RTP Processing Failure Cluster",
      "current_exposure": 12400000.0, "transactions_affected": 1842, "contributing_alerts_count": 8,
      "validation_status": "PENDING",
      "opened_at": "2026-04-12T13:24:00Z", "updated_at": "2026-04-12T13:45:00Z"
    }
  ]
}
```
Stat-card aggregates on the Investigation Queue page (High & Critical cases count, transactions requiring attention, payment value at risk) are computed **client-side** from this same list — no separate endpoint needed for those.

---

### `GET /investigation/cases/{case_id}`
Case detail — powers the Case Details + Alerts tabs.

**Path params**: `case_id` (int)

**Response 200 (proposed shape)**
```json
{
  "id": 1, "case_code": "CNO-123", "category": "NETWORK_TIMEOUT_SPIKE", "payment_rail": "RTP",
  "title": "RTP Processing Failure Cluster",
  "current_exposure": 12400000.0, "transactions_affected": 1842, "contributing_alerts_count": 8,
  "validation_status": "PENDING", "opened_at": "2026-04-12T13:24:00Z",
  "alerts": [
    {
      "id": 501, "alert_code": "ALT-RTP-10482", "source_type": "OPERATIONAL_ISSUE", "source_id": 12,
      "transaction_id": null, "payment_rail": "RTP", "anomaly_category": "Operational",
      "anomaly_type": "Failure-rate spike", "description": "Rapid payment failure acceleration",
      "detected_at": "2026-04-12T13:24:00Z"
    }
  ]
}
```
**Errors**: `404` if `case_id` doesn't exist for the requested tenant.

---

### `POST /investigation/cases/{case_id}/validate`
Sets the case's display-only validation status. **Writes only to `InvestigationCase.validation_status`** — does not touch `analyst_reviews`, does not affect `confirmation_rate`/`false_positive_rate`/`quality_trend` anywhere. No feedback loop.

**Path params**: `case_id` (int)

**Request body**: `{ "validation_status": "VALID" }` — one of `PENDING` \| `VALID` \| `INVALID`

**Response 200**: the updated case object (same shape as the detail endpoint, minus `alerts`).

---

## Fields still needing a decision before `get_detection_performance` is extended
(see prior blueprint discussion — not blocking the endpoints above)
- `median_detection_time_seconds` — delta between transaction occurrence and `detected_at`, per issue
- `detection_volume_by_category` — % split across Operational/Fraud/Reconciliation
- `detection_performance_by_rail` — success rate + median latency, grouped by rail
- `new_patterns_detected` — count of distinct categories appearing this period that didn't appear last period (definition to confirm)
