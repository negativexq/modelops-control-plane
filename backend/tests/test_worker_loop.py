import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.worker.client import ActionConflictError
from app.worker.decide import WorkerAction
from app.worker.loop import process_deployment, run_forever, run_once


class FakeWorkerClient:
    """In-memory stand-in for HttpWorkerClient (see app/worker/client.py's
    WorkerClient Protocol) - lets loop.py's logic be tested without a real control
    plane or event loop plumbing beyond plain asyncio.run()."""

    def __init__(self) -> None:
        self.deployments: dict[str, dict[str, Any]] = {}
        self.evaluations: dict[str, list[dict[str, Any]]] = {}
        self.evaluate_result: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.conflict_on: set[tuple[str, str]] = set()

    def _maybe_conflict(self, action: str, deployment_id: str) -> None:
        self.calls.append((action, deployment_id))
        if (action, deployment_id) in self.conflict_on:
            raise ActionConflictError(f"{action} conflict")

    async def list_deployments(self) -> list[dict[str, Any]]:
        return list(self.deployments.values())

    async def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        return self.deployments[deployment_id]

    async def get_policy_evaluations(self, deployment_id: str) -> list[dict[str, Any]]:
        return self.evaluations.get(deployment_id, [])

    async def evaluate(self, deployment_id: str) -> dict[str, Any]:
        self._maybe_conflict("evaluate", deployment_id)
        self.evaluations.setdefault(deployment_id, []).insert(
            0, {"evaluated_at": datetime.now(UTC).isoformat()}
        )
        return {
            "deployment_id": deployment_id,
            "overall_result": self.evaluate_result.get(deployment_id, "PASS"),
            "checks": [],
        }

    async def advance_traffic(self, deployment_id: str) -> dict[str, Any]:
        self._maybe_conflict("advance_traffic", deployment_id)
        return {}

    async def promote(self, deployment_id: str) -> dict[str, Any]:
        self._maybe_conflict("promote", deployment_id)
        return {}

    async def rollback(self, deployment_id: str) -> dict[str, Any]:
        self._maybe_conflict("rollback", deployment_id)
        return {}

    async def record_inconclusive(self, deployment_id: str) -> dict[str, Any]:
        self._maybe_conflict("record_inconclusive", deployment_id)
        return {}


def _deployment(
    deployment_id: str = "dep-1",
    status: str = "CANARY_RUNNING",
    canary_weight: float = 0.1,
    evaluation_window_seconds: int = 300,
    automation_paused: bool = False,
) -> dict[str, Any]:
    return {
        "id": deployment_id,
        "status": status,
        "stable_version": "v1",
        "canary_version": "v2-good",
        "policy_config": {"evaluation_window_seconds": evaluation_window_seconds},
        "automation_paused": automation_paused,
        "traffic_allocation": {
            "targets": [
                {"version": "v1", "weight": 1 - canary_weight},
                {"version": "v2-good", "weight": canary_weight},
            ]
        },
    }


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- action dispatch -----------------------------------------------------------


def test_pass_below_full_traffic_advances() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.1)
    client.evaluate_result["dep-1"] = "PASS"

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action == WorkerAction.ADVANCE_TRAFFIC
    assert ("advance_traffic", "dep-1") in client.calls
    assert ("promote", "dep-1") not in client.calls
    assert ("rollback", "dep-1") not in client.calls


def test_pass_at_full_traffic_promotes() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=1.0)
    client.evaluate_result["dep-1"] = "PASS"

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action == WorkerAction.PROMOTE
    assert ("promote", "dep-1") in client.calls


def test_fail_rolls_back() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.5)
    client.evaluate_result["dep-1"] = "FAIL"

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action == WorkerAction.ROLLBACK
    assert ("rollback", "dep-1") in client.calls
    assert ("advance_traffic", "dep-1") not in client.calls


def test_inconclusive_records_and_does_not_touch_traffic() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.25)
    client.evaluate_result["dep-1"] = "INCONCLUSIVE"

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action == WorkerAction.RECORD_INCONCLUSIVE
    assert ("record_inconclusive", "dep-1") in client.calls
    assert ("advance_traffic", "dep-1") not in client.calls
    assert ("promote", "dep-1") not in client.calls
    assert ("rollback", "dep-1") not in client.calls


# --- non-active deployments are skipped -----------------------------------------


def test_terminal_deployment_is_skipped_without_evaluating() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(status="PROMOTED")

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action is None
    assert ("evaluate", "dep-1") not in client.calls


# --- evaluation-window gating (dedup) -------------------------------------------


def test_skips_evaluation_when_window_has_not_elapsed() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(evaluation_window_seconds=300)
    client.evaluations["dep-1"] = [
        {"evaluated_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat()}
    ]

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action is None
    assert ("evaluate", "dep-1") not in client.calls


def test_evaluates_again_once_window_has_elapsed() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(evaluation_window_seconds=300)
    client.evaluate_result["dep-1"] = "PASS"
    client.evaluations["dep-1"] = [
        {"evaluated_at": (datetime.now(UTC) - timedelta(seconds=400)).isoformat()}
    ]

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action == WorkerAction.ADVANCE_TRAFFIC
    assert ("evaluate", "dep-1") in client.calls


def test_evaluates_immediately_when_no_prior_evaluation_exists() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment()
    client.evaluate_result["dep-1"] = "PASS"

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action == WorkerAction.ADVANCE_TRAFFIC
    assert ("evaluate", "dep-1") in client.calls


def test_handles_naive_timestamp_from_sqlite_round_trip() -> None:
    """SQLite doesn't actually persist tzinfo (DateTime(timezone=True) isn't enforced
    by the driver), so a real control plane can return an offset-naive ISO timestamp
    even though everything is written in UTC. Regression test for a real bug found
    via manual end-to-end verification, not caught by fakes that always produced
    offset-aware timestamps."""
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(evaluation_window_seconds=300)
    naive_recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
    client.evaluations["dep-1"] = [{"evaluated_at": naive_recent.isoformat()}]

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action is None  # still within the window - and, critically, no crash
    assert ("evaluate", "dep-1") not in client.calls


# --- restart-durability: no in-memory state needed ------------------------------


def test_process_deployment_needs_no_prior_call_to_behave_correctly() -> None:
    """Simulates a worker restart: a fresh FakeWorkerClient/process_deployment call
    with no warm-up, reading only what the control plane (here, the fake) already
    has on record - proving there's no in-memory state to lose."""
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.25)
    client.evaluations["dep-1"] = [
        {"evaluated_at": (datetime.now(UTC) - timedelta(seconds=301)).isoformat()}
    ]
    client.evaluate_result["dep-1"] = "PASS"

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))
    assert action == WorkerAction.ADVANCE_TRAFFIC


# --- race conditions: server-side 409s are handled gracefully ------------------


def test_action_conflict_during_evaluate_is_swallowed() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment()
    client.conflict_on.add(("evaluate", "dep-1"))

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    assert action is None


def test_action_conflict_during_action_dispatch_is_swallowed() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.1)
    client.evaluate_result["dep-1"] = "PASS"
    client.conflict_on.add(("advance_traffic", "dep-1"))

    action = run(process_deployment(client, "dep-1", default_window_seconds=300))

    # The evaluate call still happened and returned PASS, but the actual traffic
    # advance was rejected server-side (someone else acted first) - process_deployment
    # must not crash, and must not report an action as having succeeded.
    assert action is None
    assert ("evaluate", "dep-1") in client.calls
    assert ("advance_traffic", "dep-1") in client.calls


# --- run_once sweeps only active deployments ------------------------------------


def test_run_once_only_processes_active_deployments() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-active"] = _deployment(deployment_id="dep-active", canary_weight=0.1)
    client.deployments["dep-promoted"] = _deployment(
        deployment_id="dep-promoted", status="PROMOTED"
    )
    client.evaluate_result["dep-active"] = "PASS"

    run(run_once(client, default_window_seconds=300))

    assert ("evaluate", "dep-active") in client.calls
    assert ("evaluate", "dep-promoted") not in client.calls


def test_run_once_skips_automation_paused_deployment_entirely() -> None:
    """A paused deployment must produce zero worker-originated API calls for
    itself - not just a skipped action - since run_once filters it out of
    active_ids before process_deployment (and therefore /evaluate) is ever
    called. See run_once's docstring for why the "paused" event is written by
    whoever set the flag, not by the worker noticing it here."""
    client = FakeWorkerClient()
    client.deployments["dep-paused"] = _deployment(
        deployment_id="dep-paused", canary_weight=0.1, automation_paused=True
    )
    client.deployments["dep-active"] = _deployment(deployment_id="dep-active", canary_weight=0.1)
    client.evaluate_result["dep-active"] = "PASS"

    run(run_once(client, default_window_seconds=300))

    assert ("evaluate", "dep-paused") not in client.calls
    assert ("advance_traffic", "dep-paused") not in client.calls
    assert ("evaluate", "dep-active") in client.calls


def test_run_once_processes_deployment_normally_once_resumed() -> None:
    """The flip side of the paused test above: once automation_paused goes back
    to False (mirroring what resume_automation does server-side), the worker
    treats the deployment exactly like any other active one - no special-casing
    left over from having been paused."""
    client = FakeWorkerClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.1, automation_paused=False)
    client.evaluate_result["dep-1"] = "PASS"

    run(run_once(client, default_window_seconds=300))

    assert ("evaluate", "dep-1") in client.calls
    assert ("advance_traffic", "dep-1") in client.calls


def test_run_once_continues_after_one_deployment_errors() -> None:
    client = FakeWorkerClient()
    client.deployments["dep-broken"] = _deployment(deployment_id="dep-broken")
    client.deployments["dep-ok"] = _deployment(deployment_id="dep-ok", canary_weight=0.1)
    client.evaluate_result["dep-ok"] = "PASS"
    # Force an unexpected error (not ActionConflictError) for dep-broken by removing
    # it from the deployments dict right before get_deployment is awaited - simulate
    # via a KeyError from a deliberately mismatched id.
    del client.deployments["dep-broken"]
    client.deployments["dep-broken-placeholder"] = _deployment(deployment_id="dep-broken")

    run(run_once(client, default_window_seconds=300))

    # dep-ok must still have been processed despite dep-broken raising.
    assert ("evaluate", "dep-ok") in client.calls


# --- run_forever survives a sweep-level failure ---------------------------------


def test_run_forever_survives_list_deployments_failure() -> None:
    """Regression test: a control-plane outage during list_deployments() itself
    (not a single deployment's processing) used to propagate out of run_once and
    kill the whole worker process. Found via manual end-to-end verification (the
    worker crashed on startup racing a backend that hadn't finished migrating yet)
    - a FakeWorkerClient always resolves cleanly, so no test had exercised this
    failure mode before."""

    class FailingListClient(FakeWorkerClient):
        def __init__(self) -> None:
            super().__init__()
            self.list_calls = 0

        async def list_deployments(self) -> list[dict[str, Any]]:
            self.list_calls += 1
            if self.list_calls == 1:
                raise RuntimeError("control plane unavailable")
            return await super().list_deployments()

    client = FailingListClient()
    client.deployments["dep-1"] = _deployment(canary_weight=0.1)
    client.evaluate_result["dep-1"] = "PASS"

    async def run_two_cycles() -> None:
        task = asyncio.ensure_future(
            run_forever(client, poll_interval_seconds=0, default_window_seconds=300)
        )
        # Let the loop run a couple of iterations, then cancel it - run_forever has
        # no natural exit point (it's designed to run for the container's lifetime).
        for _ in range(20):
            await asyncio.sleep(0)
            if client.list_calls >= 2 and ("evaluate", "dep-1") in client.calls:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(run_two_cycles())

    assert client.list_calls >= 2  # first call failed, loop kept going and retried
    assert ("evaluate", "dep-1") in client.calls  # and successfully processed after
