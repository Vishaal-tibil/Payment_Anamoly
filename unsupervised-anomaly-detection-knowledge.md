# Unsupervised Transaction Anomaly Detection — Knowledge File

## 1. Objective
Build a fully unsupervised anomaly detection system for payment transactions — no anomaly labels or flags used in training. Detection happens at two entity levels:
- **Merchants**
- **Individual accounts**

Three complementary techniques are combined:
- **Isolation Forest** — point/behavioral anomalies
- **HDBSCAN clustering** — unusual behavioral groups and behavior shifts
- **Time-series detection** — spikes, drifts, and temporal anomalies

Output: an **anomaly score (0–100)** plus the contributing signals — not a binary fraud label.

## 2. Core Principle
The system doesn't learn "what fraud looks like." It learns **what normal behavior looks like for each merchant/account**, then scores new transactions by how far they deviate from that entity's own baseline. This is a *profile-based* system, not one global model.

## 3. High-Level Flow
```
Raw Transactions → Data Prep → Entity ID (Merchant / Account)
→ Behavioral Feature Store → [Isolation Forest + HDBSCAN + Time Series]
→ Risk Aggregator → Merchant Score + Account Score → Final Score
→ Normal / Review / Critical
```

## 4. Data Preparation
- Merge pre-settlement + post-settlement data; standardize timestamps, amounts, currencies, IDs.
- Sort chronologically; identify merchant, originating account, beneficiary.
- Dedupe, handle missing values.
- **Strip all existing anomaly/threshold flags** (e.g., `new_payee_risk_flag`, `velocity_threshold_breached`, `network_timeout_flag`) from model inputs — these represent the answer, not a feature.

## 5. Feature Engineering
Build **entity-level behavioral snapshots**, not raw transaction rows, across rolling windows: 5m / 15m / 1h / 6h / 24h / 7d.

**Merchant features:** transaction counts per window, amount totals/avg/median/std, unique beneficiaries, new-beneficiary ratio, retry ratio, response-time stats, timeout ratio, settlement delay.

**Account features:** transaction counts, amount stats, unique beneficiaries/merchants, new-payee ratio, retry ratio, response time, timeout ratio, amount z-score, velocity ratio, relationship/account age.

Derive these from raw history — don't reuse pre-built anomaly/velocity flags already in the schema.

## 6. Training Data Split
Chronological, not random (e.g., Jan–Jun train / Jul validate / Aug test) to avoid future data leaking into the baseline.

## 7. Model Layers

| Layer | Purpose | Notes |
|---|---|---|
| **Isolation Forest** | Flags feature combinations far from an entity's historical norm | Trained per-entity on behavioral snapshots |
| **HDBSCAN** | Groups entities into behavioral clusters; flags cluster changes/noise | Preferred over K-Means since the number of natural groups is unknown |
| **Time-series** | Detects spikes/drift per entity | Start with rolling mean/std, z-score, EWMA, CUSUM before LSTM/Transformer |

A cluster is not automatically "fraud" — a cluster **change** (e.g., a merchant moving from Cluster 2 to Cluster 7) is the useful signal.

## 8. Score Aggregation
```
Entity Score = 0.40 × IsolationForest + 0.25 × Clustering + 0.35 × TimeSeries
Final Score  = combine(Merchant Score, Account Score, transaction-level signals)
```
Weights are starting points — calibrate later using analyst feedback and investigation outcomes.

Score bands (example): 0–30 Normal · 30–60 Low/Medium · 60–80 High · 80–100 Critical.

## 9. Handling Insufficient History
Not every entity has enough data for its own model. Use a fallback hierarchy:

| Observations | Approach |
|---|---|
| < 50 | Global / segment baseline (by merchant type, rail, currency, geography) |
| 50–200 | Entity-specific baseline |
| > 200 | Entity-specific Isolation Forest + clustering + time-series |

Thresholds should be tuned experimentally, not assumed.

## 10. Mapping to Anomaly Categories

**Fraud:**
- *New Payee* — account baseline + Isolation Forest + clustering
- *Funnel Account* — beneficiary/account relationship features (unique senders, new-sender ratio) + time-series + eventual graph analysis
- *Velocity* — time-series + Isolation Forest
- *Structuring* — time-series + amount distribution + threshold-relative features + clustering

**Operational:**
- *Network/Processor Timeout* — response-time time series
- *Batch/File Not Reaching Settlement* — settlement-lifecycle time series
- *Duplicate Payment (Retry)* — transaction similarity + time proximity + account/beneficiary/amount/reference matching
- *Formatting Rejection* — validation rules + statistical monitoring

**Reconciliation:**
- *Network-to-Ledger Mismatch* — deterministic amount/reference matching first, then statistical anomaly detection on the variance itself (not Isolation Forest as primary detector)

## 11. Inference Flow (New Transaction)
```
TX → identify Merchant + Account → pull historical profiles
→ update rolling features → score IF, HDBSCAN, time-series
→ aggregate → Merchant Score + Account Score → Final Score → Alert / Normal
```

## 12. Model Lifecycle
- **Training:** historical data → features → entity profiles → train IF/HDBSCAN/time-series baselines → save model + feature config.
- **Inference:** as above.
- **Retraining:** periodic batch retraining + rolling baseline updates + new-entity onboarding + drift monitoring.

## 13. Known Constraint (POC-specific)
Current dataset (~520 pre-settlement + ~500 post-settlement transactions) is too small for reliable independent per-entity models. Use it to validate the pipeline/feature logic; production needs substantially more history per entity. Track observation counts, model version, training period, feature version, score distribution, and alert rate over time.

## 14. Key Takeaway
Don't build "a fraud model." Build **behavioral baselines per merchant and per account**, then measure deviation from those baselines. This is both more accurate (accounts for scale differences between entities) and easier to explain operationally ("unusual for this merchant" vs. "IF score = 0.87").
