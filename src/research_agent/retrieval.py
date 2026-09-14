"""Evidence-preserving document chunking and hybrid retrieval helpers."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict

import httpx

from research_agent.contracts import ParsedDocument, RetrievalHit, TextBlock

CHUNKER_VERSION = "locator-chunks-v1"
TARGET_CHARS = 900
MAX_CHARS = 1200
OVERLAP_CHARS = 120

_ASCII_TERM = re.compile(r"(?:/[A-Za-z0-9_./:+#-]+|[A-Za-z0-9][A-Za-z0-9_./:+#-]*)")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def lexical_terms(text: str) -> list[str]:
    """Stable multilingual terms: exact technical tokens plus overlapping CJK bigrams."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    terms = _ASCII_TERM.findall(normalized)
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(terms))


def lexical_text(text: str) -> str:
    return " ".join(lexical_terms(text))


def lexical_tsquery(text: str) -> str:
    return " | ".join("'" + term.replace("'", "''") + "'" for term in lexical_terms(text)[:64])


def _split_range(text: str, left: int, right: int):
    while left < right:
        hard_stop = min(right, left + MAX_CHARS)
        stop = hard_stop
        if hard_stop < right:
            floor = min(hard_stop, left + TARGET_CHARS)
            candidates = [
                text.rfind(marker, floor, hard_stop)
                for marker in ("\n\n", "\n", "。", "！", "？", ". ", "; ")
            ]
            boundary = max(candidates)
            if boundary > left:
                stop = boundary + (0 if text.startswith("\n", boundary) else 1)
        if stop <= left:
            stop = hard_stop
        yield left, stop
        if stop >= right:
            break
        left = max(left + 1, stop - OVERLAP_CHARS)


def _split_table_range(text: str, left: int, right: int):
    """Split only at row boundaries and overlap complete rows, never partial cells."""
    while left < right:
        hard_stop = min(right, left + MAX_CHARS)
        if hard_stop < right:
            newline = text.rfind("\n", left + 1, hard_stop)
            stop = newline + 1 if newline > left else hard_stop
        else:
            stop = right
        yield left, stop
        if stop >= right:
            break
        overlap_floor = max(left, stop - OVERLAP_CHARS)
        row_start = text.rfind("\n", left, overlap_floor)
        left = row_start + 1 if row_start >= left else stop


def chunk_document(source_id: str, parsed_hash: str, document: ParsedDocument) -> list[dict]:
    chunks: list[dict] = []
    ordinal = 0
    groups: list[list[tuple[int, TextBlock]]] = []
    pending: list[tuple[int, TextBlock]] = []
    boundaries = {"heading", "code", "table", "formula", "figure", "caption", "page"}

    def flush():
        nonlocal pending
        if pending:
            groups.append(pending)
            pending = []

    for block_index, block in enumerate(document.blocks):
        if block.kind in boundaries:
            flush()
            groups.append([(block_index, block)])
            continue
        if pending:
            previous = pending[-1][1]
            same_region = (
                previous.page == block.page
                and previous.section_path == block.section_path
                and block.end - pending[0][1].start <= MAX_CHARS
            )
            if not same_region:
                flush()
        pending.append((block_index, block))
    flush()

    for group in groups:
        block_index, block = group[0]
        group_end = group[-1][1].end
        ranges = (
            _split_table_range(document.text, block.start, group_end)
            if block.kind == "table"
            else _split_range(document.text, block.start, group_end)
        )
        for start, end in ranges:
            value = document.text[start:end]
            if not value.strip():
                continue
            if len(group) == 1:
                block_id = block.id or f"block-{block_index}"
            else:
                block_id = "group:" + ":".join(
                    item.id or str(index) for index, item in group
                )
            chunk_id = hashlib.sha256(
                f"{source_id}:{parsed_hash}:{CHUNKER_VERSION}:{start}:{end}:{value}".encode()
            ).hexdigest()
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "ordinal": ordinal,
                    "start": start,
                    "end": end,
                    "page": block.page,
                    "bbox": block.bbox,
                    "kind": block.kind,
                    "section_path": block.section_path,
                    "text": value,
                    "lexical_text": lexical_text(value + " " + document.title),
                    "block_id": block_id,
                }
            )
            ordinal += 1
    return chunks


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        raise ValueError("embedding_zero_vector")
    return [value / norm for value in vector]


def fuse_results(
    lexical: list[dict],
    dense: list[dict],
    query: str,
    *,
    limit: int = 8,
    rrf_k: int = 60,
) -> list[RetrievalHit]:
    by_id: dict[str, dict] = {}
    scores: dict[str, dict[str, float]] = defaultdict(dict)
    for label, rows in (("lexical", lexical), ("dense", dense)):
        for rank, row in enumerate(rows, 1):
            chunk_id = row["chunk_id"]
            by_id[chunk_id] = row
            scores[chunk_id][label] = float(row.get("score", 0))
            scores[chunk_id][label + "_rrf"] = 1 / (rrf_k + rank)

    exact = [term for term in lexical_terms(query) if any(c.isdigit() for c in term) or len(term) >= 4]
    ranked = []
    for chunk_id, row in by_id.items():
        detail = scores[chunk_id]
        normalized_text = unicodedata.normalize("NFKC", row["text"]).lower()
        normalized_title = unicodedata.normalize("NFKC", row.get("title", "")).lower()
        exact_boost = 0.02 if exact and any(term in normalized_text for term in exact) else 0.0
        title_boost = 0.01 if exact and any(term in normalized_title for term in exact) else 0.0
        detail["exact_boost"] = exact_boost
        detail["title_boost"] = title_boost
        detail["rrf"] = (
            detail.get("lexical_rrf", 0)
            + detail.get("dense_rrf", 0)
            + exact_boost
            + title_boost
        )
        ranked.append((detail["rrf"], row, detail))
    ranked.sort(key=lambda item: (-item[0], item[1]["source_id"], item[1]["start"]))

    selected: list[RetrievalHit] = []
    per_source: dict[str, int] = defaultdict(int)
    intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for _, row, detail in ranked:
        source_id = row["source_id"]
        if per_source[source_id] >= 3:
            continue
        if any(
            max(0, min(row["end"], end) - max(row["start"], start))
            / max(1, min(row["end"] - row["start"], end - start))
            >= 0.7
            for start, end in intervals[source_id]
        ):
            continue
        intervals[source_id].append((row["start"], row["end"]))
        per_source[source_id] += 1
        selected.append(
            RetrievalHit(
                source_id=source_id,
                chunk_id=row["chunk_id"],
                title=row.get("title", ""),
                start=row["start"],
                end=row["end"],
                page=row.get("page"),
                kind=row.get("kind", "paragraph"),
                snippet=row["text"][:1200],
                score_details=detail,
            )
        )
        if len(selected) >= limit:
            break
    return selected


class EmbeddingClient:
    def __init__(self, url: str, timeout: int = 90, dimensions: int = 768):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            response = await client.post(self.url + "/embed", json={"texts": texts})
            response.raise_for_status()
            vectors = response.json()["vectors"]
        if len(vectors) != len(texts):
            raise ValueError("embedding_count_mismatch")
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError("embedding_dimension_mismatch")
        return [normalize_vector([float(value) for value in vector]) for vector in vectors]
