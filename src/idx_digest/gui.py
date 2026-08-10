from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
import webbrowser
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .cached_reducer import CachedCompanyReducer
from .financial_refiner import CachedFinancialRefiner
from .audit_view import build_company_audit_view
from .db import Database
from .observability import RunObserver
from .pipeline import Pipeline
from .prompts import PROMPT_KEYS, PromptStore
from .profiles import ProfileManager
from .stock_master import StockMasterCache
from .share_export import (
    DEFAULT_SHARE_SECTIONS,
    SIGNALS_ONLY_SECTIONS,
    default_share_filename,
    load_share_bundle,
    render_bundle,
    write_share_export,
)
from .timeutils import parse_boundary


class RunRequest(BaseModel):
    mode: Literal["scrape", "reduce_cached", "refine_financials"] = "scrape"
    start: str
    end: str
    ticker: str | None = None
    keyword: str = ""
    skip_llm: bool = False
    max_announcements: int | None = Field(default=None, ge=1)
    llm_concurrency: int = Field(default=2, ge=1, le=8)
    llm_per_announcement_concurrency: int = Field(default=2, ge=1, le=8)
    llm_document_chunk_concurrency: int = Field(default=2, ge=1, le=4)
    extraction_workers: int = Field(default=3, ge=1, le=8)
    extraction_queue_size: int = Field(default=8, ge=1, le=32)
    llm_adaptive_concurrency: bool = True
    routine_triage_enabled: bool = True
    attachment_dedup_enabled: bool = True
    trace_browser: bool = True
    browser_headless: bool | None = None
    force_existing_company_summaries: bool = False
    instrument_scope: Literal["stocks", "all"] = "stocks"
    attachment_policy: Literal["smart", "all_supported"] = "smart"
    metadata_mode: Literal["incremental", "historical_audit"] = "incremental"


class PromptUpdateRequest(BaseModel):
    prompts: dict[str, str]
    profile_name: str | None = None


class PromptResetRequest(BaseModel):
    keys: list[str] | None = None


class ShareRequest(BaseModel):
    start: str
    end: str
    ticker: str | None = None
    format: Literal["md", "txt", "json"] = "md"
    sections: list[str] | None = None
    signals_only: bool = False


class CompanyDetailRequest(BaseModel):
    start: str
    end: str
    ticker: str


class ProfileCreateRequest(BaseModel):
    name: str
    description: str = ""
    copy_current_config: bool = True
    copy_current_prompts: bool = True


class ProfileStateRequest(BaseModel):
    state: dict[str, Any]


class ProfileRenameRequest(BaseModel):
    name: str
    description: str | None = None


class LibraryWindowRequest(BaseModel):
    start_at: str
    end_at: str
    ticker: str | None = None


@dataclass
class RunRecord:
    run_id: str
    request: dict[str, Any]
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    report: dict[str, Any] | None = None
    summaries: dict[str, Any] = field(default_factory=dict)
    partial_summaries: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failure: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    last_event_at: str | None = None
    storage_dir: Path | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)
    io_lock: threading.RLock = field(default_factory=threading.RLock)

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request": self.request,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "report": self.report,
            "summaries": self.summaries,
            "partial_summaries": self.partial_summaries,
            "failure": self.failure,
            "artifacts": sorted(self.artifacts),
            "last_seq": self.next_seq - 1,
            "last_event_at": self.last_event_at,
        }

    def persist(self) -> None:
        if self.storage_dir is None:
            return
        with self.io_lock:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            with self.condition:
                payload = self._snapshot_unlocked()
                payload["artifact_paths"] = dict(self.artifacts)
            target = self.storage_dir / "state.json"
            temp = self.storage_dir / ".state.json.tmp"
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(target)

    def publish(self, payload: dict[str, Any]) -> None:
        with self.condition:
            event = dict(payload)
            self.last_event_at = str(event.get("timestamp") or datetime.now().isoformat())
            if event.get("type") == "event":
                stage = event.get("stage")
                fields = event.get("fields") or {}
                ticker = str(fields.get("ticker") or "").strip().upper()
                summary = fields.get("summary")
                if ticker and isinstance(summary, dict) and stage in {"llm-company", "company-cache"}:
                    self.summaries[ticker] = summary
            event["seq"] = self.next_seq
            self.next_seq += 1
            self.events.append(event)
            if len(self.events) > 5000:
                self.events = self.events[-4000:]
            self.condition.notify_all()
        if self.storage_dir is not None:
            with self.io_lock:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                with (self.storage_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                self.persist()

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return self._snapshot_unlocked()

    def events_after(self, cursor: int) -> list[dict[str, Any]]:
        with self.condition:
            return [event for event in self.events if int(event["seq"]) > cursor]

    def wait_for_events(self, cursor: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        with self.condition:
            available = [event for event in self.events if int(event["seq"]) > cursor]
            if available:
                return available
            if self.status in {"completed", "partial", "failed", "interrupted"}:
                return []
            self.condition.wait(timeout=timeout)
            return [event for event in self.events if int(event["seq"]) > cursor]


class RunManager:
    def __init__(
        self,
        runs_root: Path | None = None,
        *,
        settings: Settings | None = None,
        profile_id: str = "main",
    ) -> None:
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._active_id: str | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._base_settings = settings or Settings()
        self.profile_id = profile_id
        self._runs_root = runs_root or (self._base_settings.data_dir / "runs")
        self._runs_root.mkdir(parents=True, exist_ok=True)
        self._load_persisted_runs()

    def settings(self) -> Settings:
        return self._base_settings

    def _load_persisted_runs(self) -> None:
        state_files = sorted(
            self._runs_root.glob("*/state.json"),
            key=lambda path: path.stat().st_mtime,
        )[-20:]
        for state_path in state_files:
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                record = RunRecord(
                    run_id=str(payload["run_id"]),
                    request=dict(payload.get("request") or {}),
                    status=str(payload.get("status") or "interrupted"),
                    created_at=str(payload.get("created_at") or ""),
                    started_at=payload.get("started_at"),
                    finished_at=payload.get("finished_at"),
                    report=payload.get("report"),
                    summaries=dict(payload.get("summaries") or {}),
                    partial_summaries=dict(payload.get("partial_summaries") or {}),
                    failure=payload.get("failure"),
                    artifacts=dict(payload.get("artifact_paths") or {}),
                    next_seq=int(payload.get("last_seq") or 0) + 1,
                    last_event_at=payload.get("last_event_at"),
                    storage_dir=state_path.parent,
                )
                event_path = state_path.parent / "events.jsonl"
                if event_path.exists():
                    record.artifacts.setdefault("stream", str(event_path))
                    lines = event_path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
                    for line in lines:
                        try:
                            record.events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                if record.status in {"queued", "running"}:
                    record.status = "interrupted"
                    record.finished_at = record.finished_at or datetime.now(
                        ZoneInfo(self.settings().app_timezone)
                    ).isoformat(timespec="milliseconds")
                    record.failure = record.failure or "GUI or computer stopped before the run completed"
                    self._refresh_recovery(record)
                    record.persist()
                self._runs[record.run_id] = record
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue

    def _reconcile_worker(self, record: RunRecord) -> None:
        thread = self._threads.get(record.run_id)
        if record.status in {"queued", "running"} and thread is not None and not thread.is_alive():
            record.status = "interrupted"
            record.finished_at = record.finished_at or datetime.now(ZoneInfo(self.settings().app_timezone)).isoformat(timespec="milliseconds")
            record.failure = record.failure or "Worker stopped before the run completed"
            self._refresh_recovery(record)
            record.persist()
            if self._active_id == record.run_id:
                self._active_id = None

    def snapshot_for(self, record: RunRecord) -> dict[str, Any]:
        with self._lock:
            self._reconcile_worker(record)
            payload = record.snapshot()
            thread = self._threads.get(record.run_id)
            payload["worker_alive"] = bool(thread and thread.is_alive())
            if record.last_event_at:
                try:
                    last = datetime.fromisoformat(record.last_event_at)
                    now = datetime.now(last.tzinfo or ZoneInfo(self.settings().app_timezone))
                    payload["stale_seconds"] = max(0.0, (now-last).total_seconds())
                except Exception:
                    payload["stale_seconds"] = None
            else:
                payload["stale_seconds"] = None
            return payload

    def active(self) -> RunRecord | None:
        with self._lock:
            if not self._active_id:
                return None
            record = self._runs.get(self._active_id)
            if record is not None:
                self._reconcile_worker(record)
            return record if record is not None and record.status in {"queued", "running"} else None

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._runs.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._runs.values())[-10:]
        return [self.snapshot_for(record) for record in reversed(records)]

    def start(self, request: RunRequest) -> RunRecord:
        with self._lock:
            active = self.active()
            if active and active.status in {"queued", "running"}:
                raise RuntimeError("A scraper run is already active")
            settings = self.settings()
            now = datetime.now(ZoneInfo(settings.app_timezone)).isoformat(timespec="milliseconds")
            run_id = uuid.uuid4().hex[:12]
            run_dir = settings.data_dir / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            record = RunRecord(
                run_id=run_id,
                request=request.model_dump(),
                created_at=now,
                storage_dir=run_dir,
            )
            (run_dir / "request.json").write_text(
                json.dumps(record.request, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            record.artifacts["request"] = str(run_dir / "request.json")
            record.artifacts["stream"] = str(run_dir / "events.jsonl")
            record.persist()
            self._runs[run_id] = record
            while len(self._runs) > 20:
                self._runs.popitem(last=False)
            self._active_id = run_id
            thread = threading.Thread(
                target=self._execute,
                args=(record, request),
                daemon=True,
                name=f"idx-gui-run-{run_id}",
            )
            self._threads[run_id] = thread
            thread.start()
            return record

    def resume(self, run_id: str) -> RunRecord:
        previous = self.get(run_id)
        if previous.status in {"queued", "running"}:
            raise RuntimeError("That run is still active")
        try:
            request = RunRequest.model_validate(previous.request)
        except Exception as exc:
            raise RuntimeError(f"Saved run request is invalid: {exc}") from exc
        return self.start(request)

    @staticmethod
    def _log_path(settings: Settings, run_id: str) -> Path:
        stamp = datetime.now(ZoneInfo(settings.app_timezone)).strftime("%Y%m%d-%H%M%S-%f%z")
        return settings.data_dir / "logs" / f"idx-gui-{run_id}-{stamp}.jsonl"

    @staticmethod
    def _boundaries(request: RunRequest, settings: Settings) -> tuple[datetime, datetime]:
        return (
            parse_boundary(request.start, settings.app_timezone, is_end=False),
            parse_boundary(request.end, settings.app_timezone, is_end=True),
        )

    @staticmethod
    def _load_summaries(
        settings: Settings,
        request: RunRequest,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any]:
        start_at, end_at = RunManager._boundaries(request, settings)
        return Database(settings.database_path).company_window_summary_map(
            start_at.isoformat(),
            end_at.isoformat(),
            ticker=(request.ticker or "").strip() or None,
            model=model,
            prompt_version=prompt_version,
        )

    @staticmethod
    def _load_partial_summaries(
        settings: Settings,
        request: RunRequest,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        start_at, end_at = RunManager._boundaries(request, settings)
        return Database(settings.database_path).partial_announcement_summaries(
            start_at.isoformat(),
            end_at.isoformat(),
            ticker=(request.ticker or "").strip() or None,
            model=model,
            prompt_version=prompt_version,
        )

    def _refresh_recovery(
        self,
        record: RunRecord,
        settings: Settings | None = None,
        request: RunRequest | None = None,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        settings = settings or self.settings()
        request = request or RunRequest.model_validate(record.request)
        try:
            start_at, end_at = self._boundaries(request, settings)
            db = Database(settings.database_path)
            record.summaries = db.company_window_summary_map(
                start_at.isoformat(),
                end_at.isoformat(),
                ticker=(request.ticker or "").strip() or None,
            )
            record.partial_summaries = db.partial_announcement_summaries(
                start_at.isoformat(),
                end_at.isoformat(),
                ticker=(request.ticker or "").strip() or None,
                model=model,
                prompt_version=prompt_version,
            )
            recovery_dir = (record.storage_dir or settings.data_dir / "runs" / record.run_id) / "recovery"
            snapshot = db.export_recovery(
                recovery_dir,
                start_at.isoformat(),
                end_at.isoformat(),
                ticker=(request.ticker or "").strip() or None,
            )
            record.artifacts["recovery"] = str(recovery_dir / "recovery.json")
            if record.report is None:
                record.report = {
                    "status": record.status,
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                    "ticker_filter": (request.ticker or "").strip().upper() or None,
                    "companies": snapshot.get("companies") or [],
                    "company_summaries": len(record.summaries),
                    "errors": ([{"stage": "interrupted", "error": record.failure}] if record.failure else []),
                    "recovery": {
                        key: snapshot[key]
                        for key in (
                            "announcement_count",
                            "attachment_count",
                            "document_summary_count",
                            "announcement_summary_count",
                        )
                    },
                    "diagnostics": {},
                }
        except Exception:
            return

    def _execute(self, record: RunRecord, request: RunRequest) -> None:
        settings = self.settings()
        updates: dict[str, Any] = {
            "llm_concurrency": request.llm_concurrency,
            "llm_per_announcement_concurrency": request.llm_per_announcement_concurrency,
            "llm_document_chunk_concurrency": request.llm_document_chunk_concurrency,
            "extraction_workers": request.extraction_workers,
            "extraction_queue_size": request.extraction_queue_size,
            "llm_adaptive_concurrency": request.llm_adaptive_concurrency,
            "routine_triage_enabled": request.routine_triage_enabled,
            "attachment_dedup_enabled": request.attachment_dedup_enabled,
        }
        if request.browser_headless is not None:
            updates["idx_browser_headless"] = request.browser_headless
        settings = settings.model_copy(update=updates)
        log_path = self._log_path(settings, record.run_id)
        observer = RunObserver(
            timezone_name=settings.app_timezone,
            verbose=False,
            trace_browser=request.trace_browser and request.mode == "scrape",
            browser_network=False,
            stream_llm=False,
            show_progress=False,
            show_cache_events=False,
            show_page_events=False,
            log_file=log_path,
            event_sink=record.publish,
            console_output=False,
        )
        pipeline: Pipeline | None = None
        reducer: CachedCompanyReducer | None = None
        refiner: CachedFinancialRefiner | None = None
        record.status = "running"
        record.started_at = observer.run_started_at
        record.persist()
        record.publish({
            "type": "run_state",
            "status": "running",
            "timestamp": observer.now_iso(),
            "mode": request.mode,
        })
        try:
            start_at, end_at = self._boundaries(request, settings)
            model: str | None = None
            company_prompt: str | None = None
            announcement_prompt: str | None = None

            if request.mode == "refine_financials":
                refiner = CachedFinancialRefiner(settings, observer=observer)
                prompt_bundle = refiner.summarizer.prompts
                model = refiner.summarizer.model
                company_prompt = refiner.summarizer.company_prompt_version
                announcement_prompt = refiner.summarizer.announcement_prompt_version
                report = refiner.refine(
                    start_at=start_at.isoformat(),
                    end_at=end_at.isoformat(),
                    ticker=(request.ticker or "").strip().upper() or None,
                )
                report["mode"] = "refine_financials"
                record.summaries = self._load_summaries(settings, request)
                record.partial_summaries = self._load_partial_summaries(settings, request)
                report["processed_announcements"] = len(report.get("rebuilt_announcements", []))
                report["company_summaries"] = len(record.summaries)
                report["companies"] = sorted(record.summaries)
                report["errors"] = report.get("failed", [])
                report["diagnostics"] = {
                    "log_file": str(log_path),
                    "browser_trace": None,
                    **observer.slowdown_report(),
                }
            elif request.mode == "reduce_cached":
                reducer = CachedCompanyReducer(settings, observer=observer)
                prompt_bundle = reducer.summarizer.prompts
                model = reducer.summarizer.model
                company_prompt = reducer.summarizer.company_prompt_version
                report = reducer.reduce(
                    start_at=start_at.isoformat(),
                    end_at=end_at.isoformat(),
                    ticker=(request.ticker or "").strip().upper() or None,
                    force=request.force_existing_company_summaries,
                )
                # Normalize reducer diagnostics to the same GUI report envelope.
                all_summaries = self._load_summaries(settings, request)
                all_partials = self._load_partial_summaries(settings, request)
                report["mode"] = "reduce_cached"
                report["processed_announcements"] = sum(len(v) for v in all_partials.values())
                report["company_summaries"] = len(all_summaries)
                report["companies"] = sorted(all_partials)
                report["errors"] = [
                    {"id": item.get("ticker"), "stage": "company-summary", "error": item.get("error")}
                    for item in report.get("failed", [])
                ]
                report["diagnostics"] = {
                    "log_file": str(log_path),
                    "browser_trace": None,
                    **observer.slowdown_report(),
                }
                record.summaries = all_summaries
                record.partial_summaries = all_partials
            else:
                pipeline = Pipeline(settings, skip_llm=request.skip_llm, observer=observer)
                if pipeline.summarizer is not None:
                    prompt_bundle = pipeline.summarizer.prompts
                    model = pipeline.summarizer.model
                    company_prompt = getattr(pipeline.summarizer, "company_prompt_version", None)
                    announcement_prompt = getattr(pipeline.summarizer, "announcement_prompt_version", None)
                else:
                    prompt_bundle = None
                report = pipeline.run(
                    start_at=start_at,
                    end_at=end_at,
                    ticker=(request.ticker or "").strip() or None,
                    keyword=request.keyword.strip(),
                    max_announcements=request.max_announcements,
                    attachment_policy=request.attachment_policy,
                    instrument_scope=request.instrument_scope,
                    metadata_mode=request.metadata_mode,
                )
                record.summaries = self._load_summaries(
                    settings, request, model=model, prompt_version=company_prompt
                )
                record.partial_summaries = self._load_partial_summaries(
                    settings, request, model=model, prompt_version=announcement_prompt
                )

            # Freeze the exact prompt profile used by either execution mode.
            if request.mode == "refine_financials":
                prompt_bundle = refiner.summarizer.prompts if refiner is not None else None
            elif request.mode == "reduce_cached":
                prompt_bundle = reducer.summarizer.prompts if reducer is not None else None
            elif pipeline is not None and pipeline.summarizer is not None:
                prompt_bundle = pipeline.summarizer.prompts
            else:
                prompt_bundle = None
            if prompt_bundle is not None:
                prompts_path = (record.storage_dir or settings.data_dir / "runs" / record.run_id) / "prompts.json"
                prompts_path.write_text(
                    json.dumps(
                        {
                            "profile_name": prompt_bundle.profile_name,
                            "hashes": prompt_bundle.hashes,
                            "prompts": prompt_bundle.prompts,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                record.artifacts["prompts"] = str(prompts_path)

            record.report = report
            run_dir = record.storage_dir or settings.data_dir / "runs" / record.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            if record.summaries:
                try:
                    share_bundle = load_share_bundle(
                        settings.database_path,
                        start_at=start_at.isoformat(),
                        end_at=end_at.isoformat(),
                        ticker=(request.ticker or "").strip().upper() or None,
                        sections=DEFAULT_SHARE_SECTIONS,
                    )
                    share_md = run_dir / "all_company_summaries.md"
                    share_txt = run_dir / "all_company_summaries.txt"
                    write_share_export(share_bundle, share_md, fmt="md")
                    write_share_export(share_bundle, share_txt, fmt="txt")
                    record.artifacts["share_md"] = str(share_md)
                    record.artifacts["share_txt"] = str(share_txt)
                    latest_suffix = f"-{(request.ticker or '').strip().upper()}" if (request.ticker or '').strip() else "-all-companies"
                    latest_dir = settings.data_dir / "share"
                    write_share_export(share_bundle, latest_dir / f"latest{latest_suffix}.md", fmt="md")
                    write_share_export(share_bundle, latest_dir / f"latest{latest_suffix}.txt", fmt="txt")
                except Exception as exc:
                    observer.event("export", "combined share export failed", level="WARNING", error=str(exc))
            report_path = run_dir / "report.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            record.artifacts["report"] = str(report_path)
            diagnostics = report.get("diagnostics") or {}
            if diagnostics.get("log_file"):
                record.artifacts["log"] = str(diagnostics["log_file"])
            if diagnostics.get("browser_trace"):
                record.artifacts["trace"] = str(diagnostics["browser_trace"])

            # Do not filter by prompt generation here. Recovery should show every
            # committed legacy and current announcement checkpoint in the window.
            self._refresh_recovery(record, settings, request)
            record.status = "partial" if report.get("status") == "partial" else "completed"
            record.finished_at = observer.now_iso()
            record.failure = report.get("scrape_error") or report.get("stop_reason")
            record.persist()
            record.publish({
                "type": "run_state",
                "status": record.status,
                "timestamp": record.finished_at,
                "report": report,
                "summaries": record.summaries,
                "partial_summaries": record.partial_summaries,
                "error": record.failure,
                "mode": request.mode,
            })
        except Exception as exc:
            record.failure = str(exc)
            record.finished_at = observer.now_iso()
            self._refresh_recovery(record, settings, request)
            record.status = "partial" if record.partial_summaries else "failed"
            if record.report is not None:
                record.report["status"] = record.status
            record.persist()
            record.publish({
                "type": "run_state",
                "status": record.status,
                "timestamp": record.finished_at,
                "error": str(exc),
                "report": record.report,
                "summaries": record.summaries,
                "partial_summaries": record.partial_summaries,
                "mode": request.mode,
            })
        finally:
            if pipeline is not None:
                pipeline.close()
            if reducer is not None:
                reducer.close()
            if refiner is not None:
                refiner.close()
            observer.close()
            record.persist()
            with self._lock:
                if self._active_id == record.run_id:
                    self._active_id = None
                self._threads.pop(record.run_id, None)
            with record.condition:
                record.condition.notify_all()



_workspace_lock = threading.RLock()
_profile_manager: ProfileManager | None = None
_manager: RunManager | None = None
_workspace_root: Path | None = None


def _workspace() -> tuple[ProfileManager, RunManager]:
    global _profile_manager, _manager, _workspace_root
    base = Settings()
    root = base.data_dir.resolve()
    with _workspace_lock:
        if _profile_manager is None or _manager is None or _workspace_root != root:
            _profile_manager = ProfileManager(base)
            active_settings = _profile_manager.settings_for()
            _manager = RunManager(settings=active_settings, profile_id=_profile_manager.active_id)
            _workspace_root = root
        return _profile_manager, _manager


def _active_settings() -> Settings:
    profile_manager, _ = _workspace()
    return profile_manager.settings_for()


def _active_manager() -> RunManager:
    return _workspace()[1]


def _replace_manager(profile_id: str) -> RunManager:
    global _manager
    profile_manager, current = _workspace()
    active = current.active()
    if active and active.status in {"queued", "running"}:
        raise RuntimeError("Finish or stop the active run before switching profiles")
    profile_manager.activate(profile_id)
    _manager = RunManager(
        settings=profile_manager.settings_for(profile_id),
        profile_id=profile_id,
    )
    return _manager


def _profile_summary(profile_manager: ProfileManager, profile_id: str) -> dict[str, Any]:
    profile = profile_manager.get(profile_id)
    settings = profile_manager.settings_for(profile_id)
    counts = Database(settings.database_path).profile_counts()
    run_count = sum(1 for _ in (settings.data_dir / "runs").glob("*/state.json"))
    return {
        **profile.as_dict(),
        "data_dir": str(settings.data_dir),
        "run_count": run_count,
        **counts,
    }


def _run_library(settings: Settings) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    state_files = sorted(
        (settings.data_dir / "runs").glob("*/state.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for state_path in state_files:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        request = dict(payload.get("request") or {})
        report = dict(payload.get("report") or {})
        output.append({
            "run_id": payload.get("run_id") or state_path.parent.name,
            "status": payload.get("status") or "unknown",
            "created_at": payload.get("created_at"),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "mode": request.get("mode") or report.get("mode") or "scrape",
            "start": request.get("start"),
            "end": request.get("end"),
            "ticker": request.get("ticker"),
            "company_summaries": report.get("company_summaries") or len(payload.get("summaries") or {}),
            "announcement_summaries": (report.get("recovery") or {}).get("announcement_summary_count"),
            "failure": payload.get("failure"),
            "last_seq": payload.get("last_seq") or 0,
            "artifacts": sorted((payload.get("artifact_paths") or {}).keys()),
        })
    return output


def _load_saved_run(settings: Settings, run_id: str) -> dict[str, Any]:
    run_dir = (settings.data_dir / "runs" / run_id).resolve()
    runs_root = (settings.data_dir / "runs").resolve()
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid run path") from exc
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise HTTPException(status_code=404, detail="Saved run not found in this profile")
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Saved run state is unreadable") from exc


app = FastAPI(
    title="IDX Disclosure Digest",
    version=__version__,
    docs_url=None,
    redoc_url=None,
)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = files("idx_digest.web").joinpath("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
def health() -> dict[str, Any]:
    profile_manager, manager = _workspace()
    settings = profile_manager.settings_for()
    active = manager.active()
    return {
        "ok": True,
        "version": __version__,
        "timezone": settings.app_timezone,
        "model": settings.openrouter_model,
        "provider": settings.openrouter_provider,
        "api_key_configured": bool(settings.openrouter_api_key),
        "active_run_id": active.run_id if active else None,
        "active_profile_id": profile_manager.active_id,
        "active_profile_name": profile_manager.active().name,
    }


@app.get("/api/profiles")
def profiles() -> dict[str, Any]:
    profile_manager, _ = _workspace()
    items = [_profile_summary(profile_manager, p.id) for p in profile_manager.list()]
    return {
        "active_profile_id": profile_manager.active_id,
        "profiles": items,
        "state": profile_manager.state(),
    }


@app.post("/api/profiles", status_code=201)
def create_profile(payload: ProfileCreateRequest) -> dict[str, Any]:
    profile_manager, manager = _workspace()
    active = manager.active()
    if active and active.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Finish or stop the active run before creating a profile")
    source = profile_manager.active_id
    try:
        created = profile_manager.create(
            payload.name,
            description=payload.description,
            copy_state_from=source if payload.copy_current_config else None,
            copy_prompts_from=source if payload.copy_current_prompts else None,
        )
        _replace_manager(created.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "active_profile_id": created.id,
        "profile": _profile_summary(profile_manager, created.id),
        "state": profile_manager.state(created.id),
    }


@app.post("/api/profiles/{profile_id}/activate")
def activate_profile(profile_id: str) -> dict[str, Any]:
    profile_manager, _ = _workspace()
    try:
        _replace_manager(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "active_profile_id": profile_id,
        "profile": _profile_summary(profile_manager, profile_id),
        "state": profile_manager.state(profile_id),
    }


@app.put("/api/profiles/{profile_id}/state")
def save_profile_state(profile_id: str, payload: ProfileStateRequest) -> dict[str, Any]:
    profile_manager, _ = _workspace()
    try:
        saved = profile_manager.save_state(profile_id, payload.state)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    return {"profile_id": profile_id, "state": saved}


@app.patch("/api/profiles/{profile_id}")
def rename_profile(profile_id: str, payload: ProfileRenameRequest) -> dict[str, Any]:
    profile_manager, _ = _workspace()
    try:
        profile = profile_manager.rename(profile_id, name=payload.name, description=payload.description)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"profile": _profile_summary(profile_manager, profile.id)}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, Any]:
    profile_manager, manager = _workspace()
    active_run = manager.active()
    if active_run and active_run.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Finish or stop the active run before deleting a profile")
    try:
        profile = profile_manager.get(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Profile not found") from exc
    if profile.legacy or profile.id == "main":
        raise HTTPException(status_code=403, detail="The Main archive profile cannot be deleted")
    try:
        if profile_manager.active_id == profile_id:
            _replace_manager("main")
        deleted = profile_manager.delete(profile_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "deleted_profile_id": deleted.id,
        "deleted_profile_name": deleted.name,
        "active_profile_id": profile_manager.active_id,
    }


@app.get("/api/config")
def config() -> dict[str, Any]:
    profile_manager, _ = _workspace()
    settings = profile_manager.settings_for()
    prompt_bundle = PromptStore(settings.prompt_config_path).load()
    return {
        "timezone": settings.app_timezone,
        "model": settings.openrouter_model,
        "provider": settings.openrouter_provider,
        "llm_concurrency": settings.llm_concurrency,
        "llm_per_announcement_concurrency": settings.llm_per_announcement_concurrency,
        "llm_document_chunk_concurrency": settings.llm_document_chunk_concurrency,
        "extraction_workers": settings.extraction_workers,
        "extraction_queue_size": settings.extraction_queue_size,
        "llm_adaptive_concurrency": settings.llm_adaptive_concurrency,
        "routine_triage_enabled": settings.routine_triage_enabled,
        "attachment_dedup_enabled": settings.attachment_dedup_enabled,
        "company_incremental_cache_enabled": settings.company_incremental_cache_enabled,
        "company_single_announcement_promotion": settings.company_single_announcement_promotion,
        "stock_master_enabled": settings.stock_master_enabled,
        "network_watchdog_enabled": settings.network_watchdog_enabled,
        "routine_ownership_delta_threshold_pct": settings.routine_ownership_delta_threshold_pct,
        "browser_headless": settings.idx_browser_headless,
        "data_dir": str(settings.data_dir),
        "api_key_configured": bool(settings.openrouter_api_key),
        "prompt_profile": prompt_bundle.profile_name,
        "prompt_hashes": prompt_bundle.hashes,
        "profile_id": profile_manager.active_id,
        "profile_name": profile_manager.active().name,
        "profile_state": profile_manager.state(),
    }


@app.get("/api/prompts")
def get_prompts() -> dict[str, Any]:
    settings = _active_settings()
    return PromptStore(settings.prompt_config_path).snapshot()


@app.put("/api/prompts")
def update_prompts(payload: PromptUpdateRequest) -> dict[str, Any]:
    manager = _active_manager()
    active = manager.active()
    if active and active.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Prompts cannot be changed during an active run")
    unknown = sorted(set(payload.prompts) - set(PROMPT_KEYS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown prompt keys: {', '.join(unknown)}")
    settings = _active_settings()
    try:
        PromptStore(settings.prompt_config_path).save(payload.prompts, profile_name=payload.profile_name)
        return PromptStore(settings.prompt_config_path).snapshot()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/prompts/reset")
def reset_prompts(payload: PromptResetRequest) -> dict[str, Any]:
    manager = _active_manager()
    active = manager.active()
    if active and active.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Prompts cannot be changed during an active run")
    settings = _active_settings()
    try:
        PromptStore(settings.prompt_config_path).reset(payload.keys)
        return PromptStore(settings.prompt_config_path).snapshot()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    return {"runs": _active_manager().recent()}


@app.get("/api/library")
def library() -> dict[str, Any]:
    profile_manager, _ = _workspace()
    settings = profile_manager.settings_for()
    db = Database(settings.database_path)
    companies = db.company_library()
    cached_master = StockMasterCache(settings.data_dir / "stock_master.json").load() if getattr(settings, "stock_master_enabled", True) else None
    if cached_master is not None and len(cached_master.tickers) < int(getattr(settings, "stock_master_min_tickers", 500)):
        cached_master = None
    hidden_nonstocks: list[str] = []
    if cached_master is not None:
        hidden_nonstocks = sorted({str(item.get("ticker") or "").upper() for item in companies if str(item.get("ticker") or "").upper() not in cached_master.tickers})
        companies = [item for item in companies if str(item.get("ticker") or "").upper() in cached_master.tickers]
    return {
        "profile_id": profile_manager.active_id,
        "profile_name": profile_manager.active().name,
        "windows": db.library_windows(),
        "companies": companies,
        "runs": _run_library(settings),
        "counts": db.profile_counts(),
        "stock_master_count": len(cached_master.tickers) if cached_master else None,
        "legacy_non_stock_hidden": hidden_nonstocks,
    }


@app.post("/api/library/window")
def library_window(payload: LibraryWindowRequest) -> dict[str, Any]:
    settings = _active_settings()
    ticker = (payload.ticker or "").strip().upper() or None
    db = Database(settings.database_path)
    summaries = db.company_window_summary_map(payload.start_at, payload.end_at, ticker=ticker)
    partials = db.partial_announcement_summaries(payload.start_at, payload.end_at, ticker=ticker)
    cached_master = StockMasterCache(settings.data_dir / "stock_master.json").load() if getattr(settings, "stock_master_enabled", True) else None
    if cached_master is not None and len(cached_master.tickers) < int(getattr(settings, "stock_master_min_tickers", 500)):
        cached_master = None
    if cached_master is not None and ticker is None:
        summaries = {k: v for k, v in summaries.items() if k in cached_master.tickers}
        partials = {k: v for k, v in partials.items() if k in cached_master.tickers}
    snapshot = db.recovery_snapshot(payload.start_at, payload.end_at, ticker=ticker)
    if not summaries and not partials:
        raise HTTPException(status_code=404, detail="No saved summaries found for this library window")
    return {
        "status": "library",
        "start_at": payload.start_at,
        "end_at": payload.end_at,
        "ticker_filter": ticker,
        "summaries": summaries,
        "partial_summaries": partials,
        "recovery": {
            key: snapshot[key]
            for key in (
                "announcement_count",
                "attachment_count",
                "document_summary_count",
                "announcement_summary_count",
                "companies",
            )
        },
    }


@app.get("/api/library/runs/{run_id}")
def library_run(run_id: str) -> dict[str, Any]:
    return _load_saved_run(_active_settings(), run_id)


@app.get("/api/library/runs/{run_id}/events")
def library_run_events(run_id: str, limit: int = 1000) -> dict[str, Any]:
    settings = _active_settings()
    _load_saved_run(settings, run_id)
    event_path = settings.data_dir / "runs" / run_id / "events.jsonl"
    if not event_path.exists():
        return {"run_id": run_id, "events": [], "total": 0}
    lines = event_path.read_text(encoding="utf-8", errors="replace").splitlines()
    limit = max(1, min(int(limit), 5000))
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"run_id": run_id, "events": events, "total": len(lines), "returned": len(events)}


def _share_bundle(payload: ShareRequest):
    settings = _active_settings()
    start_at = parse_boundary(payload.start, settings.app_timezone, is_end=False)
    end_at = parse_boundary(payload.end, settings.app_timezone, is_end=True)
    if payload.signals_only and payload.sections:
        raise HTTPException(status_code=422, detail="Use either signals_only or explicit sections")
    sections = SIGNALS_ONLY_SECTIONS if payload.signals_only else payload.sections or DEFAULT_SHARE_SECTIONS
    try:
        bundle = load_share_bundle(
            settings.database_path,
            start_at=start_at.isoformat(),
            end_at=end_at.isoformat(),
            ticker=(payload.ticker or "").strip().upper() or None,
            sections=sections,
        )
        if not bundle.companies and "T23:59" in payload.end and payload.end.count(":") == 1:
            full_day_end = parse_boundary(payload.end.split("T", 1)[0], settings.app_timezone, is_end=True)
            bundle = load_share_bundle(
                settings.database_path,
                start_at=start_at.isoformat(),
                end_at=full_day_end.isoformat(),
                ticker=(payload.ticker or "").strip().upper() or None,
                sections=sections,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not bundle.companies:
        raise HTTPException(status_code=404, detail="No saved company summaries found for this window")
    return settings, bundle


@app.post("/api/share/render")
def render_share(payload: ShareRequest) -> dict[str, Any]:
    _, bundle = _share_bundle(payload)
    text = render_bundle(bundle, payload.format)
    return {
        "text": text,
        "format": payload.format,
        "company_count": bundle.company_count,
        "sections": list(bundle.sections),
        "characters": len(text),
        "llm_calls": 0,
    }


@app.post("/api/share/export")
def export_share(payload: ShareRequest) -> FileResponse:
    settings, bundle = _share_bundle(payload)
    destination = settings.data_dir / "share" / default_share_filename(
        payload.format, ticker=bundle.ticker_filter
    )
    write_share_export(bundle, destination, fmt=payload.format)
    media_type = {
        "md": "text/markdown; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "json": "application/json",
    }[payload.format]
    return FileResponse(destination, filename=destination.name, media_type=media_type)


def _detail_boundaries(start: str, end: str, settings: Settings) -> tuple[str, str]:
    start_at = parse_boundary(start, settings.app_timezone, is_end=False)
    end_at = parse_boundary(end, settings.app_timezone, is_end=True)
    return start_at.isoformat(), end_at.isoformat()


@app.post("/api/company-detail")
def company_detail(payload: CompanyDetailRequest) -> dict[str, Any]:
    settings = _active_settings()
    start_at, end_at = _detail_boundaries(payload.start, payload.end, settings)
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=422, detail="Ticker is required")
    detail = build_company_audit_view(
        settings.database_path, settings.prompt_config_path,
        ticker=ticker, start_at=start_at, end_at=end_at,
    )
    if detail.get("company_summary") is None and "T23:59" in payload.end and payload.end.count(":") == 1:
        full_day_end = parse_boundary(payload.end.split("T", 1)[0], settings.app_timezone, is_end=True).isoformat()
        detail = build_company_audit_view(
            settings.database_path, settings.prompt_config_path,
            ticker=ticker, start_at=start_at, end_at=full_day_end,
        )
    if detail.get("company_summary") is None and not detail.get("announcements"):
        raise HTTPException(status_code=404, detail="No saved ticker checkpoints found for this window")
    return detail


@app.get("/api/llm-audit/{audit_id}")
def llm_audit_detail(audit_id: int) -> dict[str, Any]:
    row = Database(_active_settings().database_path).llm_audit(audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="LLM audit record not found")
    return row


@app.get("/api/source-file")
def source_file(url: str, kind: Literal["file", "text"] = "file") -> FileResponse:
    settings = _active_settings()
    row = Database(settings.database_path).attachment_state(url)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    stored = row["local_path"] if kind == "file" else row["extracted_text_path"]
    if not stored:
        raise HTTPException(status_code=404, detail=f"No saved {kind} artifact for this attachment")
    path = Path(str(stored)).resolve()
    data_root = settings.data_dir.resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Stored source path is outside the active profile") from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="Saved source artifact is missing on disk")
    media = row["content_type"] if kind == "file" else "text/plain; charset=utf-8"
    return FileResponse(path, filename=path.name, media_type=media or "application/octet-stream")


@app.post("/api/recovery")
def recovery_preview(payload: RunRequest) -> dict[str, Any]:
    settings = _active_settings().model_copy(update={
        "llm_concurrency": payload.llm_concurrency,
        "llm_per_announcement_concurrency": payload.llm_per_announcement_concurrency,
        "llm_document_chunk_concurrency": payload.llm_document_chunk_concurrency,
        "extraction_workers": payload.extraction_workers,
        "extraction_queue_size": payload.extraction_queue_size,
        "llm_adaptive_concurrency": payload.llm_adaptive_concurrency,
        "routine_triage_enabled": payload.routine_triage_enabled,
        "attachment_dedup_enabled": payload.attachment_dedup_enabled,
    })
    start_at, end_at = RunManager._boundaries(payload, settings)
    db = Database(settings.database_path)
    summaries = db.company_window_summary_map(
        start_at.isoformat(),
        end_at.isoformat(),
        ticker=(payload.ticker or "").strip() or None,
    )
    partials = db.partial_announcement_summaries(
        start_at.isoformat(),
        end_at.isoformat(),
        ticker=(payload.ticker or "").strip() or None,
    )
    snapshot = db.recovery_snapshot(
        start_at.isoformat(),
        end_at.isoformat(),
        ticker=(payload.ticker or "").strip() or None,
    )
    return {
        "status": "recovered",
        "summaries": summaries,
        "partial_summaries": partials,
        "recovery": {
            key: snapshot[key]
            for key in (
                "announcement_count",
                "attachment_count",
                "document_summary_count",
                "announcement_summary_count",
                "companies",
            )
        },
    }


@app.post("/api/reduce-cached", status_code=202)
def start_cached_reducer(payload: RunRequest) -> dict[str, Any]:
    if payload.skip_llm:
        raise HTTPException(status_code=422, detail="Cached reduction requires OpenRouter summaries")
    request = payload.model_copy(update={
        "mode": "reduce_cached",
        "keyword": "",
        "max_announcements": None,
        "trace_browser": False,
    })
    try:
        record = _active_manager().start(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.snapshot()


@app.post("/api/refine-financials", status_code=202)
def start_financial_refiner(payload: RunRequest) -> dict[str, Any]:
    if payload.skip_llm:
        raise HTTPException(status_code=422, detail="Financial refinement requires OpenRouter summaries")
    request = payload.model_copy(update={
        "mode": "refine_financials",
        "keyword": "",
        "max_announcements": None,
        "trace_browser": False,
        "attachment_policy": "smart",
    })
    try:
        record = _active_manager().start(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.snapshot()


@app.post("/api/runs", status_code=202)
def start_run(payload: RunRequest) -> dict[str, Any]:
    try:
        record = _active_manager().start(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _active_manager().snapshot_for(record)


@app.post("/api/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: str) -> dict[str, Any]:
    manager = _active_manager()
    try:
        resumed = manager.resume(run_id)
        return manager.snapshot_for(resumed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    manager = _active_manager()
    try:
        return manager.snapshot_for(manager.get(run_id))
    except KeyError:
        return _load_saved_run(_active_settings(), run_id)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, cursor: int = 0) -> StreamingResponse:
    manager = _active_manager()
    try:
        record = manager.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not loaded in the active profile") from exc

    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def stream():
        nonlocal cursor
        initial = record.events_after(cursor)
        for event in initial:
            cursor = int(event["seq"])
            yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        while True:
            if await request.is_disconnected():
                break
            batch = await asyncio.to_thread(record.wait_for_events, cursor, 15.0)
            if batch:
                for event in batch:
                    cursor = int(event["seq"])
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                continue
            if record.status in {"completed", "partial", "failed", "interrupted"}:
                yield f"event: done\ndata: {json.dumps(record.snapshot(), ensure_ascii=False, default=str)}\n\n"
                break
            yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/snapshot")
def save_run_snapshot(run_id: str) -> FileResponse:
    profile_manager, _ = _workspace()
    settings = profile_manager.settings_for()
    state = _load_saved_run(settings, run_id)
    run_dir = settings.data_dir / "runs" / run_id
    manifest = {
        "saved_at": datetime.now(ZoneInfo(settings.app_timezone)).isoformat(timespec="milliseconds"),
        "profile": profile_manager.active().as_dict(),
        "run_id": run_id,
        "status": state.get("status"),
        "request": state.get("request"),
        "company_summary_count": len(state.get("summaries") or {}),
        "announcement_summary_count": sum(len(v) for v in (state.get("partial_summaries") or {}).values()),
        "note": "Run files, live stream, prompt snapshot, reports, recovery data and saved summaries are archived together.",
    }
    manifest_path = run_dir / "snapshot_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    profile_state = profile_manager.state()
    (run_dir / "profile_state_snapshot.json").write_text(
        json.dumps(profile_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    destination = run_dir / f"idx-signal-desk-{run_id}-snapshot.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        included: set[Path] = set()
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path == destination:
                continue
            resolved = path.resolve()
            included.add(resolved)
            archive.write(path, arcname=path.relative_to(run_dir))
        data_root = settings.data_dir.resolve()
        for key, raw_path in sorted((state.get("artifact_paths") or {}).items()):
            if not raw_path:
                continue
            path = Path(str(raw_path)).resolve()
            try:
                path.relative_to(data_root)
            except ValueError:
                continue
            if not path.exists() or not path.is_file() or path in included or path == destination.resolve():
                continue
            archive.write(path, arcname=Path("external_artifacts") / key / path.name)
    return FileResponse(destination, filename=destination.name, media_type="application/zip")


@app.get("/api/runs/{run_id}/artifacts/{artifact}")
def artifact(run_id: str, artifact: str) -> FileResponse:
    manager = _active_manager()
    try:
        record = manager.get(run_id)
        raw_path = record.artifacts.get(artifact)
    except KeyError:
        saved = _load_saved_run(_active_settings(), run_id)
        raw_path = (saved.get("artifact_paths") or {}).get(artifact)
    if not raw_path and artifact == "stream":
        candidate = _active_settings().data_dir / "runs" / run_id / "events.jsonl"
        if candidate.exists():
            raw_path = str(candidate)
    if not raw_path:
        raise HTTPException(status_code=404, detail="Artifact not available")
    path = Path(raw_path).resolve()
    data_root = _active_settings().data_dir.resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Artifact path is outside the active profile") from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(path, filename=path.name)


def launch_gui(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    _workspace()
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
