from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS announcements (
    id2 TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    title TEXT NOT NULL,
    announcement_no TEXT,
    announcement_type TEXT,
    subject TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_ticker_time
    ON announcements(ticker, announced_at);

CREATE TABLE IF NOT EXISTS scrape_watermarks (
    scope_key TEXT PRIMARY KEY,
    last_successful_poll_end TEXT NOT NULL,
    last_seen_announcement_at TEXT,
    baseline_source TEXT NOT NULL DEFAULT 'runtime',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrape_coverage_ranges (
    scope_key TEXT NOT NULL,
    covered_start TEXT NOT NULL,
    covered_end TEXT NOT NULL,
    baseline_source TEXT NOT NULL DEFAULT 'runtime',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, covered_start, covered_end)
);
CREATE INDEX IF NOT EXISTS idx_scrape_coverage_scope_start
    ON scrape_coverage_ranges(scope_key, covered_start);

CREATE TABLE IF NOT EXISTS attachments (
    url TEXT PRIMARY KEY,
    announcement_id TEXT NOT NULL REFERENCES announcements(id2) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    is_attachment INTEGER NOT NULL DEFAULT 0,
    local_path TEXT,
    sha256 TEXT,
    content_type TEXT,
    extracted_text_path TEXT,
    extraction_method TEXT,
    extraction_error TEXT,
    selected_for_analysis INTEGER NOT NULL DEFAULT 1,
    selection_reason TEXT,
    selection_category TEXT,
    duplicate_of_url TEXT,
    downloaded_at TEXT,
    extracted_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_announcement
    ON attachments(announcement_id);

CREATE TABLE IF NOT EXISTS document_summaries (
    url TEXT PRIMARY KEY REFERENCES attachments(url) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_chunk_summaries (
    url TEXT NOT NULL REFERENCES attachments(url) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (url, chunk_index, model, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_document_chunk_summaries_url
    ON document_chunk_summaries(url);

CREATE TABLE IF NOT EXISTS announcement_summaries (
    announcement_id TEXT PRIMARY KEY REFERENCES announcements(id2) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    analysis_mode TEXT NOT NULL DEFAULT 'full',
    triage_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    ticker TEXT,
    announcement_id TEXT,
    attachment_url TEXT,
    filename TEXT,
    window_start TEXT,
    window_end TEXT,
    chunk_index INTEGER,
    chunk_count INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    model TEXT NOT NULL,
    prompt_version TEXT,
    prompt_profile TEXT,
    system_prompt TEXT NOT NULL,
    user_prompt TEXT NOT NULL,
    raw_response TEXT,
    parsed_json TEXT,
    status TEXT NOT NULL,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_audits_ticker_time
    ON llm_audits(ticker, started_at);
CREATE INDEX IF NOT EXISTS idx_llm_audits_announcement
    ON llm_audits(announcement_id);

CREATE TABLE IF NOT EXISTS company_window_summaries (
    ticker TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_fingerprint TEXT,
    generation_mode TEXT NOT NULL DEFAULT 'llm',
    source_announcement_count INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ticker, start_at, end_at)
);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the current schema and apply additive migrations for old archives."""

    connection.executescript(SCHEMA)
    _add_columns(
        connection,
        "attachments",
        {
            "downloaded_at": "TEXT",
            "extracted_at": "TEXT",
            "selected_for_analysis": "INTEGER NOT NULL DEFAULT 1",
            "selection_reason": "TEXT",
            "selection_category": "TEXT",
            "duplicate_of_url": "TEXT",
        },
    )
    _add_columns(
        connection,
        "announcement_summaries",
        {
            "analysis_mode": "TEXT NOT NULL DEFAULT 'full'",
            "triage_json": "TEXT",
        },
    )
    _add_columns(
        connection,
        "company_window_summaries",
        {
            "input_fingerprint": "TEXT",
            "generation_mode": "TEXT NOT NULL DEFAULT 'llm'",
            "source_announcement_count": "INTEGER",
        },
    )


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
