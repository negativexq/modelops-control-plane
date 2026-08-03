from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.benchmarks import service
from app.benchmarks.scenarios_info import list_scenario_info
from app.benchmarks.schemas import BenchmarkRunOut, RunBenchmarkRequest, ScenarioInfoOut
from app.control_plane.models import BenchmarkRun
from app.db import get_db

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])

DbDep = Annotated[Session, Depends(get_db)]


@router.get("/scenarios", response_model=list[ScenarioInfoOut])
def get_scenarios() -> list[dict[str, object]]:
    return list_scenario_info()


@router.post("/run", response_model=BenchmarkRunOut, status_code=status.HTTP_202_ACCEPTED)
async def run_benchmark(payload: RunBenchmarkRequest, db: DbDep) -> BenchmarkRun:
    """Starts a benchmark scenario as a background subprocess and returns
    immediately with status=RUNNING - poll GET /api/benchmarks/current or
    GET /api/benchmarks/{run_id} for progress. 409 if one is already in flight: the
    router has a single active traffic split, so two concurrent runs would silently
    fight over it rather than fail loudly.
    """
    try:
        return await service.start_benchmark_run(
            db,
            payload.scenario,
            duration_seconds=payload.duration_seconds,
            max_wait_seconds=payload.max_wait_seconds,
        )
    except service.UnknownScenarioError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.BenchmarkAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/current", response_model=BenchmarkRunOut | None)
def get_current(db: DbDep) -> BenchmarkRun | None:
    return service.get_running_run(db)


@router.get("", response_model=list[BenchmarkRunOut])
def list_runs(db: DbDep) -> list[BenchmarkRun]:
    return service.list_runs(db)


@router.get("/{run_id}", response_model=BenchmarkRunOut)
def get_run(run_id: str, db: DbDep) -> BenchmarkRun:
    run = service.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Benchmark run '{run_id}' not found")
    return run
