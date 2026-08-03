"""Display metadata for the dashboard's Benchmarks page. Reuses the single source of
truth in scripts/benchmarks/scenarios.py (now shipped inside the backend image - see
Dockerfile's `COPY scripts ./scripts`) rather than duplicating scenario descriptions
here, so the CLI (`make benchmark-*`) and the dashboard can never describe a scenario
differently.
"""

from typing import Any

from scripts.benchmarks.scenarios import SCENARIOS

SCENARIO_TITLES: dict[str, str] = {
    "baseline": "Baseline",
    "latency-failure": "Latency Regression",
    "error-failure": "Error Rate Regression",
    "quality-failure": "Quality Regression",
    "success": "Successful Promotion",
}


def list_scenario_info() -> list[dict[str, Any]]:
    return [
        {
            "key": scenario.key,
            "title": SCENARIO_TITLES.get(scenario.key, scenario.key),
            "description": scenario.description,
            "expected_outcome": scenario.expected_outcome,
            "synthetic_disclaimer": scenario.synthetic_disclaimer,
        }
        for scenario in SCENARIOS.values()
    ]
