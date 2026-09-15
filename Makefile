.PHONY: setup infra migrate test integration integration-isolated performance api worker indexer web check
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
integration-isolated:
	docker compose -f compose.test.yaml up -d --wait
	DATABASE_URL=postgresql://research_app:research_local@localhost:25432/research ADMIN_DATABASE_URL=postgresql://postgres:postgres_local@localhost:25432/research S3_ENDPOINT=http://localhost:29000 .venv/bin/research migrate
	@status=0; DATABASE_URL=postgresql://research_app:research_local@localhost:25432/research ADMIN_DATABASE_URL=postgresql://postgres:postgres_local@localhost:25432/research S3_ENDPOINT=http://localhost:29000 RESEARCH_MODE=fixture RUN_INTEGRATION=1 .venv/bin/pytest -q || status=$$?; docker compose -f compose.test.yaml down -v; exit $$status
performance:
	.venv/bin/python scripts/benchmark_workspace.py
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
