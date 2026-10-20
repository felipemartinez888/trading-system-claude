"""Polite, resumable downloader for `data.binance.vision` with a verified raw-zip cache.

- Every zip is checked against its `.CHECKSUM` (SHA-256) before anything uses it. A mismatch is a
  hard `ChecksumError`: the bad bytes are discarded and nothing is cached or imported.
- Cache: `<cache>/<archive relpath>` and `<...>.CHECKSUM`. A cached pair is re-verified on every
  use; a cached zip that no longer matches is deleted and downloaded again once.
- Writes are atomic (temp file + rename), so an interrupted run resumes cleanly.
- Bounded concurrency (semaphore); retries with exponential back-off + full jitter on transport
  errors, HTTP 5xx and 429 (honouring `Retry-After`). HTTP 404 means "this file does not exist"
  (`ArchiveNotFound`), any other status is a `DownloadError`.

httpx is imported lazily so the rest of the archive package stays importable without it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tsclaude.marketdata.archive.checksum import ChecksumError, parse_checksum, sha256_bytes
from tsclaude.marketdata.archive.paths import ARCHIVE_BASE_URL, ArchiveFile
from tsclaude.marketdata.binance.limits import Backoff, Sleep

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)


class ArchiveNotFound(LookupError):
    """The archive has no such file (HTTP 404)."""


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    file: ArchiveFile
    data: bytes
    sha256: str
    url: str
    checksum_url: str
    cache_path: str  # posix, relative to the cache dir
    from_cache: bool


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class ArchiveDownloader:
    def __init__(
        self,
        cache_dir: Path | str,
        *,
        base_url: str = ARCHIVE_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 4,
        max_retries: int = 4,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 30.0,
        timeout_s: float = 60.0,
        sleep: Sleep = asyncio.sleep,
        rng: Any = None,
    ) -> None:
        import httpx

        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._backoff = (backoff_base_s, backoff_cap_s)
        self._sleep = sleep
        self._rng = rng
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "tsclaude/0 (historical import; paper-only research)"},
        )
        self.requests = 0
        self.retries = 0
        self.cache_hits = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ArchiveDownloader:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ public
    async def fetch(self, f: ArchiveFile) -> VerifiedFile:
        """Return the verified bytes of an archive file (cache first). Raises ArchiveNotFound."""
        zip_path = self.cache_dir / f.relpath
        sum_path = self.cache_dir / (f.relpath + ".CHECKSUM")
        url, sum_url = f.url(self.base_url), f.checksum_url(self.base_url)
        if zip_path.exists() and sum_path.exists():
            try:
                expected = parse_checksum(sum_path.read_text(encoding="utf-8"), f.name)
                data = zip_path.read_bytes()
                if sha256_bytes(data) == expected:
                    self.cache_hits += 1
                    return VerifiedFile(f, data, expected, url, sum_url, f.relpath, True)
                log.warning(
                    "cached archive file failed verification; re-downloading",
                    extra={"file": f.relpath},
                )
            except ChecksumError:
                log.warning(
                    "cached checksum file invalid; re-downloading", extra={"file": f.relpath}
                )
            zip_path.unlink(missing_ok=True)
            sum_path.unlink(missing_ok=True)

        checksum_text = (await self._get(sum_url)).decode("utf-8", errors="replace")
        expected = parse_checksum(checksum_text, f.name)
        data = await self._get(url)
        actual = sha256_bytes(data)
        if actual != expected:
            raise ChecksumError(
                f"checksum mismatch for {f.relpath}: expected {expected}, got {actual}; "
                "file discarded"
            )
        _write_atomic(sum_path, checksum_text.encode("utf-8"))
        _write_atomic(zip_path, data)
        return VerifiedFile(f, data, expected, url, sum_url, f.relpath, False)

    # ------------------------------------------------------------------ http
    async def _get(self, url: str) -> bytes:
        import httpx

        backoff = Backoff(self._backoff[0], self._backoff[1], self._rng)
        attempt = 0
        while True:
            attempt += 1
            async with self._sem:
                self.requests += 1
                try:
                    resp = await self._client.get(url)
                except httpx.TransportError as exc:
                    error: str | None = f"transport error: {exc!r}"
                    retry_after: float | None = None
                else:
                    if resp.status_code == 200:
                        return resp.content
                    if resp.status_code == 404:
                        raise ArchiveNotFound(url)
                    if resp.status_code != 429 and resp.status_code < 500:
                        raise DownloadError(f"HTTP {resp.status_code} for {url}")
                    error = f"HTTP {resp.status_code}"
                    retry_after = _retry_after_s(resp.headers.get("Retry-After"))
            if attempt > self._max_retries:
                raise DownloadError(f"{error} for {url} after {attempt} attempts")
            self.retries += 1
            delay = retry_after if retry_after is not None else backoff.next_delay()
            log.warning(
                "archive request failed; retrying",
                extra={"url": url, "error": error, "delay_s": delay},
            )
            await self._sleep(delay)


def _retry_after_s(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
