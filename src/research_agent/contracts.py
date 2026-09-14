"""Versioned domain contracts; no framework or provider imports."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1


class Template(StrEnum):
    TECHNICAL = "technical_comparison"
    PAPER = "paper_review"
    GENERAL = "general_research"


class ResearchStage(StrEnum):
    """A paper-shaped unit of work, selected from the user's actual intent."""

    INTRODUCTION = "introduction"
    RELATED_WORK = "related_work"
    METHODOLOGY = "methodology"
    EXPERIMENT = "experiment"
    RESULTS = "results"
    DISCUSSION = "discussion"
    CONCLUSION = "conclusion"


class ResearchMethod(StrEnum):
    LITERATURE_SEARCH = "literature_search"
    SOURCE_SYNTHESIS = "source_synthesis"
    GAP_ANALYSIS = "gap_analysis"
    REPRODUCTION = "reproduction"
    EXPERIMENT_EXECUTION = "experiment_execution"
    EVIDENCE_AUDIT = "evidence_audit"


class ResearchBrief(Contract):
    question: str = Field(min_length=8, max_length=6000)
    template: Template = Template.GENERAL
    audience: str = Field(default="engineers and researchers", max_length=500)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    candidates: list[str] = Field(default_factory=list, max_length=12)
    dimensions: list[str] = Field(default_factory=list, max_length=15)
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    upload_ids: list[str] = Field(default_factory=list, max_length=20)
    language: Literal["zh", "en"] = "zh"
    as_of: str | None = None
    version: int = 1


class SourceAssignment(Contract):
    source_id: str
    role: Literal["primary_reference", "supporting_reference", "domain_seed", "user_material"]
    reason: str = Field(min_length=1, max_length=500)


class StageDecision(Contract):
    stage: ResearchStage
    role: Literal["deliverable", "supporting"]
    objective: str = Field(min_length=1, max_length=1000)
    methods: list[ResearchMethod] = Field(min_length=1, max_length=4)
    deliverable: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    depends_on: list[ResearchStage] = Field(default_factory=list, max_length=6)


class ResearchIntent(Contract):
    """The compiled interpretation of one messy, multimodal user request."""

    normalized_question: str = Field(min_length=8, max_length=6000)
    mode: Literal[
        "literature_review",
        "research_design",
        "reproduction",
        "experiment",
        "decision_support",
    ]
    stages: list[StageDecision] = Field(min_length=1, max_length=7)
    source_assignments: list[SourceAssignment] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    capability_gaps: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def valid_stage_dag(self):
        if len({item.stage for item in self.stages}) != len(self.stages):
            raise ValueError("research intent stages must be unique")
        if not any(item.role == "deliverable" for item in self.stages):
            raise ValueError("research intent requires at least one deliverable stage")
        known = {item.stage for item in self.stages}
        dependencies = {item.stage: set(item.depends_on) for item in self.stages}
        if any(stage in deps or not deps <= known for stage, deps in dependencies.items()):
            raise ValueError("research intent has invalid stage dependency")
        visited: set[ResearchStage] = set()
        while len(visited) < len(known):
            ready = {stage for stage, deps in dependencies.items() if stage not in visited and deps <= visited}
            if not ready:
                raise ValueError("research intent stage dependency cycle")
            visited.update(ready)
        needed: set[ResearchStage] = set()

        def include(stage: ResearchStage):
            if stage in needed:
                return
            needed.add(stage)
            for dependency in dependencies[stage]:
                include(dependency)

        for item in self.stages:
            if item.role == "deliverable":
                include(item.stage)
        if any(item.role == "supporting" and item.stage not in needed for item in self.stages):
            raise ValueError("supporting research stage is not required by a deliverable")
        return self


class RunProfile(Contract):
    name: str = "v1-dev"
    variant: Literal["B0", "B1", "B2"] = "B2"
    concurrency: int = Field(default=3, ge=1, le=3)
    max_tasks: int = Field(default=8, ge=1, le=8)
    max_gap_rounds: int = Field(default=2, ge=0, le=2)
    max_patch_rounds: int = Field(default=2, ge=0, le=2)
    max_tool_calls: int = Field(default=80, ge=1, le=80)
    max_task_tools: int = Field(default=12, ge=1, le=12)
    max_search_calls: int = Field(default=12, ge=0, le=12)
    max_task_search_calls: int = Field(default=3, ge=0, le=12)
    max_model_calls: int = Field(default=40, ge=1, le=40)
    max_tokens: int = Field(default=250000, ge=1000, le=250000)
    prompt_token_limit: int = Field(default=64000, ge=2000, le=128000)
    max_output_tokens: int = Field(default=32768, ge=512, le=32768)
    max_seconds: int = Field(default=1200, ge=10, le=1200)
    max_usd: float = Field(default=2.0, gt=0, le=2.0)
    max_retries: int = Field(default=2, ge=0, le=2)
    max_sources: int = Field(default=24, ge=1, le=24)
    paper_related_sources: int = Field(default=3, ge=0, le=3)
    context_strategy: Literal["evidence", "full"] = "evidence"
    search_policy: Literal["live", "frozen"] = "live"
    retrieval_strategy: Literal["sequential", "lexical", "hybrid"] = "hybrid"
    max_retrieval_calls: int = Field(default=3, ge=0, le=6)
    parser_mode: Literal["native", "auto", "enhanced"] = "auto"


class CreateRun(Contract):
    brief: ResearchBrief
    profile: RunProfile = Field(default_factory=RunProfile)


class ResearchTask(Contract):
    id: str
    question_id: str
    objective: str = Field(min_length=1, max_length=2000)
    query: str = Field(min_length=1, max_length=1000)
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=10)
    source_ids: list[str] = Field(default_factory=list)
    execution_status: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    stop_reason: str | None = None
    acceptance: Literal["unchecked", "accepted", "rejected"] = "unchecked"
    stage: ResearchStage = ResearchStage.RELATED_WORK
    method: ResearchMethod = ResearchMethod.LITERATURE_SEARCH
    deliverable: str = "Evidence-grounded synthesis"
    output_role: Literal["deliverable", "supporting"] = "deliverable"


class Plan(Contract):
    questions: list[str] = Field(min_length=1, max_length=8)
    tasks: list[ResearchTask] = Field(min_length=1, max_length=8)
    rationale: str
    intent: ResearchIntent | None = None

    @model_validator(mode="after")
    def valid_dag(self):
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("duplicate task ids")
        visited: set[str] = set()
        while len(visited) < len(ids):
            ready = {t.id for t in self.tasks if t.id not in visited and set(t.depends_on) <= visited}
            if not ready:
                raise ValueError("task dependency cycle or unknown dependency")
            visited.update(ready)
        return self


class SourceVersion(Contract):
    id: str
    title: str
    url: str | None = None
    filename: str | None = None
    mime: str
    raw_hash: str
    parsed_hash: str
    parser_version: str
    raw_key: str
    parsed_key: str
    retrieved_at: datetime = Field(default_factory=now)
    provenance_cluster: str
    warnings: list[str] = Field(default_factory=list)
    parse_status: Literal["ready", "fallback", "failed"] = "ready"
    parser_capabilities: list[str] = Field(default_factory=lambda: ["text", "locators"])


class TextBlock(Contract):
    id: str | None = None
    text: str
    start: int
    end: int
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    kind: Literal[
        "heading", "paragraph", "page", "list", "code", "table", "formula", "figure", "caption"
    ] = "paragraph"
    section_path: list[str] = Field(default_factory=list)
    extraction_method: Literal["native", "ocr", "layout", "formula_recognition", "vision"] = "native"
    confidence: float | None = Field(default=None, ge=0, le=1)
    table_cells: list[dict[str, Any]] = Field(default_factory=list)
    crop_key: str | None = None


class ParsedDocument(Contract):
    title: str
    text: str
    blocks: list[TextBlock]
    warnings: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=lambda: ["text", "locators"])
    reading_order_version: str = "native-v1"


class EvidenceSpan(Contract):
    id: str
    source_id: str
    parsed_hash: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    quote_hash: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    block_id: str | None = None
    element_kind: str = "paragraph"
    extraction_method: Literal["native", "ocr", "layout", "formula_recognition", "vision"] = "native"
    confidence: float | None = Field(default=None, ge=0, le=1)
    crop_key: str | None = None


class SourceIndexStatus(Contract):
    source_id: str
    parsed_hash: str
    status: Literal["pending", "processing", "lexical_ready", "ready", "failed"] = "pending"
    total_chunks: int = 0
    embedded_chunks: int = 0
    chunker_version: str = "locator-chunks-v1"
    embedding_model: str | None = None
    embedding_revision: str | None = None
    error: str | None = None


class RetrievalHit(Contract):
    source_id: str
    chunk_id: str
    title: str = ""
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    page: int | None = None
    kind: str = "paragraph"
    snippet: str
    score_details: dict[str, float] = Field(default_factory=dict)
    evidence: Literal[False] = False


class UploadStatus(Contract):
    upload_id: str
    status: Literal["pending", "processing", "ready", "failed"]
    source_id: str | None = None
    error: str | None = None
    progress: float = Field(default=0, ge=0, le=1)


class DerivedArtifact(Contract):
    id: str
    source_id: str
    kind: Literal["figure_description", "chart_extraction"]
    text: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    crop_key: str | None = None
    crop_hash: str | None = None
    model: str
    model_revision: str
    prompt_version: str
    attribution: Literal["system_inferred"] = "system_inferred"


class Claim(Contract):
    id: str
    task_id: str
    text: str
    span_ids: list[str] = Field(min_length=1)
    stance: Literal["supports", "contradicts", "background"] = "supports"
    attribution: Literal["source_statement", "inference"] = "source_statement"
    limitations: list[str] = Field(default_factory=list)


class Extraction(Contract):
    text: str
    source_id: str
    quote: str = Field(default="", max_length=6000)
    passage_id: str | None = None
    stance: Literal["supports", "contradicts", "background"] = "supports"
    limitations: list[str] = Field(default_factory=list)


class ResearchFinding(Contract):
    task_id: str
    summary: str
    extractions: list[Extraction] = Field(default_factory=list, max_length=16)
    unresolved: list[str] = Field(default_factory=list)


class ReviewFinding(Contract):
    id: str = Field(default_factory=uid)
    location: str
    severity: Literal["info", "warning", "error"]
    category: Literal["evidence", "coverage", "conflict", "instruction", "explanation"]
    reason: str
    suggestion: str
    claim_ids: list[str] = Field(default_factory=list)


class Review(Contract):
    findings: list[ReviewFinding] = Field(default_factory=list)
    accepted_claim_ids: list[str] = Field(default_factory=list)
    covered_question_ids: list[str] = Field(default_factory=list)
    gap_tasks: list[ResearchTask] = Field(default_factory=list, max_length=3)
    sufficient: bool = False


class Limitation(Contract):
    text: str
    attribution: Literal["author_stated", "system_inferred"]
    claim_ids: list[str] = Field(default_factory=list)


class ResearchIdea(Contract):
    motivation: str
    hypothesis: str
    experiment: str
    baseline: str
    metric: str
    expected_signal: str
    failure_risk: str
    claim_ids: list[str] = Field(default_factory=list)
    novelty: Literal["unverified"] = "unverified"


class PaperUnderstanding(Contract):
    takeaways: list[str]
    problem: str
    contributions: list[str]
    principles: str
    implementation: str
    experiments: list[dict[str, str]]
    limitations: list[Limitation]
    ideas: list[ResearchIdea]
    claim_ids: list[str] = Field(default_factory=list)
    reproduction_status: Literal["not_executed"] = "not_executed"


class IntroductionArgument(Contract):
    context: str
    gap: str
    objective: str
    rationale: str
    significance: str
    hypotheses: list[str] = Field(default_factory=list, max_length=8)
    claim_ids: list[str] = Field(default_factory=list)


class ExperimentPlan(Contract):
    id: str
    hypothesis: str
    dataset: str
    baselines: list[str] = Field(min_length=1, max_length=12)
    protocol: str
    metrics: list[str] = Field(min_length=1, max_length=12)
    analysis_plan: str
    risks: list[str] = Field(default_factory=list, max_length=12)
    execution_status: Literal["not_executed", "planned", "executed", "blocked"]
    artifact_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)


class ReportNode(Contract):
    id: str
    kind: Literal["section", "paragraph", "comparison_table"]
    title: str = ""
    text: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)
    cell_claim_ids: dict[str, list[str]] = Field(default_factory=dict)
    attribution: Literal["source_statement", "inference", "hypothesis", "guidance"] = "source_statement"
    stage: ResearchStage | None = None
    output_mode: Literal[
        "evidence_synthesis", "research_proposal", "experiment_plan", "experiment_result", "guidance"
    ] = "evidence_synthesis"
    execution_status: Literal["not_applicable", "not_executed", "planned", "executed", "blocked"] = (
        "not_applicable"
    )


class ReportDraft(Contract):
    title: str
    nodes: list[ReportNode] = Field(min_length=1, max_length=40)
    paper: PaperUnderstanding | None = None
    introduction: IntroductionArgument | None = None
    experiments: list[ExperimentPlan] = Field(default_factory=list, max_length=8)
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_nodes(self):
        if len({n.id for n in self.nodes}) != len(self.nodes):
            raise ValueError("report nodes require unique stable ids")
        return self


class ReportPatch(Contract):
    replacements: list[ReportNode] = Field(default_factory=list)
    paper: PaperUnderstanding | None = None
    introduction: IntroductionArgument | None = None
    experiments: list[ExperimentPlan] | None = None
    unresolved: list[str] = Field(default_factory=list)


class ResearchPackage(Contract):
    run_id: str
    revision: int
    report: ReportDraft
    markdown: str
    quality_status: Literal["passed", "needs_review", "unchecked"]
    citation_errors: list[str]
    claims: list[Claim]
    spans: list[EvidenceSpan]
    sources: list[SourceVersion]
    manifest: dict[str, Any]


class ModelUsage(Contract):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    usd: float = 0
    estimated: bool = False
    cost_estimated: bool = True
    cost_basis: str = "configured_ceiling_rates_v1"


class ContextSnapshot(Contract):
    id: str = Field(default_factory=uid)
    role: str
    task_id: str | None = None
    strategy: str
    estimated_tokens_before: int
    estimated_tokens_after: int
    serialized_bytes_before: int = 0
    serialized_bytes_after: int = 0
    selected_ids: list[str]
    omitted_ids: list[str]
    constraints_hash: str
    truncated: bool
    token_estimator: str = "multilingual_conservative_v2"


class RunEvent(Contract):
    tenant_id: str
    run_id: str
    seq: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    task_id: str | None = None
    attempt_id: str | None = None
    trace_id: str


class EvaluationResult(Contract):
    run_id: str
    case_id: str
    split: str
    mode: str
    variant: str
    metrics: dict[str, Any]
    human_review: Literal["pending", "completed"] = "pending"
