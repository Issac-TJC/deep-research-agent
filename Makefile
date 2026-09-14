.PHONY: setup infra migrate test integration api worker indexer web check
setup:
	uv sync --extra dev --frozen
	cd apps/web && pnpm install --frozen-lockfile
infra:
	docker compose up -d postgres minio
migrate:
	.venv/bin/research migrate
test:
	.venv/bin/pytest -m 'not integration' -q
integration:
	RUN_INTEGRATION=1 .venv/bin/pytest -q
api:
	.venv/bin/uvicorn research_agent.api:app --host 127.0.0.1 --port 18000
worker:
	.venv/bin/research worker
indexer:
	.venv/bin/research indexer
web:
	cd apps/web && pnpm dev
check:
	.venv/bin/ruff check src migrations tests
	cd apps/web && pnpm build
