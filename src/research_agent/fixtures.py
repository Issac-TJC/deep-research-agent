"""Synthetic protocol fixtures, deliberately not a research-quality benchmark or live fallback."""

import json

from research_agent.contracts import ModelUsage
from research_agent.providers import Completion

CORPUS = {
    "https://fixtures.invalid/lexical": """# Synthetic lexical retrieval note

Exact identifiers such as ERR_AUTH_42 should retain literal matching in a technical knowledge base.

This synthetic example reports no measured production latency and is not a vendor benchmark.
""",
    "https://fixtures.invalid/semantic": """# Synthetic semantic retrieval paper

The proposed method maps a query and document into a shared vector space and ranks them by similarity.

The experiment compares lexical and vector retrieval with the same corpus and evaluates recall at ten.

The authors report that paraphrase queries benefit, but rare error identifiers remain a limitation.

The appendix states that the dataset is synthetic and the result does not establish performance on Chinese production documents.
""",
    "https://fixtures.invalid/hybrid": """# Synthetic hybrid retrieval limitation

Hybrid retrieval combines lexical and semantic candidates before reranking, adding another latency and cost component.

Performance numbers from different hardware, datasets or metric definitions are not directly comparable.

An ablation should compare the same candidate budget with and without reranking and include error-code queries.
""",
}


class FixtureSearch:
    async def search(self, query: str) -> dict:
        kind = (
            "hybrid"
            if any(x in query.lower() for x in ["hybrid", "gap", "counter"])
            else "semantic"
            if any(x in query.lower() for x in ["semantic", "paper", "method"])
            else "lexical"
        )
        url = "https://fixtures.invalid/" + kind
        return {
            "results": [
                {"url": url, "title": "SYNTHETIC " + kind, "snippet": "Fixture lead; fetch original."}
            ],
            "evidence": False,
            "synthetic": True,
        }


def task(tid, objective, query, stage="related_work", method="literature_search"):
    return {
        "id": tid,
        "question_id": tid,
        "objective": objective,
        "query": query,
        "acceptance_criteria": ["Read original source and preserve limitations"],
        "stage": stage,
        "method": method,
        "deliverable": "Synthetic evidence-grounded synthesis",
    }


class FixtureProvider:
    async def complete(self, messages, *, tools=None, structured=False, max_tokens=8192):
        payload = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        role = payload.get("role", "researcher")
        message = {"role": "assistant", "content": ""}
        if tools:
            responses = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            search = next((r for r in responses if "results" in r), None)
            fetched = next((r for r in responses if "source_id" in r and "text" not in r), None)
            read = next((r for r in responses if "text" in r), None)
            progress = payload.get("progress", {})
            existing = payload.get("source_ids", []) or progress.get("authorized_source_ids", [])
            candidates = progress.get("candidate_urls", [])
            has_read = read or any(progress.get("read_ranges", {}).values())
            if has_read:
                message["content"] = "Source read; ready to extract evidence."
            elif fetched or existing:
                name, args = (
                    "read_source",
                    {
                        "source_id": fetched["source_id"] if fetched else existing[0],
                        "start": 0,
                        "length": 6000,
                    },
                )
            elif search or candidates:
                url = search["results"][0]["url"] if search else candidates[0]
                name, args = "fetch", {"url": url}
            else:
                name, args = "search", {"query": payload["task"]["query"]}
            if not message["content"]:
                message["tool_calls"] = [
                    {
                        "id": "fixture_call_" + str(len(responses)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
        else:
            if role == "research_director":
                template = payload["brief"]["template"]
                stage = "methodology" if template == "technical_comparison" else "related_work"
                mode = "decision_support" if template == "technical_comparison" else "literature_review"
                value = {
                    "normalized_question": payload["brief"]["question"],
                    "mode": mode,
                    "stages": [
                        {
                            "stage": stage,
                            "objective": payload["brief"]["question"],
                            "methods": ["literature_search", "source_synthesis"],
                            "deliverable": "Synthetic evidence-grounded synthesis",
                            "reason": "Fixture intent used to verify stage-aware orchestration",
                            "role": "deliverable",
                            "depends_on": [],
                        }
                    ],
                    "source_assignments": [
                        {
                            "source_id": source["source_id"],
                            "role": "user_material",
                            "reason": "Supplied fixture source",
                        }
                        for source in payload.get("available_sources", [])
                    ],
                    "assumptions": [],
                    "capability_gaps": [],
                }
            elif role == "planner":
                selected_stage = payload.get("research_intent", {}).get("stages", [{}])[0].get(
                    "stage", "related_work"
                )
                tasks = [
                    task("lexical", "Check exact technical identifiers", "lexical", selected_stage),
                    task("semantic", "Understand method and experiment design", "semantic paper", selected_stage),
                ]
                if payload.get("variant") == "B0":
                    tasks = [
                        task("baseline", "Investigate all questions iteratively", "lexical semantic hybrid")
                    ]
                value = {
                    "questions": [t["objective"] for t in tasks],
                    "tasks": tasks,
                    "rationale": "SYNTHETIC fixture plan to exercise orchestration",
                }
            elif role == "source_analyst":
                source = payload["source"]
                source_text = "\n\n".join(p["text"] for p in payload.get("passages", [])) or source.get(
                    "text", ""
                )
                lines = [
                    x.strip()
                    for x in source_text.split("\n\n")
                    if len(x.strip()) > 20 and not x.startswith("#")
                ]
                value = {
                    "task_id": payload["task_id"],
                    "summary": "SYNTHETIC extracted source",
                    "extractions": [
                        {
                            "text": x,
                            "quote": x,
                            "passage_id": next(
                                (p["id"] for p in payload.get("passages", []) if x in p["text"]), None
                            ),
                            "source_id": source["source_id"],
                            "limitations": ["Synthetic fixture, not a real measurement"],
                        }
                        for x in lines[:3]
                    ],
                    "unresolved": ["Production measurements have not been performed"],
                }
            elif role == "reviewer":
                claims = payload.get("claims", [])
                selected_stage = payload.get("research_intent", {}).get("stages", [{}])[0].get(
                    "stage", "related_work"
                )
                gap = (
                    not payload.get("report")
                    and payload.get("gap_round", 0) == 0
                    and payload.get("variant") == "B2"
                )
                value = {
                    "accepted_claim_ids": [c["id"] for c in claims],
                    "covered_question_ids": payload.get("question_ids", []),
                    "sufficient": bool(claims) and not gap,
                    "gap_tasks": [
                        task(
                            "gap-hybrid",
                            "Check counterevidence and comparability",
                            "hybrid counter evidence",
                            selected_stage,
                        )
                    ]
                    if gap
                    else [],
                    "findings": [],
                }
            elif role == "writer":
                claims = payload.get("claims", [])
                selected_stage = payload.get("research_intent", {}).get("stages", [{}])[0].get(
                    "stage", "related_work"
                )
                nodes = [
                    {
                        "id": "summary",
                        "kind": "section",
                        "title": "Synthetic fixture report",
                        "text": "此报告用于验证系统流程，内容为合成测试材料。",
                        "attribution": "guidance",
                        "stage": selected_stage,
                        "output_mode": "guidance",
                    }
                ]
                nodes += [
                    {
                        "id": "claim-" + str(i),
                        "kind": "paragraph",
                        "text": c["text"],
                        "claim_ids": [c["id"]],
                        "stage": selected_stage,
                    }
                    for i, c in enumerate(claims)
                ]
                if payload["brief"]["template"] == "technical_comparison":
                    nodes.append(
                        {
                            "id": "comparison",
                            "kind": "comparison_table",
                            "title": "Comparison",
                            "rows": [
                                {"approach": "lexical / vector / hybrid", "measured_latency": "unknown"}
                            ],
                            "attribution": "guidance",
                            "stage": selected_stage,
                            "output_mode": "guidance",
                        }
                    )
                value = {
                    "title": "SYNTHETIC · Research package",
                    "nodes": nodes,
                    "unresolved": ["真实场景效果尚未测量，不能作为技术选型结论。"],
                }
                if payload["brief"]["template"] == "paper_review":
                    ids = [c["id"] for c in claims]
                    value["paper"] = {
                        "takeaways": ["解释方法、实验条件和局限"],
                        "problem": "Synthetic retrieval study",
                        "contributions": ["Synthetic methodological example"],
                        "principles": "共享向量空间中的相似度检索。",
                        "implementation": "编码查询与文档，再排序；未运行论文代码。",
                        "experiments": [
                            {
                                "question": "Does retrieval preserve identifiers?",
                                "metric": "recall@10",
                                "result": "unknown",
                            }
                        ],
                        "limitations": [
                            {
                                "text": "Synthetic evidence cannot establish production performance",
                                "attribution": "system_inferred",
                                "claim_ids": ids,
                            }
                        ],
                        "ideas": [
                            {
                                "motivation": "Check identifier failures",
                                "hypothesis": "Lexical candidates improve rare identifier recall",
                                "experiment": "Compare candidate unions on the same frozen queries",
                                "baseline": "vector-only",
                                "metric": "recall@10 and p95 latency",
                                "expected_signal": "Higher identifier recall",
                                "failure_risk": "Reranking cost may offset gains",
                                "claim_ids": ids,
                            }
                        ],
                        "claim_ids": ids,
                    }
            elif role == "patcher":
                value = {
                    "replacements": [],
                    "unresolved": ["Fixture review complete; real research quality unmeasured"],
                }
            else:
                raise ValueError("unknown fixture role")
            message["content"] = json.dumps(value, ensure_ascii=False)
        return Completion(
            message,
            ModelUsage(input_tokens=100, output_tokens=100, estimated=True),
            "tool_calls" if "tool_calls" in message else "stop",
            "synthetic-fixture-v1",
        )
