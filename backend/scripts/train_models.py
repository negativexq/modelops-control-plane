"""Train fraud-detection model versions and publish them to the local artifact registry.

Produces three versions under artifacts/fraud-model/:
  - v1:              LogisticRegression, stable baseline
  - v2-good:          HistGradientBoostingClassifier, a successful canary (better recall than v1)
  - v2-quality-bad:   same algorithm as v2-good but deliberately weakened, simulating a
                      quality-regression canary that should fail promotion gates

Usage:
    python scripts/train_models.py
"""

from __future__ import annotations

import argparse
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from scripts.generate_dataset import DATA_DIR

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "fraud-model"

NUMERIC_FEATURES = [f"feature_{i}" for i in range(20)] + [
    "amount",
    "transaction_hour",
    "device_risk_score",
    "customer_age_days",
    "previous_declines",
    "country_mismatch",
    "is_new_customer",
]
CATEGORICAL_FEATURES = ["merchant_category"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
TARGET = "label"

# Deliberately weak feature subset + label noise rate for the "bad" canary.
WEAK_FEATURES = ["feature_0", "feature_1", "amount", "transaction_hour"]
LABEL_NOISE_RATE = 0.35
RECALL_THRESHOLD = 0.80


def _load_splits(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    train = pd.read_csv(data_dir / "train.csv")
    val = pd.read_csv(data_dir / "validation.csv")
    test = pd.read_csv(data_dir / "test.csv")
    return train, val, test


def _build_preprocessor(numeric_features: list[str]) -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            ("numeric", StandardScaler(), numeric_features),
            ("categorical", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
        ]
    )


def build_v1_pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            ("preprocess", _build_preprocessor(NUMERIC_FEATURES)),
            (
                "model",
                LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42),
            ),
        ]
    )


#  Fraud is rare (~2%) and HistGradientBoostingClassifier's built-in "balanced" class
#  weighting under-corrects for it at the default 0.5 threshold, leaving recall below the
#  balanced LogisticRegression baseline. This explicit ratio pushes recall past v1 while
#  keeping precision competitive.
V2_GOOD_CLASS_WEIGHT = {0: 1, 1: 150}


def build_v2_good_pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            ("preprocess", _build_preprocessor(NUMERIC_FEATURES)),
            (
                "model",
                HistGradientBoostingClassifier(
                    max_iter=100,
                    max_depth=6,
                    random_state=42,
                    class_weight=V2_GOOD_CLASS_WEIGHT,
                ),
            ),
        ]
    )


def build_v2_quality_bad_pipeline() -> Pipeline:
    weak_numeric = [f for f in WEAK_FEATURES if f in NUMERIC_FEATURES]
    return Pipeline(
        steps=[
            ("preprocess", _build_preprocessor(weak_numeric)),
            (
                "model",
                HistGradientBoostingClassifier(max_iter=100, max_depth=6, random_state=42),
            ),
        ]
    )


def _noisy_labels(y: pd.Series, noise_rate: float, random_state: int = 42) -> np.ndarray:
    rng = np.random.RandomState(random_state)
    y_noisy = y.to_numpy(dtype=int).copy()
    flip_mask = rng.random(len(y_noisy)) < noise_rate
    y_noisy[flip_mask] = 1 - y_noisy[flip_mask]
    return y_noisy


def evaluate(pipeline: Pipeline, x_test: pd.DataFrame, y_test: pd.Series) -> dict[str, float]:
    y_pred = pipeline.predict(x_test)
    y_proba = pipeline.predict_proba(x_test)[:, 1]

    tn, fp, fn, tp = confusion_matrix(y_test, y_pred, labels=[0, 1]).ravel()
    false_positive_rate = fp / (fp + tn) if (fp + tn) > 0 else 0.0

    return {
        "precision": float(precision_score(y_test, y_pred, zero_division=0)),
        "recall": float(recall_score(y_test, y_pred, zero_division=0)),
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
        "false_positive_rate": float(false_positive_rate),
        "roc_auc": float(roc_auc_score(y_test, y_proba)),
        "true_positives": int(tp),
        "false_positives": int(fp),
        "true_negatives": int(tn),
        "false_negatives": int(fn),
    }


def _write_artifact(
    version: str,
    pipeline: Pipeline,
    metadata: dict[str, Any],
    evaluation: dict[str, Any],
    artifacts_dir: Path,
) -> Path:
    version_dir = artifacts_dir / version
    version_dir.mkdir(parents=True, exist_ok=True)

    joblib.dump(pipeline, version_dir / "model.joblib")
    (version_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    (version_dir / "evaluation.json").write_text(json.dumps(evaluation, indent=2))

    return version_dir


def train_all(
    data_dir: Path = DATA_DIR, artifacts_dir: Path = ARTIFACTS_DIR
) -> dict[str, dict[str, Any]]:
    train, val, test = _load_splits(data_dir)

    x_train, y_train = train[ALL_FEATURES], train[TARGET]
    x_test, y_test = test[ALL_FEATURES], test[TARGET]

    results: dict[str, dict[str, Any]] = {}
    trained_at = datetime.now(UTC).isoformat()
    common_meta = {
        "model_name": "fraud-model",
        "trained_at": trained_at,
        "dataset": {
            "train_rows": len(train),
            "validation_rows": len(val),
            "test_rows": len(test),
            "fraud_rate_train": float(y_train.mean()),
        },
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
    }

    # v1: stable baseline
    v1_pipeline = build_v1_pipeline()
    v1_pipeline.fit(x_train, y_train)
    v1_eval = evaluate(v1_pipeline, x_test, y_test)
    v1_meta = {
        **common_meta,
        "version": "v1",
        "role": "stable",
        "algorithm": "LogisticRegression",
        "hyperparameters": {
            "class_weight": "balanced",
            "max_iter": 1000,
            "random_state": 42,
        },
        "features": ALL_FEATURES,
    }
    _write_artifact("v1", v1_pipeline, v1_meta, v1_eval, artifacts_dir)
    results["v1"] = {"metadata": v1_meta, "evaluation": v1_eval}

    # v2-good: successful canary, should beat v1 on recall
    v2_good_pipeline = build_v2_good_pipeline()
    v2_good_pipeline.fit(x_train, y_train)
    v2_good_eval = evaluate(v2_good_pipeline, x_test, y_test)
    v2_good_meta = {
        **common_meta,
        "version": "v2-good",
        "role": "canary-success",
        "algorithm": "HistGradientBoostingClassifier",
        "hyperparameters": {
            "max_iter": 100,
            "max_depth": 6,
            "random_state": 42,
            "class_weight": V2_GOOD_CLASS_WEIGHT,
        },
        "features": ALL_FEATURES,
    }
    _write_artifact("v2-good", v2_good_pipeline, v2_good_meta, v2_good_eval, artifacts_dir)
    results["v2-good"] = {"metadata": v2_good_meta, "evaluation": v2_good_eval}

    # v2-quality-bad: deliberately weakened canary (weak feature subset + label noise)
    weak_features = [f for f in WEAK_FEATURES if f in NUMERIC_FEATURES] + CATEGORICAL_FEATURES
    x_train_weak = train[weak_features]
    y_train_noisy = _noisy_labels(y_train, LABEL_NOISE_RATE)
    x_test_weak = test[weak_features]

    v2_bad_pipeline = build_v2_quality_bad_pipeline()
    v2_bad_pipeline.fit(x_train_weak, y_train_noisy)
    v2_bad_eval = evaluate(v2_bad_pipeline, x_test_weak, y_test)
    v2_bad_meta = {
        **common_meta,
        "version": "v2-quality-bad",
        "role": "canary-quality-regression",
        "algorithm": "HistGradientBoostingClassifier",
        "hyperparameters": {"max_iter": 100, "max_depth": 6, "random_state": 42},
        "features": weak_features,
        "degradation": {
            "label_noise_rate": LABEL_NOISE_RATE,
            "note": "Deliberately trained on a reduced feature subset with injected label "
            "noise to simulate a quality-regressed canary for promotion-gate testing.",
        },
    }
    _write_artifact("v2-quality-bad", v2_bad_pipeline, v2_bad_meta, v2_bad_eval, artifacts_dir)
    results["v2-quality-bad"] = {"metadata": v2_bad_meta, "evaluation": v2_bad_eval}

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args()

    results = train_all(data_dir=args.data_dir, artifacts_dir=args.artifacts_dir)
    for version, payload in results.items():
        ev = payload["evaluation"]
        print(
            f"{version}: precision={ev['precision']:.3f} recall={ev['recall']:.3f} "
            f"f1={ev['f1']:.3f} fpr={ev['false_positive_rate']:.4f} roc_auc={ev['roc_auc']:.3f}"
        )


if __name__ == "__main__":
    main()
