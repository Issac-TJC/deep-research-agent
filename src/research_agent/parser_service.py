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


async def parse_isolated(raw: bytes, mime: str, title: str, settings: Settings) -> ParsedDocument:
    if len(raw) > settings.max_upload_bytes:
        raise ValueError("document_size_limit")
    if settings.parser_url:
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            r = await client.post(
                settings.parser_url + "/parse", content=raw, params={"mime": mime, "title": title}
            )
            r.raise_for_status()
            return ParsedDocument.model_validate(r.json())
    # Local development: bounded subprocess with a scrubbed environment. Network isolation is supplied by Compose.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "research_agent.parser_service",
        mime,
        title,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.path.dirname(sys.executable), "LANG": "en_US.UTF-8"},
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(raw), 35)
        if proc.returncode:
            raise ValueError("document_parse_failed")
        return ParsedDocument.model_validate_json(stdout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise


@app.post("/parse")
async def parse(request: Request, mime: str, title: str = "Source"):
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(413, "document size limit")
    try:
        return await parse_isolated(bytes(raw), mime, title[:500], Settings(parser_url=None, _env_file=None))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


if __name__ == "__main__":
    import resource

    from research_agent.parsing import parse_document

    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    document = parse_document(sys.stdin.buffer.read(20 * 1024 * 1024 + 1), sys.argv[1], sys.argv[2])
    print(json.dumps(document.model_dump(mode="json"), ensure_ascii=False))
