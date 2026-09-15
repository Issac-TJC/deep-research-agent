from uuid import uuid4

import pytest

from research_agent.checkpoints import ConversationSaver
from research_agent.contracts import CreateRun, ResearchBrief
from research_agent.memory import MemoryJobRunner
from research_agent.worker import execute_claim

pytestmark = pytest.mark.integration


async def create_project(client):
    response = await client.post(
        "/projects",
        json={
            "name": "3D Gaussian Splatting",
            "objective": "跟踪三维重建、渲染和压缩方法",
            "timezone": "Asia/Shanghai",
            "tags": ["3DGS", "三维重建"],
            "exclusions": ["纯营销内容"],
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_conversation_run_and_tenant_isolation(env):
    client = env["client"]
    health = (await client.get("/health")).json()
    assert health["research_mode"] == "fixture"
    assert isinstance(health["live_provider_ready"], bool)
    project = await create_project(client)
    projects = (await client.get("/projects")).json()
    assert any(item["is_default"] for item in projects)
    assert any(item["id"] == project["id"] for item in projects)

    conversations = (await client.get(f"/projects/{project['id']}/conversations")).json()
    assert len(conversations) == 1
    conversation_id = conversations[0]["id"]
    sent = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"content": "比较近期 3DGS 压缩方法并保留论文证据。"},
    )
    assert sent.status_code == 202, sent.text
    run = sent.json()["run"]
    assert run["project_id"] == project["id"]
    assert run["conversation_id"] == conversation_id
    messages = (await client.get(f"/conversations/{conversation_id}/messages")).json()
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[-1]["run_id"] == run["id"]
    busy = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"content": "同一个对话中的第二个并发任务应被拒绝。"},
    )
    assert busy.status_code == 409
    assert busy.json()["detail"] == "conversation_busy"

    other = {"Authorization": "Bearer " + env["other_key"]}
    assert (await client.get(f"/projects/{project['id']}", headers=other)).status_code == 404
    assert (await client.get(f"/conversations/{conversation_id}/messages", headers=other)).status_code == 404


async def test_project_assets_memory_profile_search_and_digest_run(env):
    client = env["client"]
    project = await create_project(client)
    upload = await client.post(
        "/uploads",
        headers={"Idempotency-Key": str(uuid4())},
        files={
            "file": (
                "project-notes.md",
                b"# Compression constraint\n\nPreserve geometry while reducing storage cost.",
                "text/markdown",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    source_id = upload.json()["source"]["id"]
    artifact = await client.post(
        f"/projects/{project['id']}/artifacts",
        json={"source_version_id": source_id, "title": "Compression notes", "tags": ["compression"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert artifact.status_code == 201, artifact.text
    assets = (await client.get(f"/projects/{project['id']}/artifacts")).json()
    assert assets[0]["source_version_id"] == source_id

    memory = await client.post(
        f"/projects/{project['id']}/memories",
        json={
            "type": "constraint",
            "content": "必须保留几何质量",
            "status": "candidate",
            "confidence": 0.9,
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert memory.status_code == 201
    confirmed = await client.patch(
        f"/projects/{project['id']}/memories/{memory.json()['id']}",
        json={"status": "confirmed"},
    )
    assert confirmed.json()["status"] == "confirmed"

    profile = await client.patch(
        "/users/me/research-profile", json={"learning_enabled": True, "preferences": {"depth": "technical"}}
    )
    assert profile.json()["learning_enabled"] is True
    signal = await client.post(
        "/users/me/research-profile/signals",
        json={"field": "method", "value": "工程实现", "source": "explicit"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert signal.status_code == 201

    search = await client.post(
        f"/projects/{project['id']}/search", json={"query": "几何质量", "types": ["memory"]}
    )
    assert search.status_code == 200, search.text
    assert search.json()["results"][0]["type"] == "memory"

    subscription = await client.post(
        f"/projects/{project['id']}/subscriptions",
        json={"topic": "3DGS compression", "timezone": "Asia/Shanghai"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert subscription.status_code == 201, subscription.text
    preview = await client.post(
        f"/subscriptions/{subscription.json()['id']}/preview",
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert preview.status_code == 201, preview.text
    assert preview.json()["status"] == "draft"
    assert preview.json()["query_snapshot"]["paper_count"] == 5
    assert "openalex" in preview.json()["query_snapshot"]["connectors"]

    run_key = str(uuid4())
    started = await client.post(
        f"/subscriptions/{subscription.json()['id']}/run",
        headers={"Idempotency-Key": run_key},
    )
    assert started.status_code == 202, started.text
    assert started.json()["created"] is True
    assert started.json()["digest"]["status"] == "collecting"
    repeated = await client.post(
        f"/subscriptions/{subscription.json()['id']}/run",
        headers={"Idempotency-Key": run_key},
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json() == started.json()
    domain_repeat = await client.post(
        f"/subscriptions/{subscription.json()['id']}/run",
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert domain_repeat.json()["run_id"] == started.json()["run_id"]
    assert domain_repeat.json()["created"] is False
    claim = await env["db"].claim(expected=started.json()["run_id"])
    assert claim
    await execute_claim(env["db"], env["store"], claim)
    digests = (await client.get(f"/subscriptions/{subscription.json()['id']}/digests")).json()
    assert digests[0]["status"] == "needs_review"
    assert digests[0]["quality"]["state"] == "unchecked"


async def test_archive_and_soft_delete_guards(env):
    client = env["client"]
    project = await create_project(client)
    archived = await client.patch(f"/projects/{project['id']}", json={"status": "archived"})
    assert archived.json()["status"] == "archived"
    conversations = (
        await client.get(f"/projects/{project['id']}/conversations", params={"include_archived": True})
    ).json()
    blocked = await client.post(
        f"/conversations/{conversations[0]['id']}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"content": "归档后不能创建新的研究执行。"},
    )
    assert blocked.status_code == 409
    await client.patch(f"/projects/{project['id']}", json={"status": "active"})
    deleted = await client.delete(f"/projects/{project['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/projects/{project['id']}")).status_code == 404
    restored = await client.post(f"/projects/{project['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"


async def test_message_idempotency_context_snapshot_and_event_reconnect(env):
    client = env["client"]
    project = await create_project(client)
    conversation_id = (await client.get(f"/projects/{project['id']}/conversations")).json()[0]["id"]
    key = str(uuid4())
    body = {"content": "请快速说明这个项目当前的研究目标。", "mode": "quick_answer"}
    first = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json=body,
    )
    replay = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json=body,
    )
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["run"]["run_kind"] == "quick_answer"
    assert len((await client.get(f"/conversations/{conversation_id}/messages")).json()) == 2
    changed = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json={**body, "content": "同一个键不允许换成另一段内容。"},
    )
    assert changed.status_code == 409
    snapshot = first.json()["run"]["context_snapshot"]
    assert snapshot["project_id"] == project["id"]
    assert snapshot["context_policy"] == "evomemory-v2-budgeted"
    assert snapshot["memory_system_version"] == 2
    assert snapshot["estimated_tokens"] <= snapshot["budget_tokens"]
    events = await env["db"].conversation_events(env["tenant"], conversation_id)
    assert [item["event_type"] for item in events] == [
        "message.created",
        "message.created",
        "run.linked",
    ]
    assert not await env["db"].conversation_events(
        env["tenant"], conversation_id, events[-1]["id"]
    )


async def test_deleted_project_hides_every_entry_point_and_restore_recovers_it(env):
    client = env["client"]
    project = await create_project(client)
    project_id = project["id"]
    conversation_id = (await client.get(f"/projects/{project_id}/conversations")).json()[0]["id"]
    upload = await client.post(
        "/uploads",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("private.md", b"deleted project source", "text/markdown")},
    )
    source_id = upload.json()["source"]["id"]
    await client.post(
        f"/projects/{project_id}/artifacts",
        headers={"Idempotency-Key": str(uuid4())},
        json={"source_version_id": source_id},
    )
    memory = await client.post(
        f"/projects/{project_id}/memories",
        headers={"Idempotency-Key": str(uuid4())},
        json={"type": "constraint", "content": "private constraint"},
    )
    subscription = await client.post(
        f"/projects/{project_id}/subscriptions",
        headers={"Idempotency-Key": str(uuid4())},
        json={"topic": "private research"},
    )
    message = await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"content": "研究这份项目私有材料的具体约束。"},
    )
    run_id = message.json()["run"]["id"]
    assert (await client.delete(f"/projects/{project_id}")).status_code == 204
    checks = [
        await client.get(f"/projects/{project_id}"),
        await client.get(f"/projects/{project_id}/conversations"),
        await client.get(f"/conversations/{conversation_id}/messages"),
        await client.get(f"/projects/{project_id}/artifacts"),
        await client.get(f"/projects/{project_id}/memories"),
        await client.get(f"/projects/{project_id}/subscriptions"),
        await client.get(f"/projects/{project_id}/audit-events"),
        await client.get(f"/research-runs/{run_id}"),
        await client.get(f"/sources/{source_id}"),
        await client.post(f"/projects/{project_id}/search", json={"query": "private"}),
        await client.get(f"/subscriptions/{subscription.json()['id']}/digests"),
    ]
    assert all(response.status_code == 404 for response in checks)
    restored = await client.post(f"/projects/{project_id}/restore")
    assert restored.status_code == 200
    assert (await client.get(f"/projects/{project_id}/memories")).json()[0]["id"] == memory.json()["id"]
    assert (await client.get(f"/sources/{source_id}")).status_code == 200


async def test_summary_stops_before_pending_protocol_and_is_injected(env):
    client = env["client"]
    project = await create_project(client)
    conversation_id = (await client.get(f"/projects/{project['id']}/conversations")).json()[0]["id"]
    async with env["db"].tx(env["tenant"]) as conn:
        for index in range(8):
            await env["db"]._append_message(
                conn, env["tenant"], conversation_id, "user", f"用户研究轮次 {index}"
            )
            await env["db"]._append_message(
                conn, env["tenant"], conversation_id, "assistant", f"闭合回答 {index}"
            )
    summary, _ = await env["service"].memory.compactor.compact(env["tenant"], conversation_id)
    assert summary and summary["memory_version"] == 2
    assert summary["content"]["dialogue_digest"][-1]["text"] == "闭合回答 7"
    async with env["db"].tx(env["tenant"]) as conn:
        await env["db"]._append_message(
            conn, env["tenant"], conversation_id, "user", "尚未闭合的问题"
        )
        await env["db"]._append_message(
            conn, env["tenant"], conversation_id, "assistant", "处理中", "running"
        )
    unchanged, result = await env["service"].memory.compactor.compact(env["tenant"], conversation_id)
    assert unchanged["id"] == summary["id"] and result["status"] == "noop"
    prepared = await env["service"]._prepare_request(
        env["tenant"],
        CreateRun(
            brief=ResearchBrief(question="继续研究并使用此前闭合对话摘要。"),
            project_id=project["id"],
            conversation_id=conversation_id,
        ),
    )
    assert prepared.context_snapshot["summary_id"] == str(summary["id"])
    assert prepared.brief.memory_context["summary"]
    assert len(prepared.brief.memory_context["recent"]) == 12
    assert all("尚未闭合的问题" not in item for item in prepared.brief.memory_context["recent"])
    checkpoint = await ConversationSaver(env["db"], env["tenant"], conversation_id).load()
    assert checkpoint["summary_id"] == str(summary["id"])
    assert checkpoint["read_memory_ids"] == [
        item["id"] for item in prepared.context_snapshot["entries"]
    ]


async def test_observation_governance_jobs_scope_and_context(env):
    client = env["client"]
    project = await create_project(client)
    other_project = await create_project(client)
    candidate = await client.post(
        f"/projects/{project['id']}/observations",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "memory_type": "procedural",
            "scope": "project",
            "summary": "先查原始论文",
            "body": "检索命中后必须读取原文范围，再形成 claim。",
            "status": "candidate",
        },
    )
    assert candidate.status_code == 201, candidate.text
    candidate_id = candidate.json()["id"]
    context = await env["db"].project_context(
        env["tenant"], project["id"], query="原始论文"
    )
    assert not any(str(item["id"]) == candidate_id for item in context["observations"])
    confirmed = await client.patch(
        f"/projects/{project['id']}/observations/{candidate_id}",
        json={"status": "confirmed"},
    )
    assert confirmed.json()["status"] == "confirmed"
    context = await env["db"].project_context(
        env["tenant"], project["id"], query="原始论文"
    )
    assert any(str(item["id"]) == candidate_id for item in context["observations"])
    assert not (await env["db"].project_context(
        env["tenant"], other_project["id"], query="原始论文"
    ))["observations"]

    edited = await client.patch(
        f"/projects/{project['id']}/observations/{candidate_id}",
        json={"body": "必须先读取原文证据范围，再形成 claim。"},
    )
    assert edited.status_code == 200
    assert edited.json()["supersedes_id"] == candidate_id
    rows = (await client.get(f"/projects/{project['id']}/observations")).json()
    replacement = next(item for item in rows if item["id"] == edited.json()["id"])
    assert replacement["evidence"] == [{"type": "user", "id": env["tenant"]}]
    assert any(item["relation"] == "supersedes" for item in replacement["relations"])

    global_observation = await client.post(
        f"/projects/{project['id']}/observations",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "memory_type": "semantic",
            "scope": "user_global",
            "summary": "报告偏好",
            "body": "报告必须先给结论。",
            "status": "confirmed",
        },
    )
    other_context = await env["db"].project_context(
        env["tenant"], other_project["id"], query="报告偏好"
    )
    assert any(
        str(item["id"]) == global_observation.json()["id"]
        for item in other_context["observations"]
    )
    link_material = await env["db"].observation_link_material(
        env["tenant"], project["id"], global_observation.json()["id"]
    )
    assert all(item["scope"] == "user_global" for item in link_material["candidates"])
    assert (
        await client.get(
            f"/projects/{project['id']}/observations",
            headers={"Authorization": "Bearer " + env["other_key"]},
        )
    ).status_code == 404

    memory_claim = await env["db"].claim_memory_job()
    assert memory_claim
    await MemoryJobRunner(env["db"]).execute(memory_claim["tenant"], memory_claim["job_id"])
    job = await client.get(f"/memory-jobs/{memory_claim['job_id']}")
    assert job.json()["status"] == "completed"
    first_job = await env["db"].enqueue_memory_job(
        env["tenant"], project["id"], None, "embedding_backfill", "test", "same-source",
        "same-digest", {"object_type": "observation", "object_id": candidate_id, "text": "x"},
    )
    repeated_job = await env["db"].enqueue_memory_job(
        env["tenant"], project["id"], None, "embedding_backfill", "test", "same-source",
        "same-digest", {"object_type": "observation", "object_id": candidate_id, "text": "x"},
    )
    assert repeated_job["id"] == first_job["id"]


async def test_profile_source_and_sensitive_attribute_guards(env):
    client = env["client"]
    inferred = await client.post(
        "/users/me/research-profile/signals",
        headers={"Idempotency-Key": str(uuid4())},
        json={"field": "method", "value": "inferred", "source": "inferred"},
    )
    assert inferred.status_code == 422
    sensitive = await client.post(
        "/users/me/research-profile/signals",
        headers={"Idempotency-Key": str(uuid4())},
        json={"field": "religion", "value": "private", "source": "explicit"},
    )
    assert sensitive.status_code == 409
    signal = await client.post(
        "/users/me/research-profile/signals",
        headers={"Idempotency-Key": str(uuid4())},
        json={"field": "method", "value": "system design", "source": "explicit"},
    )
    rejected = await client.delete(f"/users/me/research-profile/signals/{signal.json()['id']}")
    assert rejected.status_code == 204
    loaded = await client.get("/users/me/research-profile")
    item = next(row for row in loaded.json()["signals"] if row["id"] == signal.json()["id"])
    assert item["state"] == "rejected" and item["rejected_until"]


async def test_expired_project_cleanup_removes_database_graph(env):
    client = env["client"]
    project = await create_project(client)
    project_id = project["id"]
    conversation_id = (await client.get(f"/projects/{project_id}/conversations")).json()[0]["id"]
    await client.post(
        f"/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": str(uuid4())},
        json={"content": "创建一个随后会进入清理窗口的研究任务。"},
    )
    await client.delete(f"/projects/{project_id}")
    async with env["db"].tx(env["tenant"]) as conn:
        await conn.execute(
            "UPDATE projects SET purge_after=now()-interval '1 second' WHERE id=%s", (project_id,)
        )
    purged = await env["db"].purge_expired_projects(env["tenant"])
    assert [item["project_id"] for item in purged] == [project_id]
    async with env["db"].tx(env["tenant"]) as conn:
        row = await (await conn.execute("SELECT id FROM projects WHERE id=%s", (project_id,))).fetchone()
    assert row is None


async def test_digest_schedule_and_notification_are_idempotent(env):
    client = env["client"]
    project = await create_project(client)
    subscription = await client.post(
        f"/projects/{project['id']}/subscriptions",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "topic": "reliable weekly systems",
            "timezone": "America/New_York",
            "allow_needs_review_delivery": True,
        },
    )
    first = await client.post(
        f"/subscriptions/{subscription.json()['id']}/run",
        headers={"Idempotency-Key": str(uuid4())},
    )
    second = await client.post(
        f"/subscriptions/{subscription.json()['id']}/run",
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.json()["created"] is False
    async with env["db"].tx(env["tenant"]) as conn:
        run_row = await (
            await conn.execute(
                "SELECT context_snapshot FROM research_runs WHERE id=%s", (first.json()["run_id"],)
            )
        ).fetchone()
    assert run_row["context_snapshot"]["weekly_budget_allocation"] == {
        "planning": 0.10,
        "screening": 0.20,
        "deep_reading": 0.40,
        "review": 0.15,
        "writing_revision": 0.15,
    }
    claim = await env["db"].claim(expected=first.json()["run_id"])
    await execute_claim(env["db"], env["store"], claim)
    notifications = (await client.get("/notifications")).json()
    assert len(notifications) == 1
    assert notifications[0]["read_at"] is None
    marked = await client.post(f"/notifications/{notifications[0]['id']}/read")
    assert marked.status_code == 200 and marked.json()["read_at"]
    assert (await client.get("/notifications", params={"unread_only": True})).json() == []


async def test_cursor_pagination_and_cross_type_search_fairness(env):
    client = env["client"]
    first_project = await create_project(client)
    await create_project(client)
    first_page = await client.get("/projects", params={"limit": 1})
    assert len(first_page.json()) == 1 and first_page.headers.get("x-next-cursor")
    second_page = await client.get(
        "/projects", params={"limit": 1, "cursor": first_page.headers["x-next-cursor"]}
    )
    assert len(second_page.json()) == 1
    assert second_page.json()[0]["id"] != first_page.json()[0]["id"]

    project_id = first_project["id"]
    conversation_id = (await client.get(f"/projects/{project_id}/conversations")).json()[0]["id"]
    async with env["db"].tx(env["tenant"]) as conn:
        for index in range(105):
            await env["db"]._append_message(
                conn, env["tenant"], conversation_id, "user", f"fairnessneedle message {index}"
            )
    await client.post(
        f"/projects/{project_id}/memories",
        headers={"Idempotency-Key": str(uuid4())},
        json={"type": "constraint", "content": "fairnessneedle memory", "status": "confirmed"},
    )
    upload = await client.post(
        "/uploads",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("fairness.md", b"fairnessneedle artifact", "text/markdown")},
    )
    await client.post(
        f"/projects/{project_id}/artifacts",
        headers={"Idempotency-Key": str(uuid4())},
        json={"source_version_id": upload.json()["source"]["id"], "title": "fairnessneedle artifact"},
    )
    search = await client.post(
        f"/projects/{project_id}/search",
        json={"query": "fairnessneedle", "types": ["message", "memory", "artifact"], "limit": 6},
    )
    assert {item["type"] for item in search.json()["results"]} >= {"message", "memory", "artifact"}


async def test_memory_relations_cannot_cross_projects(env):
    client = env["client"]
    projects = [await create_project(client), await create_project(client)]
    memories = []
    for project in projects:
        memories.append(
            (
                await client.post(
                    f"/projects/{project['id']}/memories",
                    headers={"Idempotency-Key": str(uuid4())},
                    json={"type": "decision", "content": f"decision for {project['id']}"},
                )
            ).json()
        )
    response = await client.patch(
        f"/projects/{projects[0]['id']}/memories/{memories[0]['id']}",
        json={"conflicts_with_id": memories[1]["id"]},
    )
    assert response.status_code == 404
