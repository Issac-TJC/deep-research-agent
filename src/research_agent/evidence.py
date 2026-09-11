import json
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from research_agent.contracts import Claim, EvidenceSpan, Extraction, ParsedDocument, SourceVersion
from research_agent.db import Database
from research_agent.fetch import fetch_public
from research_agent.parser_service import parse_isolated
from research_agent.parsing import PARSER_VERSION
from research_agent.storage import ObjectStore, sha256


def stable_id(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join(parts)))


def candidate_passages(doc: ParsedDocument, start: int, end: int) -> list[dict]:
    """Model selects an ID; the application owns verbatim text and offsets. Never cross a locator block."""
    passages = []
    for block in doc.blocks:
        left, right = max(start, block.start), min(end, block.end)
        while left < right:
            stop = min(right, left + 800)
            if stop < right:
                boundary = doc.text.rfind("\n", left + 400, stop)
                if boundary > left:
                    stop = boundary
            passages.append(
                {"id": f"p{left}-{stop}", "start": left, "end": stop, "text": doc.text[left:stop]}
            )
            left = stop
    return passages


class EvidenceService:
    def __init__(self, db: Database, store: ObjectStore, tenant: str):
        self.db, self.store, self.tenant = db, store, tenant

    async def ingest(
        self, raw: bytes, mime: str, title: str, url: str | None = None, filename: str | None = None
    ) -> SourceVersion:
        parsed = await parse_isolated(raw, mime, title, self.db.settings)
        raw_hash = sha256(raw)
        parsed_raw = parsed.model_dump_json().encode()
        parsed_hash = sha256(parsed_raw)
        source_id = stable_id(self.tenant, raw_hash, parsed_hash, PARSER_VERSION, url or filename or "upload")
        try:
            return SourceVersion.model_validate(await self.db.get(self.tenant, source_id, "source"))
        except Exception as exc:
            from research_agent.db import NotFound

            if not isinstance(exc, NotFound):
                raise
        raw_key = await self.store.put(self.tenant, raw, "raw", mime)
        parsed_key = await self.store.put(self.tenant, parsed_raw, "parsed", "application/json")
        # Domain clusters are supplemented by raw-hash groups in each run's manifest.
        cluster = urlsplit(url).hostname if url else "uploaded:" + raw_hash
        version = SourceVersion(
            id=source_id,
            title=parsed.title,
            url=url,
            filename=filename,
            mime=mime,
            raw_hash=raw_hash,
            parsed_hash=parsed_hash,
            parser_version=PARSER_VERSION,
            raw_key=raw_key,
            parsed_key=parsed_key,
            provenance_cluster=cluster or raw_hash,
            warnings=parsed.warnings,
        )
        await self.db.put(self.tenant, "source", source_id, version.model_dump(mode="json"))
        return version

    async def fetch(self, url: str, mode: str) -> SourceVersion:
        if mode == "fixture":
            from research_agent.fixtures import CORPUS

            if url not in CORPUS:
                raise ValueError("fixture_mode_only_allows_fixture_urls")
            return await self.ingest(CORPUS[url].encode(), "text/markdown", url.rsplit("/", 1)[-1], url)
        raw, mime, canonical = await fetch_public(url, self.db.settings.max_upload_bytes)
        return await self.ingest(raw, mime, canonical, canonical)

    async def document(self, source_id: str) -> tuple[SourceVersion, ParsedDocument]:
        source = SourceVersion.model_validate(await self.db.get(self.tenant, source_id, "source"))
        raw = await self.store.get(self.tenant, source.parsed_key)
        if sha256(raw) != source.parsed_hash:
            raise ValueError("parsed_artifact_hash_mismatch")
        return source, ParsedDocument.model_validate_json(raw)

    async def extract(
        self,
        run_id: str,
        fence: int,
        task_id: str,
        extraction: Extraction,
        allowed_sources: set[str],
        start_hint: int = 0,
    ) -> tuple[Claim, EvidenceSpan]:
        if extraction.source_id not in allowed_sources:
            raise ValueError("unread_source_in_extraction")
        source, doc = await self.document(extraction.source_id)
        start = doc.text.find(extraction.quote, start_hint)
        if start < 0:
            raise ValueError("quote_not_found_verbatim")
        end = start + len(extraction.quote)
        block = next((b for b in doc.blocks if b.start <= start and b.end >= end), None)
        if not block:
            raise ValueError("quote_crosses_locator_blocks")
        span_id = stable_id(run_id, source.id, str(start), str(end))
        span = EvidenceSpan(
            id=span_id,
            source_id=source.id,
            parsed_hash=source.parsed_hash,
            start=start,
            end=end,
            quote=extraction.quote,
            quote_hash=sha256(extraction.quote.encode()),
            page=block.page,
            bbox=block.bbox,
        )
        claim_id = stable_id(run_id, task_id, span_id, extraction.text)
        claim = Claim(
            id=claim_id,
            task_id=task_id,
            text=extraction.text,
            span_ids=[span.id],
            stance=extraction.stance,
            limitations=extraction.limitations,
        )
        await self.db.put(self.tenant, "span", span.id, span.model_dump(mode="json"), run_id, fence)
        await self.db.put(self.tenant, "claim", claim.id, claim.model_dump(mode="json"), run_id, fence)
        return claim, span

    async def validate_spans(self, spans: list[EvidenceSpan]) -> list[str]:
        errors = []
        for span in spans:
            try:
                source, doc = await self.document(span.source_id)
                if source.parsed_hash != span.parsed_hash or doc.text[span.start : span.end] != span.quote:
                    errors.append(f"{span.id}: span version/offset mismatch")
                if sha256(span.quote.encode()) != span.quote_hash:
                    errors.append(f"{span.id}: quote hash mismatch")
            except (ValueError, PermissionError):
                errors.append(f"{span.id}: source cannot be verified")
        return errors


def provenance_groups(sources: list[SourceVersion]) -> list[list[str]]:
    """Conservatively union same-origin material and byte-identical cross-domain copies."""
    groups: list[set[str]] = []
    by_id = {source.id: source for source in sources}
    for source in sources:
        related = [
            group
            for group in groups
            if any(
                by_id[sid].raw_hash == source.raw_hash
                or by_id[sid].provenance_cluster == source.provenance_cluster
                for sid in group
            )
        ]
        merged = {source.id}.union(*related)
        groups = [group for group in groups if group not in related] + [merged]
    return sorted(sorted(group) for group in groups)


def render_markdown(
    report, claims: list[Claim], spans: list[EvidenceSpan], sources: list[SourceVersion]
) -> str:
    source_map, span_map = {s.id: s for s in sources}, {s.id: s for s in spans}
    claim_map = {c.id: c for c in claims}
    lines = ["# " + report.title, ""]
    for node in report.nodes:
        if node.title:
            lines.append("## " + node.title)
        if node.text:
            lines.append(node.text)
        if node.rows:
            columns = list(dict.fromkeys(k for row in node.rows for k in row))

            def clean(x):
                return str(x).replace("|", "\\|").replace("\n", " ")

            lines += ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
            for index, row in enumerate(node.rows):
                cells = []
                for column in columns:
                    refs = node.cell_claim_ids.get(f"{index}.{column}", [])
                    suffix = " " + " ".join(f"[{cid}]" for cid in refs) if refs else ""
                    cells.append(clean(row.get(column, "unknown")) + suffix)
                lines.append("| " + " | ".join(cells) + " |")
        refs = list(
            dict.fromkeys(node.claim_ids + [c for values in node.cell_claim_ids.values() for c in values])
        )
        for cid in refs:
            for sid in claim_map[cid].span_ids if cid in claim_map else []:
                span = span_map.get(sid)
                if span and span.source_id in source_map:
                    s = source_map[span.source_id]
                    lines.append(
                        f"> [{cid} / {sid}] {s.title}; page {span.page or 'n/a'}; chars {span.start}–{span.end}\n> {span.quote}"
                    )
        lines.append("")
    if report.paper:
        p = report.paper
        lines += [
            "## Paper understanding",
            p.problem,
            p.principles,
            p.implementation,
            "### Takeaways",
            *["- " + x for x in p.takeaways],
            "### Contributions",
            *["- " + x for x in p.contributions],
            "### Experiments",
            *["- " + json.dumps(x, ensure_ascii=False) for x in p.experiments],
            "### Limitations",
            *[f"- [{x.attribution}] {x.text}" for x in p.limitations],
            "### Research ideas",
        ]
        for idea in p.ideas:
            lines += [
                f"- Motivation: {idea.motivation}\n  Hypothesis: {idea.hypothesis}\n  Experiment: {idea.experiment}\n"
                f"  Baseline: {idea.baseline}; metric: {idea.metric}; signal: {idea.expected_signal}\n"
                f"  Risk: {idea.failure_risk}; novelty: unverified"
            ]
    if report.unresolved:
        lines += ["## Unresolved", *["- " + x for x in report.unresolved]]
    lines += ["## Source versions"]
    for source in sources:
        lines.append(
            f"- {source.id}: {source.title}; {source.url or source.filename or 'uploaded'}; "
            f"retrieved {source.retrieved_at.isoformat()}; raw SHA-256 {source.raw_hash}; "
            f"parsed SHA-256 {source.parsed_hash}"
        )
    return "\n\n".join(lines)
