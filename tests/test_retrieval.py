import httpx
import pytest
from pydantic import ValidationError

from research_agent.contracts import ParsedDocument, RetrievalHit, TextBlock
from research_agent.indexer import embed_with_backoff
from research_agent.retrieval import (
    MAX_CHARS,
    chunk_document,
    fuse_results,
    lexical_terms,
    lexical_tsquery,
    normalize_vector,
)
from research_agent.retrieval_benchmark import RelevantPassage, overlaps


def document(parts: list[tuple[str, str]]) -> ParsedDocument:
    text = "\n\n".join(value for _, value in parts)
    blocks = []
    offset = 0
    for index, (kind, value) in enumerate(parts):
        blocks.append(
            TextBlock(
                id=f"b{index}",
                text=value,
                start=offset,
                end=offset + len(value),
                page=1,
                kind=kind,
                section_path=["Method"],
            )
        )
        offset += len(value) + 2
    return ParsedDocument(title="ERR_AUTH_42 检索方法", text=text, blocks=blocks)


def test_cjk_and_technical_lexical_terms_are_stable():
    terms = lexical_terms("混合检索 ERR_AUTH_42 v2.1 /api/search")
    assert terms[:3] == ["err_auth_42", "v2.1", "/api/search"]
    assert "混合" in terms and "合检" in terms and "检索" in terms
    query = lexical_tsquery("ERR_AUTH_42 v2.1")
    assert query == "'err_auth_42' | 'v2.1'"


def test_chunks_preserve_offsets_group_paragraphs_and_isolate_tables():
    doc = document(
        [
            ("heading", "Method"),
            ("paragraph", "a" * 430),
            ("paragraph", "b" * 430),
            ("table", "name | score\nhybrid | 0.91"),
        ]
    )
    chunks = chunk_document("source", "hash", doc)
    assert all(doc.text[item["start"] : item["end"]] == item["text"] for item in chunks)
    assert all(len(item["text"]) <= MAX_CHARS for item in chunks)
    assert [item["kind"] for item in chunks] == ["heading", "paragraph", "table"]
    assert "err_auth_42" in chunks[1]["lexical_text"]


def test_rrf_rewards_exact_identifier_and_deduplicates_overlaps():
    lexical = [
        {
            "source_id": "s1",
            "chunk_id": "generic",
            "start": 0,
            "end": 100,
            "text": "authentication problem",
            "score": 0.8,
        },
        {
            "source_id": "s1",
            "chunk_id": "exact",
            "start": 200,
            "end": 300,
            "text": "The service emits ERR_AUTH_42.",
            "score": 0.7,
        },
        {
            "source_id": "s1",
            "chunk_id": "overlap",
            "start": 210,
            "end": 305,
            "text": "ERR_AUTH_42 is emitted.",
            "score": 0.6,
        },
    ]
    hits = fuse_results(lexical, [], "ERR_AUTH_42", limit=8)
    assert hits[0].chunk_id == "exact"
    assert [hit.chunk_id for hit in hits].count("overlap") == 0
    assert all(hit.evidence is False for hit in hits)


def test_embedding_normalization_is_deterministic():
    assert normalize_vector([3.0, 4.0]) == [0.6, 0.8]


def test_retrieval_hit_can_never_be_marked_as_evidence():
    with pytest.raises(ValidationError):
        RetrievalHit(
            source_id="s",
            chunk_id="c",
            start=0,
            end=10,
            snippet="not original evidence",
            evidence=True,
        )


def test_benchmark_relevance_uses_source_and_span_overlap():
    passage = RelevantPassage(source_id="s1", start=100, end=200)
    assert overlaps({"source_id": "s1", "start": 150, "end": 250}, passage)
    assert not overlaps({"source_id": "s2", "start": 150, "end": 250}, passage)
    assert not overlaps({"source_id": "s1", "start": 200, "end": 250}, passage)


async def test_embedding_503_recovers_with_bounded_backoff(monkeypatch):
    delays = []

    async def no_wait(delay):
        delays.append(delay)

    class RecoveringClient:
        calls = 0

        async def embed(self, texts):
            self.calls += 1
            if self.calls < 3:
                response = httpx.Response(503, request=httpx.Request("POST", "http://embedding/embed"))
                raise httpx.HTTPStatusError("unavailable", request=response.request, response=response)
            return [[1.0, 0.0]]

    monkeypatch.setattr("research_agent.indexer.asyncio.sleep", no_wait)
    client = RecoveringClient()
    assert await embed_with_backoff(client, ["text"]) == [[1.0, 0.0]]
    assert client.calls == 3 and delays == [1, 2]
