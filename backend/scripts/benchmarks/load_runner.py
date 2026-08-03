"""Runs Locust headless (as a subprocess) against the router and parses its CSV
summary into a LoadTestResult. Locust owns load generation; this module only shells
out to it and parses output - no HTTP calls of its own.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

from scripts.benchmarks.report import LoadTestResult

LOCUSTFILE = Path(__file__).resolve().parent / "locustfile.py"


def parse_locust_stats_csv(stats_csv_path: Path, duration_seconds: float) -> LoadTestResult:
    with stats_csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    aggregated = next((row for row in rows if row["Name"] == "Aggregated"), None)
    if aggregated is None:
        raise ValueError(f"No 'Aggregated' row found in {stats_csv_path}")

    total_requests = int(aggregated["Request Count"])
    total_failures = int(aggregated["Failure Count"])
    error_rate = (total_failures / total_requests) if total_requests else 0.0

    return LoadTestResult(
        total_requests=total_requests,
        total_failures=total_failures,
        requests_per_second=float(aggregated["Requests/s"]),
        error_rate=error_rate,
        p50_ms=float(aggregated["50%"]),
        p95_ms=float(aggregated["95%"]),
        p99_ms=float(aggregated["99%"]),
        duration_seconds=duration_seconds,
    )


def start_locust(
    *,
    host: str,
    duration_seconds: int,
    users: int,
    target_rps: float,
    output_prefix: Path,
) -> subprocess.Popen[bytes]:
    """Launches Locust in the background (headless) so the caller can poll the
    control plane concurrently while load is in flight."""
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(LOCUSTFILE),
        "--headless",
        "-u",
        str(users),
        "-r",
        str(users),
        "--run-time",
        f"{duration_seconds}s",
        "--host",
        host,
        "--csv",
        str(output_prefix),
        "--only-summary",
        # The error-failure scenario deliberately induces request failures - that's
        # the subject under test, not a load-tool crash. Locust's default nonzero
        # exit on any failed request would make finish_locust() treat expected
        # failures the same as Locust itself crashing; force it to always exit 0 and
        # let the caller judge success/failure from the parsed stats CSV instead.
        "--exit-code-on-error",
        "0",
    ]
    env = {
        "BENCHMARK_TARGET_RPS": str(target_rps),
        "LOCUST_USER_COUNT": str(users),
    }
    return subprocess.Popen(
        cmd,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def finish_locust(
    process: subprocess.Popen[bytes], output_prefix: Path, duration_seconds: float, timeout: float
) -> LoadTestResult:
    """Waits for a Popen started by start_locust() to exit and parses its results."""
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise RuntimeError(f"locust did not exit within {timeout}s") from exc

    if process.returncode != 0:
        raise RuntimeError(
            f"locust exited with code {process.returncode}:\n"
            f"{stdout.decode(errors='replace') if stdout else ''}"
        )

    stats_path = output_prefix.with_name(output_prefix.name + "_stats.csv")
    return parse_locust_stats_csv(stats_path, duration_seconds)


def run_locust(
    *,
    host: str,
    duration_seconds: int,
    users: int,
    target_rps: float,
    output_prefix: Path,
) -> LoadTestResult:
    """Blocking convenience wrapper for callers that don't need to do anything else
    while load is running (e.g. the baseline scenario)."""
    process = start_locust(
        host=host,
        duration_seconds=duration_seconds,
        users=users,
        target_rps=target_rps,
        output_prefix=output_prefix,
    )
    return finish_locust(process, output_prefix, duration_seconds, timeout=duration_seconds + 60)
