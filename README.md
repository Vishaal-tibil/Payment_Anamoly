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

## Team & ownership — Fraud/Anomaly Detection engine

Track A (behavioral feature snapshots — the shared input every other track builds
on) is **done**. The three model layers from the knowledge doc's Section 7 are
split as follows:

| Track | Owner | Builds | Branch | Writes to |
|---|---|---|---|---|
| B | **Harshitha** | Isolation Forest + final score aggregation | `track-b-isolation-forest` | `isolation_forest_score`, then later `final_anomaly_score` / `anomaly_band` |
| C | **Vishaal** | Time-series drift detection | `track-c-timeseries` | `timeseries_drift_score` |
| D | **Shruthi** | HDBSCAN clustering | `track-d-hdbscan` | `cluster_id`, `cluster_changed` |

Everyone reads from the same table (`EntitySnapshot`) and writes back to the same
rows — no separate output files or formats. See **Input contract** and **Output
contract** below before writing any code; each track section after that is a
self-contained step-by-step for that person.

## Setup (everyone, first)

```bash
git clone https://github.com/Vishaal-tibil/Payment_Anamoly.git
cd Payment_Anamoly
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

pytest -q                       # confirm you're starting from a green baseline: 36 passing
uvicorn app.main:app --reload   # optional — starts the API if you'd rather hit it over HTTP than query the DB directly
```

`data/payments.db` is committed to this repo **with real data already loaded**:
Meridian Trust Bank, 1,020 transactions, resolved into 10 merchants + 91
individuals (Step 4), Step 5 dashboard features computed, and Track A's
behavioral snapshots already generated (83 merchant weekly rows + 91 individual
to-date rows). You do not need to run any ingestion pipeline — just pull `main`
and start querying `EntitySnapshot`.

If you ever do need to regenerate it from scratch (e.g. after a mapping-config
fix upstream):
```bash
python -m scripts.seed_meridian_mappings
python -m scripts.ingest_meridian_data
python -c "from app.database import SessionLocal; from app.resolution import resolve_parties; from app.feature_store import compute_features; from app.anomaly.features import compute_snapshots; db = SessionLocal(); resolve_parties(db, tenant_bank_id='MERIDIAN_TRUST_BANK'); compute_features(db, tenant_bank_id='MERIDIAN_TRUST_BANK'); compute_snapshots(db, tenant_bank_id='MERIDIAN_TRUST_BANK'); db.close()"
```

## What's already in the codebase, and where

- `app/models.py` — the core pipeline schema: `canonical_events` (every transaction,
  normalized), `source_column_mappings` (config-driven field mapping), `merchants`
  / `individuals` (resolved identities), `party_features` (Step 5 dashboard
  summaries — **do not use this for model training**, see Input contract below).
- `app/resolution.py`, `app/feature_store.py` — read these as the pattern to follow:
  a plain, testable, idempotent batch-compute function (`resolve_parties()`,
  `compute_features()`), callable standalone or via an endpoint, that reads
  `canonical_events` and writes a derived table. Your track's module should look
  like these.
- `app/anomaly/` — the fraud/anomaly engine's own subpackage. `models.py` has
  `EntitySnapshot` (the shared table); `features.py` is Track A (already built —
  read its module docstring, it explains the leakage rule in full). Your new file
  goes here too.
- `unsupervised-anomaly-detection-knowledge.md` (repo root) — the design doc this
  engine follows: profile-based, unsupervised, Isolation Forest + HDBSCAN +
  time-series, scored 0–100 per entity. **Read this in full before writing any
  model code** — every track section below assumes you have.

## A known constraint — read before choosing your approach

Confirmed against our actual data, not assumed: every one of our 101 resolved
parties has under 50 transactions (merchants: 19–45, individuals: median 2, max
6). Per the knowledge doc's own Section 9 fallback table, that puts **100% of our
entities in the "< 50 observations → global/segment baseline" tier** — nobody
qualifies for a personal per-entity model yet.

This applies to all three of you: build **one model per segment**
(`MERCHANT` or `INDIVIDUAL` — pool every entity's snapshot rows in that segment
together), not one model per entity. Structure your code so the per-entity /
full-model tiers from Section 9 are a real, checked branch (keyed off observation
count) even though only the segment-baseline branch will actually run today —
that way this upgrades cleanly once there's more history, instead of needing a
rewrite.

---

## Input contract: `EntitySnapshot` (`app/anomaly/models.py`)

This is what all three of you read from. One row per merchant per week
(`window_type="WEEKLY"`) they were active in, or one row per individual to-date
(`window_type="TO_DATE"`) — see `app/anomaly/features.py`'s module docstring for
exactly why individuals don't get windowed (too few transactions each for a
weekly sequence to mean anything).

**How to get it:**
```python
from app.database import SessionLocal
from app.anomaly.models import EntitySnapshot

db = SessionLocal()
merchant_rows = db.query(EntitySnapshot).filter_by(
    tenant_bank_id="MERIDIAN_TRUST_BANK", segment="MERCHANT",
).all()
individual_rows = db.query(EntitySnapshot).filter_by(
    tenant_bank_id="MERIDIAN_TRUST_BANK", segment="INDIVIDUAL",
).all()
```
Or over HTTP: `GET /anomaly/snapshots?tenant_bank_id=MERIDIAN_TRUST_BANK&segment=MERCHANT`
(paginated — pass `limit`/`offset`). To regenerate the table after a Track A
change: `POST /anomaly/snapshots/compute`.

**Feature columns** (the ones you train/score on): `transaction_count`,
`amount_total`, `amount_avg`, `amount_median`, `amount_std`,
`unique_counterparties`, `new_counterparty_ratio`, `retry_ratio`,
`avg_response_time_ms`, `timeout_ratio`, `format_reject_ratio`,
`account_age_days`. Some of these can be `None` (e.g. `avg_response_time_ms` when
a rail never reports response timing) — handle nulls explicitly (impute or drop
per-feature) and document what you chose.

**Bookkeeping columns**: `party_id`, `party_type`, `segment`, `window_type`,
`window_start`/`window_end`, `rails_used`, `split` (chronological "train"/"test"
per Section 6 — only meaningful for `WEEKLY` rows; all `TO_DATE` rows are
`"train"` since there's nothing to hold out for a single observation).

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

**This is also why you must not read from `app.models.PartyFeatures` /
`party_features`** — that table (Step 5) is a dashboard summary built *from*
those same flags, for a different purpose. Fine to glance at for a sanity
comparison after the fact, never as a model input.
`tests/test_anomaly_features.py::test_snapshot_values_are_unaffected_by_the_flags_it_must_not_use`
is a regression test proving `EntitySnapshot` doesn't leak these — if your track
adds a new column to `EntitySnapshot`, extend that test to cover it.

## Output contract: write back to the same row, never a new table

| Column | Owner |
|---|---|
| `isolation_forest_score` | Harshitha (Track B) |
| `timeseries_drift_score` | Vishaal (Track C) |
| `cluster_id`, `cluster_changed` | Shruthi (Track D) |
| `final_anomaly_score`, `anomaly_band` | Harshitha, after A+B+C are all merged |

Update your column(s) on the **existing** `EntitySnapshot` row by `id` —
`db.query(EntitySnapshot).filter_by(id=row.id).update({...})` or load-modify-commit
on the object. Don't insert new rows, and don't touch a column that isn't yours.
This is what makes combining the three signals a matter of reading four columns
off one table, not reconciling three different output formats.

---

## Harshitha — Track B: Isolation Forest + final aggregation

**What you're building**: Section 7's Isolation Forest layer, scoring how far
each entity's snapshot sits from the segment's historical norm. Once Vishaal and
Shruthi have merged their columns, you also own Section 8's weighted combination
into the final score.

**Where to get your data**: see **Input contract** above. You'll train two
separate models — one on `segment="MERCHANT"` rows, one on `segment="INDIVIDUAL"`
rows. Use `split="train"` rows to fit the model, `split="test"` rows to sanity
check it (merchant/`WEEKLY` rows only; individuals have no test split).

**Process**:
1. `git checkout main && git pull && git checkout -b track-b-isolation-forest`
2. Add `scikit-learn` to `requirements.txt`, then `pip install -r requirements.txt`.
3. Create `app/anomaly/isolation_forest.py`. Follow the shape of `resolve_parties()`
   in `app/resolution.py` or `compute_features()` in `app/feature_store.py`: a
   plain function, e.g. `train_and_score(db, tenant_bank_id=None) -> dict`,
   callable standalone (for scripts/tests) and easy to wire into an endpoint later.
4. For each segment: build a feature matrix from the train-split rows (handle
   nulls, pick a null strategy, document it in a comment), fit
   `sklearn.ensemble.IsolationForest`, then score **every** row in that segment
   (train and test both) with `score_samples`/`decision_function`.
5. Rescale the raw score to a 0–100 range so it's comparable to the other two
   tracks' outputs (document your rescaling formula — a simple min-max or
   percentile rank against the segment's own score distribution is fine for v1).
6. Write the rescaled score to `EntitySnapshot.isolation_forest_score` on each
   row, `db.commit()`.
7. Write tests in `tests/test_isolation_forest.py`: at least one synthetic-fixture
   test proving an obviously-outlier snapshot (e.g. 50x the amount of everything
   else in its segment) scores meaningfully higher than normal ones, plus a
   real-data run printing the score distribution (min/median/max) for both
   segments as a sanity check.
8. `pytest -q` — must stay green (36 existing + yours).
9. Commit, push your branch, open a PR into `main`. In the description: segment
   sample sizes you trained on, the score distribution, and 2–3 example entities
   you'd flag and why.
10. Once Vishaal's and Shruthi's PRs are also merged: pull `main`, read
    `timeseries_drift_score` and `cluster_changed` (treat a `True`/changed
    cluster as a 0/1 signal, `None` — not applicable yet — as 0 for now, and say
    so in a comment) alongside your own `isolation_forest_score`, compute
    `final_score = 0.40*IF + 0.25*clustering_signal + 0.35*timeseries`, write
    `final_anomaly_score` (0–100) and `anomaly_band` (Normal/Low-Medium/High/
    Critical — example cutoffs in the knowledge doc Section 8) back onto every
    row. Open a second PR for this pass.

**Where to push your output**: `EntitySnapshot.isolation_forest_score`, then
later `final_anomaly_score`/`anomaly_band` — same rows, via your
`track-b-isolation-forest` branch → PR into `main`.

---

## Vishaal — Track C: Time-series drift

**What you're building**: Section 7's time-series layer — rolling mean/std,
z-score, EWMA, CUSUM per entity, starting simple per the knowledge doc ("before
LSTM/Transformer"). Meaningful mainly for merchants' `WEEKLY` rows, where an
actual sequence exists to detect drift in; individuals only have one `TO_DATE`
row each right now, so there's no sequence to speak of yet for them — leave
`timeseries_drift_score` null for individuals rather than forcing a number, and
say so in the code.

**Where to get data**: same **Input contract** as above, filtered to
`segment="MERCHANT"`, ordered by `party_id`, `window_start` to get each
merchant's chronological sequence of weekly snapshots.

**Process**:
1. `git checkout main && git pull && git checkout -b track-c-timeseries`
2. `app/anomaly/timeseries.py`, function `score_drift(db, tenant_bank_id=None) -> dict`,
   same shape as everyone else's module.
3. Per merchant, per feature (start with `amount_total`, `transaction_count`,
   `new_counterparty_ratio`): compute rolling mean/std across that merchant's own
   prior weeks, z-score the current week against it, combine into one drift score
   per row.
4. Rescale to 0–100, write to `EntitySnapshot.timeseries_drift_score`.
5. Tests + a real-data run showing which merchants/weeks show the largest drift.
6. `pytest -q` green, PR into `main` with the same kind of summary as the others.

**Where to push**: `EntitySnapshot.timeseries_drift_score`, via
`track-c-timeseries` → PR into `main`.

---

## Shruthi — Track D: HDBSCAN clustering

**Heads up before you start**: with 10 merchants and 91 individuals, per-entity
clustering will be too coarse to mean much regardless of window size — that's
expected at this data volume (the knowledge doc's own Section 13 flags this),
not a bug in your code. Cluster at the *segment* level (all merchant weekly rows
pooled together, all individual to-date rows pooled together) as the real
deliverable, and don't be surprised if HDBSCAN finds mostly noise/one big cluster
at this volume — report that honestly rather than tuning parameters until it
looks more confident than it is.

**Where to get data**: same **Input contract** as above, both segments.

**Process**:
1. `git checkout main && git pull && git checkout -b track-d-hdbscan`
2. Add `scikit-learn` (>=1.3 has `sklearn.cluster.HDBSCAN` built in) to
   `requirements.txt` if it isn't there yet from Harshitha's branch — check
   before adding a duplicate line, it's an easy merge conflict to avoid.
3. `app/anomaly/clustering.py`, function `cluster_and_score(db, tenant_bank_id=None) -> dict`.
4. Cluster each segment's pooled rows with HDBSCAN. Write the resulting
   `cluster_id` onto each row (`-1` for HDBSCAN's "noise"/unclustered points is
   fine and expected, don't treat it as an error).
5. A cluster number alone isn't the signal — a cluster **change** for a given
   entity between one snapshot and the next is (Section 7). For each merchant's
   `WEEKLY` rows in chronological order, compare each row's `cluster_id` to that
   same merchant's *previous* row's `cluster_id`; set `cluster_changed = True` if
   it differs. Individuals only have one `TO_DATE` row each — there's no "previous"
   to compare to, so leave `cluster_changed` null for them, same reasoning as
   Vishaal's track.
6. Tests + a real-data run reporting cluster counts/sizes per segment and which
   (if any) merchants changed cluster week to week.
7. `pytest -q` green, PR into `main`.
8. If you finish early and clustering genuinely isn't producing anything useful
   at this volume (likely, and fine to say so): pair with Track A's
   `_MIN_TXNS_FOR_WEEKLY_WINDOWING`/segment-tier logic in `app/anomaly/features.py`,
   or start sketching the `/anomaly` summary endpoint that will eventually surface
   `final_anomaly_score`/`anomaly_band` per entity.

**Where to push**: `EntitySnapshot.cluster_id`, `EntitySnapshot.cluster_changed`,
via `track-d-hdbscan` → PR into `main`.

---

## Shared workflow (everyone)

1. Branch off latest `main` using the branch name in the ownership table above —
   don't build on top of someone else's unmerged branch.
2. Build against `data/payments.db` as committed — real resolved data and Track A
   snapshots are already there, start immediately.
3. Write tests the way the existing codebase does (`tests/test_anomaly_features.py`,
   `tests/test_feature_store.py` are the pattern): synthetic fixtures for
   correctness, plus a real-data sanity check you can point to before calling it
   done.
4. PR into `main` when your column(s) are populated and tested. Put a real
   summary in the PR description (sample sizes, score/cluster distribution,
   example entities) — the point is that combining the three signals shouldn't
   be a surprise to anyone.
5. Merge order doesn't matter between B, C, and D — they write independent
   columns. Harshitha's final-aggregation pass is the one thing that has to come
   last, after the other two are merged.
6. Once all four columns (`isolation_forest_score`, `timeseries_drift_score`,
   `cluster_id`/`cluster_changed`) plus the aggregation are in, we review the
   combined output together and decide the next step.

## Running tests

```bash
pytest -q                                   # full suite — should stay green; 36 passing as of this commit
pytest tests/test_anomaly_features.py -v    # just Track A, if you want to see the pattern your track's tests should follow
```
