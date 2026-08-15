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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.control_plane import label_service, metrics_service, service
from app.control_plane.models import Deployment, DeploymentStatus, GroundTruthLabel
from app.control_plane.schemas import MetricIn
from app.db import Base


class FakeRouterGateway:
    """Router-shaped fake: tracks observed (model_name, deployment_id, revision,
    targets) and rejects a same-model push whose revision isn't strictly
    greater, exactly like app/router/main.py's real staleness check (Sprint
    14: model-scoped generation, not per-deployment) - see StaleRevisionError.
    A single instance is meant to be shared across "both sides" of a race in
    these tests, since in reality there's only ever one router.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, list[dict[str, Any]]]] = []
        self.observed_model_name: str | None = None
        self.observed_deployment_id: str | None = None
        self.observed_revision: int = 0
        self.observed_targets: list[dict[str, Any]] | None = None

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, revision: int, targets: list[dict[str, Any]]
    ) -> None:
        from app.control_plane.router_gateway import StaleRevisionError

        same_model = model_name == self.observed_model_name
        if same_model and revision <= self.observed_revision:
            raise StaleRevisionError(f"stale revision {revision} for {deployment_id}")
        self.observed_model_name = model_name
        self.observed_deployment_id = deployment_id
        self.observed_revision = revision
        self.observed_targets = targets
        self.calls.append((model_name, deployment_id, revision, targets))


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
def file_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    """A sessionmaker bound to a *file-based* SQLite database, each Session
    getting its own real, independent connection - unlike `db_session`'s
    `StaticPool` in-memory engine, where every Session bound to it shares the
    exact same physical connection (confirmed empirically: an uncommitted flush
    on one Session is visible to another Session's plain SELECT before the
    first one even commits). That makes StaticPool unable to prove anything
    about a genuine cross-connection race - session_b's own pre-checks would
    "see" session_a's uncommitted writes and behave as if they were already
    serialized, which a real concurrent request pair never gets to assume. Two
    connections against the same file is what an actual pair of concurrent API
    requests looks like.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrency-test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def file_deployment_id(file_session_factory: sessionmaker[Session]) -> str:
    session = file_session_factory()
    try:
        deployment = Deployment(
            model_name="fraud-model",
            stable_version="v1",
            canary_version="v2-good",
            status=DeploymentStatus.CANARY_RUNNING,
        )
        session.add(deployment)
        session.commit()
        return deployment.id
    finally:
        session.close()


def test_concurrent_promote_and_rollback_router_reflects_winner_only(
    file_session_factory: sessionmaker[Session], file_deployment_id: str
) -> None:
    """The sprint's actual reason for existing: with the old push-before-commit
    ordering, a losing writer's router push could land *before* its DB commit
    was rejected by the optimistic lock, leaving the router observing state the
    DB never actually agreed to. With desired state committed first (see
    service.promote_deployment/rollback_deployment), the loser's commit fails
    at the DB layer and its push is never even attempted - so the router's
    final state must reflect the winner's desired state, exactly, using real
    independent DB connections (see file_session_factory) and a single shared
    router fake, since there's only ever one real router.
    """
    router = FakeRouterGateway()
    session_a = file_session_factory()
    session_b = file_session_factory()
    try:
        deployment_a = session_a.get(Deployment, file_deployment_id)
        deployment_b = session_b.get(Deployment, file_deployment_id)
        assert deployment_a is not None
        assert deployment_b is not None
        assert deployment_a.version_id == deployment_b.version_id

        promoted = run(
            service.promote_deployment(session_a, router, deployment_a, triggered_by="manual")
        )
        assert promoted.status == DeploymentStatus.PROMOTED

        with pytest.raises(service.ConcurrentUpdateError):
            run(
                service.rollback_deployment(
                    session_b, router, deployment_b, triggered_by="automatic"
                )
            )

        # The router never even saw the loser's rollback - its commit failed
        # before service._push_best_effort ever ran.
        assert len(router.calls) == 1
        assert router.observed_deployment_id == file_deployment_id
        assert router.observed_targets == [{"version": "v2-good", "weight": 1.0}]

        final = file_session_factory().get(Deployment, file_deployment_id)
        assert final is not None
        assert final.status == DeploymentStatus.PROMOTED
    finally:
        session_a.close()
        session_b.close()


def test_stale_push_is_rejected_and_does_not_change_router_state() -> None:
    """Direct proof of the router-shaped fake's staleness check (mirroring
    app/router/main.py's real one, see StaleRevisionError) - a push for the same
    deployment_id at an equal-or-lower revision than what's already applied is
    rejected outright, and the router's observed state is untouched by it."""
    router = FakeRouterGateway()

    async def _apply() -> None:
        await router.push_traffic_allocation(
            "fraud-model", "dep-1", 2, [{"version": "v2-good", "weight": 1.0}]
        )

    run(_apply())
    assert router.observed_revision == 2

    from app.control_plane.router_gateway import StaleRevisionError

    async def _stale_push(revision: int) -> None:
        await router.push_traffic_allocation(
            "fraud-model", "dep-1", revision, [{"version": "v1", "weight": 1.0}]
        )

    for stale_revision in (1, 2):  # both older and equal must be rejected
        with pytest.raises(StaleRevisionError):
            run(_stale_push(stale_revision))
        # Rejected push must not have mutated observed state.
        assert router.observed_revision == 2
        assert router.observed_targets == [{"version": "v2-good", "weight": 1.0}]

    # A genuinely newer revision is still accepted.
    run(_stale_push(3))
    assert router.observed_revision == 3
    assert router.observed_targets == [{"version": "v1", "weight": 1.0}]


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


# --- Real concurrent ground-truth label + metric writes (Sprint 14) --------------
#
# The bug this section exists to close: the pre-Sprint-14 design (label
# ingestion checking for a matching PredictionMetric, metric ingestion
# checking for a matching PendingLabel - both check-then-act) had a real race.
# Two sequential-arrival-order tests (label-before-metric, metric-before-label)
# aren't a concurrency test - they never let one transaction's check run while
# the other's write is still uncommitted. The test below reproduces that exact
# interleaving, using two genuinely independent file-based connections (see
# file_session_factory above and its docstring on why StaticPool can't prove
# this).


def test_concurrent_label_and_metric_writes_always_end_up_joined(
    file_session_factory: sessionmaker[Session], file_deployment_id: str
) -> None:
    """Reproduces the exact interleaving that broke the old PendingLabel
    design (see label_service.py's module docstring): the label side's own
    existence check runs while the metric side's write is still uncommitted
    and invisible to it, and the label side's own write only completes
    *after* the metric side has already committed - the worst-case ordering
    for any design that tries to link the two together at write time.

    Sprint 14's design has no check-then-act left to race: metrics_service.
    record_metric no longer looks anything up (a plain INSERT), and
    label_service.ingest_label's own SELECT only matters for idempotency, not
    for finding the metric - the two rows are only ever joined later, by a
    read (metrics_service.compute_version_summary). So this must hold
    regardless of interleaving, which is exactly what this test proves rather
    than assumes.
    """
    prediction_id = "concurrent-race-pred-1"
    session_label = file_session_factory()
    session_metric = file_session_factory()
    try:
        # 1. Label side's own "does this already exist" check - nothing
        #    committed from either side yet.
        existing = session_label.execute(
            select(GroundTruthLabel).where(GroundTruthLabel.prediction_id == prediction_id)
        ).scalar_one_or_none()
        assert existing is None

        # 2. Metric side writes and commits *first*, on a completely
        #    independent connection - the label side's session above has
        #    already run its check and can't see this.
        metrics_service.record_metric(
            session_metric,
            file_deployment_id,
            MetricIn(
                model_version="v2-good",
                latency_ms=12.5,
                status_code=200,
                prediction=1,
                prediction_id=prediction_id,
            ),
        )

        # 3. Label side's write completes and commits *after* the metric already
        #    landed - under the old design this exact ordering (check ran
        #    before the metric existed, write happened after) is what left a
        #    PendingLabel row that nothing would ever consume.
        session_label.add(
            GroundTruthLabel(
                prediction_id=prediction_id,
                actual_label=1,
                occurred_at=datetime.now(UTC),
            )
        )
        session_label.commit()
    finally:
        session_label.close()
        session_metric.close()

    verify_session = file_session_factory()
    try:
        summary = metrics_service.compute_version_summary(
            verify_session, file_deployment_id, "v2-good", 3600
        )
    finally:
        verify_session.close()

    assert summary.sample_count == 1
    assert summary.labeled_sample_count == 1
    assert summary.positive_label_count == 1


def test_concurrent_duplicate_label_writes_do_not_both_apply(
    file_session_factory: sessionmaker[Session],
) -> None:
    """Two independent connections racing to be the *first* GroundTruthLabel
    row for the same prediction_id - the unique constraint on prediction_id
    is what actually makes this race-safe (see label_service.ingest_label's
    IntegrityError handling), not the SELECT-then-INSERT ordering, which two
    real connections can't serialize on their own."""
    prediction_id = "concurrent-duplicate-pred-1"
    session_a = file_session_factory()
    session_b = file_session_factory()
    try:
        # Both read "not found" before either has committed anything.
        assert (
            session_a.execute(
                select(GroundTruthLabel).where(GroundTruthLabel.prediction_id == prediction_id)
            ).scalar_one_or_none()
            is None
        )
        assert (
            session_b.execute(
                select(GroundTruthLabel).where(GroundTruthLabel.prediction_id == prediction_id)
            ).scalar_one_or_none()
            is None
        )

        occurred_at = datetime.now(UTC)
        outcome_a = label_service.ingest_label(session_a, prediction_id, 1, occurred_at)
        # session_b's INSERT loses the race against session_a's already-committed
        # row - label_service.ingest_label must catch the IntegrityError and
        # resolve it as a no-op (same value) rather than letting it propagate.
        outcome_b = label_service.ingest_label(session_b, prediction_id, 1, occurred_at)
    finally:
        session_a.close()
        session_b.close()

    assert outcome_a == label_service.LabelIngestOutcome.PENDING
    assert outcome_b == label_service.LabelIngestOutcome.NO_OP

    verify_session = file_session_factory()
    try:
        rows = list(
            verify_session.execute(
                select(GroundTruthLabel).where(GroundTruthLabel.prediction_id == prediction_id)
            )
            .scalars()
            .all()
        )
    finally:
        verify_session.close()
    assert len(rows) == 1
    assert rows[0].actual_label == 1
