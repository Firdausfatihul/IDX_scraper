from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .attachment_selector import classify_attachments, is_financial_report_announcement
from .config import Settings
from .db import Database
from .extractors import extract_document
from .observability import RunObserver
from .share_export import refresh_latest_share_exports
from .summarizer import OpenRouterSummarizer


class CachedFinancialRefiner:
    """Rebuild cached financial-report summaries from primary financial sources only.

    This path never contacts IDX and never redownloads an attachment. It reuses the
    announcement raw JSON, downloaded/extracted artifacts and any existing document
    summaries, then regenerates affected announcement/company reducers.
    """

    def __init__(self, settings: Settings, observer: RunObserver | None = None):
        self.settings = settings
        self.observer = observer
        self.db = Database(settings.database_path)
        self.summarizer = OpenRouterSummarizer(settings, observer=observer)

    def close(self) -> None:
        self.summarizer.close()

    def _financial_announcements(self, start_at: str, end_at: str, ticker: str | None) -> list[dict[str, Any]]:
        filters = ["announced_at BETWEEN ? AND ?"]
        params: list[Any] = [start_at, end_at]
        if ticker:
            filters.append("ticker=?")
            params.append(ticker.strip().upper())
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM announcements WHERE {' AND '.join(filters)} ORDER BY announced_at",
                params,
            ).fetchall()
        return [dict(row) for row in rows if is_financial_report_announcement(str(row["title"] or ""))]

    def _cached_attachment_candidates(
        self, announcement: dict[str, Any], raw_attachments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Merge historical raw JSON with the durable attachment table.

        Older scraper versions did not always preserve the same attachment bundle in
        ``announcements.raw_json`` that was ultimately downloaded. The attachment
        table is therefore a second source of truth for offline refinement.
        """
        merged: list[dict[str, Any]] = []
        by_url: dict[str, int] = {}
        by_name: dict[str, int] = {}

        def add(candidate: dict[str, Any]) -> None:
            item = dict(candidate)
            url = str(item.get("FullSavePath") or "").strip()
            name = str(item.get("OriginalFilename") or item.get("PDFFilename") or "").strip()
            name_key = name.casefold()

            existing_index = by_url.get(url) if url else None
            if existing_index is None and name_key:
                existing_index = by_name.get(name_key)
            if existing_index is not None:
                existing = merged[existing_index]
                # Prefer explicit raw metadata while filling missing durable fields.
                for key, value in item.items():
                    if existing.get(key) in (None, "") and value not in (None, ""):
                        existing[key] = value
                return

            index = len(merged)
            merged.append(item)
            if url:
                by_url[url] = index
            if name_key:
                by_name[name_key] = index

        for item in raw_attachments:
            if isinstance(item, dict):
                add(item)

        for row in self.db.announcement_attachments(str(announcement["id2"])):
            add({
                "FullSavePath": str(row["url"] or ""),
                "OriginalFilename": str(row["original_filename"] or "attachment"),
                "PDFFilename": str(row["original_filename"] or "attachment"),
                "IsAttachment": bool(row["is_attachment"]),
            })

        return merged

    def _selected_documents(self, announcement: dict[str, Any], decisions: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        documents: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        selected = [decision for decision in decisions if decision.selected]
        states: dict[str, Any] = {}

        # Persist every keep/skip decision first, then preflight the complete
        # selected source set. Do not spend LLM calls on a partial financial
        # bundle when a required XLSX/LK source is known but missing locally.
        for decision in decisions:
            attachment = decision.attachment
            url = str(attachment.get("FullSavePath") or "")
            if not url:
                if decision.selected:
                    failures.append({"filename": decision.filename, "error": "selected attachment URL missing from cache metadata"})
                continue
            self.db.upsert_attachment(
                str(announcement["id2"]),
                attachment,
                selected_for_analysis=decision.selected,
                selection_reason=decision.reason,
                selection_category=decision.category,
            )
            if self.observer:
                self.observer.event(
                    "attachment-filter",
                    "cached attachment selected" if decision.selected else "cached attachment excluded",
                    ticker=announcement["ticker"],
                    announcement_id=announcement["id2"],
                    filename=decision.filename,
                    category=decision.category,
                    reason=decision.reason,
                )
            if decision.selected:
                states[url] = self.db.attachment_state(url)

        for decision in selected:
            url = str(decision.attachment.get("FullSavePath") or "")
            state = states.get(url)
            if state is None:
                failures.append({"filename": decision.filename, "error": "attachment metadata missing"})
                continue
            local_path = state["local_path"]
            text_path = state["extracted_text_path"]
            local_exists = bool(local_path and Path(local_path).exists())
            text_exists = bool(text_path and Path(text_path).exists())
            if not local_exists and not text_exists:
                failures.append({
                    "filename": decision.filename,
                    "error": "selected financial source is not cached locally; run a normal smart scrape for this ticker",
                })

        if failures:
            if self.observer:
                self.observer.event(
                    "refine-financials",
                    "selected financial source set incomplete; skipping LLM for announcement",
                    level="WARNING",
                    always=True,
                    ticker=announcement["ticker"],
                    announcement_id=announcement["id2"],
                    failures=len(failures),
                )
            return [], failures

        for decision in selected:
            attachment = decision.attachment
            url = str(attachment.get("FullSavePath") or "")
            state = states[url]
            local_path = state["local_path"]
            text_path = state["extracted_text_path"]
            suffix = Path(decision.filename).suffix.casefold()

            # Always refresh spreadsheets with the v0.4.3+ trimmed-cell reader.
            # For PDFs/docs, re-extract only if historical text is absent.
            should_extract = bool(
                local_path
                and Path(local_path).exists()
                and (suffix in {".xlsx", ".xlsm"} or not text_path or not Path(text_path).exists())
            )
            if should_extract:
                result = extract_document(
                    Path(local_path),
                    str(state["content_type"] or ""),
                    self.settings,
                    self.observer,
                )
                target = (
                    Path(text_path)
                    if text_path
                    else self.settings.data_dir / "text" / str(announcement["ticker"])
                    / f"{state['sha256'] or Path(local_path).stem}.txt"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(result.text, encoding="utf-8")
                self.db.update_extraction(url, text_path=str(target), method=result.method, error=None)
                text_path = str(target)

            if not text_path or not Path(text_path).exists():
                # This should be unreachable after preflight, but preserve a hard
                # guard against partial source analysis.
                return [], [{"filename": decision.filename, "error": "cached extraction unavailable after preflight"}]

            text = Path(text_path).read_text(encoding="utf-8", errors="replace")
            summary = self.summarizer.summarize_document(
                ticker=str(announcement["ticker"]),
                filename=decision.filename,
                text=text,
                source_url=url,
                announcement_id=str(announcement["id2"]),
                chunk_chars=min(self.settings.llm_chunk_chars, 22_000),
            )
            self.db.save_document_summary(
                url,
                str(announcement["ticker"]),
                summary,
                self.summarizer.model,
                self.summarizer.document_prompt_version,
            )
            documents.append({
                "url": url,
                "filename": decision.filename,
                "extraction_error": state["extraction_error"],
                "selection_category": decision.category,
                "selection_reason": decision.reason,
                "summary": summary,
            })
        return documents, failures

    def preview(self, *, start_at: str, end_at: str, ticker: str | None = None) -> dict[str, Any]:
        """Show the exact cached keep/skip plan without any LLM call."""
        announcements = self._financial_announcements(start_at, end_at, ticker)
        items: list[dict[str, Any]] = []
        for announcement in announcements:
            raw = json.loads(str(announcement["raw_json"]))
            candidates = self._cached_attachment_candidates(
                announcement, list(raw.get("attachments") or [])
            )
            decisions = classify_attachments(
                str(announcement["title"] or ""), candidates, policy="smart"
            )
            rendered = []
            for decision in decisions:
                url = str(decision.attachment.get("FullSavePath") or "")
                state = self.db.attachment_state(url) if url else None
                rendered.append({
                    "filename": decision.filename,
                    "selected": decision.selected,
                    "category": decision.category,
                    "reason": decision.reason,
                    "cached_file": bool(state and state["local_path"] and Path(state["local_path"]).exists()),
                    "cached_text": bool(state and state["extracted_text_path"] and Path(state["extracted_text_path"]).exists()),
                })
            items.append({
                "ticker": str(announcement["ticker"]),
                "announcement_id": str(announcement["id2"]),
                "title": str(announcement["title"] or ""),
                "attachments": rendered,
            })
        return {
            "status": "preview",
            "llm_calls": 0,
            "start_at": start_at,
            "end_at": end_at,
            "ticker_filter": ticker,
            "financial_announcements": len(items),
            "announcements": items,
        }

    def refine(self, *, start_at: str, end_at: str, ticker: str | None = None) -> dict[str, Any]:
        announcements = self._financial_announcements(start_at, end_at, ticker)
        affected_tickers: set[str] = set()
        rebuilt_announcements: list[str] = []
        failures: list[dict[str, str]] = []

        if self.observer:
            self.observer.event(
                "refine-financials",
                "cached financial refinement started",
                always=True,
                announcements=len(announcements),
                ticker=ticker or "ALL",
            )

        for announcement in announcements:
            raw = json.loads(str(announcement["raw_json"]))
            attachment_candidates = self._cached_attachment_candidates(
                announcement, list(raw.get("attachments") or [])
            )
            decisions = classify_attachments(
                str(announcement["title"] or ""),
                attachment_candidates,
                policy="smart",
            )
            documents, document_failures = self._selected_documents(announcement, decisions)
            for failure in document_failures:
                failures.append({
                    "ticker": str(announcement["ticker"]),
                    "announcement_id": str(announcement["id2"]),
                    **failure,
                })
            if not documents:
                failures.append({
                    "ticker": str(announcement["ticker"]),
                    "announcement_id": str(announcement["id2"]),
                    "error": "no selected financial statement source is available locally",
                })
                continue

            payload = self.summarizer.summarize_announcement(
                announcement=announcement,
                documents=documents,
            )
            self.db.save_announcement_summary(
                str(announcement["id2"]),
                str(announcement["ticker"]),
                payload,
                self.summarizer.model,
                self.summarizer.announcement_prompt_version,
            )
            rebuilt_announcements.append(str(announcement["id2"]))
            affected_tickers.add(str(announcement["ticker"]))
            self.db.export_company(
                str(announcement["ticker"]),
                self.settings.data_dir / "companies" / str(announcement["ticker"]),
            )

        rebuilt_companies: list[str] = []
        for company in sorted(affected_tickers):
            records = self.db.company_announcement_summaries(company, start_at, end_at)
            if not records:
                continue
            payload = self.summarizer.summarize_company_window(
                ticker=company,
                start_at=start_at,
                end_at=end_at,
                announcements=records,
            )
            self.db.save_company_summary(
                company,
                start_at,
                end_at,
                payload,
                self.summarizer.model,
                self.summarizer.company_prompt_version,
            )
            self.db.export_company(company, self.settings.data_dir / "companies" / company)
            rebuilt_companies.append(company)
            refresh_latest_share_exports(
                self.settings.database_path,
                self.settings.data_dir,
                start_at=start_at,
                end_at=end_at,
            )

        if self.observer:
            self.observer.event(
                "refine-financials",
                "cached financial refinement finished",
                always=True,
                rebuilt_announcements=len(rebuilt_announcements),
                rebuilt_companies=len(rebuilt_companies),
                failures=len(failures),
            )
        return {
            "status": "completed" if not failures else "partial",
            "start_at": start_at,
            "end_at": end_at,
            "ticker_filter": ticker,
            "financial_announcements": len(announcements),
            "rebuilt_announcements": rebuilt_announcements,
            "rebuilt_companies": rebuilt_companies,
            "failed": failures,
        }
