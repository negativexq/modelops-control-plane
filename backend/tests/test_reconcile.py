"""app/control_plane/reconcile.py - closing drift between desired (DB) and
observed (router) state. See docs/DESIGN_NOTES.md#desired-observed-
reconciliation.
"""

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from app.control_plane import service
from app.control_plane.models import Deployment, DeploymentStatus
from app.control_plane.reconcile import reconcile_router_state
from app.control_plane.router_gateway import RouterUpdateError, StaleRevisionError


class FakeRouterGateway:
    """Router-shaped fake with observed state AND a directly-settable "as if the
    router restarted / diverged" knob (see `desync`), plus `should_fail` to
    simulate the router being completely unreachable."""

    def __init__(self, should_fail: bool = False, model_name: str = "fraud-model") -> None:
        # A real router always has *some* model_name (set via its own
        # RouterSettings env var, never None) even before any deployment has
        # ever pushed a config to it - this is that bootstrap value.
        self.should_fail = should_fail
        self.calls: list[tuple[str, str, int, list[dict[str, Any]]]] = []
        self.observed_model_name: str | None = model_name
        self.observed_deployment_id: str | None = None
        self.observed_revision: int = 0
        self.observed_targets: list[dict[str, Any]] | None = None

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, revision: int, targets: list[dict[str, Any]]
    ) -> None:
        if self.should_fail:
            raise RouterUpdateError("simulated router failure")
        # Model-scoped generation (Sprint 14), not per-deployment - see
        # app/router/main.py's put_config.
        same_model = model_name == self.observed_model_name
        if same_model and revision <= self.observed_revision:
            raise StaleRevisionError(f"stale revision {revision} for {deployment_id}")
        self.observed_model_name = model_name
        self.observed_deployment_id = deployment_id
        self.observed_revision = revision
        self.observed_targets = targets
        self.calls.append((model_name, deployment_id, revision, targets))

    async def get_observed_config(self) -> dict[str, Any] | None:
        if self.should_fail:
            return None
        # deployment_id=None is a legitimate observed state (a router that never
        # had, or just lost, a real config still answers GET /router/config with
        # its bootstrap default) - only should_fail means "unreachable".
        return {
            "model_name": self.observed_model_name,
            "deployment_id": self.observed_deployment_id,
            "revision": self.observed_revision,
            "targets": self.observed_targets,
        }

    def desync(self, *, deployment_id: str | None, revision: int) -> None:
        """Directly overwrites observed state without going through
        push_traffic_allocation's staleness check - simulates the router
        restarting and losing its config (deployment_id=None, revision reset)
        or otherwise ending up out of step with the DB, without needing a real
        restart."""
        self.observed_deployment_id = deployment_id
        self.observed_revision = revision


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _create_and_promote(db: Session, router: FakeRouterGateway, model_name: str) -> Deployment:
    deployment, _ = run(
        service.create_deployment(
            db,
            router,
            model_name=model_name,
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    return deployment


def test_reconcile_is_a_noop_when_already_in_sync(db_session: Session) -> None:
    router = FakeRouterGateway()
    _create_and_promote(db_session, router, "sync-model")
    calls_before = len(router.calls)

    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is False
    assert result.reason == "already in sync"
    # No re-push and no event for a no-op reconcile - see reconcile.py's
    # docstring on why silence matters here.
    assert len(router.calls) == calls_before
    deployment = db_session.get(Deployment, router.observed_deployment_id)
    assert deployment is not None
    event_types = [e.event_type for e in deployment.events]
    assert "router_reconciled" not in event_types


def test_reconcile_corrects_drift_and_logs_exactly_one_event(db_session: Session) -> None:
    router = FakeRouterGateway()
    deployment = _create_and_promote(db_session, router, "drift-model")
    real_revision = router.observed_revision

    # Simulate the router losing its config (e.g. a restart) without going
    # through the DB at all - the DB's desired state is unchanged.
    router.desync(deployment_id=None, revision=0)

    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is True
    assert result.deployment_id == deployment.id
    assert result.from_revision == 0
    assert result.to_revision == real_revision
    assert router.observed_deployment_id == deployment.id
    assert router.observed_revision == real_revision

    db_session.refresh(deployment)
    reconcile_events = [e for e in deployment.events if e.event_type == "router_reconciled"]
    assert len(reconcile_events) == 1


def test_reconcile_second_tick_after_correction_is_a_noop(db_session: Session) -> None:
    """Drift gets fixed once; a second tick right after must find things already
    in sync and do nothing further - no double-correction, no duplicate event."""
    router = FakeRouterGateway()
    deployment = _create_and_promote(db_session, router, "drift-model-2")
    router.desync(deployment_id=None, revision=0)

    first = run(reconcile_router_state(db_session, router))
    assert first.reconciled is True

    second = run(reconcile_router_state(db_session, router))
    assert second.reconciled is False
    assert second.reason == "already in sync"

    db_session.refresh(deployment)
    reconcile_events = [e for e in deployment.events if e.event_type == "router_reconciled"]
    assert len(reconcile_events) == 1


def test_reconcile_handles_unreachable_router_without_raising(db_session: Session) -> None:
    router = FakeRouterGateway(should_fail=True)
    _create_and_promote(db_session, FakeRouterGateway(), "unreachable-model")

    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is False
    assert result.reason == "router unreachable"


def test_reconcile_writes_exactly_one_unreachable_event_across_repeated_failures(
    db_session: Session,
) -> None:
    """A sustained outage must not be silent (see
    docs/DESIGN_NOTES.md#desired-observed-reconciliation) but also must not spam
    the timeline - one event when it goes down, not one per failing tick."""
    healthy = FakeRouterGateway()
    deployment = _create_and_promote(db_session, healthy, "flaky-model")

    failing = FakeRouterGateway(should_fail=True)
    for _ in range(3):
        result = run(reconcile_router_state(db_session, failing))
        assert result.reason == "router unreachable"

    db_session.refresh(deployment)
    unreachable_events = [e for e in deployment.events if e.event_type == "router_unreachable"]
    assert len(unreachable_events) == 1


def test_reconcile_writes_exactly_one_recovered_event_after_outage(db_session: Session) -> None:
    router = FakeRouterGateway()
    deployment = _create_and_promote(db_session, router, "recovering-model")

    router.should_fail = True
    run(reconcile_router_state(db_session, router))
    db_session.refresh(deployment)
    assert len([e for e in deployment.events if e.event_type == "router_unreachable"]) == 1

    router.should_fail = False
    for _ in range(3):
        run(reconcile_router_state(db_session, router))

    db_session.refresh(deployment)
    recovered_events = [e for e in deployment.events if e.event_type == "router_recovered"]
    assert len(recovered_events) == 1
    # Still only the one unreachable event from before the recovery.
    assert len([e for e in deployment.events if e.event_type == "router_unreachable"]) == 1


def test_reconcile_noop_when_no_deployment_at_all_for_model(db_session: Session) -> None:
    router = FakeRouterGateway()
    # A non-None deployment_id so get_observed_config() returns a real payload
    # (not "router unreachable") - the model_name it reports simply has no
    # deployment of any kind (active or terminal) in the DB at all.
    router.desync(deployment_id="some-stale-deployment-id", revision=1)
    router.observed_model_name = "no-such-model"

    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is False
    assert result.reason == "no authoritative allocation"


def test_post_commit_push_failure_is_corrected_by_a_later_reconcile_tick(
    db_session: Session,
) -> None:
    """The scenario the sprint exists for: desired state (DB) advances via a
    normal promote/rollback/advance call even though the router push right
    after that commit failed - DB and router are left briefly diverged, and the
    *next* reconcile tick is what actually catches the router up, not the
    original request retrying anything.
    """
    router = FakeRouterGateway(should_fail=True, model_name="post-commit-fail-model")
    deployment, _ = run(
        service.create_deployment(
            db_session,
            router,
            model_name="post-commit-fail-model",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    # Desired state is correct even though the router never got it.
    assert deployment.status == DeploymentStatus.CANARY_RUNNING
    assert deployment.traffic_allocation is not None
    assert deployment.traffic_allocation.revision == 1
    assert router.observed_deployment_id is None  # push never landed

    # Router "comes back" - the next reconcile tick finds and fixes the drift.
    router.should_fail = False
    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is True
    assert router.observed_deployment_id == deployment.id
    assert router.observed_revision == 1


def test_reconcile_recovers_promoted_deployment_after_post_commit_push_failure(
    db_session: Session,
) -> None:
    """Section 1's core scenario: promote_deployment's own commit already made
    the deployment PROMOTED with a 100% canary TrafficAllocation *before* the
    router push happens - a router push failure right after that commit used
    to leave the drift permanent, because the reconciler only ever looked at
    get_active_deployment (CANARY_RUNNING/EVALUATING only), which has nothing
    left to find once a deployment reaches PROMOTED. The reconciler now uses
    get_authoritative_allocation instead, which still finds a PROMOTED
    deployment's final allocation - see
    docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    """
    router = FakeRouterGateway(model_name="promote-recovery-model")
    deployment, _ = run(
        service.create_deployment(
            db_session,
            router,
            model_name="promote-recovery-model",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    assert router.observed_revision == 1  # create's own push landed fine

    router.should_fail = True
    promoted = run(
        service.promote_deployment(db_session, router, deployment, triggered_by="manual")
    )
    assert promoted.status == DeploymentStatus.PROMOTED
    assert promoted.traffic_allocation is not None
    assert promoted.traffic_allocation.targets == [{"version": "v2-good", "weight": 1.0}]

    # The router never got the promote push - still stuck at the original 90/10
    # split from create_deployment.
    assert router.observed_revision == 1
    assert router.observed_targets == [
        {"version": "v1", "weight": 0.9},
        {"version": "v2-good", "weight": 0.1},
    ]

    router.should_fail = False
    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is True
    assert router.observed_deployment_id == deployment.id
    assert router.observed_targets == [{"version": "v2-good", "weight": 1.0}]


def test_reconcile_recovers_rolled_back_deployment_after_post_commit_push_failure(
    db_session: Session,
) -> None:
    """Same scenario as the PROMOTED test above, for ROLLED_BACK - both are
    terminal statuses get_authoritative_allocation must treat as authoritative
    (unlike FAILED - see service._AUTHORITATIVE_TERMINAL_STATUSES)."""
    router = FakeRouterGateway(model_name="rollback-recovery-model")
    deployment, _ = run(
        service.create_deployment(
            db_session,
            router,
            model_name="rollback-recovery-model",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    assert router.observed_revision == 1

    router.should_fail = True
    rolled_back = run(
        service.rollback_deployment(db_session, router, deployment, triggered_by="automatic")
    )
    assert rolled_back.status == DeploymentStatus.ROLLED_BACK
    assert rolled_back.traffic_allocation is not None
    assert rolled_back.traffic_allocation.targets == [{"version": "v1", "weight": 1.0}]

    assert router.observed_revision == 1
    assert router.observed_targets == [
        {"version": "v1", "weight": 0.9},
        {"version": "v2-good", "weight": 0.1},
    ]

    router.should_fail = False
    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is True
    assert router.observed_deployment_id == deployment.id
    assert router.observed_targets == [{"version": "v1", "weight": 1.0}]


# --- INCONCLUSIVE as authoritative routing state ----------------------------------


def test_reconcile_serves_frozen_inconclusive_allocation_not_prior_promoted(
    db_session: Session,
) -> None:
    """The exact scenario this fix closes: D1 is PROMOTED (100% v2-good), D2
    is a later rollout for the same model that gets frozen INCONCLUSIVE at
    75/25 - record_inconclusive's own contract is "freeze the traffic split
    for manual review", not "revert to whatever came before". A reconcile
    tick must keep the router on D2's frozen split, never fall back to D1's
    just because D2 left ACTIVE_STATUSES.
    """
    router = FakeRouterGateway(model_name="inconclusive-model")
    d1, _ = run(
        service.create_deployment(
            db_session,
            router,
            model_name="inconclusive-model",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    run(service.promote_deployment(db_session, router, d1, triggered_by="manual"))
    assert router.observed_targets == [{"version": "v2-good", "weight": 1.0}]

    d2, _ = run(
        service.create_deployment(
            db_session,
            router,
            model_name="inconclusive-model",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.25,
            idempotency_key=None,
        )
    )
    service.record_inconclusive(db_session, d2, max_retries=0)
    db_session.refresh(d2)
    assert d2.status == DeploymentStatus.INCONCLUSIVE
    assert d2.traffic_allocation is not None
    assert d2.traffic_allocation.targets == [
        {"version": "v1", "weight": 0.75},
        {"version": "v2-good", "weight": 0.25},
    ]

    # Simulate the router losing D2's config (e.g. a restart) without
    # touching the DB - the desired state (D2's frozen split) is unchanged.
    router.desync(deployment_id=None, revision=0)

    result = run(reconcile_router_state(db_session, router))

    assert result.reconciled is True
    assert result.deployment_id == d2.id
    assert router.observed_deployment_id == d2.id
    assert router.observed_targets == [
        {"version": "v1", "weight": 0.75},
        {"version": "v2-good", "weight": 0.25},
    ]


def test_reconcile_handles_router_unreachable_when_authoritative_deployment_is_terminal(
    db_session: Session,
) -> None:
    """The router-unreachable branch (GET itself fails) must mark reachability
    on whatever deployment is currently authoritative - including a terminal
    PROMOTED one - not just ACTIVE_STATUSES ones, and only once per outage."""
    healthy = FakeRouterGateway(model_name="terminal-outage-model")
    deployment, _ = run(
        service.create_deployment(
            db_session,
            healthy,
            model_name="terminal-outage-model",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    run(service.promote_deployment(db_session, healthy, deployment, triggered_by="manual"))
    db_session.refresh(deployment)
    assert deployment.status == DeploymentStatus.PROMOTED

    failing = FakeRouterGateway(should_fail=True)
    for _ in range(3):
        result = run(reconcile_router_state(db_session, failing))
        assert result.reason == "router unreachable"

    db_session.refresh(deployment)
    unreachable_events = [e for e in deployment.events if e.event_type == "router_unreachable"]
    assert len(unreachable_events) == 1
