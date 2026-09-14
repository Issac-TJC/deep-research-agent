import asyncio
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse

from research_agent.contracts import CreateRun, RunEvent
from research_agent.db import Conflict, Database, NotFound
from research_agent.evidence import EvidenceService
from research_agent.service import ResearchService
from research_agent.settings import settings
from research_agent.storage import ObjectStore


@asynccontextmanager
async def lifespan(app):
    db = Database(settings())
    await db.open()
    store = ObjectStore(settings())
    await store.setup()
    app.state.service = ResearchService(db, store)
    yield
    await db.close()


app = FastAPI(title="Deep Research Agent", version="0.2.0", lifespan=lifespan)


def service(request: Request) -> ResearchService:
    return request.app.state.service


async def tenant(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "API key required")
    result = await service(request).db.auth(authorization[7:])
    if not result:
        raise HTTPException(401, "Invalid API key")
    return result


Tenant = Annotated[str, Depends(tenant)]
Service = Annotated[ResearchService, Depends(service)]


@app.exception_handler(NotFound)
async def not_found(request, exc):
    return JSONResponse({"detail": "Resource not found"}, status_code=404)


@app.exception_handler(Conflict)
async def conflict(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse({"detail": "Invalid request or source: " + str(exc)[:200]}, status_code=422)


@app.get("/health")
async def health():
    from research_agent.settings import RUNTIME_FINGERPRINT

    return {"status": "ok", "version": "0.2.0", "runtime_fingerprint": RUNTIME_FINGERPRINT}


@app.post("/research-runs", status_code=202)
async def create(
    body: CreateRun,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    run, created = await service.create(tenant, body, idempotency_key)
    return {"run_id": str(run["id"]), "created": created, "status": run["status"]}


@app.get("/research-runs")
async def runs(tenant: Tenant, service: Service):
    return await service.db.runs(tenant)


@app.get("/research-runs/{run_id}")
async def run(run_id: UUID, tenant: Tenant, service: Service):
    result = await service.db.run(tenant, str(run_id))
    result["tasks"] = await service.db.records(tenant, str(run_id), "task")
    result["sources"] = [
        {
            **record["source"],
            "index": await service.db.index_status(tenant, record["source"]["id"]),
        }
        for record in await service.db.records(tenant, str(run_id), "source_ref")
    ]
    result["reviews"] = await service.db.records(tenant, str(run_id), "review")
    result["contexts"] = await service.db.records(tenant, str(run_id), "context")
    result["warnings"] = await service.db.records(tenant, str(run_id), "warning")
    intents = await service.db.records(tenant, str(run_id), "intent")
    result["intent"] = intents[-1] if intents else None
    return result


@app.post("/research-runs/{run_id}/cancel", status_code=202)
async def cancel(run_id: UUID, tenant: Tenant, service: Service):
    await service.db.cancel(tenant, str(run_id))
    return {"status": "cancellation_recorded"}


@app.post("/research-runs/{run_id}/resume", status_code=202)
async def resume(run_id: UUID, tenant: Tenant, service: Service):
    await service.db.resume(tenant, str(run_id))
    return {"status": "resume_recorded"}


@app.get("/research-runs/{run_id}/usage")
async def usage(run_id: UUID, tenant: Tenant, service: Service):
    await service.db.run(tenant, str(run_id))
    return await service.db.usage(tenant, str(run_id))


@app.get("/research-runs/{run_id}/usage-summary")
async def usage_summary(run_id: UUID, tenant: Tenant, service: Service):
    return await service.db.usage_summary(tenant, str(run_id))


@app.get("/research-runs/{run_id}/events")
async def events(
    run_id: UUID,
    tenant: Tenant,
    service: Service,
    request: Request,
    after_seq: int = Query(0, ge=0),
    last_event_id: Annotated[str | None, Header()] = None,
):
    run_id = str(run_id)
    await service.db.run(tenant, run_id)
    try:
        cursor = max(after_seq, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(400, "Invalid Last-Event-ID") from exc

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            batch = await service.db.events(tenant, run_id, cursor)
            for event in batch:
                cursor = event["seq"]
                model = RunEvent(
                    tenant_id=tenant,
                    run_id=run_id,
                    seq=cursor,
                    event_type=event["event_type"],
                    occurred_at=event["occurred_at"],
                    payload=event["payload"],
                    trace_id=run_id,
                    task_id=event["payload"].get("task_id"),
                    attempt_id=event["payload"].get("id"),
                )
                yield f"id: {cursor}\ndata: {model.model_dump_json()}\n\n"
            current = await service.db.run(tenant, run_id)
            if (
                current["status"] in {"completed", "cancelled", "failed", "interrupted"}
                and cursor >= current["seq"]
            ):
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/uploads")
async def upload(
    tenant: Tenant,
    service: Service,
    file: UploadFile = File(),
    parser_mode: str = Query("auto", pattern="^(native|auto|enhanced)$"),
):
    filename = os.path.basename(file.filename or "upload")[:200]
    mime = file.content_type or ""
    if filename.lower().endswith(".md"):
        mime = "text/markdown"
    if mime not in {"text/html", "text/plain", "text/markdown", "application/pdf"}:
        raise HTTPException(415, "Supported: HTML, PDF, Markdown, plain text")
    raw = await file.read(service.db.settings.max_upload_bytes + 1)
    if len(raw) > service.db.settings.max_upload_bytes:
        raise HTTPException(413, "Upload too large")
    if mime == "application/pdf" and parser_mode != "native":
        pending = await service.start_upload(tenant, raw, mime, filename, parser_mode)
        state = await service.wait_upload(
            tenant, pending.upload_id, service.db.settings.index_wait_seconds
        )
        if state.status == "ready" and state.source_id:
            src = await service.db.get(tenant, state.source_id, "source")
            index = await service.db.index_status(tenant, state.source_id)
            return JSONResponse(
                jsonable_encoder({"upload_id": state.upload_id, "source": src, "index": index}),
                status_code=201,
            )
        if state.status == "failed":
            return JSONResponse(jsonable_encoder(state), status_code=422)
        return JSONResponse(jsonable_encoder(state), status_code=202)
    source = await EvidenceService(service.db, service.store, tenant).ingest(
        raw, mime, filename, filename=filename, parser_mode=parser_mode
    )
    index = await service.db.index_status(tenant, source.id)
    return JSONResponse(
        jsonable_encoder({"upload_id": source.id, "source": source, "index": index}),
        status_code=201,
    )


@app.get("/uploads/{upload_id}")
async def upload_status(upload_id: UUID, tenant: Tenant, service: Service):
    return await service.upload_state(tenant, str(upload_id))


@app.get("/sources/{source_id}")
async def source(source_id: UUID, tenant: Tenant, service: Service):
    src, document = await EvidenceService(service.db, service.store, tenant).document(str(source_id))
    index = await service.db.index_status(tenant, str(source_id))
    artifacts = await service.db.source_artifacts(tenant, str(source_id))
    return {"source": src, "document": document, "index": index, "artifacts": artifacts}


@app.get("/sources/{source_id}/raw")
async def source_raw(source_id: UUID, tenant: Tenant, service: Service):
    source = await service.db.get(tenant, str(source_id), "source")
    raw = await service.store.get(tenant, source["raw_key"])
    # Never render arbitrary uploaded HTML in the application origin.
    return Response(
        raw,
        media_type=source["mime"],
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@app.get("/evidence-spans/{span_id}")
async def span(span_id: UUID, tenant: Tenant, service: Service):
    return await service.db.get(tenant, str(span_id), "span")


@app.get("/evidence-spans/{span_id}/crop")
async def span_crop(span_id: UUID, tenant: Tenant, service: Service):
    evidence = await service.db.get(tenant, str(span_id), "span")
    if not evidence.get("crop_key"):
        raise NotFound("visual crop unavailable")
    raw = await service.store.get(tenant, evidence["crop_key"])
    return Response(
        raw,
        media_type="image/png",
        headers={"Cache-Control": "private, immutable", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/research-runs/{run_id}/report")
async def report(
    run_id: UUID,
    tenant: Tenant,
    service: Service,
    revision: int | None = None,
    format: str = Query("json", pattern="^(json|markdown)$"),
):
    result = await service.report(tenant, str(run_id), revision)
    if format == "markdown":
        return Response(
            result.markdown,
            media_type="text/markdown",
            headers={"Content-Disposition": "attachment; filename=research.md"},
        )
    return JSONResponse(jsonable_encoder(result))
