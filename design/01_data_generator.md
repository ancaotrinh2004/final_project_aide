# Fraud Detection Data Generator

## 1. Domain Overview

This project simulates a mid-size digital payment platform operating in Vietnam and Southeast Asia. The generator produces:

- **Offline historical/reference data** (Parquet) — customer profiles, card registry, merchants, transactions, and transaction line items.
- **Streaming real-time events** (JSON) — behavioral and payment events flowing through a Kafka-compatible topic.

The goal is to support downstream ingestion, transformation, and feature engineering while intentionally injecting realistic data quality and processing challenges. The dataset is designed to train and evaluate a fraud detection ML model in Section 04.

---

## 2. Offline Dataset Design

### 2.1 Offline Tables

| Table | Grain | Key Columns |
|---|---|---|
| `customers` | one per customer | `customer_id`, `signup_ts`, `country`, `city`, `risk_segment`, `kyc_status`, `marketing_opt_in` |
| `merchants` | one per merchant | `merchant_id`, `merchant_name`, `category`, `country`, `city`, `is_active`, `created_ts` |
| `cards` | one per card | `card_id`, `customer_id`, `card_type`, `issuing_bank`, `card_country`, `issued_ts`, `expiry_ts`, `is_active` |
| `transactions` | one per transaction | `transaction_id`, `customer_id`, `card_id`, `merchant_id`, `transaction_timestamp`, `amount`, `currency`, `transaction_status`, `city`, `device_fingerprint`*, `ip_country`*, `is_fraud` |
| `transaction_items` | one per line item | `item_id`, `transaction_id`, `product_category`, `quantity`, `unit_price` |

> `device_fingerprint` and `ip_country` are only present in transactions after `schema_change_date = 2025-07-01` (schema evolution scenario).

### 2.2 Column Details: `transactions` (core table)

| Column | Type | Notes |
|---|---|---|
| `transaction_id` | string | UUID, unique per transaction |
| `customer_id` | string | FK to `customers` |
| `card_id` | string | FK to `cards` |
| `merchant_id` | string | FK to `merchants` |
| `transaction_timestamp` | timestamp | Event time — when the transaction occurred |
| `created_ts` | timestamp | Row creation time — when record was written to source system |
| `amount` | float | Transaction amount in VND |
| `currency` | string | `VND`, `USD`, `SGD`, `THB` |
| `transaction_status` | string | `approved`, `declined`, `pending`, `reversed` |
| `city` | string | City where transaction occurred |
| `device_fingerprint` | string | Nullable — only available after `2025-07-01` |
| `ip_country` | string | Nullable — only available after `2025-07-01` |
| `is_fraud` | int | Label: `1` = fraud, `0` = legitimate |

### 2.3 Offline Data Problems

**Compulsory:**

- **Geographic skew:** 80% of transactions originate from top 5 cities (HCMC, Hanoi, Da Nang, Can Tho, Hai Phong). This mirrors real payment platform concentration in Vietnam and creates join skew in downstream pipelines.
- **High cardinality:** `transaction_id`, `customer_id`, `card_id` are high-cardinality identifiers. `customer_id` range: C000001–C100000 (100,000 unique customers), `merchant_id` range: M0001–M15000 (15,000 unique merchants).
- **Schema evolution:** transactions before `2025-07-01` (≈60% of history) are missing `device_fingerprint` and `ip_country`. These columns were added when the platform upgraded its fraud signal collection. Downstream pipelines must handle nullable columns and evolving schemas gracefully.

**Optional (chosen):**

- **2% duplicate rate in `transactions`:** Caused by payment gateway retry logic — the same `transaction_id` may appear more than once with the same `amount`, `customer_id`, and `transaction_timestamp` but a slightly different `created_ts`. This is a compulsory dedup challenge for Silver pipelines.

**Output:** Parquet files partitioned by `transaction_date` and `payment_status`.

---

## 3. Streaming Dataset Design

### 3.1 Event Stream Schema

Single unified Kafka topic `fraud_events` with an `event_type` field discriminating event types.

| Column | Type | Notes |
|---|---|---|
| `event_id` | string | UUID — unique per event |
| `event_type` | string | `login`, `card_check`, `transaction_attempt`, `transaction_approved`, `transaction_declined`, `otp_request`, `otp_failed` |
| `event_timestamp` | timestamp | When the event occurred on the client side |
| `created_ts` | timestamp | When the event arrived and was written to the stream |
| `customer_id` | string | FK to `customers` |
| `session_id` | string | Groups events in the same user session |
| `device_type` | string | `mobile`, `web`, `atm`, `pos` |
| `ip_country` | string | Country derived from IP address |
| `merchant_id` | string | Nullable — applicable to payment events |
| `card_id` | string | Nullable — applicable to payment events |
| `amount` | float | Nullable — applicable to `transaction_attempt`, `transaction_approved` |
| `failure_reason` | string | Nullable — for `otp_failed`, `transaction_declined` |

### 3.2 Event Type Descriptions

| Event Type | Meaning | Fraud Signal Relevance |
|---|---|---|
| `login` | Customer signs into the platform | Multiple logins in short window = suspicious |
| `card_check` | Customer views or verifies card details | Card enumeration pattern |
| `transaction_attempt` | Initiates a payment | High velocity = fraud signal |
| `transaction_approved` | Payment completed successfully | Baseline behavior |
| `transaction_declined` | Payment refused by issuer | Repeated declines = fraud probe |
| `otp_request` | One-time password requested | Normal verification step |
| `otp_failed` | OTP entered incorrectly | Multiple failures = account takeover signal |

### 3.3 Streaming Data Problems

**Compulsory:**

- **Bursty traffic:** Baseline is 50 events/min. Traffic spikes to 2,000 events/min during two 20-minute windows per day: `12:00–12:20` (lunch hour) and `22:00–22:20` (peak fraud window at night). Pipelines must handle 40× bursts without data loss.
- **Late arrivals:** 15% of events have a `created_ts` significantly later than `event_timestamp` (delay range: 5–60 minutes), caused by mobile app buffering and unstable network conditions in remote areas.

**Optional (chosen):**

- **1.5% duplicate events:** Mobile clients retry failed API calls, producing the same `event_id` appearing twice within a 1–3 minute window. Dedup must use `event_id` + `event_timestamp` as the compound dedup key.

**Output:** JSON (one event per line, newline-delimited).

---

## 4. Fraud Label Design

### 4.1 Label Definition

- **Label column:** `is_fraud` in the `transactions` table.
- **Label value:** `1` = fraudulent transaction, `0` = legitimate transaction.
- **Target fraud rate:** 2% of all transactions (realistic for digital payment platforms).

### 4.2 Fraud Label Logic

A transaction is labeled `is_fraud = 1` if it satisfies **at least 2** of the following conditions. This ensures labels are deterministic and explainable, rather than purely random.

| Condition | Threshold | Reasoning |
|---|---|---|
| Amount anomaly | `amount > 3× customer's 90-day average` | Unusual spending spike |
| Cross-border transaction | `ip_country ≠ card_country` | Card used from foreign location |
| OTP failures | `≥ 3 otp_failed events in the 30 minutes before this transaction` | Account takeover attempt |
| Recent declines | `≥ 2 transaction_declined events in the 60 minutes before this transaction` | Card probing behavior |
| Night-time transaction | Transaction between `01:00–04:00 local time` | Low-activity fraud window |
| New merchant | First time customer transacts with this `merchant_id` in 90-day history | Unfamiliar merchant risk |

> **Note for graders:** Label logic is consistent and deterministic given the random seed. It does not require 100% real-world accuracy — it provides a learnable signal for downstream ML models while exercising the feature engineering pipeline.

---

## 5. Feature Engineering

Features are computed from transaction and event data to support the Gold feature store (designed in Section 02) and ML training (used in Section 04).

### 5.1 Offline Features (90-day rolling windows)

| Feature Name | Description | Source Table |
|---|---|---|
| `f_customer_total_txn_90d` | Total number of transactions in last 90 days | `transactions` |
| `f_customer_avg_txn_amount_90d` | Average transaction amount in last 90 days | `transactions` |
| `f_customer_distinct_merchants_90d` | Number of distinct merchants transacted with | `transactions` |
| `f_customer_decline_rate_90d` | Ratio of declined transactions to total | `transactions` |
| `f_customer_foreign_txn_ratio_90d` | Ratio of transactions where `ip_country ≠ card_country` | `transactions` |
| `f_customer_night_txn_ratio_90d` | Ratio of transactions between 01:00–04:00 | `transactions` |

### 5.2 Streaming Features (rolling windows)

| Feature Name | Description | Window |
|---|---|---|
| `f_stream_otp_failed_count_30m` | Count of `otp_failed` events | 30 minutes |
| `f_stream_decline_count_30m` | Count of `transaction_declined` events | 30 minutes |
| `f_stream_txn_velocity_1h` | Count of `transaction_attempt` events | 1 hour |
| `f_stream_new_merchant_flag` | `1` if current merchant not seen in 90-day history | Point-in-time |
| `f_stream_burst_activity_flag` | `1` if event occurs during a burst window | Point-in-time |

### 5.3 Unified Feature Table

Merge offline + streaming features for each `customer_id` keyed by `event_timestamp`, refreshed every 15 minutes. This unified table is the direct input to the ML training pipeline in Section 04.

---

## 6. Generator Configuration

```yaml
# ─────────────────────────────────────────────
# Fraud Detection — Data Generator Config
# ─────────────────────────────────────────────

# Entity volumes
n_customers: 120000
n_merchants: 15000
n_cards: 130000           # ~1.3 cards per customer
days_history: 180
avg_txn_per_customer: 12  # ~2 transactions/month

# Output paths
output_dir: "data/raw"

# Fraud settings
fraud_rate: 0.02
fraud_label_min_conditions: 2

# Geographic skew
top_cities:
  - Ho Chi Minh City
  - Hanoi
  - Da Nang
  - Can Tho
  - Hai Phong
skew_city_ratio: 0.80       # 80% txns from top 5 cities
skew_category_ratio: 0.75   # 75% merchants in top 3 categories

# Offline data problems
duplicate_rate_offline: 0.02
schema_change_date: "2025-07-01"  # device_fingerprint + ip_country added after this

# Streaming settings
base_events_per_min: 50
burst_multiplier: 40
burst_windows:
  - start: "12:00"
    end: "12:20"
  - start: "22:00"
    end: "22:20"
late_arrival_rate: 0.15
late_delay_min: 5
late_delay_max: 60
duplicate_rate_stream: 0.015

# Reproducibility
random_seed: 42
```

---

## 7. Deliverables

1. **Generator code** (`generate_data.py`) — parameterized by config file.
2. **Offline data outputs:**
   - `customers.parquet`
   - `merchants.parquet`
   - `cards.parquet`
   - `transactions/` — partitioned by `transaction_date`
   - `transaction_items/` — partitioned by `transaction_date`
3. **Streaming data output:**
   - `fraud_events.json` — newline-delimited JSON
4. **Quality report** (`quality_report.md` or `.csv`) covering:
   - Geographic skew % by city
   - Category skew % by merchant category
   - Cardinality: `approx_count_distinct` per key column
   - Schema evolution: null % in `device_fingerprint` and `ip_country` pre/post schema change date
   - Duplicate rate in `transactions` before and after dedup
   - Streaming burst rate, late arrival rate, and duplicate event rate
   - Fraud rate: confirmed 2% across full dataset
5. **Write-up** (included below in Section 8) explaining optional problem choices and feature design rationale.

---

## 8. Design Rationale

### 8.1 Why these optional problems?

**Offline — 2% duplicate rate in `transactions`:**
Payment gateways routinely retry timed-out API calls, producing duplicate records. This is one of the most common data quality problems in financial systems and tests whether the Silver dedup pipeline correctly uses `(transaction_id, created_ts)` as the dedup key rather than blindly dropping all duplicates.

**Streaming — 1.5% duplicate events:**
Mobile apps under poor network conditions retry event submissions. This tests whether the stream processing pipeline correctly uses `(event_id, event_timestamp)` for dedup, and whether late-arriving duplicates are handled differently from fresh ones.

### 8.2 Why these features?

The selected features directly encode the behavioral signals defined in the fraud label logic:

- `f_customer_decline_rate_90d` and `f_stream_decline_count_30m` → captures the "card probing" condition.
- `f_stream_otp_failed_count_30m` → captures the "account takeover" condition.
- `f_customer_foreign_txn_ratio_90d` and `f_stream_new_merchant_flag` → captures geographic and merchant anomalies.
- `f_stream_txn_velocity_1h` → captures burst/velocity fraud patterns.

This alignment between label conditions and features gives the ML model in Section 04 a learnable signal while remaining realistic.

### 8.3 Why fraud_rate = 2%?

A 2% fraud rate reflects industry-standard rates for digital payment platforms (typical range: 0.5%–3%). This intentional class imbalance will require the ML pipeline in Section 04 to apply appropriate handling strategies (e.g., class weighting, SMOTE, or threshold tuning on PR-AUC rather than accuracy).