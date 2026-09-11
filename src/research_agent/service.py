from research_agent.contracts import CreateRun, ResearchPackage
from research_agent.db import Database, NotFound
from research_agent.evidence import EvidenceService
from research_agent.storage import ObjectStore


class ResearchService:
    def __init__(self, db: Database, store: ObjectStore):
        self.db, self.store = db, store

    async def create(self, tenant: str, request: CreateRun, idempotency_key: str):
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
