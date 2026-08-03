from datetime import datetime
from typing import Any

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


class BenchmarkRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    scenario: str
    status: BenchmarkRunStatus
    started_at: datetime
    completed_at: datetime | None
    result: dict[str, Any] | None
    error_message: str | None
