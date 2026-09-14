import asyncio
import io
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from reportlab.pdfgen.canvas import Canvas

from research_agent.checkpoints import FencedSaver
from research_agent.contracts import CreateRun, ResearchBrief, RunProfile
from research_agent.db import BudgetExceeded, Conflict, NotFound, StaleLease
from research_agent.evidence import EvidenceService
from research_agent.fixtures import FixtureProvider
from research_agent.gateway import Gateway
from research_agent.indexer import process_upload
from research_agent.retrieval import CHUNKER_VERSION
from research_agent.worker import execute_claim

pytestmark = pytest.mark.integration


def request(template="technical_comparison", **profile):
    return CreateRun(
        brief=ResearchBrief(
            question="Compare lexical semantic and hybrid retrieval for Chinese technical documents",
            template=template,
            constraints=["Preserve exact error identifiers"],
        ),
        profile=RunProfile(**profile),
    )


async def create_claim(env, **profile):
    run, _ = await env["service"].create(env["tenant"], request(**profile), str(uuid4()))
    claim = await env["db"].claim()
    assert claim and claim["run_id"] == str(run["id"])
    return run, claim


@pytest.mark.parametrize("template", ["technical_comparison", "paper_review", "general_research"])
async def test_complete_research_templates(env, template):
    db, tenant = env["db"], env["tenant"]
    run, _ = await env["service"].create(tenant, request(template), str(uuid4()))
    claim = await db.claim()
    await execute_claim(db, env["store"], claim)
    final = await db.run(tenant, str(run["id"]))
    assert final["status"] == "completed", final
    package = await env["service"].report(tenant, str(run["id"]))
    assert package.claims and package.spans and package.sources
    assert package.quality_status == "unchecked"  # synthetic output cannot certify research quality
    assert final["model_calls"] > 3 and final["tool_calls"] > 0
    assert final["reserved_usd"] == 0 and final["reserved_tokens"] == 0
    assert not package.citation_errors, package.citation_errors
    summary = await db.usage_summary(tenant, str(run["id"]))
    assert summary["soft_target"] == 180000 and summary["hard_cap"] == 250000
    assert summary["budget_groups"]["scope_plan"]["model_calls"] >= 2
    scope_action = await db.action(tenant, str(run["id"]), "scope:attempt:0")
    assert scope_action["role"] == "research_director"
    assert scope_action["estimated_input_tokens"] > 0 and scope_action["request"]
    if template == "paper_review":
        assert package.report.paper and package.report.paper.ideas
        assert package.report.paper.reproduction_status == "not_executed"
    events = await db.events(tenant, str(run["id"]), 0)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    await db.run(tenant, str(run["id"]))
    with psycopg.connect(env["settings"].admin_database_url) as conn:
        count = conn.execute(
            "SELECT count(DISTINCT thread_id) FROM checkpoints WHERE tenant_id=%s", (tenant,)
        ).fetchone()[0]
    assert count >= 3  # parent and separately persisted researcher graphs
    Path("artifacts/test-runs").mkdir(parents=True, exist_ok=True)
    Path(f"artifacts/test-runs/{template}.json").write_text(package.model_dump_json(indent=2))


async def test_api_idempotency_isolation_files_and_events(env):
    client = env["client"]
    headers = {"Idempotency-Key": "same-request"}
    first = await client.post("/research-runs", json=request().model_dump(mode="json"), headers=headers)
    repeat = await client.post("/research-runs", json=request().model_dump(mode="json"), headers=headers)
    assert first.status_code == repeat.status_code == 202
    run_id = first.json()["run_id"]
    assert repeat.json()["run_id"] == run_id and not repeat.json()["created"]
    changed = request().model_dump(mode="json")
    changed["brief"]["question"] = "A different research question"
    assert (await client.post("/research-runs", json=changed, headers=headers)).status_code == 409
    other = {"Authorization": "Bearer " + env["other_key"]}
    for path in [
        f"/research-runs/{run_id}",
        f"/research-runs/{run_id}/events",
        f"/research-runs/{run_id}/report",
    ]:
        assert (await client.get(path, headers=other)).status_code == 404
    assert (await client.post(f"/research-runs/{run_id}/cancel", headers=other)).status_code == 404
    raw = b"# Private constraints\n\nKeep error code ERR_42 exactly."
    upload = await client.post("/uploads", files={"file": ("same.md", raw, "text/markdown")})
    assert upload.status_code == 201, upload.text
    sid = upload.json()["upload_id"]
    other_upload = await client.post(
        "/uploads", files={"file": ("same.md", raw, "text/markdown")}, headers=other
    )
    assert other_upload.json()["upload_id"] != sid
    assert (await client.get(f"/sources/{sid}", headers=other)).status_code == 404
    assert (await client.get(f"/sources/{sid}/raw", headers=other)).status_code == 404
    assert upload.json()["source"]["raw_key"] != other_upload.json()["source"]["raw_key"]
    isolated_search = await EvidenceService(
        env["db"], env["store"], env["tenant"]
    ).search([other_upload.json()["upload_id"]], "ERR_42", "lexical")
    assert isolated_search["results"] == [] and isolated_search["fallback"] == "index_not_ready"
    with pytest.raises(PermissionError):
        await env["store"].get(env["other"], upload.json()["source"]["raw_key"])
    await client.post(f"/research-runs/{run_id}/cancel")
    stream = await client.get(f"/research-runs/{run_id}/events?after_seq=0")
    blocks = [json.loads(x[6:]) for x in stream.text.splitlines() if x.startswith("data: ")]
    after = await client.get(
        f"/research-runs/{run_id}/events", headers={"Last-Event-ID": str(blocks[0]["seq"])}
    )
    assert f"id: {blocks[0]['seq']}\n" not in after.text


async def test_rls_context_reset_and_checkpoint_fencing(env):
    db, tenant = env["db"], env["tenant"]
    run, claim = await create_claim(env)
    rid, token = str(run["id"]), claim["token"]
    saver = FencedSaver(db, tenant, rid, token)
    config = {"configurable": {"thread_id": rid, "checkpoint_ns": ""}}
    cp = empty_checkpoint()
    cp["channel_values"] = {
        "secret": "tenant A evidence",
        "progress": {"authorized_source_ids": ["source-a"], "read_ranges": {"source-a": [[0, 20]]}},
    }
    cp["channel_versions"] = {"secret": "1", "progress": "1"}
    await saver.aput(config, cp, {}, {"secret": "1", "progress": "1"})
    assert (await saver.aget_tuple(config)).checkpoint["channel_values"]["secret"] == "tenant A evidence"
    restored_progress = (await saver.aget_tuple(config)).checkpoint["channel_values"]["progress"]
    assert restored_progress["authorized_source_ids"] == ["source-a"] and "messages" not in restored_progress
    other_saver = FencedSaver(db, env["other"], rid, token)
    assert await other_saver.aget_tuple(config) is None
    async with db.pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM research_runs")).fetchone())["n"] == 0
    await db.cancel(tenant, rid)
    with pytest.raises(StaleLease):
        await saver.aput(config, empty_checkpoint(), {}, {})
    with pytest.raises(StaleLease):
        await db.put(tenant, "task", "late", {}, rid, token)
    with pytest.raises(Conflict):
        await db.resume(tenant, rid)


async def test_pending_upload_blocks_run_then_resolves_to_immutable_source(env):
    data = io.BytesIO()
    pdf = Canvas(data)
    pdf.drawString(50, 700, "ERR_UPLOAD_42 is explained on this page.")
    pdf.save()
    pending = await env["service"].start_upload(
        env["tenant"], data.getvalue(), "application/pdf", "pending.pdf", "native"
    )
    request_with_upload = request()
    request_with_upload.brief.upload_ids = [pending.upload_id]
    with pytest.raises(Conflict, match="upload_not_ready"):
        await env["service"].create(env["tenant"], request_with_upload, str(uuid4()))
    with pytest.raises(NotFound):
        await env["db"].upload_status(env["other"], pending.upload_id)
    claimed = await env["db"].claim_upload()
    assert claimed and claimed["upload_id"] == pending.upload_id
    await process_upload(env["db"], env["store"], claimed)
    ready = await env["service"].upload_state(env["tenant"], pending.upload_id)
    assert ready.status == "ready" and ready.source_id
    run, _ = await env["service"].create(env["tenant"], request_with_upload, str(uuid4()))
    assert run["brief"]["upload_ids"] == [ready.source_id]


async def test_concurrent_budget_reservation_unknown_cost_and_cancel(env):
    db, tenant = env["db"], env["tenant"]
    run, claim = await create_claim(env, max_usd=0.10)
    rid, fence = str(run["id"]), claim["token"]
    outcomes = await asyncio.gather(
        *(db.reserve(tenant, rid, fence, f"race:{i}", "model", 0.06, 100) for i in range(3)),
        return_exceptions=True,
    )
    assert sum(not isinstance(x, Exception) for x in outcomes) == 1
    assert sum(isinstance(x, BudgetExceeded) for x in outcomes) == 2
    state = await db.run(tenant, rid)
    assert float(state["reserved_usd"]) == 0.06 and state["model_calls"] == 1
    winner = outcomes.index(None)
    await db.settle(tenant, rid, fence, f"race:{winner}", None, {}, error="timeout", unknown=True)
    assert float((await db.run(tenant, rid))["reserved_usd"]) == 0.06


async def test_expired_lease_reclaims_without_rebilling_committed_model(env):
    db, tenant = env["db"], env["tenant"]
    run, claim = await create_claim(env)
    rid = str(run["id"])
    calls = 0
    gateway = Gateway(db, tenant, rid, claim["token"], RunProfile(), FixtureProvider(), "fixture")

    async def operation():
        nonlocal calls
        calls += 1
        return {"value": "saved"}

    assert await gateway.tool("fetch", "stable-step", operation) == {"value": "saved"}
    with psycopg.connect(env["settings"].admin_database_url) as conn:
        conn.execute("UPDATE research_runs SET lease_until=now()-interval '1 second' WHERE id=%s", (rid,))
    newer = await db.claim()
    assert newer["token"] > claim["token"]
    gateway.fence = newer["token"]
    assert await gateway.tool("fetch", "stable-step", operation) == {"value": "saved"}
    assert calls == 1 and (await db.run(tenant, rid))["tool_calls"] == 1
    with pytest.raises(StaleLease):
        await db.phase(tenant, rid, claim["token"], "late")


async def test_hard_budget_delivers_partial_package(env):
    db, tenant = env["db"], env["tenant"]
    run, claim = await create_claim(env, max_model_calls=1)
    await execute_claim(db, env["store"], claim)
    final = await db.run(tenant, str(run["id"]))
    assert final["status"] == "completed" and final["quality_status"] == "needs_review"
    package = await env["service"].report(tenant, str(run["id"]))
    assert package.report.unresolved and package.revision == 999


async def test_failed_embedding_build_keeps_lexical_index_and_requeues_new_version(env):
    db, tenant = env["db"], env["tenant"]
    source_id, old_version, new_version = "source-" + str(uuid4()), str(uuid4()), str(uuid4())
    await db.enqueue_index(
        tenant, source_id, "parsed-hash", old_version, CHUNKER_VERSION, "gte", "9bbca17"
    )
    await db.save_lexical_chunks(
        tenant,
        source_id,
        "parsed-hash",
        old_version,
        [
            {
                "chunk_id": "chunk-1",
                "ordinal": 0,
                "start": 0,
                "end": 8,
                "page": 1,
                "bbox": None,
                "kind": "paragraph",
                "section_path": [],
                "block_id": "block-1",
                "text": "evidence",
                "lexical_text": "evidence",
            }
        ],
    )
    await db.fail_index(tenant, source_id, old_version, "HTTPStatusError:503")
    assert (await db.usable_index_status(tenant, source_id))["index_version"] == old_version
    await db.requeue_index(
        tenant, source_id, "parsed-hash", new_version, CHUNKER_VERSION, "gte", "9bbca17"
    )
    assert (await db.index_status(tenant, source_id))["index_version"] == new_version
    assert (await db.usable_index_status(tenant, source_id))["index_version"] == old_version
