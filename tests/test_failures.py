import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from research_agent.contracts import CreateRun, ResearchBrief, RunProfile
from research_agent.db import Conflict
from research_agent.fixtures import FixtureProvider
from research_agent.gateway import Gateway
from research_agent.providers import Completion, ProviderError
from research_agent.worker import execute_claim

pytestmark = pytest.mark.integration


async def new_run(env, **kwargs):
    req = CreateRun(
        brief=ResearchBrief(
            question="Compare lexical and semantic retrieval under explicit constraints",
            template="technical_comparison",
        ),
        profile=RunProfile(**kwargs),
    )
    run, _ = await env["service"].create(env["tenant"], req, str(uuid4()))
    return str(run["id"])


async def test_provider_timeout_retries_keep_unknown_reservation(env):
    rid = await new_run(env)
    claim = await env["db"].claim(rid)
    gateway = Gateway(
        env["db"], env["tenant"], rid, claim["token"], RunProfile(max_retries=1), FixtureProvider(), "fixture"
    )
    attempts = 0

    async def failing():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("injected_timeout", unknown=True)
        return {"ok": True}, {"usd": 0.01}

    assert await gateway._invoke("model", "timeout", failing, 0.02, 10) == {"ok": True}
    state = await env["db"].run(env["tenant"], rid)
    assert float(state["spent_usd"]) == 0.01 and float(state["reserved_usd"]) == 0.02
    assert state["model_calls"] == 2


async def test_invalid_structured_response_charges_actual_usage(env):
    from research_agent.contracts import ModelUsage, Plan

    class Invalid:
        async def complete(self, *args, **kwargs):
            return Completion(
                {"role": "assistant", "content": "{not valid json"},
                ModelUsage(input_tokens=10, output_tokens=5, usd=0.001),
                "stop",
                "injected",
            )

    rid = await new_run(env)
    claim = await env["db"].claim(rid)
    gateway = Gateway(
        env["db"], env["tenant"], rid, claim["token"], RunProfile(max_retries=1), Invalid(), "fixture"
    )
    with pytest.raises(ProviderError, match="attempts_exhausted"):
        await gateway.structured("planner", "bad-json", {}, Plan)
    state = await env["db"].run(env["tenant"], rid)
    assert state["model_calls"] == 2 and float(state["spent_usd"]) == 0.002 and state["reserved_tokens"] == 0


async def test_tenant_write_policy_rejects_cross_tenant_row(env):
    async with env["db"].tx(env["tenant"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(
                "INSERT INTO records(tenant_id,id,kind,data) VALUES (%s,'forbidden','source','{}')",
                (env["other"],),
            )


async def test_queue_and_cumulative_limits(env):
    for _ in range(10):
        await new_run(env)
    with pytest.raises(Conflict, match="queue is full"):
        await new_run(env)


async def test_execution_configuration_is_frozen_and_version_mismatch_interrupts(env):
    rid = await new_run(env)
    run = await env["db"].run(env["tenant"], rid)
    assert run["configuration"]["settings"]["deepseek_model"] == env["settings"].deepseek_model
    assert "deepseek_api_key" not in run["configuration"]["settings"]
    with psycopg.connect(env["settings"].admin_database_url) as conn:
        conn.execute(
            "UPDATE research_runs SET configuration=jsonb_set(configuration,'{runtime_fingerprint}', '\"incompatible-build\"') WHERE id=%s",
            (rid,),
        )
    claim = await env["db"].claim(rid)
    await execute_claim(env["db"], env["store"], claim)
    final = await env["db"].run(env["tenant"], rid)
    assert final["status"] == "interrupted" and final["stop_reason"] == "runtime_version_mismatch"
    assert final["model_calls"] == 0 and final["tool_calls"] == 0


@pytest.mark.parametrize("stage", ["after_fetch", "before_draft_commit"])
async def test_real_worker_process_kill_and_resume(env, tmp_path, stage):
    rid = await new_run(env)
    marker = tmp_path / "worker-boundary"
    script = r"""
import asyncio, os
from pathlib import Path
from research_agent.gateway import Gateway
from research_agent.settings import Settings
from research_agent.worker import work
original_tool, original_structured = Gateway.tool, Gateway.structured
async def tool(self, kind, key, operation, task_id=None):
    value = await original_tool(self, kind, key, operation, task_id)
    if kind == 'fetch' and os.environ['KILL_STAGE'] == 'after_fetch':
        Path(os.environ['KILL_MARKER']).write_text(key)
        await asyncio.sleep(300)
    return value
async def structured(self, role, key, payload, schema, closing=False):
    value = await original_structured(self, role, key, payload, schema, closing)
    if role == 'writer' and os.environ['KILL_STAGE'] == 'before_draft_commit':
        Path(os.environ['KILL_MARKER']).write_text(key)
        await asyncio.sleep(300)
    return value
Gateway.tool, Gateway.structured = tool, structured
asyncio.run(work(Settings(research_mode='fixture'), once=True))
"""
    process_env = {**os.environ, "RESEARCH_MODE": "fixture", "KILL_STAGE": stage, "KILL_MARKER": str(marker)}
    log = (tmp_path / "worker.log").open("w")
    process = subprocess.Popen([sys.executable, "-c", script], env=process_env, stdout=log, stderr=log)
    try:
        for _ in range(200):
            if marker.exists() or process.poll() is not None:
                break
            await asyncio.sleep(0.1)
        assert marker.exists(), (tmp_path / "worker.log").read_text()
        before = await env["db"].run(env["tenant"], rid)
        process.kill()
        process.wait(timeout=5)
        with psycopg.connect(env["settings"].admin_database_url) as conn:
            # Inject expiry rather than wait 45 seconds; the subprocess itself was actually SIGKILLed.
            conn.execute("UPDATE research_runs SET lease_until=now()-interval '1 second' WHERE id=%s", (rid,))
        reclaimed = await env["db"].claim(rid)
        assert reclaimed["token"] > before["fence"]
        await execute_claim(env["db"], env["store"], reclaimed)
        final = await env["db"].run(env["tenant"], rid)
        assert final["status"] == "completed"
        assert final["tokens"] >= before["tokens"]
        calls = await env["db"].usage(env["tenant"], rid)
        assert len([c for c in calls if c["id"] == "plan:attempt:0"]) == 1
        if stage == "before_draft_commit":
            assert len([c for c in calls if c["id"].startswith("draft:attempt:")]) == 1
        package = await env["service"].report(env["tenant"], rid)
        assert package.spans and not package.citation_errors
        evidence = {
            "scenario": stage,
            "mode": "fixture",
            "real_process_killed": True,
            "lease_expiry_injected": True,
            "run_id": rid,
            "fence_before": before["fence"],
            "fence_after": final["fence"],
            "tokens_before": before["tokens"],
            "tokens_after": final["tokens"],
            "status": final["status"],
        }
        Path("artifacts/recovery").mkdir(parents=True, exist_ok=True)
        Path(f"artifacts/recovery/{stage}.json").write_text(json.dumps(evidence, indent=2))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        log.close()
