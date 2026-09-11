"""Small metered integration run. Stop other workers first; credentials stay local."""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from research_agent.contracts import CreateRun, ResearchBrief, RunProfile
from research_agent.db import Database
from research_agent.evaluation import measure
from research_agent.service import ResearchService
from research_agent.settings import Settings
from research_agent.storage import ObjectStore
from research_agent.worker import execute_claim


async def main(request_file: Path | None = None):
    settings = Settings(research_mode="live")
    if not settings.deepseek_api_key or not settings.tavily_api_key:
        raise SystemExit("Configure both keys in the project .env before running.")
    key = os.environ.get("RESEARCH_API_KEY")
    if not key:
        key = json.loads(Path(".local/test-tenants.jsonl").read_text().splitlines()[0])["api_key"]
    db, store = Database(settings), ObjectStore(settings)
    await db.open()
    await store.setup()
    try:
        tenant = await db.auth(key)
        if not tenant:
            raise SystemExit("Invalid local tenant key.")
        request = CreateRun(
            brief=ResearchBrief(
                question="为中文技术知识库比较关键词检索、向量检索、混合检索与重排。重点解释原理、错误码精确匹配、同义改写与长文档的适用条件，给出小团队可实施的选型建议和后续评测设计。",
                template="technical_comparison",
                constraints=[
                    "优先官方文档；至少进行一次搜索发现遗漏来源。",
                    "没有同条件测量时，成本、中文召回率和延迟必须标为未知，不编造数字。",
                    "保留 ERR_AUTH_42 这样的完整技术标识符；不声称执行过实验。",
                    "控制范围：只比较检索方法，不做数据库厂商全面排名。",
                ],
                candidates=["关键词", "向量", "混合", "重排"],
                dimensions=["原理", "精确标识符", "语义改写", "实施复杂度", "成本与验证"],
                source_urls=["https://raw.githubusercontent.com/pgvector/pgvector/master/README.md"],
            ),
            profile=RunProfile(
                max_usd=0.50,
                max_search_calls=4,
                max_task_search_calls=2,
                max_task_tools=8,
                max_tasks=2,
                max_gap_rounds=0,
                max_patch_rounds=1,
            ),
        )
        if request_file:
            request = CreateRun.model_validate_json(request_file.read_text())
            if request.profile.max_usd > 0.50:
                raise SystemExit("Smoke requests must stay within $0.50 per run.")
        service = ResearchService(db, store)
        run, _ = await service.create(tenant, request, "live-smoke:" + str(uuid4()))
        rid = str(run["id"])
        print(json.dumps({"run_id": rid, "mode": "live", "run_cap_usd": request.profile.max_usd}), flush=True)
        claim = await db.claim(rid)
        if not claim:
            raise SystemExit("Another run is active. Stop workers and finish/cancel it first.")
        started = time.monotonic()
        execution = asyncio.create_task(execute_claim(db, store, claim))
        while not execution.done():
            await asyncio.wait([execution], timeout=20)
            state = await db.run(tenant, rid)
            print(
                json.dumps(
                    {
                        k: str(state[k])
                        for k in [
                            "status",
                            "phase",
                            "model_calls",
                            "tool_calls",
                            "search_calls",
                            "tokens",
                            "spent_usd",
                            "reserved_usd",
                        ]
                    }
                ),
                flush=True,
            )
        await execution
        final = await db.run(tenant, rid)
        output = Path("artifacts/live")
        output.mkdir(parents=True, exist_ok=True)
        calls = await db.usage(tenant, rid)
        reports = await db.records(tenant, rid, "report")
        result = {
            "run_id": rid,
            "status": final["status"],
            "stop_reason": final["stop_reason"],
            "wall_seconds": time.monotonic() - started,
            "metering": {
                key: str(final[key])
                for key in (
                    "model_calls",
                    "tool_calls",
                    "search_calls",
                    "tokens",
                    "spent_usd",
                    "reserved_usd",
                )
            },
        }
        if reports:
            package = await service.report(tenant, rid)
            result["metrics"] = measure(
                final,
                package,
                calls,
                await db.records(tenant, rid, "context"),
                await db.records(tenant, rid, "review"),
            )
            (output / f"{rid}.json").write_text(package.model_dump_json(indent=2))
            (output / f"{rid}.md").write_text(package.markdown)
        (output / f"{rid}.metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_file", nargs="?", type=Path)
    asyncio.run(main(parser.parse_args().request_file))
