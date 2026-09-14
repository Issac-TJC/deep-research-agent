"""Frozen passage-retrieval benchmark used by the rollout quality gate."""

from __future__ import annotations

import math
import time

from pydantic import BaseModel, ConfigDict, Field

from research_agent.evidence import EvidenceService


class RelevantPassage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    query: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1, max_length=24)
    relevant: list[RelevantPassage] = Field(min_length=1)
    exact_identifier: bool = False


def overlaps(hit: dict, passage: RelevantPassage) -> bool:
    return (
        hit["source_id"] == passage.source_id
        and min(hit["end"], passage.end) > max(hit["start"], passage.start)
    )


async def run_benchmark(evidence: EvidenceService, cases: list[RetrievalCase]) -> dict:
    results = {}
    for strategy in ("lexical", "dense", "hybrid"):
        recalled = exact_recalled = exact_total = 0
        latencies = []
        degraded = 0
        for case in cases:
            started = time.perf_counter()
            response = await evidence.search(case.source_ids, case.query, strategy, 8)
            degraded += int(response["strategy"] != strategy)
            latencies.append(time.perf_counter() - started)
            found = any(
                overlaps(hit, passage)
                for hit in response["results"]
                for passage in case.relevant
            )
            recalled += int(found)
            if case.exact_identifier:
                exact_total += 1
                exact_recalled += int(found)
        ordered = sorted(latencies)
        p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0
        results[strategy] = {
            "recall_at_8": recalled / len(cases) if cases else 0,
            "exact_identifier_recall_at_8": (
                exact_recalled / exact_total if exact_total else None
            ),
            "p95_seconds": p95 if latencies else 0,
            "queries": len(cases),
            "degraded_queries": degraded,
        }
    hybrid = results["hybrid"]
    results["quality_gate"] = {
        "enough_cases": len(cases) >= 150,
        "dense_and_hybrid_available": (
            results["dense"]["degraded_queries"] == 0
            and results["hybrid"]["degraded_queries"] == 0
        ),
        "recall_at_8": hybrid["recall_at_8"] >= 0.90,
        "exact_identifier_recall_at_8": hybrid["exact_identifier_recall_at_8"] == 1.0,
        "hybrid_not_worse": hybrid["recall_at_8"] >= max(
            results["lexical"]["recall_at_8"], results["dense"]["recall_at_8"]
        ),
        "p95_seconds": hybrid["p95_seconds"] <= 1.0,
    }
    results["quality_gate"]["passed"] = all(results["quality_gate"].values())
    return results
