"""Locust load definition for the ModelOps router. Run via load_runner.py (which
shells out to `locust --headless -f locustfile.py ...`) - not meant to be run
directly, though `locust -f locustfile.py --host http://localhost:8080` works fine
for ad hoc use.

Sends real rows sampled from backend/data/test.csv (see scripts/generate_dataset.py)
rather than random noise - every trained version's dynamic pydantic request model
(see app/serving/schemas.py) ignores fields it doesn't need, so the same payload
works whether the router forwards to v1, v2-good, or v2-quality-bad.

Each user is also the real ground-truth label source for its own requests: it
knows the sampled row's true `label` the moment it sends the request (a real
system's request generator - here, the load tool standing in for it - is exactly
who would know this), reads back the `prediction_id` the response was minted
with, and after a configurable delay reports a (deliberately incomplete) subset
of them via POST /api/labels/batch - see docs/DESIGN_NOTES.md's "Label
ingestion" section for why labels arrive delayed and incomplete in any real
system, and why this simulates that rather than sidestepping it.
"""

from __future__ import annotations

import csv
import os
import random
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from locust import HttpUser, constant_pacing, events, task

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "test.csv"

TARGET_RPS = float(os.environ.get("BENCHMARK_TARGET_RPS", "100"))
USER_COUNT = int(os.environ.get("LOCUST_USER_COUNT", "20"))
# Each user waits this long between requests so that USER_COUNT users together
# average out to TARGET_RPS total (constant_pacing keeps each user's own request
# rate steady even if individual requests are slow).
_PACING_SECONDS = max(USER_COUNT / TARGET_RPS, 0.001)

CONTROL_PLANE_URL = os.environ.get("BENCHMARK_CONTROL_PLANE_URL", "http://localhost:8000")
# Kept below label_maturity_seconds by default so a healthy run's labels are
# already matured by the time the policy engine's quality window looks for them.
LABEL_DELAY_SECONDS = float(os.environ.get("BENCHMARK_LABEL_DELAY_SECONDS", "10"))
# Deliberately < 1.0 by default: at coverage=1.0 every prediction gets labeled and
# minimum_label_coverage's gate would never actually be exercised by this harness.
LABEL_COVERAGE = float(os.environ.get("BENCHMARK_LABEL_COVERAGE", "0.8"))
_LABEL_FLUSH_INTERVAL_SECONDS = 2.0
_LABEL_FLUSH_BATCH_SIZE = 50

_NON_FEATURE_COLUMNS = {"transaction_id", "customer_id", "label", "merchant_category"}
_INT_COLUMNS = {
    "transaction_hour",
    "customer_age_days",
    "previous_declines",
    "country_mismatch",
    "is_new_customer",
}


def _load_dataset_rows() -> list[tuple[dict[str, Any], int]]:
    """Loads (payload, true_label) pairs from the real held-out test set - the same
    file used to evaluate the trained models, not synthetic noise."""
    with DATA_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        rows: list[tuple[dict[str, Any], int]] = []
        for raw in reader:
            payload: dict[str, Any] = {}
            for key, value in raw.items():
                if key in _NON_FEATURE_COLUMNS:
                    continue
                payload[key] = int(value) if key in _INT_COLUMNS else float(value)
            payload["merchant_category"] = raw["merchant_category"]
            rows.append((payload, int(raw["label"])))
        return rows


_DATASET_ROWS = _load_dataset_rows()
_POSITIVE_ROWS = [row for row in _DATASET_ROWS if row[1] == 1]
_NEGATIVE_ROWS = [row for row in _DATASET_ROWS if row[1] == 0]


def _sample_row(positive_ratio: float | None = None) -> tuple[dict[str, Any], int]:
    """Samples one (payload, true_label) row from the real dataset.

    `positive_ratio`, when given, overrides the dataset's natural ~2% positive
    (fraud) rate: with that probability, sample from the positive-class rows
    instead of the whole dataset. This exists for CI (see
    scripts/ci_smoke_test.py) and, where a benchmark scenario needs it - a
    short-lived scenario's quality window otherwise typically contains only 1-3
    positive examples at the natural rate, which makes `recall` (denominator
    TP+FN) statistically meaningless regardless of how many labeled samples
    clear minimum_labeled_samples/minimum_label_coverage - see
    app/policy/engine.py's minimum_positive_labels gate and
    docs/DESIGN_NOTES.md. Left at its default (`None`, natural dataset ratio)
    for RouterUser's own demo traffic below - real production traffic isn't
    stratified either, and this is a deliberate, visible knob, not a hidden one.
    """
    if positive_ratio is None:
        return random.choice(_DATASET_ROWS)
    if random.random() < positive_ratio:
        return random.choice(_POSITIVE_ROWS)
    return random.choice(_NEGATIVE_ROWS)


class _LabelFeeder:
    """Owns a queue of (send_at, prediction_id, actual_label) tuples and a
    background thread that flushes matured ones to POST /api/labels/batch.
    Reusable outside Locust too - see scripts/ci_smoke_test.py, which
    instantiates this directly (plain `threading`, no gevent involved) rather
    than duplicating the delay/coverage/batching logic.
    """

    def __init__(
        self,
        control_plane_url: str = CONTROL_PLANE_URL,
        label_delay_seconds: float = LABEL_DELAY_SECONDS,
        label_coverage: float = LABEL_COVERAGE,
    ) -> None:
        self._label_delay_seconds = label_delay_seconds
        self._label_coverage = label_coverage
        self._lock = threading.Lock()
        self._queue: list[tuple[float, str, int]] = []
        self._stop = threading.Event()
        self._client = httpx.Client(base_url=control_plane_url, timeout=10.0)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=_LABEL_FLUSH_INTERVAL_SECONDS + 5)
        self._client.close()

    def queue_label(self, prediction_id: str, actual_label: int) -> None:
        if random.random() > self._label_coverage:
            return
        send_at = time.monotonic() + self._label_delay_seconds
        with self._lock:
            self._queue.append((send_at, prediction_id, actual_label))

    def _due_batch(self) -> list[tuple[str, int]]:
        now = time.monotonic()
        due = []
        with self._lock:
            remaining = []
            for send_at, prediction_id, actual_label in self._queue:
                if send_at <= now and len(due) < _LABEL_FLUSH_BATCH_SIZE:
                    due.append((prediction_id, actual_label))
                else:
                    remaining.append((send_at, prediction_id, actual_label))
            self._queue = remaining
        return due

    def _run(self) -> None:
        while not self._stop.is_set():
            self._flush_once()
            self._stop.wait(_LABEL_FLUSH_INTERVAL_SECONDS)
        # Drain whatever's left once, best-effort, so a short benchmark run doesn't
        # lose its whole tail of not-yet-due labels when the process exits.
        self._flush_once()

    def _flush_once(self) -> None:
        due = self._due_batch()
        if not due:
            return
        now_iso = datetime.now(UTC).isoformat()
        body = [
            {"prediction_id": prediction_id, "actual_label": actual_label, "occurred_at": now_iso}
            for prediction_id, actual_label in due
        ]
        try:
            self._client.post("/api/labels/batch", json=body)
        except httpx.HTTPError:
            pass  # best-effort; a dropped label batch just lowers observed coverage


_feeder = _LabelFeeder()


@events.test_start.add_listener
def _on_test_start(**_kwargs: Any) -> None:
    _feeder.start()


@events.test_stop.add_listener
def _on_test_stop(**_kwargs: Any) -> None:
    _feeder.stop()


class RouterUser(HttpUser):
    wait_time = constant_pacing(_PACING_SECONDS)

    @task
    def predict(self) -> None:
        payload, true_label = _sample_row()
        response = self.client.post("/router/predict", json=payload, name="/router/predict")
        if response.status_code != 200:
            return
        prediction_id = response.json().get("prediction_id")
        if isinstance(prediction_id, str):
            _feeder.queue_label(prediction_id, true_label)
