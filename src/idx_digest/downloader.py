from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

try:  # Optional dependency: TLS-fingerprint impersonation transport.
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - exercised only when extra is absent
    curl_requests = None

from .browser_transport import IDXBrowserTransport
from .config import Settings
from .observability import RunObserver


class RetryableDownloadError(RuntimeError):
    pass


class ImpersonateDownloadBlocked(RuntimeError):
    """All impersonation profiles were challenged for one attachment URL."""


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
        # Latch: once TLS impersonation clears the attachment host in auto mode,
        # skip the httpx path that would 403 again on every file.
        self._auto_prefer_impersonate = False
        self._curl = None
        self._curl_profile: str | None = None
        self.client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(90, connect=20),
            follow_redirects=True,
            http2=True,
        )

    def _impersonate_profiles(self) -> list[str]:
        configured = getattr(self.settings, "idx_impersonate_profile", "chrome131") or "chrome131"
        order = [configured, "chrome131", "chrome124", "chrome120", "chrome116"]
        seen: set[str] = set()
        return [p for p in order if p and not (p in seen or seen.add(p))]

    def _curl_session_for(self, profile: str):
        if curl_requests is None:
            raise ImpersonateDownloadBlocked(
                "curl_cffi is not installed; the 'impersonate' transport is unavailable."
            )
        if self._curl is not None and self._curl_profile == profile:
            return self._curl
        headers = {
            "Referer": f"{self.settings.idx_base_url}/id/perusahaan-tercatat/keterbukaan-informasi",
        }
        if self.settings.idx_cookie:
            headers["Cookie"] = self.settings.idx_cookie
        if self._curl is not None:
            try:
                self._curl.close()
            except Exception:
                pass
        self._curl = curl_requests.Session(headers=headers, impersonate=profile, timeout=90)
        self._curl_profile = profile
        return self._curl

    def _download_bytes_impersonate(self, url: str, task_id: int | None = None) -> tuple[bytes, str]:
        """Download an attachment through curl_cffi TLS impersonation, rotating profiles on a 403.

        The IDX static attachment host sits behind the same Cloudflare
        fingerprint gate as the metadata endpoint, so a stock Python TLS
        handshake is served a 403 challenge page. Impersonating Chrome fetches
        the real bytes with no browser.
        """
        profiles = self._impersonate_profiles()
        last: str | None = None
        for profile in profiles:
            session = self._curl_session_for(profile)
            if self.observer:
                self.observer.event("download", "impersonate download request", url=url, profile=profile)
            try:
                response = session.get(url)
            except Exception as exc:  # curl_cffi network/transport failure -> retryable
                raise RetryableDownloadError(f"impersonate download transport error: {exc}") from exc
            status = int(response.status_code)
            if status == 429 or status >= 500:
                raise RetryableDownloadError(f"HTTP {status} for {url}")
            if status == 403:
                last = f"403 under {profile}"
                if self.observer:
                    self.observer.event(
                        "download", "attachment host challenged impersonation; rotating",
                        level="WARNING", url=url, profile=profile,
                    )
                continue
            if status >= 400:
                response.raise_for_status()
            content = response.content
            content_type = response.headers.get("content-type", "application/octet-stream")
            if self.observer:
                self.observer.update_task(task_id, completed=len(content), total=len(content))
                self.observer.event(
                    "download", "impersonate download complete",
                    url=url, profile=profile, bytes=len(content), content_type=content_type,
                )
            return content, content_type
        raise ImpersonateDownloadBlocked(f"all impersonation profiles blocked for {url}: {last}")

    def close(self) -> None:
        self.client.close()
        if self._curl is not None:
            try:
                self._curl.close()
            except Exception:
                pass

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
        if transport not in {"auto", "http", "browser", "impersonate"}:
            raise ValueError("IDX_TRANSPORT must be one of: auto, http, browser, impersonate")

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

        if transport == "impersonate" or (transport == "auto" and self._auto_prefer_impersonate):
            try:
                return self._download_bytes_impersonate(url, task_id)
            except ImpersonateDownloadBlocked as exc:
                # Every profile was blocked; escalate to the browser only as a last resort.
                if self.browser_transport_factory is not None:
                    self._auto_prefer_browser = True
                    if self.observer:
                        self.observer.event(
                            "download", "impersonation blocked for attachment; escalating to Chromium",
                            level="WARNING", always=True, url=url, error=str(exc),
                        )
                    content, content_type = self.browser_transport_factory().get_bytes(url)
                    if self.observer:
                        self.observer.update_task(task_id, completed=len(content), total=len(content))
                    return content, content_type
                raise

        try:
            if task_id is None:
                return self._download_bytes_http(url)
            return self._download_bytes_http(url, task_id)
        except httpx.HTTPStatusError as exc:
            if transport == "auto" and exc.response.status_code == 403:
                # The attachment host is fingerprint-gated too. Try TLS impersonation
                # before spinning up Chromium, and latch it for remaining files.
                if self.observer:
                    self.observer.event(
                        "download",
                        "HTTP 403; trying TLS impersonation for attachment before Chromium",
                        level="WARNING",
                        always=True,
                        url=url,
                    )
                try:
                    content, content_type = self._download_bytes_impersonate(url, task_id)
                    self._auto_prefer_impersonate = True
                    return content, content_type
                except (ImpersonateDownloadBlocked, RetryableDownloadError) as imp_exc:
                    if self.browser_transport_factory is not None:
                        self._auto_prefer_browser = True
                        if self.observer:
                            self.observer.event(
                                "download",
                                "impersonation failed for attachment; learned browser transport",
                                level="WARNING",
                                always=True,
                                url=url,
                                error=str(imp_exc),
                            )
                        content, content_type = self.browser_transport_factory().get_bytes(url)
                        if self.observer:
                            self.observer.update_task(task_id, completed=len(content), total=len(content))
                        return content, content_type
                    raise
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
