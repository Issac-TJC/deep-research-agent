"""Run on an internal-only network without provider, database or S3 credentials."""

import asyncio
import json
import os
import sys

import httpx
from fastapi import FastAPI, HTTPException, Request

from research_agent.contracts import ParsedDocument
from research_agent.settings import Settings

app = FastAPI(title="Bounded parser", docs_url=None, redoc_url=None)


async def parse_isolated(
    raw: bytes, mime: str, title: str, settings: Settings, parser_mode: str = "auto"
) -> ParsedDocument:
    if len(raw) > settings.max_upload_bytes:
        raise ValueError("document_size_limit")

    async def parse_once(mode: str) -> ParsedDocument:
        if settings.parser_url:
            async with httpx.AsyncClient(timeout=settings.request_timeout, trust_env=False) as client:
                response = await client.post(
                    settings.parser_url + "/parse",
                    content=raw,
                    params={"mime": mime, "title": title, "parser_mode": mode},
                )
                response.raise_for_status()
                return ParsedDocument.model_validate(response.json())
        # Local development: bounded subprocess with a scrubbed environment.
        safe_environment = {"PATH": os.path.dirname(sys.executable), "LANG": "en_US.UTF-8"}
        for name in (
            "DOCLING_ARTIFACTS_PATH",
            "HF_HOME",
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "OMP_NUM_THREADS",
        ):
            if value := os.environ.get(name):
                safe_environment[name] = value
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "research_agent.parser_service",
            mime,
            title,
            mode,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_environment,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(raw), settings.request_timeout)
            if proc.returncode:
                raise ValueError("document_parse_failed")
            return ParsedDocument.model_validate_json(stdout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise

    try:
        return await parse_once(parser_mode)
    except (ValueError, httpx.HTTPError, TimeoutError):
        if mime != "application/pdf" or parser_mode != "auto":
            raise
        fallback = await parse_once("native")
        fallback.warnings.append("enhanced parser fallback")
        return fallback


@app.post("/parse")
async def parse(request: Request, mime: str, title: str = "Source", parser_mode: str = "auto"):
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(413, "document size limit")
    try:
        return await parse_isolated(
            bytes(raw), mime, title[:500], Settings(parser_url=None, _env_file=None), parser_mode
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


if __name__ == "__main__":
    import resource

    from research_agent.parsing import parse_document, parse_document_enhanced

    parser_mode = sys.argv[3] if len(sys.argv) > 3 else "auto"
    enhanced = sys.argv[1] == "application/pdf" and parser_mode in {"auto", "enhanced"}
    cpu_limit = 300 if enhanced else 25
    memory_limit = (6 * 1024 if enhanced else 768) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    raw = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
    mime, title = sys.argv[1], sys.argv[2]
    if mime == "application/pdf" and parser_mode in {"auto", "enhanced"}:
        try:
            document = parse_document_enhanced(raw, title)
        except Exception:
            if parser_mode == "enhanced":
                raise
            document = parse_document(raw, mime, title)
            document.warnings.append("enhanced parser fallback")
    else:
        document = parse_document(raw, mime, title)
    print(json.dumps(document.model_dump(mode="json"), ensure_ascii=False))
