"""Bounded scholarly metadata connectors and deterministic weekly-paper selection."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from typing import Any
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return re.sub(r"\s+", " ", unescape(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))).strip()


def _doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.lower().strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    return value if value.startswith("10.") else None


def _arxiv(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?:arxiv:|/abs/|/pdf/)?([a-z-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", value, re.I)
    return match.group(1).lower() if match else None


def canonical_id(item: dict[str, Any]) -> str:
    if value := _doi(item.get("doi")):
        return "doi:" + value
    if value := _arxiv(item.get("arxiv_id") or item.get("url")):
        return "arxiv:" + value
    if value := item.get("pmid"):
        return "pmid:" + str(value)
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", item.get("title", "").lower()).strip()
    first_author = (item.get("authors") or [""])[0].lower()
    stable = f"{title}|{first_author}|{item.get('year') or ''}"
    return "work:" + hashlib.sha256(stable.encode()).hexdigest()[:24]


def _abstract_from_index(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words = [(position, word) for word, positions in index.items() for position in positions]
    return " ".join(word for _, word in sorted(words))


class AcademicSearch:
    _arxiv_lock = asyncio.Lock()
    _last_arxiv_request = 0.0

    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client

    async def collect(
        self,
        topic: str,
        terms: list[str],
        start: date,
        end: date,
        connectors: list[str] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        requested = connectors or ["openalex", "crossref", "arxiv", "pubmed"]
        query = " ".join(dict.fromkeys([topic, *terms])).strip()
        if self.settings.research_mode == "fixture":
            candidates = [
                {
                    "title": f"{topic} study {index}",
                    "authors": [f"Researcher {index}"],
                    "published_date": end.isoformat(),
                    "year": end.year,
                    "doi": f"10.5555/{hashlib.sha256((topic + str(index)).encode()).hexdigest()[:12]}",
                    "url": f"https://example.org/papers/{index}",
                    "abstract": f"A fixture abstract about {query}, method {index}, evaluation and limitations.",
                    "venue": "Fixture Proceedings",
                    "license": "CC-BY-4.0",
                    "cited_by_count": index,
                    "provider_ids": {"fixture": str(index)},
                    "connectors": [requested[index % len(requested)]],
                }
                for index in range(1, 9)
            ]
            for item in candidates:
                item["canonical_id"] = canonical_id(item)
            attempts = [
                {
                    "connector": name,
                    "status": "succeeded",
                    "attempt": 1,
                    "result_count": len(candidates) // len(requested),
                    "latency_ms": 1,
                    "provider_cost_usd": 0,
                    "query": {"text": query, "start": start.isoformat(), "end": end.isoformat()},
                }
                for name in requested
            ]
            return candidates, attempts
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.academic_connector_timeout),
            follow_redirects=True,
            headers={"User-Agent": self.settings.academic_user_agent},
        )
        try:
            results = await asyncio.gather(
                *(self._bounded(client, name, query, start, end) for name in requested)
            )
        finally:
            if own_client:
                await client.aclose()
        attempts = [attempt for _, connector_attempts in results for attempt in connector_attempts]
        merged: dict[str, dict] = {}
        for items, connector_attempts in results:
            attempt = connector_attempts[-1]
            for item in items:
                item["canonical_id"] = canonical_id(item)
                existing = merged.get(item["canonical_id"])
                if not existing:
                    item["connectors"] = [attempt["connector"]]
                    merged[item["canonical_id"]] = item
                    continue
                existing["connectors"] = list(
                    dict.fromkeys([*existing["connectors"], attempt["connector"]])
                )
                existing.setdefault("provider_ids", {}).update(item.get("provider_ids", {}))
                for field in ("abstract", "doi", "arxiv_id", "pmid", "url", "venue", "license"):
                    if not existing.get(field) and item.get(field):
                        existing[field] = item[field]
        return list(merged.values()), attempts

    async def _bounded(self, client, connector: str, query: str, start: date, end: date):
        started = time.monotonic()
        error = None
        attempt_log = []
        for attempt in range(1, self.settings.academic_connector_attempts + 1):
            try:
                method = getattr(self, f"_{connector}")
                items, provider_cost = await method(client, query, start, end)
                attempt_log.append(
                    {
                        "connector": connector,
                        "status": "succeeded",
                        "attempt": attempt,
                        "result_count": len(items),
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "provider_cost_usd": provider_cost,
                        "query": {"text": query, "start": start.isoformat(), "end": end.isoformat()},
                    }
                )
                return items, attempt_log
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                error = type(exc).__name__
                status = "timed_out"
            except Exception as exc:  # connector isolation is intentional
                error = f"{type(exc).__name__}: {str(exc)[:180]}"
                status = "failed"
            attempt_log.append(
                {
                    "connector": connector,
                    "status": status,
                    "attempt": attempt,
                    "result_count": 0,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "provider_cost_usd": None,
                    "error": error,
                    "query": {"text": query, "start": start.isoformat(), "end": end.isoformat()},
                }
            )
            if attempt < self.settings.academic_connector_attempts:
                await asyncio.sleep(0.25 * 2 ** (attempt - 1))
        return [], attempt_log

    async def _openalex(self, client, query, start, end):
        params = {
            "search": query,
            "filter": f"from_publication_date:{start},to_publication_date:{end}",
            "per_page": self.settings.academic_candidates_per_connector,
            "select": "id,title,publication_date,doi,authorships,abstract_inverted_index,primary_location,cited_by_count",
        }
        if self.settings.openalex_api_key:
            params["api_key"] = self.settings.openalex_api_key
        response = await client.get("https://api.openalex.org/works", params=params)
        response.raise_for_status()
        body = response.json()
        items = []
        for row in body.get("results", []):
            location = row.get("primary_location") or {}
            source = location.get("source") or {}
            items.append(
                {
                    "title": _text(row.get("title")),
                    "authors": [a.get("author", {}).get("display_name", "") for a in row.get("authorships", [])],
                    "published_date": row.get("publication_date"),
                    "year": int(str(row.get("publication_date") or "0")[:4] or 0),
                    "doi": _doi(row.get("doi")),
                    "url": row.get("doi") or row.get("id"),
                    "abstract": _abstract_from_index(row.get("abstract_inverted_index")),
                    "venue": source.get("display_name"),
                    "license": location.get("license"),
                    "cited_by_count": row.get("cited_by_count", 0),
                    "provider_ids": {"openalex": row.get("id")},
                }
            )
        return items, body.get("meta", {}).get("cost_usd")

    async def _crossref(self, client, query, start, end):
        params = {
            "query": query,
            "filter": f"from-pub-date:{start},until-pub-date:{end}",
            "rows": self.settings.academic_candidates_per_connector,
            "select": "DOI,title,author,published,abstract,URL,container-title,license,is-referenced-by-count",
        }
        if self.settings.academic_contact_email:
            params["mailto"] = self.settings.academic_contact_email
        response = await client.get("https://api.crossref.org/works", params=params)
        response.raise_for_status()
        rows = response.json().get("message", {}).get("items", [])
        items = []
        for row in rows:
            parts = (row.get("published") or {}).get("date-parts", [[0]])[0]
            published = "-".join(str(x).zfill(2) for x in parts) if parts and parts[0] else None
            items.append(
                {
                    "title": _text(row.get("title")),
                    "authors": [
                        _text(f"{a.get('given', '')} {a.get('family', '')}") for a in row.get("author", [])
                    ],
                    "published_date": published,
                    "year": parts[0] if parts else None,
                    "doi": _doi(row.get("DOI")),
                    "url": row.get("URL"),
                    "abstract": _text(row.get("abstract")),
                    "venue": _text(row.get("container-title")),
                    "license": (row.get("license") or [{}])[0].get("URL"),
                    "cited_by_count": row.get("is-referenced-by-count", 0),
                    "provider_ids": {"crossref": row.get("DOI")},
                }
            )
        return items, None

    async def _arxiv(self, client, query, start, end):
        async with self._arxiv_lock:
            delay = 3.0 - (time.monotonic() - self._last_arxiv_request)
            if delay > 0:
                await asyncio.sleep(delay)
            params = {
                "search_query": f'all:"{query}" AND submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]',
                "start": 0,
                "max_results": self.settings.academic_candidates_per_connector,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            response = await client.get("https://export.arxiv.org/api/query", params=params)
            self.__class__._last_arxiv_request = time.monotonic()
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
        items = []
        for entry in root.findall("a:entry", ns):
            identifier = _text(entry.findtext("a:id", namespaces=ns))
            doi = _doi(entry.findtext("x:doi", namespaces=ns))
            published = _text(entry.findtext("a:published", namespaces=ns))
            items.append(
                {
                    "title": _text(entry.findtext("a:title", namespaces=ns)),
                    "authors": [_text(a.findtext("a:name", namespaces=ns)) for a in entry.findall("a:author", ns)],
                    "published_date": published[:10] or None,
                    "year": int(published[:4]) if published[:4].isdigit() else None,
                    "doi": doi,
                    "arxiv_id": _arxiv(identifier),
                    "url": identifier,
                    "abstract": _text(entry.findtext("a:summary", namespaces=ns)),
                    "venue": "arXiv",
                    "license": None,
                    "cited_by_count": 0,
                    "provider_ids": {"arxiv": _arxiv(identifier)},
                }
            )
        return items, None

    async def _pubmed(self, client, query, start, end):
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        common = {"tool": "deep_research_agent"}
        if self.settings.academic_contact_email:
            common["email"] = self.settings.academic_contact_email
        if self.settings.ncbi_api_key:
            common["api_key"] = self.settings.ncbi_api_key
        search = await client.get(
            f"{base}/esearch.fcgi",
            params={
                **common,
                "db": "pubmed",
                "term": f"({query}) AND ({start:%Y/%m/%d}[PDAT] : {end:%Y/%m/%d}[PDAT])",
                "retmode": "json",
                "retmax": self.settings.academic_candidates_per_connector,
                "sort": "pub_date",
            },
        )
        search.raise_for_status()
        ids = search.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return [], None
        await asyncio.sleep(0.34 if not self.settings.ncbi_api_key else 0.1)
        fetched = await client.get(
            f"{base}/efetch.fcgi",
            params={**common, "db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        )
        fetched.raise_for_status()
        root = ElementTree.fromstring(fetched.content)
        items = []
        for article in root.findall(".//PubmedArticle"):
            citation = article.find(".//MedlineCitation")
            detail = article.find(".//Article")
            if citation is None or detail is None:
                continue
            pmid = _text(citation.findtext("PMID"))
            year = _text(detail.findtext("Journal/JournalIssue/PubDate/Year"))
            doi = None
            for identifier in article.findall(".//ArticleId"):
                if identifier.attrib.get("IdType") == "doi":
                    doi = _doi(identifier.text)
            items.append(
                {
                    "title": _text(detail.findtext("ArticleTitle")),
                    "authors": [
                        _text(f"{a.findtext('ForeName') or ''} {a.findtext('LastName') or ''}")
                        for a in detail.findall("AuthorList/Author")
                    ],
                    "published_date": year or None,
                    "year": int(year) if year.isdigit() else None,
                    "doi": doi,
                    "pmid": pmid,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "abstract": _text(" ".join(x.text or "" for x in detail.findall("Abstract/AbstractText"))),
                    "venue": _text(detail.findtext("Journal/Title")),
                    "license": None,
                    "cited_by_count": 0,
                    "provider_ids": {"pubmed": pmid},
                }
            )
        return items, None


def rank_candidates(
    candidates: list[dict], topic: str, terms: list[str], feedback: dict[str, list[str]] | None = None
) -> list[dict]:
    feedback = feedback or {}
    query_terms = set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", " ".join([topic, *terms]).lower()))
    today = datetime.now(timezone.utc).date()
    ranked = []
    for item in candidates:
        if "never_recommend" in feedback.get(item["canonical_id"], []):
            continue
        if "recently_recommended" in feedback.get(item["canonical_id"], []):
            continue
        haystack = f"{item.get('title', '')} {item.get('abstract', '')}".lower()
        matched = sum(term in haystack for term in query_terms)
        relevance = min(100, round(100 * matched / max(1, len(query_terms))))
        novelty = 75 if item.get("abstract") else 35
        credibility = min(
            100,
            25 + (25 if item.get("doi") else 0) + (25 if item.get("abstract") else 0) + min(25, len(item.get("authors") or []) * 5),
        )
        impact = min(100, 45 + int(item.get("cited_by_count") or 0) * 2)
        try:
            age = max(0, (today - date.fromisoformat(str(item.get("published_date"))[:10])).days)
            timeliness = max(0, 100 - age * 4)
        except ValueError:
            timeliness = 40
        components = {
            "direction_relevance": relevance,
            "novelty": novelty,
            "method_credibility": credibility,
            "potential_impact": impact,
            "timeliness": timeliness,
        }
        base_total = round(
            relevance * 0.35 + novelty * 0.20 + credibility * 0.20 + impact * 0.15 + timeliness * 0.10,
            2,
        )
        past = feedback.get(item["canonical_id"], [])
        adjustment = 5 if {"useful", "saved"} & set(past) else 0
        adjustment -= 10 if "irrelevant" in past else 0
        total = max(0, min(100, base_total + adjustment))
        item = {
            **item,
            "scores": {**components, "feedback_adjustment": adjustment, "total": total},
            "evidence_scope": "abstract_only" if item.get("abstract") else "metadata_only",
            "selection_rationale": (
                f"方向相关性 {relevance}/100；证据范围为"
                f"{'摘要' if item.get('abstract') else '元数据'}；综合得分 {total:.1f}/100。"
            ),
        }
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["scores"]["total"], item["canonical_id"]))
    diverse = []
    for item in ranked:
        title = item.get("title", "").lower()
        if any(SequenceMatcher(None, title, old.get("title", "").lower()).ratio() >= 0.88 for old in diverse):
            continue
        diverse.append(item)
    return diverse
