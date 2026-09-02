"""
Synthetic data generator for Payment Reconciliation Engine.

Produces paired ledger_transactions and settlement_records datasets with
configurable anomaly injection. Anomaly types mirror real payment-ops scenarios:

  - Settlement delays (settlement arrives days after charge)
  - Partial refunds (one-to-many ledger→settlement)
  - Duplicate transactions (same ref, same or conflicting amounts)
  - Currency rounding differences (sub-cent discrepancies)
  - Out-of-order arrivals (settlement before ledger finalization)
  - Currency mismatches (different currency codes)
  - Missing counterparts (ledger-only or settlement-only orphans)

Usage:
    python -m reconciliation.generator --num-records 10000 --anomaly-rate 0.15 --seed 42
"""

import csv
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import click
import numpy as np
import pandas as pd
from faker import Faker

from reconciliation.config import settings


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURRENCIES = ["USD", "EUR", "GBP", "INR", "JPY"]
CURRENCY_MINOR_UNITS = {"USD": 2, "EUR": 2, "GBP": 2, "INR": 2, "JPY": 0}

ANOMALY_TYPES = [
    "settlement_delay",
    "partial_refund",
    "duplicate_same_amount",
    "duplicate_conflicting_amount",
    "rounding_difference",
    "out_of_order",
    "currency_mismatch",
    "missing_settlement",
    "missing_ledger",
]

# Weights control how frequently each anomaly type is injected
ANOMALY_WEIGHTS = [
    0.20,  # settlement_delay
    0.15,  # partial_refund
    0.10,  # duplicate_same_amount
    0.05,  # duplicate_conflicting_amount
    0.15,  # rounding_difference
    0.10,  # out_of_order
    0.05,  # currency_mismatch
    0.10,  # missing_settlement
    0.10,  # missing_ledger
]


def _round_to_currency(amount: float, currency: str) -> float:
    """Round to the appropriate minor-unit precision for a currency."""
    decimals = CURRENCY_MINOR_UNITS.get(currency, 2)
    return round(amount, decimals)


def _generate_base_transactions(
    num_records: int,
    seed: int,
    fake: Faker,
    rng: random.Random,
) -> pd.DataFrame:
    """
    Generate clean, perfectly matching base transaction pairs.

    Returns a DataFrame with columns for both ledger and settlement data,
    before any anomalies are injected.
    """
    np.random.seed(seed)

    records = []
    base_date = datetime(2024, 1, 1, tzinfo=timezone.utc)

    for i in range(num_records):
        order_id = f"ORD-{uuid.UUID(int=rng.getrandbits(128)).hex[:12].upper()}"
        customer_id = f"CUST-{rng.randint(1000, 9999)}"
        currency = rng.choice(CURRENCIES)
        amount = _round_to_currency(rng.uniform(5.00, 5000.00), currency)

        # Fee is typically 2-3.5% of gross
        fee_rate = rng.uniform(0.02, 0.035)
        fee_amount = _round_to_currency(amount * fee_rate, currency)
        net_amount = _round_to_currency(amount - fee_amount, currency)

        # Settlement typically arrives 1-3 days after creation
        created_at = base_date + timedelta(
            days=rng.randint(0, 180),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        settled_at = created_at + timedelta(
            days=rng.randint(1, 3),
            hours=rng.randint(0, 12),
        )
        batch_id = f"BATCH-{settled_at.strftime('%Y%m%d')}-{rng.randint(1, 5)}"

        records.append({
            "order_id": order_id,
            "customer_id": customer_id,
            "ledger_amount": amount,
            "currency": currency,
            "ledger_status": "completed",
            "created_at": created_at,
            "updated_at": created_at,
            "external_transaction_ref": order_id,  # clean match
            "gross_amount": amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "settlement_currency": currency,
            "settled_at": settled_at,
            "settlement_status": "settled",
            "source_batch_id": batch_id,
            "anomaly_type": "none",
        })

    return pd.DataFrame(records)


def _inject_anomalies(
    df: pd.DataFrame,
    anomaly_rate: float,
    rng: random.Random,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Inject anomalies into a fraction of records.

    Returns the modified DataFrame and a count dict of injected anomaly types.
    Anomalies are selected by weighted random choice and applied in place.
    """
    n_anomalies = int(len(df) * anomaly_rate)
    anomaly_indices = rng.sample(range(len(df)), min(n_anomalies, len(df)))

    counts: dict[str, int] = {t: 0 for t in ANOMALY_TYPES}
    extra_rows = []  # for partial refunds and duplicates that add rows

    for idx in anomaly_indices:
        anomaly = rng.choices(ANOMALY_TYPES, weights=ANOMALY_WEIGHTS, k=1)[0]
        row = df.iloc[idx]
        currency = row["currency"]

        if anomaly == "settlement_delay":
            # Settlement arrives 6-15 days late (beyond normal window)
            delay = timedelta(days=rng.randint(6, 15))
            df.at[idx, "settled_at"] = row["created_at"] + delay
            df.at[idx, "anomaly_type"] = "settlement_delay"
            counts["settlement_delay"] += 1

        elif anomaly == "partial_refund":
            # Create 1-3 partial refund settlement entries
            original_amount = float(row["gross_amount"])
            num_refunds = rng.randint(1, 3)
            remaining = original_amount
            df.at[idx, "ledger_status"] = "partially_refunded"
            df.at[idx, "anomaly_type"] = "partial_refund"

            for r in range(num_refunds):
                if r == num_refunds - 1:
                    refund_amount = _round_to_currency(remaining * 0.3, currency)
                else:
                    refund_amount = _round_to_currency(
                        remaining * rng.uniform(0.1, 0.4), currency
                    )
                remaining -= refund_amount

                refund_settled = row["settled_at"] + timedelta(days=rng.randint(1, 10))
                extra_rows.append({
                    "order_id": row["order_id"],
                    "customer_id": row["customer_id"],
                    "ledger_amount": -refund_amount,
                    "currency": currency,
                    "ledger_status": "refunded",
                    "created_at": row["created_at"] + timedelta(days=rng.randint(1, 5)),
                    "updated_at": row["created_at"] + timedelta(days=rng.randint(1, 5)),
                    "external_transaction_ref": row["order_id"],
                    "gross_amount": -refund_amount,
                    "fee_amount": 0,
                    "net_amount": -refund_amount,
                    "settlement_currency": currency,
                    "settled_at": refund_settled,
                    "settlement_status": "settled",
                    "source_batch_id": f"BATCH-{refund_settled.strftime('%Y%m%d')}-R",
                    "anomaly_type": "partial_refund",
                })
            counts["partial_refund"] += 1

        elif anomaly == "duplicate_same_amount":
            # Exact duplicate settlement record
            dup_row = row.to_dict()
            dup_row["settled_at"] = row["settled_at"] + timedelta(seconds=rng.randint(1, 300))
            dup_row["anomaly_type"] = "duplicate_same_amount"
            extra_rows.append(dup_row)
            df.at[idx, "anomaly_type"] = "duplicate_same_amount"
            counts["duplicate_same_amount"] += 1

        elif anomaly == "duplicate_conflicting_amount":
            # Duplicate with different amount — true discrepancy
            dup_row = row.to_dict()
            amount_diff = _round_to_currency(rng.uniform(1.0, 50.0), currency)
            dup_row["gross_amount"] = float(row["gross_amount"]) + amount_diff
            dup_row["net_amount"] = float(row["net_amount"]) + amount_diff
            dup_row["settled_at"] = row["settled_at"] + timedelta(seconds=rng.randint(1, 300))
            dup_row["anomaly_type"] = "duplicate_conflicting_amount"
            extra_rows.append(dup_row)
            df.at[idx, "anomaly_type"] = "duplicate_conflicting_amount"
            counts["duplicate_conflicting_amount"] += 1

        elif anomaly == "rounding_difference":
            # Sub-cent rounding noise on the settlement side
            epsilon = rng.choice([-0.01, 0.01, -0.02, 0.02])
            if currency == "JPY":
                epsilon = rng.choice([-1, 1])
            df.at[idx, "gross_amount"] = float(row["gross_amount"]) + epsilon
            df.at[idx, "net_amount"] = float(row["net_amount"]) + epsilon
            df.at[idx, "anomaly_type"] = "rounding_difference"
            counts["rounding_difference"] += 1

        elif anomaly == "out_of_order":
            # Settlement timestamp before ledger creation
            df.at[idx, "settled_at"] = row["created_at"] - timedelta(
                hours=rng.randint(1, 48)
            )
            df.at[idx, "anomaly_type"] = "out_of_order"
            counts["out_of_order"] += 1

        elif anomaly == "currency_mismatch":
            # Settlement in a different currency than ledger
            other_currencies = [c for c in CURRENCIES if c != currency]
            df.at[idx, "settlement_currency"] = rng.choice(other_currencies)
            df.at[idx, "anomaly_type"] = "currency_mismatch"
            counts["currency_mismatch"] += 1

        elif anomaly == "missing_settlement":
            # No settlement record — mark settlement columns as None
            df.at[idx, "external_transaction_ref"] = None
            df.at[idx, "gross_amount"] = None
            df.at[idx, "fee_amount"] = None
            df.at[idx, "net_amount"] = None
            df.at[idx, "settled_at"] = None
            df.at[idx, "settlement_status"] = None
            df.at[idx, "source_batch_id"] = None
            df.at[idx, "anomaly_type"] = "missing_settlement"
            counts["missing_settlement"] += 1

        elif anomaly == "missing_ledger":
            # No ledger record — mark ledger columns as None
            df.at[idx, "order_id"] = None
            df.at[idx, "customer_id"] = None
            df.at[idx, "ledger_amount"] = None
            df.at[idx, "ledger_status"] = None
            df.at[idx, "created_at"] = None
            df.at[idx, "updated_at"] = None
            # Generate a settlement-only ref
            df.at[idx, "external_transaction_ref"] = f"EXT-{uuid.uuid4().hex[:12].upper()}"
            df.at[idx, "anomaly_type"] = "missing_ledger"
            counts["missing_ledger"] += 1

    # Append extra rows (duplicates, partial refunds)
    if extra_rows:
        extra_df = pd.DataFrame(extra_rows)
        df = pd.concat([df, extra_df], ignore_index=True)

    return df, counts


def split_to_datasets(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split combined DataFrame into separate ledger and settlement datasets.

    Filters out rows with null key columns to produce clean per-source files.
    """
    # Ledger: rows that have order_id
    ledger_mask = df["order_id"].notna()
    ledger_df = df.loc[ledger_mask, [
        "order_id", "customer_id", "ledger_amount", "currency",
        "ledger_status", "created_at", "updated_at",
    ]].copy()
    ledger_df.columns = [
        "order_id", "customer_id", "amount", "currency",
        "status", "created_at", "updated_at",
    ]

    # Settlement: rows that have external_transaction_ref
    settlement_mask = df["external_transaction_ref"].notna()
    settlement_df = df.loc[settlement_mask, [
        "external_transaction_ref", "gross_amount", "fee_amount",
        "net_amount", "settlement_currency", "settled_at",
        "settlement_status", "source_batch_id",
    ]].copy()
    settlement_df.columns = [
        "external_transaction_ref", "gross_amount", "fee_amount",
        "net_amount", "currency", "settled_at",
        "status", "source_batch_id",
    ]

    return ledger_df, settlement_df


def generate(
    num_records: int = 10000,
    anomaly_rate: float = 0.15,
    seed: int = 42,
    output_dir: Optional[str] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """
    Main generation entrypoint.

    Returns:
        (combined_df, ledger_df, settlement_df, anomaly_counts)
    """
    rng = random.Random(seed)
    fake = Faker()
    Faker.seed(seed)

    print(f"🔧 Generating {num_records} base transactions (seed={seed})...")
    df = _generate_base_transactions(num_records, seed, fake, rng)

    print(f"💉 Injecting anomalies at {anomaly_rate:.0%} rate...")
    df, counts = _inject_anomalies(df, anomaly_rate, rng)

    ledger_df, settlement_df = split_to_datasets(df)

    # Write CSVs
    if output_dir:
        out = Path(output_dir)
    else:
        out = Path("data")
    out.mkdir(parents=True, exist_ok=True)

    ledger_path = out / "ledger_transactions.csv"
    settlement_path = out / "settlement_records.csv"
    ground_truth_path = out / "ground_truth.csv"

    ledger_df.to_csv(ledger_path, index=False, quoting=csv.QUOTE_NONNUMERIC)
    settlement_df.to_csv(settlement_path, index=False, quoting=csv.QUOTE_NONNUMERIC)
    df[["order_id", "external_transaction_ref", "anomaly_type"]].to_csv(
        ground_truth_path, index=False, quoting=csv.QUOTE_NONNUMERIC
    )

    # Print summary
    print(f"\n{'='*60}")
    print(f"  DATA GENERATION SUMMARY")
    print(f"{'='*60}")
    print(f"  Base records generated:    {num_records:>8,}")
    print(f"  Total ledger rows:         {len(ledger_df):>8,}")
    print(f"  Total settlement rows:     {len(settlement_df):>8,}")
    print(f"  Total combined rows:       {len(df):>8,}")
    print(f"{'─'*60}")
    print(f"  ANOMALY INJECTION BREAKDOWN:")
    total_anomalies = sum(counts.values())
    for anomaly_type, count in sorted(counts.items(), key=lambda x: -x[1]):
        pct = (count / num_records * 100) if num_records > 0 else 0
        bar = "█" * int(pct * 2)
        print(f"    {anomaly_type:<35} {count:>5}  ({pct:5.1f}%) {bar}")
    print(f"{'─'*60}")
    print(f"  Total anomalies injected:  {total_anomalies:>8,}")
    print(f"  Clean records:             {num_records - total_anomalies:>8,}")
    print(f"{'='*60}")
    print(f"\n  📁 Files written to: {out.resolve()}")
    print(f"     • {ledger_path.name}")
    print(f"     • {settlement_path.name}")
    print(f"     • {ground_truth_path.name}")

    return df, ledger_df, settlement_df, counts


@click.command()
@click.option("--num-records", default=settings.default_num_records, help="Number of base records to generate")
@click.option("--anomaly-rate", default=settings.default_anomaly_rate, help="Fraction of records to inject anomalies (0.0–1.0)")
@click.option("--seed", default=settings.default_seed, help="Random seed for reproducibility")
@click.option("--output-dir", default="data", help="Output directory for CSV files")
def cli(num_records: int, anomaly_rate: float, seed: int, output_dir: str):
    """Generate synthetic payment reconciliation datasets."""
    generate(
        num_records=num_records,
        anomaly_rate=anomaly_rate,
        seed=seed,
        output_dir=output_dir,
    )


if __name__ == "__main__":
    cli()
