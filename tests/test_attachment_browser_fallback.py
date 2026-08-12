from __future__ import annotations

import httpx

from idx_digest.config import Settings
from idx_digest.downloader import AttachmentDownloader, ImpersonateDownloadBlocked


class FakeBrowser:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        self.urls.append(url)
        return b"browser-pdf", "application/pdf"


def forbidden(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError("forbidden", request=request, response=response)


def test_auto_falls_back_to_shared_browser(monkeypatch, tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        idx_transport="auto",
        _env_file=None,
    )
    browser = FakeBrowser()
    downloader = AttachmentDownloader(
        settings,
        browser_transport_factory=lambda: browser,  # type: ignore[arg-type]
    )
    url = "https://www.idx.co.id/StaticData/example.pdf"

    def fail_http(_: str):
        raise forbidden(url)

    # In auto mode a 403 first tries TLS impersonation; only when every profile
    # is blocked does it escalate to the shared browser. Stub impersonation as
    # blocked so this test exercises the browser fallback deterministically.
    def blocked_impersonate(_url: str, _task_id=None):
        raise ImpersonateDownloadBlocked("all profiles blocked")

    monkeypatch.setattr(downloader, "_download_bytes_http", fail_http)
    monkeypatch.setattr(downloader, "_download_bytes_impersonate", blocked_impersonate)
    try:
        content, content_type = downloader._download_bytes(url)
    finally:
        downloader.close()

    assert content == b"browser-pdf"
    assert content_type == "application/pdf"
    assert browser.urls == [url]


def test_auto_prefers_impersonation_over_browser(monkeypatch, tmp_path):
    """On a 403 in auto mode, a working impersonation profile serves the file
    with no browser involvement, and the choice latches for later files."""
    settings = Settings(data_dir=tmp_path, idx_transport="auto", _env_file=None)
    browser = FakeBrowser()
    downloader = AttachmentDownloader(settings, browser_transport_factory=lambda: browser)  # type: ignore[arg-type]
    calls = {"http": 0, "impersonate": 0}

    def fail_http(url: str):
        calls["http"] += 1
        raise forbidden(url)

    def ok_impersonate(_url: str, _task_id=None):
        calls["impersonate"] += 1
        return b"impersonated-pdf", "application/pdf"

    monkeypatch.setattr(downloader, "_download_bytes_http", fail_http)
    monkeypatch.setattr(downloader, "_download_bytes_impersonate", ok_impersonate)
    try:
        first = downloader._download_bytes("https://www.idx.co.id/a.pdf")
        second = downloader._download_bytes("https://www.idx.co.id/b.pdf")
    finally:
        downloader.close()

    assert first[0] == b"impersonated-pdf"
    assert second[0] == b"impersonated-pdf"
    # httpx tried once, then impersonation latched; browser never touched.
    assert calls["http"] == 1
    assert calls["impersonate"] == 2
    assert browser.urls == []


def test_http_mode_does_not_fallback(monkeypatch, tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        idx_transport="http",
        _env_file=None,
    )
    browser = FakeBrowser()
    downloader = AttachmentDownloader(
        settings,
        browser_transport_factory=lambda: browser,  # type: ignore[arg-type]
    )
    url = "https://www.idx.co.id/StaticData/example.pdf"

    def fail_http(_: str):
        raise forbidden(url)

    monkeypatch.setattr(downloader, "_download_bytes_http", fail_http)
    try:
        try:
            downloader._download_bytes(url)
        except httpx.HTTPStatusError:
            pass
        else:
            raise AssertionError("HTTP mode should preserve the 403 error")
    finally:
        downloader.close()

    assert browser.urls == []


def test_auto_learns_browser_after_first_403(monkeypatch, tmp_path):
    settings = Settings(data_dir=tmp_path, idx_transport="auto", _env_file=None)
    browser = FakeBrowser()
    downloader = AttachmentDownloader(settings, browser_transport_factory=lambda: browser)  # type: ignore[arg-type]
    calls = {"http": 0}

    def fail_http(url: str):
        calls["http"] += 1
        raise forbidden(url)

    def blocked_impersonate(_url: str, _task_id=None):
        raise ImpersonateDownloadBlocked("all profiles blocked")

    monkeypatch.setattr(downloader, "_download_bytes_http", fail_http)
    monkeypatch.setattr(downloader, "_download_bytes_impersonate", blocked_impersonate)
    try:
        first = downloader._download_bytes("https://www.idx.co.id/a.pdf")
        second = downloader._download_bytes("https://www.idx.co.id/b.pdf")
    finally:
        downloader.close()

    assert first[0] == b"browser-pdf"
    assert second[0] == b"browser-pdf"
    assert calls["http"] == 1
    assert browser.urls == ["https://www.idx.co.id/a.pdf", "https://www.idx.co.id/b.pdf"]
