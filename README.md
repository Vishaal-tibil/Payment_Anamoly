# Merchant Payment Intelligence Platform — Backend

FastAPI service for a multi-tenant payment intelligence platform. It ingests
transaction data across five real payment rails (ACH, WIRE, CARD, FEDNOW,
CHEQUE), normalises it into one canonical event shape, resolves every
transaction to a merchant or individual, and produces four intelligence
outcomes per party: **Fraud/Anomaly**, **Operational Issues**,
**Reconciliation**, and **Payment Health**.

Purely observational — dashboards, alerts and reports. It never gates, holds
or declines a live transaction.

Frontend lives in a separate repo: `Payment_Anamoly_Frontend`.

---

## The one rule that shaped this codebase

**Never fabricate a number.** If a figure isn't derivable from real ingested
data, the API doesn't return it and the UI says so plainly. This is why you
will find endpoints that honestly return `available: false`, charts that were
deleted rather than filled with plausible-looking data, and comments
explaining why a metric *can't* exist yet.

Two concrete consequences worth knowing before you extend anything:

- **Models never read labels or pre-computed risk flags.** `party_features`
  (Step 5) is a dashboard summary built *from* the source's own flags — it is
  never a model input. `tests/test_anomaly_features.py::test_snapshot_values_are_unaffected_by_the_flags_it_must_not_use`
  is the regression test that enforces this.
- **`detected_at` is not an event time.** It's a batch-compute-run timestamp;
  every row from one run shares it. Anything time-based must anchor on the
  real underlying event date — that's what `app/claim_dates.py` exists for.

---

## Architecture

```
Excel/CSV ─► ingest ─► CanonicalEvent ─► identity resolution ─► EntitySnapshot
                                                                     │
                    ┌────────────────────────────────────────────────┤
                    ▼                    ▼                ▼          ▼
             Fraud/Anomaly        Operational       Reconciliation  Payment
             (Tracks A–D)           Issues                          Health
                    │                    │                │          │
                    └────────────┬───────┴────────────────┴──────────┘
                                 ▼
                    read-only aggregation + AI narration
                                 ▼
                        REST API  ──►  React frontend
```

| Module | What it owns |
|---|---|
| `app/canonical_store.py`, `app/resolution.py` | Ingestion, canonical shape, merchant/individual identity |
| `app/anomaly/` | Fraud engine — Isolation Forest, HDBSCAN clustering, time-series drift, funnel accounts |
| `app/operations/` | Duplicate payments, format rejections, batch-not-settled, timeout spikes |
| `app/reconciliation/` | Confirmed breaks and provisional variances |
| `app/health/` | Composite Payment Health score |
| `app/investigation/` | Case clustering, SLA ageing, weekly trends, AI patterns |
| `app/review/` | Analyst confirm/dismiss ledger — the real model-feedback loop |
| `app/agent/` | Mistral narration (Step 7) |
| `app/priority.py` | Peer-relative 4-band severity (Critical/High/Medium/Low) |
| `app/dashboard.py`, `app/exposure.py` | Read-only aggregation — reshape, never recompute |
| `app/routers/` | `analyst.py`, `head_of_operations.py`, `pipeline.py` |

**60 endpoints** (39 GET, 21 POST). Full contract: `docs/openapi.json`.

Deep design rationale and per-track history: **[docs/engineering-notes.md](docs/engineering-notes.md)**.

---

## The ML surface

All four fraud tracks write back onto the *same* `EntitySnapshot` rows — no
separate output tables, no model artifacts.

| Track | Technique | Writes |
|---|---|---|
| A | Behavioural feature snapshots | every raw-derived column |
| B | Isolation Forest (`scikit-learn`) | `isolation_forest_score`, `final_anomaly_score`, `anomaly_band` |
| C | Rolling z-score drift | `timeseries_drift_score`, `funnel_drift_score` |
| D | HDBSCAN (`sklearn.cluster.HDBSCAN`) | `cluster_id`, `cluster_changed` |

**Nothing is serialised to disk.** Every engine retrains in memory on each
compute run and writes its scores back to the database. There is no model
registry, no `.pkl`, and nothing to mount besides the database itself — which
is why the Docker image needs no model volume.

HDBSCAN comes from **scikit-learn ≥ 1.3**, not the standalone `hdbscan`
package. scikit-learn links OpenMP at import time, so the runtime image
installs `libgomp1` — without it, imports fail the first time someone
triggers a compute run, not at build time.

---

## AI narration (Mistral)

`POST /agent/narrate` turns an incident's real computed facts into a short
analyst-readable narrative plus 1–3 ranked recommended actions.

- **Grounded in code, not just in the prompt.** The response is rejected
  unless the real identifier (`party_id`, `transaction_id`, `case_code`…)
  appears verbatim in the output. A truncated or paraphrased identifier is a
  correctness bug in an operational tool, so it's checked, not trusted.
- **Cached** in `agent_narratives`, keyed by
  `(signal_type, tenant, reference_id)`. Measured: **~14s cold, ~200ms
  cached**. Never called on page load — only from an explicit user action.
- **Optional.** Without a key the narration endpoints fail with a clear error
  and every other endpoint keeps working.
- The shared API tier intermittently returns a mis-worded `403` that is really
  a burst rate limit; the client retries with backoff.

### Quota failover across multiple keys

The shared tier exhausts. Configure more than one key and narration moves to
the next one automatically instead of going dark mid-demo:

```bash
MISTRAL_API_KEY=primary-key
MISTRAL_API_KEY_2=spare-key
MISTRAL_API_KEY_3=another-spare
# or, for container env vars:
# MISTRAL_API_KEYS=key1,key2,key3
```

This is **failover, not load balancing** — the primary key is always preferred
and spares stay untouched until it genuinely fails. Numbered variables must be
contiguous (`_2`, then `_3`): a gap ends the list, so a typo can't silently
leave a key unused.

A key is only abandoned when its own failure says so:

| Failure | Behaviour |
|---|---|
| `401` / invalid key | Skipped immediately — no backoff burned on a credential that can never work |
| `429` / quota / `tier_not_allowed` | Retried on that key first (this tier's most common error is genuinely transient), then failover |
| Anything else | Retried on the same key |

If every key is exhausted the call still raises, so the UI falls back to the
plain-facts display rather than showing anything ungrounded.

Verify your keys — one real call each, key values never printed:

```bash
python scripts/check_mistral_keys.py
```

---

## Setup

Requires **Python 3.11+**.

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS/Linux
pip install -r requirements.txt

cp .env.example .env             # then add MISTRAL_API_KEY (optional)
uvicorn app.main:app --reload --port 8000
```

`data/payments.db` is committed and already contains real resolved
`MERIDIAN_TRUST_BANK` data with all engines computed — the API is useful
immediately, no pipeline run needed.

Interactive docs: <http://localhost:8000/docs>

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MISTRAL_API_KEY` | — | AI narration. Omit and narration is disabled; nothing else breaks. |
| `MISTRAL_MODEL` | `mistral-large-latest` | Narration model. |
| `DB_PATH` | `data/payments.db` | SQLite file. Point at a mounted volume in Docker. |
| `DATA_DIR` | `data` | Parent directory, created on boot if missing. |
| `CORS_ALLOW_ORIGINS` | *(unset)* | Comma-separated exact origins. **When set, the localhost dev regex no longer applies.** |

### Tests

```bash
python -m pytest tests/ -q      # 307 tests
```

Tests share the committed file-backed database, so anything writing to it uses
get-or-create helpers or an explicit reset fixture. If you add a test that
seeds real rows, follow that pattern or you'll create order-dependent failures.

---

## Running the full pipeline on fresh data

Only needed for a new tenant or new source files — the committed database is
already computed. Order matters; each step reads the previous step's output:

```bash
POST /ingest/file                              # per rail, PRE then POST
POST /resolve/parties                          # merchant/individual identity
POST /features/compute
POST /anomaly/snapshots/compute                # Track A
POST /anomaly/isolation-forest/train           # Track B
POST /anomaly/clustering/compute               # Track D
POST /anomaly/timeseries/compute               # Track C
POST /anomaly/beneficiary-snapshots/compute
POST /anomaly/funnel/compute
POST /anomaly/final-score/compute              # blends B+C+D
POST /operations/{duplicate-payments,format-rejections,format-rejections/spikes,batches,timeout}/compute
POST /reconciliation/breaks/compute
POST /health/compute
POST /investigation/cases/compute              # clusters everything above
```

---

## Deployment — Docker on EC2

The image is a two-stage build: stage 1 resolves wheels (build toolchain
included), stage 2 ships only the virtualenv and the app. Runs as a non-root
user on port 8000.

**The database is a mounted volume, never baked into the image.** It holds
ingested transactions, analyst reviews and cached narratives — all of which
must survive a redeploy.

### 1. Launch and prepare the instance

t3.small or larger (scikit-learn wants headroom during compute runs).
Security group: allow 443 from the world, 22 from your IP. **Do not expose
8000 publicly** — there is no authentication yet.

```bash
sudo yum install -y docker git && sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user     # re-login after this
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/local/bin/docker-compose && sudo chmod +x /usr/local/bin/docker-compose
```

### 2. Build and run

```bash
git clone <this-repo> && cd Payment_Anamoly

cat > .env <<'EOF'
MISTRAL_API_KEY=sk-your-real-key
MISTRAL_MODEL=mistral-large-latest
CORS_ALLOW_ORIGINS=https://your-project.web.app,https://your-project.firebaseapp.com
EOF
chmod 600 .env

docker compose up -d --build
docker compose logs -f api
curl localhost:8000/healthz          # {"status":"ok"}
```

`CORS_ALLOW_ORIGINS` must list **both** Firebase domains (`.web.app` and
`.firebaseapp.com`), plus any custom domain. Miss one and the browser blocks
every request from it.

### 3. Seed the database

The volume starts empty and the app creates an empty schema on boot. Load the
committed demo database:

```bash
docker cp data/payments.db payment-anomaly-api:/data/payments.db
docker compose restart api
```

For a genuinely new tenant, skip this and run the pipeline sequence above
instead.

### 4. TLS

Firebase serves over HTTPS, so browsers block calls to a plain-http API.
Put nginx + certbot (or an ALB with ACM) in front, proxying 443 → 127.0.0.1:8000.

```nginx
server {
    listen 443 ssl;
    server_name api.your-domain.com;
    ssl_certificate     /etc/letsencrypt/live/api.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.your-domain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # AI narration takes ~14s; compute runs longer
    }
}
```

The long `proxy_read_timeout` matters — a default 60s cuts off pipeline
compute runs mid-request.

### Operations

```bash
docker compose logs -f api                                          # logs
docker compose up -d --build                                        # redeploy (volume survives)
docker run --rm -v payment-anomaly_payments-data:/data \
  -v $(pwd):/backup alpine tar czf /backup/db-$(date +%F).tgz /data  # back up
```

### Before this is more than a pilot

Honest limitations, not oversights:

1. **No authentication.** CORS restricts which *browser origins* may call the
   API; it authenticates nobody. Anything with network access can call every
   endpoint. Put a real auth layer in front before non-demo data.
2. **SQLite, one worker.** WAL mode handles concurrent readers well, but
   multiple worker processes writing one file is how that breaks. Scale by
   moving to Postgres (a `create_engine` URL change) *before* adding workers.
3. **Single tenant in the UI.** The backend is tenant-scoped throughout; the
   frontend hardcodes `MERIDIAN_TRUST_BANK`.
4. **Compute endpoints are unauthenticated and synchronous.** Move them to a
   queue or schedule before real load.

---

## Performance notes

`CanonicalEventLookup` deliberately defers the 9 JSON columns on
`CanonicalEvent` — deserialising them was ~97% of its cost and nothing reading
through it touches them. Removing that deferral silently puts ~5.6x back on
nearly every dashboard endpoint. If you need a JSON column, query it
separately.

Measured page loads after that work: all under 450ms; Analyst → Anomalies went
3760ms → 231ms.
