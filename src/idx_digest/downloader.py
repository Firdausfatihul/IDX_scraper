from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .browser_transport import IDXBrowserTransport
from .config import Settings
from .observability import RunObserver


class RetryableDownloadError(RuntimeError):
    pass


def safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip(" .")
    return name[:180] or "attachment"


class AttachmentDownloader:
    def __init__(
        self,
        settings: Settings,
        *,
        browser_transport_factory: Callable[[], IDXBrowserTransport] | None = None,
        observer: RunObserver | None = None,
    ):
        headers = {
            "User-Agent": settings.idx_user_agent,
            "Referer": f"{settings.idx_base_url}/id/perusahaan-tercatat/keterbukaan-informasi",
        }
        if settings.idx_cookie:
            headers["Cookie"] = settings.idx_cookie
        self.settings = settings
        self.browser_transport_factory = browser_transport_factory
        self.observer = observer
        self._auto_prefer_browser = False
        self.client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(90, connect=20),
            follow_redirects=True,
            http2=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AttachmentDownloader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RetryableDownloadError)),
        wait=wait_exponential_jitter(initial=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _download_bytes_http(self, url: str, task_id: int | None = None) -> tuple[bytes, str]:
        if self.observer:
            self.observer.event("download", "HTTP download request", url=url)
            self.observer.update_task(task_id, completed=0)
        started = time.perf_counter()
        chunks: list[bytes] = []
        received = 0
        with self.client.stream("GET", url) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableDownloadError(f"HTTP {response.status_code} for {url}")
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            total = int(content_length) if content_length and content_length.isdigit() else None
            if self.observer and total is not None:
                self.observer.update_task(task_id, total=total)
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                received += len(chunk)
                if self.observer:
                    self.observer.update_task(task_id, completed=received)
            content_type = response.headers.get("content-type", "application/octet-stream")

        if self.observer:
            self.observer.event(
                "download",
                "HTTP download complete",
                url=url,
                bytes=received,
                elapsed_seconds=f"{time.perf_counter() - started:.3f}",
                content_type=content_type,
            )
        return b"".join(chunks), content_type

    def _download_bytes(self, url: str, task_id: int | None = None) -> tuple[bytes, str]:
        transport = self.settings.idx_transport
        if transport not in {"auto", "http", "browser"}:
            raise ValueError("IDX_TRANSPORT must be one of: auto, http, browser")

        if transport == "browser" or (transport == "auto" and self._auto_prefer_browser):
            if self.browser_transport_factory is None:
                raise RuntimeError("Browser attachment transport is not configured")
            if self.observer:
                self.observer.event(
                    "download",
                    "using learned browser attachment transport" if self._auto_prefer_browser and transport == "auto" else "using browser transport",
                    url=url,
                )
            content, content_type = self.browser_transport_factory().get_bytes(url)
            if self.observer:
                self.observer.update_task(task_id, completed=len(content), total=len(content))
            return content, content_type

        try:
            if task_id is None:
                return self._download_bytes_http(url)
            return self._download_bytes_http(url, task_id)
        except httpx.HTTPStatusError as exc:
            if (
                transport == "auto"
                and exc.response.status_code == 403
                and self.browser_transport_factory is not None
            ):
                self._auto_prefer_browser = True
                if self.observer:
                    self.observer.event(
                        "download",
                        "HTTP 403; learned browser transport for remaining attachments",
                        level="WARNING",
                        always=True,
                        url=url,
                    )
                content, content_type = self.browser_transport_factory().get_bytes(url)
                if self.observer:
                    self.observer.update_task(task_id, completed=len(content), total=len(content))
                return content, content_type
            raise

    def download(
        self,
        *,
        ticker: str,
        announcement_id: str,
        url: str,
        original_filename: str,
    ) -> tuple[Path, str, str]:
        task_id = self.observer.start_task(
            f"Downloading {ticker} • {safe_filename(original_filename)}",
            total=None,
            kind="bytes",
        ) if self.observer else None
        started = time.perf_counter()
        try:
            content, content_type = self._download_bytes(url, task_id)
            digest = hashlib.sha256(content).hexdigest()
            url_suffix = Path(urlparse(url).path).suffix
            filename = safe_filename(original_filename)
            if not Path(filename).suffix and url_suffix:
                filename += url_suffix
            destination = self.settings.data_dir / "raw" / ticker / safe_filename(announcement_id)
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / f"{digest[:12]}_{filename}"
            if not path.exists():
                path.write_bytes(content)
                cache_status = "written"
            else:
                cache_status = "already-exists"
            if self.observer:
                self.observer.update_task(task_id, completed=len(content), total=len(content))
                self.observer.event(
                    "download",
                    "attachment stored",
                    ticker=ticker,
                    filename=original_filename,
                    path=str(path),
                    bytes=len(content),
                    sha256=digest,
                    cache_status=cache_status,
                    elapsed_seconds=f"{time.perf_counter() - started:.3f}",
                )
            return path, digest, content_type
        finally:
            if self.observer:
                self.observer.finish_task(task_id)
