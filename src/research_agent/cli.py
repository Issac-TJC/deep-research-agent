import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
import typer
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from research_agent.contracts import CreateRun, SourceVersion, uid
from research_agent.db import Database
from research_agent.schema import CHECKPOINT_TABLES, policy_sql
from research_agent.service import ResearchService
from research_agent.settings import settings
from research_agent.storage import ObjectStore

app = typer.Typer(no_args_is_help=True)


@app.command()
def migrate():
    """Apply domain migrations and initialize the fenced PostgreSQL checkpointer schema."""
    s = settings()
    command.upgrade(Config("alembic.ini"), "head")

    async def setup():
        async with await psycopg.AsyncConnection.connect(s.admin_database_url, autocommit=True) as conn:
            await AsyncPostgresSaver(conn).setup()
            for table in CHECKPOINT_TABLES:
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id uuid DEFAULT nullif(current_setting('app.tenant_id',true),'')::uuid"
                )
                existing = await (
                    await conn.execute(
                        "SELECT 1 FROM pg_policies WHERE tablename=%s AND policyname='tenant_isolation'",
                        (table,),
                    )
                ).fetchone()
                if not existing:
                    await conn.execute(policy_sql(table), prepare=False)
                await conn.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO research_app")
            await conn.execute("REVOKE ALL ON checkpoint_migrations FROM research_app")

    asyncio.run(setup())
    typer.echo("Migrations applied; runtime uses a separate non-BYPASSRLS role.")


@app.command()
def seed():
    """Generate two local test API keys. Keys are printed once; database stores only hashes."""
    with psycopg.connect(settings().admin_database_url) as conn:
        for name in ["test-tenant-a", "test-tenant-b"]:
            tenant = str(uuid5(NAMESPACE_URL, "research-agent:" + name))
            key = "ra_" + secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO tenants(id,name) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING", (tenant, name)
            )
            conn.execute(
                "INSERT INTO api_keys(key_hash,tenant_id) VALUES (%s,%s)",
                (hashlib.sha256(key.encode()).hexdigest(), tenant),
            )
            typer.echo(json.dumps({"name": name, "tenant_id": tenant, "api_key": key}))


@app.command()
def worker(once: bool = False):
    from research_agent.worker import work

    asyncio.run(work(settings(), once))


@app.command()
def indexer(once: bool = False):
    """Run the asynchronous parser and document index worker."""
    from research_agent.indexer import work

    asyncio.run(work(settings(), once))


@app.command()
def reindex(all_sources: bool = typer.Option(False, "--all")):
    """Queue additive index builds without invalidating the last usable version."""
    if not all_sources:
        raise typer.BadParameter("Pass --all to confirm reindexing every source")
    s = settings()

    async def run():
        async with await psycopg.AsyncConnection.connect(s.admin_database_url) as admin:
            rows = await (
                await admin.execute(
                    "SELECT tenant_id,data FROM records WHERE kind='source' ORDER BY created_at"
                )
            ).fetchall()
        db = Database(s)
        await db.open()
        try:
            from research_agent.retrieval import CHUNKER_VERSION

            for tenant_id, data in rows:
                source = SourceVersion.model_validate(data)
                await db.requeue_index(
                    str(tenant_id),
                    source.id,
                    source.parsed_hash,
                    uid(),
                    CHUNKER_VERSION,
                    s.embedding_model,
                    s.embedding_revision,
                )
        finally:
            await db.close()
        typer.echo(f"Queued {len(rows)} source index builds")

    asyncio.run(run())


@app.command()
def submit(brief_file: Path, api_key: str = typer.Option(envvar="RESEARCH_API_KEY")):
    async def run():
        db = Database(settings())
        await db.open()
        try:
            tenant = await db.auth(api_key)
            if not tenant:
                raise typer.BadParameter("Invalid API key")
            request = CreateRun.model_validate_json(brief_file.read_text())
            created, _ = await ResearchService(db, ObjectStore(settings())).create(
                tenant, request, secrets.token_hex(16)
            )
            typer.echo(str(created["id"]))
        finally:
            await db.close()

    asyncio.run(run())


@app.command()
def evaluate(
    split: str = "dev",
    variant: str = "B2",
    limit: int = 1,
    repeats: int = 1,
    live: bool = False,
    api_key: str = typer.Option(envvar="RESEARCH_API_KEY"),
    output: Path = Path("artifacts/evaluation.jsonl"),
    concurrency: int = 3,
    context: str = "evidence",
    corpus_manifest: Path | None = None,
):
    from research_agent.evaluation import evaluate_suite

    asyncio.run(
        evaluate_suite(
            settings(),
            api_key,
            split,
            variant,
            limit,
            repeats,
            live,
            output,
            concurrency,
            context,
            corpus_manifest,
        )
    )


@app.command("retrieval-benchmark")
def retrieval_benchmark(
    dataset: Path,
    api_key: str = typer.Option(envvar="RESEARCH_API_KEY"),
    enforce: bool = False,
):
    """Evaluate lexical, dense and hybrid Recall@8 on a frozen annotated corpus."""
    from pydantic import TypeAdapter

    from research_agent.evidence import EvidenceService
    from research_agent.retrieval_benchmark import RetrievalCase, run_benchmark

    cases = TypeAdapter(list[RetrievalCase]).validate_json(dataset.read_text(encoding="utf-8"))

    async def run():
        s = settings()
        db, store = Database(s), ObjectStore(s)
        await db.open()
        await store.setup()
        try:
            tenant = await db.auth(api_key)
            if not tenant:
                raise typer.BadParameter("Invalid API key")
            result = await run_benchmark(EvidenceService(db, store, tenant), cases)
            typer.echo(json.dumps(result, indent=2))
            if enforce and not result["quality_gate"]["passed"]:
                raise typer.Exit(1)
        finally:
            await db.close()

    asyncio.run(run())


@app.command()
def freeze(
    urls_file: Path,
    output: Path = Path("artifacts/corpus.json"),
    api_key: str = typer.Option(envvar="RESEARCH_API_KEY"),
):
    """Freeze public source snapshots before a controlled model experiment; no LLM calls."""
    from research_agent.evaluation import freeze_corpus

    urls = [line.strip() for line in urls_file.read_text().splitlines() if line.strip()]
    if len(urls) > 20:
        raise typer.BadParameter("A corpus may contain at most 20 seed URLs")
    asyncio.run(freeze_corpus(settings(), api_key, urls, output))


@app.command()
def doctor():
    """Read-only environment checks; does not make paid provider calls."""
    s = settings()
    typer.echo(
        json.dumps(
            {
                "mode": s.research_mode,
                "deepseek_key_configured": bool(s.deepseek_api_key),
                "tavily_key_configured": bool(s.tavily_api_key),
                "model": s.deepseek_model,
                "python_environment": str(Path(__import__("sys").prefix)),
                "live_campaign_cap_usd": s.live_campaign_usd,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
