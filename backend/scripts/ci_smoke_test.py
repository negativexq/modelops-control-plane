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
see README's "Troubleshooting" section for the two entries this script would have
caught immediately: SQLite tzinfo and the 100%-canary starvation.
"""

import os
import sys
import time

import httpx

from scripts.benchmarks.locustfile import _sample_payload

BACKEND_URL = os.environ.get("CI_BACKEND_URL", "http://localhost:8000")
ROUTER_URL = os.environ.get("CI_ROUTER_URL", "http://localhost:8080")
FRONTEND_URL = os.environ.get("CI_FRONTEND_URL", "http://localhost:3000")

# Every HTTP-exposed service in docker-compose.yml. The worker has no HTTP surface
# by design (see README) so it isn't checked here directly - its effects (if any)
# would show up in the deployment's timeline instead, and this script deliberately
# doesn't wait on it (its poll interval is 15s; a human-triggered promote below
# is what actually verifies the rollout can be moved, deterministically and fast).
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
    raise SystemExit(f"[FAIL] {name} never became ready at {url}: {last_error}")


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
    raise SystemExit(
        f"[FAIL] metrics never reached {minimum}/side within {timeout_seconds}s: {counts}"
    )


def main() -> None:
    print("=== waiting for every service to be healthy ===")
    for name, url in HEALTH_ENDPOINTS.items():
        _wait_for(name, url)

    print("\n=== creating a canary deployment ===")
    create_response = httpx.post(
        f"{BACKEND_URL}/api/deployments",
        json={
            "model_name": "fraud-model",
            "stable_version": "v1",
            "canary_version": "v2-good",
            "canary_weight": 0.5,
            # Low minimum_requests / long window: this run only sends a couple
            # dozen predictions total, and the point is to prove the pipeline works
            # end to end, not to reproduce production-scale traffic.
            "policy_config": {"minimum_requests": 5, "evaluation_window_seconds": 3600},
        },
        headers={"Idempotency-Key": f"ci-smoke-{int(time.time())}"},
        timeout=10,
    )
    create_response.raise_for_status()
    deployment = create_response.json()
    deployment_id = deployment["id"]
    if deployment["status"] != "CANARY_RUNNING":
        raise SystemExit(f"[FAIL] expected CANARY_RUNNING, got: {deployment}")
    print(f"[ok] deployment {deployment_id} is CANARY_RUNNING (50/50 v1 / v2-good)")

    print("\n=== sending predictions through the router ===")
    for _ in range(40):
        response = httpx.post(f"{ROUTER_URL}/router/predict", json=_sample_payload(), timeout=10)
        if response.status_code != 200:
            raise SystemExit(
                f"[FAIL] /router/predict returned {response.status_code}: {response.text}"
            )
    print("[ok] sent 40 predictions")

    _wait_for_min_samples(deployment_id, minimum=5)

    print("\n=== evaluating policies ===")
    evaluate_response = httpx.post(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/evaluate", timeout=10
    )
    evaluate_response.raise_for_status()
    evaluation = evaluate_response.json()
    if evaluation["overall_result"] not in {"PASS", "FAIL", "INCONCLUSIVE"}:
        raise SystemExit(f"[FAIL] unexpected overall_result: {evaluation}")
    print(
        f"[ok] evaluated: overall_result={evaluation['overall_result']}, "
        f"{len(evaluation['checks'])} checks"
    )

    print("\n=== promoting the canary ===")
    promote_response = httpx.post(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/promote", timeout=10
    )
    promote_response.raise_for_status()
    promoted = promote_response.json()
    if promoted["status"] != "PROMOTED":
        raise SystemExit(f"[FAIL] expected PROMOTED after promote, got: {promoted}")
    print(f"[ok] deployment {deployment_id} is PROMOTED")

    print("\n=== checking the timeline tells the same story ===")
    timeline_response = httpx.get(
        f"{BACKEND_URL}/api/deployments/{deployment_id}/timeline", timeout=10
    )
    timeline_response.raise_for_status()
    timeline = timeline_response.json()
    has_policy_item = any(item["type"] == "policy_evaluation" for item in timeline)
    has_promoted_event = any(
        item["type"] == "event" and "PROMOTED" in item["message"] for item in timeline
    )
    if not (has_policy_item and has_promoted_event):
        raise SystemExit(f"[FAIL] timeline missing expected entries: {timeline}")
    print(f"[ok] timeline has {len(timeline)} chronologically merged entries")

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
