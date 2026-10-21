"""ArchiveDownloader against httpx.MockTransport: verification, cache, retries, 404."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arci_helpers import FakeArchive, require

httpx = require("httpx")

from arc_helpers import kline_lines  # noqa: E402

from tsclaude.contracts.market import Market  # noqa: E402
from tsclaude.marketdata.archive.checksum import ChecksumError  # noqa: E402
from tsclaude.marketdata.archive.download import (  # noqa: E402
    ArchiveDownloader,
    ArchiveNotFound,
    DownloadError,
)
from tsclaude.marketdata.archive.paths import ArchiveDataType, ArchiveFile, Period  # noqa: E402

F = ArchiveFile(Market.SPOT, ArchiveDataType.KLINES, "BTCUSDT", "1m", Period.DAILY, "2024-12-05")


def _archive(**kw: Any) -> FakeArchive:
    a = FakeArchive()
    a.add(F, kline_lines(F.start_ms, F.start_ms + 5 * 60_000), **kw)
    return a


def _handler(archive: FakeArchive, calls: list[str], fail_first: int = 0, status: int = 500):  # type: ignore[no-untyped-def]
    state = {"fails": fail_first}

    def handler(request: Any) -> Any:
        calls.append(request.url.path)
        if state["fails"] > 0:
            state["fails"] -= 1
            return httpx.Response(status, headers={"Retry-After": "0"})
        rel = request.url.path.lstrip("/")
        if rel.endswith(".CHECKSUM"):
            entry = archive.files.get(rel.removesuffix(".CHECKSUM"))
            return httpx.Response(200, text=entry[1]) if entry else httpx.Response(404)
        entry = archive.files.get(rel)
        return httpx.Response(200, content=entry[0]) if entry else httpx.Response(404)

    return handler


async def _no_sleep(_s: float) -> None:
    return None


def _dl(tmp_path: Path, handler: Any) -> ArchiveDownloader:
    return ArchiveDownloader(
        tmp_path / "cache", transport=httpx.MockTransport(handler), sleep=_no_sleep, max_retries=3
    )


def test_verified_download_then_cache_without_network(tmp_path: Path) -> None:
    archive, calls = _archive(), []

    async def main() -> None:
        dl = _dl(tmp_path, _handler(archive, calls))
        vf = await dl.fetch(F)
        assert not vf.from_cache
        assert vf.data == archive.files[F.relpath][0]
        assert calls == ["/" + F.relpath + ".CHECKSUM", "/" + F.relpath]
        again = await dl.fetch(F)
        assert again.from_cache
        assert len(calls) == 2  # no network on a verified cache hit
        await dl.aclose()

    asyncio.run(main())
    assert (tmp_path / "cache" / F.relpath).exists()


def test_checksum_mismatch_rejected_and_not_cached(tmp_path: Path) -> None:
    archive, calls = _archive(corrupt_checksum=True), []

    async def main() -> None:
        dl = _dl(tmp_path, _handler(archive, calls))
        with pytest.raises(ChecksumError, match="mismatch"):
            await dl.fetch(F)
        await dl.aclose()

    asyncio.run(main())
    assert not (tmp_path / "cache" / F.relpath).exists()


def test_corrupted_cache_is_redownloaded(tmp_path: Path) -> None:
    archive, calls = _archive(), []

    async def main() -> None:
        dl = _dl(tmp_path, _handler(archive, calls))
        await dl.fetch(F)
        (tmp_path / "cache" / F.relpath).write_bytes(b"bit rot")
        vf = await dl.fetch(F)
        assert not vf.from_cache
        assert vf.data == archive.files[F.relpath][0]
        await dl.aclose()

    asyncio.run(main())
    assert len(calls) == 4


def test_404_and_retries(tmp_path: Path) -> None:
    async def main() -> None:
        calls: list[str] = []
        dl = _dl(tmp_path, _handler(FakeArchive(), calls))
        with pytest.raises(ArchiveNotFound):
            await dl.fetch(F)
        await dl.aclose()

        calls2: list[str] = []
        dl2 = _dl(tmp_path / "b", _handler(_archive(), calls2, fail_first=2, status=503))
        vf = await dl2.fetch(F)
        assert vf.sha256
        assert dl2.retries == 2
        await dl2.aclose()

        dl3 = _dl(tmp_path / "c", _handler(_archive(), [], fail_first=10, status=500))
        with pytest.raises(DownloadError, match="attempts"):
            await dl3.fetch(F)
        await dl3.aclose()

        dl4 = _dl(tmp_path / "d", _handler(_archive(), [], fail_first=1, status=403))
        with pytest.raises(DownloadError, match="403"):
            await dl4.fetch(F)
        await dl4.aclose()

    asyncio.run(main())
