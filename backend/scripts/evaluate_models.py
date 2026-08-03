"""Validate trained fraud-model artifacts against promotion-gate thresholds.

Reads evaluation.json for each version via the local registry (no retraining) and checks:
  - v2-good has higher recall than v1
  - v2-quality-bad recall is below the minimum recall threshold

Exits non-zero if a check fails, so it can be used as a CI/promotion gate.

Usage:
    python scripts/evaluate_models.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.registry.local import LocalModelRegistry
from scripts.train_models import ARTIFACTS_DIR, RECALL_THRESHOLD

MODEL_NAME = "fraud-model"
VERSIONS = ("v1", "v2-good", "v2-quality-bad")


def evaluate_gate(artifacts_dir: Path) -> bool:
    registry = LocalModelRegistry(artifacts_dir)
    ok = True

    versions = {v: registry.get_evaluation(MODEL_NAME, v) for v in VERSIONS}
    for version, evaluation in versions.items():
        print(
            f"{version}: precision={evaluation['precision']:.3f} recall={evaluation['recall']:.3f} "
            f"f1={evaluation['f1']:.3f} fpr={evaluation['false_positive_rate']:.4f} "
            f"roc_auc={evaluation['roc_auc']:.3f}"
        )

    v1_recall = versions["v1"]["recall"]
    v2_good_recall = versions["v2-good"]["recall"]
    v2_bad_recall = versions["v2-quality-bad"]["recall"]

    if v2_good_recall > v1_recall:
        print(f"[PASS] v2-good recall ({v2_good_recall:.3f}) > v1 recall ({v1_recall:.3f})")
    else:
        print(f"[FAIL] v2-good recall ({v2_good_recall:.3f}) <= v1 recall ({v1_recall:.3f})")
        ok = False

    if v2_bad_recall < RECALL_THRESHOLD:
        print(
            f"[PASS] v2-quality-bad recall ({v2_bad_recall:.3f}) < threshold ({RECALL_THRESHOLD})"
        )
    else:
        print(
            f"[FAIL] v2-quality-bad recall ({v2_bad_recall:.3f}) >= threshold ({RECALL_THRESHOLD})"
        )
        ok = False

    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DIR.parent)
    args = parser.parse_args()

    ok = evaluate_gate(args.artifacts_dir)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
