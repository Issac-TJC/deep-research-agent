import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from research_agent.contracts import CreateRun, ReportDraft, ResearchBrief, ResearchTask, RunProfile
from research_agent.db import BudgetExceeded, StaleLease
from research_agent.fetch import UnsafeURL, fetch_public
from research_agent.fixtures import FixtureProvider
from research_agent.gateway import Gateway
from research_agent.graph import ResearchEngine
from research_agent.providers import DeepSeekProvider, ProviderError
from research_agent.settings import Settings
from research_agent.worker import execute_claim


async def test_provider_429_is_retryable_without_unknown_charge(monkeypatch):
    async def post(*args, **kwargs):
        return httpx.Response(429, json={"error": "limited"})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ProviderError) as error:
        await DeepSeekProvider(Settings(deepseek_api_key="test-only", _env_file=None)).complete([])
    assert error.value.retryable and not error.value.unknown
    assert str(error.value) == "model_http_429"


async def test_fetch_pins_dns_and_rejects_private_redirect(monkeypatch):
    import research_agent.fetch as fetch

    seen = []

    async def dns(host, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        seen.append((url, kwargs))
        yield httpx.Response(302, headers={"location": "http://localhost/secret"})

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    monkeypatch.setattr(fetch.httpx.AsyncClient, "stream", stream)
    with pytest.raises(UnsafeURL, match="local hostname"):
        await fetch_public("https://example.com/page", 1000)
    assert len(seen) == 1
    assert seen[0][0] == "https://93.184.216.34:443/page"
    assert seen[0][1]["headers"]["Host"] == "example.com"
    assert seen[0][1]["extensions"]["sni_hostname"] == "example.com"


async def setup_engine(env):
    request = CreateRun(brief=ResearchBrief(question="Compare retrieval under explicit constraints"))
    run, _ = await env["service"].create(env["tenant"], request, str(uuid4()))
    lease = await env["db"].claim(str(run["id"]))
    return ResearchEngine(env["db"], env["store"], env["tenant"], run, lease["token"])


class _Snapshot:
    values = {}


class _ResearcherGraph:
    def __init__(self, failure=None):
        self.failure = failure
        self.initial = []

    async def aget_state(self, config):
        return _Snapshot()

    async def ainvoke(self, initial, config):
        self.initial.append(initial)
        if self.failure:
            raise self.failure


def review_state(task, *, allowed_method="source_synthesis"):
    intent = {
        "normalized_question": "Compare retrieval under explicit constraints",
        "mode": "research_design",
        "stages": [
            {
                "stage": "experiment",
                "role": "deliverable",
                "objective": "Design a valid evaluation",
                "methods": [allowed_method],
                "deliverable": "Experiment plan",
                "reason": "Required by the brief",
            }
        ],
    }
    return {
        "intent": intent,
        "plan": {"intent": intent, "tasks": [task], "questions": ["q"], "rationale": "r"},
        "tasks": [task],
        "gap_round": 0,
        "revision": 0,
    }


@pytest.mark.integration
async def test_research_budget_exhaustion_defers_dependents_and_reaches_writer(env):
    engine = await setup_engine(env)
    first = ResearchTask(
        id="budgeted-task",
        question_id="q1",
        objective="Collect evidence",
        query="evidence",
        acceptance_criteria=["one claim"],
    )
    dependent = ResearchTask(
        id="dependent-task",
        question_id="q2",
        objective="Design the evaluation",
        query="evaluation",
        acceptance_criteria=["protocol"],
        depends_on=[first.id],
        stage="experiment",
    )
    for task in (first, dependent):
        await engine.put("task", task.id, task.model_dump(mode="json"), immutable=False)
    graph = _ResearcherGraph(BudgetExceeded("soft_target:research_extraction"))
    engine.researcher_graph = lambda: graph

    result = await engine.research(
        {"tasks": [first.model_dump(mode="json"), dependent.model_dump(mode="json")], "gap_round": 0}
    )

    tasks = {task["id"]: task for task in result["tasks"]}
    assert result["stop_reason"] == "budget:soft_target:research_extraction"
    assert result["research_budget_exhausted"] is True
    assert tasks[first.id]["execution_status"] == "failed"
    assert tasks[dependent.id]["execution_status"] == "cancelled"
    assert tasks[dependent.id]["stop_reason"].startswith("deferred_after_budget:")
    assert engine.route_review({**result, "review": {"gap_tasks": [{}]}}) == "write"
    assert engine.route_write({**result, "report": {"title": "bounded"}}) == "publish"
    events = await engine.db.events(engine.tenant, engine.run_id, 0)
    assert any(event["event_type"] == "research.budget_exhausted" for event in events)


@pytest.mark.integration
async def test_exhausted_retrieval_quota_falls_back_without_ending_research(env):
    engine = await setup_engine(env)
    engine.profile = engine.profile.model_copy(update={"max_retrieval_calls": 0})
    task = ResearchTask(
        id="retrieval-task",
        question_id="q1",
        objective="Use already-authorized evidence",
        query="evidence",
        acceptance_criteria=["read source"],
        source_ids=["authorized-source"],
    )
    await engine.put("task", task.id, task.model_dump(mode="json"), immutable=False)
    graph = _ResearcherGraph()
    engine.researcher_graph = lambda: graph

    result = await engine.research({"tasks": [task.model_dump(mode="json")], "gap_round": 0})

    assert "stop_reason" not in result
    assert result["tasks"][0]["execution_status"] == "completed"
    assert graph.initial[0]["retrieval_hits"] == []
    warnings = await engine.db.records(engine.tenant, engine.run_id, "warning")
    assert warnings[-1]["reason"] == "retrieval_budget_exhausted"


@pytest.mark.integration
async def test_writer_budget_failure_produces_structured_degraded_report(env):
    engine = await setup_engine(env)
    task = ResearchTask(
        id="unfinished-deliverable",
        question_id="q1",
        objective="Design a reproducible experiment",
        query="experiment",
        acceptance_criteria=["datasets", "baselines", "metrics"],
        stage="experiment",
        execution_status="cancelled",
        stop_reason="deferred_after_budget:soft_target:research_extraction",
    ).model_dump(mode="json")
    await engine.put("task", task["id"], task, immutable=False)

    async def out_of_budget(*args, **kwargs):
        raise BudgetExceeded("soft_target:writer_patch")

    engine.gateway.structured = out_of_budget
    result = await engine.write(
        {
            "tasks": [task],
            "plan": {"tasks": [task]},
            "review": {"accepted_claim_ids": [], "findings": [], "sufficient": False},
            "stop_reason": "budget:soft_target:research_extraction",
            "research_budget_exhausted": True,
        }
    )

    report = result["report"]
    assert report["title"] == "阶段性研究报告"
    assert report["nodes"][0]["id"] == "research-status"
    assert any(node["stage"] == "experiment" for node in report["nodes"])
    assert all(node["id"] != "partial" for node in report["nodes"])
    events = await engine.db.events(engine.tenant, engine.run_id, 0)
    assert any(event["event_type"] == "writer.degraded" for event in events)


@pytest.mark.integration
async def test_reviewer_budget_failure_routes_provisional_evidence_to_writer(env):
    engine = await setup_engine(env)
    task = ResearchTask(
        id="review-budget-task",
        question_id="q1",
        objective="Review available evidence",
        query="evidence",
        acceptance_criteria=["coverage"],
        execution_status="completed",
    ).model_dump(mode="json")
    await engine.put("task", task["id"], task, immutable=False)

    async def out_of_budget(*args, **kwargs):
        raise BudgetExceeded("soft_target:review_gap")

    engine.gateway.structured = out_of_budget
    result = await engine.review(
        {
            "tasks": [task],
            "plan": {"tasks": [task], "questions": ["q1"]},
            "gap_round": 0,
            "revision": 0,
        }
    )

    assert result["review"]["sufficient"] is False
    assert result["stop_reason"] == "budget:soft_target:review_gap"
    assert result["research_budget_exhausted"] is True
    assert engine.route_review(result) == "write"
    events = await engine.db.events(engine.tenant, engine.run_id, 0)
    assert any(event["event_type"] == "review.degraded" for event in events)


@pytest.mark.integration
async def test_invalid_gap_task_is_rejected_and_routes_to_writer(env):
    engine = await setup_engine(env)
    task = ResearchTask.model_validate(
        {
            "id": "existing-task",
            "question_id": "q0",
            "objective": "Design a valid evaluation",
            "query": "evaluation",
            "acceptance_criteria": ["protocol"],
            "stage": "experiment",
            "method": "source_synthesis",
        }
    ).model_dump(mode="json")
    await engine.put("task", task["id"], task, immutable=False)

    async def invalid_review(*args, **kwargs):
        return {
            "sufficient": False,
            "accepted_claim_ids": [],
            "covered_question_ids": [],
            "gap_tasks": [
                {
                    **task,
                    "id": "invalid-gap",
                    "method": "literature_search",
                }
            ],
        }

    engine.gateway.structured = invalid_review
    state = review_state(task)
    result = await engine.review(state)
    assert result["review"]["gap_tasks"] == []
    assert any("gap_task_outside_research_intent" in f["reason"] for f in result["review"]["findings"])
    assert engine.route_review({**state, **result}) == "write"
    events = await engine.db.events(engine.tenant, engine.run_id, 0)
    assert any(event["event_type"] == "review.gap_rejected" for event in events)


@pytest.mark.integration
async def test_legal_gap_task_is_preserved(env):
    engine = await setup_engine(env)
    task = ResearchTask.model_validate(
        {
            "id": "existing-task",
            "question_id": "q0",
            "objective": "Design a valid evaluation",
            "query": "evaluation",
            "acceptance_criteria": ["protocol"],
            "stage": "experiment",
            "method": "source_synthesis",
        }
    ).model_dump(mode="json")
    await engine.put("task", task["id"], task, immutable=False)

    async def legal_review(*args, **kwargs):
        return {
            "sufficient": False,
            "accepted_claim_ids": [],
            "covered_question_ids": [],
            "gap_tasks": [{**task, "id": "legal-gap"}],
        }

    engine.gateway.structured = legal_review
    state = review_state(task)
    result = await engine.review(state)
    assert [item["id"] for item in result["review"]["gap_tasks"]] == ["legal-gap"]
    assert engine.route_review({**state, **result}) == "gaps"


@pytest.mark.integration
async def test_cancel_rejects_actual_inflight_call_result(env):
    engine = await setup_engine(env)
    gateway = Gateway(
        env["db"], env["tenant"], engine.run_id, engine.fence, RunProfile(), FixtureProvider(), "fixture"
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def external_call():
        started.set()
        await release.wait()
        return {"should_not_commit": True}, {"usd": 0.01}

    pending = asyncio.create_task(gateway._invoke("model", "late-result", external_call, 0.02, 10))
    await started.wait()
    await env["db"].cancel(env["tenant"], engine.run_id)
    release.set()
    with pytest.raises(StaleLease):
        await pending
    action = await env["db"].action(env["tenant"], engine.run_id, "late-result:attempt:0")
    assert action["result"] is None and action["status"] != "completed"
    run = await env["db"].run(env["tenant"], engine.run_id)
    assert run["status"] == "cancelled" and float(run["reserved_usd"]) == 0.02


@pytest.mark.integration
async def test_local_patch_preserves_other_nodes_and_immutable_draft(env):
    engine = await setup_engine(env)
    report = ReportDraft(
        title="Draft",
        nodes=[
            {"id": "keep", "kind": "paragraph", "text": "Untouched", "attribution": "guidance"},
            {"id": "fix", "kind": "paragraph", "text": "Unsupported", "attribution": "guidance"},
        ],
    )
    await engine.put("draft", engine.run_id + ":draft:1", report.model_dump(mode="json"))
    state = {
        "report": report.model_dump(mode="json"),
        "revision": 1,
        "review": {"findings": [{"location": "fix", "severity": "error"}]},
    }

    async def patch(*args, **kwargs):
        return {
            "replacements": [
                {"id": "fix", "kind": "paragraph", "text": "Evidence missing", "attribution": "guidance"}
            ]
        }

    engine.gateway.structured = patch
    result = await engine.patch(state)
    assert result["report"]["nodes"][0] == state["report"]["nodes"][0]
    assert result["revision"] == 2 and result["report"]["nodes"][1]["text"] == "Evidence missing"
    first = await env["db"].get(env["tenant"], engine.run_id + ":draft:1", "draft")
    assert first["nodes"][1]["text"] == "Unsupported"
    state.update({"patch_round": 1, "revision": 2})
    state["review"]["findings"] = [{"location": "keep", "severity": "error"}]
    with pytest.raises(ValueError, match="outside_authorized"):
        await engine.patch(state)


@pytest.mark.integration
async def test_comparison_cell_cannot_bypass_accepted_claim_gate(env):
    engine = await setup_engine(env)
    state = {
        "report": {
            "title": "Comparison",
            "nodes": [
                {
                    "id": "matrix",
                    "kind": "comparison_table",
                    "rows": [{"latency": "99ms"}],
                    "cell_claim_ids": {"0.latency": ["invented-claim"]},
                }
            ],
        },
        "revision": 1,
        "review": {"sufficient": True, "accepted_claim_ids": []},
    }
    await engine.publish(state)
    package = await env["service"].report(env["tenant"], engine.run_id)
    assert package.quality_status == "needs_review"
    assert "matrix: references unaccepted claims" in package.citation_errors
    assert "matrix: unknown table cell claim reference" in package.citation_errors


@pytest.mark.integration
async def test_structural_gate_routes_to_patch_when_model_review_misses_cell_refs(env):
    engine = await setup_engine(env)
    state = {
        "report": {
            "title": "Missing cell citation",
            "nodes": [{"id": "matrix", "kind": "comparison_table", "rows": [{"latency": "99ms"}]}],
        },
        "revision": 1,
        "plan": {"tasks": []},
        "tasks": [],
    }

    async def optimistic_review(*args, **kwargs):
        return {"sufficient": True, "findings": [], "accepted_claim_ids": []}

    engine.gateway.structured = optimistic_review
    checked = await engine.review(state)
    defects = checked["review"]["findings"]
    assert any(f["location"] == "matrix" and "cell lacks evidence" in f["reason"] for f in defects)
    assert engine.route_review({**state, **checked}) == "patch"

    async def repair(*args, **kwargs):
        return {
            "replacements": [{"id": "matrix", "kind": "comparison_table", "rows": [{"latency": "unknown"}]}]
        }

    engine.gateway.structured = repair
    patched = await engine.patch({**state, **checked})
    _, _, _, _, remaining = await engine.report_checks({**state, **checked, **patched})
    assert not any(error.startswith("matrix:") for error in remaining)
    assert "no valid evidence collected" in remaining  # A structural repair does not fabricate evidence.


@pytest.mark.integration
async def test_large_evidence_context_selects_linked_bundles_without_losing_brief(env):
    from research_agent.context import token_upper_bound

    engine = await setup_engine(env)
    engine.profile = engine.profile.model_copy(update={"prompt_token_limit": 22000})
    spans = [{"id": f"span-{i}", "quote": "原文证据。" * 150} for i in range(8)]
    claims = [{"id": f"claim-{i}", "text": "Atomic statement", "span_ids": [f"span-{i}"]} for i in range(8)]
    result = await engine.evidence_context("writer", "large-evidence", {"claims": claims, "spans": spans})
    assert 0 < len(result["claims"]) < len(claims)
    assert result["brief"] == engine.brief.model_dump(mode="json")
    assert result["omitted_artifact_ids"]
    assert token_upper_bound(result) <= engine.profile.prompt_token_limit - 12000
    assert {sid for c in result["claims"] for sid in c["span_ids"]} == {s["id"] for s in result["spans"]}
    snapshots = await env["db"].records(env["tenant"], engine.run_id, "context")
    assert snapshots[-1]["truncated"] and snapshots[-1]["constraints_hash"]


@pytest.mark.integration
async def test_search_burst_does_not_discard_already_read_evidence(env, monkeypatch):
    original = FixtureProvider.complete

    async def burst(self, messages, **kwargs):
        completion = await original(self, messages, **kwargs)
        calls = completion.message.get("tool_calls", [])
        if calls and calls[0]["function"]["name"] == "read_source":
            completion.message["tool_calls"] += [
                {
                    "id": f"extra-search-{i}",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": json.dumps({"query": "unnecessary search"}),
                    },
                }
                for i in range(4)
            ]
        return completion

    monkeypatch.setattr(FixtureProvider, "complete", burst)
    request = CreateRun(
        brief=ResearchBrief(
            question="Compare retrieval with scarce search budget",
            source_urls=["https://fixtures.invalid/lexical"],
        ),
        profile=RunProfile(max_search_calls=1, max_task_search_calls=1, max_gap_rounds=0),
    )
    run, _ = await env["service"].create(env["tenant"], request, str(uuid4()))
    rid = str(run["id"])
    await execute_claim(env["db"], env["store"], await env["db"].claim(rid))
    package = await env["service"].report(env["tenant"], rid)
    assert package.claims and package.spans
    assert await env["db"].records(env["tenant"], rid, "tool_rejection")
    assert (await env["db"].run(env["tenant"], rid))["search_calls"] == 1
    tasks = await env["db"].records(env["tenant"], rid, "task")
    assert all(t["execution_status"] == "completed" for t in tasks)


@pytest.mark.integration
@pytest.mark.parametrize("finish_reason", ["length", "stop"])
async def test_structured_repair_is_diagnosable_and_rebudgets_added_context(env, finish_reason):
    from research_agent.contracts import ModelUsage
    from research_agent.providers import Completion

    seen = []

    class Repairable:
        async def complete(self, messages, **kwargs):
            seen.append(list(messages))
            if len(seen) == 1:
                return Completion(
                    {"role": "assistant", "content": '{"broken": "' + "x" * 2000},
                    ModelUsage(input_tokens=10, output_tokens=10),
                    finish_reason,
                    "injected",
                )
            value = {
                "title": "Repaired",
                "nodes": [{"id": "one", "kind": "paragraph", "text": "unknown", "attribution": "guidance"}],
            }
            return Completion(
                {"role": "assistant", "content": json.dumps(value)},
                ModelUsage(input_tokens=20, output_tokens=20),
                "stop",
                "injected",
            )

    engine = await setup_engine(env)
    engine.gateway.provider = Repairable()
    value = await engine.gateway.structured("writer", "repair", {}, ReportDraft, closing=True)
    assert value["title"] == "Repaired" and len(seen) == 2
    assert "Repair the response" in seen[1][-1]["content"]
    first = await env["db"].action(env["tenant"], engine.run_id, "repair:attempt:0")
    second = await env["db"].action(env["tenant"], engine.run_id, "repair:attempt:1")
    assert first["status"] == "failed" and first["result"]["message"]["content"].startswith('{"broken"')
    assert first["usage"]["diagnostic"]["finish_reason"] == finish_reason
    assert second["reserved_tokens"] > first["reserved_tokens"]
    assert await env["db"].action(env["other"], engine.run_id, "repair:attempt:0") is None
