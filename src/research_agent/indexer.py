"""Asynchronous source parsing and evidence-preserving document indexing."""

from __future__ import annotations

import asyncio
import logging

import httpx

from research_agent.db import Database
from research_agent.evidence import EvidenceService
from research_agent.retrieval import EmbeddingClient, chunk_document
from research_agent.settings import Settings
from research_agent.storage import ObjectStore

logger = logging.getLogger("research.indexer")


async def embed_with_backoff(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """Retry only transient service failures; total delay is bounded to seven seconds."""
    for attempt, delay in enumerate((1, 2, 4)):
        try:
            return await client.embed(texts)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {429, 502, 503, 504} or attempt == 2:
                raise
        except (httpx.ConnectError, httpx.ReadTimeout):
            if attempt == 2:
                raise
        await asyncio.sleep(delay)
    raise RuntimeError("embedding_retry_exhausted")


async def keep_lease(callback, seconds: int):
    try:
        while True:
            await asyncio.sleep(min(30, max(1, seconds // 3)))
            await callback()
    except asyncio.CancelledError:
        return


async def process_upload(db: Database, store: ObjectStore, claimed: dict):
    tenant, upload_id = str(claimed["tenant"]), claimed["upload_id"]
    row = await db.upload_status(tenant, upload_id)
    heartbeat = asyncio.create_task(
        keep_lease(
            lambda: db.heartbeat_upload(tenant, upload_id), db.settings.index_lease_seconds
        )
    )
    try:
        raw = await store.get(tenant, row["raw_key"])
        source = await EvidenceService(db, store, tenant).ingest(
            raw,
            row["mime"],
            row["title"],
            filename=row["title"],
            parser_mode=row["parser_mode"],
        )
        await db.complete_upload(tenant, upload_id, source.id)
    except Exception as exc:
        logger.exception("upload processing failed upload_id=%s", upload_id)
        await db.fail_upload(tenant, upload_id, type(exc).__name__ + ":" + str(exc))
    finally:
        heartbeat.cancel()
        await heartbeat


async def process_index(db: Database, store: ObjectStore, claimed: dict):
    tenant = str(claimed["tenant"])
    source_id, index_version = claimed["source_id"], claimed["index_version"]
    heartbeat = asyncio.create_task(
        keep_lease(
            lambda: db.heartbeat_index(tenant, source_id, index_version),
            db.settings.index_lease_seconds,
        )
    )
    try:
        _, document = await EvidenceService(db, store, tenant).document(source_id)
        status = await db.index_job(tenant, source_id, index_version)
        if not status:
            return
        chunks = chunk_document(source_id, status["parsed_hash"], document)
        await db.save_lexical_chunks(tenant, source_id, status["parsed_hash"], index_version, chunks)
        if not db.settings.embedding_url:
            await db.fail_index(
                tenant, source_id, index_version, "embedding_service_disabled", retry=False
            )
            return
        client = EmbeddingClient(
            db.settings.embedding_url,
            db.settings.request_timeout,
            db.settings.embedding_dimensions,
        )
        for start in range(0, len(chunks), 32):
            batch = chunks[start : start + 32]
            vectors = await embed_with_backoff(client, [chunk["text"] for chunk in batch])
            await db.save_embeddings(
                tenant,
                source_id,
                index_version,
                [
                    (chunk["chunk_id"], vector)
                    for chunk, vector in zip(batch, vectors, strict=True)
                ],
                db.settings.embedding_model,
                db.settings.embedding_revision,
            )
    except Exception as exc:
        logger.exception("source indexing failed source_id=%s", source_id)
        await db.fail_index(tenant, source_id, index_version, type(exc).__name__ + ":" + str(exc))
    finally:
        heartbeat.cancel()
        await heartbeat


async def work(settings: Settings, once: bool = False):
    db, store = Database(settings), ObjectStore(settings)
    await db.open()
    await store.setup()
    try:
        while True:
            upload = await db.claim_upload()
            if upload:
                await process_upload(db, store, upload)
            index = await db.claim_index()
            if index:
                await process_index(db, store, index)
            if once or (not upload and not index):
                if once:
                    break
                await asyncio.sleep(1)
    finally:
        await db.close()
