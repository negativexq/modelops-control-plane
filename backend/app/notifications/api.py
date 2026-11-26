"""GET /api/notifications/deliveries: what was sent where, and what wasn't."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import WebhookDelivery, WebhookDeliveryStatus
from app.db import get_db

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

DbDep = Annotated[Session, Depends(get_db)]


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    deployment_id: str
    event: str
    url: str
    status: WebhookDeliveryStatus
    attempts: int
    last_status_code: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None
    next_attempt_at: datetime | None
    payload: dict[str, Any]


@router.get("/deliveries", response_model=list[WebhookDeliveryOut])
def list_deliveries(
    db: DbDep,
    deployment_id: str | None = None,
    status: WebhookDeliveryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[WebhookDelivery]:
    query = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)
    if deployment_id is not None:
        query = query.where(WebhookDelivery.deployment_id == deployment_id)
    if status is not None:
        query = query.where(WebhookDelivery.status == status)
    return list(db.scalars(query).all())
