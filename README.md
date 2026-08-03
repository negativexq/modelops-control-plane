# ModelOps Control Plane

A lightweight ModelOps platform that rolls out new ML model versions via controlled
canary deployments, with policy-based promotion/rollback.

## Stack

- **Backend:** FastAPI + SQLAlchemy + SQLite + Alembic + pytest
- **Frontend:** Next.js + TypeScript + Tailwind + Recharts
- **Model serving:** scikit-learn + joblib (planned)
- **Runtime:** Docker + Docker Compose

Designed to run comfortably on a 16 GB RAM machine; heavy components like Kubernetes,
MLflow, and Prometheus are not part of it yet.

## Structure

```
backend/    FastAPI service
frontend/   Next.js dashboard
```

Separate folders for model serving and router services will be added in future sprints.

## Development

```bash
make dev     # bring up backend + frontend via docker compose
make test    # run backend tests
make lint    # backend (ruff, mypy) and frontend (eslint, tsc) lint/type-check
make down    # stop the services
```

- Backend: http://localhost:8000/health
- Frontend: http://localhost:3000/api/health
