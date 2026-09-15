import asyncio
import logging

from research_agent.db import BudgetExceeded, Database, StaleLease
from research_agent.graph import ResearchEngine
from research_agent.memory import MemoryJobRunner
from research_agent.providers import ProviderError
from research_agent.settings import Settings
from research_agent.storage import ObjectStore

logger = logging.getLogger("research.worker")


async def execute_claim(db: Database, store: ObjectStore, claim: dict):
    tenant, run_id, fence = claim["tenant"], claim["run_id"], claim["token"]
    run = await db.run(tenant, run_id)
    from research_agent.settings import RUNTIME_FINGERPRINT

    expected = run.get("configuration", {}).get("runtime_fingerprint")
    if expected and expected != RUNTIME_FINGERPRINT:
        await db.finish(tenant, run_id, fence, "interrupted", "runtime_version_mismatch")
        return
    engine = ResearchEngine(db, store, tenant, run, fence)
    await engine.phase("recovering" if fence > 1 else "starting", fence=fence)
    if fence > 1:
        async with db.tx(tenant) as conn:
            await db.guard(conn, run_id, fence)
            pending = await (
                await conn.execute(
                    """UPDATE actions SET status='unknown',error='worker_interrupted',
                finished_at=now() WHERE run_id=%s AND status='reserved' RETURNING id""",
                    (run_id,),
                )
            ).fetchall()
            for action in pending:
                await db.event(
                    conn,
                    tenant,
                    run_id,
                    "action.unknown",
                    {"id": action["id"], "reason": "worker_interrupted"},
                )
    execution = asyncio.create_task(engine.execute())

    async def heartbeat():
        while True:
            await asyncio.sleep(db.settings.heartbeat_seconds)
            try:
                await db.heartbeat(tenant, run_id, fence)
            except StaleLease:
                execution.cancel()
                raise

    heart = asyncio.create_task(heartbeat())
    try:
        result = await execution
        reports = await db.records(tenant, run_id, "report")
        quality = max(reports, key=lambda p: p["revision"])["quality_status"] if reports else "unchecked"
        await db.finish(tenant, run_id, fence, "completed", result.get("stop_reason", "completed"), quality)
    except BudgetExceeded as exc:
        await engine.partial("budget:" + str(exc))
        await db.finish(tenant, run_id, fence, "completed", "budget:" + str(exc), "needs_review")
    except ProviderError as exc:
        # Exhausted logical actions cannot gain new retries by repeatedly resuming the run.
        status = "interrupted" if exc.retryable else "failed"
        try:
            await engine.partial("provider:" + str(exc))
            await db.finish(tenant, run_id, fence, status, str(exc), "needs_review")
        except StaleLease:
            pass
    except (StaleLease, asyncio.CancelledError):
        # No late writes. Cancellation state is already persistent; a crash lease can be reclaimed.
        pass
    except Exception as exc:
        logger.error("run failed run_id=%s error_type=%s", run_id, type(exc).__name__)
        try:
            await db.finish(tenant, run_id, fence, "interrupted", type(exc).__name__)
        except StaleLease:
            pass
        raise
    finally:
        heart.cancel()
        await asyncio.gather(heart, return_exceptions=True)


async def work(settings: Settings, once=False):
    db, store = Database(settings), ObjectStore(settings)
    await db.open()
    await store.setup()
    try:
        while True:
            claim = await db.claim()
            if claim:
                await execute_claim(db, store, claim)
            else:
                memory_claim = await db.claim_memory_job()
                if memory_claim:
                    await MemoryJobRunner(db).execute(
                        memory_claim["tenant"], memory_claim["job_id"]
                    )
                elif once:
                    break
                else:
                    await asyncio.sleep(1)
            if once:
                break
    finally:
        await db.close()
