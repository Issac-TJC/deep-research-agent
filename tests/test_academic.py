from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from research_agent.academic import AcademicSearch, canonical_id, rank_candidates
from research_agent.service import local_week_period


def test_canonical_identifier_priority_and_feedback_scoring():
    item = {
        "canonical_id": "doi:10.1/demo",
        "title": "Neural rendering method",
        "authors": ["A"],
        "year": 2026,
        "published_date": date.today().isoformat(),
        "doi": "10.1/demo",
        "arxiv_id": "2601.12345",
        "abstract": "neural rendering method evaluation",
        "cited_by_count": 2,
    }
    assert canonical_id(item) == "doi:10.1/demo"
    base = rank_candidates([item], "neural rendering", ["method"])[0]
    useful = rank_candidates([item], "neural rendering", ["method"], {item["canonical_id"]: ["useful"]})[0]
    assert useful["scores"]["total"] == min(100, base["scores"]["total"] + 5)
    assert rank_candidates([item], "neural rendering", [], {item["canonical_id"]: ["never_recommend"]}) == []


def test_local_week_period_is_stable_across_new_york_dst_transition():
    before = local_week_period("America/New_York", datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc))
    after = local_week_period("America/New_York", datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))
    assert before == after == (date(2026, 3, 2), date(2026, 3, 8))


@pytest.mark.asyncio
async def test_connector_partial_failure_is_isolated_and_provenance_is_merged():
    settings = SimpleNamespace(
        research_mode="live",
        academic_connector_timeout=0.1,
        academic_connector_attempts=1,
        academic_candidates_per_connector=5,
        academic_contact_email="",
        academic_user_agent="test",
        openalex_api_key="",
        ncbi_api_key="",
    )

    class Search(AcademicSearch):
        async def _openalex(self, client, query, start, end):
            return [
                {
                    "title": "Shared paper",
                    "authors": ["Ada"],
                    "year": 2026,
                    "published_date": "2026-09-01",
                    "doi": "10.1234/shared",
                    "url": "https://doi.org/10.1234/shared",
                    "abstract": "evidence",
                    "provider_ids": {"openalex": "W1"},
                }
            ], 0

        async def _crossref(self, client, query, start, end):
            return [
                {
                    "title": "Shared paper",
                    "authors": ["Ada"],
                    "year": 2026,
                    "published_date": "2026-09-01",
                    "doi": "10.1234/shared",
                    "url": "https://doi.org/10.1234/shared",
                    "abstract": "",
                    "provider_ids": {"crossref": "10.1234/shared"},
                }
            ], None

        async def _arxiv(self, client, query, start, end):
            raise httpx.ReadTimeout("fixture timeout")

    async with httpx.AsyncClient() as client:
        candidates, attempts = await Search(settings, client).collect(
            "shared topic",
            [],
            date(2026, 9, 1),
            date(2026, 9, 15),
            ["openalex", "crossref", "arxiv"],
        )
    assert len(candidates) == 1
    assert candidates[0]["connectors"] == ["openalex", "crossref"]
    states = {item["connector"]: item["status"] for item in attempts}
    assert states == {"openalex": "succeeded", "crossref": "succeeded", "arxiv": "timed_out"}
