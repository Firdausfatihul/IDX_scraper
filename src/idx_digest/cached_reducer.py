from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Database
from .incremental import company_input_fingerprint, promote_single_announcement
from .observability import RunObserver
from .share_export import refresh_latest_share_exports
from .summarizer import OpenRouterSummarizer, SummaryError


@dataclass(frozen=True)
class CompanyReduceJob:
    ticker: str
    announcements: list[dict[str, Any]]
    legacy_announcements: int = 0
    input_fingerprint: str = ""


class CachedCompanyReducer:
    """Build company-window digests only from already committed announcement summaries.

    No IDX requests, browser work, downloads, extraction, or document summarization are
    performed here. Each successful company result is committed immediately so the
    reducer is safe to rerun after a disconnect or process interruption.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        observer: RunObserver | None = None,
        summarizer: OpenRouterSummarizer | None = None,
    ) -> None:
        self.settings = settings
        self.observer = observer
        self.db = Database(settings.database_path)
        self.summarizer = summarizer or OpenRouterSummarizer(settings, observer=observer)
        self._owns_summarizer = summarizer is None

    def close(self) -> None:
        if self._owns_summarizer:
            self.summarizer.close()

    @staticmethod
    def _usable_summary(summary: Any) -> bool:
        if not isinstance(summary, dict) or not summary:
            return False
        # v0.3.7 and earlier summaries do not contain all v0.3.8+ analytical fields,
        # but they are still perfectly useful input to a company reducer.
        return bool(
            str(summary.get("executive_summary") or summary.get("summary") or "").strip()
            or summary.get("material_facts")
            or summary.get("corporate_actions")
            or summary.get("financial_figures")
        )

    @staticmethod
    def _networkish(exc: BaseException) -> bool:
        text = str(exc).lower()
        needles = (
            "openrouter request failed",
            "connecterror",
            "connection",
            "timed out",
            "timeout",
            "temporary failure",
            "network",
            "429",
            "502",
            "503",
            "504",
        )
        return isinstance(exc, SummaryError) and any(token in text for token in needles)

    def _jobs(
        self,
        start_at: str,
        end_at: str,
        *,
        ticker: str | None,
        force: bool,
    ) -> tuple[list[CompanyReduceJob], list[str], list[str]]:
        grouped = self.db.partial_announcement_summaries(start_at, end_at, ticker=ticker)
        jobs: list[CompanyReduceJob] = []
        skipped_existing: list[str] = []
        skipped_invalid: list[str] = []

        for raw_ticker, records in sorted(grouped.items()):
            company = str(raw_ticker or "").strip().upper()
            if not company:
                skipped_invalid.append("<blank ticker>")
                continue
            usable: list[dict[str, Any]] = []
            legacy = 0
            for record in records:
                summary = record.get("summary")
                if not self._usable_summary(summary):
                    continue
                if not self.summarizer.is_valid_announcement_summary(summary):
                    legacy += 1
                usable.append(
                    {
                        "announcement_id": record.get("announcement_id"),
                        "announced_at": record.get("announced_at"),
                        "title": record.get("title"),
                        "summary": summary,
                        "source_model": record.get("model"),
                        "source_prompt_version": record.get("prompt_version"),
                    }
                )
            if usable:
                fingerprint = company_input_fingerprint(
                    ticker=company, start_at=start_at, end_at=end_at, announcements=usable,
                    model=self.summarizer.model,
                    prompt_version=getattr(self.summarizer, "company_prompt_version", "legacy-company"),
                )
                if not force and self.db.company_summary_is_current(
                    company, start_at, end_at, model=self.summarizer.model,
                    prompt_version=getattr(self.summarizer, "company_prompt_version", "legacy-company"),
                    input_fingerprint=fingerprint,
                ):
                    skipped_existing.append(company)
                    continue
                jobs.append(CompanyReduceJob(company, usable, legacy, fingerprint))
            else:
                skipped_invalid.append(company)

        return jobs, skipped_existing, skipped_invalid

    def reduce(
        self,
        *,
        start_at: str,
        end_at: str,
        ticker: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        jobs, skipped_existing, skipped_invalid = self._jobs(
            start_at, end_at, ticker=ticker, force=force
        )
        workers = min(self.settings.llm_concurrency, len(jobs)) if jobs else 0
        errors: list[dict[str, Any]] = []
        completed: list[str] = []
        promoted: list[str] = []
        stopped_early = False
        stop_reason: str | None = None

        task = (
            self.observer.start_task(
                f"Cached company reducers • workers={workers}", total=len(jobs), kind="items"
            )
            if self.observer
            else None
        )
        if self.observer:
            self.observer.event(
                "reduce-cached",
                "cached company reduction started",
                always=True,
                candidates=len(jobs),
                workers=workers,
                skipped_existing=len(skipped_existing),
                skipped_invalid=len(skipped_invalid),
                force=force,
            )

        if not jobs:
            if self.observer:
                self.observer.finish_task(task)
            try:
                share_exports = refresh_latest_share_exports(
                    self.settings.database_path,
                    self.settings.data_dir,
                    start_at=start_at,
                    end_at=end_at,
                    ticker=ticker,
                )
            except Exception as exc:
                share_exports = {}
                if self.observer:
                    self.observer.event("export", "share snapshot refresh failed", level="WARNING", error=str(exc))
            return {
                "status": "completed",
                "start_at": start_at,
                "end_at": end_at,
                "ticker_filter": ticker,
                "candidate_companies": 0,
                "completed_companies": 0,
                "skipped_existing": skipped_existing,
                "skipped_invalid": skipped_invalid,
                "failed": [],
                "remaining_companies": [],
                "stopped_early": False,
                "stop_reason": None,
                "llm_concurrency": self.settings.llm_concurrency,
                "share_exports": share_exports,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }

        def run_job(job: CompanyReduceJob) -> tuple[CompanyReduceJob, dict[str, Any]]:
            if (not force) and len(job.announcements) == 1 and getattr(self.settings, "company_single_announcement_promotion", True):
                if self.observer:
                    self.observer.event(
                        "company-cache", "single cached announcement promoted without extra LLM call",
                        ticker=job.ticker, announcements=1,
                    )
                return job, promote_single_announcement(
                    ticker=job.ticker, start_at=start_at, end_at=end_at, record=job.announcements[0],
                )
            if self.observer:
                self.observer.event(
                    "llm-company",
                    f"START {job.ticker} cached company summary",
                    ticker=job.ticker,
                    announcements=len(job.announcements),
                    legacy_announcements=job.legacy_announcements,
                )
            result = self.summarizer.summarize_company_window(
                ticker=job.ticker,
                start_at=start_at,
                end_at=end_at,
                announcements=job.announcements,
            )
            return job, result

        next_index = 0
        network_failures_since_success = 0
        in_flight: dict[Future[tuple[CompanyReduceJob, dict[str, Any]]], CompanyReduceJob] = {}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="idx-company-reducer") as executor:
            while next_index < len(jobs) and len(in_flight) < workers:
                job = jobs[next_index]
                next_index += 1
                in_flight[executor.submit(run_job, job)] = job

            while in_flight:
                done, _ = wait(set(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    job = in_flight.pop(future)
                    try:
                        finished_job, summary = future.result()
                        summary = dict(summary)
                        summary["_checkpoint"] = {
                            "source": "cached-announcement-reducer",
                            "announcement_summaries_used": len(finished_job.announcements),
                            "legacy_announcement_summaries_used": finished_job.legacy_announcements,
                        }
                        self.db.save_company_summary(
                            finished_job.ticker,
                            start_at,
                            end_at,
                            summary,
                            self.summarizer.model,
                            getattr(self.summarizer, "company_prompt_version", "legacy-company"),
                            input_fingerprint=finished_job.input_fingerprint,
                            generation_mode=("single_announcement_promotion" if (not force) and len(finished_job.announcements) == 1 and getattr(self.settings, "company_single_announcement_promotion", True) else "llm"),
                            source_announcement_count=len(finished_job.announcements),
                        )
                        # Export immediately. A power/network failure after this point cannot
                        # erase the completed company from either SQLite or data/companies.
                        self.db.export_company(
                            finished_job.ticker,
                            self.settings.data_dir / "companies" / finished_job.ticker,
                        )
                        completed.append(finished_job.ticker)
                        generation_mode = "single_announcement_promotion" if (not force) and len(finished_job.announcements) == 1 and getattr(self.settings, "company_single_announcement_promotion", True) else "llm"
                        if generation_mode == "single_announcement_promotion":
                            promoted.append(finished_job.ticker)
                        try:
                            refresh_latest_share_exports(
                                self.settings.database_path,
                                self.settings.data_dir,
                                start_at=start_at,
                                end_at=end_at,
                                ticker=ticker,
                            )
                        except Exception as exc:
                            if self.observer:
                                self.observer.event(
                                    "export",
                                    "share snapshot refresh failed",
                                    level="WARNING",
                                    ticker=finished_job.ticker,
                                    error=str(exc),
                                )
                        network_failures_since_success = 0
                        if self.observer:
                            self.observer.event(
                                "llm-company",
                                "cached company summary stored",
                                always=True,
                                ticker=finished_job.ticker,
                                announcements=len(finished_job.announcements),
                                legacy_announcements=finished_job.legacy_announcements,
                                generation_mode=generation_mode,
                                summary=summary,
                            )
                    except Exception as exc:
                        errors.append({"ticker": job.ticker, "error": str(exc)})
                        if self._networkish(exc):
                            network_failures_since_success += 1
                        if self.observer:
                            self.observer.event(
                                "llm-company",
                                "cached company reducer failed",
                                level="ERROR",
                                always=True,
                                ticker=job.ticker,
                                error=str(exc),
                            )
                    finally:
                        if self.observer:
                            self.observer.update_task(task, advance=1)

                # If every worker lane has failed with a network/API style error since
                # the last success, stop feeding new work. Rerunning later resumes from
                # the companies that were already committed.
                if network_failures_since_success >= max(1, workers):
                    stopped_early = True
                    stop_reason = "OpenRouter/network circuit breaker opened after all active worker lanes failed"
                    if self.observer:
                        self.observer.event(
                            "reduce-cached",
                            "network circuit breaker opened; unscheduled companies left for resume",
                            level="WARNING",
                            always=True,
                            failures=network_failures_since_success,
                            workers=workers,
                        )
                    for pending in list(in_flight):
                        pending.cancel()
                    # Let already-running futures finish, but do not schedule more.
                    next_index = len(jobs)

                while not stopped_early and next_index < len(jobs) and len(in_flight) < workers:
                    job = jobs[next_index]
                    next_index += 1
                    in_flight[executor.submit(run_job, job)] = job

        if self.observer:
            self.observer.finish_task(task)

        completed_set = set(completed)
        failed_set = {item["ticker"] for item in errors}
        remaining = [
            job.ticker
            for job in jobs
            if job.ticker not in completed_set and job.ticker not in failed_set
        ]
        status = "completed" if not errors and not remaining else "partial"
        try:
            share_exports = refresh_latest_share_exports(
                self.settings.database_path,
                self.settings.data_dir,
                start_at=start_at,
                end_at=end_at,
                ticker=ticker,
            )
        except Exception as exc:
            share_exports = {}
            if self.observer:
                self.observer.event("export", "final share snapshot refresh failed", level="WARNING", error=str(exc))
        report = {
            "status": status,
            "start_at": start_at,
            "end_at": end_at,
            "ticker_filter": ticker,
            "candidate_companies": len(jobs),
            "completed_companies": len(completed),
            "completed_tickers": completed,
            "promoted_single": promoted,
            "llm_generated_companies": len(completed) - len(promoted),
            "skipped_existing": skipped_existing,
            "skipped_invalid": skipped_invalid,
            "failed": errors,
            "remaining_companies": remaining,
            "stopped_early": stopped_early,
            "stop_reason": stop_reason,
            "llm_concurrency": self.settings.llm_concurrency,
            "share_exports": share_exports,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        if self.observer:
            self.observer.event(
                "reduce-cached",
                "cached company reduction finished",
                always=True,
                status=status,
                completed=len(completed),
                failed=len(errors),
                remaining=len(remaining),
                elapsed_seconds=report["elapsed_seconds"],
            )
        return report
