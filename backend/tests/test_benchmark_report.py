import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.benchmarks.report import (
    BenchmarkResult,
    LoadTestResult,
    compute_time_to_action_seconds,
    compute_time_to_detect_seconds,
    parse_timestamp,
    render_markdown_report,
    save_json_report,
)


def test_parse_timestamp_keeps_explicit_offset() -> None:
    parsed = parse_timestamp("2026-08-03T20:00:00+00:00")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 8, 3, 20, 0, 0, tzinfo=UTC)


def test_parse_timestamp_assumes_utc_when_naive() -> None:
    """Regression coverage for the same SQLite-doesn't-persist-tzinfo class of bug
    fixed in app/worker/loop.py during Sprint 8."""
    parsed = parse_timestamp("2026-08-03T20:00:00")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 8, 3, 20, 0, 0, tzinfo=UTC)


# --- compute_time_to_detect_seconds ---------------------------------------------


def test_time_to_detect_returns_earliest_matching_result() -> None:
    started_at = "2026-08-03T20:00:00+00:00"
    evaluations = [
        {"result": "PASS", "evaluated_at": "2026-08-03T20:00:05+00:00"},
        {"result": "FAIL", "evaluated_at": "2026-08-03T20:00:30+00:00"},
        {"result": "FAIL", "evaluated_at": "2026-08-03T20:01:00+00:00"},  # later FAIL - ignored
    ]
    result = compute_time_to_detect_seconds(started_at, evaluations, "FAIL")
    assert result == pytest.approx(30.0)


def test_time_to_detect_none_when_no_matching_result() -> None:
    started_at = "2026-08-03T20:00:00+00:00"
    evaluations = [{"result": "PASS", "evaluated_at": "2026-08-03T20:00:05+00:00"}]
    assert compute_time_to_detect_seconds(started_at, evaluations, "FAIL") is None


def test_time_to_detect_empty_evaluations_returns_none() -> None:
    assert compute_time_to_detect_seconds("2026-08-03T20:00:00+00:00", [], "FAIL") is None


# --- compute_time_to_action_seconds ---------------------------------------------


def test_time_to_action_matches_message_substring() -> None:
    started_at = "2026-08-03T20:00:00+00:00"
    events = [
        {
            "message": "PENDING -> DEPLOYING: starting canary rollout",
            "created_at": "2026-08-03T20:00:00+00:00",
        },
        {
            "message": "EVALUATING -> ROLLING_BACK: automatic rollback requested",
            "created_at": "2026-08-03T20:00:40+00:00",
        },
        {
            "message": "ROLLING_BACK -> ROLLED_BACK: traffic rolled back to stable (automatic)",
            "created_at": "2026-08-03T20:00:45+00:00",
        },
    ]
    result = compute_time_to_action_seconds(started_at, events, "rolled back to stable")
    assert result == pytest.approx(45.0)


def test_time_to_action_none_when_no_matching_event() -> None:
    started_at = "2026-08-03T20:00:00+00:00"
    events = [{"message": "canary receiving traffic", "created_at": "2026-08-03T20:00:00+00:00"}]
    assert compute_time_to_action_seconds(started_at, events, "rolled back to stable") is None


# --- report rendering / persistence ---------------------------------------------


def _sample_result(load: LoadTestResult | None = None) -> BenchmarkResult:
    return BenchmarkResult(
        scenario="latency-failure",
        description="Test scenario",
        expected_outcome="rollback",
        observed_outcome="ROLLED_BACK",
        outcome_matches_expectation=True,
        deployment_id="dep-123",
        model_name="benchmark-latency-failure",
        started_at="2026-08-03T20:00:00+00:00",
        final_status="ROLLED_BACK",
        load=load,
        time_to_detect_seconds=12.5,
        time_to_action_seconds=45.0,
        notes=["a note"],
    )


def test_save_json_report_roundtrip(tmp_path: Path) -> None:
    load = LoadTestResult(
        total_requests=1000,
        total_failures=5,
        requests_per_second=99.5,
        error_rate=0.005,
        p50_ms=12.0,
        p95_ms=30.0,
        p99_ms=50.0,
        duration_seconds=60.0,
    )
    result = _sample_result(load)
    path = tmp_path / "report.json"

    save_json_report(result, path)

    loaded = json.loads(path.read_text())
    assert loaded["scenario"] == "latency-failure"
    assert loaded["outcome_matches_expectation"] is True
    assert loaded["load"]["total_requests"] == 1000
    assert loaded["time_to_action_seconds"] == 45.0


def test_render_markdown_report_includes_key_fields() -> None:
    load = LoadTestResult(
        total_requests=500,
        total_failures=0,
        requests_per_second=50.0,
        error_rate=0.0,
        p50_ms=10.0,
        p95_ms=20.0,
        p99_ms=25.0,
        duration_seconds=30.0,
    )
    markdown = render_markdown_report(_sample_result(load))

    assert "# Benchmark: latency-failure" in markdown
    assert "Expected: **rollback**" in markdown
    assert "Observed: **ROLLED_BACK**" in markdown
    assert "✅ yes" in markdown
    assert "Time to detect: 12.5s" in markdown
    assert "Time to action: 45.0s" in markdown
    assert "Total requests: 500" in markdown
    assert "a note" in markdown


def test_render_markdown_report_handles_missing_load_and_timings() -> None:
    result = BenchmarkResult(
        scenario="quality-failure",
        description="desc",
        expected_outcome="inconclusive_freeze",
        observed_outcome="INCONCLUSIVE",
        outcome_matches_expectation=True,
        deployment_id="dep-1",
        model_name="benchmark-quality-failure",
        started_at=None,
        final_status="INCONCLUSIVE",
        load=None,
        time_to_detect_seconds=None,
        time_to_action_seconds=None,
    )
    markdown = render_markdown_report(result)
    assert "N/A (not observed within the wait window)" in markdown
    assert "## Load test" not in markdown
