from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_REDIRECTS = 5
MAX_HTML_BYTES = 6 * 1024 * 1024
MAX_ASSET_BYTES = 5 * 1024 * 1024


class FetchError(RuntimeError):
    pass


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FetchError("Only public HTTP(S) website URLs are supported")
    return value


def assert_public_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FetchError("Invalid HTTP(S) URL")
    if parsed.username or parsed.password:
        raise FetchError("Credentials in website URLs are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host: {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise FetchError(f"Website host resolves to a non-public address: {parsed.hostname}")


def fetch_limited(url: str, max_bytes: int, *, accept: str = "*/*") -> tuple[str, bytes, str]:
    current = normalize_url(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KoloDesignStudio/0.1; +https://kolo.ai)",
        "Accept": accept,
    }
    with httpx.Client(timeout=12, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            assert_public_url(current)
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("Redirect response had no Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                advertised = response.headers.get("content-length")
                if advertised and int(advertised) > max_bytes:
                    raise FetchError(f"Response exceeded {max_bytes} byte limit")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"Response exceeded {max_bytes} byte limit")
                    chunks.append(chunk)
                return current, b"".join(chunks), response.headers.get("content-type", "")
    raise FetchError("Too many redirects")
