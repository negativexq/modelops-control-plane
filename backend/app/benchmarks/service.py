import asyncio
import json
import logging
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.benchmarks.config import BACKEND_DIR, RESULTS_DIR, benchmark_api_settings
from app.control_plane.models import BenchmarkRun, BenchmarkRunStatus
from app.db import SessionLocal
from scripts.benchmarks.scenarios import SCENARIOS

logger = logging.getLogger("benchmarks_api")

# Strong references to in-flight monitor tasks, purely so asyncio doesn't warn about
# (or garbage-collect) a pending task with no other referrer - same pattern as the
# router's fire-and-forget metric tasks (Sprint 5/8). Nothing here ever awaits them.
_background_tasks: set[asyncio.Task[None]] = set()


class UnknownScenarioError(Exception):
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        super().__init__(f"unknown benchmark scenario '{scenario}'")


class BenchmarkAlreadyRunningError(Exception):
    """Only one benchmark can run at a time - the router has a single active traffic
    slot (see README's "Benchmark suite" section), so a second concurrent run would
    silently steal the first one's traffic split rather than fail loudly."""

    def __init__(self, running_run_id: str) -> None:
        self.running_run_id = running_run_id
        super().__init__(f"a benchmark run ({running_run_id}) is already in progress")


def get_running_run(db: Session) -> BenchmarkRun | None:
    stmt = select(BenchmarkRun).where(BenchmarkRun.status == BenchmarkRunStatus.RUNNING)
    return db.execute(stmt).scalars().first()


def get_run(db: Session, run_id: str) -> BenchmarkRun | None:
    return db.get(BenchmarkRun, run_id)


def list_runs(db: Session) -> list[BenchmarkRun]:
    stmt = select(BenchmarkRun).order_by(BenchmarkRun.started_at.desc())
    return list(db.execute(stmt).scalars().all())


async def _monitor_run(
    run_id: str, process: asyncio.subprocess.Process, scenario_key: str
) -> None:
    """Awaits the benchmark subprocess and records its outcome. Runs as a
    fire-and-forget asyncio task (see start_benchmark_run) - uses its own DB session
    since the request-scoped one from Depends(get_db) is closed long before this
    finishes; a benchmark run can take minutes.
    """
    stdout, _ = await process.communicate()
    db = SessionLocal()
    try:
        run = db.get(BenchmarkRun, run_id)
        if run is None:
            logger.warning(
                "benchmark run %s vanished before completion could be recorded", run_id
            )
            return

        run.completed_at = datetime.now(UTC)
        if process.returncode == 0:
            run.status = BenchmarkRunStatus.COMPLETED
            report_path = RESULTS_DIR / f"{scenario_key}.json"
            if report_path.exists():
                try:
                    run.result = json.loads(report_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("could not read report for run %s: %s", run_id, exc)
        else:
            run.status = BenchmarkRunStatus.FAILED
            tail = stdout.decode(errors="replace")[-4000:] if stdout else "(no output captured)"
            run.error_message = tail
        db.commit()
    finally:
        db.close()


async def start_benchmark_run(
    db: Session,
    scenario_key: str,
    *,
    duration_seconds: int | None = None,
    max_wait_seconds: int | None = None,
) -> BenchmarkRun:
    if scenario_key not in SCENARIOS:
        raise UnknownScenarioError(scenario_key)

    existing = get_running_run(db)
    if existing is not None:
        raise BenchmarkAlreadyRunningError(existing.id)

    run = BenchmarkRun(scenario=scenario_key, status=BenchmarkRunStatus.RUNNING)
    db.add(run)
    db.commit()
    db.refresh(run)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "scripts.benchmarks.run_benchmark",
        "--scenario",
        scenario_key,
        "--router-url",
        benchmark_api_settings.router_url,
        "--control-plane-url",
        benchmark_api_settings.control_plane_url,
        "--users",
        str(benchmark_api_settings.default_users),
        "--target-rps",
        str(benchmark_api_settings.default_target_rps),
        "--output-dir",
        str(RESULTS_DIR),
    ]
    if duration_seconds is not None:
        cmd += ["--duration-seconds", str(duration_seconds)]
    if max_wait_seconds is not None:
        cmd += ["--max-wait-seconds", str(max_wait_seconds)]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=BACKEND_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Fire-and-forget, same pattern as the router's metric emission (Sprint 5): the
    # request returns immediately with status=RUNNING, and this task updates the row
    # once the subprocess (which can run for minutes) actually finishes.
    task = asyncio.create_task(_monitor_run(run.id, process, scenario_key))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return run
