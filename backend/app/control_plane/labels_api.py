"""Ground-truth label ingestion endpoints - see label_service.py for the actual
matching/idempotency logic this just wraps in HTTP status codes.

| Situation | Response |
|---|---|
| Matching PredictionMetric exists, label seen for the first time | 201 |
| Same prediction_id, same actual_label | 200 (idempotent no-op) |
| Same prediction_id, different actual_label | 409 (+ audit event, if a deployment is known) |
| No matching PredictionMetric yet | 202 (label is durably recorded regardless) |

Both endpoints always return a LabelReportItem (or a list of them) - even the 409
case - rather than FastAPI's default error envelope, so a caller (in particular
POST /batch, which must keep going past an individual conflict) never needs two
different response shapes depending on outcome.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.control_plane import label_service
from app.control_plane.label_service import LabelConflictError, LabelIngestOutcome
from app.control_plane.schemas import LabelIn, LabelReportItem
from app.db import get_db

router = APIRouter(prefix="/api/labels", tags=["labels"])

DbDep = Annotated[Session, Depends(get_db)]

_STATUS_CODE_FOR_OUTCOME: dict[LabelIngestOutcome, int] = {
    LabelIngestOutcome.APPLIED: 201,
    LabelIngestOutcome.NO_OP: 200,
    LabelIngestOutcome.PENDING: 202,
}


def _ingest_one(db: Session, payload: LabelIn) -> tuple[LabelReportItem, int]:
    try:
        outcome = label_service.ingest_label(
            db, payload.prediction_id, payload.actual_label, payload.occurred_at
        )
    except LabelConflictError as exc:
        report = LabelReportItem(
            prediction_id=payload.prediction_id, status="conflict", detail=str(exc)
        )
        return report, 409
    return (
        LabelReportItem(prediction_id=payload.prediction_id, status=outcome.value),
        _STATUS_CODE_FOR_OUTCOME[outcome],
    )


@router.post("", response_model=LabelReportItem)
def ingest_label(payload: LabelIn, response: Response, db: DbDep) -> LabelReportItem:
    report, status_code = _ingest_one(db, payload)
    response.status_code = status_code
    return report


@router.post("/batch", response_model=list[LabelReportItem])
def ingest_labels_batch(payload: list[LabelIn], db: DbDep) -> list[LabelReportItem]:
    """Always 200 overall - each item carries its own status (including
    "conflict"), so a feeder sending a mixed batch gets a full report instead of
    the whole batch aborting on the first problem row."""
    return [_ingest_one(db, item)[0] for item in payload]
