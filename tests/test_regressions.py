import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from research_agent.contracts import CreateRun, ReportDraft, ResearchBrief, RunProfile
from research_agent.db import StaleLease
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
