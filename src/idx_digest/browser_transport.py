from __future__ import annotations

import base64
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import APIResponse, BrowserContext, Page, Playwright, Response, sync_playwright

from .config import Settings
from .observability import RunObserver


class IDXBrowserError(RuntimeError):
    pass


class IDXBrowserTransport:
    """Fetch IDX data and attachments inside one persistent Chromium context.

    This does not solve or bypass CAPTCHAs. When IDX presents an interactive
    verification page and headed mode is enabled, the user can complete it in
    the browser window; the transport then retries automatically.
    """

    DISCLOSURE_PAGE = "/id/perusahaan-tercatat/keterbukaan-informasi"
    ENDPOINT = "/primary/ListedCompany/GetAnnouncement"

    def __init__(self, settings: Settings, observer: RunObserver | None = None):
        self.settings = settings
        self.observer = observer
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.trace_path: Path | None = None
        self._attachment_transport_mode = "context"
        self._trace_started = False

    def _browser_log(self, message: str, **fields: Any) -> None:
        if self.observer:
            self.observer.browser(message, **fields)

    def _on_response(self, response: Response) -> None:
        if "idx.co.id" not in response.url:
            return
        self._browser_log(
            "network response",
            method=response.request.method,
            status=response.status,
            resource_type=response.request.resource_type,
            url=response.url,
        )

    def _on_request_failed(self, request: Any) -> None:
        self._browser_log(
            "network request failed",
            method=request.method,
            resource_type=request.resource_type,
            url=request.url,
            failure=request.failure,
        )

    def start(self) -> None:
        if self.context is not None:
            return

        profile_dir = Path(self.settings.idx_browser_profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._browser_log(
            "launching persistent Chromium",
            profile_dir=str(profile_dir),
            headless=self.settings.idx_browser_headless,
            timezone=self.settings.app_timezone,
        )

        started = time.perf_counter()
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.settings.idx_browser_headless,
            locale="id-ID",
            timezone_id=self.settings.app_timezone,
            viewport={"width": 1440, "height": 900},
        )
        self._browser_log(
            "Chromium launched",
            elapsed_seconds=f"{time.perf_counter() - started:.3f}",
            pages=len(self.context.pages),
        )

        if self.observer and self.observer.trace_browser:
            trace_dir = self.settings.data_dir / "traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(self.observer.timezone).strftime("%Y%m%d-%H%M%S-%f%z")
            self.trace_path = trace_dir / f"idx-browser-{stamp}.zip"
            self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self._trace_started = True
            self._browser_log("Playwright trace started", trace_path=str(self.trace_path))

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        if self.observer and self.observer.browser_network:
            self.page.on("response", self._on_response)
            self.page.on("requestfailed", self._on_request_failed)

        disclosure_url = f"{self.settings.idx_base_url}{self.DISCLOSURE_PAGE}"
        self._browser_log("navigating to disclosure page", url=disclosure_url)
        navigation_started = time.perf_counter()
        response = self.page.goto(
            disclosure_url,
            wait_until="domcontentloaded",
            timeout=self.settings.idx_browser_navigation_timeout_ms,
        )
        self._browser_log(
            "disclosure page loaded",
            status=response.status if response else None,
            final_url=self.page.url,
            title=self.page.title(),
            elapsed_seconds=f"{time.perf_counter() - navigation_started:.3f}",
        )

    def close(self) -> None:
        if self.context is not None:
            if self._trace_started and self.trace_path is not None:
                try:
                    self.context.tracing.stop(path=str(self.trace_path))
                    self._browser_log("Playwright trace saved", trace_path=str(self.trace_path))
                except Exception as exc:  # trace failure should not hide the scrape result
                    self._browser_log("failed to save Playwright trace", error=str(exc))
                finally:
                    self._trace_started = False
            self._browser_log("closing Chromium context")
            self.context.close()
            self.context = None
            self.page = None
        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None
            self._browser_log("Playwright stopped")

    def __enter__(self) -> "IDXBrowserTransport":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _wait_for_verification(self, *, status: int | None = None) -> None:
        assert self.page is not None
        self._browser_log(
            "waiting for browser verification",
            status=status,
            current_url=self.page.url,
            wait_seconds=2,
        )
        self.page.bring_to_front()
        self.page.wait_for_timeout(2_000)

    def _rate_limit_cooldown_seconds(
        self,
        *,
        attempt: int,
        retry_after: str | None = None,
        remaining_seconds: float | None = None,
    ) -> float:
        """Return a conservative 429 cooldown with exponential backoff + jitter.

        Retry-After wins when IDX supplies a numeric value. The browser verification
        deadline remains the outer bound so a permanently throttled run becomes a
        recoverable partial checkpoint instead of looping forever.
        """
        header_wait = 0.0
        if retry_after:
            try:
                header_wait = max(0.0, float(str(retry_after).strip()))
            except (TypeError, ValueError):
                header_wait = 0.0
        base = max(
            header_wait,
            min(
                float(self.settings.idx_429_cooldown_max_seconds),
                float(self.settings.idx_429_cooldown_initial_seconds) * (2 ** max(0, attempt - 1)),
            ),
        )
        jitter = random.uniform(0.0, float(self.settings.idx_429_jitter_seconds)) if self.settings.idx_429_jitter_seconds else 0.0
        wait = base + jitter
        if remaining_seconds is not None:
            wait = min(wait, max(0.0, remaining_seconds))
        return max(0.0, wait)

    def _wait_for_rate_limit(
        self,
        *,
        attempt: int,
        retry_after: str | None,
        deadline: float,
    ) -> float:
        assert self.page is not None
        remaining = max(0.0, deadline - time.monotonic())
        wait = self._rate_limit_cooldown_seconds(
            attempt=attempt, retry_after=retry_after, remaining_seconds=remaining
        )
        self._browser_log(
            "IDX rate limited; cooling down before retry",
            status=429,
            attempt=attempt,
            cooldown_seconds=round(wait, 2),
            retry_after=retry_after or None,
            current_url=self.page.url,
        )
        if wait > 0:
            self.page.wait_for_timeout(int(wait * 1_000))
        return wait

    def _fetch_once(self, params: dict[str, Any], *, endpoint: str | None = None) -> dict[str, Any]:
        self.start()
        assert self.page is not None

        endpoint = endpoint or self.ENDPOINT
        self._browser_log(
            "fetching IDX JSON API in page",
            endpoint=endpoint,
            index_from=params.get("indexFrom"),
            page_size=params.get("pageSize"),
            ticker=params.get("kodeEmiten") or "ALL",
            date_from=params.get("dateFrom"),
            date_to=params.get("dateTo"),
        )
        started = time.perf_counter()
        result = self.page.evaluate(
            """
            async ({baseUrl, endpoint, params}) => {
              const query = new URLSearchParams();
              for (const [key, value] of Object.entries(params)) {
                query.set(key, String(value ?? ""));
              }
              const response = await fetch(`${baseUrl}${endpoint}?${query.toString()}`, {
                method: "GET",
                credentials: "include",
                headers: {
                  "Accept": "application/json, text/plain, */*",
                  "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.7,en;q=0.6"
                }
              });
              return {
                status: response.status,
                contentType: response.headers.get("content-type") || "",
                retryAfter: response.headers.get("retry-after") || "",
                body: await response.text()
              };
            }
            """,
            {
                "baseUrl": self.settings.idx_base_url,
                "endpoint": endpoint,
                "params": params,
            },
        )
        if not isinstance(result, dict):
            raise IDXBrowserError("Unexpected result returned by the browser")
        self._browser_log(
            "IDX JSON API response",
            status=result.get("status"),
            content_type=result.get("contentType"),
            body_characters=len(str(result.get("body") or "")),
            elapsed_seconds=f"{time.perf_counter() - started:.3f}",
        )
        return result

    def get_json(self, params: dict[str, Any], *, endpoint: str | None = None, require_replies: bool = True) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.idx_browser_verification_timeout_seconds
        last_status: int | None = None
        last_preview = ""
        attempt = 0

        while True:
            attempt += 1
            result = self._fetch_once(params, endpoint=endpoint)
            status = int(result.get("status") or 0)
            content_type = str(result.get("contentType") or "").lower()
            body = str(result.get("body") or "")
            last_status = status
            last_preview = body[:250].replace("\n", " ")

            if status == 200 and "json" in content_type:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise IDXBrowserError("IDX returned invalid JSON inside Chromium") from exc
                if not isinstance(payload, dict):
                    raise IDXBrowserError("IDX response is not a JSON object")
                if require_replies and "Replies" not in payload:
                    raise IDXBrowserError("IDX response does not contain the expected Replies field")
                self._browser_log(
                    "IDX JSON accepted",
                    attempt=attempt,
                    result_count=payload.get("ResultCount"),
                    replies=len(payload.get("Replies") or []),
                )
                return payload

            now = time.monotonic()
            if status == 429 and now < deadline:
                self._wait_for_rate_limit(
                    attempt=attempt,
                    retry_after=str(result.get("retryAfter") or ""),
                    deadline=deadline,
                )
                if time.monotonic() < deadline:
                    continue

            retryable_challenge = status in {403, 503} or ("text/html" in content_type and status != 429)
            if retryable_challenge and time.monotonic() < deadline:
                self._wait_for_verification(status=status)
                continue

            mode = "headless" if self.settings.idx_browser_headless else "headed"
            raise IDXBrowserError(
                f"IDX browser request failed with HTTP {last_status} in {mode} mode: {last_preview!r}. "
                "If a verification page is shown, complete it in the Chromium window. "
                "This tool does not solve CAPTCHAs automatically."
            )

    def _fetch_bytes_in_page(self, url: str) -> dict[str, Any]:
        """Use Chromium's page network stack when APIRequestContext is challenged."""
        assert self.page is not None
        self._browser_log("downloading attachment with in-page fetch", url=url)
        started = time.perf_counter()
        result = self.page.evaluate(
            """
            async (url) => {
              const response = await fetch(url, {
                method: "GET",
                credentials: "include",
                headers: {
                  "Accept": "application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream,*/*"
                }
              });
              const contentType = response.headers.get("content-type") || "application/octet-stream";
              if (!response.ok || contentType.toLowerCase().includes("text/html")) {
                return {
                  status: response.status,
                  contentType,
                  bodyBase64: ""
                };
              }
              const bytes = new Uint8Array(await response.arrayBuffer());
              const chunkSize = 0x8000;
              let binary = "";
              for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
              }
              return {
                status: response.status,
                contentType,
                bodyBase64: btoa(binary)
              };
            }
            """,
            url,
        )
        if not isinstance(result, dict):
            raise IDXBrowserError("Unexpected attachment result returned by Chromium")
        self._browser_log(
            "in-page attachment response",
            url=url,
            status=result.get("status"),
            content_type=result.get("contentType"),
            encoded_characters=len(str(result.get("bodyBase64") or "")),
            elapsed_seconds=f"{time.perf_counter() - started:.3f}",
        )
        return result

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """Download an attachment through the browser's active IDX session."""

        self.start()
        assert self.context is not None

        deadline = time.monotonic() + self.settings.idx_browser_verification_timeout_seconds
        last_status: int | None = None
        last_content_type = ""
        attempt = 0

        while True:
            attempt += 1
            if self._attachment_transport_mode != "in-page":
                self._browser_log("downloading attachment with browser request context", attempt=attempt, url=url)
                started = time.perf_counter()
                response: APIResponse = self.context.request.get(
                    url,
                    headers={
                        "Accept": "application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream,*/*",
                        "Referer": f"{self.settings.idx_base_url}{self.DISCLOSURE_PAGE}",
                    },
                    timeout=self.settings.idx_browser_navigation_timeout_ms,
                    fail_on_status_code=False,
                )
                status = response.status
                content_type = response.headers.get("content-type", "application/octet-stream").lower()
                last_status = status
                last_content_type = content_type
                self._browser_log(
                    "browser request-context attachment response",
                    attempt=attempt,
                    url=url,
                    status=status,
                    content_type=content_type,
                    elapsed_seconds=f"{time.perf_counter() - started:.3f}",
                )

                if status == 200 and "text/html" not in content_type:
                    body = response.body()
                    self._browser_log("attachment bytes received", url=url, bytes=len(body), transport="browser-context")
                    return body, content_type
            else:
                self._browser_log("using learned in-page attachment transport", attempt=attempt, url=url)

            page_result = self._fetch_bytes_in_page(url)
            page_status = int(page_result.get("status") or 0)
            page_content_type = str(
                page_result.get("contentType") or "application/octet-stream"
            ).lower()
            page_body = str(page_result.get("bodyBase64") or "")
            last_status = page_status
            last_content_type = page_content_type
            if page_status == 200 and page_body and "text/html" not in page_content_type:
                try:
                    decoded = base64.b64decode(page_body, validate=True)
                except ValueError as exc:
                    raise IDXBrowserError("Chromium returned invalid base64 attachment data") from exc
                if self._attachment_transport_mode != "in-page":
                    self._attachment_transport_mode = "in-page"
                    self._browser_log(
                        "attachment transport learned",
                        transport="in-page-fetch",
                        reason="browser request context was challenged while page fetch succeeded",
                    )
                self._browser_log("attachment bytes received", url=url, bytes=len(decoded), transport="in-page-fetch")
                return decoded, page_content_type

            retryable_challenge = page_status in {403, 429, 503} or "text/html" in page_content_type
            if retryable_challenge and time.monotonic() < deadline:
                self._wait_for_verification(status=page_status)
                continue

            mode = "headless" if self.settings.idx_browser_headless else "headed"
            raise IDXBrowserError(
                f"IDX attachment request failed with HTTP {last_status} in {mode} mode "
                f"(content-type {last_content_type!r}) for {url}. "
                "If IDX asks for verification, complete it in the Chromium window."
            )
