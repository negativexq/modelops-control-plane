from typing import Any

from scripts.benchmarks.run_benchmark import _release_stale_deployments


class FakeClient:
    def __init__(self, deployments: list[dict[str, Any]]) -> None:
        self.deployments = deployments
        self.rolled_back: list[str] = []

    def list_deployments(self) -> list[dict[str, Any]]:
        return self.deployments

    def rollback(self, deployment_id: str) -> dict[str, Any]:
        self.rolled_back.append(deployment_id)
        return {}


def _d(id_: str, model: str, status: str) -> dict[str, Any]:
    return {"id": id_, "model_name": model, "status": status}


def test_rolls_back_only_the_scenarios_own_live_or_frozen_rollouts() -> None:
    client = FakeClient(
        [
            _d("frozen", "benchmark-quality-failure", "INCONCLUSIVE"),
            _d("killed", "benchmark-quality-failure", "CANARY_RUNNING"),
            _d("done", "benchmark-quality-failure", "ROLLED_BACK"),
            _d("other", "benchmark-success", "INCONCLUSIVE"),
        ]
    )
    released = _release_stale_deployments(client, "benchmark-quality-failure")  # type: ignore[arg-type]
    assert released == ["frozen", "killed"]
    assert client.rolled_back == ["frozen", "killed"]


def test_never_touches_a_non_benchmark_model() -> None:
    client = FakeClient([_d("real", "fraud-detector", "INCONCLUSIVE")])
    assert _release_stale_deployments(client, "fraud-detector") == []  # type: ignore[arg-type]
    assert client.rolled_back == []
