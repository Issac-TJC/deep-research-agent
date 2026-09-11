"""Frozen-corpus experiments and descriptive metrics. No synthetic quality scores or silent live costs."""

import json
import re
import time
from pathlib import Path
from uuid import uuid4

from research_agent.contracts import CreateRun, EvaluationResult, ResearchBrief, RunProfile
from research_agent.db import Database
from research_agent.evidence import EvidenceService
from research_agent.service import ResearchService
from research_agent.storage import ObjectStore
from research_agent.worker import execute_claim


async def frozen_search(evidence, sources, query):
    """Deterministic lexical search over a fixed authorized corpus; same tool for every arm."""
    terms = set(re.findall(r"[\w]+", query.lower()))
    ranked = []
    for source in sources:
        _, document = await evidence.document(source.id)
        text = document.text.lower()
        score = sum(text.count(term) for term in terms)
        ranked.append(
            (
                score,
                source.id,
                {
                    "source_id": source.id,
                    "url": source.url,
                    "title": source.title,
                    "snippet": document.text[:800],
                },
            )
        )
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return {"results": [row[2] for row in ranked[:5]], "evidence": False, "search_policy": "frozen"}


def measure(run, package, calls, contexts, reviews):
    cited = [n for n in package.report.nodes if n.attribution == "source_statement" and (n.text or n.rows)]
    usage = [c["usage"] or {} for c in calls]
    all_tokens = sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in usage)
    latest_review = reviews[-1] if reviews else {}
    return {
        "execution_status": run["status"],
        "quality_status": package.quality_status,
        "sources": len(package.sources),
        "claims": len(package.claims),
        "spans": len(package.spans),
        "report_gate_errors": len(package.citation_errors),
        "citation_locator_errors": sum(
            any(
                marker in error
                for marker in (
                    "span version/offset mismatch",
                    "quote hash mismatch",
                    "source cannot be verified",
                )
            )
            for error in package.citation_errors
        ),
        "factual_nodes": len(cited),
        "factual_nodes_with_refs": sum(bool(n.claim_ids) for n in cited),
        "covered_question_ids": latest_review.get("covered_question_ids", []),
        "unresolved_count": len(package.report.unresolved),
        "model_calls": run["model_calls"],
        "tool_calls": run["tool_calls"],
        "search_calls": run["search_calls"],
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "total_tokens": all_tokens,
        "cache_hit_tokens": sum(u.get("cache_hit_tokens", 0) for u in usage),
        "cache_miss_tokens": sum(u.get("cache_miss_tokens", 0) for u in usage),
        "spent_usd": float(run["spent_usd"]),
        "unresolved_reserved_usd": float(run["reserved_usd"]),
        "retries": sum(":attempt:0" not in c["id"] for c in calls),
        "failed_or_unknown_calls": sum(c["status"] != "completed" for c in calls),
        "context_snapshots": len(contexts),
        "contexts_with_omissions": sum(c["truncated"] for c in contexts),
        "context_omission_rate": sum(c["truncated"] for c in contexts) / len(contexts) if contexts else None,
        "context_tokens_before_estimated": sum(c["estimated_tokens_before"] for c in contexts),
        "context_tokens_after_estimated": sum(c["estimated_tokens_after"] for c in contexts),
        "human_citation_precision": None,
        "human_factual_support_rate": None,
        "paper_explanation_fidelity": None,
        "research_idea_actionability": None,
        "quality_metrics_note": "Reference presence is not entailment. Human scores remain null until reviewed.",
    }


async def evaluate_suite(
    settings,
    api_key,
    split,
    variant,
    limit,
    repeats,
    live,
    output,
    concurrency,
    context,
    corpus_manifest: Path | None = None,
):
    if split not in {"dev", "heldout"} or variant not in {"B0", "B1", "B2"} or min(limit, repeats) < 1:
        raise ValueError("invalid evaluation configuration")
    if live and (not settings.deepseek_api_key or corpus_manifest is None):
        raise ValueError("Live comparisons require a DeepSeek key and frozen corpus manifest")
    if not live:
        settings = settings.model_copy(update={"research_mode": "fixture"})
    else:
        settings = settings.model_copy(update={"research_mode": "live"})
    cases = json.loads(Path(f"evals/{split}.json").read_text())[:limit]
    manifest = json.loads(corpus_manifest.read_text()) if corpus_manifest else None
    db, store = Database(settings), ObjectStore(settings)
    await db.open()
    await store.setup()
    try:
        tenant = await db.auth(api_key)
        if not tenant:
            raise ValueError("invalid evaluation tenant key")
        corpus_ids = []
        if manifest:
            for entry in manifest["sources"]:
                source = await db.get(tenant, entry["id"], "source")
                if source["raw_hash"] != entry["raw_hash"] or source["parsed_hash"] != entry["parsed_hash"]:
                    raise ValueError("frozen corpus version mismatch")
                corpus_ids.append(source["id"])
        output.parent.mkdir(parents=True, exist_ok=True)
        service = ResearchService(db, store)
        for case in cases:
            for repeat in range(repeats):
                brief = ResearchBrief.model_validate(case["brief"])
                if live:
                    brief.source_urls = []
                    brief.upload_ids = corpus_ids
                request = CreateRun(
                    brief=brief,
                    profile=RunProfile(
                        variant=variant,
                        concurrency=concurrency,
                        context_strategy=context,
                        search_policy="frozen" if live else "live",
                    ),
                )
                run, _ = await service.create(tenant, request, "eval:" + str(uuid4()))
                run_id = str(run["id"])
                started = time.monotonic()
                # Evaluation owns one queued run at a time. It never silently executes another user's run.
                claim = await db.claim(run_id)
                if not claim or claim["run_id"] != run_id:
                    raise RuntimeError(
                        "Evaluation requires an idle dedicated worker/database; another run was selected"
                    )
                await execute_claim(db, store, claim)
                final = await db.run(tenant, run_id)
                package = await service.report(tenant, run_id)
                metrics = measure(
                    final,
                    package,
                    await db.usage(tenant, run_id),
                    await db.records(tenant, run_id, "context"),
                    await db.records(tenant, run_id, "review"),
                )
                metrics.update(
                    {
                        "wall_seconds": time.monotonic() - started,
                        "repeat": repeat,
                        "rubric": case["rubric"],
                        "corpus_id": manifest.get("id") if manifest else "synthetic-v1",
                    }
                )
                record = EvaluationResult(
                    run_id=run_id,
                    case_id=case["id"],
                    split=split,
                    mode=settings.research_mode,
                    variant=variant,
                    metrics=metrics,
                )
                with output.open("a") as handle:
                    handle.write(record.model_dump_json() + "\n")
                (output.parent / (run_id + ".json")).write_text(package.model_dump_json(indent=2))
    finally:
        await db.close()


async def freeze_corpus(settings, api_key, urls: list[str], output: Path):
    db, store = Database(settings), ObjectStore(settings)
    await db.open()
    await store.setup()
    try:
        tenant = await db.auth(api_key)
        if not tenant:
            raise ValueError("invalid tenant key")
        evidence = EvidenceService(db, store, tenant)
        sources = []
        for url in urls:
            source = await evidence.fetch(url, "live")
            sources.append(source.model_dump(mode="json"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"id": str(uuid4()), "sources": sources}, indent=2, ensure_ascii=False))
    finally:
        await db.close()
