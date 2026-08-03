import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    # Python-side, microsecond-resolution default. SQLite's CURRENT_TIMESTAMP (the
    # server_default below) is only second-resolution, which made otherwise-correct
    # "ORDER BY created_at DESC" ties break in insertion order instead of the other
    # way around for rows created within the same second.
    return datetime.now(UTC)


class DeploymentStatus(enum.StrEnum):
    PENDING = "PENDING"
    DEPLOYING = "DEPLOYING"
    CANARY_RUNNING = "CANARY_RUNNING"
    EVALUATING = "EVALUATING"
    PROMOTING = "PROMOTING"
    PROMOTED = "PROMOTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


TERMINAL_STATUSES = frozenset(
    {
        DeploymentStatus.PROMOTED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.FAILED,
    }
)


def _new_id() -> str:
    return str(uuid.uuid4())


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    stable_version: Mapped[str] = mapped_column(String(100), nullable=False)
    canary_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus, native_enum=False, length=32),
        nullable=False,
        default=DeploymentStatus.PENDING,
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200), unique=True, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    traffic_allocation: Mapped["TrafficAllocation | None"] = relationship(
        back_populates="deployment", uselist=False, cascade="all, delete-orphan"
    )
    events: Mapped[list["DeploymentEvent"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        order_by="DeploymentEvent.created_at",
    )


class TrafficAllocation(Base):
    __tablename__ = "traffic_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id"), unique=True, nullable=False
    )
    # [{"version": "v1", "weight": 0.9}, {"version": "v2-good", "weight": 0.1}, ...]
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=_utcnow
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="traffic_allocation")


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="events")


class PredictionMetric(Base):
    """One row per /router/predict forward. Written on the hot path (router fires one
    of these after every forward) - deliberately has no relationship back to
    Deployment and no join is needed to insert one; reads (aggregation) filter by
    deployment_id + model_version + a time window instead.
    """

    __tablename__ = "prediction_metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id"), nullable=False, index=True
    )
    model_version: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    prediction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_label: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, index=True
    )
