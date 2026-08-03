from pathlib import Path

from scripts.generate_dataset import generate_fraud_dataframe, save_dataset, split_dataset

EXPECTED_BUSINESS_COLUMNS = {
    "transaction_id",
    "customer_id",
    "amount",
    "merchant_category",
    "transaction_hour",
    "device_risk_score",
    "customer_age_days",
    "previous_declines",
    "country_mismatch",
    "is_new_customer",
    "label",
}


def test_generation_is_deterministic() -> None:
    df1 = generate_fraud_dataframe(n_samples=2000, random_state=42)
    df2 = generate_fraud_dataframe(n_samples=2000, random_state=42)
    assert df1.equals(df2)


def test_different_seed_changes_output() -> None:
    df1 = generate_fraud_dataframe(n_samples=2000, random_state=42)
    df2 = generate_fraud_dataframe(n_samples=2000, random_state=7)
    assert not df1.equals(df2)


def test_row_count_and_columns() -> None:
    df = generate_fraud_dataframe(n_samples=1000, random_state=42)
    assert len(df) == 1000
    assert EXPECTED_BUSINESS_COLUMNS.issubset(set(df.columns))
    assert "transaction_id" in df.columns
    assert df["transaction_id"].is_unique


def test_fraud_rate_close_to_target() -> None:
    df = generate_fraud_dataframe(n_samples=20_000, fraud_rate=0.02, random_state=42)
    fraud_rate = df["label"].mean()
    assert 0.01 < fraud_rate < 0.03


def test_split_dataset_is_stratified_and_disjoint() -> None:
    df = generate_fraud_dataframe(n_samples=5000, random_state=42)
    train, val, test = split_dataset(df, random_state=42)

    assert len(train) + len(val) + len(test) == len(df)

    train_ids = set(train["transaction_id"])
    val_ids = set(val["transaction_id"])
    test_ids = set(test["transaction_id"])
    assert train_ids.isdisjoint(val_ids)
    assert train_ids.isdisjoint(test_ids)
    assert val_ids.isdisjoint(test_ids)

    overall_rate = df["label"].mean()
    for split in (train, val, test):
        assert abs(split["label"].mean() - overall_rate) < 0.01


def test_save_dataset_writes_csv_files(tmp_path: Path) -> None:
    counts = save_dataset(output_dir=tmp_path, n_samples=1000, random_state=42)

    assert (tmp_path / "train.csv").exists()
    assert (tmp_path / "validation.csv").exists()
    assert (tmp_path / "test.csv").exists()
    assert sum(counts.values()) == 1000
