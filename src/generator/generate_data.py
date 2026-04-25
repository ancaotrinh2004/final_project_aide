"""
src/generator/generate_data.py

Main entry point for the Fraud Detection data generator.

Usage:
    python -m src.generator.generate_data
    python -m src.generator.generate_data --config config/generator_config.yaml

Output:
    data/raw/offline/customers.parquet
    data/raw/offline/merchants.parquet
    data/raw/offline/cards.parquet
    data/raw/offline/transactions/transaction_date=YYYY-MM-DD/*.parquet
    data/raw/offline/transaction_items/transaction_date=YYYY-MM-DD/*.parquet
    data/raw/streaming/fraud_events.json
    data/raw/quality_reports/quality_report.csv
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.generator.offline.customers import generate_customers
from src.generator.offline.merchants import generate_merchants
from src.generator.offline.cards import generate_cards
from src.generator.offline.transactions import generate_transactions
from src.generator.offline.transaction_items import generate_transaction_items
from src.generator.streaming.event_producer import generate_streaming_events
from src.generator.quality.quality_report import compute_offline_quality, save_quality_report


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    print(f"  Saved: {path}  ({len(df):,} rows)")


def save_partitioned_parquet(df: pd.DataFrame, base_path: Path, partition_col: str) -> None:
    """Save DataFrame partitioned by a date column."""
    base_path.mkdir(parents=True, exist_ok=True)
    df[partition_col] = pd.to_datetime(df[partition_col]).dt.date

    for date_val, group in df.groupby(partition_col):
        partition_dir = base_path / f"{partition_col}={date_val}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        out_path = partition_dir / "data.parquet"
        group.drop(columns=[partition_col]).to_parquet(out_path, index=False)

    n_partitions = df[partition_col].nunique()
    print(f"  Saved: {base_path}  ({len(df):,} rows, {n_partitions} partitions)")


def main(config_path: str = "config/generate_config.yaml") -> None:
    start_time = time.time()
    print(f"\n{'='*60}")
    print("Fraud Detection — Data Generator")
    print(f"Config: {config_path}")
    print(f"{'='*60}\n")

    # ── Load config ───────────────────────────────────────────────────────────
    cfg = load_config(config_path)

    rng = np.random.default_rng(cfg["random_seed"])
    output_dir = Path(cfg["output_dir"])
    offline_dir = output_dir / "offline"
    streaming_dir = output_dir / "streaming"
    quality_dir = output_dir / "quality_reports"

    schema_change_date = datetime.strptime(cfg["schema_change_date"], "%Y-%m-%d")

    # ── 1. Customers ──────────────────────────────────────────────────────────
    print("► Generating customers...")
    customers_df = generate_customers(
        n_customers=cfg["n_customers"],
        days_history=cfg["days_history"],
        rng=rng,
    )
    save_parquet(customers_df, offline_dir / "customers.parquet")
    customer_ids = customers_df["customer_id"].tolist()

    # ── 2. Merchants ──────────────────────────────────────────────────────────
    print("\n► Generating merchants...")
    merchants_df = generate_merchants(
        n_merchants=cfg["n_merchants"],
        skew_category_ratio=cfg["skew_category_ratio"],
        rng=rng,
    )
    save_parquet(merchants_df, offline_dir / "merchants.parquet")
    merchant_ids = merchants_df["merchant_id"].tolist()

    # ── 3. Cards ──────────────────────────────────────────────────────────────
    print("\n► Generating cards...")
    cards_df = generate_cards(
        n_cards=cfg["n_cards"],
        customer_ids=customer_ids,
        rng=rng,
    )
    save_parquet(cards_df, offline_dir / "cards.parquet")
    card_ids = cards_df["card_id"].tolist()

    # ── 4. Transactions ───────────────────────────────────────────────────────
    print("\n► Generating transactions...")
    transactions_df = generate_transactions(
        n_customers=cfg["n_customers"],
        avg_txn_per_customer=cfg["avg_txn_per_customer"],
        customer_ids=customer_ids,
        card_ids=card_ids,
        merchant_ids=merchant_ids,
        customer_df=customers_df,
        card_df=cards_df,
        days_history=cfg["days_history"],
        skew_city_ratio=cfg["skew_city_ratio"],
        schema_change_date=schema_change_date,
        fraud_rate=cfg["fraud_rate"],
        fraud_label_min_conditions=cfg["fraud_label_min_conditions"],
        duplicate_rate=cfg["duplicate_rate_offline"],
        rng=rng,
    )
    # Add partition column before saving
    transactions_df["transaction_date"] = pd.to_datetime(
        transactions_df["transaction_timestamp"]
    ).dt.date.astype(str)
    save_partitioned_parquet(
        transactions_df,
        base_path=offline_dir / "transactions",
        partition_col="transaction_date",
    )

    # ── 5. Transaction Items ──────────────────────────────────────────────────
    print("\n► Generating transaction items...")
    items_df = generate_transaction_items(
        transaction_df=transactions_df,
        rng=rng,
    )
    items_df["transaction_date"] = pd.to_datetime(
        items_df["transaction_date"]
    ).dt.date.astype(str)
    save_partitioned_parquet(
        items_df,
        base_path=offline_dir / "transaction_items",
        partition_col="transaction_date",
    )

    # ── 6. Streaming Events ───────────────────────────────────────────────────
    print("\n► Generating streaming events...")
    streaming_summary = generate_streaming_events(
        customer_ids=customer_ids,
        merchant_ids=merchant_ids,
        card_ids=card_ids,
        days_history=cfg["days_history"],
        base_events_per_min=cfg["base_events_per_min"],
        burst_multiplier=cfg["burst_multiplier"],
        burst_windows=cfg["burst_windows"],
        late_arrival_rate=cfg["late_arrival_rate"],
        late_delay_min=cfg["late_delay_min"],
        late_delay_max=cfg["late_delay_max"],
        duplicate_rate=cfg["duplicate_rate_stream"],
        output_path=streaming_dir / "fraud_events.json",
        rng=rng,
    )

    # ── 7. Quality Report ─────────────────────────────────────────────────────
    print("\n► Computing quality report...")
    quality_df = compute_offline_quality(
        customers_df=customers_df,
        merchants_df=merchants_df,
        cards_df=cards_df,
        transactions_df=transactions_df,
        items_df=items_df,
        schema_change_date=schema_change_date,
        streaming_summary=streaming_summary,
    )
    save_quality_report(quality_df, quality_dir)

    elapsed = round(time.time() - start_time, 1)
    print(f"\n{'='*60}")
    print(f"✓ Generation complete in {elapsed}s")
    print(f"  Output directory: {output_dir.resolve()}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fraud Detection Data Generator")
    parser.add_argument(
        "--config",
        default="config/generate_config.yaml",
        help="Path to generator config YAML",
    )
    args = parser.parse_args()
    main(config_path=args.config)