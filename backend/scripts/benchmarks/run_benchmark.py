"""Orchestrates one benchmark scenario end to end: turn on the scenario's fault (if
any) via the target serving container's runtime /fault-injection endpoint, create an
isolated deployment, generate load with Locust, wait for the worker to act (or not),
turn the fault back off, and write a JSON + Markdown report.

Deliberately has no dependency on the `docker` CLI or the Docker socket - fault
containers (model-serving-v2-good-latency-fault, model-serving-v2-good-error-fault)
run continuously with fault injection off by default; this script only ever makes
plain HTTP calls to them. That's what lets this same script run unmodified from
inside the backend container too (see app/benchmarks/service.py), which has no
access to the host's Docker daemon and shouldn't need any.

Usage:
    python -m scripts.benchmarks.run_benchmark --scenario baseline
    python -m scripts.benchmarks.run_benchmark --scenario latency-failure --duration-seconds 60
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import threading
import time
from pathlib import Path

import httpx

from scripts.benchmarks.control_plane_client import ControlPlaneClient
from scripts.benchmarks.load_runner import finish_locust, start_locust
from scripts.benchmarks.report import (
    BenchmarkResult,
    compute_time_to_action_seconds,
    compute_time_to_detect_seconds,
    render_markdown_report,
    save_json_report,
)
from scripts.benchmarks.scenarios import SCENARIOS, FaultTarget, Scenario

logger = logging.getLogger("benchmark")

RESULTS_DIR = Path(__file__).resolve().parents[2] / "benchmark-results"

TERMINAL_STATUSES = {"PROMOTED", "ROLLED_BACK", "FAILED", "INCONCLUSIVE"}
OUTCOME_STATUS = {
    "rollback": "ROLLED_BACK",
    "promote": "PROMOTED",
    "inconclusive_freeze": "INCONCLUSIVE",
}

# Where each always-running fault container's /fault-injection endpoint lives.
# Defaults match the host-mapped ports in docker-compose.yml, for CLI/Makefile use
# (`make benchmark-latency-failure` etc., run from the host). When this script is
# spawned from inside the backend container instead (dashboard-triggered runs), the
# env vars are overridden to the docker-internal service hostnames - see the
# BENCHMARK_*_FAULT_URL entries on the `backend` service in docker-compose.yml.
FAULT_TARGET_URLS: dict[FaultTarget, tuple[str, str]] = {
    "latency": ("BENCHMARK_LATENCY_FAULT_URL", "http://localhost:8004"),
    "error": ("BENCHMARK_ERROR_FAULT_URL", "http://localhost:8005"),
}


def _fault_target_url(target: FaultTarget) -> str:
    env_var, default = FAULT_TARGET_URLS[target]
    return os.environ.get(env_var, default)


def _set_fault_injection(target: FaultTarget, latency_ms: int, error_rate: float) -> None:
    url = _fault_target_url(target)
    response = httpx.put(
        f"{url}/fault-injection",
        json={"latency_ms": latency_ms, "error_rate": error_rate},
        timeout=10,
    )
    response.raise_for_status()


def _reset_fault_injection(target: FaultTarget | None) -> None:
    """Always called, success or failure (see run_scenario's try/finally) - a
    benchmark that crashes partway through must not leave a fault live for whatever
    runs next, real traffic included."""
    if target is None:
        return
    try:
        _set_fault_injection(target, 0, 0.0)
    except httpx.HTTPError:
        logger.exception(
            "failed to reset fault injection for target=%s at %s - manual cleanup "
            'needed (PUT {"latency_ms": 0, "error_rate": 0} to its /fault-injection)',
            target,
            _fault_target_url(target),
        )


def _feed_stable_side_health_checks(
    client: ControlPlaneClient,
    deployment_id: str,
    stable_version: str,
    stop_event: threading.Event,
    interval_seconds: float = 5.0,
    batch_size: int = 15,
) -> None:
    """Runs until `stop_event` is set, periodically posting a small batch of plain
    (unlabeled) healthy metrics for `stable_version` - found necessary via manual
    end-to-end verification: once the canary reaches 100% traffic, the router
    sends stable zero real requests, so minimum_requests (which requires BOTH
    sides to have enough samples) would stay unmet forever and the deployment
    could never reach the final PROMOTE step. This simulates the kind of
    background health-check/synthetic-monitoring traffic real systems commonly
    send to an idle standby instance - it carries no `actual_label`, unlike the
    real (canary-side) traffic the Locust label feeder generates.
    """
    rng = random.Random(1234)
    while not stop_event.is_set():
        for _ in range(batch_size):
            client.post_metric(
                deployment_id,
                stable_version,
                latency_ms=rng.uniform(8, 20),
                status_code=200,
            )
        stop_event.wait(interval_seconds)


def _wait_for_outcome(
    client: ControlPlaneClient, deployment_id: str, expected_outcome: str, max_wait_seconds: float
) -> dict[str, object]:
    target_status = OUTCOME_STATUS.get(expected_outcome)
    deadline = time.monotonic() + max_wait_seconds
    last: dict[str, object] = client.get_deployment(deployment_id)

    while time.monotonic() < deadline:
        last = client.get_deployment(deployment_id)
        status = last["status"]
        if target_status is not None and status == target_status:
            return last
        if status in TERMINAL_STATUSES:
            # Reached a terminal state, just not the expected one - nothing more
            # will happen automatically from here, so there's no point waiting out
            # the rest of the deadline.
            return last
        time.sleep(5)

    return last


def run_scenario(
    scenario: Scenario,
    *,
    control_plane_url: str,
    router_url: str,
    duration_seconds: int | None,
    max_wait_seconds: int | None,
    users: int,
    target_rps: float,
    output_dir: Path = RESULTS_DIR,
    label_delay_seconds: float = 10.0,
    label_coverage: float = 0.8,
) -> BenchmarkResult:
    load_duration = duration_seconds or scenario.load_duration_seconds
    wait_budget = max_wait_seconds or scenario.max_wait_seconds

    if scenario.fault_target is not None:
        _set_fault_injection(
            scenario.fault_target, scenario.fault_latency_ms, scenario.fault_error_rate
        )

    health_check_stop = threading.Event()
    health_check_thread: threading.Thread | None = None

    try:
        with ControlPlaneClient(control_plane_url) as client:
            deployment = client.create_deployment(
                model_name=scenario.model_name,
                stable_version=scenario.stable_version,
                canary_version=scenario.canary_version,
                canary_weight=scenario.canary_weight,
                policy_config=scenario.policy_config,
            )
            deployment_id = deployment["id"]

            if scenario.needs_stable_side_health_checks:
                health_check_thread = threading.Thread(
                    target=_feed_stable_side_health_checks,
                    args=(client, deployment_id, scenario.stable_version, health_check_stop),
                    daemon=True,
                )
                health_check_thread.start()

            output_prefix = output_dir / f"{scenario.key}-{int(time.time())}"
            locust_process = start_locust(
                host=router_url,
                duration_seconds=load_duration,
                users=users,
                target_rps=target_rps,
                output_prefix=output_prefix,
                control_plane_url=control_plane_url,
                label_delay_seconds=label_delay_seconds,
                label_coverage=label_coverage,
            )

            final_deployment = _wait_for_outcome(
                client, deployment_id, scenario.expected_outcome, wait_budget
            )

            load_result = finish_locust(
                locust_process, output_prefix, load_duration, timeout=load_duration + 60
            )

            events = final_deployment.get("events", [])
            evaluations = client.get_policy_evaluations(deployment_id)
            started_at = final_deployment.get("started_at") or final_deployment.get("created_at")

            time_to_detect = None
            time_to_action = None
            if scenario.expected_outcome == "rollback":
                time_to_detect = compute_time_to_detect_seconds(started_at, evaluations, "FAIL")
                time_to_action = compute_time_to_action_seconds(
                    started_at, events, "rolled back to stable"
                )
            elif scenario.expected_outcome == "promote":
                time_to_action = compute_time_to_action_seconds(
                    started_at, events, "promoted to 100%"
                )
            elif scenario.expected_outcome == "inconclusive_freeze":
                time_to_action = compute_time_to_action_seconds(
                    started_at, events, "freezing for manual review"
                )

            observed_status = final_deployment["status"]
            target_status = OUTCOME_STATUS.get(scenario.expected_outcome)
            if scenario.expected_outcome == "throughput_only":
                observed_outcome = "throughput_only"
                matches = True
            else:
                observed_outcome = observed_status
                matches = observed_status == target_status

            result = BenchmarkResult(
                scenario=scenario.key,
                description=scenario.description,
                expected_outcome=scenario.expected_outcome,
                observed_outcome=observed_outcome,
                outcome_matches_expectation=matches,
                deployment_id=deployment_id,
                model_name=scenario.model_name,
                started_at=started_at,
                final_status=observed_status,
                load=load_result,
                time_to_detect_seconds=time_to_detect,
                time_to_action_seconds=time_to_action,
                notes=list(scenario.notes),
            )
    finally:
        health_check_stop.set()
        if health_check_thread is not None:
            health_check_thread.join(timeout=10)
        _reset_fault_injection(scenario.fault_target)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--control-plane-url", default="http://localhost:8000")
    parser.add_argument("--router-url", default="http://localhost:8080")
    parser.add_argument("--duration-seconds", type=int, default=None)
    parser.add_argument("--max-wait-seconds", type=int, default=None)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--target-rps", type=float, default=100.0)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument(
        "--label-delay-seconds",
        type=float,
        default=10.0,
        help="Delay before a sampled request's true label is reported via POST "
        "/api/labels/batch - should stay below the deployment's "
        "label_maturity_seconds so a healthy run's labels are matured in time.",
    )
    parser.add_argument(
        "--label-coverage",
        type=float,
        default=0.8,
        help="Fraction of predictions whose true label actually gets reported "
        "(< 1.0 so minimum_label_coverage's gate is genuinely exercised).",
    )
    args = parser.parse_args()

    scenario = SCENARIOS[args.scenario]
    result = run_scenario(
        scenario,
        control_plane_url=args.control_plane_url,
        router_url=args.router_url,
        duration_seconds=args.duration_seconds,
        max_wait_seconds=args.max_wait_seconds,
        users=args.users,
        target_rps=args.target_rps,
        output_dir=args.output_dir,
        label_delay_seconds=args.label_delay_seconds,
        label_coverage=args.label_coverage,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{scenario.key}.json"
    md_path = args.output_dir / f"{scenario.key}.md"
    save_json_report(result, json_path)
    md_path.write_text(render_markdown_report(result))

    print(render_markdown_report(result))
    print(f"\nSaved: {json_path}")
    print(f"Saved: {md_path}")

    if not result.outcome_matches_expectation:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
