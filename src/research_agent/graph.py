from __future__ import annotations

import asyncio
import json
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_agent import __version__
from research_agent.checkpoints import FencedSaver
from research_agent.context import (
    POLICY,
    ResearchProgress,
    build_context,
    degradation_level,
    merge_read_ranges,
    progress_signal,
    relevance_terms,
    tool_protocol_closed,
)
from research_agent.contracts import (
    Claim,
    EvidenceSpan,
    Plan,
    ReportDraft,
    ReportNode,
    ReportPatch,
    ResearchBrief,
    ResearchFinding,
    ResearchIntent,
    ResearchMethod,
    ResearchPackage,
    ResearchStage,
    ResearchTask,
    Review,
    ReviewFinding,
    RunProfile,
    SourceVersion,
)
from research_agent.db import BudgetExceeded, Database, digest
from research_agent.evidence import (
    EvidenceService,
    candidate_passages,
    provenance_groups,
    render_markdown,
    stable_id,
)
from research_agent.fixtures import FixtureProvider, FixtureSearch
from research_agent.gateway import Gateway
from research_agent.providers import DeepSeekProvider, ProviderError, TavilySearch
from research_agent.storage import ObjectStore


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=1000)


class FetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2048)


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    start: int = Field(default=0, ge=0)
    length: int = Field(default=6000, ge=100, le=12000)


class SearchSourcesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=8, ge=1, le=12)


class SearchMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=1000)
    include_candidates: bool = True
    limit: int = Field(default=6, ge=1, le=10)


class ReadMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: str = Field(pattern="^(project_memory|observation)$")
    object_id: str


TOOL_MODELS = {
    "search": SearchArgs,
    "fetch": FetchArgs,
    "search_sources": SearchSourcesArgs,
    "read_source": ReadArgs,
    "search_memory": SearchMemoryArgs,
    "read_memory": ReadMemoryArgs,
}
TOOLS = [
    {
        "type": "function",
        "function": {"name": name, "parameters": model.model_json_schema(), "description": description},
    }
    for name, model, description in [
        ("search", SearchArgs, "Discover candidate public sources; search snippets are not evidence."),
        (
            "fetch",
            FetchArgs,
            "Fetch and persist a public HTML/PDF/Markdown source; returns an authorized source id.",
        ),
        (
            "search_sources",
            SearchSourcesArgs,
            "Find relevant ranges in authorized persisted sources. Results are leads, not evidence; read each range.",
        ),
        (
            "read_source",
            ReadArgs,
            "Read a bounded range of an authorized original source; follow up to read more.",
        ),
        (
            "search_memory",
            SearchMemoryArgs,
            "Search confirmed and candidate governed memories. Results are leads, not claim evidence.",
        ),
        (
            "read_memory",
            ReadMemoryArgs,
            "Read one governed memory with status and evidence pointers before using it.",
        ),
    ]
]

STAGE_AGENT_INSTRUCTIONS = {
    ResearchStage.INTRODUCTION: (
        "Act as an Introduction research agent. Establish the field context and find evidence for a precise gap. "
        "Connect the gap to a concrete objective, rationale, significance and falsifiable hypotheses."
    ),
    ResearchStage.RELATED_WORK: (
        "Act as a Related Work research agent. Find primary work, build a defensible taxonomy, compare assumptions, "
        "methods and evaluation conditions, and preserve disagreements and limitations."
    ),
    ResearchStage.METHODOLOGY: (
        "Act as a Method research agent. Identify relevant baselines and mechanisms, then test whether each proposed "
        "design choice addresses an evidenced limitation and record its failure modes."
    ),
    ResearchStage.EXPERIMENT: (
        "Act as an Experiment research agent. Find datasets, baselines, protocols, metrics and reproducibility details "
        "needed to test the hypothesis. Never imply that a planned experiment was executed."
    ),
    ResearchStage.RESULTS: (
        "Act as a Results research agent. Trace every value to a run artifact or an explicitly attributed source result, "
        "and preserve uncertainty, evaluation conditions, ablations and negative results."
    ),
    ResearchStage.DISCUSSION: (
        "Act as a Discussion research agent. Evaluate interpretations, alternative explanations, threats to validity "
        "and the boundary within which conclusions can be generalized."
    ),
    ResearchStage.CONCLUSION: (
        "Act as a Conclusion research agent. Identify the claims that answer the requested question, their confidence "
        "boundaries and the most justified next step; introduce no new unsupported facts."
    ),
}


class ResearcherState(TypedDict, total=False):
    task: dict
    dependency_context: dict
    messages: list[dict]
    source_ids: list[str]
    read_slices: list[dict]
    iteration: int
    done: bool
    unresolved: list[str]
    candidate_urls: list[str]
    retrieval_hits: list[dict]
    progress: dict


class ResearchState(TypedDict, total=False):
    intent: dict
    plan: dict
    tasks: list[dict]
    gap_round: int
    patch_round: int
    review: dict
    report: dict
    revision: int
    stop_reason: str
    previous_claims: int
    previous_signal: str
    no_gain: bool
    pending_patch_findings: list[dict]
    degradation_level: int
    research_budget_exhausted: bool


class ResearchEngine:
    def __init__(self, db: Database, store: ObjectStore, tenant: str, run: dict, fence: int):
        self.db, self.store, self.tenant = db, store, tenant
        self.run_id, self.fence = str(run["id"]), fence
        self.project_id = str(run["project_id"]) if run.get("project_id") else None
        self.brief = ResearchBrief.model_validate(run["brief"])
        self.profile = RunProfile.model_validate(run["profile"])
        self.mode = run["mode"]
        self.configuration = run.get("configuration") or db.settings.execution_snapshot()
        self.execution_settings = db.settings.model_copy(update=self.configuration.get("settings", {}))
        provider = FixtureProvider() if self.mode == "fixture" else DeepSeekProvider(self.execution_settings)
        self.search = FixtureSearch() if self.mode == "fixture" else TavilySearch(self.execution_settings)
        self.gateway = Gateway(
            db, tenant, self.run_id, fence, self.profile, provider, self.mode, self.execution_settings
        )
        self.evidence = EvidenceService(db, store, tenant)
        self.saver = FencedSaver(db, tenant, self.run_id, fence)

    async def phase(self, name, **payload):
        await self.db.phase(self.tenant, self.run_id, self.fence, name, payload)

    async def put(self, kind, key, data, immutable=True):
        await self.db.put(self.tenant, kind, key, data, self.run_id, self.fence, immutable)

    async def sources(self) -> list[SourceVersion]:
        return [
            SourceVersion.model_validate(r["source"])
            for r in await self.db.records(self.tenant, self.run_id, "source_ref")
        ]

    async def claims(self) -> list[dict]:
        return await self.db.records(self.tenant, self.run_id, "claim")

    async def evidence_context(self, role, key, payload):
        # Evidence is selectable working material, not part of the immutable brief.
        # Pack a claim together with its spans so truncation never creates dangling refs.
        payload = dict(payload)
        claims = payload.pop("claims", [])
        spans = payload.pop("spans", [])
        span_map = {span["id"]: span for span in spans}
        terms = relevance_terms(payload)
        artifacts = [
            {
                "id": "claim:" + claim["id"],
                "kind": "claim_evidence",
                "claim": claim,
                "spans": [span_map[sid] for sid in claim["span_ids"] if sid in span_map],
            }
            for claim in claims
        ]
        artifacts.sort(
            key=lambda item: (
                -len(terms & relevance_terms(item)),
                str(item["id"]),
            )
        )
        payload["claims"], payload["spans"] = [], []
        payload["evidence_selection_note"] = (
            "Only claims and spans supplied in this request are available for support decisions. "
            "Omitted claim IDs remain in persistent storage but are not reviewed here. "
            "Do not infer coverage from omitted evidence or invent quotations."
        )
        packed = await self.context(role, key, payload, artifacts)
        bundles = [a for a in packed["artifacts"] if a.get("kind") == "claim_evidence"]
        packed["claims"] = [a["claim"] for a in bundles]
        packed["spans"] = list({span["id"]: span for a in bundles for span in a["spans"]}.values())
        packed["artifacts"] = [a for a in packed["artifacts"] if a.get("kind") != "claim_evidence"]
        return packed

    async def context(self, role, key, pinned, artifacts, task_id=None):
        # Leave room for the system policy, JSON schema, message serialization and tool definitions.
        packing = self.profile.model_copy(
            update={"prompt_token_limit": max(1000, self.profile.prompt_token_limit - 12000)}
        )
        try:
            payload, snapshot = build_context(role, self.brief, packing, pinned, artifacts, task_id)
        except ValueError as exc:
            if str(exc) == "pinned_context_exceeds_limit":
                raise BudgetExceeded("pinned_context_exceeds_limit") from exc
            raise
        snapshot.id = stable_id(self.run_id, "context", key)
        await self.put("context", snapshot.id, snapshot.model_dump(mode="json"))
        return payload

    async def budget_level(self) -> int:
        run = await self.db.run(self.tenant, self.run_id)
        level = degradation_level(int(run["tokens"]))
        reasons = {
            70: "capsule_required_and_duplicate_evidence_reduced",
            85: "new_source_discovery_disabled",
            95: "gap_research_disabled_and_writer_protected",
        }
        for threshold in (70, 85, 95):
            if level >= threshold:
                await self.db.record_degradation(self.tenant, self.run_id, threshold, reasons[threshold])
        return level

    async def bootstrap(self, state):
        await self.phase("intake")
        for source_id in self.brief.upload_ids:
            source = await self.db.get(self.tenant, source_id, "source")
            await self.db.attach_source(self.tenant, self.run_id, self.fence, source)
        for index, url in enumerate(self.brief.source_urls):

            async def fetch(url=url):
                source = await self.evidence.fetch(url, self.mode, self.profile.parser_mode)
                await self.db.attach_source(
                    self.tenant, self.run_id, self.fence, source.model_dump(mode="json")
                )
                return {"source_id": source.id}

            try:
                await self.gateway.tool("fetch", f"intake:{index}", fetch)
            except (ValueError, ProviderError) as exc:
                await self.put(
                    "warning",
                    self.run_id + f":intake-warning:{index}",
                    {"source_url": url, "error": type(exc).__name__},
                )
        return {"gap_round": 0, "patch_round": 0, "revision": 0}

    async def understand(self, state):
        """Compile a messy request into the paper-shaped work that is actually needed."""
        await self.phase("scoping")
        sources = await self.sources()
        if self.profile.variant == "B0":
            stage = (
                ResearchStage.METHODOLOGY
                if self.brief.template == "technical_comparison"
                else ResearchStage.RELATED_WORK
            )
            intent = ResearchIntent(
                normalized_question=self.brief.question,
                mode="decision_support"
                if self.brief.template == "technical_comparison"
                else "literature_review",
                stages=[
                    {
                        "stage": stage,
                        "objective": self.brief.question,
                        "methods": [ResearchMethod.LITERATURE_SEARCH],
                        "deliverable": "Evidence-grounded synthesis",
                        "reason": "Single-researcher baseline preserves the requested scope.",
                        "role": "deliverable",
                    }
                ],
                source_assignments=[
                    {"source_id": source.id, "role": "user_material", "reason": "User supplied source"}
                    for source in sources
                ],
            )
        else:
            payload = await self.context(
                "research_director",
                "scope",
                {
                    "available_sources": [
                        {
                            "source_id": source.id,
                            "title": source.title,
                            "url": source.url,
                            "filename": source.filename,
                        }
                        for source in sources
                    ],
                    "available_capabilities": [
                        "search", "fetch", "read_source", "search_memory", "read_memory"
                    ],
                    "instructions": (
                        "Infer the user's research intent instead of asking them to choose a template. Select only the paper "
                        "modules the user wants as role=deliverable. Add hidden role=supporting modules when a deliverable"
                        " needs their research. For example, Introduction or Methodology often depends_on Related Work;"
                        " Experiment may depend_on Methodology. Supporting modules are internal evidence producers, not final"
                        " report sections. Introduction means constructing context -> concrete gap -> "
                        "objective -> rationale -> significance; related_work means evidence-backed literature synthesis; "
                        "methodology means critique or design; experiment means reproduction or an executable protocol; "
                        "results is allowed only when result evidence already exists. Assign each supplied source a semantic "
                        "role. Put any requested but unavailable capability, especially code or experiment execution, in "
                        "capability_gaps. State useful assumptions, but do not manufacture clarification questions."
                    ),
                },
                [],
            )
            intent = ResearchIntent.model_validate(
                await self.gateway.structured("research_director", "scope", payload, ResearchIntent)
            )
        known_sources = {source.id for source in sources}
        if not {item.source_id for item in intent.source_assignments} <= known_sources:
            raise ValueError("intent_invented_source_ids")
        await self.put("intent", self.run_id + ":intent", intent.model_dump(mode="json"))
        return {"intent": intent.model_dump(mode="json")}

    async def plan(self, state):
        await self.phase("planning")
        if self.profile.variant == "B0":
            plan = Plan(
                questions=[self.brief.question],
                rationale="Single iterative Researcher baseline",
                tasks=[
                    ResearchTask(
                        id="baseline",
                        question_id="q0",
                        objective=self.brief.question,
                        query=self.brief.question,
                        acceptance_criteria=self.brief.dimensions or ["Cover the complete brief"],
                    )
                ],
            )
        else:
            payload = await self.context(
                "planner",
                "plan",
                {
                    "variant": self.profile.variant,
                    "max_tasks": min(
                        self.profile.max_tasks,
                        max(3, len(state["intent"]["stages"])),
                    ),
                    "source_ids": [s.id for s in await self.sources()],
                    "research_intent": state["intent"],
                    "instructions": "Plan independent research questions with explicit acceptance criteria. Keep all user constraints."
                    " Be concise: objectives under 120 Chinese characters, 2-3 acceptance criteria each. Do not repeat the brief's constraints."
                    " Prefer 2-3 initial tasks. Create at least one task for every selected module. Every task must name its"
                    " paper stage, research method and concrete artifact. Respect the intent's stage dependency DAG."
                    " Supporting tasks gather inputs for deliverable tasks and must run first. Do not add stages that the"
                    " research director did not select.",
                },
                [],
            )
            plan = Plan.model_validate(await self.gateway.structured("planner", "plan", payload, Plan))
        plan.intent = ResearchIntent.model_validate(state["intent"])
        decisions = {decision.stage: decision for decision in plan.intent.stages}
        if self.profile.variant == "B0":
            decision = plan.intent.stages[0]
            for task in plan.tasks:
                task.stage = decision.stage
                task.method = decision.methods[0]
                task.deliverable = decision.deliverable
                task.output_role = decision.role
        if any(
            task.stage not in decisions or task.method not in decisions[task.stage].methods
            for task in plan.tasks
        ):
            raise ValueError("planner_task_outside_research_intent")
        tasks_by_stage = {stage: [task for task in plan.tasks if task.stage == stage] for stage in decisions}
        if any(not tasks for tasks in tasks_by_stage.values()):
            raise ValueError("planner_omitted_research_stage")
        for task in plan.tasks:
            decision = decisions[task.stage]
            task.output_role = decision.role
            dependency_ids = [
                dependency.id for stage in decision.depends_on for dependency in tasks_by_stage[stage]
            ]
            task.depends_on = list(dict.fromkeys(task.depends_on + dependency_ids))
        plan = Plan.model_validate(plan.model_dump(mode="json"))
        if len(plan.tasks) > self.profile.max_tasks:
            raise ValueError("planner_exceeded_task_limit")
        mapping = {t.id: stable_id(self.run_id, "task", t.id) for t in plan.tasks}
        for task in plan.tasks:
            old_id = task.id
            task.id = mapping[old_id]
            task.depends_on = [mapping[d] for d in task.depends_on]
            task.source_ids = [s.id for s in await self.sources()]
            await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
        await self.put("plan", self.run_id + ":plan", plan.model_dump(mode="json"))
        return {
            "plan": plan.model_dump(mode="json"),
            "tasks": [t.model_dump(mode="json") for t in plan.tasks],
        }

    def researcher_graph(self):
        graph = StateGraph(ResearcherState)

        async def decide(state):
            task = ResearchTask.model_validate(state["task"])
            run = await self.db.run(self.tenant, self.run_id)
            level = await self.budget_level()
            progress = ResearchProgress.model_validate(
                state.get("progress")
                or {
                    "authorized_source_ids": state.get("source_ids", []),
                    "candidate_urls": state.get("candidate_urls", self.brief.source_urls),
                    "retrieval_hits": state.get("retrieval_hits", []),
                    "acceptance_coverage": [],
                    "unresolved": state.get("unresolved", []),
                }
            )
            if level >= 95:
                return {
                    "done": True,
                    "progress": progress.model_dump(mode="json"),
                    "unresolved": progress.unresolved
                    + ["Soft-token 95% threshold reached; proceeding to review and writing"],
                }
            if progress.no_gain_rounds >= 2:
                return {
                    "done": True,
                    "progress": progress.model_dump(mode="json"),
                    "unresolved": progress.unresolved
                    + ["Stopped after two tool rounds without new sources, ranges, hits, or coverage"],
                }
            usage = await self.db.usage(self.tenant, self.run_id)
            task_tools = [a for a in usage if a["task_id"] == task.id and a["kind"] != "model"]
            search_cap = (
                self.profile.max_search_calls
                if self.profile.variant == "B0"
                else self.profile.max_task_search_calls
            )
            remaining_searches = max(
                0,
                min(
                    self.profile.max_search_calls - run["search_calls"],
                    search_cap - sum(a["kind"] == "search" for a in task_tools),
                ),
            )
            # Retrieval is a run-wide allowance. Using the per-task count here used
            # to advertise search_sources after another task had exhausted the
            # global quota, guaranteeing a late BudgetExceeded.
            remaining_retrievals = max(
                0,
                self.profile.max_retrieval_calls - int(run["retrieval_calls"]),
            )
            retrieval_degraded = any(
                item.startswith("search_sources: fallback:") for item in progress.unresolved
            )
            remaining_tools = min(
                self.profile.max_tool_calls - run["tool_calls"],
                (self.profile.max_tool_calls if self.profile.variant == "B0" else self.profile.max_task_tools)
                - len(task_tools),
            )
            if remaining_tools <= 0:
                return {
                    "done": True,
                    "unresolved": state.get("unresolved", [])
                    + ["Tool limit reached; extract already-read evidence"],
                }
            available_tools = [
                tool
                for tool in TOOLS
                if (remaining_searches or tool["function"]["name"] != "search")
                and (
                    (remaining_retrievals and not retrieval_degraded)
                    or tool["function"]["name"] != "search_sources"
                )
                and (level < 85 or tool["function"]["name"] not in {"search", "fetch"})
            ]
            iteration = state.get("iteration", 0)
            max_turns = (
                self.profile.max_tool_calls if self.profile.variant == "B0" else self.profile.max_task_tools
            )
            if iteration >= max_turns:
                return {
                    "done": True,
                    "unresolved": state.get("unresolved", []) + ["Researcher turn limit reached"],
                }
            # A completed tool batch is a legal protocol boundary. Start a stable,
            # bounded provider conversation from the capsule rather than replaying history.
            payload = await self.context(
                task.stage.value + "_agent",
                f"{task.id}:capsule:{iteration}",
                {
                    "task": task.model_dump(mode="json"),
                    "instructions": "Choose search/fetch/read iteratively. Read original evidence and limitations."
                    " Fetch promising search results and read them before searching again; summaries are not evidence."
                    " For every retrieval hit you use, call read_source around its start/end before extraction."
                    " Use search_memory/read_memory only for governed prior context; candidate memories are unconfirmed"
                    " and no memory substitutes for a cited original source."
                    " Stop with a short final message when sufficient; extraction runs next. Never spawn agents. "
                    + STAGE_AGENT_INSTRUCTIONS[task.stage],
                    "dependency_context": state.get("dependency_context", {}),
                    "progress": progress.prompt_view(),
                },
                [],
                task.id,
            )
            messages = [
                {"role": "system", "content": POLICY},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "remaining_task_tools": remaining_tools,
                            "remaining_search_calls": remaining_searches,
                            "degradation_level": level,
                            "instruction": "Stay within these limits. Prioritize fetching and reading existing candidates. Reserve calls for reading originals; stop once sufficient.",
                        }
                    ),
                },
            ]
            result = await self.gateway.model(
                f"{task.id}:decide:{iteration}", messages, tools=available_tools, task_id=task.id
            )
            message = result["message"]
            return {
                "messages": messages + [message],
                "iteration": iteration + 1,
                "done": not bool(message.get("tool_calls")),
                "progress": progress.model_dump(mode="json"),
            }

        async def use_tools(state):
            task = ResearchTask.model_validate(state["task"])
            progress = ResearchProgress.model_validate(state.get("progress") or {})
            before_signal = progress_signal(progress)
            sources = set(state.get("source_ids", []))
            slices = list(state.get("read_slices", []))
            retrieval_hits = list(state.get("retrieval_hits", []))
            messages = list(state["messages"])
            unresolved = list(state.get("unresolved", []))
            candidates = set(state.get("candidate_urls", self.brief.source_urls))
            outcomes = []
            for idx, tool_call in enumerate(messages[-1].get("tool_calls", [])):
                name = tool_call.get("function", {}).get("name", "unknown")
                key = f"{task.id}:tool:{state['iteration']}:{idx}"

                async def operation():
                    if name not in TOOL_MODELS:
                        raise ValueError("tool_not_allowed")
                    args = TOOL_MODELS[name].model_validate_json(tool_call["function"]["arguments"])
                    if name == "search":
                        if await self.budget_level() >= 85:
                            raise BudgetExceeded("new_source_discovery_disabled")
                        if self.profile.search_policy == "frozen":
                            from research_agent.evaluation import frozen_search

                            return await frozen_search(self.evidence, await self.sources(), args.query)
                        return await self.search.search(args.query)
                    if name == "fetch":
                        if await self.budget_level() >= 85:
                            raise BudgetExceeded("new_source_discovery_disabled")
                        if args.url not in candidates:
                            raise ValueError("fetch_url_not_discovered; search for the source first")
                        if self.profile.search_policy == "frozen":
                            match = next((s for s in await self.sources() if s.url == args.url), None)
                            if match is None:
                                raise ValueError("source_outside_frozen_corpus")
                            return {"source_id": match.id, "title": match.title, "warnings": match.warnings}
                        source = await self.evidence.fetch(args.url, self.mode, self.profile.parser_mode)
                        await self.db.attach_source(
                            self.tenant,
                            self.run_id,
                            self.fence,
                            source.model_dump(mode="json"),
                            related=args.url not in self.brief.source_urls,
                        )
                        return {"source_id": source.id, "title": source.title, "warnings": source.warnings}
                    if name == "search_sources":
                        return await self.evidence.search(
                            sorted(sources),
                            args.query,
                            self.profile.retrieval_strategy,
                            args.limit,
                            0 if self.mode == "fixture" else self.db.settings.index_wait_seconds,
                        )
                    if name == "search_memory":
                        if not self.project_id:
                            raise ValueError("memory_requires_project")
                        return {
                            "results": await self.db.search_memory(
                                self.tenant, self.project_id, args.query,
                                args.include_candidates, args.limit,
                            ),
                            "instruction": (
                                "Memory is planning context, not report evidence. Read selected items; "
                                "candidate status means user confirmation is still pending."
                            ),
                        }
                    if name == "read_memory":
                        if not self.project_id:
                            raise ValueError("memory_requires_project")
                        return await self.db.read_memory(
                            self.tenant, self.project_id, args.object_type, args.object_id
                        )
                    if args.source_id not in sources:
                        raise ValueError("source_not_authorized_for_task")
                    _, doc = await self.evidence.document(args.source_id)
                    if args.start >= len(doc.text):
                        raise ValueError("read_start_outside_document")
                    requested_end = min(len(doc.text), args.start + args.length)
                    if any(
                        start <= args.start and end >= requested_end
                        for start, end in progress.read_ranges.get(args.source_id, [])
                    ):
                        return {
                            "source_id": args.source_id,
                            "start": args.start,
                            "end": requested_end,
                            "deduplicated": True,
                            "instruction": "This range is already in the capsule; do not read it again.",
                        }
                    text = doc.text[args.start : args.start + args.length]
                    return {
                        "source_id": args.source_id,
                        "start": args.start,
                        "end": args.start + len(text),
                        "text": text,
                        "total_chars": len(doc.text),
                        "has_more": args.start + len(text) < len(doc.text),
                    }

                try:
                    result = await self.gateway.tool(
                        "retrieve"
                        if name == "search_sources"
                        else "memory"
                        if name in {"search_memory", "read_memory"}
                        else name
                        if name in TOOL_MODELS
                        else "invalid_tool",
                        key,
                        operation,
                        task.id,
                    )
                    if "source_id" in result and name in {"fetch", "read_source"}:
                        sources.add(result["source_id"])
                    if "results" in result:
                        candidates.update(r["url"] for r in result["results"] if r.get("url"))
                        if name == "search_sources":
                            for hit in result["results"]:
                                if not any(
                                    old.get("chunk_id") == hit.get("chunk_id") for old in retrieval_hits
                                ):
                                    retrieval_hits.append(hit)
                    if name == "search_sources" and result.get("fallback"):
                        unresolved.append("search_sources: fallback:" + result["fallback"])
                        await self.put(
                            "warning",
                            key + ":retrieval-fallback",
                            {"task_id": task.id, "reason": result["fallback"]},
                        )
                    if "text" in result:
                        ranges = [
                            (item["start"], item["start"] + len(item["text"]))
                            for item in slices
                            if item["source_id"] == result["source_id"]
                        ] + [(result["start"], result["start"] + len(result["text"]))]
                        merged = merge_read_ranges(ranges)
                        slices = [item for item in slices if item["source_id"] != result["source_id"]]
                        _, doc = await self.evidence.document(result["source_id"])
                        slices.extend(
                            {
                                "source_id": result["source_id"],
                                "start": start,
                                "end": end,
                                "text": doc.text[start:end],
                                "total_chars": len(doc.text),
                                "has_more": end < len(doc.text),
                            }
                            for start, end in merged
                        )
                except BudgetExceeded as exc:
                    # Keep tool message pairing and all earlier reads, even if a burst exceeds quota.
                    result = {
                        "error": "budget:" + str(exc),
                        "tool": name,
                        "instruction": "Use remaining allowed tools or finish; already-read evidence is retained.",
                    }
                    unresolved.append(f"{name}: budget:{exc}")
                    await self.put("tool_rejection", key, result)
                except (ValueError, ValidationError, ProviderError) as exc:
                    result = {"error": str(exc)[:300], "tool": name}
                    unresolved.append(f"{name}: {str(exc)[:120]}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                outcomes.append(
                    {
                        "tool": name,
                        "ok": "error" not in result,
                        "source_id": result.get("source_id"),
                        "result_count": len(result.get("results", [])),
                        "error": result.get("error"),
                        "fallback": result.get("fallback"),
                    }
                )
            progress.authorized_source_ids = sorted(sources)
            progress.candidate_urls = sorted(candidates)
            progress.read_ranges = {
                source_id: merge_read_ranges(
                    [
                        (item["start"], item["start"] + len(item["text"]))
                        for item in slices
                        if item["source_id"] == source_id
                    ]
                )
                for source_id in sorted(sources)
            }
            progress.retrieval_hits = retrieval_hits[-24:]
            progress.unresolved = list(dict.fromkeys(unresolved))[-24:]
            progress.last_tool_outcomes = outcomes[-8:]
            after_signal = progress_signal(progress)
            progress.no_gain_rounds = progress.no_gain_rounds + 1 if after_signal == before_signal else 0
            progress.signal = after_signal
            if not tool_protocol_closed(messages):
                raise ValueError("unclosed_tool_protocol")
            return {
                # The assistant/tool envelope was kept intact through this batch. It
                # is now closed, so the next turn is rebuilt from the capsule.
                "messages": [],
                "source_ids": sorted(sources),
                "read_slices": slices,
                "retrieval_hits": retrieval_hits,
                "unresolved": unresolved,
                "candidate_urls": sorted(candidates),
                "progress": progress.model_dump(mode="json"),
            }

        async def extract(state):
            task = ResearchTask.model_validate(state["task"])
            unresolved, claim_ids, summaries = list(state.get("unresolved", [])), [], []
            for index, source_slice in enumerate(state.get("read_slices", [])):
                _, document = await self.evidence.document(source_slice["source_id"])
                passages = candidate_passages(
                    document, source_slice["start"], source_slice["start"] + len(source_slice["text"])
                )
                passage_map = {p["id"]: p for p in passages}
                payload = {
                    "role": "source_analyst",
                    "brief": self.brief.model_dump(mode="json"),
                    "task_id": task.id,
                    "objective": task.objective,
                    "source": {k: v for k, v in source_slice.items() if k != "text"},
                    "passages": passages,
                    "instructions": "Extract at most 4 highly relevant atomic claims from this source. Each claim must be one concise sentence, under 250 characters."
                    " Set passage_id to one of the supplied passage IDs and leave quote empty; the application binds the original text."
                    " The selected passage must support EVERY assertion in that claim. Split or omit unsupported clauses."
                    " Do not add navigation menu items, unrelated implementation details or repeated claims. Keep the summary under 200 characters."
                    " Preserve author attribution, units, experimental conditions and limitations.",
                }
                finding = ResearchFinding.model_validate(
                    await self.gateway.structured(
                        "source_analyst", f"{task.id}:extract:{index}", payload, ResearchFinding
                    )
                )
                summaries.append(finding.summary)
                unresolved.extend(finding.unresolved)
                for ex in finding.extractions:
                    try:
                        if ex.passage_id:
                            if ex.passage_id not in passage_map:
                                raise ValueError("invented_passage_id")
                            ex.quote = passage_map[ex.passage_id]["text"]
                        elif self.mode == "live":
                            raise ValueError("passage_id_required_for_live_extraction")
                        if ex.source_id != source_slice["source_id"] or ex.quote not in source_slice["text"]:
                            raise ValueError("extraction_outside_read_slice")
                        claim, _ = await self.evidence.extract(
                            self.run_id,
                            self.fence,
                            task.id,
                            ex,
                            {source_slice["source_id"]},
                            source_slice["start"],
                        )
                        claim_ids.append(claim.id)
                    except ValueError as exc:
                        unresolved.append(str(exc))
            task.execution_status = "completed"
            task.stop_reason = "evidence_collected" if claim_ids else "no_valid_evidence"
            task.acceptance = "unchecked"
            await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
            await self.put(
                "finding",
                task.id + ":finding",
                {
                    "task_id": task.id,
                    "claim_ids": claim_ids,
                    "summary": "\n".join(summaries),
                    "unresolved": list(dict.fromkeys(unresolved)),
                },
            )
            return {"done": True}

        graph.add_node("decide", decide)
        graph.add_node("tools", use_tools)
        graph.add_node("extract", extract)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges("decide", lambda s: "extract" if s.get("done") else "tools")
        graph.add_edge("tools", "decide")
        graph.add_edge("extract", END)
        return graph.compile(checkpointer=self.saver)

    async def research(self, state):
        await self.phase("research", gap_round=state.get("gap_round", 0))
        tasks = [ResearchTask.model_validate(t) for t in state["tasks"]]
        finished: set[str] = set()
        succeeded: set[str] = set()
        failed: set[str] = set()
        research_budget_stop: str | None = None
        for task in tasks:
            saved = await self.db.get(self.tenant, task.id, "task")
            task.execution_status = saved["execution_status"]
            if task.execution_status in {"completed", "failed", "cancelled"}:
                finished.add(task.id)
                (succeeded if task.execution_status == "completed" else failed).add(task.id)
        semaphore = asyncio.Semaphore(self.profile.concurrency if self.profile.variant != "B0" else 1)

        async def run_task(task):
            nonlocal research_budget_stop
            async with semaphore:
                if research_budget_stop:
                    task.execution_status = "cancelled"
                    task.stop_reason = "deferred_after_" + research_budget_stop
                    await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
                    finished.add(task.id)
                    failed.add(task.id)
                    return
                task.execution_status = "running"
                await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
                graph = self.researcher_graph()
                config = {
                    "configurable": {"thread_id": self.run_id + ":researcher:" + task.id},
                    "recursion_limit": 200,
                }
                existing = await graph.aget_state(config)
                dependency_claims = [
                    claim for claim in await self.claims() if claim["task_id"] in task.depends_on
                ]
                dependency_span_ids = {
                    span_id for claim in dependency_claims for span_id in claim["span_ids"]
                }
                dependency_spans = [
                    span
                    for span in await self.db.records(self.tenant, self.run_id, "span")
                    if span["id"] in dependency_span_ids
                ]
                dependency_findings = [
                    finding
                    for finding in await self.db.records(self.tenant, self.run_id, "finding")
                    if finding["task_id"] in task.depends_on
                ]
                inherited_sources = {span["source_id"] for span in dependency_spans}
                authorized_sources = sorted(set(task.source_ids) | inherited_sources)
                try:
                    retrieval = {"results": [], "strategy": "sequential", "fallback": None}
                    if (
                        not existing.values
                        and authorized_sources
                        and self.profile.retrieval_strategy != "sequential"
                    ):
                        current = await self.db.run(self.tenant, self.run_id)
                        if int(current["retrieval_calls"]) >= self.profile.max_retrieval_calls:
                            retrieval["fallback"] = "retrieval_budget_exhausted"
                        else:

                            async def initial_retrieval():
                                return await self.evidence.search(
                                    authorized_sources,
                                    task.query + "\n" + task.objective,
                                    self.profile.retrieval_strategy,
                                    8,
                                    0 if self.mode == "fixture" else self.db.settings.index_wait_seconds,
                                )

                            try:
                                retrieval = await self.gateway.tool(
                                    "retrieve", f"{task.id}:retrieve:initial", initial_retrieval, task.id
                                )
                            except BudgetExceeded as exc:
                                # A racing task may consume the last global retrieval
                                # reservation after the check above. Retrieval quota is
                                # tool-local: continue with authorized sources instead
                                # of terminating the research run.
                                if str(exc) != "retrieval_calls":
                                    raise
                                retrieval["fallback"] = "retrieval_budget_exhausted"
                        if self.db.settings.retrieval_shadow:
                            await self.put(
                                "retrieval_shadow",
                                f"{task.id}:retrieval-shadow",
                                {"task_id": task.id, **retrieval},
                            )
                            retrieval = {**retrieval, "results": []}
                        if retrieval.get("fallback"):
                            await self.put(
                                "warning",
                                f"{task.id}:retrieval-fallback",
                                {"task_id": task.id, "reason": retrieval["fallback"]},
                            )
                    initial_unresolved = (
                        ["search_sources: fallback:" + retrieval["fallback"]]
                        if retrieval.get("fallback")
                        else []
                    )
                    initial = (
                        None
                        if existing.values
                        else {
                            "task": task.model_dump(mode="json"),
                            "dependency_context": {
                                "claims": [
                                    {
                                        "id": claim["id"],
                                        "text": claim["text"],
                                        "span_ids": claim["span_ids"],
                                        "stance": claim["stance"],
                                    }
                                    for claim in dependency_claims
                                ],
                                "spans": [
                                    {
                                        "id": span["id"],
                                        "source_id": span["source_id"],
                                        "start": span["start"],
                                        "end": span["end"],
                                    }
                                    for span in dependency_spans
                                ],
                                "findings": [
                                    {
                                        "task_id": finding["task_id"],
                                        "summary": finding["summary"],
                                        "unresolved": finding["unresolved"],
                                    }
                                    for finding in dependency_findings
                                ],
                                "note": "Unreviewed prior-module evidence; re-read original sources before extending claims.",
                            },
                            "messages": [],
                            "source_ids": authorized_sources,
                            "read_slices": [],
                            "retrieval_hits": retrieval.get("results", []),
                            "iteration": 0,
                            "unresolved": initial_unresolved,
                            "progress": ResearchProgress(
                                authorized_source_ids=authorized_sources,
                                candidate_urls=list(self.brief.source_urls),
                                retrieval_hits=retrieval.get("results", []),
                                acceptance_coverage=[],
                                unresolved=initial_unresolved,
                                last_tool_outcomes=(
                                    [
                                        {
                                            "tool": "search_sources",
                                            "ok": True,
                                            "result_count": len(retrieval.get("results", [])),
                                            "fallback": retrieval["fallback"],
                                        }
                                    ]
                                    if retrieval.get("fallback")
                                    else []
                                ),
                            ).model_dump(mode="json"),
                        }
                    )
                    await graph.ainvoke(initial, config)
                    # A crash between child END and parent commit can replay this branch.
                    saved = await self.db.get(self.tenant, task.id, "task")
                    if saved["execution_status"] == "running":
                        saved["execution_status"] = "completed"
                        await self.put("task", task.id, saved, immutable=False)
                except BudgetExceeded as exc:
                    task.execution_status, task.stop_reason = (
                        "failed",
                        type(exc).__name__ + ":" + str(exc)[:160],
                    )
                    await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
                    research_budget_stop = "budget:" + str(exc)
                    await self.db.emit_event(
                        self.tenant,
                        self.run_id,
                        "research.budget_exhausted",
                        {"task_id": task.id, "reason": research_budget_stop},
                    )
                    failed.add(task.id)
                except (ProviderError, ValueError) as exc:
                    task.execution_status, task.stop_reason = (
                        "failed",
                        type(exc).__name__ + ":" + str(exc)[:160],
                    )
                    await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
                    failed.add(task.id)
                else:
                    succeeded.add(task.id)
                finished.add(task.id)

        while len(finished) < len(tasks):
            if research_budget_stop:
                for task in tasks:
                    if task.id in finished:
                        continue
                    task.execution_status = "cancelled"
                    task.stop_reason = "deferred_after_" + research_budget_stop
                    await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
                    finished.add(task.id)
                    failed.add(task.id)
                break
            blocked = [task for task in tasks if task.id not in finished and set(task.depends_on) & failed]
            for task in blocked:
                task.execution_status = "cancelled"
                task.stop_reason = "dependency_failed"
                await self.put("task", task.id, task.model_dump(mode="json"), immutable=False)
                finished.add(task.id)
                failed.add(task.id)
            ready = [task for task in tasks if task.id not in finished and set(task.depends_on) <= succeeded]
            if not ready:
                if len(finished) == len(tasks):
                    break
                raise ValueError("unschedulable_plan")
            await asyncio.gather(*(run_task(t) for t in ready))
        result = {"tasks": await self.db.records(self.tenant, self.run_id, "task")}
        if research_budget_stop:
            result.update(
                {
                    "stop_reason": research_budget_stop,
                    "research_budget_exhausted": True,
                }
            )
        return result

    async def review(self, state):
        await self.phase("review", gap_round=state.get("gap_round", 0))
        claims = await self.claims()
        spans = await self.db.records(self.tenant, self.run_id, "span")
        if state.get("report"):
            draft = ReportDraft.model_validate(state["report"])
            cited = {c for n in draft.nodes for c in n.claim_ids}
            cited.update(c for n in draft.nodes for values in n.cell_claim_ids.values() for c in values)
            if draft.paper:
                cited.update(draft.paper.claim_ids)
                cited.update(c for x in draft.paper.limitations + draft.paper.ideas for c in x.claim_ids)
            if draft.introduction:
                cited.update(draft.introduction.claim_ids)
            cited.update(c for experiment in draft.experiments for c in experiment.claim_ids)
            claims = [c for c in claims if c["id"] in cited]
            cited_spans = {s for c in claims for s in c["span_ids"]}
            spans = [s for s in spans if s["id"] in cited_spans]
        question_ids = [t["question_id"] for t in state["plan"]["tasks"]]
        intent_data = state.get("intent") or state.get("plan", {}).get("intent")
        intent = ResearchIntent.model_validate(intent_data) if intent_data else None
        allowed_methods = (
            {
                decision.stage.value: [method.value for method in decision.methods]
                for decision in intent.stages
            }
            if intent
            else {}
        )
        payload = {
            "role": "reviewer",
            "brief": self.brief.model_dump(mode="json"),
            "research_intent": state.get("intent") or state.get("plan", {}).get("intent"),
            "allowed_gap_methods_by_stage": allowed_methods,
            "claims": claims,
            "spans": spans,
            "question_ids": question_ids,
            "variant": self.profile.variant,
            "gap_round": state.get("gap_round", 0),
            "report": state.get("report"),
            "tasks": [
                {
                    k: t[k]
                    for k in (
                        "id",
                        "question_id",
                        "objective",
                        "stage",
                        "method",
                        "output_role",
                        "execution_status",
                        "stop_reason",
                    )
                }
                for t in state["tasks"]
            ],
            "sources": [
                {
                    k: s.model_dump(mode="json")[k]
                    for k in ("id", "title", "url", "raw_hash", "provenance_cluster", "warnings")
                }
                for s in await self.sources()
            ],
            "provenance_groups": provenance_groups(await self.sources()),
            "instructions": "Independently check entailment, qualifiers, coverage, contradictions, source independence"
            " and paper explanation fidelity. Also audit factual assertions outside the claim list (tables and paper fields)."
            " Do not treat author-reported results as reproduced. Mark research hypotheses as hypotheses, not established facts."
            " For report defects set location to the exact node ID, or 'paper' for the structured paper block."
            " Keep each finding concise (reason and suggestion under 160 Chinese characters). Group repeated issues."
            " Verify that every selected research stage has its promised deliverable. Introduction must make the evidence-backed"
            " context, gap, objective, rationale and significance legible. Experiment outputs must disclose whether anything was"
            " actually executed. Return gap tasks only when missing evidence can materially change the answer.",
        }
        key = f"review:g{state.get('gap_round', 0)}:r{state.get('revision', 0)}"
        payload = await self.evidence_context("reviewer", key, payload)
        review_budget_stop = None
        try:
            review = Review.model_validate(
                await self.gateway.structured("reviewer", key, payload, Review, closing=True)
            )
        except BudgetExceeded as exc:
            # Independent review is preferable, but exhausting its reservation
            # must not make the already-collected evidence unreachable to Writer.
            # Exact-span extraction has already run; expose those claims as
            # provisional and force needs_review at publication.
            review_budget_stop = "budget:" + str(exc)
            visible_claims = payload.get("claims", [])
            visible_task_ids = {claim["task_id"] for claim in visible_claims}
            review = Review(
                sufficient=False,
                accepted_claim_ids=[claim["id"] for claim in visible_claims],
                covered_question_ids=[
                    task["question_id"] for task in state["tasks"] if task["id"] in visible_task_ids
                ],
                findings=[
                    ReviewFinding(
                        id=stable_id(self.run_id, key, "budget-review"),
                        location="review",
                        severity="warning",
                        category="coverage",
                        reason=f"Independent review was curtailed by {review_budget_stop}; exact-span claims are provisional.",
                        suggestion="Use the provisional evidence in a bounded report and disclose missing coverage.",
                    )
                ],
            )
            await self.db.emit_event(
                self.tenant,
                self.run_id,
                "review.degraded",
                {"reason": review_budget_stop, "provisional_claims": len(visible_claims)},
            )
        level = await self.budget_level()
        valid_gap_tasks = []
        for gap_task in review.gap_tasks:
            stage = gap_task.stage.value
            method = gap_task.method.value
            reason = None
            if stage not in allowed_methods or method not in allowed_methods[stage]:
                reason = "gap_task_outside_research_intent"
            elif state.get("research_budget_exhausted") or review_budget_stop:
                reason = "research_budget_exhausted"
            elif level >= 95:
                reason = "soft_token_95_threshold"
            if reason is None:
                valid_gap_tasks.append(gap_task)
                continue
            review.sufficient = False
            review.findings.append(
                ReviewFinding(
                    id=stable_id(self.run_id, key, "gap-rejected", gap_task.id),
                    location="review.gap_tasks",
                    severity="warning",
                    category="instruction",
                    reason=f"Rejected gap task {gap_task.id}: {reason} ({stage}/{method}).",
                    suggestion="Resolve the disclosed evidence gap in a new run or revise the frozen research intent.",
                )
            )
            await self.db.emit_event(
                self.tenant,
                self.run_id,
                "review.gap_rejected",
                {
                    "task_id": gap_task.id,
                    "stage": stage,
                    "method": method,
                    "allowed_methods": allowed_methods.get(stage, []),
                    "reason": reason,
                },
            )
        review.gap_tasks = valid_gap_tasks
        if state.get("research_budget_exhausted"):
            review.sufficient = False
            budget_finding_id = stable_id(self.run_id, key, "budget-coverage")
            if not any(finding.id == budget_finding_id for finding in review.findings):
                review.findings.append(
                    ReviewFinding(
                        id=budget_finding_id,
                        location="research",
                        severity="warning",
                        category="coverage",
                        reason="Research stopped at its budget boundary; one or more planned deliverables remain incomplete.",
                        suggestion="Write a bounded report from accepted evidence and disclose every deferred task.",
                    )
                )
        known = {c["id"] for c in claims}
        visible = {c["id"] for c in payload["claims"]}
        if not set(review.accepted_claim_ids) <= known:
            raise ValueError("reviewer_invented_claim_ids")
        # A claim omitted from this model request cannot be accepted by this review.
        review.accepted_claim_ids = [cid for cid in review.accepted_claim_ids if cid in visible]
        if visible != known:
            review.sufficient = False
            review.findings.append(
                ReviewFinding(
                    id=stable_id(self.run_id, key, "context-omission"),
                    location="context",
                    severity="warning",
                    category="coverage",
                    reason="Some claims were omitted from the bounded review context and remain unreviewed.",
                    suggestion="Narrow the brief or split the research; do not claim complete evidence coverage.",
                )
            )
        if not set(question_ids) <= set(review.covered_question_ids):
            review.sufficient = False
        # The same deterministic gate runs before routing and again at publication.
        # Model review alone can miss structural defects such as absent per-cell refs.
        if state.get("report"):
            _, _, _, _, errors = await self.report_checks({**state, "review": review.model_dump(mode="json")})
            locations = [n["id"] for n in state["report"]["nodes"]]
            grouped = {}
            for error in errors:
                location = next((n for n in locations if error.startswith(n + ":")), None)
                if not location and error.startswith("paper ") and state["report"].get("paper"):
                    location = "paper"
                if not location and error.startswith("introduction:") and state["report"].get("introduction"):
                    location = "introduction"
                if not location and error.startswith("experiments:") and state["report"].get("experiments"):
                    location = "experiments"
                if location:
                    grouped.setdefault(location, []).append(error)
            for location, reasons in grouped.items():
                review.findings.append(
                    ReviewFinding(
                        id=stable_id(self.run_id, key, "gate", location),
                        location=location,
                        severity="error",
                        category="evidence",
                        reason="Deterministic report check: " + "; ".join(reasons),
                        suggestion="Bind each factual cell with cell_claim_ids['0.column'] to accepted evidence; "
                        "remove unsupported assertions or explicitly mark unavailable values as unknown. "
                        "Keep numeric formatting consistent with source evidence.",
                    )
                )
        # Preserve authorized report defects across evidence gathering. A new evidence-only
        # review must not accidentally publish the old, unchanged draft.
        if state.get("pending_patch_findings"):
            locations = {f.location for f in review.findings}
            review.findings.extend(
                Review.model_validate({"findings": [f]}).findings[0]
                for f in state["pending_patch_findings"]
                if f["location"] not in locations
            )
        accepted = set(review.accepted_claim_ids)
        for task in state["tasks"]:
            task_claims = {c["id"] for c in claims if c["task_id"] == task["id"]}
            task["acceptance"] = "accepted" if task_claims and task_claims <= accepted else "rejected"
            await self.put("task", task["id"], task, immutable=False)
        await self.put("review", self.run_id + ":" + key, review.model_dump(mode="json"))
        unique_evidence = len({(s["source_id"], s["start"], s["end"]) for s in spans})
        signal = digest(
            {
                "spans": sorted({(s["source_id"], s["start"], s["end"]) for s in spans}),
                "origins": provenance_groups(await self.sources()),
                "covered": sorted(review.covered_question_ids),
                "conflicts": sorted(
                    (f.location, f.reason) for f in review.findings if f.category == "conflict"
                ),
            }
        )
        result = {
            "review": review.model_dump(mode="json"),
            "previous_claims": unique_evidence,
            "previous_signal": signal,
            "no_gain": state.get("gap_round", 0) > 0 and state.get("previous_signal") == signal,
            "degradation_level": level,
        }
        if review_budget_stop and not state.get("stop_reason"):
            result.update(
                {
                    "stop_reason": review_budget_stop,
                    "research_budget_exhausted": True,
                }
            )
        return result

    def route_review(self, state):
        review = state["review"]
        if state.get("research_budget_exhausted") or str(state.get("stop_reason", "")).startswith("budget:"):
            return "publish" if state.get("report") else "write"
        if (
            self.profile.variant == "B2"
            and review["gap_tasks"]
            and not state.get("no_gain")
            and state.get("gap_round", 0) < self.profile.max_gap_rounds
            and len(state["tasks"]) < self.profile.max_tasks
            and state.get("degradation_level", 0) < 95
        ):
            return "gaps"
        if not state.get("report"):
            return "write"
        errors = any(f["severity"] == "error" for f in review["findings"])
        if errors and state.get("patch_round", 0) < self.profile.max_patch_rounds:
            return "patch"
        return "publish"

    def route_write(self, state):
        if state.get("research_budget_exhausted") or str(state.get("stop_reason", "")).startswith("budget:"):
            return "publish"
        return "review"

    async def gaps(self, state):
        tasks = list(state["tasks"])
        proposals = state["review"]["gap_tasks"][: self.profile.max_tasks - len(tasks)]
        mapping = {
            t["id"]: stable_id(self.run_id, "gap", str(state["gap_round"]), t["id"]) for t in proposals
        }
        known = {t["id"] for t in tasks}
        intent = ResearchIntent.model_validate(state.get("intent") or state["plan"]["intent"])
        decisions = {decision.stage: decision for decision in intent.stages}
        for proposal in proposals:
            t = ResearchTask.model_validate(proposal)
            if t.stage not in decisions or t.method not in decisions[t.stage].methods:
                await self.db.emit_event(
                    self.tenant,
                    self.run_id,
                    "review.gap_rejected",
                    {
                        "task_id": t.id,
                        "stage": t.stage.value,
                        "method": t.method.value,
                        "allowed_methods": [m.value for m in decisions.get(t.stage, intent.stages[0]).methods]
                        if decisions
                        else [],
                        "reason": "gap_task_outside_research_intent_defensive_gate",
                    },
                )
                continue
            t.output_role = decisions[t.stage].role
            t.id = mapping[t.id]
            # Reviewer may reference existing tasks, but cannot introduce cycles or hidden dependencies.
            if not set(t.depends_on) <= known:
                t.depends_on = []
            t.source_ids = [s.id for s in await self.sources()]
            await self.put("task", t.id, t.model_dump(mode="json"), immutable=False)
            tasks.append(t.model_dump(mode="json"))
        return {
            "tasks": tasks,
            "gap_round": state["gap_round"] + 1,
            "pending_patch_findings": state["review"]["findings"] if state.get("report") else [],
        }

    async def degraded_report(self, state, reason: str) -> ReportDraft:
        """Build a useful, auditable report without another model reservation.

        This is the last line of defence for a closing-stage budget failure. It
        synthesizes task findings rather than dumping arbitrary claims in storage
        order, and makes every missing deliverable explicit.
        """
        tasks = state.get("tasks") or await self.db.records(self.tenant, self.run_id, "task")
        findings = await self.db.records(self.tenant, self.run_id, "finding")
        finding_by_task = {finding["task_id"]: finding for finding in findings}
        accepted = set(state.get("review", {}).get("accepted_claim_ids", []))
        completed = sum(task.get("execution_status") == "completed" for task in tasks)
        nodes = [
            ReportNode(
                id="research-status",
                kind="section",
                title="研究状态与可用结论",
                text=(
                    f"本次研究在预算保护下提前收口：{len(tasks)} 个任务中完成 {completed} 个。"
                    "以下内容按原定交付模块整理；未完成部分明确标为待验证，不代表研究目标已经满足。"
                ),
                attribution="guidance",
                output_mode="guidance",
            )
        ]
        unresolved = [reason]
        output_modes = {
            ResearchStage.METHODOLOGY.value: "research_proposal",
            ResearchStage.EXPERIMENT.value: "experiment_plan",
        }
        for task in tasks:
            if task.get("output_role") != "deliverable":
                continue
            finding = finding_by_task.get(task["id"])
            claim_ids = [cid for cid in (finding or {}).get("claim_ids", []) if cid in accepted]
            if finding and finding.get("summary") and claim_ids:
                text = finding["summary"]
                attribution = "source_statement"
            else:
                criteria = "；".join(task.get("acceptance_criteria", [])) or "原定验收条件"
                text = (
                    f"该模块未形成经过独立审查的完整交付物。原定目标：{task.get('deliverable', task['objective'])}。"
                    f"仍需完成：{criteria}。"
                )
                attribution = "guidance"
            nodes.append(
                ReportNode(
                    id="degraded-" + task["id"],
                    kind="section",
                    title=task.get("deliverable") or task["objective"],
                    text=text,
                    claim_ids=claim_ids,
                    attribution=attribution,
                    stage=task.get("stage"),
                    output_mode=output_modes.get(task.get("stage"), "evidence_synthesis"),
                    execution_status=(
                        "not_executed"
                        if task.get("stage") in {"methodology", "experiment"}
                        else "not_applicable"
                    ),
                )
            )
            if task.get("stop_reason"):
                unresolved.append(f"{task.get('stage', 'task')}: {task['stop_reason']}")
            unresolved.extend((finding or {}).get("unresolved", []))
        nodes.append(
            ReportNode(
                id="next-run-boundary",
                kind="section",
                title="下一步验证边界",
                text=(
                    "优先补齐标记为未完成的交付模块，并复核 provisional evidence；在这些缺口解决前，"
                    "不得把当前内容解释为完整方案、复现实验结果或优越性结论。"
                ),
                attribution="guidance",
                output_mode="guidance",
            )
        )
        return ReportDraft(
            title="阶段性研究报告",
            nodes=nodes[:8],
            unresolved=list(dict.fromkeys(unresolved))[:40],
        )

    async def write(self, state):
        if state.get("report"):
            return await self.patch(state)
        await self.phase("writing")
        accepted = set(state["review"]["accepted_claim_ids"])
        claims = [c for c in await self.claims() if c["id"] in accepted]
        payload = {
            "role": "writer",
            "brief": self.brief.model_dump(mode="json"),
            "research_intent": state.get("intent") or state.get("plan", {}).get("intent"),
            "plan": state.get("plan"),
            "claims": claims,
            "spans": await self.db.records(self.tenant, self.run_id, "span"),
            "findings": [
                {"task_id": f["task_id"], "unresolved": f["unresolved"]}
                for f in await self.db.records(self.tenant, self.run_id, "finding")
            ],
            "review": state["review"],
            "instructions": "Produce stable node IDs and cite claim IDs for every external factual"
            " assertion including comparison table cells. Use cell_claim_ids keyed by zero-based row.column for each factual cell."
            " Unknown data must stay unknown. Do not assign unsupported scores."
            " Technical comparison needs a comparison_table and conditional decision memo. Paper review must include the"
            " paper object: explain principles, implementation, experiment design, author/inferred limitations and actionable"
            " hypotheses with baseline, metric and failure risk. Novelty is unverified. Populate the structured introduction"
            " object whenever Introduction is selected, following context -> gap -> objective -> rationale -> significance."
            " Populate structured experiments whenever Experiment is selected. No experiment execution.",
        }
        payload["instructions"] += (
            " Organize the report by the selected research stages, set stage on every substantive node, and make each node's"
            " output_mode explicit. For introduction use context -> gap -> objective -> rationale -> significance, not a generic"
            " summary. For related_work compare claims, methods and limitations across sources. For experiment return an"
            " experiment_plan unless actual run artifacts are present; set execution_status honestly. Keep the report concise:"
            " Compose only role=deliverable modules into report sections. Use claims from role=supporting tasks inside those"
            " deliverables, but never emit a standalone section for a supporting module."
            " at most 8 nodes, under 4000 Chinese characters of prose. Do not repeat the evidence appendix in report prose."
        )
        payload = await self.evidence_context("writer", "draft", payload)
        try:
            report = ReportDraft.model_validate(
                await self.gateway.structured("writer", "draft", payload, ReportDraft, closing=True)
            )
        except BudgetExceeded as exc:
            reason = state.get("stop_reason") or "budget:" + str(exc)
            report = await self.degraded_report(state, reason)
            await self.db.emit_event(
                self.tenant,
                self.run_id,
                "writer.degraded",
                {"reason": "budget:" + str(exc), "nodes": len(report.nodes)},
            )
            await self.put("draft", self.run_id + ":draft:1", report.model_dump(mode="json"))
            return {
                "report": report.model_dump(mode="json"),
                "revision": 1,
                "stop_reason": reason,
                "research_budget_exhausted": True,
            }
        await self.put("draft", self.run_id + ":draft:1", report.model_dump(mode="json"))
        return {"report": report.model_dump(mode="json"), "revision": 1}

    async def patch(self, state):
        if state.get("patch_round", 0) >= self.profile.max_patch_rounds:
            return {}
        await self.phase("patching")
        report = ReportDraft.model_validate(state["report"])
        allowed = {
            f["location"] for f in state["review"]["findings"] if f["severity"] in {"error", "warning"}
        }
        allowed &= {n.id for n in report.nodes} | {"paper", "introduction", "experiments"}
        if not allowed:
            return {"patch_round": self.profile.max_patch_rounds}
        round_ = state.get("patch_round", 0) + 1
        payload = {
            "role": "patcher",
            "brief": self.brief.model_dump(mode="json"),
            "report": state["report"],
            "review": state["review"],
            "allowed_node_ids": sorted(allowed),
            "claims": await self.claims(),
            "spans": await self.db.records(self.tenant, self.run_id, "span"),
            "instructions": "Replace only explicitly allowed existing nodes. Preserve all other IDs. Do not rewrite the report."
            " Structured paper, introduction and experiments blocks may be replaced only when their matching location is"
            " explicitly allowed; otherwise return the corresponding field as null.",
        }
        payload = await self.evidence_context("patcher", f"patch:{round_}", payload)
        patch = ReportPatch.model_validate(
            await self.gateway.structured("patcher", f"patch:{round_}", payload, ReportPatch, closing=True)
        )
        if len({n.id for n in patch.replacements}) != len(patch.replacements):
            raise ValueError("duplicate_patch_node_ids")
        if any(n.id not in allowed for n in patch.replacements):
            raise ValueError("patch_outside_authorized_nodes")
        if patch.paper is not None:
            if "paper" not in allowed or report.paper is None:
                raise ValueError("paper_patch_not_authorized")
            report.paper = patch.paper
        if patch.introduction is not None:
            if "introduction" not in allowed or report.introduction is None:
                raise ValueError("introduction_patch_not_authorized")
            report.introduction = patch.introduction
        if patch.experiments is not None:
            if "experiments" not in allowed or not report.experiments:
                raise ValueError("experiments_patch_not_authorized")
            report.experiments = patch.experiments
        replacements = {n.id: n for n in patch.replacements}
        report.nodes = [replacements.get(n.id, n) for n in report.nodes]
        report.unresolved = list(dict.fromkeys(report.unresolved + patch.unresolved))
        revision = state["revision"] + 1
        await self.put("draft", self.run_id + f":draft:{revision}", report.model_dump(mode="json"))
        return {
            "report": report.model_dump(mode="json"),
            "revision": revision,
            "patch_round": round_,
            "pending_patch_findings": [],
        }

    async def report_checks(self, state):
        report = ReportDraft.model_validate(state["report"])
        claims = [Claim.model_validate(c) for c in await self.claims()]
        spans = [
            EvidenceSpan.model_validate(s) for s in await self.db.records(self.tenant, self.run_id, "span")
        ]
        sources = await self.sources()
        errors = await self.evidence.validate_spans(spans)
        claim_map, span_map = {c.id: c for c in claims}, {s.id: s for s in spans}
        unknown_values = {"", "unknown", "未知", "待核验", "n/a"}
        for node in report.nodes:
            cell_refs = {c for refs in node.cell_claim_ids.values() for c in refs}
            all_refs = set(node.claim_ids) | cell_refs
            if not cell_refs <= set(claim_map):
                errors.append(node.id + ": unknown table cell claim reference")
            if node.kind == "comparison_table" and node.attribution == "source_statement":
                for row_index, row in enumerate(node.rows):
                    for column, value in row.items():
                        if value.strip().lower() not in unknown_values and not node.cell_claim_ids.get(
                            f"{row_index}.{column}"
                        ):
                            errors.append(f"{node.id}:{row_index}.{column}: table cell lacks evidence")
            if not set(node.claim_ids) <= set(claim_map):
                errors.append(node.id + ": unknown claim reference")
            has_facts = node.text.strip().lower() not in unknown_values or any(
                value.strip().lower() not in unknown_values for row in node.rows for value in row.values()
            )
            if node.attribution == "source_statement" and has_facts and not all_refs:
                errors.append(node.id + ": factual content without evidence")
            supporting_text = " ".join(
                span_map[sid].quote
                for cid in all_refs
                if cid in claim_map
                for sid in claim_map[cid].span_ids
                if sid in span_map
            )
            if node.attribution == "source_statement":
                numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?", node.text + json.dumps(node.rows)))
                if numbers - set(re.findall(r"\b\d+(?:\.\d+)?%?", supporting_text)):
                    errors.append(node.id + ": numerical claim requires verification")
        if self.brief.template == "paper_review" and report.paper is None:
            errors.append("paper understanding missing")
        if self.brief.template == "technical_comparison" and not any(
            n.kind == "comparison_table" for n in report.nodes
        ):
            errors.append("comparison matrix missing")
        intent_data = state.get("intent") or state.get("plan", {}).get("intent")
        if intent_data:
            intent = ResearchIntent.model_validate(intent_data)
            delivered = {node.stage for node in report.nodes if node.stage is not None}
            if report.introduction:
                delivered.add(ResearchStage.INTRODUCTION)
            if report.experiments:
                delivered.add(ResearchStage.EXPERIMENT)
            for decision in intent.stages:
                if decision.role != "deliverable":
                    continue
                if decision.stage not in delivered:
                    errors.append(f"research stage missing: {decision.stage.value}")
            supporting = {decision.stage for decision in intent.stages if decision.role == "supporting"}
            for node in report.nodes:
                if node.stage in supporting:
                    errors.append(node.id + ": supporting research stage leaked into final report")
            if ResearchStage.INTRODUCTION in supporting and report.introduction:
                errors.append("introduction: supporting research stage leaked into final report")
            if ResearchStage.EXPERIMENT in supporting and report.experiments:
                errors.append("experiments: supporting research stage leaked into final report")
            for node in report.nodes:
                if node.output_mode == "experiment_result" and node.execution_status != "executed":
                    errors.append(node.id + ": experiment result is not marked executed")
                if node.execution_status == "executed" and node.output_mode != "experiment_result":
                    errors.append(node.id + ": executed status requires an experiment result")
            selected = {decision.stage for decision in intent.stages if decision.role == "deliverable"}
            if ResearchStage.INTRODUCTION in selected and report.introduction is None:
                errors.append("introduction: structured argument missing")
            if ResearchStage.EXPERIMENT in selected and not report.experiments:
                errors.append("experiments: structured experiment package missing")
        if report.introduction:
            if not set(report.introduction.claim_ids) <= set(claim_map):
                errors.append("introduction: unknown claim reference")
            if not report.introduction.claim_ids:
                errors.append("introduction: gap/context require evidence")
        for experiment in report.experiments:
            if not set(experiment.claim_ids) <= set(claim_map):
                errors.append(f"experiments:{experiment.id}: unknown claim reference")
            if experiment.execution_status == "executed" and not experiment.artifact_ids:
                errors.append(f"experiments:{experiment.id}: executed experiment lacks artifacts")
        if report.paper:
            refs = report.paper.claim_ids + [x for lim in report.paper.limitations for x in lim.claim_ids]
            refs += [x for idea in report.paper.ideas for x in idea.claim_ids]
            if not set(refs) <= set(claim_map) or not report.paper.claim_ids:
                errors.append("paper evidence references missing or invalid")
        review = state.get("review", {})
        accepted = set(review.get("accepted_claim_ids", []))
        for node in report.nodes:
            refs = set(node.claim_ids) | {c for values in node.cell_claim_ids.values() for c in values}
            if not refs <= accepted:
                errors.append(node.id + ": references unaccepted claims")
        if report.paper:
            refs = set(report.paper.claim_ids)
            refs.update(c for item in report.paper.limitations + report.paper.ideas for c in item.claim_ids)
            if not refs <= accepted:
                errors.append("paper references unaccepted claims")
        if report.introduction and not set(report.introduction.claim_ids) <= accepted:
            errors.append("introduction: references unaccepted claims")
        for experiment in report.experiments:
            if not set(experiment.claim_ids) <= accepted:
                errors.append(f"experiments:{experiment.id}: references unaccepted claims")
        if not claims:
            errors.append("no valid evidence collected")
        if not review.get("sufficient") or any(f["severity"] == "error" for f in review.get("findings", [])):
            errors.append("review coverage/support requirements unresolved")
        if any(
            t["execution_status"] in {"failed", "cancelled"}
            for t in await self.db.records(self.tenant, self.run_id, "task")
        ):
            errors.append("one or more research tasks failed or were deferred")
        return report, claims, spans, sources, errors

    async def publish(self, state):
        await self.phase("citation_check")
        report, claims, spans, sources, errors = await self.report_checks(state)
        report.unresolved = list(dict.fromkeys(report.unresolved + errors))
        quality = "needs_review" if errors else "passed"
        # A synthetic test can pass workflow checks but is never a verified research result.
        if self.mode == "fixture" and quality == "passed":
            quality = "unchecked"
        run = await self.db.run(self.tenant, self.run_id)
        package = ResearchPackage(
            run_id=self.run_id,
            revision=state["revision"],
            report=report,
            markdown=render_markdown(report, claims, spans, sources),
            quality_status=quality,
            citation_errors=errors,
            claims=claims,
            spans=spans,
            sources=sources,
            manifest={
                "app_version": __version__,
                "workflow_version": "v1",
                "research_intent": state.get("intent") or state.get("plan", {}).get("intent"),
                "mode": self.mode,
                "brief_hash": digest(self.brief.model_dump(mode="json")),
                "profile": self.profile.model_dump(),
                "provider": "fixture" if self.mode == "fixture" else "deepseek",
                "model": self.execution_settings.deepseek_model,
                "thinking": self.execution_settings.deepseek_thinking,
                "effort": self.execution_settings.deepseek_effort,
                "execution_configuration": self.configuration,
                "source_hashes": [s.raw_hash for s in sources],
                "provenance_groups": provenance_groups(sources),
                "spent_usd": float(run["spent_usd"]),
                "reserved_usd": float(run["reserved_usd"]),
                "tokens": run["tokens"],
                "prompt_version": "v2-paper-stage-intent",
                "source_status": "snapshot_only; current retraction status unknown",
            },
        )
        await self.put(
            "report", self.run_id + f":report:{state['revision']}", package.model_dump(mode="json")
        )
        return {
            "stop_reason": state.get("stop_reason") or ("quality_passed" if not errors else "needs_review")
        }

    def graph(self):
        graph = StateGraph(ResearchState)
        for name, node in [
            ("intake", self.bootstrap),
            ("scope", self.understand),
            ("plan", self.plan),
            ("research", self.research),
            ("review", self.review),
            ("gaps", self.gaps),
            ("write", self.write),
            ("patch", self.patch),
            ("publish", self.publish),
        ]:
            graph.add_node(name, node)
        graph.add_edge(START, "intake")
        graph.add_edge("intake", "scope")
        graph.add_edge("scope", "plan")
        graph.add_edge("plan", "research")
        graph.add_edge("research", "review")
        graph.add_conditional_edges("review", self.route_review)
        graph.add_edge("gaps", "research")
        graph.add_conditional_edges("write", self.route_write)
        graph.add_edge("patch", "review")
        graph.add_edge("publish", END)
        return graph.compile(checkpointer=self.saver)

    async def execute(self):
        graph = self.graph()
        config = {"configurable": {"thread_id": self.run_id}, "recursion_limit": 100}
        snapshot = await graph.aget_state(config)
        return await graph.ainvoke(None if snapshot.values else {}, config)

    async def partial(self, reason: str):
        claims = await self.claims()
        tasks = await self.db.records(self.tenant, self.run_id, "task")
        plans = await self.db.records(self.tenant, self.run_id, "plan")
        intents = await self.db.records(self.tenant, self.run_id, "intent")
        review = Review(
            sufficient=False,
            accepted_claim_ids=[claim["id"] for claim in claims],
            covered_question_ids=list(
                dict.fromkeys(
                    task["question_id"]
                    for task in tasks
                    if any(claim["task_id"] == task["id"] for claim in claims)
                )
            ),
            findings=[
                ReviewFinding(
                    id=stable_id(self.run_id, "terminal-partial", reason),
                    location="run",
                    severity="warning",
                    category="coverage",
                    reason="The normal workflow terminated before independent review: " + reason,
                    suggestion="Treat all retained claims as provisional and complete the deferred modules in a new run.",
                )
            ],
        )
        state = {
            "tasks": tasks,
            "review": review.model_dump(mode="json"),
            "stop_reason": reason if reason.startswith("budget:") else "terminal:" + reason,
            "research_budget_exhausted": reason.startswith("budget:"),
            "revision": 999,
        }
        if plans:
            state["plan"] = plans[-1]
        if intents:
            state["intent"] = intents[-1]
        state["report"] = (await self.degraded_report(state, reason)).model_dump(mode="json")
        await self.publish(state)
