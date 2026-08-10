from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .browser_transport import IDXBrowserTransport
from .config import Settings
from .observability import RunObserver
from .stock_master import StockMasterCache


class IDXResponseError(RuntimeError):
    pass


class RetryableHTTPError(RuntimeError):
    pass


class IDXClient:
    ENDPOINT = "/primary/ListedCompany/GetAnnouncement"
    STOCK_MASTER_ENDPOINT = "/primary/ListedCompany/GetCompanyProfiles"

    def __init__(self, settings: Settings, observer: RunObserver | None = None):
        self.settings = settings
        self.observer = observer
        self.browser: IDXBrowserTransport | None = None
        self.use_browser = settings.idx_transport == "browser"

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.7,en;q=0.6",
            "Referer": f"{settings.idx_base_url}/id/perusahaan-tercatat/keterbukaan-informasi",
            "User-Agent": settings.idx_user_agent,
        }
        if settings.idx_cookie:
            headers["Cookie"] = settings.idx_cookie
        self.client = httpx.Client(
            base_url=settings.idx_base_url,
            headers=headers,
            timeout=httpx.Timeout(45.0, connect=20.0),
            follow_redirects=True,
            http2=True,
        )
        self._ticker_shard_requests = 0
        self._last_ticker_shard_request_at = 0.0

    def browser_transport(self) -> IDXBrowserTransport:
        """Return the single browser transport shared by metadata and attachments."""
        if self.browser is None:
            if self.observer:
                self.observer.event(
                    "idx",
                    "creating shared browser transport",
                    profile_dir=str(self.settings.idx_browser_profile_dir),
                )
            self.browser = IDXBrowserTransport(self.settings, observer=self.observer)
        self.use_browser = True
        return self.browser


    def _pace_ticker_shard(self, *, ticker: str, day: str) -> None:
        """Pace correctness-preserving per-ticker fallback queries.

        The IDX endpoint only exposes day-granularity date filters. If an entire
        day is still truncated after date sharding, per-ticker queries are the
        final completeness fallback. They must be deliberately paced so the
        fallback itself does not create a Cloudflare 429 storm.
        """
        interval = float(getattr(self.settings, "idx_ticker_shard_delay_seconds", 1.0))
        jitter_max = float(getattr(self.settings, "idx_ticker_shard_jitter_seconds", 0.20))
        burst_size = int(getattr(self.settings, "idx_ticker_shard_burst_size", 25))
        burst_cooldown = float(getattr(self.settings, "idx_ticker_shard_burst_cooldown_seconds", 12.0))
        now = time.monotonic()
        wait = max(0.0, interval - max(0.0, now - self._last_ticker_shard_request_at))
        if self._ticker_shard_requests and self._ticker_shard_requests % burst_size == 0:
            wait = max(wait, burst_cooldown)
        if jitter_max > 0:
            wait += random.uniform(0.0, jitter_max)
        if wait > 0:
            if self.observer:
                self.observer.event(
                    "idx",
                    "pacing ticker-shard fallback",
                    ticker=ticker,
                    day=day,
                    wait_seconds=round(wait, 2),
                    request_number=self._ticker_shard_requests + 1,
                    burst_size=burst_size,
                )
            time.sleep(wait)
        self._ticker_shard_requests += 1
        self._last_ticker_shard_request_at = time.monotonic()

    def close(self) -> None:
        self.client.close()
        if self.browser is not None:
            self.browser.close()

    def __enter__(self) -> "IDXClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RetryableHTTPError)),
        wait=wait_exponential_jitter(initial=1, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get_json_http(self, params: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if self.observer:
            self.observer.event(
                "idx",
                "HTTP metadata request",
                index_from=params.get("indexFrom"),
                page_size=params.get("pageSize"),
                ticker=params.get("kodeEmiten") or "ALL",
                date_from=params.get("dateFrom"),
                date_to=params.get("dateTo"),
            )
        response = self.client.get(self.ENDPOINT, params=params)
        if self.observer:
            self.observer.event(
                "idx",
                "HTTP metadata response",
                status=response.status_code,
                content_type=response.headers.get("content-type"),
                bytes=len(response.content),
                elapsed_seconds=f"{time.perf_counter() - started:.3f}",
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableHTTPError(f"IDX returned HTTP {response.status_code}")
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            preview = response.text[:200].replace("\n", " ")
            raise IDXResponseError(
                f"Expected JSON from IDX but received {content_type!r}: {preview!r}. "
                "The internal endpoint may have changed or Cloudflare may require a session."
            )
        payload = response.json()
        if not isinstance(payload, dict) or "Replies" not in payload:
            raise IDXResponseError("IDX response does not contain the expected Replies field")
        return payload

    def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        transport = self.settings.idx_transport
        if transport not in {"auto", "http", "browser"}:
            raise ValueError("IDX_TRANSPORT must be one of: auto, http, browser")

        if self.use_browser or transport == "browser":
            if self.observer:
                self.observer.event("idx", "metadata request using Chromium", transport=transport)
            return self.browser_transport().get_json(params)

        try:
            return self._get_json_http(params)
        except httpx.HTTPStatusError as exc:
            if transport == "auto" and exc.response.status_code == 403:
                if self.observer:
                    self.observer.event(
                        "idx",
                        "HTTP 403; switching all IDX requests to Chromium",
                        level="WARNING",
                        always=True,
                    )
                return self.browser_transport().get_json(params)
            raise
        except IDXResponseError as exc:
            if transport == "auto":
                if self.observer:
                    self.observer.event(
                        "idx",
                        "unexpected HTTP response; switching to Chromium",
                        level="WARNING",
                        always=True,
                        error=str(exc),
                    )
                return self.browser_transport().get_json(params)
            raise

    def _get_json_endpoint(self, endpoint: str, params: dict[str, Any], *, require_replies: bool = False) -> dict[str, Any]:
        """Fetch another read-only IDX JSON endpoint through the learned transport."""
        transport = self.settings.idx_transport
        if self.use_browser or transport == "browser":
            return self.browser_transport().get_json(params, endpoint=endpoint, require_replies=require_replies)
        try:
            response = self.client.get(endpoint, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableHTTPError(f"IDX returned HTTP {response.status_code}")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise IDXResponseError("IDX endpoint response is not a JSON object")
            if require_replies and "Replies" not in payload:
                raise IDXResponseError("IDX endpoint response does not contain Replies")
            return payload
        except (httpx.HTTPStatusError, httpx.TransportError, ValueError, IDXResponseError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if transport == "auto" and (status == 403 or isinstance(exc, (httpx.TransportError, IDXResponseError, ValueError))):
                if self.observer:
                    self.observer.event("idx", "switching auxiliary IDX endpoint to Chromium", level="WARNING", endpoint=endpoint, error=str(exc))
                return self.browser_transport().get_json(params, endpoint=endpoint, require_replies=require_replies)
            raise

    def stock_master_tickers(self) -> frozenset[str] | None:
        if not getattr(self.settings, "stock_master_enabled", True):
            return None
        cache = StockMasterCache(self.settings.data_dir / "stock_master.json")
        max_age = getattr(self.settings, "stock_master_max_age_hours", 24.0)
        minimum = int(getattr(self.settings, "stock_master_min_tickers", 500))
        cached = cache.load(max_age_hours=max_age)
        stale_cached = cache.load()
        if cached is not None and len(cached.tickers) < minimum:
            cached = None
        if stale_cached is not None and len(stale_cached.tickers) < minimum:
            stale_cached = None
        if cached is not None:
            if self.observer:
                self.observer.event("stock-master", "stock master cache hit", tickers=len(cached.tickers), source=cached.source, retrieved_at=cached.retrieved_at)
            return cached.tickers
        params = {
            "start": 0,
            "length": 2000,
            "indexFrom": 0,
            "pageSize": 2000,
            "emitenType": "s",
            "lang": "id",
        }
        try:
            master = cache.refresh(
                lambda: self._get_json_endpoint(self.STOCK_MASTER_ENDPOINT, params, require_replies=False),
                source=f"IDX:{self.STOCK_MASTER_ENDPOINT}",
            )
            if len(master.tickers) < minimum:
                if stale_cached is not None:
                    cache.save(stale_cached)
                else:
                    try:
                        cache.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise ValueError(f"stock-master response contained only {len(master.tickers)} tickers; minimum trusted size is {minimum}")
            if self.observer:
                self.observer.event("stock-master", "stock master refreshed", always=True, tickers=len(master.tickers), source=master.source)
            return master.tickers
        except Exception as exc:
            if stale_cached is not None:
                if self.observer:
                    self.observer.event(
                        "stock-master", "stock master refresh failed; using stale cached allowlist",
                        level="WARNING", always=True, error=str(exc), tickers=len(stale_cached.tickers), retrieved_at=stale_cached.retrieved_at,
                    )
                return stale_cached.tickers
            if self.observer:
                self.observer.event(
                    "stock-master", "authoritative stock master unavailable; retaining emitenType=s server scope",
                    level="WARNING", always=True, error=str(exc),
                )
            return None

    def _collect_range_once(
        self,
        start_at: datetime,
        end_at: datetime,
        *,
        ticker: str,
        keyword: str,
        emiten_type: str,
        page_size_override: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        offset = 0
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        reported_total: int | None = None
        page_number = 0
        complete = False
        reason: str | None = None
        page_size = int(page_size_override or self.settings.idx_page_size)

        while True:
            page_number += 1
            params = {
                "kodeEmiten": ticker,
                "emitenType": emiten_type,
                "indexFrom": offset,
                "pageSize": page_size,
                "dateFrom": start_at.strftime("%Y%m%d"),
                "dateTo": end_at.strftime("%Y%m%d"),
                "lang": "id",
                "keyword": keyword,
            }
            label = f"metadata page {page_number} offset={offset}"
            if self.observer:
                with self.observer.timed("idx", label, ticker=ticker or "ALL"):
                    payload = self._get_json(params)
            else:
                payload = self._get_json(params)
            replies = payload.get("Replies") or []
            reported_total = int(payload.get("ResultCount") or len(replies))
            if self.observer:
                self.observer.event(
                    "idx", "metadata page parsed", page=page_number, offset=offset,
                    replies=len(replies), result_count=reported_total, unique_seen=len(seen),
                    shard_start=start_at.strftime("%Y-%m-%d"), shard_end=end_at.strftime("%Y-%m-%d"),
                    page_size=page_size,
                )

            if not replies:
                if offset == 0:
                    complete = True
                elif offset >= (reported_total or 0):
                    complete = True
                else:
                    reason = f"unexpected empty page at offset {offset} after full page(s); IDX reported {reported_total} results"
                break

            for item in replies:
                announcement = item.get("pengumuman") or {}
                item_id = str(announcement.get("Id2") or "")
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    items.append(item)

            # A short page is a stronger end-of-range signal than ResultCount,
            # which IDX has been observed to report inconsistently.
            if len(replies) < page_size:
                complete = True
                break
            offset += len(replies)
            if reported_total is not None and offset >= reported_total:
                complete = True
                break
            if self.settings.idx_request_delay_seconds:
                if self.observer:
                    self.observer.event("idx", "polite request delay", seconds=self.settings.idx_request_delay_seconds)
                time.sleep(self.settings.idx_request_delay_seconds)

        return items, {
            "complete": complete,
            "reason": reason,
            "reported_total": reported_total,
            "collected": len(items),
            "pages": page_number,
            "start": start_at.strftime("%Y-%m-%d"),
            "end": end_at.strftime("%Y-%m-%d"),
            "ticker": ticker or None,
            "page_size": page_size,
        }

    def _wide_page_probe(
        self,
        start_at: datetime,
        end_at: datetime,
        *,
        ticker: str,
        keyword: str,
        emiten_type: str,
        failed_report: dict[str, Any],
        label: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        """Retry a truncated shard with one larger first page before ticker fanout.

        IDX has been observed returning an empty page for indexFrom>0 even while
        ResultCount says more rows exist. The endpoint's date filters are only
        calendar-date granularity, so the previously proposed intra-day time
        bisection is not available here. A larger pageSize is substantially
        cheaper and, when ResultCount fits inside the probe size, can reconstruct
        the complete shard in a single request.

        Returning ``None`` means a probe would not be useful (for example, the
        reported total is larger than the configured probe size).
        """
        base_probe_size = int(getattr(self.settings, "idx_wide_page_probe_size", 200))
        max_probe_size = int(getattr(self.settings, "idx_wide_page_probe_max_size", 1000))
        normal_size = int(self.settings.idx_page_size)
        reported_total = int(failed_report.get("reported_total") or 0)
        if max_probe_size <= normal_size:
            return None
        # Adapt the one-shot page to the shard IDX itself reports. v0.15.2 used
        # a fixed 200-row rescue, which worked for 189 rows but needlessly gave
        # up on a 321-row day. We now request exactly enough rows when the total
        # is within a bounded maximum, then independently verify the response.
        probe_size = min(max_probe_size, max(base_probe_size, reported_total or base_probe_size))
        if reported_total > max_probe_size:
            if self.observer:
                self.observer.event(
                    "idx",
                    "wide-page probe skipped; reported shard exceeds adaptive probe maximum",
                    label=label,
                    reported_total=reported_total,
                    base_probe_page_size=base_probe_size,
                    probe_page_size=probe_size,
                    max_probe_page_size=max_probe_size,
                    ticker=ticker or "ALL",
                    shard_start=start_at.strftime("%Y-%m-%d"),
                    shard_end=end_at.strftime("%Y-%m-%d"),
                )
            return None
        if self.observer:
            self.observer.event(
                "idx",
                "retrying truncated shard with wide first page",
                level="WARNING",
                always=True,
                label=label,
                reported_total=reported_total,
                previous_page_size=failed_report.get("page_size") or normal_size,
                base_probe_page_size=base_probe_size,
                probe_page_size=probe_size,
                max_probe_page_size=max_probe_size,
                ticker=ticker or "ALL",
                shard_start=start_at.strftime("%Y-%m-%d"),
                shard_end=end_at.strftime("%Y-%m-%d"),
            )
        items, report = self._collect_range_once(
            start_at,
            end_at,
            ticker=ticker,
            keyword=keyword,
            emiten_type=emiten_type,
            page_size_override=probe_size,
        )
        report = dict(report)
        report["wide_page_probe"] = True
        report["wide_page_probe_size"] = probe_size
        # A server-side hard cap can return (for example) only 50 rows even when
        # pageSize=200. _collect_range_once normally treats a short page as a
        # strong end signal because IDX ResultCount can be noisy. For this probe,
        # however, the entire purpose is to verify that the advertised shard fits
        # in one wide page, so fewer rows than ResultCount must remain incomplete.
        probe_reported = int(report.get("reported_total") or 0)
        probe_collected = int(report.get("collected") or 0)
        if report["complete"] and probe_reported > probe_collected:
            report["complete"] = False
            report["reason"] = (
                f"wide-page probe returned only {probe_collected} of {probe_reported} reported rows; "
                "server may be enforcing a smaller response cap"
            )
        if self.observer:
            self.observer.event(
                "idx",
                "wide-page probe complete" if report["complete"] else "wide-page probe still incomplete",
                level="INFO" if report["complete"] else "WARNING",
                always=True,
                label=label,
                complete=report["complete"],
                reported_total=report.get("reported_total"),
                collected=report.get("collected"),
                probe_page_size=probe_size,
                ticker=ticker or "ALL",
                shard_start=start_at.strftime("%Y-%m-%d"),
                shard_end=end_at.strftime("%Y-%m-%d"),
            )
        return items, report

    def collect_announcements(
        self,
        start_at: datetime,
        end_at: datetime,
        ticker: str | None = None,
        keyword: str = "",
        emiten_type: str = "s",
        *,
        allow_ticker_fallback: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ticker_value = ticker.strip().upper() if ticker else ""
        items, primary = self._collect_range_once(
            start_at, end_at, ticker=ticker_value, keyword=keyword, emiten_type=emiten_type,
        )
        if primary["complete"]:
            return items, {"complete": True, "strategy": "pagination", "primary": primary, "shards": []}

        # If the entire requested range contains no more rows than the configured
        # wide page, try one large first page before exploding the range into days.
        primary_probe = self._wide_page_probe(
            start_at,
            end_at,
            ticker=ticker_value,
            keyword=keyword,
            emiten_type=emiten_type,
            failed_report=primary,
            label="primary-range",
        )
        if primary_probe is not None:
            probe_items, probe_report = primary_probe
            if probe_report["complete"]:
                return probe_items, {
                    "complete": True,
                    "strategy": "wide-page-probe",
                    "primary": primary,
                    "wide_page_probe": probe_report,
                    "shards": [],
                    "collected": len(probe_items),
                }

        if self.observer:
            self.observer.event(
                "idx", "metadata pagination inconsistent; falling back to date shards",
                level="WARNING", always=True, **primary,
            )
        all_items: dict[str, dict[str, Any]] = {}
        shard_reports: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        day = start_at.date()
        while day <= end_at.date():
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=start_at.tzinfo)
            day_end = datetime.combine(day, datetime.max.time(), tzinfo=end_at.tzinfo)
            day_items, report = self._collect_range_once(
                day_start, day_end, ticker=ticker_value, keyword=keyword, emiten_type=emiten_type,
            )
            shard_reports.append(report)
            if report["complete"]:
                for item in day_items:
                    item_id = str((item.get("pengumuman") or {}).get("Id2") or "")
                    if item_id:
                        all_items[item_id] = item
            else:
                # First retry the busy day with a larger page. IDX currently only
                # exposes dateFrom/dateTo at calendar-day granularity, so intra-day
                # time-window bisection is not a supported request primitive here.
                # A 200-row probe turns the observed 183-row day from ~962 ticker
                # requests into one request when the server honors pageSize.
                day_probe = self._wide_page_probe(
                    day_start,
                    day_end,
                    ticker=ticker_value,
                    keyword=keyword,
                    emiten_type=emiten_type,
                    failed_report=report,
                    label=f"day:{day.isoformat()}",
                )
                if day_probe is not None:
                    probe_items, probe_report = day_probe
                    shard_reports.append(probe_report)
                    if probe_report["complete"]:
                        for item in probe_items:
                            item_id = str((item.get("pengumuman") or {}).get("Id2") or "")
                            if item_id:
                                all_items[item_id] = item
                        day += timedelta(days=1)
                        continue
                    report = probe_report

                # Last resort only: if one day still cannot be reconstructed with
                # a wide page, split by authoritative stock ticker. This remains
                # correctness-preserving but is intentionally no longer the first
                # response to a busy day.
                master = None
                if allow_ticker_fallback and not ticker_value and getattr(self.settings, "stock_master_enabled", True):
                    master = self.stock_master_tickers()
                if master:
                    ticker_reports: list[dict[str, Any]] = []
                    ticker_complete = True
                    if self.observer:
                        self.observer.event(
                            "idx",
                            "wide-page recovery unavailable or incomplete; entering last-resort paced per-ticker fallback",
                            level="WARNING",
                            always=True,
                            day=day.isoformat(),
                            tickers=len(master),
                            delay_seconds=float(getattr(self.settings, "idx_ticker_shard_delay_seconds", 1.0)),
                            burst_size=int(getattr(self.settings, "idx_ticker_shard_burst_size", 25)),
                            burst_cooldown_seconds=float(getattr(self.settings, "idx_ticker_shard_burst_cooldown_seconds", 12.0)),
                        )
                    for stock_ticker in sorted(master):
                        self._pace_ticker_shard(ticker=stock_ticker, day=day.isoformat())
                        sub_items, sub = self._collect_range_once(
                            day_start, day_end, ticker=stock_ticker, keyword=keyword, emiten_type=emiten_type,
                        )
                        ticker_reports.append(sub)
                        if not sub["complete"]:
                            ticker_complete = False
                        for item in sub_items:
                            item_id = str((item.get("pengumuman") or {}).get("Id2") or "")
                            if item_id:
                                all_items[item_id] = item
                    report = dict(report)
                    report["ticker_shard_complete"] = ticker_complete
                    report["ticker_shards"] = len(ticker_reports)
                    if not ticker_complete:
                        incomplete.append(report)
                else:
                    report = dict(report)
                    if not allow_ticker_fallback:
                        report["ticker_fallback_skipped"] = True
                        report["reason"] = (report.get("reason") or "shard incomplete") + "; incremental mode does not fan out across the full stock master"
                        if self.observer:
                            self.observer.event(
                                "idx",
                                "incremental shard remained incomplete; historical ticker fanout skipped",
                                level="WARNING", always=True,
                                day=day.isoformat(),
                                reported_total=report.get("reported_total"),
                                collected=report.get("collected"),
                            )
                    incomplete.append(report)
            day += timedelta(days=1)

        # Preserve deterministic IDX/newest-first ordering as much as possible.
        result = list(all_items.values())
        result.sort(key=lambda item: str((item.get("pengumuman") or {}).get("TglPengumuman") or ""), reverse=True)
        complete = not incomplete
        return result, {
            "complete": complete,
            "strategy": "date-shards" if complete else "date-shards-partial",
            "primary": primary,
            "shards": shard_reports,
            "incomplete_shards": incomplete,
            "collected": len(result),
        }

    def iter_announcements(
        self,
        start_at: datetime,
        end_at: datetime,
        ticker: str | None = None,
        keyword: str = "",
        emiten_type: str = "s",
    ) -> Iterator[dict[str, Any]]:
        items, _diagnostics = self.collect_announcements(
            start_at, end_at, ticker=ticker, keyword=keyword, emiten_type=emiten_type,
        )
        yield from items

