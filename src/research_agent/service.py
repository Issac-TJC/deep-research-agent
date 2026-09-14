import asyncio

from research_agent.contracts import CreateRun, ResearchPackage, UploadStatus
from research_agent.db import Database, NotFound
from research_agent.evidence import EvidenceService
from research_agent.intake import extract_urls
from research_agent.storage import ObjectStore, sha256


class ResearchService:
    def __init__(self, db: Database, store: ObjectStore):
        self.db, self.store = db, store

    async def create(self, tenant: str, request: CreateRun, idempotency_key: str):
        # The main input is intentionally free-form. URL recognition is deterministic;
        # the intent compiler later decides what each successfully fetched source means.
        request = request.model_copy(deep=True)
        inline_urls = extract_urls(request.brief.question)
        request.brief.source_urls = list(dict.fromkeys(request.brief.source_urls + inline_urls))[:20]
        request.brief.upload_ids = await self.db.resolve_upload_ids(
            tenant, request.brief.upload_ids
        )
        return await self.db.create_run(tenant, request, idempotency_key)

    async def report(self, tenant: str, run_id: str, revision: int | None = None) -> ResearchPackage:
        await self.db.run(tenant, run_id)
        reports = await self.db.records(tenant, run_id, "report")
        if revision is not None:
            reports = [r for r in reports if r["revision"] == revision]
        if not reports:
            raise NotFound("report not available")
        return ResearchPackage.model_validate(max(reports, key=lambda r: r["revision"]))

    async def upload(self, tenant: str, raw: bytes, mime: str, filename: str):
        if len(raw) > self.db.settings.max_upload_bytes:
            raise ValueError("upload exceeds size limit")
        return await EvidenceService(self.db, self.store, tenant).ingest(
            raw, mime, filename, filename=filename
        )

    async def start_upload(
        self, tenant: str, raw: bytes, mime: str, filename: str, parser_mode: str = "auto"
    ) -> UploadStatus:
        """Persist bytes before scheduling enhanced parsing; safe to retry by content."""
        if len(raw) > self.db.settings.max_upload_bytes:
            raise ValueError("upload exceeds size limit")
        raw_hash = sha256(raw)
        from research_agent.evidence import stable_id

        upload_id = stable_id(tenant, "upload", raw_hash, filename, parser_mode)
        raw_key = await self.store.put(tenant, raw, "raw", mime)
        await self.db.enqueue_upload(
            tenant, upload_id, raw_hash, raw_key, mime, filename, parser_mode
        )
        return await self.upload_state(tenant, upload_id)

    async def upload_state(self, tenant: str, upload_id: str) -> UploadStatus:
        row = await self.db.upload_status(tenant, upload_id)
        return UploadStatus(
            upload_id=upload_id,
            status=row["status"],
            source_id=row["source_id"],
            error=row["error"],
            progress=row["progress"],
        )

    async def wait_upload(self, tenant: str, upload_id: str, seconds: float) -> UploadStatus:
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            state = await self.upload_state(tenant, upload_id)
            if state.status in {"ready", "failed"} or asyncio.get_running_loop().time() >= deadline:
                return state
            await asyncio.sleep(self.db.settings.index_poll_seconds)
