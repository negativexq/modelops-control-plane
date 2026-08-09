.PHONY: dev test lint down backend-install frontend-install \
	generate-data train-models evaluate-models prepare-models migrate \
	benchmark-baseline benchmark-latency-failure benchmark-error-failure \
	benchmark-quality-failure benchmark-success benchmark-all ci-smoke-test coverage

dev:
	docker compose up --build

migrate: backend-install
	cd backend && .venv/bin/alembic upgrade head

down:
	docker compose down

backend-install:
	cd backend && python3.12 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e ".[dev]"

frontend-install:
	cd frontend && npm install

test: backend-install
	cd backend && .venv/bin/pytest -q

# HTML report at backend/htmlcov/index.html; terminal summary printed either way.
coverage: backend-install
	cd backend && .venv/bin/pytest -q --cov=app --cov-report=term-missing --cov-report=html

lint: backend-install frontend-install
	cd backend && .venv/bin/ruff check . && .venv/bin/mypy app
	cd frontend && npm run lint && npm run type-check

# Runs against an already-up stack (`make dev` in another terminal, or CI's
# "integration" job which brings the stack up itself) - see
# backend/scripts/ci_smoke_test.py for what it actually checks and why.
ci-smoke-test: backend-install
	cd backend && .venv/bin/python -m scripts.ci_smoke_test

generate-data: backend-install
	cd backend && .venv/bin/python scripts/generate_dataset.py

train-models: backend-install
	cd backend && .venv/bin/python -m scripts.train_models

evaluate-models: backend-install
	cd backend && .venv/bin/python -m scripts.evaluate_models

prepare-models: generate-data train-models evaluate-models

# Benchmarks assume backend, router, worker, and the model-serving-* services are
# already up (`make dev`, or `docker compose up backend router worker
# model-serving-v1 model-serving-v2-good model-serving-v2-quality-bad`). Each one
# runs alone in its own isolated model_name - see backend/scripts/benchmarks/ - but
# they share the router's single active traffic split, so run them one at a time,
# not concurrently with each other or with a real demo you care about.
benchmark-baseline: backend-install
	cd backend && .venv/bin/python -m scripts.benchmarks.run_benchmark --scenario baseline

benchmark-latency-failure: backend-install
	cd backend && .venv/bin/python -m scripts.benchmarks.run_benchmark --scenario latency-failure

benchmark-error-failure: backend-install
	cd backend && .venv/bin/python -m scripts.benchmarks.run_benchmark --scenario error-failure

benchmark-quality-failure: backend-install
	cd backend && .venv/bin/python -m scripts.benchmarks.run_benchmark --scenario quality-failure

benchmark-success: backend-install
	cd backend && .venv/bin/python -m scripts.benchmarks.run_benchmark --scenario success

benchmark-all: benchmark-baseline benchmark-latency-failure benchmark-error-failure \
	benchmark-quality-failure benchmark-success
