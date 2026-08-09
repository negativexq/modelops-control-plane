"""Optimistic-locking protection against two concurrent writes to the same
Deployment row (see Deployment.version_id in app/control_plane/models.py) -
simulating a human and the automated worker acting on the same deployment at the
same moment, each starting from an independent, request-scoped DB session that
read the row *before* the other one committed.

Uses two separate `Session` objects bound to the same engine (rather than real
threads/asyncio concurrency) so the race is deterministic, not timing-dependent:
each session reads its own copy of the deployment first, then one commits, then
the other attempts to commit against what is now a stale version_id. This is
exactly the sequence a real concurrent request pair produces, just without
relying on OS thread scheduling to land requests in the "bad" order for the test
to mean anything.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.control_plane import service
from app.control_plane.models import Deployment, DeploymentStatus


class FakeRouterGateway:
    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, targets: list[dict[str, Any]]
    ) -> None:
        pass


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def session_factory(db_session: Session) -> sessionmaker[Session]:
    """A sessionmaker bound to the SAME in-memory engine as `db_session` - lets two
    independent Session objects each hold their own identity map / view of the same
    row, exactly like two separate FastAPI requests would (see app/db.py's
    SessionLocal, one per request)."""
    return sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)


@pytest.fixture
def deployment_id(db_session: Session) -> str:
    deployment = Deployment(
        model_name="fraud-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add(deployment)
    db_session.commit()
    return deployment.id


def test_concurrent_promote_and_rollback_only_one_succeeds(
    session_factory: sessionmaker[Session], deployment_id: str
) -> None:
    session_a = session_factory()
    session_b = session_factory()
    try:
        deployment_a = session_a.get(Deployment, deployment_id)
        deployment_b = session_b.get(Deployment, deployment_id)
        assert deployment_a is not None
        assert deployment_b is not None
        assert deployment_a.version_id == deployment_b.version_id  # both read the same version

        # session_a wins the race.
        promoted = run(
            service.promote_deployment(
                session_a, FakeRouterGateway(), deployment_a, triggered_by="manual"
            )
        )
        assert promoted.status == DeploymentStatus.PROMOTED

        # session_b's copy is now stale - its commit must be rejected, not silently
        # overwrite what session_a just did.
        with pytest.raises(service.ConcurrentUpdateError):
            run(
                service.rollback_deployment(
                    session_b, FakeRouterGateway(), deployment_b, triggered_by="automatic"
                )
            )

        # The database reflects session_a's write only.
        final = session_factory().get(Deployment, deployment_id)
        assert final is not None
        assert final.status == DeploymentStatus.PROMOTED
    finally:
        session_a.close()
        session_b.close()


def test_concurrent_advance_traffic_and_promote_only_one_succeeds(
    session_factory: sessionmaker[Session], deployment_id: str
) -> None:
    """advance_traffic's success path only mutates TrafficAllocation, a different
    table - this proves _touch() still makes that race-safe, not just the
    transitions that directly change Deployment.status."""
    session_a = session_factory()
    session_b = session_factory()
    try:
        deployment_a = session_a.get(Deployment, deployment_id)
        deployment_b = session_b.get(Deployment, deployment_id)
        assert deployment_a is not None
        assert deployment_b is not None

        advanced = run(
            service.advance_traffic(session_a, FakeRouterGateway(), deployment_a)
        )
        assert advanced.status == DeploymentStatus.CANARY_RUNNING

        with pytest.raises(service.ConcurrentUpdateError):
            run(
                service.promote_deployment(
                    session_b, FakeRouterGateway(), deployment_b, triggered_by="manual"
                )
            )
    finally:
        session_a.close()
        session_b.close()


def test_second_active_deployment_conflict_is_a_distinct_error_from_concurrency(
    db_session: Session,
) -> None:
    """Sanity check that ActiveDeploymentExistsError (task 9) and
    ConcurrentUpdateError (task 8) are genuinely different exception types, since
    both ultimately surface as 409s at the API layer but mean different things."""
    assert not issubclass(service.ActiveDeploymentExistsError, service.ConcurrentUpdateError)
    assert not issubclass(service.ConcurrentUpdateError, service.ActiveDeploymentExistsError)
