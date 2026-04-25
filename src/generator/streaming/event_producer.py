"""
src/generator/streaming/event_producer.py

Chunk-based streaming event generator (1 chunk = 1 day).
Memory usage stays constant regardless of days_history.
"""

import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from tqdm import tqdm


EVENT_TYPES    = np.array(["login","card_check","transaction_attempt","transaction_approved",
                            "transaction_declined","otp_request","otp_failed"])
EVENT_WEIGHTS  = np.array([0.20, 0.08, 0.25, 0.22, 0.08, 0.12, 0.05])
DEVICE_TYPES   = np.array(["mobile","web","atm","pos"])
DEVICE_WEIGHTS = np.array([0.55, 0.30, 0.08, 0.07])
COUNTRIES      = np.array(["Vietnam","Singapore","Thailand","Malaysia","Philippines","USA"])
COUNTRY_WEIGHTS= np.array([0.70, 0.10, 0.07, 0.06, 0.04, 0.03])
OTP_FAIL_REASONS = np.array(["wrong_code","expired_otp","too_many_attempts"])
DECLINE_REASONS  = np.array(["insufficient_funds","card_blocked","fraud_suspect",
                              "invalid_card","limit_exceeded"])

PAYMENT_EVENT_TYPES = {"transaction_attempt","transaction_approved","transaction_declined",
                        "card_check","otp_request","otp_failed"}
AMOUNT_EVENT_TYPES  = {"transaction_attempt","transaction_approved"}


def _make_uuids(n: int, rng: np.random.Generator) -> np.ndarray:
    raw = rng.bytes(16 * n)
    out = []
    for i in range(n):
        b = bytearray(raw[i*16:(i+1)*16])
        b[6] = (b[6] & 0x0f) | 0x40
        b[8] = (b[8] & 0x3f) | 0x80
        h = b.hex()
        out.append(f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}")
    return np.array(out)


def _generate_day(
    day_start_ts: int,          # unix seconds
    customer_arr, merchant_arr, card_arr,
    base_events_per_min: int,
    burst_multiplier: int,
    burst_windows: list,
    late_arrival_rate: float,
    late_delay_min: int,
    late_delay_max: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate all events for a single day. Returns a DataFrame."""
    MINUTES_PER_DAY = 1440

    # Per-minute counts
    minute_unix = day_start_ts + np.arange(MINUTES_PER_DAY, dtype=np.int64) * 60
    hour_of_day = (minute_unix % 86400) // 3600
    min_of_hour  = (minute_unix % 3600)  // 60

    in_burst = np.zeros(MINUTES_PER_DAY, dtype=bool)
    for w in burst_windows:
        sh, sm = map(int, w["start"].split(":"))
        eh, em = map(int, w["end"].split(":"))
        cur = hour_of_day * 60 + min_of_hour
        in_burst |= (cur >= sh * 60 + sm) & (cur <= eh * 60 + em)

    rates  = np.where(in_burst, base_events_per_min * burst_multiplier, base_events_per_min)
    counts = np.maximum(1, (rates * rng.uniform(0.8, 1.2, size=MINUTES_PER_DAY)).astype(int))
    n = int(counts.sum())

    # Expand to per-event
    min_idx      = np.repeat(np.arange(MINUTES_PER_DAY), counts)
    event_unix   = day_start_ts + min_idx * 60 + rng.integers(0, 60, size=n)

    # Columns
    event_types  = rng.choice(EVENT_TYPES, size=n, p=EVENT_WEIGHTS)
    pay_mask     = np.isin(event_types, list(PAYMENT_EVENT_TYPES))
    amt_mask     = np.isin(event_types, list(AMOUNT_EVENT_TYPES))
    otp_mask     = event_types == "otp_failed"
    dec_mask     = event_types == "transaction_declined"

    merchant_col = np.where(pay_mask, rng.choice(merchant_arr, size=n), None)
    card_col     = np.where(pay_mask, rng.choice(card_arr,     size=n), None)
    amounts      = np.where(amt_mask, np.round(rng.lognormal(13.0, 1.2, size=n), 2), np.nan)

    failure_col  = np.full(n, None, dtype=object)
    if otp_mask.any():
        failure_col[otp_mask] = rng.choice(OTP_FAIL_REASONS, size=otp_mask.sum())
    if dec_mask.any():
        failure_col[dec_mask] = rng.choice(DECLINE_REASONS,  size=dec_mask.sum())

    # Timestamps
    late_mask    = rng.random(size=n) < late_arrival_rate
    lag          = np.where(late_mask,
                            rng.integers(late_delay_min*60, late_delay_max*60, size=n),
                            rng.integers(1, 10, size=n))
    created_unix = event_unix + lag

    event_ids = _make_uuids(n, rng)
    session_nums = rng.integers(1, 9_999_999, size=n)

    df = pd.DataFrame({
        "event_id":        event_ids,
        "event_type":      event_types,
        "event_timestamp": pd.to_datetime(event_unix,   unit="s").strftime("%Y-%m-%dT%H:%M:%S"),
        "created_ts":      pd.to_datetime(created_unix, unit="s").strftime("%Y-%m-%dT%H:%M:%S"),
        "customer_id":     rng.choice(customer_arr, size=n),
        "session_id":      np.char.add("S", np.char.zfill(session_nums.astype(str), 7)),
        "device_type":     rng.choice(DEVICE_TYPES, size=n, p=DEVICE_WEIGHTS),
        "ip_country":      rng.choice(COUNTRIES,    size=n, p=COUNTRY_WEIGHTS),
        "merchant_id":     merchant_col,
        "card_id":         card_col,
        "amount":          amounts,
        "failure_reason":  failure_col,
    })
    df["amount"] = df["amount"].where(df["amount"].notna(), other=None)

    return df, int(late_mask.sum()), int(in_burst[min_idx].sum())


def generate_streaming_events(
    customer_ids, merchant_ids, card_ids, days_history,
    base_events_per_min, burst_multiplier, burst_windows,
    late_arrival_rate, late_delay_min, late_delay_max,
    duplicate_rate, output_path, rng,
) -> dict:
    """
    Generate streaming events day-by-day.
    Peak RAM = ~1 day of events (~70k rows at default config) instead of full dataset.
    """
    customer_arr = np.array(customer_ids)
    merchant_arr = np.array(merchant_ids)
    card_arr     = np.array(card_ids)

    end_date   = datetime(2025, 10, 1)
    start_date = end_date - timedelta(days=days_history)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_events = 0
    total_late   = 0
    total_burst  = 0
    dupe_buffer  = []   # collect rows to duplicate

    print(f"  [streaming] Generating {days_history} days chunk-by-chunk (1 chunk = 1 day)...")

    with open(output_path, "w") as f:
        for day_offset in tqdm(range(days_history), desc="Generating events", unit="day"):
            day_dt       = start_date + timedelta(days=day_offset)
            day_start_ts = int(day_dt.timestamp())

            df_day, n_late, n_burst = _generate_day(
                day_start_ts=day_start_ts,
                customer_arr=customer_arr,
                merchant_arr=merchant_arr,
                card_arr=card_arr,
                base_events_per_min=base_events_per_min,
                burst_multiplier=burst_multiplier,
                burst_windows=burst_windows,
                late_arrival_rate=late_arrival_rate,
                late_delay_min=late_delay_min,
                late_delay_max=late_delay_max,
                rng=rng,
            )

            total_events += len(df_day)
            total_late   += n_late
            total_burst  += n_burst

            # Collect ~duplicate_rate rows to dupe later (reservoir sample)
            n_to_sample = max(0, int(len(df_day) * duplicate_rate))
            if n_to_sample > 0:
                sample_idx = rng.choice(len(df_day), size=n_to_sample, replace=False)
                dupe_buffer.append(df_day.iloc[sample_idx].copy())

            # Write day chunk
            records = df_day.to_dict(orient="records")
            f.write("\n".join(json.dumps(r, default=str) for r in records) + "\n")

        # Write duplicates at the end (out-of-order, realistic retry)
        if dupe_buffer:
            dupes_df = pd.concat(dupe_buffer, ignore_index=True)
            n_dupes  = len(dupes_df)
            retry_delays = rng.integers(60, 180, size=n_dupes)
            orig_unix    = pd.to_datetime(dupes_df["created_ts"]).astype(np.int64) // 10**9
            dupes_df["created_ts"] = pd.to_datetime(
                orig_unix.values + retry_delays, unit="s"
            ).strftime("%Y-%m-%dT%H:%M:%S")

            records = dupes_df.to_dict(orient="records")
            f.write("\n".join(json.dumps(r, default=str) for r in records) + "\n")
        else:
            n_dupes = 0

    total_with_dupes = total_events + n_dupes

    summary = {
        "total_events":             total_with_dupes,
        "base_events":              total_events,
        "duplicate_events":         n_dupes,
        "late_events":              total_late,
        "burst_events":             total_burst,
        "late_arrival_rate_actual": round(total_late / max(total_events, 1), 4),
        "duplicate_rate_actual":    round(n_dupes    / max(total_events, 1), 4),
        "burst_event_share":        round(total_burst / max(total_events, 1), 4),
    }

    print(f"  [streaming] Done. Total: {total_with_dupes:,} "
          f"(base: {total_events:,}, dupes: {n_dupes:,}, late: {total_late:,})")
    return summary