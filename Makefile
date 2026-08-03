.PHONY: dev test lint down backend-install frontend-install

dev:
	docker compose up --build

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
