"""Public-only HTTP fetch with DNS pinning, redirect revalidation and bounded decompression."""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class UnsafeURL(ValueError):
    pass


async def resolve_public(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeURL("only public http(s) URLs without credentials are allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise UnsafeURL("nonstandard source port")
    host = parsed.hostname.encode("idna").decode("ascii")
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise UnsafeURL("local hostname")
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5
    )
    addresses = sorted({r[4][0] for r in records})
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses
    ):
        raise UnsafeURL("source DNS includes non-public address")
    ip = addresses[0]
    authority = f"[{ip}]" if ":" in ip else ip
    pinned = urlunsplit((parsed.scheme, f"{authority}:{port}", parsed.path or "/", parsed.query, ""))
    return pinned, host


async def fetch_public(url: str, max_bytes: int) -> tuple[bytes, str, str]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            pinned, host = await resolve_public(url)
            async with client.stream(
                "GET",
                pinned,
                headers={"Host": host, "User-Agent": "ResearchAgent/0.1"},
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect_without_location")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";")[0].lower()
                if mime not in {"text/html", "text/plain", "text/markdown", "application/pdf"}:
                    raise ValueError("unsupported_source_mime")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("source_exceeds_size_limit")
                    chunks.append(chunk)
                return b"".join(chunks), mime, url
        raise UnsafeURL("redirect_limit")
