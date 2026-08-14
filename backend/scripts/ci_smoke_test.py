"""End-to-end smoke test against a *running* docker-compose stack - the CI
"integration" job (see .github/workflows/ci.yml) builds the whole stack, waits for
this to pass, then tears it down. Not a pytest test: it makes real HTTP calls to
real containers, on purpose.

Why this exists: every real bug found in this project so far (the worker crashing
on a transient control-plane outage, SQLite silently dropping tzinfo, a 100%-canary
starving the stable side of traffic, Locust's nonzero exit code on any failed
request, the frontend misparsing an offset-less timestamp as local time) was found
by running the actual stack, not by a unit test with mocked collaborators. Unit
tests keep verifying logic in isolation; this catches the wiring between services -
see README's "Troubleshooting" section.

Four scenarios, run in sequence (the router holds a single active traffic split -
see docs/DESIGN_NOTES.md#benchmark-suite - so these can't run concurrently, and
each fully completes before the next one starts). Each scenario now uses its own
model_name (SCENARIO_MODEL_NAMES below), not a shared "fraud-model": the
active-deployment-per-model DB invariant (see
docs/DESIGN_NOTES.md#control-plane--deployment-lifecycle) then points at
exactly which scenario left something behind if one ever does, instead of every
scenario racing a single ambiguous name. `_assert_no_orphaned_deployments`
checks this explicitly before scenario 1 even starts.

1. `run_manual_smoke_scenario` - the fast path: create (already
   automation_paused=True - see docs/DESIGN_NOTES.md#manual-automation-hold for
   why), send traffic, evaluate, manually promote. Exercises the CRUD-ish
   surface without waiting on the worker.
2. `run_automatic_rollback_scenario` - injects real latency into a canary and
   waits for the *actual* automated worker (not a manual call standing in for it)
   to detect the FAIL and roll back on its own.
3. `run_automatic_quality_promote_scenario` - a healthy canary (v2-good), real
   traffic, and real delayed ground-truth labels (via POST /api/labels/batch,
   see scripts/benchmarks/locustfile.py's _LabelFeeder) flowing through the
   platform's actual label ingestion path - no direct-DB actual_label write
   anywhere. Waits for the worker to walk it through every traffic stage
   (10% -> 25% -> 50% -> 100%) and promote it on a genuine minimum_recall PASS.
4. `run_automatic_quality_rollback_scenario` - a deliberately weak canary
   (v2-quality-bad), same real delayed label flow as scenario 3, but recall
   genuinely fails the threshold - the worker automatically rolls it back for a
   quality reason alone, no fault injection involved. This exercises a path that
   was previously impossible to test for real: minimum_recall actually
   resolving to FAIL, not just staying perpetually INCONCLUSIVE.
5. `run_router_restart_reconcile_scenario` - the platform's one real proof of
   "self-healing control plane" (see
   docs/DESIGN_NOTES.md#desired-observed-reconciliation): sets up a canary
   rollout, `docker compose restart`s the router (it loses its in-memory
   config entirely - no outbox, no persistence there by design), confirms the
   router really did lose it, then waits for the worker's own reconcile tick
   (POST /api/router/reconcile) to catch it back up on its own - not a human,
   not a replay of the original request. Deliberately uses model_name="fraud-
   model" (the router's own fixed ROUTER_MODEL_NAME, not a ci-smoke-* name) -
   see the scenario function's own docstring for why that's the realistic
   case, and why `revision` (not `deployment_id`) is what actually proves the
   reconciler, specifically, did the healing.

Scenarios 3 and 4 use a *stratified* request stream (QUALITY_SCENARIO_POSITIVE_
RATIO, default 0.5 - see _BackgroundTraffic/locustfile.py's _sample_row), not
the dataset's natural ~2% positive rate: at the natural rate, a short
evaluation window typically contains only 1-3 positive examples, and recall
(TP/(TP+FN)) computed from that few examples is statistically meaningless -
see app/policy/engine.py's minimum_positive_labels gate and
docs/DESIGN_NOTES.md. This is a deliberate measurability choice specific to
these two CI scenarios, not something the platform does with real traffic;
the Locust demo load (scripts/benchmarks/locustfile.py's RouterUser) samples
at the dataset's natural, unstratified rate.

Scenarios 2-4 only finish in reasonable CI time because the worker's poll
interval, and scenarios 3/4's label_maturity_seconds/evaluation_window_seconds,
are turned way down for this job specifically - see
WORKER_POLL_INTERVAL_SECONDS in .github/workflows/ci.yml and docker-compose.yml
(defaults to 15s for real/local use, unaffected unless that env var is set).
"""

import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

import httpx

from scripts.benchmarks.locustfile import _LabelFeeder, _sample_row

REPO_ROOT = Path(__file__).resolve().parents[2]

BACKEND_URL = os.environ.get("CI_BACKEND_URL", "http://localhost:8000")
ROUTER_URL = os.environ.get("CI_ROUTER_URL", "http://localhost:8080")
FRONTEND_URL = os.environ.get("CI_FRONTEND_URL", "http://localhost:3000")
LATENCY_FAULT_URL = os.environ.get("CI_LATENCY_FAULT_URL", "http://localhost:8004")
# Kept short so scenarios 3/4's quality window matures within CI's time budget -
# see PolicyConfig.label_maturity_seconds in app/policy/config.py.
LABEL_DELAY_SECONDS = float(os.environ.get("CI_LABEL_DELAY_SECONDS", "2"))
LABEL_MATURITY_SECONDS = int(os.environ.get("CI_LABEL_MATURITY_SECONDS", "5"))
# Overrides the dataset's natural ~2% positive rate for scenarios 3/4 - see
# _BackgroundTraffic's docstring and app/policy/engine.py's
# minimum_positive_labels gate. Deliberately generous margin, not a bare
# minimum: the goal is a deterministic CI check, not a tight-but-fragile one.
QUALITY_SCENARIO_POSITIVE_RATIO = float(os.environ.get("CI_POSITIVE_RATIO", "0.5"))

# Every HTTP-exposed service in docker-compose.yml. The worker has no HTTP surface
# by design (see README) so it isn't health-checked directly here - scenarios 2/3
# below wait on its *effects* instead (a deployment actually reaching ROLLED_BACK/
# PROMOTED on its own), which is a stronger check than a liveness probe anyway.
HEALTH_ENDPOINTS = {
    "backend": f"{BACKEND_URL}/health",
    "frontend": FRONTEND_URL,
    "router": f"{ROUTER_URL}/router/health",
    "model-serving-v1": "http://localhost:8001/ready",
    "model-serving-v2-good": "http://localhost:8002/ready",
    "model-serving-v2-quality-bad": "http://localhost:8003/ready",
    "model-serving-v2-good-latency-fault": "http://localhost:8004/ready",
    "model-serving-v2-good-error-fault": "http://localhost:8005/ready",
}


class SmokeTestFailure(SystemExit):
    pass


def _wait_for(name: str, url: str, timeout_seconds: float = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: str = "never attempted"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200:
                print(f"[ready] {name} ({url})")
                return
            last_error = f"status {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SmokeTestFailure(f"[FAIL] {name} never became ready at {url}: {last_error}")


def _wait_for_min_samples(deployment_id: str, minimum: int, timeout_seconds: float = 60) -> None:
    """The router publishes metrics fire-and-forget (asyncio.create_task, not
    awaited - see docs/DESIGN_NOTES.md#metrics), so there's a real, if small, delay
    between a /router/predict response and its PredictionMetric row landing. Poll
    instead of guessing a sleep duration.
    """
    deadline = time.monotonic() + timeout_seconds
    counts = (0, 0)
    while time.monotonic() < deadline:
        response = httpx.get(f"{BACKEND_URL}/api/deployments/{deployment_id}/metrics", timeout=10)
        response.raise_for_status()
        metrics = response.json()
        counts = (metrics["stable"]["sample_count"], metrics["canary"]["sample_count"])
        if min(counts) >= minimum:
            print(f"[ok] metrics landed: stable={counts[0]} canary={counts[1]}")
            return
        time.sleep(1)
    raise SmokeTestFailure(
        f"[FAIL] metrics never reached {minimum}/side within {timeout_seconds}s: {counts}"
    )


def _wait_for_deployment_status(
    deployment_id: str, target_statuses: Iterable[str], timeout_seconds: float
) -> dict[str, object]:
    """Polls GET /api/deployments/{id} until its status is one of
    `target_statuses` - used by the two "wait for the real worker" scenarios below,
    since there's no webhook/push for this: the worker acts on its own schedule."""
    targets = set(target_statuses)
    deadline = time.monotonic() + timeout_seconds
    last_status = "<never fetched>"
    while time.monotonic() < deadline:
        response = httpx.get(f"{BACKEND_URL}/api/deployments/{deployment_id}", timeout=10)
        response.raise_for_status()
        deployment = response.json()
        last_status = deployment["status"]
        if last_status in targets:
            return deployment  # type: ignore[no-any-return]
        time.sleep(2)
    raise SmokeTestFailure(
        f"[FAIL] deployment {deployment_id} never reached {targets} within "
        f"{timeout_seconds}s (last status: {last_status}) - is the worker running, "
        "and is WORKER_POLL_INTERVAL_SECONDS actually turned down for this job?"
    )


class _BackgroundTraffic:
    """Keeps POSTing real, labeled rows to /router/predict on a background thread
    until stopped, optionally queueing each one's true label with `label_feeder`
    (see scripts/benchmarks/locustfile.py's _LabelFeeder - same class, same
    delayed/partial-coverage semantics, reused here rather than duplicated).

    Scenarios 2 and 3 below need traffic flowing *continuously*, not just once:
    the worker's minimum_requests/latency/error checks all read a sliding time
    window (policy_config.evaluation_window_seconds), so a single burst of
    predictions sent at t=0 ages out of that window by the time the worker gets
    around to its second or third evaluation cycle. A real deployment has
    continuous production traffic; this stands in for that.

    `positive_ratio` (see locustfile.py's _sample_row) is what makes scenarios
    3/4 deterministic: at the dataset's natural ~2% positive rate, a short
    evaluation window typically contains only 1-3 positive examples, which
    makes recall a coin flip, not a measurement - see
    app/policy/engine.py's minimum_positive_labels gate. Left unset (natural
    ratio) for scenario 2, which never reads recall at all.
    """

    def __init__(
        self,
        label_feeder: "_LabelFeeder | None" = None,
        interval_seconds: float = 0.2,
        positive_ratio: float | None = None,
    ) -> None:
        self._label_feeder = label_feeder
        self._interval_seconds = interval_seconds
        self._positive_ratio = positive_ratio
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload, true_label = _sample_row(self._positive_ratio)
                response = httpx.post(
                    f"{ROUTER_URL}/router/predict", json=payload, timeout=10
                )
                if self._label_feeder is not None and response.status_code == 200:
                    prediction_id = response.json().get("prediction_id")
                    if isinstance(prediction_id, str):
                        self._label_feeder.queue_label(prediction_id, true_label)
            except httpx.HTTPError:
                pass  # a single dropped request shouldn't kill the traffic generator
            self._stop.wait(self._interval_seconds)

    def __enter__(self) -> "_BackgroundTraffic":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _post_metric(deployment_id: str, model_version: str, *, latency_ms: float) -> None:
    """Posts a plain, unlabeled metric row - used only to keep the stable side of
    a benchmark deployment satisfying minimum_requests once the canary has
    claimed all real traffic (see run_automatic_promote_scenario). The metrics
    endpoint has no `actual_label` field at all - see MetricIn's docstring;
    real ground truth only ever arrives through POST /api/labels(/batch)."""
    response = httpx.post(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/metrics",
        json={"model_version": model_version, "latency_ms": latency_ms, "status_code": 200},
        timeout=10,
    )
    response.raise_for_status()


class _StableSideHealthChecks:
    """Runs until stopped, periodically posting a batch of plain (unlabeled)
    health-check metrics for the stable version - once the canary approaches
    100% traffic, the router sends stable ~0 real requests, so minimum_requests
    (which needs BOTH sides, over a short evaluation_window_seconds in this CI
    job) would otherwise never be satisfied again and PROMOTE would never fire.
    Mirrors scripts/benchmarks/run_benchmark.py's
    _feed_stable_side_health_checks, including its batch size - one post per
    interval isn't enough to clear minimum_requests within a 10s window."""

    def __init__(
        self,
        deployment_id: str,
        stable_version: str,
        interval_seconds: float = 3.0,
        batch_size: int = 15,
    ) -> None:
        self._deployment_id = deployment_id
        self._stable_version = stable_version
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            for _ in range(self._batch_size):
                try:
                    _post_metric(self._deployment_id, self._stable_version, latency_ms=12.0)
                except httpx.HTTPError:
                    pass  # a single dropped request shouldn't kill the health checks
            self._stop.wait(self._interval_seconds)

    def __enter__(self) -> "_StableSideHealthChecks":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _set_fault_injection(url: str, latency_ms: int, error_rate: float) -> None:
    response = httpx.put(
        f"{url}/fault-injection",
        json={"latency_ms": latency_ms, "error_rate": error_rate},
        timeout=10,
    )
    response.raise_for_status()


DEPLOYMENT_ID_LOG = os.environ.get("CI_DEPLOYMENT_ID_LOG", "/tmp/ci_smoke_deployment_ids.log")


def _record_deployment_id(scenario_label: str, deployment_id: str) -> None:
    """Appends "scenario_label=deployment_id" to DEPLOYMENT_ID_LOG - the only way
    a later "dump diagnostics on failure" CI step can find out which deployments
    this run created, since a SmokeTestFailure kills this process before it can
    print anything else and the DB itself disappears the moment `docker compose
    down` runs. See .github/workflows/ci.yml's "Dump policy diagnostics on
    failure" step, which greps this file and curls /timeline for each id."""
    with open(DEPLOYMENT_ID_LOG, "a") as f:
        f.write(f"{scenario_label}={deployment_id}\n")


def _create_deployment(
    *,
    model_name: str,
    canary_version: str,
    canary_weight: float,
    policy_config: dict[str, object],
    automation_paused: bool = False,
) -> dict[str, object]:
    response = httpx.post(
        f"{BACKEND_URL}/api/deployments",
        json={
            "model_name": model_name,
            "stable_version": "v1",
            "canary_version": canary_version,
            "canary_weight": canary_weight,
            "policy_config": policy_config,
            "automation_paused": automation_paused,
        },
        headers={"Idempotency-Key": f"ci-smoke-{model_name}-{uuid.uuid4()}"},
        timeout=10,
    )
    response.raise_for_status()
    result: dict[str, object] = response.json()
    return result


def _assert_no_orphaned_deployments(model_names: Iterable[str]) -> None:
    """Pre-flight check: none of this script's own model_names may have a
    non-terminal deployment left over from a previous (likely crashed) local
    run. Fails loudly with the offending deployment's id and status rather than
    silently deleting it - a left-over CANARY_RUNNING deployment is evidence a
    previous run didn't clean up after itself, and that's worth looking at, not
    papering over. Each scenario uses its own model_name specifically so this
    check (and the active-deployment-per-model DB invariant - see
    docs/DESIGN_NOTES.md#control-plane--deployment-lifecycle) can point at
    exactly which scenario left something behind, rather than every scenario
    sharing one ambiguous "fraud-model" name.
    """
    terminal = {"PROMOTED", "ROLLED_BACK", "FAILED"}
    response = httpx.get(f"{BACKEND_URL}/api/deployments", timeout=10)
    response.raise_for_status()
    offenders = [
        d
        for d in response.json()
        if d["model_name"] in set(model_names) and d["status"] not in terminal
    ]
    if offenders:
        details = ", ".join(f"{d['id']} ({d['model_name']}: {d['status']})" for d in offenders)
        raise SmokeTestFailure(
            f"[FAIL] found non-terminal deployment(s) left over from a previous run: "
            f"{details} - clean these up manually (promote/rollback them) before "
            "re-running; this script will not delete them for you."
        )


def run_manual_smoke_scenario() -> None:
    """The fast path: create a deployment, send it traffic, evaluate, and manually
    promote it - proves the CRUD-ish surface and the timeline endpoint work, without
    waiting on the worker (that's what the other three scenarios are for).

    Created with automation_paused=True: this scenario's whole point is a purely
    manual flow, but the always-on worker sweeps every CANARY_RUNNING/EVALUATING
    deployment on every poll cycle regardless of which "scenario" created it -
    without a hold, the worker can (and, on a real CI runner, reproducibly did -
    see docs/DESIGN_NOTES.md#manual-automation-hold) evaluate and auto-rollback
    this deployment before the manual /evaluate call below even runs, since real
    p95 latency noise between v1/v2-good under shared CI compute can cross the
    *default* (non-generous) latency_p95_increase threshold this scenario
    deliberately leaves untouched. automation_paused=True at creation - not a
    separate pause-automation call after - closes that race entirely rather than
    narrowing it.
    """
    print("\n### scenario 1/4: manual create -> evaluate -> promote ###")
    deployment = _create_deployment(
        model_name="ci-smoke-manual",
        canary_version="v2-good",
        canary_weight=0.5,
        # Low minimum_requests / long window: this scenario only sends a couple
        # dozen predictions total, and the point is to prove the pipeline works
        # end to end, not to reproduce production-scale traffic.
        policy_config={"minimum_requests": 5, "evaluation_window_seconds": 3600},
        automation_paused=True,
    )
    deployment_id = deployment["id"]
    _record_deployment_id("scenario1-manual", deployment_id)
    if deployment["status"] != "CANARY_RUNNING":
        raise SmokeTestFailure(f"[FAIL] expected CANARY_RUNNING, got: {deployment}")
    if not deployment["automation_paused"]:
        raise SmokeTestFailure(f"[FAIL] expected automation_paused=true, got: {deployment}")
    print(
        f"[ok] deployment {deployment_id} is CANARY_RUNNING (50/50 v1 / v2-good), "
        "automation paused"
    )

    for _ in range(40):
        payload, _true_label = _sample_row()
        response = httpx.post(f"{ROUTER_URL}/router/predict", json=payload, timeout=10)
        if response.status_code != 200:
            raise SmokeTestFailure(
                f"[FAIL] /router/predict returned {response.status_code}: {response.text}"
            )
    print("[ok] sent 40 predictions")

    _wait_for_min_samples(deployment_id, minimum=5)

    evaluate_response = httpx.post(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/evaluate", timeout=10
    )
    evaluate_response.raise_for_status()
    evaluation = evaluate_response.json()
    if evaluation["overall_result"] not in {"PASS", "FAIL", "INCONCLUSIVE"}:
        raise SmokeTestFailure(f"[FAIL] unexpected overall_result: {evaluation}")
    print(
        f"[ok] evaluated: overall_result={evaluation['overall_result']}, "
        f"{len(evaluation['checks'])} checks"
    )

    promote_response = httpx.post(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/promote", timeout=10
    )
    promote_response.raise_for_status()
    promoted = promote_response.json()
    if promoted["status"] != "PROMOTED":
        raise SmokeTestFailure(f"[FAIL] expected PROMOTED after manual promote, got: {promoted}")
    print(f"[ok] deployment {deployment_id} is PROMOTED (manual)")

    timeline = httpx.get(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/timeline", timeout=10
    ).json()
    has_policy_item = any(item["type"] == "policy_evaluation" for item in timeline)
    has_promoted_event = any(
        item["type"] == "event" and "PROMOTED" in item["message"] for item in timeline
    )
    has_automation_paused_event = any(
        item["type"] == "event" and item["event_type"] == "automation_paused" for item in timeline
    )
    if not (has_policy_item and has_promoted_event and has_automation_paused_event):
        raise SmokeTestFailure(f"[FAIL] timeline missing expected entries: {timeline}")
    print(
        f"[ok] timeline has {len(timeline)} chronologically merged entries, "
        "including the automation_paused event"
    )


def run_automatic_rollback_scenario() -> None:
    """Injects real +400ms latency into a canary and waits for the *actual*
    automated worker to detect the resulting FAIL and roll back on its own - no
    manual /promote or /rollback call anywhere in this scenario. This is the
    specific claim the README makes about CI, and it has to be true: earlier
    versions of this script called /evaluate then manually /promoted, which never
    exercised the worker's poll loop or its FAIL-detection at all.
    """
    print("\n### scenario 2/4: automatic rollback (worker-driven, not manual) ###")
    try:
        _set_fault_injection(LATENCY_FAULT_URL, latency_ms=400, error_rate=0.0)
        print("[ok] injected +400ms latency into model-serving-v2-good-latency-fault")

        deployment = _create_deployment(
            model_name="ci-smoke-latency-rollback",
            canary_version="v2-good-latency-fault",
            canary_weight=0.3,
            policy_config={
                "minimum_requests": 5,
                "evaluation_window_seconds": 10,
                "max_inconclusive_retries": 30,
                "latency": {"p95_max_increase_percent": 20.0},
            },
        )
        deployment_id = deployment["id"]
        _record_deployment_id("scenario2-latency-rollback", deployment_id)
        print(f"[ok] deployment {deployment_id} is {deployment['status']} (canary has the fault)")

        with _BackgroundTraffic():
            final = _wait_for_deployment_status(
                deployment_id, target_statuses={"ROLLED_BACK", "PROMOTED", "FAILED"},
                timeout_seconds=90,
            )
    finally:
        # Always reset, even if the wait above raised - a stuck fault would corrupt
        # every scenario/benchmark that runs after this one. See
        # scripts/benchmarks/run_benchmark.py's identical finally-block pattern.
        _set_fault_injection(LATENCY_FAULT_URL, latency_ms=0, error_rate=0.0)
        print("[ok] reset fault injection back to zero")

    if final["status"] != "ROLLED_BACK":
        raise SmokeTestFailure(
            f"[FAIL] expected the worker to roll this back automatically, got: {final}"
        )
    print(f"[ok] deployment {deployment_id} is ROLLED_BACK - the worker did this, not manually")

    timeline = httpx.get(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/timeline", timeout=10
    ).json()
    has_fail_check = any(
        item["type"] == "policy_evaluation"
        and item["policy_name"] == "latency_p95_increase"
        and item["result"] == "FAIL"
        for item in timeline
    )
    has_automatic_rollback_event = any(
        item["type"] == "event" and "rolled back to stable (automatic)" in item["message"]
        for item in timeline
    )
    if not (has_fail_check and has_automatic_rollback_event):
        raise SmokeTestFailure(
            f"[FAIL] timeline doesn't show a FAIL check + an automatic rollback event: {timeline}"
        )
    print("[ok] timeline shows the FAIL check and an automatic (not manual) rollback event")


def run_automatic_quality_promote_scenario() -> None:
    """A healthy canary (v2-good), waiting for the worker to really walk it
    through every traffic stage (10% -> 25% -> 50% -> 100%) and promote it on a
    genuine minimum_recall PASS - no manual /advance-traffic or /promote call,
    and no direct-DB actual_label write, anywhere in this scenario.

    Ground truth flows through the platform's real ingestion path: each
    background request's true label (known by _BackgroundTraffic, which sampled
    the row) is queued on a _LabelFeeder and reported, delayed, via POST
    /api/labels/batch - exactly the same mechanism scripts/benchmarks/
    locustfile.py uses. label_maturity_seconds/minimum_labeled_samples/
    minimum_label_coverage are tuned down so the quality window matures within
    this scenario's time budget; QUALITY_SCENARIO_POSITIVE_RATIO (see module
    docstring) is what makes the resulting recall PASS/FAIL deterministic
    rather than a coin flip - see minimum_positive_labels below and
    app/policy/engine.py.
    """
    print("\n### scenario 3/4: automatic quality-based promote (worker-driven) ###")
    stable_version = "v1"
    canary_version = "v2-good"
    deployment = _create_deployment(
        model_name="ci-smoke-quality-promote",
        canary_version=canary_version,
        canary_weight=0.10,
        policy_config={
            "minimum_requests": 5,
            "evaluation_window_seconds": 30,
            "max_inconclusive_retries": 30,
            "label_maturity_seconds": LABEL_MATURITY_SECONDS,
            "minimum_labeled_samples": 25,
            "minimum_label_coverage": 0.3,
            # See QUALITY_SCENARIO_POSITIVE_RATIO above - at 0.5 positive_ratio,
            # ~30s window, and 10-30% canary weight, the quality window comfortably
            # clears this with a wide margin (see docstring on _BackgroundTraffic).
            "minimum_positive_labels": 20,
            # Generous thresholds: this is a real (non-injected) v2-good instance
            # under CI-runner load, and the point of this scenario is proving the
            # worker's PASS -> advance/promote mechanics work, not re-litigating
            # real inference-latency variance (see scripts/benchmarks/scenarios.py's
            # own notes on this exact false-positive class of failure).
            "latency": {"p95_max_increase_percent": 2000.0},
            "reliability": {"max_error_rate_percent": 20.0},
            "quality": {"minimum_recall": 0.6},
        },
    )
    deployment_id = deployment["id"]
    _record_deployment_id("scenario3-quality-promote", deployment_id)
    print(f"[ok] deployment {deployment_id} is {deployment['status']} (10% healthy canary)")

    label_feeder = _LabelFeeder(
        control_plane_url=BACKEND_URL, label_delay_seconds=LABEL_DELAY_SECONDS, label_coverage=0.9
    )
    label_feeder.start()
    try:
        with (
            _BackgroundTraffic(
                label_feeder,
                interval_seconds=0.03,
                positive_ratio=QUALITY_SCENARIO_POSITIVE_RATIO,
            ),
            _StableSideHealthChecks(deployment_id, stable_version),
        ):
            final = _wait_for_deployment_status(
                deployment_id,
                target_statuses={"PROMOTED", "ROLLED_BACK", "INCONCLUSIVE", "FAILED"},
                timeout_seconds=240,
            )
    finally:
        label_feeder.stop()

    if final["status"] != "PROMOTED":
        raise SmokeTestFailure(
            f"[FAIL] expected the worker to promote this automatically, got: {final}"
        )
    print(f"[ok] deployment {deployment_id} is PROMOTED - the worker did this, not a manual call")

    timeline = httpx.get(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/timeline", timeout=10
    ).json()
    advance_events = [
        item
        for item in timeline
        if item["type"] == "event" and item["event_type"] == "traffic_advanced"
    ]
    has_automatic_promote_event = any(
        item["type"] == "event" and "promoted to 100% traffic (automatic)" in item["message"]
        for item in timeline
    )
    has_recall_pass = any(
        item["type"] == "policy_evaluation"
        and item["policy_name"] == "minimum_recall"
        and item["result"] == "PASS"
        for item in timeline
    )
    if len(advance_events) < 3 or not has_automatic_promote_event or not has_recall_pass:
        raise SmokeTestFailure(
            f"[FAIL] expected 3 traffic_advanced events (10->25->50->100), an "
            f"automatic promote event, and a real minimum_recall PASS; got "
            f"{len(advance_events)} advances, promote event="
            f"{has_automatic_promote_event}, recall PASS={has_recall_pass}: {timeline}"
        )
    print(
        f"[ok] timeline shows {len(advance_events)} automatic traffic advances, "
        "an automatic promote event, and a genuine minimum_recall PASS"
    )


def run_automatic_quality_rollback_scenario() -> None:
    """A deliberately weak canary (v2-quality-bad), same real delayed label flow
    as scenario 3, but recall genuinely fails the threshold - the worker
    automatically rolls it back for a quality reason alone. No fault injection
    anywhere in this scenario: this is the first CI proof that minimum_recall can
    actually resolve to FAIL (not just stay perpetually INCONCLUSIVE) and drive a
    real automatic rollback.
    """
    print("\n### scenario 4/4: automatic quality-based rollback (worker-driven) ###")
    stable_version = "v1"
    canary_version = "v2-quality-bad"
    deployment = _create_deployment(
        model_name="ci-smoke-quality-rollback",
        canary_version=canary_version,
        canary_weight=0.3,
        policy_config={
            "minimum_requests": 5,
            "evaluation_window_seconds": 30,
            "max_inconclusive_retries": 30,
            "label_maturity_seconds": LABEL_MATURITY_SECONDS,
            "minimum_labeled_samples": 25,
            "minimum_label_coverage": 0.3,
            # See QUALITY_SCENARIO_POSITIVE_RATIO above - at 0.5 positive_ratio,
            # ~30s window, and 10-30% canary weight, the quality window comfortably
            # clears this with a wide margin (see docstring on _BackgroundTraffic).
            "minimum_positive_labels": 20,
            "latency": {"p95_max_increase_percent": 2000.0},
            "reliability": {"max_error_rate_percent": 20.0},
            "quality": {"minimum_recall": 0.6},
        },
    )
    deployment_id = deployment["id"]
    _record_deployment_id("scenario4-quality-rollback", deployment_id)
    print(f"[ok] deployment {deployment_id} is {deployment['status']} (30% weak canary)")

    label_feeder = _LabelFeeder(
        control_plane_url=BACKEND_URL, label_delay_seconds=LABEL_DELAY_SECONDS, label_coverage=0.9
    )
    label_feeder.start()
    try:
        with (
            _BackgroundTraffic(
                label_feeder,
                interval_seconds=0.03,
                positive_ratio=QUALITY_SCENARIO_POSITIVE_RATIO,
            ),
            _StableSideHealthChecks(deployment_id, stable_version),
        ):
            final = _wait_for_deployment_status(
                deployment_id,
                target_statuses={"PROMOTED", "ROLLED_BACK", "INCONCLUSIVE", "FAILED"},
                timeout_seconds=240,
            )
    finally:
        label_feeder.stop()

    if final["status"] != "ROLLED_BACK":
        raise SmokeTestFailure(
            f"[FAIL] expected the worker to roll this back automatically on a real "
            f"recall FAIL, got: {final}"
        )
    print(f"[ok] deployment {deployment_id} is ROLLED_BACK - a genuine quality-based rollback")

    timeline = httpx.get(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/timeline", timeout=10
    ).json()
    has_recall_fail = any(
        item["type"] == "policy_evaluation"
        and item["policy_name"] == "minimum_recall"
        and item["result"] == "FAIL"
        for item in timeline
    )
    has_automatic_rollback_event = any(
        item["type"] == "event" and "rolled back to stable (automatic)" in item["message"]
        for item in timeline
    )
    if not (has_recall_fail and has_automatic_rollback_event):
        raise SmokeTestFailure(
            f"[FAIL] timeline doesn't show a real minimum_recall FAIL + an automatic "
            f"rollback event: {timeline}"
        )
    print("[ok] timeline shows a genuine minimum_recall FAIL and an automatic rollback event")


def run_router_restart_reconcile_scenario() -> None:
    """See the module docstring's item 5 for the full rationale. Uses
    model_name="fraud-model" - the router's own fixed ROUTER_MODEL_NAME -
    deliberately, not a ci-smoke-* name: the reconciler looks up the desired
    deployment via the *router's own reported model_name*
    (app/control_plane/reconcile.py's _find_active_deployment_for_model), which
    after a full restart is the router's bootstrap default, not whatever
    deployment last pushed to it. In this project's real topology there's only
    ever one router bound to one fixed model_name, so this is the realistic
    case, not a special one.

    Whether the "still broken" intermediate state is actually *observable* by
    this script is a race this scenario doesn't try to win: the router's own
    startup-sync (app/router/main.py's lifespan) can independently recover
    `deployment_id` (though never `revision` - see app/control_plane/api.py's
    get_router_config, which intentionally omits it) before this script's
    first post-restart GET even lands, and with WORKER_POLL_INTERVAL_SECONDS
    turned down for CI, the reconciler itself can close the remaining
    revision gap within a second or two of the healthcheck passing - found by
    running this scenario for real, not assumed. So the definitive proof here
    isn't "the router was observably broken" (best-effort, logged, not fatal)
    - it's the `router_reconciled` DeploymentEvent itself, which reconcile.py
    only ever writes on a genuine correction, never on a no-op tick.

    automation_paused=True so the ONLY thing that can move this deployment's
    desired revision during the scenario is its own creation - not the
    worker's evaluate/promote/rollback machinery - keeping the desired-vs-
    observed comparison unambiguous. No real prediction traffic needed: this
    scenario is entirely about the router's *config* state, not routing
    decisions.
    """
    print("\n### scenario 5/5: router restart -> automatic reconcile ###")
    deployment = _create_deployment(
        model_name="fraud-model",
        canary_version="v2-good",
        canary_weight=0.3,
        policy_config={"minimum_requests": 1_000_000},  # never evaluates
        automation_paused=True,
    )
    deployment_id = deployment["id"]
    _record_deployment_id("scenario5-router-restart", deployment_id)
    desired_revision = deployment["traffic_allocation"]["revision"]
    print(
        f"[ok] deployment {deployment_id} is {deployment['status']} "
        f"(desired revision {desired_revision})"
    )

    before = httpx.get(f"{ROUTER_URL}/router/config", timeout=10).json()
    if before.get("deployment_id") != deployment_id or before.get("revision") != desired_revision:
        raise SmokeTestFailure(
            f"[FAIL] router wasn't even synced to this deployment before the restart: {before}"
        )
    print(f"[ok] router observed deployment {deployment_id} at revision {desired_revision}")

    subprocess.run(
        ["docker", "compose", "restart", "router"], check=True, cwd=REPO_ROOT, timeout=60
    )
    _wait_for("router", f"{ROUTER_URL}/router/health", timeout_seconds=60)

    after_restart = httpx.get(f"{ROUTER_URL}/router/config", timeout=10).json()
    if (
        after_restart.get("deployment_id") == deployment_id
        and after_restart.get("revision") == desired_revision
    ):
        # Not fatal - see the docstring above on why this is a race this
        # scenario doesn't try to win: reconciliation can genuinely finish
        # before this GET lands. The router_reconciled event check below is
        # the real proof.
        print(
            "[note] router already shows the desired state right after restart - "
            "either lifespan startup-sync + a fast reconcile tick already closed "
            f"the gap, or this GET simply lost the race: {after_restart}"
        )
    else:
        print(f"[ok] observed the router genuinely out of sync after restart: {after_restart}")

    deadline = time.monotonic() + 60
    current = after_restart
    while time.monotonic() < deadline:
        current = httpx.get(f"{ROUTER_URL}/router/config", timeout=10).json()
        if (
            current.get("deployment_id") == deployment_id
            and current.get("revision") == desired_revision
        ):
            break
        time.sleep(2)
    else:
        raise SmokeTestFailure(
            f"[FAIL] router never reconciled back to deployment {deployment_id} "
            f"revision {desired_revision} within 60s (last observed: {current})"
        )
    print(f"[ok] router automatically reconciled back to revision {desired_revision}")

    weights = {t["version"]: t["weight"] for t in current["targets"]}
    if abs(weights.get("v2-good", 0.0) - 0.3) > 1e-6:
        raise SmokeTestFailure(
            f"[FAIL] reconciled targets don't match desired 30% canary: {current}"
        )

    timeline = httpx.get(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/timeline", timeout=10
    ).json()
    has_reconcile_event = any(
        item["type"] == "event" and item["event_type"] == "router_reconciled" for item in timeline
    )
    if not has_reconcile_event:
        raise SmokeTestFailure(f"[FAIL] timeline missing router_reconciled event: {timeline}")
    print("[ok] timeline shows the automatic router_reconciled event")


SCENARIO_MODEL_NAMES = (
    "ci-smoke-manual",
    "ci-smoke-latency-rollback",
    "ci-smoke-quality-promote",
    "ci-smoke-quality-rollback",
    # Not "ci-smoke-*" - see run_router_restart_reconcile_scenario's docstring
    # for why this one deliberately uses the router's own fixed model_name.
    "fraud-model",
)


def main() -> None:
    print("=== waiting for every service to be healthy ===")
    for name, url in HEALTH_ENDPOINTS.items():
        _wait_for(name, url)

    _assert_no_orphaned_deployments(SCENARIO_MODEL_NAMES)

    run_manual_smoke_scenario()
    run_automatic_rollback_scenario()
    run_automatic_quality_promote_scenario()
    run_automatic_quality_rollback_scenario()
    run_router_restart_reconcile_scenario()

    print("\nAll integration checks passed.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(
            f"[FAIL] {exc.request.method} {exc.request.url} -> "
            f"{exc.response.status_code}: {exc.response.text}"
        )
        sys.exit(1)
