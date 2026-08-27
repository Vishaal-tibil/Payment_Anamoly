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
| 6a | **Fraud/Anomaly Detection engine — this doc** | ✅ Built — Tracks A–D + final aggregation all merged |
| 6b | **Operational Issues engine — this doc** | 🔨 In progress, see below |
| 6c–d | Reconciliation / Payment Health engines | ⏳ Not started |
| 7 | LLM Agent Layer (Mistral) | ⏳ Not started |
| 8 | Serving API | ⏳ Not started |

## Team & ownership — Fraud/Anomaly Detection engine (concluded)

All four tracks are merged into `main`. Recap of who built what:

| Track | Owner | Built | Writes to |
|---|---|---|---|
| A | Vishaal | Behavioral feature snapshots (`EntitySnapshot`/`BeneficiarySnapshot`) — the shared input every other track reads | `EntitySnapshot`, `BeneficiarySnapshot` (all raw-derived columns) |
| B | **Harshitha** | Isolation Forest + Section 8 final aggregation | `isolation_forest_score`, `final_anomaly_score` / `anomaly_band` |
| C | **Vishaal** | Time-series drift detection (merchant drift + Funnel Account drift) | `timeseries_drift_score`, `funnel_drift_score` |
| D | **Shruthi** | HDBSCAN clustering | `cluster_id`, `cluster_changed` |

Everyone reads from the same tables (`EntitySnapshot`/`BeneficiarySnapshot`) and
writes back to the same rows — no separate output files or formats. See **Input
contract** and **Output contract** below; each track section after that is kept
as a record of what was built and how, useful background if you're extending
one of these tracks later.

### How the four branches actually came together — read this if you're Harshitha or Shruthi

Both `track-b-isolation-forest` and `track-d-hdbscan` were branched before
Track C's time-series work and Funnel Account detection landed on `main`. That
caused three genuine overlaps, each resolved a specific way during the merge —
worth knowing before you pull `main` next, since some of what you pushed isn't
what ended up live:

- **`near_threshold_ratio`** (structuring feature): both Harshitha and Shruthi
  independently added this to `EntitySnapshot`/`features.py`, with different
  logic (Harshitha: single $10k CTR band; Shruthi: $10k + $3k bands). **Kept
  Harshitha's** — her Isolation Forest was already trained and tested against
  it; swapping in a different implementation would have silently changed her
  model's inputs after the fact. Shruthi's `clustering.py` deliberately
  doesn't use this feature at all (see its own comment on why), so nothing
  about clustering was affected either way.
- **Funnel Account detection**: Shruthi independently built her own
  `BeneficiarySnapshot`/`funnel.py` (a simple threshold rule:
  `distinct_senders≥3 AND new_sender_ratio≥0.6`, not knowing Track C's
  version — weekly snapshots + z-score drift against each beneficiary's own
  history — was already merged and validated against real data. **Kept the
  Track C version**, since a fixed global threshold applied to every
  beneficiary regardless of its own normal volume is exactly the kind of
  brittle rule this engine's profile-based design otherwise avoids. Shruthi's
  `funnel.py` and her own `timeseries.py` (also independently rebuilt, same
  root cause) were not merged.
- **HDBSCAN clustering** (`clustering.py`): Shruthi's real, independent Track D
  deliverable — merged as-is, it's genuinely good work (correctly excludes
  `near_threshold_ratio`/`timeout_ratio` from the clustering feature set with
  real reasoning tested against real data, handles the observation-tier
  fallback correctly). Only the file itself was cherry-picked in (not the
  whole branch), since the branch also carried the funnel/timeseries
  duplicates above.

Also fixed while concluding: the final-aggregation **scale mismatch**
(`cluster_changed` was contributing as a literal `1.0`/`0.0` against two
0–100-scale signals, making the Critical band mathematically unreachable —
max possible score was 75.25). Now rescaled to `100.0`/`0.0`; confirmed
against real data that Critical is reachable post-fix.

If either of you disagrees with a call above, easy to revisit — nothing here
is final in the sense of "can't be changed," just what's live on `main` today.

## Setup (everyone, first)

```bash
git clone https://github.com/Vishaal-tibil/Payment_Anamoly.git
cd Payment_Anamoly
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

pytest -q                       # confirm you're starting from a green baseline -- see note below on 2 expected failures
uvicorn app.main:app --reload   # optional — starts the API if you'd rather hit it over HTTP than query the DB directly
```

`data/payments.db` is committed to this repo **with real data already loaded**:
Meridian Trust Bank, 1,020 transactions, resolved into 10 merchants + 91
individuals (Step 4), Step 5 dashboard features computed, and the fraud engine's
full output already populated — Track A snapshots (83 merchant weekly rows + 91
individual to-date rows), `isolation_forest_score`, `cluster_id`/`cluster_changed`,
`timeseries_drift_score`/`funnel_drift_score`, and `final_anomaly_score`/
`anomaly_band` (174 rows scored: 98 Normal, 68 Low-Medium, 6 High, 2 Critical).
You do not need to run any ingestion or scoring pipeline — just pull `main` and
start querying `EntitySnapshot`.

**Expect exactly 2 failures, not 0**, the first time you run `pytest -q` against
this committed real data: `test_ingest_sample_pre_then_post_merges_into_one_row`
and `test_ingest_card_then_resolve_then_list_merchants` in
`test_api_integration.py`. Both are KEYBANK-demo tests that assume a clean DB;
the committed real data already has KEYBANK rows pre-resolved from the initial
commit, so a fresh upload in those tests doesn't create what the test expects.
Confirmed benign every time by resetting to a clean DB (`rm data/payments.db`,
recreate tables, `pytest -q`) — full suite goes green. Not something to "fix"
by touching the committed data; just expected noise from two tests and real
data coexisting in the same file-backed DB.

If you ever do need to regenerate it from scratch (e.g. after a mapping-config
fix upstream), tables first (a fresh DB only registers the tables modules that
have actually been imported, so import both `app.models` and
`app.anomaly.models` before `create_all` or the anomaly tables won't exist):
```bash
python -m scripts.seed_meridian_mappings
python -m scripts.ingest_meridian_data
python -c "from app.database import Base, engine; import app.models, app.anomaly.models; Base.metadata.create_all(bind=engine)"
python -c "
from app.database import SessionLocal
from app.resolution import resolve_parties
from app.feature_store import compute_features
from app.anomaly.features import compute_snapshots
from app.anomaly.timeseries import score_drift, score_funnel_drift
from app.anomaly.beneficiary_features import compute_beneficiary_snapshots
from app.anomaly.isolation_forest import train_and_score, compute_final_score
from app.anomaly.clustering import cluster_and_score
db = SessionLocal()
T = 'MERIDIAN_TRUST_BANK'
resolve_parties(db, tenant_bank_id=T)
compute_features(db, tenant_bank_id=T)
compute_snapshots(db, tenant_bank_id=T)
score_drift(db, tenant_bank_id=T)
compute_beneficiary_snapshots(db, tenant_bank_id=T)
score_funnel_drift(db, tenant_bank_id=T)
train_and_score(db, tenant_bank_id=T)
cluster_and_score(db, tenant_bank_id=T)
compute_final_score(db, tenant_bank_id=T)
db.close()
"
```

## API contract (for the frontend)

[`docs/openapi.json`](docs/openapi.json) (and `docs/openapi.yaml`) is the OpenAPI
3.1 spec for all 18 endpoints on `main`, including the complete fraud/anomaly
engine (Tracks A–D + final aggregation). Unlike `GET /docs` (FastAPI's live
Swagger UI, generated straight from the code), this file also documents
response shapes and includes realistic examples, since none of the endpoints
declare a `response_model` in code.

Paste it into [editor.swagger.io](https://editor.swagger.io) or Postman/Insomnia's
import to browse it, or point Swagger UI/Redoc at the file directly.

Regenerate after any endpoint or response-shape change:
```bash
python -m scripts.generate_openapi
```
`app/main.py`'s actual routes/request bodies are the source of truth; the script
only *adds* response schemas/examples on top of what FastAPI already generates
correctly.

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

## Operational Issues engine (Step 6b) — a separate track from the fraud engine above

This is a **different engine from Tracks A–D above**, not another column on
`EntitySnapshot`. The fraud engine asks "is this behavior unusual" (needs a
learned baseline — Isolation Forest/HDBSCAN/time-series). Operational Issues
asks "did the payment pipeline itself work correctly" — for 3 of the 4 issues
below, the ground truth already sits directly in `CanonicalEvent`'s existing
columns; only 1 needs any statistics, and it's simple rolling z-score, not ML.

| Issue | Approach | Why |
|---|---|---|
| Network/Processor Timeout | **Rolling z-score** (statistical, untrained) | The only one where "is this rate normal" isn't a fact — needs a baseline to compare against. |
| Batch Never Settles | **Deterministic rule** | `file_reached_settlement` is a literal fact; either it's true or it isn't. No model needed. |
| Duplicate Payment | **Deterministic match**, exact-key join, no ML | `idempotency_key`/`original_transaction_id` already link a retry to its original — a lookup problem, not a prediction problem. |
| Formatting Rejection | **Deterministic listing** + rolling z-score for the spike half | `format_validation_status` is a fact; only "is the reject *rate* spiking" needs statistics. |

**Owner**: _TBD — tell me who's picking this up (one person across all four, or
split by issue across your team) and I'll personalize this section with named
runbooks the same way Tracks B/C/D above are personalized to Harshitha/Vishaal/
Shruthi._

### The rule here is the opposite of the fraud engine's — read carefully

Tracks A–D above have a hard rule: never read `network_timeout_flag`,
`format_validation_status`, etc. as a model input, because those are the
source's own pre-computed **fraud** risk verdicts and using them would leak
the answer. **That rule does not apply here.** `network_timeout_flag`,
`file_reached_settlement`, `duplicate_check_status`, and
`format_validation_status` are not fraud verdicts being reverse-engineered —
they're plain operational facts (did the network respond in time, did the
batch settle, is this a flagged duplicate, did the message pass format
validation), and reading them directly is the entire point of this engine.
Don't import the fraud engine's exclusion list into this one by habit.

### Where to get data

Straight from `canonical_events` — no new snapshot table needed for the two
deterministic issues, and no new aggregation code needed for the two
rate-based ones:

```python
from app.database import SessionLocal
from app.models import CanonicalEvent

db = SessionLocal()
events = db.query(CanonicalEvent).filter(
    CanonicalEvent.tenant_bank_id == "MERIDIAN_TRUST_BANK",
).all()
```

For the two rolling-z-score issues, **reuse Track A's existing per-week rates
instead of re-aggregating from scratch** — `EntitySnapshot.timeout_ratio` and
`EntitySnapshot.format_reject_ratio` (`app/anomaly/models.py`) are already
computed per merchant per week. Read them (never write to that table — it's
the fraud engine's output contract, see above); z-score each merchant's
current week against that same merchant's prior weeks, the same core
algorithm as Track C's `_score_sequence()` in `app/anomaly/timeseries.py` —
worth importing/reusing rather than reimplementing.

### Output contract: a new table, `OperationalIssue`

One flat table, one row per detected issue instance — keeps all four issue
types queryable from one place for whoever builds the Step 8 serving API
later, even though they're detected by different logic:

```python
# app/operations/models.py
class OperationalIssue(Base):
    __tablename__ = "operational_issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_type = Column(String, nullable=False, index=True)
    # "NETWORK_TIMEOUT_SPIKE" | "BATCH_NOT_SETTLED" | "DUPLICATE_PAYMENT" | "FORMAT_REJECTION_SPIKE"

    tenant_bank_id = Column(String, nullable=False, index=True)
    reference_type = Column(String, nullable=False)  # "TRANSACTION" | "BATCH" | "PARTY"
    reference_id = Column(String, nullable=False, index=True)
    # transaction_id for duplicates, batch_id for stuck batches, party_id for rate spikes

    window_start = Column(DateTime(timezone=True), nullable=True)  # rate-based issues only
    window_end = Column(DateTime(timezone=True), nullable=True)

    severity_score = Column(Float, nullable=True)  # 0-100 for the two z-score issues; null for the two deterministic ones (they're binary, not scored)
    details = Column(JSON, nullable=True)  # e.g. {"expected_settlement_at": ..., "days_overdue": 4} or {"duplicate_of": "TXN-123", "amount_delta": 0.0}

    detected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

### Process

1. `git checkout main && git pull && git checkout -b operational-issues` (or
   `operational-issues-<your-name>` if you're splitting the four issues across
   people — check with the group first so two people don't grab the same one).
2. New subpackage `app/operations/` (sibling to `app/anomaly/`, same pattern):
   `models.py` (the table above), then one module per detection approach —
   e.g. `rules.py` for the two deterministic checks, `drift.py` for the two
   z-score checks. Each a plain function returning a summary dict, same shape
   as `resolve_parties()`/`compute_snapshots()`/`score_drift()`.
3. **Batch Never Settles**: group `canonical_events` where `batch_id` is not
   null by `batch_id`; flag a batch where `expected_settlement_at` has passed
   (compare to now) and `file_reached_settlement` isn't `True`.
4. **Duplicate Payment**: group by `idempotency_key` (and separately by
   `original_transaction_id` where `is_retry=True`) where not null; flag a
   group where more than one row reached a settled/completed `status` — a
   genuine retry should only have one winner.
5. **Network/Processor Timeout** and **Formatting Rejection** (spike half):
   per merchant, per week, z-score `timeout_ratio`/`format_reject_ratio`
   against that merchant's own prior weeks (≥2 prior weeks needed, same
   floor Track C uses). Rescale to 0–100, write as `severity_score`.
   **Formatting Rejection** (listing half): a plain filter over
   `canonical_events` for `format_validation_status` indicating a reject —
   no scoring, just list them; still worth a row in `OperationalIssue` per
   rejected transaction so it shows up in the same feed as everything else.
6. Tests in `tests/test_operational_issues.py`: synthetic fixtures per issue
   type (a batch that's overdue vs. one that's on time; a real duplicate vs.
   a legitimate single retry; a stable timeout rate vs. a spike) — same
   pattern as `tests/test_timeseries.py`. Plus a real-data run against
   `data/payments.db` reporting how many of each issue type actually show up.
7. `pytest -q` — must stay green (56 existing + yours).
8. Add whatever endpoints make sense following `app/main.py`'s existing
   pattern (a `POST /operations/issues/compute` to run detection, a
   `GET /operations/issues?tenant_bank_id=...&issue_type=...` to list) —
   same request/response shape as the `/anomaly/*` endpoints, and regenerate
   `docs/openapi.json` (`python -m scripts.generate_openapi`) once they're in.
9. Commit, push your branch, open a PR into `main`. In the description: how
   many of each issue type were found in the real Meridian data, and 1–2
   concrete examples per issue type.

**Where to push**: new table `operational_issues`, via your branch → PR into
`main` — same merge pattern as Tracks B/C/D (branch off `main`, verify tests
green on a clean DB reset, restore real data, push, PR).

### How this fits into the complete pipeline

Steps 6a (fraud/anomaly) and 6b (this) are independent engines that both read
`canonical_events`/`EntitySnapshot` and both write their own output table —
neither blocks the other, and there's no shared column to coordinate on (unlike
Tracks B/C/D, which do share `EntitySnapshot`). Once both are far enough along,
Step 7 (the LLM agent layer) and Step 8 (the serving API) read from *both*
`EntitySnapshot`/`BeneficiarySnapshot` (fraud) and `OperationalIssue`
(operational) to produce one combined per-merchant/per-individual picture —
that integration is the next milestone after both engines have real output to
combine, not something either engine needs to anticipate in its own code now.

## Shared workflow (everyone) — this is how Tracks A–D actually shipped

Kept as a record of the process, and the pattern to reuse for the next engine
(Operational Issues, Step 6b, above) or for extending any of these tracks later:

1. Branch off latest `main` — don't build on top of someone else's unmerged
   branch (this bit us: both `track-b-isolation-forest` and
   `track-d-hdbscan` forked before Track C/Funnel Account landed, which is
   exactly why the reconciliation section above exists — pull `main` right
   before branching, not whenever you happened to start).
2. Build against `data/payments.db` as committed — real resolved data and
   every track's output are already there, start immediately.
3. Write tests the way the existing codebase does (`tests/test_anomaly_features.py`,
   `tests/test_feature_store.py` are the pattern): synthetic fixtures for
   correctness, plus a real-data sanity check you can point to before calling it
   done.
4. PR into `main` when your column(s) are populated and tested. Put a real
   summary in the PR description (sample sizes, score/cluster distribution,
   example entities) — the point is that combining the three signals shouldn't
   be a surprise to anyone.
5. Merge order doesn't matter between B, C, and D — they write independent
   columns. Final-aggregation is the one thing that has to come last, after
   the other three are merged.

**Concluded state**: all four columns (`isolation_forest_score`,
`timeseries_drift_score`/`funnel_drift_score`, `cluster_id`/`cluster_changed`)
plus `final_anomaly_score`/`anomaly_band` are populated on real data —
174 rows scored: 98 Normal, 68 Low-Medium, 6 High, 2 Critical. Query
`GET /anomaly/snapshots?tenant_bank_id=MERIDIAN_TRUST_BANK` for the full
combined output. Next up: Step 6b (Operational Issues, above), then Steps 7–8.

## Running tests

```bash
pytest -q                                   # full suite, 82 tests total. Against the committed real data: 80
                                             # passing, 2 expected KEYBANK failures (see Setup above). On a clean
                                             # DB reset: 81 passing, 1 expected failure (needs real Meridian data --
                                             # test_train_endpoint_scores_real_meridian_data). Never both states at
                                             # once; that's expected, not a regression either way.
pytest tests/test_anomaly_features.py -v    # Track A
pytest tests/test_isolation_forest.py -v    # Track B + final aggregation
pytest tests/test_timeseries.py -v          # Track C (merchant drift + Funnel Account drift)
pytest tests/test_clustering.py -v          # Track D
```
