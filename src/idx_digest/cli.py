from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import typer

from .cached_reducer import CachedCompanyReducer
from .config import Settings
from .db import Database
from .financial_refiner import CachedFinancialRefiner
from .observability import RunObserver
from .pipeline import Pipeline
from .prompts import PromptStore
from .share_export import (
    DEFAULT_SHARE_SECTIONS,
    SIGNALS_ONLY_SECTIONS,
    default_share_filename,
    load_share_bundle,
    write_share_export,
)
from .timeutils import parse_boundary

app = typer.Typer(no_args_is_help=True, help="Scrape and summarize IDX disclosures per company.")


def _default_log_path(settings: Settings) -> Path:
    stamp = datetime.now(ZoneInfo(settings.app_timezone)).strftime("%Y%m%d-%H%M%S-%f%z")
    return settings.data_dir / "logs" / f"idx-digest-{stamp}.jsonl"


@app.command()
def run(
    start: str = typer.Option(..., help="ISO date/datetime, e.g. 2026-08-05T21:00:00+07:00"),
    end: str = typer.Option(..., help="ISO date/datetime. A date-only value includes the full day."),
    ticker: str | None = typer.Option(None, help="One IDX ticker, e.g. ANTM. Omit for all companies."),
    keyword: str = typer.Option("", help="Optional IDX announcement keyword."),
    skip_llm: bool = typer.Option(False, help="Download and extract only; do not call OpenRouter."),
    max_announcements: int | None = typer.Option(None, min=1, help="Safety cap for testing."),
    llm_concurrency: int | None = typer.Option(
        None,
        "--llm-concurrency",
        min=1,
        max=8,
        help="Global OpenRouter worker slots across tickers/stages. Overrides LLM_CONCURRENCY.",
    ),
    llm_per_announcement: int | None = typer.Option(
        None, "--llm-per-announcement", min=1, max=8,
        help="Maximum concurrent LLM jobs from one announcement. Overrides LLM_PER_ANNOUNCEMENT_CONCURRENCY.",
    ),
    llm_document_chunks: int | None = typer.Option(
        None, "--llm-document-chunks", min=1, max=4,
        help="Maximum concurrent chunks inside one long document. Overrides LLM_DOCUMENT_CHUNK_CONCURRENCY.",
    ),
    extraction_workers: int | None = typer.Option(
        None, "--extraction-workers", min=1, max=8,
        help="Background local extraction workers. Browser downloads remain single-owner.",
    ),
    extraction_queue_size: int | None = typer.Option(
        None, "--extraction-queue-size", min=1, max=32,
        help="Maximum active+queued extraction jobs before browser/download backpressure.",
    ),
    adaptive_llm: bool = typer.Option(
        True, "--adaptive-llm/--fixed-llm",
        help="Adapt actual OpenRouter request concurrency after throttling/transient failures.",
    ),
    routine_triage: bool = typer.Option(
        True, "--routine-triage/--no-routine-triage",
        help="Use conservative direct analysis for low-risk routine registration reports.",
    ),
    safe_dedup: bool = typer.Option(
        True, "--safe-dedup/--no-safe-dedup",
        help="Suppress exact/high-confidence near-duplicate attachments after extraction.",
    ),
    instrument_scope: str = typer.Option(
        "stocks",
        "--instrument-scope",
        help="IDX instrument scope: stocks (default) or all.",
    ),
    attachment_policy: str = typer.Option(
        "smart",
        "--attachment-policy",
        help="Attachment selection: smart (default) or all_supported.",
    ),
    historical_audit: bool = typer.Option(
        False,
        "--historical-audit",
        help="Force a full requested-range recheck, including intervals already in saved coverage. Normal runs fetch only uncovered gaps.",
    ),
    diagnostics: bool = typer.Option(
        False,
        "--diagnostics",
        help="Enable concise timestamped diagnostics, progress, browser trace, JSONL detail log, and slowdown report.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show timestamped stage, retry, and timing logs. Chatty cache/page/network events stay hidden unless explicitly enabled.",
    ),
    trace_browser: bool = typer.Option(
        False,
        "--trace-browser",
        help="Save a Playwright trace ZIP. Browser asset requests stay out of the terminal.",
    ),
    browser_network: bool = typer.Option(
        False,
        "--browser-network",
        help="Print every IDX browser request/response. Very noisy; the trace ZIP is usually better.",
    ),
    cache_logs: bool = typer.Option(
        False,
        "--cache-logs",
        help="Print individual cache-hit lines. Cache details are always retained in the JSONL log.",
    ),
    page_logs: bool = typer.Option(
        False,
        "--page-logs",
        help="Print one extraction line per PDF page. Page details are always retained in the JSONL log.",
    ),
    stream_summary: bool = typer.Option(
        False,
        "--stream-summary",
        help="Stream structured summary JSON as OpenRouter generates it.",
    ),
    progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        help="Show live announcement, attachment, download, and extraction progress bars.",
    ),
    log_file: Path | None = typer.Option(
        None,
        "--log-file",
        help="JSONL diagnostic log path. A timestamped path under data/logs is used when logging is enabled.",
    ),
) -> None:
    instrument_scope = instrument_scope.strip().lower()
    attachment_policy = attachment_policy.strip().lower()
    if instrument_scope not in {"stocks", "all"}:
        raise typer.BadParameter("--instrument-scope must be stocks or all")
    if attachment_policy not in {"smart", "all_supported"}:
        raise typer.BadParameter("--attachment-policy must be smart or all_supported")
    settings = Settings()
    updates: dict[str, int] = {}
    if llm_concurrency is not None:
        updates["llm_concurrency"] = llm_concurrency
    if llm_per_announcement is not None:
        updates["llm_per_announcement_concurrency"] = llm_per_announcement
    if llm_document_chunks is not None:
        updates["llm_document_chunk_concurrency"] = llm_document_chunks
    if extraction_workers is not None:
        updates["extraction_workers"] = extraction_workers
    if extraction_queue_size is not None:
        updates["extraction_queue_size"] = extraction_queue_size
    updates["llm_adaptive_concurrency"] = adaptive_llm
    updates["routine_triage_enabled"] = routine_triage
    updates["attachment_dedup_enabled"] = safe_dedup
    if updates:
        settings = settings.model_copy(update=updates)
    if diagnostics:
        verbose = True
        trace_browser = True
    if browser_network or cache_logs or page_logs:
        verbose = True
    progress_disabled_for_stream = bool(stream_summary and progress)
    if stream_summary:
        # A live Rich dashboard and raw token stream cannot safely own the same
        # terminal. Prefer readable JSON over animated terminal confetti.
        progress = False
    logging_enabled = (
        verbose
        or trace_browser
        or browser_network
        or cache_logs
        or page_logs
        or stream_summary
        or diagnostics
    )
    resolved_log_file = log_file or (_default_log_path(settings) if logging_enabled else None)

    observer = RunObserver(
        timezone_name=settings.app_timezone,
        verbose=verbose,
        trace_browser=trace_browser,
        browser_network=browser_network,
        stream_llm=stream_summary and not skip_llm,
        show_progress=progress,
        show_cache_events=cache_logs,
        show_page_events=page_logs,
        log_file=resolved_log_file,
    )
    if progress_disabled_for_stream:
        observer.event(
            "display",
            "progress dashboard disabled because --stream-summary was requested",
            level="WARNING",
            always=True,
        )
    start_at = parse_boundary(start, settings.app_timezone, is_end=False)
    end_at = parse_boundary(end, settings.app_timezone, is_end=True)
    pipeline = Pipeline(settings, skip_llm=skip_llm, observer=observer)
    try:
        report = pipeline.run(
            start_at=start_at,
            end_at=end_at,
            ticker=ticker,
            keyword=keyword,
            max_announcements=max_announcements,
            attachment_policy=attachment_policy,
            instrument_scope=instrument_scope,
            metadata_mode="historical_audit" if historical_audit else "incremental",
        )
    finally:
        pipeline.close()
        observer.close()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("reduce-cached")
def reduce_cached(
    start: str = typer.Option(..., help="ISO start date/datetime of the cached disclosure window."),
    end: str = typer.Option(..., help="ISO end date/datetime of the cached disclosure window."),
    ticker: str | None = typer.Option(None, help="Optional ticker. Omit to reduce every cached company."),
    llm_concurrency: int | None = typer.Option(
        None,
        "--llm-concurrency",
        min=1,
        max=8,
        help="Parallel company-reducer workers. Overrides LLM_CONCURRENCY.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Rebuild company summaries that already exist for this exact window.",
    ),
    diagnostics: bool = typer.Option(
        True,
        "--diagnostics/--no-diagnostics",
        help="Show reducer progress and write a JSONL diagnostic log.",
    ),
) -> None:
    """Finish company digests from committed announcement summaries only.

    This command never contacts IDX and never redownloads or re-extracts a document.
    Legacy v0.3.7 announcement summaries are accepted as reducer input.
    """
    settings = Settings()
    if llm_concurrency is not None:
        settings = settings.model_copy(update={"llm_concurrency": llm_concurrency})
    settings.ensure_directories()
    observer = RunObserver(
        timezone_name=settings.app_timezone,
        verbose=diagnostics,
        trace_browser=False,
        browser_network=False,
        stream_llm=False,
        show_progress=True,
        show_cache_events=False,
        show_page_events=False,
        log_file=_default_log_path(settings) if diagnostics else None,
    )
    reducer = CachedCompanyReducer(settings, observer=observer)
    try:
        start_at = parse_boundary(start, settings.app_timezone, is_end=False)
        end_at = parse_boundary(end, settings.app_timezone, is_end=True)
        report = reducer.reduce(
            start_at=start_at.isoformat(),
            end_at=end_at.isoformat(),
            ticker=(ticker or "").strip().upper() or None,
            force=force,
        )
    finally:
        reducer.close()
        observer.close()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("refine-financials")
def refine_financials(
    start: str = typer.Option(..., help="ISO start date/datetime of cached announcements."),
    end: str = typer.Option(..., help="ISO end date/datetime of cached announcements."),
    ticker: str | None = typer.Option(None, help="Optional ticker, e.g. PJAA. Omit for all cached stocks."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview keep/skip sources with zero LLM calls."),
    diagnostics: bool = typer.Option(True, "--diagnostics/--no-diagnostics"),
) -> None:
    """Rebuild cached financial-report summaries from XLSX + primary LK PDF only.

    This command does not contact IDX and does not redownload attachments.
    """
    settings = Settings()
    settings.ensure_directories()
    observer = RunObserver(
        timezone_name=settings.app_timezone,
        verbose=diagnostics,
        trace_browser=False,
        browser_network=False,
        stream_llm=False,
        show_progress=True,
        show_cache_events=False,
        show_page_events=False,
        log_file=_default_log_path(settings) if diagnostics else None,
    )
    refiner = CachedFinancialRefiner(settings, observer=observer)
    try:
        start_at = parse_boundary(start, settings.app_timezone, is_end=False)
        end_at = parse_boundary(end, settings.app_timezone, is_end=True)
        normalized_ticker = (ticker or "").strip().upper() or None
        if dry_run:
            report = refiner.preview(
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
                ticker=normalized_ticker,
            )
        else:
            report = refiner.refine(
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
                ticker=normalized_ticker,
            )
    finally:
        refiner.close()
        observer.close()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("recover")
def recover(
    start: str = typer.Option(..., help="Original ISO start date/datetime."),
    end: str = typer.Option(..., help="Original ISO end date/datetime."),
    ticker: str | None = typer.Option(None, help="Optional ticker filter."),
    destination: Path | None = typer.Option(
        None,
        "--destination",
        help="Recovery export directory. Defaults to data/recovery/<timestamp>.",
    ),
) -> None:
    """Export every committed partial summary without internet or OpenRouter."""
    settings = Settings()
    start_at = parse_boundary(start, settings.app_timezone, is_end=False)
    end_at = parse_boundary(end, settings.app_timezone, is_end=True)
    if destination is None:
        stamp = datetime.now(ZoneInfo(settings.app_timezone)).strftime("%Y%m%d-%H%M%S")
        destination = settings.data_dir / "recovery" / stamp
    snapshot = Database(settings.database_path).export_recovery(
        destination,
        start_at.isoformat(),
        end_at.isoformat(),
        ticker=(ticker or "").strip() or None,
    )
    typer.echo(json.dumps({
        "destination": str(destination),
        "announcement_count": snapshot["announcement_count"],
        "document_summary_count": snapshot["document_summary_count"],
        "announcement_summary_count": snapshot["announcement_summary_count"],
        "companies": snapshot["companies"],
    }, ensure_ascii=False, indent=2))


@app.command("export-all")
def export_all(
    start: str = typer.Option(..., help="Exact ISO start date/datetime of saved company digests."),
    end: str = typer.Option(..., help="Exact ISO end date/datetime of saved company digests."),
    ticker: str | None = typer.Option(None, help="Optional one-ticker export. Omit for every company in the window."),
    format: str = typer.Option("md", "--format", "-f", help="Output format: md, txt, or json."),
    sections: str | None = typer.Option(
        None,
        "--sections",
        help="Comma-separated sections. Omit for the friend-ready default set.",
    ),
    signals_only: bool = typer.Option(
        False,
        "--signals-only",
        help="Share only the corporate-action/expansion signal sections plus overview and monitoring.",
    ),
    destination: Path | None = typer.Option(
        None,
        "--destination",
        "-o",
        help="Output file. Defaults to data/share/<timestamp>.",
    ),
) -> None:
    """Append saved per-company digests into one shareable file, without any LLM call."""
    settings = Settings()
    start_at = parse_boundary(start, settings.app_timezone, is_end=False)
    end_at = parse_boundary(end, settings.app_timezone, is_end=True)
    if signals_only and sections:
        raise typer.BadParameter("Use either --signals-only or --sections, not both")
    selected_sections = (
        SIGNALS_ONLY_SECTIONS
        if signals_only
        else [part.strip() for part in sections.split(",") if part.strip()]
        if sections
        else DEFAULT_SHARE_SECTIONS
    )
    normalized_format = format.strip().lower()
    if normalized_format not in {"md", "markdown", "txt", "text", "json"}:
        raise typer.BadParameter("--format must be md, txt, or json")
    normalized_ticker = (ticker or "").strip().upper() or None
    bundle = load_share_bundle(
        settings.database_path,
        start_at=start_at.isoformat(),
        end_at=end_at.isoformat(),
        ticker=normalized_ticker,
        sections=selected_sections,
    )
    if not bundle.companies:
        raise typer.BadParameter("No saved company summaries found for this exact window")
    if destination is None:
        destination = settings.data_dir / "share" / default_share_filename(
            normalized_format, ticker=normalized_ticker
        )
    write_share_export(bundle, destination, fmt=normalized_format)
    typer.echo(json.dumps({
        "destination": str(destination),
        "company_count": bundle.company_count,
        "sections": list(bundle.sections),
        "format": normalized_format,
        "llm_calls": 0,
    }, ensure_ascii=False, indent=2))


@app.command("gui")
def gui_command(
    host: str = typer.Option(
        "127.0.0.1",
        help="Bind address. Keep 127.0.0.1 unless you add your own network security.",
    ),
    port: int = typer.Option(8787, min=1, max=65535, help="Local dashboard port."),
    open_browser: bool = typer.Option(
        True,
        "--open-browser/--no-open-browser",
        help="Open the dashboard in the default browser after startup.",
    ),
) -> None:
    """Launch the local modern web dashboard."""
    from .gui import launch_gui

    if host not in {"127.0.0.1", "localhost", "::1"}:
        typer.echo(
            "WARNING: the GUI has no built-in authentication. Do not expose it publicly.",
            err=True,
        )
    typer.echo(f"IDX Signal Desk: http://{host}:{port}", err=True)
    launch_gui(host=host, port=port, open_browser=open_browser)


@app.command("show-config")
def show_config() -> None:
    settings = Settings()
    safe = settings.model_dump()
    safe["openrouter_api_key"] = "***" if settings.openrouter_api_key else ""
    safe["idx_cookie"] = "***" if settings.idx_cookie else ""
    safe["data_dir"] = str(settings.data_dir)
    safe["prompt_config_path"] = str(settings.prompt_config_path)
    prompt_bundle = PromptStore(settings.prompt_config_path).load()
    safe["prompt_profile"] = prompt_bundle.profile_name
    safe["prompt_hashes"] = prompt_bundle.hashes
    typer.echo(json.dumps(safe, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    app()
