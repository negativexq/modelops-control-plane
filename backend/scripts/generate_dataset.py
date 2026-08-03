"""Generate a synthetic fraud-detection dataset and split it into train/val/test CSVs.

Usage:
    python scripts/generate_dataset.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

MERCHANT_CATEGORIES = [
    "grocery",
    "electronics",
    "travel",
    "gaming",
    "fashion",
    "restaurants",
    "utilities",
    "subscriptions",
    "jewelry",
    "crypto",
]

RANDOM_HOUR_CHOICES = [0, 1, 2, 3, 22, 23]


def generate_fraud_dataframe(
    n_samples: int = 100_000,
    n_features: int = 20,
    fraud_rate: float = 0.02,
    random_state: int = 42,
) -> pd.DataFrame:
    """Build a synthetic fraud dataset with ML features plus business columns.

    Deterministic for a fixed (n_samples, n_features, fraud_rate, random_state).
    """
    x, y = make_classification(
        n_samples=n_samples,
        n_features=n_features,
        n_informative=12,
        n_redundant=5,
        n_repeated=0,
        n_clusters_per_class=2,
        weights=[1 - fraud_rate, fraud_rate],
        flip_y=0.001,
        class_sep=1.2,
        random_state=random_state,
    )

    feature_columns = [f"feature_{i}" for i in range(n_features)]
    df = pd.DataFrame(x, columns=feature_columns)

    rng = np.random.RandomState(random_state)
    n = len(df)
    is_fraud = y.astype(bool)

    df.insert(0, "transaction_id", [f"TXN{100_000_000 + i}" for i in range(n)])
    df.insert(1, "customer_id", [f"CUST{cid}" for cid in rng.randint(1, 20_000, size=n)])

    base_amount = rng.lognormal(mean=3.6, sigma=1.0, size=n)
    fraud_amount_boost = np.where(is_fraud, rng.uniform(20, 150, size=n), 0.0)
    df["amount"] = np.round(base_amount + fraud_amount_boost, 2)

    df["merchant_category"] = rng.choice(MERCHANT_CATEGORIES, size=n)

    hour_base = rng.randint(0, 24, size=n)
    hour_night = rng.choice(RANDOM_HOUR_CHOICES, size=n)
    use_night = rng.random(n) < np.where(is_fraud, 0.25, 0.08)
    df["transaction_hour"] = np.where(use_night, hour_night, hour_base)

    device_risk_legit = rng.beta(2, 6, size=n)
    device_risk_fraud = rng.beta(4, 4, size=n)
    df["device_risk_score"] = np.round(np.where(is_fraud, device_risk_fraud, device_risk_legit), 4)

    age_days_legit = np.minimum(rng.exponential(900, size=n) + 1, 3650).astype(int)
    age_days_fraud = np.minimum(rng.exponential(400, size=n) + 1, 3650).astype(int)
    df["customer_age_days"] = np.where(is_fraud, age_days_fraud, age_days_legit)

    declines_legit = rng.poisson(0.15, size=n)
    declines_fraud = rng.poisson(0.5, size=n)
    df["previous_declines"] = np.where(is_fraud, declines_fraud, declines_legit)

    country_mismatch_prob = np.where(is_fraud, 0.15, 0.03)
    df["country_mismatch"] = (rng.random(n) < country_mismatch_prob).astype(int)

    df["is_new_customer"] = (df["customer_age_days"] < 30).astype(int)

    df["label"] = y.astype(int)

    return df


def split_dataset(
    df: pd.DataFrame,
    test_size: float = 0.15,
    val_size: float = 0.15,
    random_state: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Stratified train/validation/test split."""
    train_val, test = train_test_split(
        df, test_size=test_size, stratify=df["label"], random_state=random_state
    )
    val_relative_size = val_size / (1 - test_size)
    train, val = train_test_split(
        train_val,
        test_size=val_relative_size,
        stratify=train_val["label"],
        random_state=random_state,
    )
    return (
        train.reset_index(drop=True),
        val.reset_index(drop=True),
        test.reset_index(drop=True),
    )


def save_dataset(
    output_dir: Path,
    n_samples: int = 100_000,
    n_features: int = 20,
    fraud_rate: float = 0.02,
    random_state: int = 42,
) -> dict[str, int]:
    df = generate_fraud_dataframe(
        n_samples=n_samples,
        n_features=n_features,
        fraud_rate=fraud_rate,
        random_state=random_state,
    )
    train, val, test = split_dataset(df, random_state=random_state)

    output_dir.mkdir(parents=True, exist_ok=True)
    train.to_csv(output_dir / "train.csv", index=False)
    val.to_csv(output_dir / "validation.csv", index=False)
    test.to_csv(output_dir / "test.csv", index=False)

    return {"train": len(train), "validation": len(val), "test": len(test)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-samples", type=int, default=100_000)
    parser.add_argument("--n-features", type=int, default=20)
    parser.add_argument("--fraud-rate", type=float, default=0.02)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()

    counts = save_dataset(
        output_dir=args.output_dir,
        n_samples=args.n_samples,
        n_features=args.n_features,
        fraud_rate=args.fraud_rate,
        random_state=args.random_state,
    )
    print(f"Wrote dataset to {args.output_dir}: {counts}")


if __name__ == "__main__":
    main()
