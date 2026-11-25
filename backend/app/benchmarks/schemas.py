from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.control_plane.models import BenchmarkRunStatus


class ScenarioInfoOut(BaseModel):
    key: str
    title: str
    description: str
    expected_outcome: str
    synthetic_disclaimer: str | None


class RunBenchmarkRequest(BaseModel):
    scenario: str
    # Optional overrides of the scenario's own defaults - mainly useful for a quick
    # manual smoke test from the dashboard; leave unset to use what
    # scripts/benchmarks/scenarios.py defines for the scenario.
    duration_seconds: int | None = None
    max_wait_seconds: int | None = None


class LoadTestResultOut(BaseModel):
    """Mirrors scripts/benchmarks/report.py's LoadTestResult (what the benchmark
    subprocess writes); declared here so the API schema is exact."""

    total_requests: int
    total_failures: int
    requests_per_second: float
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    duration_seconds: float


class BenchmarkResultOut(BaseModel):
    """Mirrors scripts/benchmarks/report.py's BenchmarkResult."""

    # Always present in responses, so the API schema marks it required there.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    scenario: str
    description: str
    expected_outcome: str
    observed_outcome: str
    outcome_matches_expectation: bool
    deployment_id: str
    model_name: str
    started_at: str | None
    final_status: str
    load: LoadTestResultOut | None
    time_to_detect_seconds: float | None
    time_to_action_seconds: float | None
    notes: list[str] = []
    run_at: str


class BenchmarkRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scenario: str
    status: BenchmarkRunStatus
    started_at: datetime
    completed_at: datetime | None
    result: BenchmarkResultOut | None
    error_message: str | None
