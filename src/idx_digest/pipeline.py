from __future__ import annotations

import json
import inspect
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .attachment_selector import classify_attachments, is_financial_report_announcement
from .attachment_dedup import AttachmentEvidence, deduplicate_attachments
from .db import Database
from .disclosure_classifier import disclosure_class
from .downloader import AttachmentDownloader
from .extractors import extract_document
from .extraction_scheduler import BoundedExtractionScheduler, ExtractionJob
from .idx_client import IDXClient
from .llm_scheduler import GlobalLLMScheduler, ScheduledJob
from .incremental import (
    CoverageRange,
    company_input_fingerprint,
    coverage_contains,
    normalize_coverage_ranges,
    promote_single_announcement,
    subtract_coverage,
)
from .observability import RunObserver
from .performance_advisor import build_performance_summary
from .routine_triage import RoutineEvidence, evaluate_routine_disclosure
from .summarizer import OpenRouterSummarizer
from .share_export import DEFAULT_SHARE_SECTIONS, load_share_bundle, write_share_export
from .timeutils import parse_idx_datetime


@dataclass(frozen=True)
class PreparedAttachment:
    url: str
    filename: str
    text_path: Path


@dataclass(frozen=True)
class DownloadedAttachment:
    url: str
    filename: str
    path: Path
    content_type: str
    sha256: str


@dataclass
class PreparationPlan:
    announcement_id: str
    ticker: str
    title: str
    announced_at: str
    prepared: list[PreparedAttachment] = field(default_factory=list)
    pending_extractions: int = 0
    selected_sources: int = 0
    extraction_failures: int = 0
    preparation_closed: bool = False
    downstream_queued: bool = False
    attachment_task: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class AnnouncementPlan:
    announcement_id: str
    ticker: str
    title: str
    announced_at: str
    prepared: list[PreparedAttachment]
    pending_documents: int = 0
    document_updates: int = 0
    document_failures: int = 0
    reducer_scheduled: bool = False
    analysis_mode: str = "full"
    triage: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        skip_llm: bool = False,
        observer: RunObserver | None = None,
    ):
        self.settings = settings
        self.observer = observer
        self.settings.ensure_directories()
        self.db = Database(settings.database_path)
        self.summarizer = None if skip_llm else OpenRouterSummarizer(settings, observer=observer)
        self._company_export_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._share_export_lock = threading.Lock()
        self._phase3_lock = threading.Lock()
        self._phase3_metrics: dict[str, int] = {
            "routine_direct": 0, "routine_full": 0, "dedup_suppressed": 0,
        }

    def _download_or_cached_attachment(
        self,
        downloader: AttachmentDownloader,
        *,
        announcement_id: str,
        ticker: str,
        attachment: dict[str, Any],
    ) -> PreparedAttachment | DownloadedAttachment | None:
        """Keep browser I/O on the producer thread and return local work."""
        url = attachment["FullSavePath"]
        original_filename = attachment.get("OriginalFilename") or attachment.get("PDFFilename") or "attachment"
        state = self.db.attachment_state(url)

        path: Path
        content_type: str
        digest: str
        if state and state["local_path"] and Path(state["local_path"]).exists():
            path = Path(state["local_path"])
            content_type = state["content_type"] or ""
            digest = str(state["sha256"] or path.stem)
            if self.observer:
                self.observer.event(
                    "cache", "attachment file cache hit",
                    ticker=ticker, filename=original_filename, path=str(path), bytes=path.stat().st_size,
                )
        else:
            label = f"{ticker} download {original_filename}"
            if self.observer:
                with self.observer.timed("download", label, announcement_id=announcement_id):
                    path, digest, content_type = downloader.download(
                        ticker=ticker, announcement_id=announcement_id, url=url, original_filename=original_filename,
                    )
            else:
                path, digest, content_type = downloader.download(
                    ticker=ticker, announcement_id=announcement_id, url=url, original_filename=original_filename,
                )
            self.db.update_attachment_file(url, local_path=str(path), sha256=digest, content_type=content_type)

        state = self.db.attachment_state(url)
        if state and state["extracted_text_path"] and Path(state["extracted_text_path"]).exists():
            text_path = Path(state["extracted_text_path"])
            if self.observer:
                self.observer.event(
                    "cache", "extracted text cache hit",
                    ticker=ticker, filename=original_filename, path=str(text_path), bytes=text_path.stat().st_size,
                )
            return PreparedAttachment(url=url, filename=original_filename, text_path=text_path)

        return DownloadedAttachment(
            url=url, filename=original_filename, path=path, content_type=content_type, sha256=digest,
        )

    def _extract_downloaded_attachment(
        self,
        downloaded: DownloadedAttachment,
        *,
        announcement_id: str,
        ticker: str,
    ) -> PreparedAttachment | None:
        """Extract a local file. Safe for the bounded background extraction pool."""
        try:
            label = f"{ticker} extract {downloaded.filename}"
            if self.observer:
                with self.observer.timed("extract", label, announcement_id=announcement_id):
                    result = extract_document(downloaded.path, downloaded.content_type, self.settings, self.observer)
            else:
                result = extract_document(downloaded.path, downloaded.content_type, self.settings)
            text_dir = self.settings.data_dir / "text" / ticker
            text_dir.mkdir(parents=True, exist_ok=True)
            text_path = text_dir / f"{downloaded.sha256}.txt"
            text_path.write_text(result.text, encoding="utf-8")
            self.db.update_extraction(downloaded.url, text_path=str(text_path), method=result.method, error=None)
            if self.observer:
                self.observer.event(
                    "extract", "extracted text stored",
                    ticker=ticker, filename=downloaded.filename, path=str(text_path),
                    method=result.method, characters=len(result.text),
                )
            return PreparedAttachment(url=downloaded.url, filename=downloaded.filename, text_path=text_path)
        except Exception as exc:
            self.db.update_extraction(downloaded.url, text_path=None, method=None, error=str(exc))
            if self.observer:
                self.observer.event(
                    "extract", "attachment extraction failed", level="ERROR", always=True,
                    ticker=ticker, filename=downloaded.filename, error=str(exc),
                )
            return None

    def _prepare_attachment(
        self,
        downloader: AttachmentDownloader,
        *,
        announcement_id: str,
        ticker: str,
        attachment: dict[str, Any],
    ) -> PreparedAttachment | None:
        """Compatibility helper for single-attachment callers/tests.

        The market pipeline uses the split download/extract path so extraction can
        overlap browser I/O.
        """
        local = self._download_or_cached_attachment(
            downloader, announcement_id=announcement_id, ticker=ticker, attachment=attachment,
        )
        if local is None or isinstance(local, PreparedAttachment):
            return local
        return self._extract_downloaded_attachment(local, announcement_id=announcement_id, ticker=ticker)

    def _summarize_documents_parallel(
        self,
        prepared: list[PreparedAttachment],
        *,
        announcement_id: str,
        ticker: str,
        title: str,
    ) -> tuple[list[dict[str, str]], int]:
        """Summarize uncached documents with bounded thread concurrency.

        SQLite writes are performed by the caller thread as futures complete. This
        avoids concurrent database writers while still overlapping network-bound
        OpenRouter inference calls.
        """
        if not self.summarizer:
            return [], 0

        pending: list[PreparedAttachment] = []
        for document in prepared:
            cached_summary = self.db.get_document_summary(
                document.url,
                model=self.summarizer.model,
                prompt_version=self._document_prompt_version(title),
            )
            if self.summarizer.is_valid_document_summary(cached_summary):
                if self.observer:
                    self.observer.event(
                        "cache",
                        "document summary cache hit",
                        ticker=ticker,
                        filename=document.filename,
                    )
            else:
                pending.append(document)

        if not pending:
            return [], 0

        workers = min(self.settings.llm_concurrency, len(pending))
        errors: list[dict[str, str]] = []
        completed_summaries = 0
        summary_task = self.observer.start_task(
            f"Document summaries {ticker} • {title[:52]} • workers={workers}",
            total=len(pending),
            kind="items",
        ) if self.observer else None

        if self.observer:
            self.observer.event(
                "parallel",
                "document summary batch started",
                always=True,
                ticker=ticker,
                announcement_id=announcement_id,
                documents=len(pending),
                workers=workers,
            )
            if self.observer.stream_llm and workers > 1:
                self.observer.event(
                    "parallel",
                    "raw document streams disabled to prevent interleaved JSON; announcement and company streams remain enabled",
                    level="WARNING",
                    always=True,
                    workers=workers,
                )

        def summarize_one(document: PreparedAttachment) -> dict[str, Any]:
            text = document.text_path.read_text(encoding="utf-8", errors="replace")
            label = f"{ticker} document summary {document.filename}"
            # Multiple raw SSE streams cannot share one terminal. Stream only when
            # this batch is effectively sequential; otherwise retain timestamped
            # request/completion events for each worker.
            stream = None if workers == 1 else False
            financial_chunk_chars = (
                min(self.settings.llm_chunk_chars, 22_000)
                if is_financial_report_announcement(title)
                else None
            )
            if self.observer:
                with self.observer.timed(
                    "llm-document",
                    label,
                    announcement_id=announcement_id,
                    filename=document.filename,
                    characters=len(text),
                    workers=workers,
                ):
                    kwargs = {
                        "ticker": ticker,
                        "filename": document.filename,
                        "text": text,
                        "stream": stream,
                        "source_url": document.url,
                        "announcement_id": announcement_id,
                        "document_profile": self._document_profile(title),
                    }
                    if financial_chunk_chars is not None:
                        kwargs["chunk_chars"] = financial_chunk_chars
                    if not self._document_profile_supported():
                        kwargs.pop("document_profile", None)
                    return self.summarizer.summarize_document(**kwargs)
            kwargs = {
                "ticker": ticker,
                "filename": document.filename,
                "text": text,
                "stream": stream,
                "source_url": document.url,
                "announcement_id": announcement_id,
                "document_profile": self._document_profile(title),
            }
            if financial_chunk_chars is not None:
                kwargs["chunk_chars"] = financial_chunk_chars
            if not self._document_profile_supported():
                kwargs.pop("document_profile", None)
            return self.summarizer.summarize_document(**kwargs)

        futures: dict[Future[dict[str, Any]], PreparedAttachment] = {}
        try:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="idx-document-summary",
            ) as executor:
                for document in pending:
                    futures[executor.submit(summarize_one, document)] = document

                for future in as_completed(futures):
                    document = futures[future]
                    try:
                        summary = future.result()
                        self.db.save_document_summary(
                            document.url,
                            ticker,
                            summary,
                            self.summarizer.model,
                            self._document_prompt_version(title),
                        )
                        completed_summaries += 1
                        if self.observer:
                            self.observer.event(
                                "llm-document",
                                "document summary stored",
                                ticker=ticker,
                                filename=document.filename,
                                chunk_count=summary.get("chunk_count"),
                                summary_preview=str(summary.get("summary") or "")[:180],
                            )
                    except Exception as exc:
                        errors.append(
                            {
                                "id": announcement_id,
                                "filename": document.filename,
                                "stage": "document-summary",
                                "error": str(exc),
                            }
                        )
                        if self.observer:
                            self.observer.event(
                                "llm-document",
                                "document summary failed",
                                level="ERROR",
                                always=True,
                                ticker=ticker,
                                announcement_id=announcement_id,
                                filename=document.filename,
                                error=str(exc),
                            )
                    finally:
                        if self.observer:
                            self.observer.update_task(summary_task, advance=1)
        finally:
            if self.observer:
                self.observer.finish_task(summary_task)
                self.observer.event(
                    "parallel",
                    "document summary batch finished",
                    always=True,
                    ticker=ticker,
                    announcement_id=announcement_id,
                    requested=len(pending),
                    completed=completed_summaries,
                    failed=len(errors),
                    workers=workers,
                )

        return errors, completed_summaries

    def _refresh_share_exports(self, start_at: str, end_at: str, ticker: str | None = None) -> dict[str, str]:
        bundle = load_share_bundle(
            self.settings.database_path,
            start_at=start_at,
            end_at=end_at,
            ticker=(ticker or "").strip().upper() or None,
            sections=DEFAULT_SHARE_SECTIONS,
        )
        if not bundle.companies:
            return {}
        share_dir = self.settings.data_dir / "share"
        suffix = f"-{ticker.strip().upper()}" if ticker else "-all-companies"
        md_path = share_dir / f"latest{suffix}.md"
        txt_path = share_dir / f"latest{suffix}.txt"
        write_share_export(bundle, md_path, fmt="md")
        write_share_export(bundle, txt_path, fmt="txt")
        return {"markdown": str(md_path), "text": str(txt_path)}

    def close(self) -> None:
        if self.summarizer:
            self.summarizer.close()

    def _metadata_poll_snapshot(self) -> datetime:
        """Return the latest boundary this run can safely prove complete."""

        return datetime.now(ZoneInfo(self.settings.app_timezone))

    def _append_error(self, errors: list[dict[str, str]], lock: threading.Lock, payload: dict[str, str]) -> None:
        with lock:
            errors.append(payload)

    def _successful_coverage_from_reports(
        self,
        *,
        scope_key: str,
        ticker: str | None,
    ) -> list[CoverageRange]:
        """Recover only intervals that old local reports actually prove.

        v0.15.4 no-op reports are deliberately excluded: their requested start
        may be earlier than the single high watermark and is therefore the bug
        this migration is repairing. Reducer/refiner reports are not metadata
        polls either. A bare legacy watermark never supplies a start boundary.
        """
        report_timezone = ZoneInfo(self.settings.app_timezone)

        def report_datetime(value: Any) -> datetime:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=report_timezone)

        expected_ticker = ticker.strip().upper() if ticker else None
        candidates: list[CoverageRange] = []
        paths = [self.settings.data_dir / "last_run.json"]
        runs_root = self.settings.data_dir / "runs"
        if runs_root.exists():
            paths.extend(runs_root.glob("*/report.json"))
        for path in paths:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("status") != "completed" or payload.get("scrape_complete") is not True:
                continue
            if payload.get("metadata_noop") is True:
                continue
            if str(payload.get("mode") or "").strip().lower() in {"reduce_cached", "refine_financials"}:
                continue
            if payload.get("keyword_filter") or payload.get("max_announcements") is not None:
                continue
            report_ticker = str(payload.get("ticker_filter") or "").strip().upper() or None
            if report_ticker != expected_ticker:
                continue

            diagnostics = payload.get("metadata_diagnostics") or {}
            reported_scope = diagnostics.get("scope_key") if isinstance(diagnostics, dict) else None
            if reported_scope and str(reported_scope) != scope_key:
                continue
            if not reported_scope and scope_key.partition(":")[0] != "stocks":
                # Reports predating explicit scope diagnostics used the stock
                # endpoint by default. They cannot prove non-stock coverage.
                continue

            report_poll_snapshot: datetime | None = None
            try:
                report_poll_snapshot = report_datetime(
                    payload.get("metadata_poll_snapshot") or payload["run_started_at"]
                )
            except (KeyError, TypeError, ValueError):
                pass

            explicit_ranges = payload.get("metadata_coverage_added")
            if explicit_ranges is not None:
                for item in explicit_ranges:
                    try:
                        explicit_end = report_datetime(item["end_at"])
                        if report_poll_snapshot is not None:
                            explicit_end = min(explicit_end, report_poll_snapshot)
                        candidates.append(CoverageRange(
                            report_datetime(item["start_at"]),
                            explicit_end,
                        ))
                    except (KeyError, TypeError, ValueError):
                        continue
                continue

            try:
                report_end = report_datetime(payload["end_at"])
                report_start = report_datetime(payload["start_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if report_poll_snapshot is not None:
                report_end = min(report_end, report_poll_snapshot)
            mode = str(payload.get("metadata_mode") or "").strip().lower()
            if mode == "incremental":
                before = payload.get("metadata_watermark_before") or {}
                legacy_anchor = before.get("last_successful_poll_end") if isinstance(before, dict) else None
                if legacy_anchor:
                    try:
                        report_start = max(
                            report_start,
                            report_datetime(legacy_anchor) + timedelta(microseconds=1),
                        )
                    except (TypeError, ValueError):
                        continue
                else:
                    try:
                        report_start = report_datetime(
                            payload.get("metadata_effective_start") or payload["start_at"]
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            try:
                candidates.append(CoverageRange(report_start, report_end))
            except ValueError:
                continue
        return normalize_coverage_ranges(candidates)

    def _latest_successful_poll_end_from_reports(self, ticker: str | None) -> datetime | None:
        """Backward-compatible wrapper for callers that still need a high end."""

        scope_key = f"stocks:{(ticker or 'ALL').strip().upper()}"
        ranges = self._successful_coverage_from_reports(scope_key=scope_key, ticker=ticker)
        return max((item.end for item in ranges), default=None)

    def _export_company_checkpoint(self, ticker: str) -> None:
        lock = self._company_export_locks[ticker]
        with lock:
            self.db.export_company(ticker, self.settings.data_dir / "companies" / ticker)

    @staticmethod
    def _assert_company_isolation(ticker: str, records: list[dict[str, Any]]) -> None:
        expected = ticker.strip().upper()
        observed: set[str] = set()
        for record in records:
            summary = record.get("summary") or {}
            value = str(summary.get("ticker") or "").strip().upper()
            if value:
                observed.add(value)
        foreign = observed - {expected}
        if foreign:
            raise ValueError(f"company isolation violation for {expected}: announcement summaries contain {sorted(foreign)}")

    def _document_profile(self, title: str) -> str:
        return "public_expose" if disclosure_class(title) == "public_expose" else "general"

    def _document_prompt_version(self, title: str) -> str:
        if not self.summarizer:
            return "legacy-document"
        if self._document_profile(title) == "public_expose":
            return getattr(self.summarizer, "public_expose_document_prompt_version", getattr(self.summarizer, "document_prompt_version", "legacy-document"))
        return getattr(self.summarizer, "document_prompt_version", "legacy-document")

    def _document_profile_supported(self) -> bool:
        if not self.summarizer:
            return False
        try:
            params = inspect.signature(self.summarizer.summarize_document).parameters
            return "document_profile" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            return False

    def _document_job(self, document: PreparedAttachment, *, announcement_id: str, ticker: str, title: str) -> dict[str, Any]:
        if not self.summarizer:
            raise RuntimeError("LLM scheduler invoked without a summarizer")
        text = document.text_path.read_text(encoding="utf-8", errors="replace")
        label = f"{ticker} document summary {document.filename}"
        financial_chunk_chars = min(self.settings.llm_chunk_chars, 22_000) if is_financial_report_announcement(title) else None
        kwargs: dict[str, Any] = {
            "ticker": ticker, "filename": document.filename, "text": text,
            "stream": False if self.settings.llm_concurrency > 1 else None,
            "source_url": document.url, "announcement_id": announcement_id,
            "document_profile": self._document_profile(title),
        }
        if financial_chunk_chars is not None:
            kwargs["chunk_chars"] = financial_chunk_chars
        if not self._document_profile_supported():
            kwargs.pop("document_profile", None)
        if self.observer:
            with self.observer.timed("llm-document", label, announcement_id=announcement_id, filename=document.filename, characters=len(text), scheduler="global"):
                summary = self.summarizer.summarize_document(**kwargs)
        else:
            summary = self.summarizer.summarize_document(**kwargs)
        self.db.save_document_summary(document.url, ticker, summary, self.summarizer.model, self._document_prompt_version(title))
        if self.observer:
            self.observer.event("llm-document", "document summary stored", ticker=ticker, announcement_id=announcement_id, filename=document.filename, attachment_url=document.url, chunk_count=summary.get("chunk_count"), summary_preview=str(summary.get("summary") or "")[:180])
        return summary

    def _queue_announcement_reducer(self, scheduler: GlobalLLMScheduler, plan: AnnouncementPlan, errors: list[dict[str, str]], errors_lock: threading.Lock) -> None:
        if not self.summarizer:
            return
        with plan.lock:
            if plan.reducer_scheduled:
                return
            plan.reducer_scheduled = True
            document_updates = plan.document_updates
        cached = self.db.get_announcement_summary(plan.announcement_id, model=self.summarizer.model, prompt_version=getattr(self.summarizer, "announcement_prompt_version", "legacy-announcement"))
        cached_valid = self.summarizer.is_valid_announcement_summary(cached)
        if cached is not None and not cached_valid:
            self.db.delete_announcement_summary(plan.announcement_id)
            if self.observer:
                self.observer.event("cache", "invalid announcement summary removed", level="WARNING", always=True, ticker=plan.ticker, id=plan.announcement_id)
        if cached_valid and document_updates == 0:
            if self.observer:
                self.observer.event("cache", "announcement summary cache hit", ticker=plan.ticker, id=plan.announcement_id)
            try:
                self._export_company_checkpoint(plan.ticker)
            except Exception as exc:
                self._append_error(errors, errors_lock, {"id": plan.announcement_id, "stage": "checkpoint-export", "error": str(exc)})
            return

        def reducer() -> dict[str, Any]:
            announcement, documents = self.db.announcement_with_documents(plan.announcement_id, model=self.summarizer.model, prompt_version=self._document_prompt_version(plan.title))
            db_ticker = str(announcement.get("ticker") or "").strip().upper()
            if db_ticker != plan.ticker:
                raise ValueError(f"announcement isolation violation: expected {plan.ticker}, database returned {db_ticker or '<blank>'}")
            label = f"{plan.ticker} announcement summary {plan.title[:80]}"
            if self.observer:
                with self.observer.timed("llm-announcement", label, announcement_id=plan.announcement_id, documents=len(documents), scheduler="global"):
                    summary = self.summarizer.summarize_announcement(announcement=announcement, documents=documents, stream=False if self.settings.llm_concurrency > 1 else None)
            else:
                summary = self.summarizer.summarize_announcement(announcement=announcement, documents=documents, stream=False if self.settings.llm_concurrency > 1 else None)
            actual = str(summary.get("ticker") or "").strip().upper()
            if actual != plan.ticker:
                raise ValueError(f"announcement summary ticker violation: expected {plan.ticker}, got {actual or '<blank>'}")
            self.db.save_announcement_summary(
                plan.announcement_id, plan.ticker, summary, self.summarizer.model,
                getattr(self.summarizer, "announcement_prompt_version", "legacy-announcement"),
                analysis_mode="full", triage=plan.triage or None,
            )
            self._export_company_checkpoint(plan.ticker)
            if self.observer:
                self.observer.event("llm-announcement", "announcement summary stored", ticker=plan.ticker, id=plan.announcement_id, summary_preview=str(summary.get("executive_summary") or "")[:220])
            return summary

        def reducer_done(_value: Any | None, error: BaseException | None) -> None:
            if error is None:
                return
            self._append_error(errors, errors_lock, {"id": plan.announcement_id, "stage": "announcement-summary", "error": str(error)})
            if self.observer:
                self.observer.event("announcement", "announcement reducer failed; document checkpoints were preserved", level="ERROR", always=True, ticker=plan.ticker, id=plan.announcement_id, error=str(error))

        scheduler.submit(ScheduledJob(job_id=f"announcement:{plan.announcement_id}", group_key=plan.announcement_id, stage="announcement", ticker=plan.ticker, announcement_id=plan.announcement_id, func=reducer, on_complete=reducer_done))

    def _queue_routine_reducer(
        self,
        scheduler: GlobalLLMScheduler,
        plan: AnnouncementPlan,
        errors: list[dict[str, str]],
        errors_lock: threading.Lock,
    ) -> None:
        if not self.summarizer or not hasattr(self.summarizer, "summarize_routine_announcement"):
            plan.analysis_mode = "full"
            self._queue_llm_plan(scheduler, plan, errors, errors_lock)
            return
        with plan.lock:
            if plan.reducer_scheduled:
                return
            plan.reducer_scheduled = True
        cached = self.db.get_announcement_summary(
            plan.announcement_id, model=self.summarizer.model,
            prompt_version=getattr(self.summarizer, "announcement_prompt_version", "legacy-announcement"),
        )
        if self.summarizer.is_valid_announcement_summary(cached):
            if self.observer:
                self.observer.event("cache", "routine announcement summary cache hit", ticker=plan.ticker, id=plan.announcement_id)
            return
        if cached is not None:
            self.db.delete_announcement_summary(plan.announcement_id)

        def reducer() -> dict[str, Any]:
            announcement, _documents = self.db.announcement_with_documents(plan.announcement_id)
            db_ticker = str(announcement.get("ticker") or "").strip().upper()
            if db_ticker != plan.ticker:
                raise ValueError(f"routine announcement isolation violation: expected {plan.ticker}, database returned {db_ticker or '<blank>'}")
            raw_documents = []
            for document in plan.prepared:
                raw_documents.append({
                    "filename": document.filename,
                    "url": document.url,
                    "text": document.text_path.read_text(encoding="utf-8", errors="replace"),
                })
            label = f"{plan.ticker} routine direct summary {plan.title[:72]}"
            if self.observer:
                with self.observer.timed(
                    "llm-routine", label, announcement_id=plan.announcement_id,
                    documents=len(raw_documents), characters=sum(len(d["text"]) for d in raw_documents),
                    scheduler="global",
                ):
                    summary = self.summarizer.summarize_routine_announcement(
                        announcement=announcement, raw_documents=raw_documents, triage=plan.triage,
                        stream=False if self.settings.llm_concurrency > 1 else None,
                    )
            else:
                summary = self.summarizer.summarize_routine_announcement(
                    announcement=announcement, raw_documents=raw_documents, triage=plan.triage,
                    stream=False if self.settings.llm_concurrency > 1 else None,
                )
            self.db.save_announcement_summary(
                plan.announcement_id, plan.ticker, summary, self.summarizer.model,
                getattr(self.summarizer, "announcement_prompt_version", "legacy-announcement"),
                analysis_mode="routine_direct", triage=plan.triage,
            )
            self._export_company_checkpoint(plan.ticker)
            if self.observer:
                self.observer.event(
                    "llm-routine", "routine direct announcement summary stored",
                    ticker=plan.ticker, id=plan.announcement_id, documents=len(raw_documents),
                    summary_preview=str(summary.get("executive_summary") or "")[:220],
                )
            return summary

        def reducer_done(_value: Any | None, error: BaseException | None) -> None:
            if error is None:
                return
            self._append_error(errors, errors_lock, {"id": plan.announcement_id, "stage": "routine-announcement-summary", "error": str(error)})
            if self.observer:
                self.observer.event(
                    "llm-routine", "routine direct reducer failed; raw extraction remains recoverable",
                    level="ERROR", always=True, ticker=plan.ticker, id=plan.announcement_id, error=str(error),
                )

        scheduler.submit(ScheduledJob(
            job_id=f"routine:{plan.announcement_id}", group_key=plan.announcement_id,
            stage="announcement", ticker=plan.ticker, announcement_id=plan.announcement_id,
            func=reducer, on_complete=reducer_done,
        ))

    def _queue_llm_plan(self, scheduler: GlobalLLMScheduler, plan: AnnouncementPlan, errors: list[dict[str, str]], errors_lock: threading.Lock) -> None:
        if not self.summarizer:
            return
        if plan.analysis_mode == "routine_direct":
            self._queue_routine_reducer(scheduler, plan, errors, errors_lock)
            return
        pending: list[PreparedAttachment] = []
        for document in plan.prepared:
            cached_summary = self.db.get_document_summary(document.url, model=self.summarizer.model, prompt_version=self._document_prompt_version(plan.title))
            if self.summarizer.is_valid_document_summary(cached_summary):
                if self.observer:
                    self.observer.event("cache", "document summary cache hit", ticker=plan.ticker, announcement_id=plan.announcement_id, filename=document.filename)
            else:
                pending.append(document)
        with plan.lock:
            plan.pending_documents = len(pending)
        if self.observer:
            self.observer.event("scheduler", "announcement registered with global LLM scheduler", always=True, ticker=plan.ticker, announcement_id=plan.announcement_id, documents=len(plan.prepared), pending_document_summaries=len(pending), global_limit=scheduler.max_workers, per_announcement_limit=scheduler.max_per_group)
        if not pending:
            self._queue_announcement_reducer(scheduler, plan, errors, errors_lock)
            return

        def make_done(document: PreparedAttachment):
            def done(_value: Any | None, error: BaseException | None) -> None:
                with plan.lock:
                    if error is None:
                        plan.document_updates += 1
                    else:
                        plan.document_failures += 1
                    plan.pending_documents -= 1
                    should_reduce = plan.pending_documents == 0
                if error is not None:
                    self._append_error(errors, errors_lock, {"id": plan.announcement_id, "filename": document.filename, "stage": "document-summary", "error": str(error)})
                    if self.observer:
                        self.observer.event("llm-document", "document summary failed", level="ERROR", always=True, ticker=plan.ticker, announcement_id=plan.announcement_id, filename=document.filename, error=str(error))
                if should_reduce:
                    self._queue_announcement_reducer(scheduler, plan, errors, errors_lock)
            return done

        for document in pending:
            scheduler.submit(ScheduledJob(job_id=f"document:{plan.announcement_id}:{document.url}", group_key=plan.announcement_id, stage="document", ticker=plan.ticker, announcement_id=plan.announcement_id, func=lambda doc=document: self._document_job(doc, announcement_id=plan.announcement_id, ticker=plan.ticker, title=plan.title), on_complete=make_done(document)))

    def _complete_preparation_if_ready(
        self,
        plan: PreparationPlan,
        scheduler: GlobalLLMScheduler | None,
        errors: list[dict[str, str]],
        errors_lock: threading.Lock,
    ) -> bool:
        with plan.lock:
            if not plan.preparation_closed or plan.pending_extractions or plan.downstream_queued:
                return False
            plan.downstream_queued = True
            prepared = list(plan.prepared)
            attachment_task = plan.attachment_task
            extraction_failures = plan.extraction_failures

        if getattr(self.settings, "attachment_dedup_enabled", True) and len(prepared) > 1:
            evidence: list[AttachmentEvidence] = []
            by_url = {document.url: document for document in prepared}
            for document in prepared:
                row = self.db.attachment_state(document.url)
                evidence.append(AttachmentEvidence(
                    url=document.url, filename=document.filename,
                    text=document.text_path.read_text(encoding="utf-8", errors="replace"),
                    sha256=str(row["sha256"] or "") if row is not None else None,
                    is_attachment=bool(row["is_attachment"]) if row is not None else True,
                ))
            duplicate_decisions = deduplicate_attachments(
                evidence,
                near_threshold=getattr(self.settings, "attachment_near_duplicate_threshold", 0.985),
            )
            kept_urls = {decision.url for decision in duplicate_decisions if decision.keep}
            suppressed = [decision for decision in duplicate_decisions if not decision.keep]
            if suppressed:
                for decision in suppressed:
                    self.db.update_attachment_selection(
                        decision.url, selected_for_analysis=False, selection_reason=decision.reason,
                        selection_category=decision.category, duplicate_of_url=decision.duplicate_of_url,
                    )
                    if self.observer:
                        self.observer.event(
                            "dedup", "duplicate attachment suppressed after extraction",
                            ticker=plan.ticker, announcement_id=plan.announcement_id,
                            filename=by_url[decision.url].filename, category=decision.category,
                            duplicate_of_url=decision.duplicate_of_url, similarity=decision.similarity,
                            reason=decision.reason,
                        )
                prepared = [document for document in prepared if document.url in kept_urls]
                self.db.delete_announcement_summary(plan.announcement_id)
                with self._phase3_lock:
                    self._phase3_metrics["dedup_suppressed"] += len(suppressed)

        analysis_mode = "full"
        triage_payload: dict[str, Any] = {}
        if getattr(self.settings, "routine_triage_enabled", True) and self.summarizer is not None:
            routine_evidence: list[RoutineEvidence] = []
            for document in prepared:
                row = self.db.attachment_state(document.url)
                routine_evidence.append(RoutineEvidence(
                    filename=document.filename,
                    text=document.text_path.read_text(encoding="utf-8", errors="replace"),
                    extraction_method=str(row["extraction_method"] or "") if row is not None else None,
                ))
            triage = evaluate_routine_disclosure(
                plan.title, routine_evidence,
                max_characters=getattr(self.settings, "routine_triage_max_chars", 70_000),
                ownership_delta_threshold_pct=getattr(self.settings, "routine_ownership_delta_threshold_pct", 0.10),
            )
            if extraction_failures and triage.mode == "routine_direct":
                triage_payload = {**triage.as_dict(), "mode": "full", "reason": "selected source extraction failed; conservative full pipeline retained", "signals": [*triage.signals, "extraction-failure"]}
                analysis_mode = "full"
            else:
                triage_payload = triage.as_dict()
                analysis_mode = triage.mode
            if triage_payload.get("reason") != "not a supported routine disclosure":
                with self._phase3_lock:
                    key = "routine_direct" if analysis_mode == "routine_direct" else "routine_full"
                    self._phase3_metrics[key] += 1
                if self.observer:
                    self.observer.event(
                        "triage", "routine disclosure routing decision", always=True,
                        ticker=plan.ticker, announcement_id=plan.announcement_id, title=plan.title,
                        **triage_payload,
                    )

        if self.observer:
            self.observer.finish_task(attachment_task)
            self.observer.event(
                "extract-queue",
                "announcement preparation ready for downstream analysis",
                ticker=plan.ticker, announcement_id=plan.announcement_id,
                prepared_documents=len(prepared),
            )
        if scheduler is not None:
            self._queue_llm_plan(
                scheduler,
                AnnouncementPlan(
                    announcement_id=plan.announcement_id, ticker=plan.ticker, title=plan.title,
                    announced_at=plan.announced_at, prepared=prepared,
                    analysis_mode=analysis_mode, triage=triage_payload,
                ),
                errors, errors_lock,
            )
        else:
            try:
                self._export_company_checkpoint(plan.ticker)
            except Exception as exc:
                self._append_error(errors, errors_lock, {"id": plan.announcement_id, "stage": "checkpoint-export", "error": str(exc)})
        return True

    def run(self, *, start_at: datetime, end_at: datetime, ticker: str | None = None, keyword: str = "", max_announcements: int | None = None, attachment_policy: str = "smart", instrument_scope: str = "stocks", metadata_mode: str = "incremental") -> dict[str, Any]:
        if end_at < start_at:
            raise ValueError("end_at must be greater than or equal to start_at")
        metadata_mode = (metadata_mode or "incremental").strip().lower()
        if metadata_mode not in {"incremental", "historical_audit"}:
            raise ValueError("metadata_mode must be incremental or historical_audit")
        metadata_poll_snapshot = self._metadata_poll_snapshot()
        if start_at.tzinfo is None and metadata_poll_snapshot.tzinfo is not None:
            metadata_poll_snapshot = metadata_poll_snapshot.replace(tzinfo=None)
        pollable_end = min(end_at, metadata_poll_snapshot)
        metadata_pollable_range = (
            CoverageRange(start_at, pollable_end) if start_at <= pollable_end else None
        )
        metadata_deferred_ranges = (
            [CoverageRange(max(start_at, metadata_poll_snapshot), end_at)]
            if end_at > metadata_poll_snapshot else []
        )
        processed = 0
        with self._phase3_lock:
            self._phase3_metrics = {"routine_direct": 0, "routine_full": 0, "dedup_suppressed": 0}
        skipped_outside_window = 0
        tickers: set[str] = set()
        errors: list[dict[str, str]] = []
        errors_lock = threading.Lock()
        browser_trace_path: str | None = None
        metadata_items: list[tuple[dict[str, Any], datetime]] = []
        raw_items: list[dict[str, Any]] = []
        metadata_total_collected = 0
        metadata_reported_total = 0
        metadata_diagnostics: dict[str, Any] = {}
        stock_master_count: int | None = None
        stock_master_filtered = 0
        metadata_cached_duplicates = 0
        metadata_effective_start = start_at
        metadata_scope_key = f"{instrument_scope}:{(ticker or 'ALL').strip().upper()}"
        metadata_watermark_before: dict[str, Any] | None = None
        metadata_baseline_source: str | None = None
        metadata_coverage_before: list[CoverageRange] = []
        metadata_coverage_after: list[CoverageRange] = []
        metadata_missing_ranges: list[CoverageRange] = []
        metadata_query_ranges: list[CoverageRange] = []
        metadata_coverage_added: list[CoverageRange] = []
        incremental_watermark_enabled = metadata_mode == "incremental" and not keyword.strip() and max_announcements is None
        scheduler_metrics: dict[str, Any] = {}
        extraction_metrics: dict[str, Any] = {}
        per_announcement_limit = min(self.settings.llm_concurrency, self.settings.llm_per_announcement_concurrency)
        if self.observer:
            self.observer.event("run", "scrape started", start_at=start_at.isoformat(), end_at=end_at.isoformat(), ticker=ticker.upper() if ticker else "ALL", keyword=keyword or None, max_announcements=max_announcements, attachment_policy=attachment_policy, instrument_scope=instrument_scope, metadata_mode=metadata_mode, llm_enabled=self.summarizer is not None, llm_concurrency=self.settings.llm_concurrency, llm_per_announcement_concurrency=per_announcement_limit, llm_document_chunk_concurrency=self.settings.llm_document_chunk_concurrency, scheduler="global-phase-4" if self.summarizer else None, adaptive_provider=getattr(self.settings, "llm_adaptive_concurrency", True), routine_triage=getattr(self.settings, "routine_triage_enabled", True), attachment_dedup=getattr(self.settings, "attachment_dedup_enabled", True), progress=self.observer.show_progress, stream_summary=self.observer.stream_llm, trace_browser=self.observer.trace_browser, prompt_profile=(self.summarizer.prompts.profile_name if self.summarizer else None), prompt_hashes=(self.summarizer.prompts.hashes if self.summarizer else None))
            if self.summarizer and self.observer.stream_llm and self.settings.llm_concurrency > 1:
                self.observer.event("scheduler", "raw LLM streaming disabled for global concurrent jobs to prevent interleaved JSON", level="WARNING", always=True, global_limit=self.settings.llm_concurrency)
        scrape_complete = True
        scrape_error: str | None = None
        metadata_noop = False
        metadata_skipped_trusted_history = 0
        if incremental_watermark_enabled:
            metadata_watermark_before = self.db.scrape_watermark(metadata_scope_key)

            # v0.15.3 briefly persisted an artificial empty-profile watermark at
            # the requested start. That is not a successful poll, so discard it.
            if (
                metadata_watermark_before
                and metadata_watermark_before.get("baseline_source") == "empty_archive"
                and not metadata_watermark_before.get("last_seen_announcement_at")
            ):
                self.db.delete_scrape_watermark(metadata_scope_key)
                metadata_watermark_before = None

            # Additive v0.15.5 migration. Completed metadata reports can prove a
            # start and an end; the legacy high watermark alone cannot.
            recovered_ranges = self._successful_coverage_from_reports(
                scope_key=metadata_scope_key,
                ticker=ticker,
            )
            persisted_before_migration = [
                CoverageRange(
                    datetime.fromisoformat(str(row["covered_start"])),
                    datetime.fromisoformat(str(row["covered_end"])),
                )
                for row in self.db.scrape_coverage(metadata_scope_key)
            ]
            for recovered in recovered_ranges:
                if any(
                    existing.start <= recovered.start and recovered.end <= existing.end
                    for existing in persisted_before_migration
                ):
                    continue
                self.db.save_scrape_coverage(
                    metadata_scope_key,
                    covered_start=recovered.start.isoformat(),
                    covered_end=recovered.end.isoformat(),
                    baseline_source="run-history",
                )
                persisted_before_migration = normalize_coverage_ranges([
                    *persisted_before_migration,
                    recovered,
                ])

            coverage_rows = self.db.scrape_coverage(metadata_scope_key)
            for row in coverage_rows:
                try:
                    metadata_coverage_before.append(CoverageRange(
                        datetime.fromisoformat(str(row["covered_start"])),
                        datetime.fromisoformat(str(row["covered_end"])),
                    ))
                except (TypeError, ValueError):
                    continue
            metadata_coverage_before = normalize_coverage_ranges(metadata_coverage_before)
            metadata_coverage_after = list(metadata_coverage_before)

            latest_existing = self.db.latest_announcement_at(ticker=ticker)
            proven_end = max((item.end for item in metadata_coverage_before), default=None)
            legacy_end: datetime | None = None
            if metadata_watermark_before and metadata_watermark_before.get("last_successful_poll_end"):
                try:
                    legacy_end = datetime.fromisoformat(str(metadata_watermark_before["last_successful_poll_end"]))
                except (TypeError, ValueError):
                    legacy_end = None
            if proven_end is not None and (legacy_end is None or proven_end > legacy_end):
                self.db.save_scrape_watermark(
                    metadata_scope_key,
                    last_successful_poll_end=proven_end.isoformat(),
                    last_seen_announcement_at=(
                        metadata_watermark_before.get("last_seen_announcement_at")
                        if metadata_watermark_before else latest_existing
                    ),
                    baseline_source="run-history",
                )
                metadata_watermark_before = self.db.scrape_watermark(metadata_scope_key)

            if coverage_rows:
                sources = {str(row.get("baseline_source") or "coverage") for row in coverage_rows}
                metadata_baseline_source = next(iter(sources)) if len(sources) == 1 else "coverage-ranges"
            elif metadata_watermark_before:
                metadata_baseline_source = "legacy-watermark-without-start"
            elif latest_existing:
                metadata_baseline_source = "existing-archive-unverified"
            else:
                metadata_baseline_source = "empty_archive"

            metadata_missing_ranges = (
                subtract_coverage(metadata_pollable_range, metadata_coverage_before)
                if metadata_pollable_range is not None else []
            )
            if not metadata_missing_ranges and not metadata_deferred_ranges:
                metadata_noop = True
                metadata_effective_start = end_at
                if self.observer:
                    self.observer.event(
                        "idx", "incremental window already covered; no metadata request needed", always=True,
                        scope_key=metadata_scope_key, requested_start=start_at.isoformat(),
                        requested_end=end_at.isoformat(),
                        coverage_ranges=[item.as_dict() for item in metadata_coverage_before],
                        baseline_source=metadata_baseline_source,
                    )
            elif metadata_missing_ranges:
                overlap = timedelta(days=float(getattr(self.settings, "idx_incremental_overlap_days", 1.0)))
                metadata_query_ranges = [
                    CoverageRange(max(start_at, gap.start - overlap), gap.end)
                    for gap in metadata_missing_ranges
                ]
                metadata_effective_start = min(item.start for item in metadata_query_ranges)
                if self.observer:
                    self.observer.event(
                        "idx", "incremental coverage gaps planned", always=True,
                        scope_key=metadata_scope_key,
                        requested_start=start_at.isoformat(), requested_end=end_at.isoformat(),
                        missing_ranges=[item.as_dict() for item in metadata_missing_ranges],
                        query_ranges=[item.as_dict() for item in metadata_query_ranges],
                        coverage_ranges=[item.as_dict() for item in metadata_coverage_before],
                        overlap_days=float(getattr(self.settings, "idx_incremental_overlap_days", 1.0)),
                        baseline_source=metadata_baseline_source,
                    )
                if not metadata_coverage_before and self.observer:
                    self.observer.event(
                        "idx", "no proven coverage yet; requested interval will be checked",
                        always=True, scope_key=metadata_scope_key, requested_start=start_at.isoformat(),
                        baseline_source=metadata_baseline_source,
                    )
            else:
                metadata_effective_start = metadata_poll_snapshot
        elif metadata_mode == "historical_audit":
            metadata_watermark_before = self.db.scrape_watermark(metadata_scope_key)
            coverage_rows = self.db.scrape_coverage(metadata_scope_key)
            metadata_coverage_before = [
                CoverageRange(
                    datetime.fromisoformat(str(row["covered_start"])),
                    datetime.fromisoformat(str(row["covered_end"])),
                )
                for row in coverage_rows
            ]
            metadata_coverage_after = list(metadata_coverage_before)
            metadata_missing_ranges = [metadata_pollable_range] if metadata_pollable_range is not None else []
            metadata_query_ranges = list(metadata_missing_ranges)
            metadata_baseline_source = "historical-audit"
            if self.observer:
                self.observer.event(
                    "idx", "historical audit mode enabled; full requested range will be completeness-checked",
                    level="WARNING", always=True, requested_start=start_at.isoformat(),
                    end_at=end_at.isoformat(), poll_snapshot=metadata_poll_snapshot.isoformat(),
                )
        else:
            metadata_missing_ranges = [metadata_pollable_range] if metadata_pollable_range is not None else []
            metadata_query_ranges = list(metadata_missing_ranges)
        if metadata_deferred_ranges and self.observer:
            self.observer.event(
                "idx", "future portion deferred until it can be polled", always=True,
                scope_key=metadata_scope_key,
                poll_snapshot=metadata_poll_snapshot.isoformat(),
                deferred_ranges=[item.as_dict() for item in metadata_deferred_ranges],
            )
        idx = IDXClient(self.settings, observer=self.observer)
        downloader = AttachmentDownloader(self.settings, browser_transport_factory=idx.browser_transport, observer=self.observer)
        per_ticker_llm_limit = self.settings.llm_concurrency if ticker else min(2, self.settings.llm_concurrency)
        scheduler = GlobalLLMScheduler(
            max_workers=self.settings.llm_concurrency,
            max_per_group=per_announcement_limit,
            max_per_ticker=per_ticker_llm_limit,
            observer=self.observer,
        ) if self.summarizer else None
        extraction_workers = self.settings.extraction_workers
        extraction_backlog = max(extraction_workers, self.settings.extraction_queue_size)
        extraction_per_ticker_limit = extraction_workers if ticker else min(2, extraction_workers)
        extraction_scheduler = BoundedExtractionScheduler(
            max_workers=extraction_workers, max_inflight=extraction_backlog,
            max_per_ticker=extraction_per_ticker_limit, observer=self.observer,
        )
        try:
            try:
                if metadata_noop:
                    raw_items = []
                    metadata_diagnostics = {
                        "complete": True,
                        "strategy": "incremental-noop",
                        "collected": 0,
                        "reported_total": 0,
                        "mode": metadata_mode,
                        "requested_start": start_at.isoformat(),
                        "effective_start": metadata_effective_start.isoformat(),
                        "scope_key": metadata_scope_key,
                        "watermark_before": metadata_watermark_before,
                        "baseline_source": metadata_baseline_source,
                        "coverage_before": [item.as_dict() for item in metadata_coverage_before],
                        "missing_ranges": [],
                        "query_ranges": [],
                        "noop": True,
                    }
                elif not metadata_query_ranges:
                    raw_items = []
                    metadata_diagnostics = {
                        "complete": True,
                        "strategy": "future-deferred",
                        "collected": 0,
                        "reported_total": 0,
                        "mode": metadata_mode,
                        "requested_start": start_at.isoformat(),
                        "effective_start": metadata_effective_start.isoformat(),
                        "scope_key": metadata_scope_key,
                        "watermark_before": metadata_watermark_before,
                        "baseline_source": metadata_baseline_source,
                        "coverage_before": [item.as_dict() for item in metadata_coverage_before],
                        "missing_ranges": [],
                        "query_ranges": [],
                        "deferred_ranges": [item.as_dict() for item in metadata_deferred_ranges],
                        "noop": False,
                    }
                elif hasattr(idx, "collect_announcements"):
                    range_diagnostics: list[dict[str, Any]] = []
                    raw_by_id: dict[str, dict[str, Any]] = {}
                    anonymous_items: list[dict[str, Any]] = []
                    for index, query_range in enumerate(metadata_query_ranges):
                        logical_gap = metadata_missing_ranges[index]
                        try:
                            collected, range_diag = idx.collect_announcements(
                                query_range.start, query_range.end, ticker=ticker, keyword=keyword,
                                emiten_type="s" if instrument_scope == "stocks" else "*",
                                allow_ticker_fallback=(metadata_mode == "historical_audit"),
                            )
                        except Exception as exc:
                            collected = []
                            range_diag = {
                                "complete": False,
                                "strategy": "collection-error",
                                "collected": 0,
                                "reported_total": 0,
                                "reason": str(exc),
                            }
                        range_diag = {
                            **range_diag,
                            "logical_gap": logical_gap.as_dict(),
                            "query_range": query_range.as_dict(),
                        }
                        range_diagnostics.append(range_diag)
                        for collected_item in collected:
                            collected_id = str((collected_item.get("pengumuman") or {}).get("Id2") or "")
                            if collected_id:
                                raw_by_id[collected_id] = collected_item
                            else:
                                anonymous_items.append(collected_item)
                    raw_items = [*raw_by_id.values(), *anonymous_items]
                    reported_total = 0
                    for item in range_diagnostics:
                        primary = item.get("primary") if isinstance(item, dict) else None
                        reported_total += int(
                            ((primary or {}).get("reported_total") if isinstance(primary, dict) else 0)
                            or item.get("reported_total")
                            or item.get("collected")
                            or 0
                        )
                    metadata_diagnostics = {
                        "complete": all(bool(item.get("complete", False)) for item in range_diagnostics),
                        "strategy": (
                            str(range_diagnostics[0].get("strategy") or "coverage-gap")
                            if len(range_diagnostics) == 1 else "coverage-gaps"
                        ),
                        "collected": len(raw_items),
                        "reported_total": reported_total,
                        "ranges": range_diagnostics,
                        "mode": metadata_mode,
                        "requested_start": start_at.isoformat(),
                        "effective_start": metadata_effective_start.isoformat(),
                        "scope_key": metadata_scope_key,
                        "watermark_before": metadata_watermark_before,
                        "baseline_source": metadata_baseline_source,
                        "coverage_before": [item.as_dict() for item in metadata_coverage_before],
                        "missing_ranges": [item.as_dict() for item in metadata_missing_ranges],
                        "query_ranges": [item.as_dict() for item in metadata_query_ranges],
                        "deferred_ranges": [item.as_dict() for item in metadata_deferred_ranges],
                    }
                else:
                    raw_items = []
                    range_diagnostics = []
                    raw_ids: set[str] = set()
                    for index, query_range in enumerate(metadata_query_ranges):
                        legacy_error = None
                        range_count = 0
                        try:
                            for legacy_item in idx.iter_announcements(
                                query_range.start, query_range.end, ticker=ticker, keyword=keyword,
                                emiten_type="s" if instrument_scope == "stocks" else "*",
                            ):
                                legacy_id = str((legacy_item.get("pengumuman") or {}).get("Id2") or "")
                                if not legacy_id or legacy_id not in raw_ids:
                                    raw_items.append(legacy_item)
                                    range_count += 1
                                    if legacy_id:
                                        raw_ids.add(legacy_id)
                        except Exception as exc:
                            legacy_error = str(exc)
                        range_diagnostics.append({
                            "complete": legacy_error is None,
                            "strategy": "legacy-iterator",
                            "collected": range_count,
                            "reason": legacy_error,
                            "logical_gap": metadata_missing_ranges[index].as_dict(),
                            "query_range": query_range.as_dict(),
                        })
                    metadata_diagnostics = {
                        "complete": all(bool(item["complete"]) for item in range_diagnostics),
                        "strategy": "legacy-iterator" if len(range_diagnostics) == 1 else "coverage-gaps",
                        "collected": len(raw_items),
                        "reported_total": len(raw_items),
                        "ranges": range_diagnostics,
                        "mode": metadata_mode,
                        "requested_start": start_at.isoformat(),
                        "effective_start": metadata_effective_start.isoformat(),
                        "scope_key": metadata_scope_key,
                        "watermark_before": metadata_watermark_before,
                        "baseline_source": metadata_baseline_source,
                        "coverage_before": [item.as_dict() for item in metadata_coverage_before],
                        "missing_ranges": [item.as_dict() for item in metadata_missing_ranges],
                        "query_ranges": [item.as_dict() for item in metadata_query_ranges],
                        "deferred_ranges": [item.as_dict() for item in metadata_deferred_ranges],
                    }
                stock_master = None if not metadata_query_ranges else (idx.stock_master_tickers() if instrument_scope == "stocks" and getattr(self.settings, "stock_master_enabled", True) and hasattr(idx, "stock_master_tickers") else None)
                stock_master_count = len(stock_master) if stock_master else None
                for item in raw_items:
                    p = item.get("pengumuman") or {}
                    company_code = str(p.get("Kode_Emiten") or "").strip().upper()
                    announcement_id_candidate = str(p.get("Id2") or "")
                    try:
                        announced = parse_idx_datetime(str(p["TglPengumuman"]), self.settings.app_timezone)
                    except Exception as exc:
                        self._append_error(errors, errors_lock, {"id": str(p.get("Id2")), "stage": "timestamp", "error": f"bad timestamp: {exc}"})
                        if self.observer:
                            self.observer.event("announcement", "invalid IDX timestamp", level="ERROR", always=True, id=str(p.get("Id2")), error=str(exc))
                        continue

                    if not (start_at <= announced <= end_at):
                        skipped_outside_window += 1
                        continue
                    # IDX filters are calendar-day granular, so a query for one
                    # gap can return rows from a neighboring covered block. Skip
                    # only timestamps proven covered, never every row below a
                    # single high watermark (which would lose backward gaps).
                    if incremental_watermark_enabled and coverage_contains(metadata_coverage_before, announced):
                        metadata_skipped_trusted_history += 1
                        continue

                    if metadata_mode == "incremental" and announcement_id_candidate:
                        cached_ready = False
                        if self.summarizer is not None:
                            cached_summary = self.db.get_announcement_summary(
                                announcement_id_candidate, model=self.summarizer.model,
                                prompt_version=getattr(self.summarizer, "announcement_prompt_version", "legacy-announcement"),
                            )
                            cached_ready = self.summarizer.is_valid_announcement_summary(cached_summary)
                        else:
                            cached_ready = self.db.announcement_exists(announcement_id_candidate)
                        if cached_ready:
                            metadata_cached_duplicates += 1
                            if self.observer:
                                self.observer.event(
                                    "cache", "incremental announcement already complete; skipped",
                                    ticker=company_code or None, id=announcement_id_candidate,
                                )
                            continue
                    if stock_master and company_code and company_code not in stock_master:
                        stock_master_filtered += 1
                        if self.observer:
                            self.observer.event(
                                "stock-master", "announcement excluded by listed-stock allowlist",
                                ticker=company_code, id=str(p.get("Id2") or ""),
                            )
                        continue
                    metadata_items.append((item, announced))
                if not metadata_diagnostics.get("complete", False):
                    scrape_complete = False
                    scrape_error = "IDX metadata collection remained incomplete"
                    self._append_error(errors, errors_lock, {"id": "run", "stage": "metadata-completeness", "error": scrape_error})
                    if self.observer:
                        self.observer.event(
                            "idx", "metadata collection incomplete",
                            level="ERROR", always=True, diagnostics=metadata_diagnostics,
                        )
            except Exception as exc:
                scrape_complete = False
                scrape_error = str(exc)
                self._append_error(errors, errors_lock, {"id": "run", "stage": "metadata", "error": scrape_error})
                if self.observer:
                    self.observer.event("run", "metadata collection interrupted; processing collected metadata", level="ERROR", always=True, error=scrape_error, collected_announcements=len(metadata_items))
            metadata_total_collected = len(metadata_items)
            primary_diag = metadata_diagnostics.get("primary") if isinstance(metadata_diagnostics, dict) else None
            metadata_reported_total = int(
                ((primary_diag or {}).get("reported_total") if isinstance(primary_diag, dict) else 0)
                or metadata_diagnostics.get("reported_total")
                or len(raw_items)
                or 0
            )
            if max_announcements is not None:
                metadata_items = metadata_items[:max_announcements]
            if self.observer:
                self.observer.event(
                    "idx", "metadata collection complete" if scrape_complete else "metadata collection partial", always=True,
                    collected_announcements=metadata_total_collected, scheduled_announcements=len(metadata_items),
                    reported_announcements=metadata_reported_total, raw_announcements=len(raw_items),
                    cached_duplicates=metadata_cached_duplicates, trusted_history_skipped=metadata_skipped_trusted_history,
                    skipped_outside_exact_window=skipped_outside_window,
                    all_pages_collected=bool(metadata_diagnostics.get("complete", scrape_complete)),
                    pagination_strategy=metadata_diagnostics.get("strategy"), metadata_mode=metadata_mode,
                    effective_start=metadata_effective_start.isoformat(), metadata_noop=metadata_noop,
                    stock_master_tickers=stock_master_count, stock_master_filtered=stock_master_filtered,
                )
            announcement_task = self.observer.start_task(f"Preparing announcements • {ticker.upper() if ticker else 'ALL'}", total=len(metadata_items), kind="items") if self.observer else None
            try:
                for item, announced in metadata_items:
                    p = item.get("pengumuman") or {}
                    announcement_id = str(p.get("Id2") or "")
                    company = str(p.get("Kode_Emiten") or "UNKNOWN").strip().upper()
                    title = str(p.get("JudulPengumuman") or "")
                    try:
                        announcement_id, company = self.db.upsert_announcement(item, announced.isoformat())
                        tickers.add(company)
                        attachments = item.get("attachments") or []
                        decisions = classify_attachments(title, attachments, policy=attachment_policy)
                        selected_count = sum(1 for d in decisions if d.selected)
                        selection_changed = False
                        for decision in decisions:
                            url = str(decision.attachment.get("FullSavePath") or "")
                            previous = self.db.attachment_state(url) if url else None
                            if previous is not None and bool(previous["selected_for_analysis"]) != decision.selected:
                                selection_changed = True
                            self.db.upsert_attachment(announcement_id, decision.attachment, selected_for_analysis=decision.selected, selection_reason=decision.reason, selection_category=decision.category)
                        if selection_changed:
                            self.db.delete_announcement_summary(announcement_id)
                        if self.observer:
                            self.observer.event("announcement", "preparing announcement", ticker=company, id=announcement_id, announced_at=announced.isoformat(), title=title, attachments=len(attachments), selected_attachments=selected_count, skipped_attachments=len(decisions)-selected_count, attachment_policy=attachment_policy)
                        attachment_task = self.observer.start_task(f"Attachments {company} • {title[:55]}", total=len(decisions), kind="items") if self.observer else None
                        prep_plan = PreparationPlan(
                            announcement_id=announcement_id, ticker=company, title=title,
                            announced_at=announced.isoformat(), attachment_task=attachment_task,
                            selected_sources=selected_count,
                        )
                        for decision in decisions:
                            if not decision.selected:
                                if self.observer:
                                    self.observer.event("attachment-filter", "attachment skipped before download", ticker=company, announcement_id=announcement_id, filename=decision.filename, category=decision.category, reason=decision.reason)
                                    self.observer.update_task(attachment_task, advance=1)
                                continue
                            if self.observer:
                                self.observer.event("attachment-filter", "attachment selected for analysis", ticker=company, announcement_id=announcement_id, filename=decision.filename, category=decision.category, reason=decision.reason)
                            local = self._download_or_cached_attachment(
                                downloader, announcement_id=announcement_id, ticker=company, attachment=decision.attachment,
                            )
                            if isinstance(local, PreparedAttachment):
                                with prep_plan.lock:
                                    prep_plan.prepared.append(local)
                                if self.observer:
                                    self.observer.update_task(attachment_task, advance=1)
                                continue
                            if isinstance(local, DownloadedAttachment):
                                with prep_plan.lock:
                                    prep_plan.pending_extractions += 1

                                def extraction_done(
                                    value: Any | None, error: BaseException | None,
                                    *, plan: PreparationPlan = prep_plan, filename: str = decision.filename,
                                ) -> None:
                                    if error is not None:
                                        self._append_error(errors, errors_lock, {
                                            "id": plan.announcement_id, "filename": filename,
                                            "stage": "extraction", "error": str(error),
                                        })
                                    with plan.lock:
                                        if isinstance(value, PreparedAttachment):
                                            plan.prepared.append(value)
                                        else:
                                            plan.extraction_failures += 1
                                        plan.pending_extractions -= 1
                                    if self.observer:
                                        self.observer.update_task(plan.attachment_task, advance=1)
                                    self._complete_preparation_if_ready(plan, scheduler, errors, errors_lock)

                                extraction_scheduler.submit(ExtractionJob(
                                    job_id=f"extract:{announcement_id}:{local.url}", ticker=company,
                                    announcement_id=announcement_id,
                                    func=lambda downloaded=local, aid=announcement_id, tick=company: self._extract_downloaded_attachment(
                                        downloaded, announcement_id=aid, ticker=tick,
                                    ),
                                    on_complete=extraction_done,
                                ))
                                continue
                            if self.observer:
                                self.observer.update_task(attachment_task, advance=1)
                        with prep_plan.lock:
                            prep_plan.preparation_closed = True
                        self._complete_preparation_if_ready(prep_plan, scheduler, errors, errors_lock)
                    except Exception as exc:
                        self._append_error(errors, errors_lock, {"id": announcement_id, "stage": "announcement-prepare", "error": str(exc)})
                        if self.observer:
                            self.observer.event("announcement", "announcement preparation failed; completed checkpoints were preserved", level="ERROR", always=True, ticker=company, id=announcement_id, error=str(exc))
                    finally:
                        processed += 1
                        if self.observer:
                            self.observer.update_task(announcement_task, advance=1)
            finally:
                if self.observer:
                    self.observer.finish_task(announcement_task, completed=processed)
        except Exception as exc:
            scrape_complete = False
            scrape_error = str(exc)
            self._append_error(errors, errors_lock, {"id": "run", "stage": "scrape", "error": scrape_error})
            if self.observer:
                self.observer.event("run", "scrape interrupted; queued LLM checkpoints will finish before recovery export", level="ERROR", always=True, error=scrape_error, processed_announcements=processed)
        finally:
            downloader.close(); idx.close()
            if idx.browser and idx.browser.trace_path:
                browser_trace_path = str(idx.browser.trace_path)
        if self.observer:
            self.observer.event("extract-queue", "waiting for bounded extraction queue", always=True, **extraction_scheduler.metrics)
        extraction_metrics = extraction_scheduler.wait()
        extraction_scheduler.close()
        if self.observer:
            self.observer.event("extract-queue", "bounded extraction queue drained", always=True, **extraction_metrics)
        if scheduler is not None:
            if self.observer:
                self.observer.event("scheduler", "waiting for global document/announcement queue", always=True, **scheduler.metrics)
            scheduler_metrics = scheduler.wait()
            if self.observer:
                self.observer.event("scheduler", "global document/announcement queue drained", always=True, **scheduler_metrics)
        company_summaries = 0
        company_count_lock = threading.Lock()
        company_stats = {
            "eligible": 0, "cache_hits": 0, "promoted_single": 0,
            "rebuilding": 0, "skipped_no_evidence": 0,
        }
        company_task = self.observer.start_task("Building company-window summaries", total=len(tickers), kind="items") if self.observer and self.summarizer else None
        if self.summarizer and scheduler is not None:
            company_prompt_version = getattr(self.summarizer, "company_prompt_version", "legacy-company")
            announcement_prompt_version = getattr(self.summarizer, "announcement_prompt_version", "legacy-announcement")
            for company in sorted(tickers):
                records = self.db.company_announcement_summaries(
                    company, start_at.isoformat(), end_at.isoformat(),
                    model=self.summarizer.model, prompt_version=announcement_prompt_version,
                )
                records = [r for r in records if self.summarizer.is_valid_announcement_summary(r.get("summary"))]
                if not records:
                    company_stats["skipped_no_evidence"] += 1
                    if self.observer:
                        self.observer.event("company-cache", "company skipped before scheduler; no valid announcement summaries", ticker=company)
                        self.observer.update_task(company_task, advance=1)
                    continue
                company_stats["eligible"] += 1
                self._assert_company_isolation(company, records)
                fingerprint = company_input_fingerprint(
                    ticker=company, start_at=start_at.isoformat(), end_at=end_at.isoformat(),
                    announcements=records, model=self.summarizer.model, prompt_version=company_prompt_version,
                )
                if getattr(self.settings, "company_incremental_cache_enabled", True) and self.db.company_summary_is_current(
                    company, start_at.isoformat(), end_at.isoformat(),
                    model=self.summarizer.model, prompt_version=company_prompt_version, input_fingerprint=fingerprint,
                ):
                    company_stats["cache_hits"] += 1
                    row = self.db.company_summary_record(company, start_at.isoformat(), end_at.isoformat())
                    cached_summary = json.loads(row["summary_json"]) if row else None
                    if self.observer:
                        self.observer.event(
                            "company-cache", "company summary cache hit", ticker=company,
                            input_fingerprint=fingerprint, generation_mode=(row["generation_mode"] if row else None),
                            summary=cached_summary,
                        )
                        self.observer.update_task(company_task, advance=1)
                    continue
                if len(records) == 1 and getattr(self.settings, "company_single_announcement_promotion", True):
                    summary = promote_single_announcement(
                        ticker=company, start_at=start_at.isoformat(), end_at=end_at.isoformat(), record=records[0],
                    )
                    summary["_checkpoint"] = {
                        "partial": not scrape_complete, "announcement_summaries_used": 1,
                        "scrape_error": scrape_error, "generation_mode": "single_announcement_promotion",
                    }
                    self.db.save_company_summary(
                        company, start_at.isoformat(), end_at.isoformat(), summary, self.summarizer.model, company_prompt_version,
                        input_fingerprint=fingerprint, generation_mode="single_announcement_promotion", source_announcement_count=1,
                    )
                    company_stats["promoted_single"] += 1
                    company_summaries += 1
                    try:
                        self._export_company_checkpoint(company)
                    except Exception as exc:
                        self._append_error(errors,errors_lock,{"id":company,"stage":"checkpoint-export","error":str(exc)})
                    if self.observer:
                        self.observer.event(
                            "llm-company", "single announcement promoted without extra LLM call",
                            ticker=company, announcements=1, input_fingerprint=fingerprint, summary=summary,
                        )
                        self.observer.update_task(company_task, advance=1)
                    continue

                company_stats["rebuilding"] += 1
                def company_job(company: str = company, records: list[dict[str, Any]] = records, fingerprint: str = fingerprint) -> dict[str, Any] | None:
                    label=f"{company} company window summary"
                    if self.observer:
                        with self.observer.timed("llm-company", label, announcements=len(records), partial=not scrape_complete, scheduler="global"):
                            summary=self.summarizer.summarize_company_window(ticker=company,start_at=start_at.isoformat(),end_at=end_at.isoformat(),announcements=records,stream=False if self.settings.llm_concurrency > 1 else None)
                    else:
                        summary=self.summarizer.summarize_company_window(ticker=company,start_at=start_at.isoformat(),end_at=end_at.isoformat(),announcements=records,stream=False if self.settings.llm_concurrency > 1 else None)
                    summary=dict(summary); summary["_checkpoint"]={"partial":not scrape_complete,"announcement_summaries_used":len(records),"scrape_error":scrape_error,"generation_mode":"llm","input_fingerprint":fingerprint}
                    self.db.save_company_summary(
                        company,start_at.isoformat(),end_at.isoformat(),summary,self.summarizer.model,company_prompt_version,
                        input_fingerprint=fingerprint,generation_mode="llm",source_announcement_count=len(records),
                    )
                    self._export_company_checkpoint(company)
                    try:
                        with self._share_export_lock:
                            self._refresh_share_exports(start_at.isoformat(),end_at.isoformat(),ticker)
                    except Exception as exc:
                        if self.observer:
                            self.observer.event("export","share snapshot refresh failed",level="WARNING",ticker=company,error=str(exc))
                    if self.observer:
                        self.observer.event("llm-company","company-window summary stored",ticker=company,announcements=len(records),partial=not scrape_complete,input_fingerprint=fingerprint,overview_preview=str(summary.get("overview") or "")[:220],summary=summary)
                    return summary
                def company_done(value: Any | None,error: BaseException | None,company: str=company) -> None:
                    nonlocal company_summaries
                    if error is not None:
                        self._append_error(errors,errors_lock,{"id":company,"stage":"company-summary","error":str(error)})
                        if self.observer:
                            self.observer.event("llm-company","company digest failed; announcement summaries remain recoverable",level="ERROR",always=True,ticker=company,error=str(error))
                    elif value is not None:
                        with company_count_lock:
                            company_summaries += 1
                    if self.observer:
                        self.observer.update_task(company_task,advance=1)
                scheduler.submit(ScheduledJob(job_id=f"company:{company}:{start_at.isoformat()}:{end_at.isoformat()}",group_key=f"company:{company}",stage="company",ticker=company,func=company_job,on_complete=company_done))
            if self.observer:
                self.observer.event(
                    "company-cache", "company reducer plan ready", always=True,
                    total_companies=len(tickers), **company_stats,
                )
            scheduler_metrics=scheduler.wait()
            if self.observer:
                self.observer.finish_task(company_task)
                self.observer.event("company-cache", "company reducer scope resolved", always=True, **company_stats)
            scheduler.close()
        elif scheduler is not None:
            scheduler.close()
        for company in tickers:
            try:
                self._export_company_checkpoint(company)
            except Exception as exc:
                self._append_error(errors,errors_lock,{"id":company,"stage":"checkpoint-export","error":str(exc)})
        try:
            share_exports=self._refresh_share_exports(start_at.isoformat(),end_at.isoformat(),ticker)
        except Exception as exc:
            share_exports={}
            if self.observer:
                self.observer.event("export","final share snapshot refresh failed",level="WARNING",error=str(exc))
        timing_report=self.observer.slowdown_report() if self.observer else {"total_elapsed_seconds":None,"slowest_stages":[]}
        stage_timings=self.observer.stage_timing_summary() if self.observer else {}
        provider_metrics=(self.summarizer.provider_metrics if self.summarizer and hasattr(self.summarizer, "provider_metrics") else {})
        performance_summary=build_performance_summary(
            processed_announcements=processed,
            total_elapsed_seconds=timing_report.get("total_elapsed_seconds"),
            scheduler_metrics=scheduler_metrics,
            extraction_metrics=extraction_metrics,
            provider_metrics=provider_metrics,
            phase3_metrics=dict(self._phase3_metrics),
            stage_timings=stage_timings,
        )
        performance_summary["company_cache"] = {
            **company_stats,
            "llm_calls_avoided": int(company_stats.get("cache_hits", 0)) + int(company_stats.get("promoted_single", 0)),
        }
        if self.observer:
            recommendation=performance_summary.get("recommendation") or {}
            self.observer.event(
                "observatory", "pipeline performance summary", always=True,
                bottleneck=performance_summary.get("bottleneck"),
                confidence=performance_summary.get("confidence"),
                throughput_announcements_per_minute=performance_summary.get("throughput_announcements_per_minute"),
                recommended_global_llm_slots=recommendation.get("global_llm_slots"),
                recommended_extraction_workers=recommendation.get("extraction_workers"),
                recommended_extraction_queue_size=recommendation.get("extraction_queue_size"),
                recommendation_changed=recommendation.get("changed"),
                company_cache_hits=company_stats.get("cache_hits", 0),
                company_promoted_single=company_stats.get("promoted_single", 0),
                company_llm_calls_avoided=performance_summary.get("company_cache", {}).get("llm_calls_avoided", 0),
            )
        watermark_after = metadata_watermark_before if not metadata_query_ranges else None
        watermark_advance_eligible = (not keyword.strip() and max_announcements is None and metadata_mode in {"incremental", "historical_audit"})
        if watermark_advance_eligible and scrape_complete and not errors and metadata_missing_ranges:
            latest_seen = self.db.latest_announcement_at(ticker=ticker)
            metadata_coverage_added = list(metadata_missing_ranges)
            coverage_source = "historical-audit" if metadata_mode == "historical_audit" else "runtime"
            self.db.save_scrape_coverages(
                metadata_scope_key,
                ranges=[
                    (proven_range.start.isoformat(), proven_range.end.isoformat())
                    for proven_range in metadata_coverage_added
                ],
                baseline_source=coverage_source,
            )
            metadata_coverage_after = [
                CoverageRange(
                    datetime.fromisoformat(str(row["covered_start"])),
                    datetime.fromisoformat(str(row["covered_end"])),
                )
                for row in self.db.scrape_coverage(metadata_scope_key)
            ]
            target_poll_end = max((item.end for item in metadata_coverage_after), default=end_at)
            if metadata_watermark_before and metadata_watermark_before.get("last_successful_poll_end"):
                try:
                    previous_poll_end = datetime.fromisoformat(str(metadata_watermark_before["last_successful_poll_end"]))
                    if previous_poll_end > target_poll_end:
                        target_poll_end = previous_poll_end
                except ValueError:
                    pass
            self.db.save_scrape_watermark(
                metadata_scope_key,
                last_successful_poll_end=target_poll_end.isoformat(),
                last_seen_announcement_at=latest_seen,
                baseline_source="runtime",
            )
            watermark_after = self.db.scrape_watermark(metadata_scope_key)
            if self.observer:
                self.observer.event(
                    "idx", "incremental coverage updated", always=True, scope_key=metadata_scope_key,
                    last_successful_poll_end=target_poll_end.isoformat(), last_seen_announcement_at=latest_seen,
                    coverage_added=[item.as_dict() for item in metadata_coverage_added],
                    coverage_ranges=[item.as_dict() for item in metadata_coverage_after],
                )
        elif not metadata_coverage_after:
            metadata_coverage_after = list(metadata_coverage_before)
        recovery=self.db.recovery_snapshot(start_at.isoformat(),end_at.isoformat(),ticker=ticker)
        run_status="completed" if scrape_complete and not errors else "partial"
        report={
            "run_started_at":self.observer.run_started_at if self.observer else None,"run_finished_at":self.observer.now_iso() if self.observer else None,
            "start_at":start_at.isoformat(),"end_at":end_at.isoformat(),"ticker_filter":ticker.upper() if ticker else None,"keyword_filter":keyword or None,"max_announcements":max_announcements,"status":run_status,
            "scrape_complete":scrape_complete,"scrape_error":scrape_error,"processed_announcements":processed,
            "metadata_announcements_collected":metadata_total_collected,"metadata_announcements_scheduled":len(metadata_items),"metadata_announcements_reported":metadata_reported_total,"metadata_raw_announcements":len(raw_items),"metadata_cached_duplicates":metadata_cached_duplicates,"metadata_trusted_history_skipped":metadata_skipped_trusted_history,"metadata_noop":metadata_noop,"metadata_mode":metadata_mode,"metadata_poll_snapshot":metadata_poll_snapshot.isoformat(),"metadata_effective_start":metadata_effective_start.isoformat(),"metadata_watermark_before":metadata_watermark_before,"metadata_watermark_after":watermark_after,"metadata_coverage_before":[item.as_dict() for item in metadata_coverage_before],"metadata_coverage_after":[item.as_dict() for item in metadata_coverage_after],"metadata_missing_ranges":[item.as_dict() for item in metadata_missing_ranges],"metadata_query_ranges":[item.as_dict() for item in metadata_query_ranges],"metadata_deferred_ranges":[item.as_dict() for item in metadata_deferred_ranges],"metadata_coverage_added":[item.as_dict() for item in metadata_coverage_added],"metadata_diagnostics":metadata_diagnostics,"stock_master_tickers":stock_master_count,"stock_master_filtered":stock_master_filtered,
            "companies":sorted(tickers),"company_summaries":len(self.db.company_window_summary_map(start_at.isoformat(), end_at.isoformat(), ticker=ticker, model=(self.summarizer.model if self.summarizer else None), prompt_version=(getattr(self.summarizer, "company_prompt_version", None) if self.summarizer else None))),"company_summaries_generated":company_summaries,"company_cache":company_stats,"skipped_outside_exact_window":skipped_outside_window,"errors":errors,
            "recovery":{"announcement_count":recovery["announcement_count"],"attachment_count":recovery["attachment_count"],"document_summary_count":recovery["document_summary_count"],"announcement_summary_count":recovery["announcement_summary_count"]},
            "share_exports":share_exports,
            "performance": performance_summary,
            "diagnostics":{"log_file":str(self.observer.log_file) if self.observer and self.observer.log_file else None,"browser_trace":browser_trace_path,"llm_concurrency":self.settings.llm_concurrency,"llm_per_announcement_concurrency":per_announcement_limit,"llm_document_chunk_concurrency":self.settings.llm_document_chunk_concurrency,"llm_per_ticker_concurrency":per_ticker_llm_limit,"scheduler":"global-phase-4" if self.summarizer else None,"scheduler_metrics":scheduler_metrics,"provider_metrics":provider_metrics,"phase3_metrics":dict(self._phase3_metrics),"phase4_performance":performance_summary,"routine_triage_enabled":getattr(self.settings, "routine_triage_enabled", True),"attachment_dedup_enabled":getattr(self.settings, "attachment_dedup_enabled", True),"adaptive_provider_enabled":getattr(self.settings, "llm_adaptive_concurrency", True),"extraction_workers":extraction_workers,"extraction_queue_size":extraction_backlog,"extraction_metrics":extraction_metrics,"stage_timings":stage_timings,"company_cache":company_stats,"metadata_mode":metadata_mode,"metadata_poll_snapshot":metadata_poll_snapshot.isoformat(),"metadata_effective_start":metadata_effective_start.isoformat(),"metadata_cached_duplicates":metadata_cached_duplicates,"metadata_reported_total":metadata_reported_total,"metadata_trusted_history_skipped":metadata_skipped_trusted_history,"metadata_noop":metadata_noop,"metadata_watermark_before":metadata_watermark_before,"metadata_watermark_after":watermark_after,"metadata_coverage_before":[item.as_dict() for item in metadata_coverage_before],"metadata_coverage_after":[item.as_dict() for item in metadata_coverage_after],"metadata_missing_ranges":[item.as_dict() for item in metadata_missing_ranges],"metadata_query_ranges":[item.as_dict() for item in metadata_query_ranges],"metadata_deferred_ranges":[item.as_dict() for item in metadata_deferred_ranges],"metadata_coverage_added":[item.as_dict() for item in metadata_coverage_added],"metadata_diagnostics":metadata_diagnostics,"prompt_profile":self.summarizer.prompts.profile_name if self.summarizer else None,"prompt_hashes":self.summarizer.prompts.hashes if self.summarizer else None,"prompt_file":str(self.settings.prompt_config_path) if self.summarizer else None,**timing_report},
        }
        report_path=self.settings.data_dir/"last_run.json"; report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        if self.observer:
            self.observer.event("run","scrape finished" if scrape_complete else "partial run checkpointed",status=run_status,scrape_complete=scrape_complete,processed_announcements=processed,company_summaries=company_summaries,recoverable_announcement_summaries=recovery["announcement_summary_count"],errors=len(errors),llm_concurrency=self.settings.llm_concurrency,llm_per_announcement_concurrency=per_announcement_limit,llm_document_chunk_concurrency=self.settings.llm_document_chunk_concurrency,llm_per_ticker_concurrency=per_ticker_llm_limit,scheduler="global-phase-4" if self.summarizer else None,phase3_metrics=dict(self._phase3_metrics),provider_metrics=provider_metrics,performance=performance_summary,elapsed_seconds=timing_report["total_elapsed_seconds"],report_path=str(report_path),log_file=str(self.observer.log_file) if self.observer.log_file else None,browser_trace=browser_trace_path)
        return report
