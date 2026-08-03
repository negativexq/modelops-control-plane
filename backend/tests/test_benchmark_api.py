import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.benchmarks import service
from app.db import get_db
from app.main import app


class FakeProcess:
    """Stands in for asyncio.subprocess.Process - communicate() resolves
    immediately (or when release() is called, for tests that need a run to stay
    RUNNING for a bit) instead of actually spawning `python -m scripts...`."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"ok") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._release = asyncio.Event()
        self._release.set()  # resolves immediately by default

    async def communicate(self) -> tuple[bytes, None]:
        await self._release.wait()
        return self._stdout, None

    def hold(self) -> None:
        self._release.clear()

    def release(self) -> None:
        self._release.set()


@pytest.fixture
def db_session_factory(db_session: Session) -> sessionmaker[Session]:
    """A sessionmaker bound to the SAME in-memory engine as `db_session` - lets
    service._monitor_run's own `SessionLocal()` call (it can't reuse the
    request-scoped session from Depends(get_db), since that's long closed by the
    time a benchmark subprocess finishes) see the same test database."""
    return sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False)


@pytest.fixture
def client(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(service, "SessionLocal", db_session_factory)
    monkeypatch.setattr(service, "RESULTS_DIR", tmp_path)

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, process: FakeProcess) -> None:
    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)


def _wait_until(predicate: object, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(interval)
    return False


# --- GET /api/benchmarks/scenarios ----------------------------------------------


def test_get_scenarios_returns_all_five_with_disclaimers(client: TestClient) -> None:
    response = client.get("/api/benchmarks/scenarios")
    assert response.status_code == 200
    body = response.json()
    keys = {s["key"] for s in body}
    assert keys == {"baseline", "latency-failure", "error-failure", "quality-failure", "success"}

    by_key = {s["key"]: s for s in body}
    assert by_key["baseline"]["synthetic_disclaimer"] is None
    assert by_key["latency-failure"]["synthetic_disclaimer"] is None
    assert by_key["quality-failure"]["synthetic_disclaimer"] is not None
    assert "demo" in by_key["quality-failure"]["synthetic_disclaimer"].lower()
    assert by_key["success"]["synthetic_disclaimer"] is not None
    assert "synthetic" in by_key["success"]["synthetic_disclaimer"].lower()


# --- POST /api/benchmarks/run ----------------------------------------------------


def test_run_benchmark_starts_and_returns_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_subprocess(monkeypatch, FakeProcess())

    response = client.post("/api/benchmarks/run", json={"scenario": "baseline"})

    assert response.status_code == 202
    body = response.json()
    assert body["scenario"] == "baseline"
    assert body["status"] == "RUNNING"
    assert body["completed_at"] is None


def test_run_benchmark_unknown_scenario_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_subprocess(monkeypatch, FakeProcess())
    response = client.post("/api/benchmarks/run", json={"scenario": "does-not-exist"})
    assert response.status_code == 404


def test_run_benchmark_already_running_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_process = FakeProcess()
    held_process.hold()
    _patch_subprocess(monkeypatch, held_process)

    first = client.post("/api/benchmarks/run", json={"scenario": "baseline"})
    assert first.status_code == 202

    second = client.post("/api/benchmarks/run", json={"scenario": "success"})
    assert second.status_code == 409
    assert first.json()["id"] in second.json()["detail"]

    held_process.release()


# --- GET /api/benchmarks/current -------------------------------------------------


def test_get_current_returns_none_when_nothing_running(client: TestClient) -> None:
    response = client.get("/api/benchmarks/current")
    assert response.status_code == 200
    assert response.json() is None


def test_get_current_returns_the_running_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_process = FakeProcess()
    held_process.hold()
    _patch_subprocess(monkeypatch, held_process)

    started = client.post("/api/benchmarks/run", json={"scenario": "baseline"}).json()

    current = client.get("/api/benchmarks/current")
    assert current.status_code == 200
    assert current.json()["id"] == started["id"]
    assert current.json()["status"] == "RUNNING"

    held_process.release()


# --- run completion (background task) --------------------------------------------


def test_completed_run_picks_up_json_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_subprocess(monkeypatch, FakeProcess(returncode=0))
    (tmp_path / "baseline.json").write_text(json.dumps({"scenario": "baseline", "ok": True}))

    run_id = client.post("/api/benchmarks/run", json={"scenario": "baseline"}).json()["id"]

    def _is_completed() -> bool:
        return client.get(f"/api/benchmarks/{run_id}").json()["status"] != "RUNNING"

    assert _wait_until(_is_completed), "run never left RUNNING status"

    body = client.get(f"/api/benchmarks/{run_id}").json()
    assert body["status"] == "COMPLETED"
    assert body["completed_at"] is not None
    assert body["result"] == {"scenario": "baseline", "ok": True}


def test_completed_run_without_report_file_still_completes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_subprocess(monkeypatch, FakeProcess(returncode=0))
    run_id = client.post("/api/benchmarks/run", json={"scenario": "baseline"}).json()["id"]

    def _is_completed() -> bool:
        return client.get(f"/api/benchmarks/{run_id}").json()["status"] != "RUNNING"

    assert _wait_until(_is_completed)
    body = client.get(f"/api/benchmarks/{run_id}").json()
    assert body["status"] == "COMPLETED"
    assert body["result"] is None


def test_failed_subprocess_marks_run_failed_with_output(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_subprocess(
        monkeypatch, FakeProcess(returncode=1, stdout=b"traceback: something broke")
    )
    run_id = client.post("/api/benchmarks/run", json={"scenario": "baseline"}).json()["id"]

    def _is_failed() -> bool:
        return client.get(f"/api/benchmarks/{run_id}").json()["status"] == "FAILED"

    assert _wait_until(_is_failed), "run never transitioned to FAILED"

    body = client.get(f"/api/benchmarks/{run_id}").json()
    assert body["status"] == "FAILED"
    assert "something broke" in body["error_message"]


def test_run_finishing_frees_up_a_new_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_subprocess(monkeypatch, FakeProcess(returncode=0))
    first_id = client.post("/api/benchmarks/run", json={"scenario": "baseline"}).json()["id"]

    def _first_done() -> bool:
        return client.get(f"/api/benchmarks/{first_id}").json()["status"] != "RUNNING"

    assert _wait_until(_first_done)

    second = client.post("/api/benchmarks/run", json={"scenario": "success"})
    assert second.status_code == 202


# --- GET /api/benchmarks/{run_id} and listing -------------------------------------


def test_get_run_not_found_404(client: TestClient) -> None:
    response = client.get("/api/benchmarks/does-not-exist")
    assert response.status_code == 404


def test_list_runs_orders_newest_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_subprocess(monkeypatch, FakeProcess(returncode=0))
    first_id = client.post("/api/benchmarks/run", json={"scenario": "baseline"}).json()["id"]

    def _first_done() -> bool:
        return client.get(f"/api/benchmarks/{first_id}").json()["status"] != "RUNNING"

    assert _wait_until(_first_done)

    second_id = client.post("/api/benchmarks/run", json={"scenario": "success"}).json()["id"]

    listing = client.get("/api/benchmarks").json()
    ids = [r["id"] for r in listing]
    assert ids.index(second_id) < ids.index(first_id)
