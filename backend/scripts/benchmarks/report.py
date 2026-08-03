"""Pure, unit-testable functions for turning raw control-plane API responses (events,
policy evaluations) plus a load-test result into timing measurements and a report.
No I/O here - orchestration (run_benchmark.py) does the fetching/writing; this module
just computes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_timestamp(value: str) -> datetime:
    """Parses an API-returned ISO timestamp, assuming UTC if no offset is present.

    SQLite's DateTime(timezone=True) isn't actually enforced by the driver, so a
    value written as UTC can round-trip through the API as an offset-naive string
    (same class of bug fixed in app/worker/loop.py in Sprint 8). Everything the
    control plane writes is UTC (see control_plane/models.py's _utcnow), so treating
    a naive timestamp as UTC here is safe, not a guess.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def compute_time_to_detect_seconds(
    started_at: str, evaluations: list[dict[str, Any]], detect_result: str
) -> float | None:
    """Seconds from rollout start to the first PolicyEvaluation row with
    `result == detect_result` (e.g. the first FAIL that eventually caused a
    rollback). None if no such row exists (e.g. it never failed, or hasn't yet)."""
    matching = [e for e in evaluations if e["result"] == detect_result]
    if not matching:
        return None
    earliest = min(parse_timestamp(e["evaluated_at"]) for e in matching)
    return (earliest - parse_timestamp(started_at)).total_seconds()


def compute_time_to_action_seconds(
    started_at: str, events: list[dict[str, Any]], message_substring: str
) -> float | None:
    """Seconds from rollout start to the first DeploymentEvent whose message
    contains `message_substring` (e.g. "ROLLED_BACK" or "PROMOTED"). None if no
    matching event exists."""
    matching = [e for e in events if message_substring in e["message"]]
    if not matching:
        return None
    earliest = min(parse_timestamp(e["created_at"]) for e in matching)
    return (earliest - parse_timestamp(started_at)).total_seconds()


@dataclass
class LoadTestResult:
    total_requests: int
    total_failures: int
    requests_per_second: float
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    duration_seconds: float


@dataclass
class BenchmarkResult:
    scenario: str
    description: str
    expected_outcome: str
    observed_outcome: str
    outcome_matches_expectation: bool
    deployment_id: str
    model_name: str
    started_at: str | None
    final_status: str
    load: LoadTestResult | None
    time_to_detect_seconds: float | None
    time_to_action_seconds: float | None
    notes: list[str] = field(default_factory=list)
    run_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def save_json_report(result: BenchmarkResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    path.write_text(json.dumps(payload, indent=2, default=str))


def render_markdown_report(result: BenchmarkResult) -> str:
    lines = [
        f"# Benchmark: {result.scenario}",
        "",
        f"_Run at {result.run_at}_",
        "",
        result.description,
        "",
        "## Outcome",
        "",
        f"- Expected: **{result.expected_outcome}**",
        f"- Observed: **{result.observed_outcome}** (final status: `{result.final_status}`)",
        f"- Matches expectation: {'✅ yes' if result.outcome_matches_expectation else '❌ no'}",
        "",
    ]

    if result.notes:
        lines.append("## Notes")
        lines.append("")
        for note in result.notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## Timing")
    lines.append("")
    lines.append(f"- Time to detect: {_format_seconds(result.time_to_detect_seconds)}")
    lines.append(f"- Time to action: {_format_seconds(result.time_to_action_seconds)}")
    lines.append("")

    if result.load is not None:
        load = result.load
        lines.extend(
            [
                "## Load test",
                "",
                f"- Duration: {load.duration_seconds:.0f}s",
                f"- Total requests: {load.total_requests}",
                f"- Total failures: {load.total_failures}",
                f"- Throughput: {load.requests_per_second:.1f} req/s",
                f"- Error rate: {load.error_rate * 100:.2f}%",
                f"- Latency p50: {load.p50_ms:.1f} ms",
                f"- Latency p95: {load.p95_ms:.1f} ms",
                f"- Latency p99: {load.p99_ms:.1f} ms",
                "",
            ]
        )

    lines.append(f"Deployment: `{result.deployment_id}` (model_name=`{result.model_name}`)")
    lines.append("")
    return "\n".join(lines)


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "N/A (not observed within the wait window)"
    return f"{value:.1f}s"
