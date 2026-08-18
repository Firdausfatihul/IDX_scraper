from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    start: str = ""
    end: str = ""
    ticker: str | None = None
    format: Literal["md", "txt", "json"] = "md"
    sections: list[str] | None = None
    signals_only: bool = False
    # "exact" keeps the legacy behaviour of matching one saved window key exactly.
    # "range" selects every saved window overlapping start..end; "all" ignores both bounds.
    date_mode: Literal["exact", "range", "all"] = "exact"
    per_ticker: Literal["latest", "all"] = "latest"
    # Exact (start_at, end_at) keys of the saved windows to export. When given, these
    # replace the start/end selection so a caller can pick individual overlapping windows.
    window_keys: list[tuple[str, str]] | None = None


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
