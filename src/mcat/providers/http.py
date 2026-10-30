"""Shared HTTP client: host allow-list, credential scoping, redirect validation, retries and caching.

Credentials are attached only to requests whose host is the provider's verified API host, and are
stripped before following any redirect. Redirects are followed manually and only to allow-listed hosts.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from mcat import user_agent
from mcat.security.redact import redact

# host -> credential scope that may be sent to it (None = never send credentials)
ALLOWED_HOSTS: dict[str, str | None] = {
    "api.modrinth.com": "modrinth",
    "cdn.modrinth.com": None,
    "api.curseforge.com": "curseforge",
    "edge.forgecdn.net": None,
    "mediafilez.forgecdn.net": None,
    "meta.fabricmc.net": None,
    "maven.fabricmc.net": None,
    "maven.neoforged.net": None,
    "maven.minecraftforge.net": None,
    "files.minecraftforge.net": None,
    "piston-meta.mojang.com": None,
    "launchermeta.mojang.com": None,
}
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 32 * 1024**2


class NetworkError(Exception):
    pass


class RateLimitedError(NetworkError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class AuthError(NetworkError):
    pass


class OfflineError(NetworkError):
    pass


def cache_dir() -> Path:
    base = os.environ.get("MCAT_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "mcat"
    )
    return Path(base) / "http"


def offline() -> bool:
    return os.environ.get("MCAT_OFFLINE") == "1"


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise NetworkError(f"refusing non-HTTPS URL: {url}")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS or parts.username or parts.password or (parts.port not in (None, 443)):
        raise NetworkError(f"host not on the MCAT allow-list: {host or url}")
    return host


@dataclass
class HttpClient:
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 1.5
    transport: httpx.BaseTransport | None = None  # injectable for tests

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            headers={"User-Agent": user_agent()},
            transport=self.transport,
        )

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        credential: tuple[str, str, str] | None = None,  # (scope, header name, value)
        ttl: float = 3600,
        method: str = "GET",
        body: Any = None,
    ) -> Any:
        full = str(httpx.URL(url, params=params or {}))
        key = hashlib.sha256(f"{method} {full} {json.dumps(body, sort_keys=True)}".encode()).hexdigest()
        cpath = cache_dir() / f"{key}.json"
        if ttl > 0 and cpath.is_file():
            try:
                cached = json.loads(cpath.read_text())
                if offline() or time.time() - cached["t"] < ttl:
                    return cached["body"]
            except (ValueError, KeyError, OSError):
                pass  # corrupt cache entries are ignored and overwritten
        if offline():
            raise OfflineError(f"MCAT_OFFLINE=1 and no cached response for {full}")
        resp = self._request(method, full, credential=credential, body=body)
        data = resp.json()
        if ttl > 0:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = cpath.with_suffix(".tmp")
            tmp.write_text(json.dumps({"t": time.time(), "url": full, "body": data}))
            tmp.replace(cpath)
        return data

    def download(self, url: str, dest: Path, *, max_bytes: int = 512 * 1024**2) -> Path:
        if offline():
            raise OfflineError(f"MCAT_OFFLINE=1; cannot download {url}")
        resp = self._request("GET", url, credential=None, stream_limit=max_bytes)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "xb") as fh:
            fh.write(resp.content)
        return dest

    def _request(
        self,
        method: str,
        url: str,
        *,
        credential: tuple[str, str, str] | None,
        body: Any = None,
        stream_limit: int = MAX_RESPONSE_BYTES,
    ) -> httpx.Response:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = check_url(current)
            headers: dict[str, str] = {}
            if credential is not None:
                scope, header, value = credential
                if ALLOWED_HOSTS.get(host) == scope:
                    headers[header] = value
            resp = self._send_with_retry(method, current, headers, body)
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                current = str(httpx.URL(current).join(location))
                credential = None  # never forward credentials across a redirect
                method, body = "GET", None
                continue
            if resp.status_code in (401, 403):
                raise AuthError(f"{host} rejected the request ({resp.status_code}); check credentials")
            if resp.status_code == 404:
                raise NetworkError(f"not found: {redact(current)}")
            if resp.status_code >= 400:
                raise NetworkError(f"{host} returned HTTP {resp.status_code}: {redact(resp.text[:300])}")
            if len(resp.content) > stream_limit:
                raise NetworkError(f"response from {host} exceeds size limit")
            return resp
        raise NetworkError(f"too many redirects for {redact(url)}")

    def _send_with_retry(self, method: str, url: str, headers: dict[str, str], body: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.request(method, url, headers=headers, json=body)
            except httpx.TransportError as e:
                last = e
                time.sleep(self.backoff * (2**attempt))
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset") or 0)
                if attempt >= self.retries or retry_after > 60:
                    raise RateLimitedError(retry_after or 60)
                time.sleep(max(retry_after, self.backoff * (2**attempt)))
                continue
            if resp.status_code >= 500 and attempt < self.retries:
                time.sleep(self.backoff * (2**attempt))
                continue
            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 5:
                time.sleep(float(resp.headers.get("x-ratelimit-reset", "1") or 1))
            return resp
        raise NetworkError(f"network error contacting {urlsplit(url).hostname}: {type(last).__name__}")
