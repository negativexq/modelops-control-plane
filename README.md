# ModelOps Control Plane

A lightweight ModelOps platform that rolls out new ML model versions via controlled
canary deployments, with policy-based promotion/rollback and a benchmark suite to
exercise the whole loop end to end.

Designed to run comfortably on a 16 GB RAM machine; heavy components like
Kubernetes, MLflow, and Prometheus are deliberately not part of it.

## Contents

- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Project layout](#project-layout)
- [Components](#components)
- [Development](#development)
- [Known limitations](#known-limitations)

## Architecture

| Layer | Tech |
|---|---|
| Backend / control plane | FastAPI + SQLAlchemy + SQLite + Alembic + pytest |
| Frontend / dashboard | Next.js + TypeScript + Tailwind + Recharts |
| Model serving | scikit-learn + joblib, one FastAPI process per model version |
| Load generation | Locust (benchmark suite) |
| Runtime | Docker + Docker Compose |

### Services & ports

| Service | Port | What it is |
|---|---|---|
| `backend` | 8000 | Control plane: deployments, policy, metrics, benchmarks API |
| `frontend` | 3000 | Dashboard (Next.js) |
| `router` | 8080 | Weighted traffic router between stable/canary |
| `model-serving-v1` | 8001 | Stable fraud model |
| `model-serving-v2-good` | 8002 | Healthy canary candidate |
| `model-serving-v2-quality-bad` | 8003 | Deliberately weak model, for the quality-regression scenario |
| `model-serving-v2-good-latency-fault` | 8004 | Same artifact as v2-good, runtime-toggleable latency fault |
| `model-serving-v2-good-error-fault` | 8005 | Same artifact as v2-good, runtime-toggleable error fault |
| `worker` | *(internal only)* | Automated promotion/rollback loop, no HTTP surface |

The control plane is the single source of truth for traffic allocation - the
router's config is just a cache, pushed by the control plane on every change. See
[docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) for the reasoning behind this and every
other cross-service boundary in the system.

## Quickstart

```bash
make prepare-models   # synthetic dataset + train v1/v2-good/v2-quality-bad + evaluate
make migrate          # apply Alembic migrations
make dev              # docker compose up --build (all services above)
```

Then check:

- Backend: http://localhost:8000/health
- Frontend: http://localhost:3000
- Router: http://localhost:8080/router/health

## Project layout

```
backend/
  app/
    control_plane/    deployment lifecycle, traffic allocation, metrics API
    policy/            PASS/FAIL/INCONCLUSIVE evaluation engine
    worker/            automated promotion/rollback loop (separate process)
    router/            weighted traffic router
    serving/           per-version model serving app (+ runtime fault injection)
    benchmarks/        dashboard-triggered benchmark runs API
  scripts/
    benchmarks/        CLI benchmark orchestrator + Locust load definition
    train_models.py, evaluate_models.py, generate_dataset.py
  alembic/             DB migrations
  tests/
frontend/
  src/app/             Next.js pages (Overview, Models, Deployments, Benchmarks)
  src/lib/             typed API client, fetch/mutation hooks, formatting
docs/
  DESIGN_NOTES.md      the "why" behind each design decision
```

## Components

Each ran as its own sprint; the tag is there for history, not because the numbering
matters day to day. Every section links to its deep dive in
[docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) for the reasoning behind the choices
below - this file only covers what exists and how to use it.

### Model registry (Sprint 1)

```bash
make prepare-models   # generate-data + train-models + evaluate-models
```

- `GET /api/models`, `GET /api/models/{name}/versions`,
  `GET /api/models/{name}/versions/{version}[/evaluation]`

### Model serving (Sprint 2)

One process per model version (`app.serving.main:app`), selected via
`MODEL_NAME`/`MODEL_VERSION`.

- `GET /health`, `GET /ready`, `POST /predict`
- `GET`/`PUT /fault-injection` — runtime-toggleable `latency_ms`/`error_rate`
  (`403` in production); this is what the benchmark suite uses to simulate a
  regression without restarting a container.

### Traffic router (Sprint 3)

```bash
curl -X POST localhost:8080/router/predict -d '{...}' -H 'content-type: application/json'
```

- `POST /router/predict` — weighted-random pick, forwards the body unmodified,
  tags the response with `routed_to`.
- `GET`/`PUT /router/config` — read/replace `{model_name, targets: [{version, weight}]}`.
- `GET /router/health` — router liveness + each target's live `/ready` status.
- A target that's unhealthy or unreachable returns **503**, never a silent
  fallback to another target ([why](docs/DESIGN_NOTES.md#traffic-router)).

### Control plane & deployment lifecycle (Sprint 4)

```bash
curl -X POST localhost:8000/api/deployments -H 'Idempotency-Key: <uuid>' -d '{...}'
```

- `POST /api/deployments`, `GET /api/deployments[/{id}]`,
  `POST /api/deployments/{id}/promote`, `POST /api/deployments/{id}/rollback`.
- State machine (invalid transitions return 409):

  ```
  PENDING -> DEPLOYING -> CANARY_RUNNING -> EVALUATING -> PROMOTING -> PROMOTED
                                                        -> ROLLING_BACK -> ROLLED_BACK
                                                        -> INCONCLUSIVE -> (promote/rollback)
  any in-flight state -> FAILED
  ```

- Every transition is recorded as a `DeploymentEvent` (the audit trail).

### Metrics (Sprint 5)

- `POST /api/deployments/{id}/metrics` — the router calls this after every forward.
- `GET /api/deployments/{id}/metrics?window_seconds=300` — p50/p95/p99 latency,
  error rate, plus precision/recall/false-positive-rate (`null` until something
  backfills `actual_label` - see [Known limitations](#known-limitations)).
- `GET /api/deployments/{id}/comparison?window_seconds=300` — same, `canary - stable`.

### Dashboard (Sprint 6)

Next.js Client Components calling the control plane directly
(`NEXT_PUBLIC_API_URL`, CORS via `MODELOPS_CORS_ALLOW_ORIGINS`).

- **Overview** (`/`), **Models** (`/models`), **Deployments** (`/deployments`,
  `/deployments/[id]` with Canary Analysis charts), **Benchmarks** (`/benchmarks`).

### Policy engine (Sprint 7)

```bash
curl -X POST localhost:8000/api/deployments/<id>/evaluate
curl localhost:8000/api/deployments/<id>/policy-evaluations
```

- Four checks (`minimum_requests`, `latency_p95_increase`, `max_error_rate`,
  `minimum_recall`), each persisted as its own `PolicyEvaluation` row.
- Overall verdict: **FAIL beats INCONCLUSIVE beats PASS**
  ([why](docs/DESIGN_NOTES.md#policy-engine)).
- Records what the policies found - a human still calls `/promote`/`/rollback`.

### Automated promotion & rollback (Sprint 8)

```bash
docker compose up backend router model-serving-v1 model-serving-v2-good worker
```

`app/worker/` is a separate, stateless process that acts on `/evaluate` results
through the control plane's own REST API - the same endpoints a human would call.

- **PASS** → advance traffic (`10% → 25% → 50% → 100%`) or promote at 100%.
- **FAIL** → rollback.
- **INCONCLUSIVE** → retry up to `max_inconclusive_retries`, then freeze (a human
  can still promote/rollback a frozen deployment).
- `triggered_by=manual|automatic` distinguishes human vs. worker actions in the
  event log.

### Benchmark suite (Sprint 9)

```bash
make benchmark-baseline        # or: benchmark-latency-failure, benchmark-error-failure,
                                #     benchmark-quality-failure, benchmark-success
make benchmark-all             # all five, sequentially
```

Drives five repeatable end-to-end scenarios against a running stack using Locust,
each in its own isolated `model_name`. **Only one benchmark runs at a time** - the
router holds a single active traffic split.

- Two scenarios use the fault-injection containers (`model-serving-v2-good-*-fault`)
  via the runtime `PUT /fault-injection` endpoint, always reset in a `finally`
  block even on crash.
- `quality-failure` and `success` use deliberately loosened thresholds (and, for
  `success`, synthetic ground-truth backfill) to demonstrate the automation paths
  honestly - see [Known limitations](#known-limitations).
- Each run writes `backend/benchmark-results/{scenario}.{json,md}` with
  time-to-detect / time-to-action measurements.

**From the dashboard**: the same five scenarios can be started and watched live
from `/benchmarks` (`POST /api/benchmarks/run`, polls `GET
/api/benchmarks/current`) instead of the CLI - useful for a live demo. `POST /run`
returns **409** while another run is active, and the UI disables every scenario's
button proactively rather than just reacting to that.

## Development

```bash
make dev     # bring up the whole stack via docker compose
make test    # backend tests (pytest)
make lint    # backend (ruff, mypy) + frontend (eslint, tsc) lint/type-check
make down    # stop the services
```

## Known limitations

There is no real `actual_label` (ground-truth) source anywhere in the platform.
This means:

- The `minimum_recall` policy check is always `INCONCLUSIVE`, never `FAIL`.
- Since `INCONCLUSIVE` beats `PASS`, **no deployment can be automatically promoted**
  past its first traffic stage without some source of labels.
- The `benchmark-success` scenario backfills synthetic `(prediction, actual_label)`
  pairs to still exercise the automatic-promotion path end to end - this is a
  simulated stand-in for a delayed label feed, not real platform behavior, and is
  called out explicitly wherever it applies (the scenario's own report, and its
  card on the dashboard).
