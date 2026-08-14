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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


# --- uq_deployments_active_per_model (DB-level backstop for the pre-check) ------
#
# Note on approach: these tests use one shared in-memory SQLite connection (see
# `session_factory` above - StaticPool hands out the *same* physical connection to
# every Session bound to it, confirmed empirically: a flush on session_a is
# visible to a plain SELECT on session_b even before session_a commits). That
# means a literal "start session_b's create before session_a commits" can never
# actually bypass the pre-check in this test setup - session_b's own pre-check
# query would see session_a's uncommitted row too, and reject it right there,
# never reaching the flush()/IntegrityError path at all. To actually exercise
# that second layer (the one that matters for a real concurrent race against two
# genuinely separate DB connections/transactions, e.g. Postgres in production),
# `test_create_deployment_falls_back_to_db_constraint_when_precheck_is_bypassed`
# below deliberately defeats the pre-check via monkeypatching instead of timing -
# the only way to make this deterministic without relying on real inter-process
# concurrency or a file-backed SQLite DB with its own locking quirks.


def test_second_create_for_same_active_model_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    session_a = session_factory()
    session_b = session_factory()
    try:
        first, created_first = run(
            service.create_deployment(
                session_a,
                FakeRouterGateway(),
                model_name="race-model",
                stable_version="v1",
                canary_version="v2-good",
                canary_weight=0.1,
                idempotency_key=None,
            )
        )
        assert created_first
        assert first.status == DeploymentStatus.CANARY_RUNNING

        with pytest.raises(service.ActiveDeploymentExistsError) as exc_info:
            run(
                service.create_deployment(
                    session_b,
                    FakeRouterGateway(),
                    model_name="race-model",
                    stable_version="v1",
                    canary_version="v2-good",
                    canary_weight=0.2,
                    idempotency_key=None,
                )
            )
        assert exc_info.value.existing_deployment_id == first.id

        listing = (
            session_factory()
            .execute(select(Deployment).where(Deployment.model_name == "race-model"))
            .scalars()
            .all()
        )
        assert len(listing) == 1
    finally:
        session_a.close()
        session_b.close()


def test_new_deployment_allowed_once_previous_one_is_terminal(
    session_factory: sessionmaker[Session],
) -> None:
    """The index only blocks *non-terminal* duplicates - PROMOTED/ROLLED_BACK/
    FAILED are explicitly exempt, same as the app-level pre-check."""
    session_a = session_factory()
    try:
        first, _ = run(
            service.create_deployment(
                session_a,
                FakeRouterGateway(),
                model_name="race-model-2",
                stable_version="v1",
                canary_version="v2-good",
                canary_weight=0.1,
                idempotency_key=None,
            )
        )
        run(service.rollback_deployment(session_a, FakeRouterGateway(), first))

        second, created_second = run(
            service.create_deployment(
                session_a,
                FakeRouterGateway(),
                model_name="race-model-2",
                stable_version="v1",
                canary_version="v2-good",
                canary_weight=0.1,
                idempotency_key=None,
            )
        )
        assert created_second
        assert second.id != first.id
        assert second.status == DeploymentStatus.CANARY_RUNNING
    finally:
        session_a.close()


def test_create_deployment_falls_back_to_db_constraint_when_precheck_is_bypassed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense-in-depth: even if the app-level pre-check somehow missed an
    existing active deployment for this model (the real-world case: a genuinely
    concurrent request against a separate DB connection that hadn't committed yet
    when this session's pre-check ran), uq_deployments_active_per_model still
    catches it at flush time, and that IntegrityError is still translated into
    the same ActiveDeploymentExistsError a caller already knows how to handle -
    not an unhandled 500, and not a silently-accepted second active deployment.
    """
    first, _ = run(
        service.create_deployment(
            db_session,
            FakeRouterGateway(),
            model_name="race-model-3",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key=None,
        )
    )
    assert first.status == DeploymentStatus.CANARY_RUNNING

    # Force the pre-check to lie ("no active deployment"), simulating the one
    # window it can't close on its own.
    monkeypatch.setattr(service, "get_active_deployment", lambda db, model_name: None)

    with pytest.raises(service.ActiveDeploymentExistsError):
        run(
            service.create_deployment(
                db_session,
                FakeRouterGateway(),
                model_name="race-model-3",
                stable_version="v1",
                canary_version="v2-good",
                canary_weight=0.2,
                idempotency_key=None,
            )
        )


def test_idempotency_key_collision_is_not_reported_as_active_deployment_conflict(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw UNIQUE-constraint violation on idempotency_key (e.g. a genuine race
    on the *same* idempotency key, past the find_by_idempotency_key pre-check's
    own TOCTOU window) must not be misreported as ActiveDeploymentExistsError -
    it's a different constraint on the same INSERT statement, with a different
    meaning, and swallowing it would hide a real idempotency bug behind the wrong
    error message.
    """
    first, _ = run(
        service.create_deployment(
            db_session,
            FakeRouterGateway(),
            model_name="idem-model-a",
            stable_version="v1",
            canary_version="v2-good",
            canary_weight=0.1,
            idempotency_key="shared-key",
        )
    )
    assert first.status == DeploymentStatus.CANARY_RUNNING

    # Force *both* pre-checks to miss (find_by_idempotency_key AND
    # get_active_deployment), so execution reaches the flush() and the only thing
    # standing in the way is the DB's own idempotency_key unique constraint - a
    # different model_name means uq_deployments_active_per_model can't be what
    # fires here.
    monkeypatch.setattr(service, "find_by_idempotency_key", lambda db, key: None)
    monkeypatch.setattr(service, "get_active_deployment", lambda db, model_name: None)

    with pytest.raises(IntegrityError) as exc_info:
        run(
            service.create_deployment(
                db_session,
                FakeRouterGateway(),
                model_name="idem-model-b",  # different model - not the active-per-model index
                stable_version="v1",
                canary_version="v2-good",
                canary_weight=0.1,
                idempotency_key="shared-key",  # same key as `first`
            )
        )
    assert not isinstance(exc_info.value, service.ActiveDeploymentExistsError)
    assert "idempotency_key" in str(exc_info.value.orig)
