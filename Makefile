.PHONY: dev test lint down backend-install frontend-install \
	generate-data train-models evaluate-models prepare-models migrate

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

lint: backend-install frontend-install
	cd backend && .venv/bin/ruff check . && .venv/bin/mypy app
	cd frontend && npm run lint && npm run type-check

generate-data: backend-install
	cd backend && .venv/bin/python scripts/generate_dataset.py

train-models: backend-install
	cd backend && .venv/bin/python -m scripts.train_models

evaluate-models: backend-install
	cd backend && .venv/bin/python -m scripts.evaluate_models

prepare-models: generate-data train-models evaluate-models
