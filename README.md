# Payment Anomaly — Merchant Payment Intelligence Platform

A payment intelligence platform for multiple tenant banks that ingests transaction
data across five payment rails (Cards, Wires, ACH, FedNow, Cheques), normalizes it
into one canonical shape, resolves every transaction to a merchant or individual
identity, and produces intelligence outcomes per merchant/individual: Fraud/Anomaly
Detection, Operational Issues, Reconciliation, and Payment Health. Purely
observational — no live transaction gating, no hold/decline decisions. Dashboards,
alerts, and reports only.

Pilot system. Data is synthetic, delivered as Excel files by a separate
generation service. SQLite for now (Postgres later is a config change, not a
rewrite). No Kafka/streaming — scheduled batch runs and on-demand endpoint calls
are sufficient at this volume.

## Pipeline status

| Step | Component | Status |
|---|---|---|
| 1 | Database Schema | ✅ Built |
| 2 | Data Shape Aligner | ✅ Built |
| 3 | Canonical Event Store | ✅ Built |
| 4 | Merchant/Individual Identity Resolution | ✅ Built |
| 5 | Feature Store (dashboard/summary features) | ✅ Built |
| 6a | **Fraud/Anomaly Detection engine — this doc** | 🔨 In progress, 3-way split below |
| 6b–d | Operational Issues / Reconciliation / Payment Health engines | ⏳ Not started |
| 7 | LLM Agent Layer (Mistral) | ⏳ Not started |
| 8 | Serving API | ⏳ Not started |

## Setup

```bash
git clone https://github.com/Vishaal-tibil/Payment_Anamoly.git
cd Payment_Anamoly
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
pip install scikit-learn        # needed for Track B/D — not in requirements.txt yet, add it in your branch

pytest -q                       # should show all tests passing
uvicorn app.main:app --reload   # starts the API; tables + demo config auto-created on first run
```

`data/payments.db` is committed to this repo with real data already loaded and
resolved — **Meridian Trust Bank**, 1,020 transactions, 10 merchants + 91
individuals resolved (Step 4), Step 5 features computed, and Track A's behavioral
snapshots (below) already generated. You don't need to re-run the ingestion
pipeline to start working — just pull and query.

If you do want to regenerate it from scratch (e.g. after a mapping-config fix):
```bash
python -m scripts.seed_meridian_mappings
python -m scripts.ingest_meridian_data
python -c "from app.database import SessionLocal; from app.resolution import resolve_parties; from app.feature_store import compute_features; from app.anomaly.features import compute_snapshots; db = SessionLocal(); resolve_parties(db, tenant_bank_id='MERIDIAN_TRUST_BANK'); compute_features(db, tenant_bank_id='MERIDIAN_TRUST_BANK'); compute_snapshots(db, tenant_bank_id='MERIDIAN_TRUST_BANK'); db.close()"
```

## What's already in the codebase, and where

- `app/models.py` — the core pipeline schema: `canonical_events` (every transaction,
  normalized), `source_column_mappings` (config-driven field mapping), `merchants`
  / `individuals` (resolved identities), `party_features` (Step 5 dashboard
  summaries — **do not use this for model training**, see below).
- `app/resolution.py`, `app/feature_store.py` — read these as the pattern to follow:
  a plain, testable, idempotent batch-compute function (`resolve_parties()`,
  `compute_features()`), callable standalone or via an endpoint, that reads
  `canonical_events` and writes a derived table.
- `app/anomaly/` — the fraud/anomaly engine's own subpackage. This is where all
  three tracks below live.
- `unsupervised-anomaly-detection-knowledge.md` (repo root) — the design doc this
  engine follows: profile-based, unsupervised, Isolation Forest + HDBSCAN +
  time-series, scored 0–100 per entity. Read this in full before writing any
  model code — the rest of this README assumes you have.

## The three tracks

Track A (behavioral feature snapshots) is **done** — see below for the contract.
The remaining three tracks each own one model layer from the knowledge doc's
Section 7, plus writing their signal back onto the same snapshot rows Track A
already created.

**A known constraint, confirmed against our actual data, not assumed:** every one
of our 101 resolved parties has under 50 transactions (merchants: 19–45,
individuals: median 2, max 6). Per the knowledge doc's own Section 9 fallback
table, that puts **100% of our entities in the "< 50 observations → global/segment
baseline" tier** — nobody qualifies for a personal per-entity model yet. Build
accordingly: one model per *segment* (`MERCHANT` or `INDIVIDUAL`, pooling all
entities in that segment together), not one model per entity. The code should
still make the tier a real, checked decision (see `segment` field below) so this
upgrades cleanly once there's more history — just don't build the per-entity path
now, there's nothing to train it on.

### Track B — Isolation Forest (+ final aggregation)
- Pool every `EntitySnapshot` row in a segment together; train one `sklearn.ensemble.IsolationForest` per segment (`MERCHANT`, `INDIVIDUAL`).
- Score every row, rescale to a comparable range, write to `EntitySnapshot.isolation_forest_score`.
- Also owns Section 8's final aggregation once Tracks C and D have written their columns:
  `final_score = 0.40*IF + 0.25*clustering + 0.35*timeseries` → write `final_anomaly_score` (0–100) and `anomaly_band` (Normal/Low-Medium/High/Critical, example bands in the knowledge doc Section 8) onto the same row.
- Suggested file: `app/anomaly/isolation_forest.py`, function `train_and_score(db, tenant_bank_id=None)`.

### Track C — Time-series drift (mine)
- Rolling mean/std, z-score, EWMA, CUSUM per entity — start simple, per the knowledge doc ("before LSTM/Transformer").
- Meaningful mainly for merchants' `WEEKLY` rows (a sequence exists to detect drift *in*); individuals only have one `TO_DATE` row each right now, so there's no sequence yet — flag this rather than force a number.
- Write to `EntitySnapshot.timeseries_drift_score`.

### Track D — HDBSCAN clustering
- **Heads up before you start**: with 10 merchants and 91 individuals, per-entity clustering will be too coarse to mean much regardless of window size. Cluster at the *segment* level (weekly merchant snapshots pooled, individual to-date snapshots pooled) as the real deliverable, and don't expect confident clusters yet — that's expected at this volume, not a bug in your code.
- A cluster number alone isn't the signal — a cluster **change** for a given entity between one snapshot and the next is (Section 7). Compare each entity's current cluster to its previous snapshot's cluster.
- Write cluster id to `EntitySnapshot.cluster_id`, and whether it changed vs. the prior snapshot to `EntitySnapshot.cluster_changed`.
- Given clustering is the least immediately useful piece at our volume, if you finish early: pair with Track A's segment-tier logic, or start on the `/anomaly` serving/summary endpoint.

## The input contract: `EntitySnapshot` (`app/anomaly/models.py`)

One row per merchant per week (`window_type="WEEKLY"`) they were active in, or one
row per individual to-date (`window_type="TO_DATE"`) — see `app/anomaly/features.py`'s
module docstring for exactly why individuals don't get windowed. Query it directly,
or `GET /anomaly/snapshots?tenant_bank_id=MERIDIAN_TRUST_BANK` / `POST
/anomaly/snapshots/compute` to regenerate.

Key fields: `party_id`, `party_type`, `segment` (train your model per segment),
`window_type`, `window_start`/`window_end`, `transaction_count`, `amount_total`/
`amount_avg`/`amount_median`/`amount_std`, `unique_counterparties`,
`new_counterparty_ratio`, `retry_ratio`, `avg_response_time_ms`, `timeout_ratio`,
`format_reject_ratio`, `rails_used`, `account_age_days`, `split` ("train"/"test",
chronological — see Section 6, only meaningful for `WEEKLY` rows).

### The one rule that matters most: where features come from

Every value in `EntitySnapshot` is computed from raw `canonical_events` facts
(amount, timestamp, counterparty name, rail, retry flag, raw response times,
format validation status) — **never** from `new_payee_risk_flag`,
`funnel_account_flag`, `velocity_threshold_breached`, `structuring_flag`,
`network_timeout_flag`, or anything inside `fraud_risk_details` (velocity_score,
distinct_originating_accounts_24h/7d, payee_relationship_age_days, ...).

Those are the source system's own pre-computed risk verdicts and aggregates.
Feeding them into your model as an input feature means training partly on the
answer the model is supposed to discover. This is explicit in the knowledge doc's
Section 4 and the closing line of Section 5 ("derive these from raw history —
don't reuse pre-built anomaly/velocity flags already in the schema").

**This is also why you should not read from `app.models.PartyFeatures` /
`party_features`** — that table (Step 5) is a dashboard summary built *from*
those same flags, for a different purpose. It's fine to look at for a sanity
comparison after the fact, never as a model input. `tests/test_anomaly_features.py::test_snapshot_values_are_unaffected_by_the_flags_it_must_not_use`
is a regression test proving `EntitySnapshot` doesn't leak these — if you add a
new field to `EntitySnapshot`, extend that test to cover it.

## The output contract: write back to the same row

Don't create a separate table or file format for your track's output. `EntitySnapshot`
already has the columns:

| Column | Written by |
|---|---|
| `isolation_forest_score` | Track B |
| `cluster_id`, `cluster_changed` | Track D |
| `timeseries_drift_score` | Track C |
| `final_anomaly_score`, `anomaly_band` | Track B, after A+B+C are all populated |

Update your column(s) on the existing row by `id` — don't insert new rows, and
don't touch a column that isn't yours. This is what makes combining the three
signals a matter of reading four columns off one table, not reconciling three
different outputs.

## Workflow

1. Branch per track: `track-b-isolation-forest`, `track-c-timeseries`,
   `track-d-hdbscan`.
2. Build against `data/payments.db` as committed — it already has real resolved
   Meridian data and Track A snapshots, so you can start immediately.
3. Write tests the same way the existing codebase does (see
   `tests/test_anomaly_features.py`, `tests/test_feature_store.py` for the
   pattern): synthetic fixtures for correctness, plus a real-data sanity check
   before you call it done.
4. Open a PR into `main` when your column(s) are populated and tested. Flag in
   the PR description what your model actually learned (even a rough summary —
   score distribution, example flagged entities) so combining the three signals
   isn't a surprise.
5. Once B, C, and D have all merged, Track B does the final aggregation pass
   (Section 8's weighted combination) and we review the combined output together
   before deciding the next step.

## Running tests

```bash
pytest -q            # full suite — should stay green; 36 passing as of this commit
pytest tests/test_anomaly_features.py -v   # just Track A, if you want to see the pattern
```
