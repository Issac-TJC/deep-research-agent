import asyncio
import base64
import hashlib
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse

from research_agent import __version__
from research_agent.contracts import (
    ConversationCompact,
    ConversationCreate,
    ConversationUpdate,
    CreateRun,
    DigestFeedbackCreate,
    MemoryCreate,
    MemoryUpdate,
    MessageCreate,
    ObservationCreate,
    ObservationUpdate,
    ProfileSignalCreate,
    ProfileSignalUpdate,
    ProjectArtifactCreate,
    ProjectArtifactUpdate,
    ProjectCreate,
    ProjectSearchRequest,
    ProjectUpdate,
    ProjectUrlArtifactCreate,
    ResearchProfileUpdate,
    RunEvent,
    SubscriptionCreate,
    SubscriptionUpdate,
)
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


app = FastAPI(title="Deep Research Agent", version=__version__, lifespan=lifespan)


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


def paginated(items: list[dict], limit: int, cursor: str | None) -> JSONResponse:
    """Keep the historical array body while exposing a stable item-id cursor."""
    start = 0
    if cursor:
        try:
            marker = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        except Exception as exc:
            raise HTTPException(400, "Invalid cursor") from exc
        for index, item in enumerate(items):
            if str(item.get("id")) == marker:
                start = index + 1
                break
        else:
            raise HTTPException(400, "Cursor is no longer available")
    page = items[start : start + limit]
    headers = {}
    if start + limit < len(items) and page:
        headers["X-Next-Cursor"] = base64.urlsafe_b64encode(str(page[-1]["id"]).encode()).decode().rstrip("=")
    return JSONResponse(jsonable_encoder(page), headers=headers)


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except Exception as exc:
        raise HTTPException(400, "Invalid cursor") from exc


def paginated_window(items: list[dict], limit: int) -> JSONResponse:
    page = items[:limit]
    headers = {}
    if len(items) > limit and page:
        headers["X-Next-Cursor"] = base64.urlsafe_b64encode(str(page[-1]["id"]).encode()).decode().rstrip("=")
    return JSONResponse(jsonable_encoder(page), headers=headers)


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
async def health(request: Request):
    from research_agent.settings import RUNTIME_FINGERPRINT

    runtime_settings = request.app.state.service.db.settings
    return {
        "status": "ok",
        "version": __version__,
        "runtime_fingerprint": RUNTIME_FINGERPRINT,
        "research_mode": runtime_settings.research_mode,
        "live_provider_ready": bool(
            runtime_settings.deepseek_api_key and runtime_settings.tavily_api_key
        ),
        "memory_system_version": 2,
        "memory_provider_ready": runtime_settings.research_mode == "fixture" or bool(
            runtime_settings.memory_api_key and runtime_settings.memory_model
        ),
    }


@app.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, "project.create", idempotency_key, payload,
        lambda: service.db.create_project(tenant, payload),
    )
    return result


@app.get("/projects")
async def projects(
    tenant: Tenant,
    service: Service,
    query: str | None = Query(default=None, max_length=200),
    tag: str | None = Query(default=None, max_length=100),
    status: str = Query(default="active", pattern="^(active|archived|deleted_pending)$"),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.projects(tenant, query, tag, status), limit, cursor)


@app.get("/projects/{project_id}")
async def project(project_id: UUID, tenant: Tenant, service: Service):
    return await service.db.project(tenant, str(project_id))


@app.patch("/projects/{project_id}")
async def update_project(project_id: UUID, body: ProjectUpdate, tenant: Tenant, service: Service):
    return await service.db.update_project(
        tenant, str(project_id), body.model_dump(mode="json", exclude_none=True)
    )


@app.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: UUID, tenant: Tenant, service: Service):
    await service.db.delete_project(tenant, str(project_id))
    return Response(status_code=204)


@app.post("/projects/{project_id}/restore")
async def restore_project(project_id: UUID, tenant: Tenant, service: Service):
    return await service.db.restore_project(tenant, str(project_id))


@app.post("/projects/{project_id}/conversations", status_code=201)
async def create_conversation(
    project_id: UUID,
    body: ConversationCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, f"conversation.create:{project_id}", idempotency_key, payload,
        lambda: service.db.create_conversation(tenant, str(project_id), payload),
    )
    return result


@app.get("/projects/{project_id}/conversations")
async def conversations(
    project_id: UUID,
    tenant: Tenant,
    service: Service,
    include_archived: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(
        await service.db.conversations(tenant, str(project_id), include_archived), limit, cursor
    )


@app.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: UUID, body: ConversationUpdate, tenant: Tenant, service: Service
):
    return await service.db.update_conversation(
        tenant, str(conversation_id), body.model_dump(mode="json", exclude_none=True)
    )


@app.get("/conversations/{conversation_id}/messages")
async def messages(
    conversation_id: UUID,
    tenant: Tenant,
    service: Service,
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated_window(
        await service.db.messages(
            tenant,
            str(conversation_id),
            after_sequence,
            limit + 1,
            decode_cursor(cursor),
        ),
        limit,
    )


@app.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: UUID,
    body: MessageCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    return await service.send_message(tenant, str(conversation_id), body, idempotency_key)


@app.get("/conversations/{conversation_id}/snapshot")
async def conversation_snapshot(conversation_id: UUID, tenant: Tenant, service: Service):
    return {"messages": await service.db.messages(tenant, str(conversation_id))}


@app.get("/conversations/{conversation_id}/memory-state")
async def conversation_memory_state(conversation_id: UUID, tenant: Tenant, service: Service):
    return await service.db.conversation_memory_state(tenant, str(conversation_id))


@app.post("/conversations/{conversation_id}/compact", status_code=202)
async def compact_conversation(
    conversation_id: UUID,
    tenant: Tenant,
    service: Service,
    body: ConversationCompact | None = None,
):
    del body
    return await service.compact_conversation(tenant, str(conversation_id))


@app.get("/conversations/{conversation_id}/events")
async def conversation_events(
    conversation_id: UUID,
    tenant: Tenant,
    service: Service,
    request: Request,
    after_sequence: int = Query(0, ge=0),
    last_event_id: Annotated[str | None, Header()] = None,
):
    try:
        cursor = max(after_sequence, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(400, "Invalid Last-Event-ID") from exc
    await service.db.conversation_project(tenant, str(conversation_id))

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            batch = await service.db.conversation_events(tenant, str(conversation_id), cursor)
            for event in batch:
                cursor = event["id"]
                payload = {**event["payload"], "occurred_at": event["occurred_at"]}
                yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(jsonable_encoder(payload), ensure_ascii=False)}\n\n"
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/projects/{project_id}/artifacts", status_code=201)
async def add_project_artifact(
    project_id: UUID,
    body: ProjectArtifactCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, f"artifact.create:{project_id}", idempotency_key, payload,
        lambda: service.db.add_project_artifact(tenant, str(project_id), payload),
    )
    return result


@app.patch("/projects/{project_id}/artifacts/{artifact_id}")
async def update_project_artifact(
    project_id: UUID,
    artifact_id: UUID,
    body: ProjectArtifactUpdate,
    tenant: Tenant,
    service: Service,
):
    return await service.db.update_project_artifact(
        tenant, str(project_id), str(artifact_id), body.model_dump(mode="json", exclude_none=True)
    )


@app.post("/projects/{project_id}/artifacts/from-url", status_code=201)
async def add_project_url_artifact(
    project_id: UUID,
    body: ProjectUrlArtifactCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")

    async def ingest_and_attach():
        source = await EvidenceService(service.db, service.store, tenant).fetch(
            payload["url"], service.db.settings.research_mode
        )
        return await service.db.add_project_artifact(
            tenant,
            str(project_id),
            {
                "source_version_id": source.id,
                "kind": "url",
                "title": payload.get("title") or source.title,
                "tags": payload.get("tags", []),
                "notes": payload.get("notes", ""),
            },
        )

    result, _ = await service.db.execute_idempotent(
        tenant, f"artifact.url:{project_id}", idempotency_key, payload, ingest_and_attach
    )
    return result


@app.get("/projects/{project_id}/artifacts")
async def project_artifacts(
    project_id: UUID,
    tenant: Tenant,
    service: Service,
    include_removed: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(
        await service.db.project_artifacts(tenant, str(project_id), include_removed), limit, cursor
    )


@app.delete("/projects/{project_id}/artifacts/{artifact_id}", status_code=204)
async def remove_project_artifact(project_id: UUID, artifact_id: UUID, tenant: Tenant, service: Service):
    await service.db.remove_project_artifact(tenant, str(project_id), str(artifact_id))
    return Response(status_code=204)


@app.post("/projects/{project_id}/search")
async def project_search(project_id: UUID, body: ProjectSearchRequest, tenant: Tenant, service: Service):
    return await service.project_search(tenant, str(project_id), body)


@app.get("/projects/{project_id}/memories")
async def memories(
    project_id: UUID,
    tenant: Tenant,
    service: Service,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.memories(tenant, str(project_id)), limit, cursor)


@app.post("/projects/{project_id}/memories", status_code=201)
async def create_memory(
    project_id: UUID,
    body: MemoryCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, f"memory.create:{project_id}", idempotency_key, payload,
        lambda: service.create_memory(tenant, str(project_id), payload),
    )
    return result


@app.patch("/projects/{project_id}/memories/{memory_id}")
async def update_memory(
    project_id: UUID,
    memory_id: UUID,
    body: MemoryUpdate,
    tenant: Tenant,
    service: Service,
):
    return await service.update_memory(
        tenant,
        str(project_id),
        str(memory_id),
        body.model_dump(mode="json", exclude_none=True),
    )


@app.get("/projects/{project_id}/observations")
async def observations(
    project_id: UUID,
    tenant: Tenant,
    service: Service,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.observations(tenant, str(project_id)), limit, cursor)


@app.post("/projects/{project_id}/observations", status_code=201)
async def create_observation(
    project_id: UUID,
    body: ObservationCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, f"observation.create:{project_id}", idempotency_key, payload,
        lambda: service.create_observation(tenant, str(project_id), payload),
    )
    return result


@app.patch("/projects/{project_id}/observations/{observation_id}")
async def update_observation(
    project_id: UUID,
    observation_id: UUID,
    body: ObservationUpdate,
    tenant: Tenant,
    service: Service,
):
    return await service.update_observation(
        tenant, str(project_id), str(observation_id),
        body.model_dump(mode="json", exclude_none=True),
    )


@app.get("/memory-jobs/{job_id}")
async def memory_job(job_id: UUID, tenant: Tenant, service: Service):
    return await service.db.memory_job(tenant, str(job_id))


@app.get("/projects/{project_id}/audit-events")
async def audit_events(
    project_id: UUID,
    tenant: Tenant,
    service: Service,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.audit_events(tenant, str(project_id)), limit, cursor)


@app.get("/users/me/research-profile")
async def research_profile(tenant: Tenant, service: Service):
    return await service.db.research_profile(tenant)


@app.get("/users/me/research-profile/export")
async def export_research_profile(tenant: Tenant, service: Service):
    profile = await service.db.research_profile(tenant)
    return JSONResponse(
        jsonable_encoder(profile),
        headers={"Content-Disposition": "attachment; filename=research-profile.json"},
    )


@app.patch("/users/me/research-profile")
async def update_research_profile(body: ResearchProfileUpdate, tenant: Tenant, service: Service):
    return await service.db.update_research_profile(tenant, body.model_dump(mode="json", exclude_none=True))


@app.post("/users/me/research-profile/signals", status_code=201)
async def create_profile_signal(
    body: ProfileSignalCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, "profile-signal.create", idempotency_key, payload,
        lambda: service.db.add_profile_signal(tenant, payload),
    )
    return result


@app.patch("/users/me/research-profile/signals/{signal_id}")
async def update_profile_signal(
    signal_id: UUID, body: ProfileSignalUpdate, tenant: Tenant, service: Service
):
    return await service.db.update_profile_signal(
        tenant, str(signal_id), body.model_dump(mode="json", exclude_none=True)
    )


@app.delete("/users/me/research-profile/signals/{signal_id}", status_code=204)
async def delete_profile_signal(signal_id: UUID, tenant: Tenant, service: Service):
    await service.db.delete_profile_signal(tenant, str(signal_id))
    return Response(status_code=204)


@app.delete("/users/me/research-profile/inferred")
async def clear_inferred_profile(tenant: Tenant, service: Service):
    return {"cleared": await service.db.clear_inferred_profile(tenant)}


@app.post("/projects/{project_id}/subscriptions", status_code=201)
async def create_subscription(
    project_id: UUID,
    body: SubscriptionCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant, f"subscription.create:{project_id}", idempotency_key, payload,
        lambda: service.db.create_subscription(tenant, str(project_id), payload),
    )
    return result


@app.get("/projects/{project_id}/subscriptions")
async def subscriptions(
    project_id: UUID,
    tenant: Tenant,
    service: Service,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.subscriptions(tenant, str(project_id)), limit, cursor)


@app.patch("/subscriptions/{subscription_id}")
async def update_subscription(
    subscription_id: UUID, body: SubscriptionUpdate, tenant: Tenant, service: Service
):
    return await service.db.update_subscription(
        tenant, str(subscription_id), body.model_dump(mode="json", exclude_none=True)
    )


@app.post("/subscriptions/{subscription_id}/preview", status_code=201)
async def preview_subscription(
    subscription_id: UUID,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    result, _ = await service.db.execute_idempotent(
        tenant,
        f"subscription.preview:{subscription_id}",
        idempotency_key,
        {},
        lambda: service.preview_subscription(tenant, str(subscription_id)),
    )
    return result


@app.post("/subscriptions/{subscription_id}/run", status_code=202)
async def run_subscription(
    subscription_id: UUID,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    result, _ = await service.db.execute_idempotent(
        tenant,
        f"subscription.run:{subscription_id}",
        idempotency_key,
        {},
        lambda: service.run_subscription(tenant, str(subscription_id)),
    )
    return result


@app.get("/subscriptions/{subscription_id}/digests")
async def digests(
    subscription_id: UUID,
    tenant: Tenant,
    service: Service,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.digests(tenant, str(subscription_id)), limit, cursor)


@app.get("/digests/{digest_id}")
async def digest_detail(digest_id: UUID, tenant: Tenant, service: Service):
    return await service.db.digest_detail(tenant, str(digest_id))


@app.post("/digests/{digest_id}/publish")
async def publish_digest(digest_id: UUID, tenant: Tenant, service: Service):
    return await service.db.publish_digest(tenant, str(digest_id))


@app.post("/digests/{digest_id}/feedback", status_code=201)
async def digest_feedback(
    digest_id: UUID,
    body: DigestFeedbackCreate,
    tenant: Tenant,
    service: Service,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
):
    payload = body.model_dump(mode="json")
    result, _ = await service.db.execute_idempotent(
        tenant,
        f"digest.feedback:{digest_id}",
        idempotency_key,
        payload,
        lambda: service.db.add_digest_feedback(tenant, str(digest_id), payload),
    )
    return result


@app.get("/notifications")
async def notifications(
    tenant: Tenant,
    service: Service,
    unread_only: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(await service.db.notifications(tenant, unread_only), limit, cursor)


@app.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: UUID, tenant: Tenant, service: Service):
    return await service.db.read_notification(tenant, str(notification_id))


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
async def runs(
    tenant: Tenant,
    service: Service,
    project_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    return paginated(
        await service.db.runs(tenant, str(project_id) if project_id else None), limit, cursor
    )


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
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
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
    async def perform_upload():
        if mime == "application/pdf" and parser_mode != "native":
            pending = await service.start_upload(tenant, raw, mime, filename, parser_mode)
            state = await service.wait_upload(
                tenant, pending.upload_id, service.db.settings.index_wait_seconds
            )
            if state.status == "ready" and state.source_id:
                src = await service.db.get(tenant, state.source_id, "source")
                index = await service.db.index_status(tenant, state.source_id)
                return {"status_code": 201, "upload_id": state.upload_id, "source": src, "index": index}
            return {"status_code": 422 if state.status == "failed" else 202, **state.model_dump(mode="json")}
        source = await EvidenceService(service.db, service.store, tenant).ingest(
            raw, mime, filename, filename=filename, parser_mode=parser_mode
        )
        index = await service.db.index_status(tenant, source.id)
        return {"status_code": 201, "upload_id": source.id, "source": source, "index": index}

    result, _ = await service.db.execute_idempotent(
        tenant,
        "upload.create",
        idempotency_key,
        {"filename": filename, "mime": mime, "parser_mode": parser_mode, "sha256": hashlib.sha256(raw).hexdigest()},
        perform_upload,
    )
    status_code = int(result.pop("status_code"))
    return JSONResponse(jsonable_encoder(result), status_code=status_code)


@app.get("/uploads/{upload_id}")
async def upload_status(upload_id: UUID, tenant: Tenant, service: Service):
    return await service.upload_state(tenant, str(upload_id))


@app.get("/sources/{source_id}")
async def source(source_id: UUID, tenant: Tenant, service: Service):
    await service.db.assert_source_visible(tenant, str(source_id))
    src, document = await EvidenceService(service.db, service.store, tenant).document(str(source_id))
    index = await service.db.index_status(tenant, str(source_id))
    artifacts = await service.db.source_artifacts(tenant, str(source_id))
    return {"source": src, "document": document, "index": index, "artifacts": artifacts}


@app.get("/sources/{source_id}/raw")
async def source_raw(source_id: UUID, tenant: Tenant, service: Service):
    await service.db.assert_source_visible(tenant, str(source_id))
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
    evidence = await service.db.get(tenant, str(span_id), "span")
    await service.db.assert_source_visible(tenant, evidence["source_id"])
    return evidence


@app.get("/evidence-spans/{span_id}/crop")
async def span_crop(span_id: UUID, tenant: Tenant, service: Service):
    evidence = await service.db.get(tenant, str(span_id), "span")
    await service.db.assert_source_visible(tenant, evidence["source_id"])
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
