import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from research_agent.academic import AcademicSearch, rank_candidates
from research_agent.contracts import (
    CreateRun,
    MessageCreate,
    ResearchBrief,
    ResearchPackage,
    RunProfile,
    UploadStatus,
    uid,
)
from research_agent.db import Database, NotFound, digest
from research_agent.evidence import EvidenceService
from research_agent.intake import extract_urls
from research_agent.memory import MemoryBroker
from research_agent.storage import ObjectStore, sha256


def local_week_period(timezone_name: str, instant: datetime | None = None):
    zone = ZoneInfo(timezone_name)
    local = (instant or datetime.now(ZoneInfo("UTC"))).astimezone(zone)
    start = (local - timedelta(days=local.weekday())).date()
    return start, start + timedelta(days=6)


class ResearchService:
    def __init__(self, db: Database, store: ObjectStore):
        self.db, self.store = db, store
        self.memory = MemoryBroker(db)

    async def _embedding(self, text: str) -> list[float] | None:
        if not text or not self.db.settings.embedding_url:
            return None
        try:
            from research_agent.retrieval import EmbeddingClient

            return (
                await EmbeddingClient(
                    self.db.settings.embedding_url,
                    self.db.settings.request_timeout,
                    self.db.settings.embedding_dimensions,
                ).embed([text])
            )[0]
        except Exception:
            return None

    async def create_memory(self, tenant: str, project_id: str, payload: dict) -> dict:
        row = await self.db.create_memory(tenant, project_id, payload)
        if embedding := await self._embedding(row["content"]):
            await self.db.set_memory_embedding(tenant, project_id, str(row["id"]), embedding)
        return row

    async def update_memory(self, tenant: str, project_id: str, memory_id: str, payload: dict) -> dict:
        row = await self.db.update_memory(tenant, project_id, memory_id, payload)
        if (payload.get("content") or payload.get("status") == "confirmed") and not row.get("embedding") \
                and (embedding := await self._embedding(row["content"])):
            await self.db.set_memory_embedding(tenant, project_id, str(row["id"]), embedding)
        return row

    async def create_observation(self, tenant: str, project_id: str, payload: dict) -> dict:
        row = await self.db.create_observation(tenant, project_id, payload)
        if embedding := await self._embedding(f"{row['summary']}\n{row['body']}"):
            await self.db.set_observation_embedding(tenant, str(row["id"]), embedding)
        return row

    async def update_observation(
        self, tenant: str, project_id: str, observation_id: str, payload: dict
    ) -> dict:
        row = await self.db.update_observation(tenant, project_id, observation_id, payload)
        if (payload.get("body") or payload.get("summary") or payload.get("status") == "confirmed") \
                and not row.get("embedding") \
                and (embedding := await self._embedding(f"{row['summary']}\n{row['body']}") ):
            await self.db.set_observation_embedding(tenant, str(row["id"]), embedding)
        return row

    async def _prepare_request(self, tenant: str, request: CreateRun) -> CreateRun:
        # The main input is intentionally free-form. URL recognition is deterministic;
        # the intent compiler later decides what each successfully fetched source means.
        request = request.model_copy(deep=True)
        caller_snapshot = dict(request.context_snapshot)
        inline_urls = extract_urls(request.brief.question)
        request.brief.source_urls = list(dict.fromkeys(request.brief.source_urls + inline_urls))[:20]
        if request.project_id:
            prepared = await self.memory.build(
                tenant,
                request.project_id,
                request.conversation_id,
                request.brief.question,
                await self._embedding(request.brief.question),
                request.profile.prompt_token_limit,
            )
            project = prepared["project"]
            request.brief.constraints = list(dict.fromkeys(
                request.brief.constraints + [f"项目目标: {project['objective']}"]
                + [f"项目排除项: {item}" for item in project["exclusions"]]
            ))[:30]
            request.brief.memory_context = prepared["memory_context"]
            request.brief.upload_ids = list(dict.fromkeys(request.brief.upload_ids + prepared["source_ids"]))[
                :20
            ]
            request.context_snapshot = {**caller_snapshot, **prepared["snapshot"],
                                        "project_id": request.project_id,
                                        "conversation_id": request.conversation_id}
        elif caller_snapshot:
            request.context_snapshot = caller_snapshot
        request.brief.upload_ids = await self.db.resolve_upload_ids(tenant, request.brief.upload_ids)
        return request

    async def create(self, tenant: str, request: CreateRun, idempotency_key: str):
        request = await self._prepare_request(tenant, request)
        run, created = await self.db.create_run(tenant, request, idempotency_key)
        return run, created

    async def run_subscription(self, tenant: str, subscription_id: str) -> dict:
        """Turn a saved weekly query into an auditable research run and in-app digest."""
        subscription = await self.db.subscription(tenant, subscription_id)
        if subscription["status"] != "active":
            raise ValueError("subscription is not active")
        period_start, period_end = local_week_period(
            subscription["schedule"].get("timezone", "UTC")
        )
        digest, reserved = await self.db.prepare_digest_run(tenant, subscription_id, period_start, period_end)
        if not reserved and digest.get("run_id"):
            return {
                "digest": digest,
                "run_id": digest["run_id"],
                "created": False,
            }
        conversations = await self.db.conversations(tenant, str(subscription["project_id"]))
        if conversations:
            conversation_id = str(conversations[0]["id"])
        else:
            conversation = await self.db.create_conversation(
                tenant,
                str(subscription["project_id"]),
                {"title": f"周报 · {subscription['name']}"},
            )
            conversation_id = str(conversation["id"])
        query = subscription["query_config"]
        candidates, attempts = await AcademicSearch(self.db.settings).collect(
            query["topic"],
            query.get("query_terms", []),
            period_start - timedelta(days=max(0, query.get("lookback_days", 14) - 7)),
            period_end,
        )
        exclusions = [
            item.lower()
            for item in dict.fromkeys(
                [*digest["query_snapshot"].get("project_exclusions", []), *query.get("exclusions", [])]
            )
        ]
        candidates = [
            item
            for item in candidates
            if not any(term in f"{item.get('title', '')} {item.get('abstract', '')}".lower() for term in exclusions)
        ]
        feedback = await self.db.feedback_for_subscription(tenant, subscription_id)
        ranked = rank_candidates(candidates, query["topic"], query.get("query_terms", []), feedback)
        paper_count = query.get("paper_count", 5)
        await self.db.save_digest_collection(
            tenant, str(digest["id"]), ranked, attempts, min(paper_count, len(ranked))
        )
        selected = ranked[:paper_count]
        terms = "、".join(query.get("query_terms", [])) or "围绕主题扩展检索词"
        exclusion_text = "、".join(query.get("exclusions", [])) or "无额外排除项"
        paper_context = "\n".join(
            f"候选 {index + 1}: {item['title']}；{item['evidence_scope']}；"
            f"{item['selection_rationale']}；摘要：{item.get('abstract', '')[:900]}"
            for index, item in enumerate(selected)
        )[:4500]
        question = (
            f"生成 {period_start.isoformat()} 至 {period_end.isoformat()} 的论文研究周报。"
            f"主题：{query['topic']}。查询词：{terms}。排除：{exclusion_text}。"
            f"重点覆盖最近 {query.get('lookback_days', 14)} 天，筛选约 {query.get('paper_count', 5)} 篇最相关论文。"
            "逐篇说明研究问题、方法、主要结果、与项目的关系和推荐理由；"
            "必须区分全文、仅摘要与仅元数据证据，不能把摘要结论表述成已核验全文结论。"
            f"\n连接器候选：\n{paper_context or '本期连接器未返回合格候选，必须明确披露候选不足。'}"
        )
        try:
            run, created = await self.create(
                tenant,
                CreateRun(
                    brief=ResearchBrief(
                        question=question,
                        template="paper_review",
                        language=query.get("language", "zh"),
                        source_urls=[item["url"] for item in selected if item.get("url")][:20],
                    ),
                    project_id=str(subscription["project_id"]),
                    conversation_id=conversation_id,
                    run_kind="weekly_digest",
                    context_snapshot={
                        "digest_id": str(digest["id"]),
                        "subscription_id": subscription_id,
                        "local_period_start": period_start.isoformat(),
                        "local_period_end": period_end.isoformat(),
                        "weekly_budget_allocation": {
                            "planning": 0.10,
                            "screening": 0.20,
                            "deep_reading": 0.40,
                            "review": 0.15,
                            "writing_revision": 0.15,
                        },
                    },
                ),
                f"weekly:{subscription_id}:{period_start.isoformat()}",
            )
            digest = await self.db.link_digest_run(tenant, str(digest["id"]), str(run["id"]))
            return {"digest": digest, "run_id": str(run["id"]), "created": created}
        except Exception as exc:
            await self.db.fail_digest(tenant, str(digest["id"]), str(exc))
            raise

    async def preview_subscription(self, tenant: str, subscription_id: str) -> dict:
        subscription = await self.db.subscription(tenant, subscription_id)
        digest = await self.db.create_digest_preview(tenant, subscription_id)
        query = subscription["query_config"]
        end = datetime.now(ZoneInfo(subscription["schedule"].get("timezone", "UTC"))).date()
        start = end - timedelta(days=query.get("lookback_days", 14) - 1)
        candidates, attempts = await AcademicSearch(self.db.settings).collect(
            query["topic"], query.get("query_terms", []), start, end
        )
        exclusions = [item.lower() for item in digest["query_snapshot"].get("exclusions", [])]
        candidates = [
            item
            for item in candidates
            if not any(term in f"{item.get('title', '')} {item.get('abstract', '')}".lower() for term in exclusions)
        ]
        ranked = rank_candidates(candidates, query["topic"], query.get("query_terms", []), {})
        updated = await self.db.save_digest_collection(
            tenant,
            str(digest["id"]),
            ranked,
            attempts,
            min(query.get("paper_count", 5), len(ranked)),
        )
        return await self.db.digest_detail(tenant, str(updated["id"]))

    async def send_message(
        self, tenant: str, conversation_id: str, message: MessageCreate, idempotency_key: str
    ) -> dict:
        if message.mode == "research" and len(message.content.strip()) < 8:
            raise ValueError("research message must contain at least 8 characters")
        project_id = await self.db.conversation_project(tenant, conversation_id)
        profile = message.profile
        if message.mode == "quick_answer":
            profile = RunProfile(
                name="quick-answer-v1",
                concurrency=1,
                max_tasks=1,
                max_gap_rounds=0,
                max_patch_rounds=0,
                max_tool_calls=1,
                max_task_tools=1,
                max_search_calls=0,
                max_task_search_calls=0,
                max_model_calls=6,
                max_tokens=16000,
                prompt_token_limit=16000,
                max_output_tokens=4000,
                max_seconds=180,
                max_usd=0.25,
                max_sources=20,
                paper_related_sources=0,
                search_policy="frozen",
                max_retrieval_calls=2,
            )
        request = CreateRun(
            brief=ResearchBrief(
                question=(
                    message.content
                    if len(message.content.strip()) >= 8
                    else f"请简洁回答用户消息：{message.content}"
                ),
                upload_ids=message.attachment_ids,
                language="zh",
            ),
            profile=profile,
            project_id=project_id,
            conversation_id=conversation_id,
            parent_run_id=message.parent_run_id,
            run_kind="quick_answer" if message.mode == "quick_answer" else "research",
        )
        request = await self._prepare_request(tenant, request)
        result, _ = await self.db.create_message_run(
            tenant,
            conversation_id,
            message.content,
            message.attachment_ids,
            request,
            idempotency_key,
            message.model_dump(mode="json"),
        )
        return result

    async def compact_conversation(self, tenant: str, conversation_id: str) -> dict:
        material = await self.db.conversation_memory_material(tenant, conversation_id)
        conversation = material["conversation"]
        source_digest = digest({
            "conversation_id": conversation_id,
            "message_ids": [str(item["id"]) for item in material["messages"]],
            "summary_id": str(material["summary"]["id"]) if material["summary"] else None,
        })
        return await self.db.enqueue_memory_job(
            tenant, str(conversation["project_id"]), conversation_id,
            "conversation_compaction", "conversation", conversation_id, source_digest,
            {"force": True},
        )

    async def project_search(self, tenant: str, project_id: str, request) -> dict:
        context = await self.db.project_context(
            tenant,
            project_id,
            request.conversation_id,
            request.query,
            await self._embedding(request.query),
        )
        objects = await self.db.search_project_objects(
            tenant,
            project_id,
            request.query,
            request.types,
            request.conversation_id,
            min(100, request.limit * 3),
        )
        document = (
            await EvidenceService(self.db, self.store, tenant).search(
                context["source_ids"], request.query, "hybrid", min(request.limit, 20)
            )
            if not request.types or "artifact" in request.types
            else {"results": [], "strategy": "object_only", "fallback": None}
        )
        hits = [
            {
                "type": "artifact",
                "id": item["chunk_id"],
                "title": item.get("title") or "项目文件",
                "snippet": item["snippet"],
                "source_id": item["source_id"],
                "locator": {
                    "page": item.get("page"),
                    "start": item["start"],
                    "end": item["end"],
                    "chunk_id": item["chunk_id"],
                },
                "match_reason": "hybrid_document_retrieval",
                "score_details": item.get("score_details", {}),
                "rrf_score": 1 / (60 + index),
            }
            for index, item in enumerate(document["results"], 1)
        ]
        filtered = []
        for item in objects:
            created = item.get("created_at")
            if request.created_after and created and created < request.created_after:
                continue
            if request.created_before and created and created > request.created_before:
                continue
            if request.tags and item["type"] == "artifact":
                if not set(request.tags) <= set(item.get("tags", [])):
                    continue
            filtered.append(item)
        groups: dict[str, list[dict]] = {}
        for item in [*hits, *filtered]:
            groups.setdefault(item["type"], []).append(item)
        combined = []
        while len(combined) < request.limit and any(groups.values()):
            for kind in ("artifact", "message", "memory", "claim", "report", "digest"):
                if groups.get(kind) and len(combined) < request.limit:
                    rank = len([item for item in combined if item["type"] == kind]) + 1
                    item = groups[kind].pop(0)
                    item["rrf_score"] = item.get("rrf_score", 1 / (60 + rank))
                    combined.append(item)
        return {
            "query_snapshot_id": uid(),
            "query": request.query,
            "strategy": document["strategy"],
            "fallback": document["fallback"],
            "results": combined,
            "groups": {
                kind: [item for item in combined if item["type"] == kind]
                for kind in dict.fromkeys(item["type"] for item in combined)
            },
        }

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
        await self.db.enqueue_upload(tenant, upload_id, raw_hash, raw_key, mime, filename, parser_mode)
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
