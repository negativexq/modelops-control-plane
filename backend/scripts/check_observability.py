"""Checks the observability stack against a running `docker compose up`.

    python -m scripts.check_observability        (from backend/, after the smoke test)

Run by CI's integration job right after scripts.ci_smoke_test, whose rollouts
are what make the "decisions recorded" and "traces exported" checks meaningful.
Exits non-zero listing every check that failed, not just the first.
"""

import csv
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

PROMETHEUS = os.getenv("CHECK_PROMETHEUS_URL", "http://localhost:9090")
GRAFANA = os.getenv("CHECK_GRAFANA_URL", "http://localhost:3001")
JAEGER = os.getenv("CHECK_JAEGER_URL", "http://localhost:16686")
ROUTER = os.getenv("CHECK_ROUTER_URL", "http://localhost:8080")
DATASET = Path(__file__).resolve().parents[1] / "data" / "test.csv"

EXPECTED_JOBS = {"backend", "router", "worker", "serving"}
EXPECTED_DASHBOARDS = {"modelops-overview", "modelops-services"}
EXPECTED_TRACE_SERVICES = {"model-router", "control-plane", "worker"}


def prom_query(client: httpx.Client, expr: str) -> list[dict[str, object]]:
    response = client.get(f"{PROMETHEUS}/api/v1/query", params={"query": expr})
    response.raise_for_status()
    result: list[dict[str, object]] = response.json()["data"]["result"]
    return result


def check_targets(client: httpx.Client) -> str | None:
    targets = client.get(f"{PROMETHEUS}/api/v1/targets").json()["data"]["activeTargets"]
    down = [f"{t['scrapeUrl']}: {t['lastError']}" for t in targets if t["health"] != "up"]
    jobs = {t["labels"]["job"] for t in targets}
    if missing := EXPECTED_JOBS - jobs:
        return f"no scrape targets for {sorted(missing)}"
    if down:
        return "targets not up: " + "; ".join(down)
    return None


def check_rules(client: httpx.Client) -> str | None:
    groups = client.get(f"{PROMETHEUS}/api/v1/rules").json()["data"]["groups"]
    rules = [r["name"] for g in groups for r in g["rules"]]
    return None if rules else "no alert rules loaded (observability/prometheus/rules.yml)"


def check_decisions(client: httpx.Client) -> str | None:
    # The smoke test promotes and rolls back for real, so both must be counted.
    result = prom_query(client, "sum by (outcome) (modelops_decisions_total)")
    counts = {r["metric"]["outcome"]: float(r["value"][1]) for r in result}  # type: ignore[index]
    missing = [o for o in ("PROMOTED", "ROLLED_BACK") if counts.get(o, 0) < 1]
    return f"no {missing} decisions in modelops_decisions_total: {counts}" if missing else None


def check_router_metrics(client: httpx.Client) -> str | None:
    if not prom_query(client, "modelops_router_requests_total"):
        return "router exported no modelops_router_requests_total series"
    if not prom_query(client, "modelops_router_traffic_weight"):
        return "router exported no modelops_router_traffic_weight series"
    return None


def check_dashboards(client: httpx.Client) -> str | None:
    response = client.get(f"{GRAFANA}/api/search", params={"tag": "modelops"})
    found = {d["uid"] for d in response.json()}
    missing = EXPECTED_DASHBOARDS - found
    return f"Grafana didn't provision {sorted(missing)}" if missing else None


def check_traces(client: httpx.Client) -> str | None:
    services = set(client.get(f"{JAEGER}/api/services").json()["data"] or [])
    if missing := EXPECTED_TRACE_SERVICES - services:
        return f"no traces from {sorted(missing)} (have {sorted(services)})"
    # A prediction's trace must reach a model service, i.e. context propagated.
    # Filtered to prediction spans (only those carry modelops.model): the most
    # recent router traces are otherwise worker sweeps and dashboard reads.
    traces = client.get(
        f"{JAEGER}/api/traces",
        params={
            "service": "model-router",
            "tags": json.dumps({"modelops.model": "fraud-model"}),
            "limit": 20,
            "lookback": "1h",
        },
    ).json()["data"]
    for trace in traces:
        names = {p["serviceName"] for p in trace["processes"].values()}
        if any(n.startswith("model-serving") for n in names):
            return None
    return "no router trace includes a model-serving span (traceparent not propagated?)"


CHECKS: list[tuple[str, Callable[[httpx.Client], str | None]]] = [
    ("Prometheus scrape targets", check_targets),
    ("Prometheus alert rules", check_rules),
    ("Router metrics", check_router_metrics),
    ("Decision counters", check_decisions),
    ("Grafana dashboards", check_dashboards),
    ("Jaeger traces", check_traces),
]


def send_fresh_predictions(count: int = 20) -> None:
    """A few predictions through the router right before checking. The smoke
    test ends by restarting the router, so it starts these checks with no
    request counters (a labelled counter has no series until its first
    increment) and no prediction traces since the restart."""
    with DATASET.open(newline="") as f:
        row = next(csv.DictReader(f))
    payload = {
        k: v if k == "merchant_category" else float(v)
        for k, v in row.items()
        if k not in {"transaction_id", "customer_id", "label"}
    }
    with httpx.Client(timeout=10) as client:
        for _ in range(count):
            client.post(f"{ROUTER}/router/models/fraud-model/predict", json=payload)


def main() -> int:
    send_fresh_predictions()
    # Prometheus scrapes every 5s and spans export in batches; give both a moment
    # to catch up with the smoke test that just finished.
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=10) as client:
        while True:
            failures = []
            for name, check in CHECKS:
                try:
                    problem = check(client)
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    problem = f"{type(exc).__name__}: {exc}"
                if problem:
                    failures.append(f"{name}: {problem}")
            if not failures or time.monotonic() > deadline:
                break
            time.sleep(5)

    for name, _ in CHECKS:
        failed = next((f for f in failures if f.startswith(f"{name}:")), None)
        print(f"{'FAIL' if failed else 'ok  '}  {failed or name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
