from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()}
            if "downloaded_at" not in columns:
                conn.execute("ALTER TABLE attachments ADD COLUMN downloaded_at TEXT")
            if "extracted_at" not in columns:
                conn.execute("ALTER TABLE attachments ADD COLUMN extracted_at TEXT")
            if "selected_for_analysis" not in columns:
                conn.execute("ALTER TABLE attachments ADD COLUMN selected_for_analysis INTEGER NOT NULL DEFAULT 1")
            if "selection_reason" not in columns:
                conn.execute("ALTER TABLE attachments ADD COLUMN selection_reason TEXT")
            if "selection_category" not in columns:
                conn.execute("ALTER TABLE attachments ADD COLUMN selection_category TEXT")
            if "duplicate_of_url" not in columns:
                conn.execute("ALTER TABLE attachments ADD COLUMN duplicate_of_url TEXT")
            announcement_columns = {row[1] for row in conn.execute("PRAGMA table_info(announcement_summaries)").fetchall()}
            if "analysis_mode" not in announcement_columns:
                conn.execute("ALTER TABLE announcement_summaries ADD COLUMN analysis_mode TEXT NOT NULL DEFAULT 'full'")
            if "triage_json" not in announcement_columns:
                conn.execute("ALTER TABLE announcement_summaries ADD COLUMN triage_json TEXT")
            company_columns = {row[1] for row in conn.execute("PRAGMA table_info(company_window_summaries)").fetchall()}
            if "input_fingerprint" not in company_columns:
                conn.execute("ALTER TABLE company_window_summaries ADD COLUMN input_fingerprint TEXT")
            if "generation_mode" not in company_columns:
                conn.execute("ALTER TABLE company_window_summaries ADD COLUMN generation_mode TEXT NOT NULL DEFAULT 'llm'")
            if "source_announcement_count" not in company_columns:
                conn.execute("ALTER TABLE company_window_summaries ADD COLUMN source_announcement_count INTEGER")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def latest_announcement_at(self, ticker: str | None = None) -> str | None:
        with self.connect() as conn:
            if ticker:
                row = conn.execute(
                    "SELECT MAX(announced_at) AS latest FROM announcements WHERE ticker=?",
                    (ticker.strip().upper(),),
                ).fetchone()
            else:
                row = conn.execute("SELECT MAX(announced_at) AS latest FROM announcements").fetchone()
        return str(row["latest"]) if row and row["latest"] else None

    def announcement_exists(self, announcement_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM announcements WHERE id2=?", (announcement_id,)).fetchone()
        return row is not None

    def scrape_watermark(self, scope_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT scope_key,last_successful_poll_end,last_seen_announcement_at,baseline_source,updated_at FROM scrape_watermarks WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
        return dict(row) if row else None

    def delete_scrape_watermark(self, scope_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM scrape_watermarks WHERE scope_key=?", (scope_key,))

    def scrape_coverage(self, scope_key: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT scope_key,covered_start,covered_end,baseline_source,updated_at
                FROM scrape_coverage_ranges
                WHERE scope_key=?
                """,
                (scope_key,),
            ).fetchall()
        result = [dict(row) for row in rows]
        result.sort(key=lambda row: datetime.fromisoformat(str(row["covered_start"])))
        return result

    def delete_scrape_coverage(self, scope_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM scrape_coverage_ranges WHERE scope_key=?", (scope_key,))

    def save_scrape_coverage(
        self,
        scope_key: str,
        *,
        covered_start: str,
        covered_end: str,
        baseline_source: str = "runtime",
    ) -> None:
        self.save_scrape_coverages(
            scope_key,
            ranges=[(covered_start, covered_end)],
            baseline_source=baseline_source,
        )

    def save_scrape_coverages(
        self,
        scope_key: str,
        *,
        ranges: Iterable[tuple[str, str]],
        baseline_source: str = "runtime",
    ) -> None:
        """Atomically add proven intervals and normalize all ranges for a scope."""

        additions: list[tuple[datetime, datetime, str]] = []
        for covered_start, covered_end in ranges:
            start = datetime.fromisoformat(covered_start)
            end = datetime.fromisoformat(covered_end)
            if end < start:
                raise ValueError("covered_end must be greater than or equal to covered_start")
            additions.append((start, end, baseline_source))
        if not additions:
            return
        timestamp = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT covered_start,covered_end,baseline_source
                FROM scrape_coverage_ranges
                WHERE scope_key=?
                """,
                (scope_key,),
            ).fetchall()
            existing_ranges = [
                (
                    datetime.fromisoformat(str(row["covered_start"])),
                    datetime.fromisoformat(str(row["covered_end"])),
                    str(row["baseline_source"]),
                )
                for row in rows
            ]
            existing_ranges.extend(additions)
            existing_ranges.sort(key=lambda item: (item[0], item[1]))
            merged: list[tuple[datetime, datetime, str]] = []
            for current_start, current_end, current_source in existing_ranges:
                if merged and current_start <= merged[-1][1]:
                    prior_start, prior_end, prior_source = merged[-1]
                    merged[-1] = (
                        prior_start,
                        max(prior_end, current_end),
                        baseline_source if baseline_source in {prior_source, current_source} else current_source,
                    )
                else:
                    merged.append((current_start, current_end, current_source))
            conn.execute("DELETE FROM scrape_coverage_ranges WHERE scope_key=?", (scope_key,))
            conn.executemany(
                """
                INSERT INTO scrape_coverage_ranges(
                    scope_key,covered_start,covered_end,baseline_source,updated_at
                ) VALUES (?,?,?,?,?)
                """,
                [
                    (scope_key, item_start.isoformat(), item_end.isoformat(), source, timestamp)
                    for item_start, item_end, source in merged
                ],
            )

    def save_scrape_watermark(
        self,
        scope_key: str,
        *,
        last_successful_poll_end: str,
        last_seen_announcement_at: str | None,
        baseline_source: str = "runtime",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scrape_watermarks(
                    scope_key,last_successful_poll_end,last_seen_announcement_at,baseline_source,updated_at
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    last_successful_poll_end=excluded.last_successful_poll_end,
                    last_seen_announcement_at=excluded.last_seen_announcement_at,
                    baseline_source=excluded.baseline_source,
                    updated_at=excluded.updated_at
                """,
                (scope_key,last_successful_poll_end,last_seen_announcement_at,baseline_source,utc_now()),
            )

    def upsert_announcement(self, item: dict[str, Any], announced_at: str) -> tuple[str, str]:
        p = item["pengumuman"]
        item_id = str(p["Id2"])
        ticker = str(p.get("Kode_Emiten") or "UNKNOWN").strip().upper()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO announcements (
                    id2, ticker, announced_at, title, announcement_no,
                    announcement_type, subject, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id2) DO UPDATE SET
                    ticker=excluded.ticker,
                    announced_at=excluded.announced_at,
                    title=excluded.title,
                    announcement_no=excluded.announcement_no,
                    announcement_type=excluded.announcement_type,
                    subject=excluded.subject,
                    raw_json=excluded.raw_json,
                    fetched_at=excluded.fetched_at
                """,
                (
                    item_id,
                    ticker,
                    announced_at,
                    str(p.get("JudulPengumuman") or ""),
                    str(p.get("NoPengumuman") or ""),
                    str(p.get("JenisPengumuman") or ""),
                    str(p.get("PerihalPengumuman") or ""),
                    json.dumps(item, ensure_ascii=False),
                    utc_now(),
                ),
            )
        return item_id, ticker

    def upsert_attachment(
        self,
        announcement_id: str,
        attachment: dict[str, Any],
        *,
        selected_for_analysis: bool = True,
        selection_reason: str | None = None,
        selection_category: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO attachments (
                    url, announcement_id, original_filename, is_attachment,
                    selected_for_analysis, selection_reason, selection_category, duplicate_of_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(url) DO UPDATE SET
                    announcement_id=excluded.announcement_id,
                    original_filename=excluded.original_filename,
                    is_attachment=excluded.is_attachment,
                    selected_for_analysis=excluded.selected_for_analysis,
                    selection_reason=excluded.selection_reason,
                    selection_category=excluded.selection_category,
                    duplicate_of_url=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    attachment["FullSavePath"],
                    announcement_id,
                    attachment.get("OriginalFilename") or attachment.get("PDFFilename") or "attachment",
                    int(bool(attachment.get("IsAttachment"))),
                    int(bool(selected_for_analysis)),
                    selection_reason,
                    selection_category,
                    utc_now(),
                ),
            )

    def attachment_state(self, url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM attachments WHERE url = ?", (url,)).fetchone()

    def announcement_attachments(self, announcement_id: str) -> list[sqlite3.Row]:
        """Return every cached attachment row for one announcement.

        Historical announcement JSON can be incomplete after older parser versions,
        while the attachment table still contains rows created during the original
        scrape. Recovery/refinement code should merge both sources rather than trust
        either representation alone.
        """
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM attachments
                WHERE announcement_id=?
                ORDER BY is_attachment, original_filename, url
                """,
                (announcement_id,),
            ).fetchall()

    def update_attachment_selection(
        self,
        url: str,
        *,
        selected_for_analysis: bool,
        selection_reason: str,
        selection_category: str,
        duplicate_of_url: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE attachments
                SET selected_for_analysis=?, selection_reason=?, selection_category=?, duplicate_of_url=?, updated_at=?
                WHERE url=?
                """,
                (
                    int(bool(selected_for_analysis)),
                    selection_reason,
                    selection_category,
                    duplicate_of_url,
                    utc_now(),
                    url,
                ),
            )

    def update_attachment_file(
        self, url: str, *, local_path: str, sha256: str, content_type: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE attachments
                SET local_path=?, sha256=?, content_type=?, downloaded_at=?, updated_at=?
                WHERE url=?
                """,
                (local_path, sha256, content_type, utc_now(), utc_now(), url),
            )

    def update_extraction(
        self,
        url: str,
        *,
        text_path: str | None,
        method: str | None,
        error: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE attachments
                SET extracted_text_path=?, extraction_method=?, extraction_error=?, extracted_at=?, updated_at=?
                WHERE url=?
                """,
                (text_path, method, error, utc_now(), utc_now(), url),
            )

    def save_document_summary(
        self,
        url: str,
        ticker: str,
        payload: dict[str, Any],
        model: str,
        prompt_version: str = "legacy-document",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO document_summaries(url, ticker, summary_json, model, prompt_version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    ticker=excluded.ticker,
                    summary_json=excluded.summary_json,
                    model=excluded.model,
                    prompt_version=excluded.prompt_version,
                    updated_at=excluded.updated_at
                """,
                (
                    url, ticker, json.dumps(payload, ensure_ascii=False), model,
                    prompt_version, utc_now(),
                ),
            )

    def get_document_summary(
        self,
        url: str,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT summary_json, model, prompt_version FROM document_summaries WHERE url=?",
                (url,),
            ).fetchone()
            if row and (
                (model is not None and row["model"] != model)
                or (prompt_version is not None and row["prompt_version"] != prompt_version)
            ):
                conn.execute("DELETE FROM document_summaries WHERE url=?", (url,))
                return None
        return json.loads(row["summary_json"]) if row else None

    def save_document_chunk_summary(
        self,
        url: str,
        *,
        chunk_index: int,
        chunk_count: int,
        chunk_sha256: str,
        payload: dict[str, Any],
        model: str,
        prompt_version: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO document_chunk_summaries(
                    url, chunk_index, chunk_count, chunk_sha256, summary_json,
                    model, prompt_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url, chunk_index, model, prompt_version) DO UPDATE SET
                    chunk_count=excluded.chunk_count,
                    chunk_sha256=excluded.chunk_sha256,
                    summary_json=excluded.summary_json,
                    updated_at=excluded.updated_at
                """,
                (
                    url, int(chunk_index), int(chunk_count), chunk_sha256,
                    json.dumps(payload, ensure_ascii=False), model, prompt_version, utc_now(),
                ),
            )

    def get_document_chunk_summary(
        self,
        url: str,
        *,
        chunk_index: int,
        chunk_count: int,
        chunk_sha256: str,
        model: str,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT summary_json, chunk_count, chunk_sha256
                FROM document_chunk_summaries
                WHERE url=? AND chunk_index=? AND model=? AND prompt_version=?
                """,
                (url, int(chunk_index), model, prompt_version),
            ).fetchone()
            if row is None:
                return None
            if int(row["chunk_count"]) != int(chunk_count) or str(row["chunk_sha256"]) != chunk_sha256:
                conn.execute(
                    "DELETE FROM document_chunk_summaries WHERE url=? AND chunk_index=? AND model=? AND prompt_version=?",
                    (url, int(chunk_index), model, prompt_version),
                )
                return None
            return json.loads(row["summary_json"])

    def document_chunk_progress(
        self,
        url: str,
        *,
        model: str,
        prompt_version: str,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT chunk_index, chunk_count, chunk_sha256, updated_at
                FROM document_chunk_summaries
                WHERE url=? AND model=? AND prompt_version=?
                ORDER BY chunk_index
                """,
                (url, model, prompt_version),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_document_chunk_summaries(self, url: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM document_chunk_summaries WHERE url=?", (url,))

    def delete_document_summary(self, url: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM document_summaries WHERE url=?", (url,))

    def get_announcement_summary(
        self,
        announcement_id: str,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT summary_json, model, prompt_version
                FROM announcement_summaries WHERE announcement_id=?
                """,
                (announcement_id,),
            ).fetchone()
            if row and (
                (model is not None and row["model"] != model)
                or (prompt_version is not None and row["prompt_version"] != prompt_version)
            ):
                conn.execute(
                    "DELETE FROM announcement_summaries WHERE announcement_id=?",
                    (announcement_id,),
                )
                return None
        return json.loads(row["summary_json"]) if row else None

    def delete_announcement_summary(self, announcement_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM announcement_summaries WHERE announcement_id=?",
                (announcement_id,),
            )

    def save_announcement_summary(
        self,
        announcement_id: str,
        ticker: str,
        payload: dict[str, Any],
        model: str,
        prompt_version: str = "legacy-announcement",
        *,
        analysis_mode: str = "full",
        triage: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO announcement_summaries(
                    announcement_id, ticker, summary_json, model, prompt_version, analysis_mode, triage_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(announcement_id) DO UPDATE SET
                    ticker=excluded.ticker,
                    summary_json=excluded.summary_json,
                    model=excluded.model,
                    prompt_version=excluded.prompt_version,
                    analysis_mode=excluded.analysis_mode,
                    triage_json=excluded.triage_json,
                    updated_at=excluded.updated_at
                """,
                (
                    announcement_id, ticker, json.dumps(payload, ensure_ascii=False),
                    model, prompt_version, analysis_mode,
                    json.dumps(triage, ensure_ascii=False) if triage is not None else None, utc_now(),
                ),
            )

    def announcement_with_documents(
        self,
        announcement_id: str,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        join_filters = ""
        parameters: list[Any] = []
        if model is not None:
            join_filters += " AND ds.model=?"
            parameters.append(model)
        if prompt_version is not None:
            join_filters += " AND ds.prompt_version=?"
            parameters.append(prompt_version)
        parameters.append(announcement_id)
        with self.connect() as conn:
            a = conn.execute("SELECT * FROM announcements WHERE id2=?", (announcement_id,)).fetchone()
            rows = conn.execute(
                f"""
                SELECT at.url, at.original_filename, at.extraction_error, ds.summary_json
                FROM attachments at
                LEFT JOIN document_summaries ds
                  ON ds.url=at.url{join_filters}
                WHERE at.announcement_id=?
                  AND at.selected_for_analysis=1
                ORDER BY at.is_attachment, at.original_filename
                """,
                parameters,
            ).fetchall()
        if not a:
            raise KeyError(announcement_id)
        documents = []
        for row in rows:
            documents.append({
                "url": row["url"],
                "filename": row["original_filename"],
                "extraction_error": row["extraction_error"],
                "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
            })
        return dict(a), documents

    def company_announcement_summaries(
        self,
        ticker: str,
        start_at: str,
        end_at: str,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = ["a.ticker=?", "a.announced_at BETWEEN ? AND ?"]
        parameters: list[Any] = [ticker, start_at, end_at]
        if model is not None:
            filters.append("s.model=?")
            parameters.append(model)
        if prompt_version is not None:
            filters.append("s.prompt_version=?")
            parameters.append(prompt_version)
        where = " AND ".join(filters)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id2, a.announced_at, a.title, s.summary_json,
                       s.model, s.prompt_version, s.updated_at AS summary_updated_at
                FROM announcements a
                JOIN announcement_summaries s ON s.announcement_id=a.id2
                WHERE {where}
                ORDER BY a.announced_at ASC
                """,
                parameters,
            ).fetchall()
        return [
            {
                "announcement_id": row["id2"],
                "announced_at": row["announced_at"],
                "title": row["title"],
                "summary": json.loads(row["summary_json"]),
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "summary_updated_at": row["summary_updated_at"],
            }
            for row in rows
        ]

    def company_window_summary_map(
        self,
        start_at: str,
        end_at: str,
        ticker: str | None = None,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        filters = ["start_at=?", "end_at=?"]
        parameters: list[Any] = [start_at, end_at]
        if ticker:
            filters.append("ticker=?")
            parameters.append(ticker.strip().upper())
        if model is not None:
            filters.append("model=?")
            parameters.append(model)
        if prompt_version is not None:
            filters.append("prompt_version=?")
            parameters.append(prompt_version)
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT ticker, summary_json FROM company_window_summaries
                    WHERE {' AND '.join(filters)} ORDER BY ticker""",
                parameters,
            ).fetchall()
        return {str(row["ticker"]): json.loads(row["summary_json"]) for row in rows}


    def save_llm_audit(
        self,
        *,
        stage: str,
        schema_name: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float,
        prompt_version: str | None = None,
        prompt_profile: str | None = None,
        ticker: str | None = None,
        announcement_id: str | None = None,
        attachment_url: str | None = None,
        filename: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        chunk_index: int | None = None,
        chunk_count: int | None = None,
        attempt: int = 1,
        raw_response: str | None = None,
        parsed: dict[str, Any] | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> int:
        usage = usage or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO llm_audits(
                    stage, schema_name, ticker, announcement_id, attachment_url, filename,
                    window_start, window_end, chunk_index, chunk_count, attempt, model,
                    prompt_version, prompt_profile, system_prompt, user_prompt, raw_response,
                    parsed_json, status, error, started_at, finished_at, elapsed_seconds,
                    prompt_tokens, completion_tokens, total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage, schema_name, ticker, announcement_id, attachment_url, filename,
                    window_start, window_end, chunk_index, chunk_count, attempt, model,
                    prompt_version, prompt_profile, system_prompt, user_prompt, raw_response,
                    json.dumps(parsed, ensure_ascii=False) if parsed is not None else None,
                    status, error, started_at, finished_at, float(elapsed_seconds),
                    usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    usage.get("total_tokens"),
                ),
            )
            return int(cursor.lastrowid)

    def llm_audit(self, audit_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM llm_audits WHERE audit_id=?", (int(audit_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        raw = item.pop("parsed_json", None)
        item["parsed"] = json.loads(raw) if raw else None
        return item

    def company_audit_bundle(self, ticker: str, start_at: str, end_at: str) -> dict[str, Any]:
        company = ticker.strip().upper()
        with self.connect() as conn:
            company_row = conn.execute(
                """SELECT ticker, start_at, end_at, summary_json, model, prompt_version, updated_at,
                          input_fingerprint, generation_mode, source_announcement_count
                   FROM company_window_summaries WHERE ticker=? AND start_at=? AND end_at=?""",
                (company, start_at, end_at),
            ).fetchone()
            announcement_rows = conn.execute(
                """
                SELECT a.*, s.summary_json AS announcement_summary_json,
                       s.model AS summary_model, s.prompt_version AS summary_prompt_version,
                       s.analysis_mode AS summary_analysis_mode, s.triage_json AS summary_triage_json,
                       s.updated_at AS summary_updated_at
                FROM announcements a
                LEFT JOIN announcement_summaries s ON s.announcement_id=a.id2
                WHERE a.ticker=? AND a.announced_at BETWEEN ? AND ?
                ORDER BY a.announced_at ASC
                """,
                (company, start_at, end_at),
            ).fetchall()
            announcement_ids = [str(row["id2"]) for row in announcement_rows]
            attachments_by_announcement: dict[str, list[dict[str, Any]]] = {}
            if announcement_ids:
                marks = ",".join("?" for _ in announcement_ids)
                attachment_rows = conn.execute(
                    f"""
                    SELECT at.*, ds.summary_json AS document_summary_json,
                           ds.model AS document_model, ds.prompt_version AS document_prompt_version,
                           ds.updated_at AS document_summary_updated_at
                    FROM attachments at
                    LEFT JOIN document_summaries ds ON ds.url=at.url
                    WHERE at.announcement_id IN ({marks})
                    ORDER BY at.announcement_id, at.is_attachment, at.original_filename
                    """,
                    announcement_ids,
                ).fetchall()
                for row in attachment_rows:
                    item = dict(row)
                    raw = item.pop("document_summary_json", None)
                    item["document_summary"] = json.loads(raw) if raw else None
                    attachments_by_announcement.setdefault(str(row["announcement_id"]), []).append(item)
            audit_rows = conn.execute(
                """
                SELECT audit_id, stage, schema_name, ticker, announcement_id, attachment_url, filename,
                       window_start, window_end, chunk_index, chunk_count, attempt, model, prompt_version,
                       prompt_profile, status, error, started_at, finished_at, elapsed_seconds,
                       prompt_tokens, completion_tokens, total_tokens
                FROM llm_audits
                WHERE ticker=? AND (
                    (window_start=? AND window_end=?) OR
                    announcement_id IN (SELECT id2 FROM announcements WHERE ticker=? AND announced_at BETWEEN ? AND ?)
                )
                ORDER BY started_at ASC, audit_id ASC
                """,
                (company, start_at, end_at, company, start_at, end_at),
            ).fetchall()

        announcements: list[dict[str, Any]] = []
        for row in announcement_rows:
            item = dict(row)
            raw_summary = item.pop("announcement_summary_json", None)
            item["announcement_summary"] = json.loads(raw_summary) if raw_summary else None
            raw_triage = item.pop("summary_triage_json", None)
            item["summary_triage"] = json.loads(raw_triage) if raw_triage else None
            item["attachments"] = attachments_by_announcement.get(str(row["id2"]), [])
            announcements.append(item)
        audits = [dict(row) for row in audit_rows]
        return {
            "ticker": company,
            "start_at": start_at,
            "end_at": end_at,
            "company_summary": json.loads(company_row["summary_json"]) if company_row else None,
            "company_metadata": {
                "model": company_row["model"],
                "prompt_version": company_row["prompt_version"],
                "updated_at": company_row["updated_at"],
                "input_fingerprint": company_row["input_fingerprint"],
                "generation_mode": company_row["generation_mode"],
                "source_announcement_count": company_row["source_announcement_count"],
            } if company_row else None,
            "announcements": announcements,
            "llm_audits": audits,
        }

    def save_company_summary(
        self,
        ticker: str,
        start_at: str,
        end_at: str,
        payload: dict[str, Any],
        model: str,
        prompt_version: str = "legacy-company",
        *,
        input_fingerprint: str | None = None,
        generation_mode: str = "llm",
        source_announcement_count: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO company_window_summaries(
                    ticker, start_at, end_at, summary_json, model, prompt_version,
                    input_fingerprint, generation_mode, source_announcement_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, start_at, end_at) DO UPDATE SET
                    summary_json=excluded.summary_json,
                    model=excluded.model,
                    prompt_version=excluded.prompt_version,
                    input_fingerprint=excluded.input_fingerprint,
                    generation_mode=excluded.generation_mode,
                    source_announcement_count=excluded.source_announcement_count,
                    updated_at=excluded.updated_at
                """,
                (
                    ticker, start_at, end_at, json.dumps(payload, ensure_ascii=False),
                    model, prompt_version, input_fingerprint, generation_mode,
                    source_announcement_count, utc_now(),
                ),
            )

    def company_summary_record(
        self, ticker: str, start_at: str, end_at: str
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM company_window_summaries
                   WHERE ticker=? AND start_at=? AND end_at=?""",
                (ticker.strip().upper(), start_at, end_at),
            ).fetchone()

    def company_summary_is_current(
        self,
        ticker: str,
        start_at: str,
        end_at: str,
        *,
        model: str,
        prompt_version: str,
        input_fingerprint: str,
    ) -> bool:
        row = self.company_summary_record(ticker, start_at, end_at)
        return bool(
            row
            and row["model"] == model
            and row["prompt_version"] == prompt_version
            and row["input_fingerprint"]
            and row["input_fingerprint"] == input_fingerprint
        )


    def tickers_in_window(
        self,
        start_at: str,
        end_at: str,
        ticker: str | None = None,
    ) -> list[str]:
        filters = ["announced_at BETWEEN ? AND ?"]
        parameters: list[Any] = [start_at, end_at]
        if ticker:
            filters.append("ticker=?")
            parameters.append(ticker.strip().upper())
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ticker FROM announcements WHERE {' AND '.join(filters)} ORDER BY ticker",
                parameters,
            ).fetchall()
        return [str(row["ticker"]) for row in rows]

    def partial_announcement_summaries(
        self,
        start_at: str,
        end_at: str,
        ticker: str | None = None,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        filters = ["a.announced_at BETWEEN ? AND ?"]
        parameters: list[Any] = [start_at, end_at]
        if ticker:
            filters.append("a.ticker=?")
            parameters.append(ticker.strip().upper())
        if model is not None:
            filters.append("s.model=?")
            parameters.append(model)
        if prompt_version is not None:
            filters.append("s.prompt_version=?")
            parameters.append(prompt_version)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.ticker, a.id2, a.announced_at, a.title, s.summary_json,
                       s.model, s.prompt_version, s.updated_at
                FROM announcements a
                JOIN announcement_summaries s ON s.announcement_id=a.id2
                WHERE {' AND '.join(filters)}
                ORDER BY a.ticker, a.announced_at ASC
                """,
                parameters,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["ticker"]), []).append({
                "announcement_id": row["id2"],
                "announced_at": row["announced_at"],
                "title": row["title"],
                "summary": json.loads(row["summary_json"]),
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "updated_at": row["updated_at"],
            })
        return grouped

    def recovery_snapshot(
        self,
        start_at: str,
        end_at: str,
        ticker: str | None = None,
    ) -> dict[str, Any]:
        filters = ["a.announced_at BETWEEN ? AND ?"]
        parameters: list[Any] = [start_at, end_at]
        if ticker:
            filters.append("a.ticker=?")
            parameters.append(ticker.strip().upper())
        where = " AND ".join(filters)
        with self.connect() as conn:
            announcement_count = conn.execute(
                f"SELECT COUNT(*) AS n FROM announcements a WHERE {where}", parameters
            ).fetchone()["n"]
            attachment_count = conn.execute(
                f"""SELECT COUNT(*) AS n FROM attachments at
                    JOIN announcements a ON a.id2=at.announcement_id WHERE {where}""",
                parameters,
            ).fetchone()["n"]
            document_summary_count = conn.execute(
                f"""SELECT COUNT(*) AS n FROM document_summaries ds
                    JOIN attachments at ON at.url=ds.url
                    JOIN announcements a ON a.id2=at.announcement_id WHERE {where}""",
                parameters,
            ).fetchone()["n"]
            document_chunk_checkpoint_count = conn.execute(
                f"""SELECT COUNT(*) AS n FROM document_chunk_summaries cs
                    JOIN attachments at ON at.url=cs.url
                    JOIN announcements a ON a.id2=at.announcement_id WHERE {where}""",
                parameters,
            ).fetchone()["n"]
            announcement_summary_count = conn.execute(
                f"""SELECT COUNT(*) AS n FROM announcement_summaries s
                    JOIN announcements a ON a.id2=s.announcement_id WHERE {where}""",
                parameters,
            ).fetchone()["n"]
            companies = [
                row["ticker"] for row in conn.execute(
                    f"SELECT DISTINCT a.ticker FROM announcements a WHERE {where} ORDER BY a.ticker",
                    parameters,
                ).fetchall()
            ]
        partials = self.partial_announcement_summaries(start_at, end_at, ticker)
        return {
            "start_at": start_at,
            "end_at": end_at,
            "ticker_filter": ticker.strip().upper() if ticker else None,
            "companies": companies,
            "announcement_count": int(announcement_count),
            "attachment_count": int(attachment_count),
            "document_summary_count": int(document_summary_count),
            "document_chunk_checkpoint_count": int(document_chunk_checkpoint_count),
            "announcement_summary_count": int(announcement_summary_count),
            "partial_announcement_summaries": partials,
            "generated_at": utc_now(),
        }

    def export_recovery(
        self,
        destination: Path,
        start_at: str,
        end_at: str,
        ticker: str | None = None,
    ) -> dict[str, Any]:
        destination.mkdir(parents=True, exist_ok=True)
        snapshot = self.recovery_snapshot(start_at, end_at, ticker)
        (destination / "recovery.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for company, records in snapshot["partial_announcement_summaries"].items():
            company_dir = destination / company
            company_dir.mkdir(parents=True, exist_ok=True)
            with (company_dir / "announcement_summaries.jsonl").open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return snapshot

    def library_windows(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return saved company-summary windows, newest first, independent of GUI filters."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT start_at, end_at, COUNT(*) AS company_count,
                       MAX(updated_at) AS updated_at
                FROM company_window_summaries
                GROUP BY start_at, end_at
                ORDER BY MAX(updated_at) DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            output: list[dict[str, Any]] = []
            for row in rows:
                tickers = [
                    str(item["ticker"])
                    for item in conn.execute(
                        """SELECT ticker FROM company_window_summaries
                           WHERE start_at=? AND end_at=? ORDER BY ticker""",
                        (row["start_at"], row["end_at"]),
                    ).fetchall()
                ]
                announcement_count = conn.execute(
                    """SELECT COUNT(*) AS n FROM announcements
                       WHERE announced_at >= ? AND announced_at <= ?""",
                    (row["start_at"], row["end_at"]),
                ).fetchone()["n"]
                announcement_summary_count = conn.execute(
                    """SELECT COUNT(*) AS n FROM announcement_summaries s
                       JOIN announcements a ON a.id2=s.announcement_id
                       WHERE a.announced_at >= ? AND a.announced_at <= ?""",
                    (row["start_at"], row["end_at"]),
                ).fetchone()["n"]
                output.append({
                    "start_at": row["start_at"],
                    "end_at": row["end_at"],
                    "company_count": int(row["company_count"] or 0),
                    "announcement_count": int(announcement_count or 0),
                    "announcement_summary_count": int(announcement_summary_count or 0),
                    "updated_at": row["updated_at"],
                    "tickers": tickers,
                })
            return output

    def company_library(self, limit: int = 2000) -> list[dict[str, Any]]:
        """Return one durable library row per ticker with its latest saved digest window."""
        with self.connect() as conn:
            tickers = conn.execute(
                """
                SELECT ticker, COUNT(*) AS window_count, MAX(updated_at) AS latest_updated_at
                FROM company_window_summaries
                GROUP BY ticker
                ORDER BY ticker
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            output: list[dict[str, Any]] = []
            for row in tickers:
                latest = conn.execute(
                    """
                    SELECT start_at, end_at, updated_at, summary_json
                    FROM company_window_summaries
                    WHERE ticker=?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (row["ticker"],),
                ).fetchone()
                summary = json.loads(latest["summary_json"]) if latest and latest["summary_json"] else {}
                output.append({
                    "ticker": row["ticker"],
                    "window_count": int(row["window_count"] or 0),
                    "latest_updated_at": row["latest_updated_at"],
                    "start_at": latest["start_at"] if latest else None,
                    "end_at": latest["end_at"] if latest else None,
                    "overview": summary.get("overview") or "",
                    "announcement_count": summary.get("announcement_count"),
                })
            return output

    def profile_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            tables = {
                "announcements": "announcements",
                "attachments": "attachments",
                "document_summaries": "document_summaries",
                "announcement_summaries": "announcement_summaries",
                "company_summaries": "company_window_summaries",
                "llm_audits": "llm_audits",
            }
            return {
                key: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] or 0)
                for key, table in tables.items()
            }

    def export_company(self, ticker: str, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id2, a.announced_at, a.title, a.announcement_no,
                       a.announcement_type, a.subject, s.summary_json
                FROM announcements a
                LEFT JOIN announcement_summaries s ON s.announcement_id=a.id2
                WHERE a.ticker=? ORDER BY a.announced_at ASC
                """,
                (ticker,),
            ).fetchall()
            windows = conn.execute(
                """
                SELECT start_at, end_at, summary_json, updated_at
                FROM company_window_summaries
                WHERE ticker=? ORDER BY updated_at DESC
                """,
                (ticker,),
            ).fetchall()

        with (destination / "announcements.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                record = dict(row)
                if record["summary_json"]:
                    record["summary"] = json.loads(record.pop("summary_json"))
                else:
                    record.pop("summary_json")
                    record["summary"] = None
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        if windows:
            latest = dict(windows[0])
            latest["summary"] = json.loads(latest.pop("summary_json"))
            (destination / "latest_window_summary.json").write_text(
                json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
