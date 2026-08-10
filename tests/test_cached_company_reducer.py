from __future__ import annotations

import threading
import time
from pathlib import Path

from idx_digest.cached_reducer import CachedCompanyReducer
from idx_digest.config import Settings
from idx_digest.db import Database


class FakeSummarizer:
    model = "fake/model@fake"
    company_prompt_version = "company-test"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    @staticmethod
    def is_valid_announcement_summary(payload):
        # Treat seeded v0.3.7-style summaries as legacy.
        return False

    def summarize_company_window(self, *, ticker, start_at, end_at, announcements):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(ticker)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return {
            "ticker": ticker,
            "period": {"start": start_at, "end": end_at},
            "announcement_count": len(announcements),
            "overview": f"Digest {ticker}",
            "timeline": [],
            "material_changes": [],
            "key_financial_figures": [],
            "corporate_actions": [],
            "expansion_projects": [],
            "management_or_control_changes": [],
            "capital_structure_events": [],
            "listing_or_regulatory_events": [],
            "analytical_scenarios": [],
            "risks_or_uncertainties": [],
            "items_to_monitor": [],
            "limitations": [],
        }

    def close(self) -> None:
        pass


def seed_announcement(db: Database, announcement_id: str, ticker: str, announced_at: str) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO announcements(
                id2, ticker, announced_at, title, announcement_no,
                announcement_type, subject, raw_json, fetched_at
            ) VALUES (?, ?, ?, ?, '', '', '', '{}', ?)
            """,
            (announcement_id, ticker, announced_at, f"Title {ticker or 'blank'}", announced_at),
        )
    db.save_announcement_summary(
        announcement_id,
        ticker,
        {
            "ticker": ticker or "UNKNOWN",
            "announcement_id": announcement_id,
            "announced_at": announced_at,
            "title": f"Title {ticker or 'blank'}",
            "executive_summary": f"Legacy summary for {ticker or 'blank'}",
            "category": None,
            "material_facts": ["fact"],
            "financial_figures": [],
            "corporate_actions": [],
            "dates_and_deadlines": [],
            "risks_or_uncertainties": [],
            "possible_investor_relevance": [],
            "source_files": [],
            "limitations": [],
        },
        "legacy/model@provider",
        "announcement-v2-legacy",
    )


def existing_company_payload(ticker: str, start: str, end: str):
    return {
        "ticker": ticker,
        "period": {"start": start, "end": end},
        "announcement_count": 1,
        "overview": "existing",
        "timeline": [],
        "material_changes": [],
        "key_financial_figures": [],
        "corporate_actions": [],
        "expansion_projects": [],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "risks_or_uncertainties": [],
        "items_to_monitor": [],
        "limitations": [],
    }


def test_reducer_uses_legacy_checkpoints_skips_existing_and_blank(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        openrouter_api_key="unused",
        llm_concurrency=2,
    )
    settings.ensure_directories()
    db = Database(settings.database_path)
    start = "2026-08-01T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    seed_announcement(db, "a1", "ANTM", "2026-08-02T10:00:00+07:00")
    seed_announcement(db, "b1", "BIKE", "2026-08-03T10:00:00+07:00")
    seed_announcement(db, "c1", "BIRD", "2026-08-04T10:00:00+07:00")
    seed_announcement(db, "blank1", "", "2026-08-05T10:00:00+07:00")
    db.save_company_summary(
        "ANTM", start, end, existing_company_payload("ANTM", start, end), "old", "old"
    )

    fake = FakeSummarizer()
    reducer = CachedCompanyReducer(settings, summarizer=fake)
    report = reducer.reduce(start_at=start, end_at=end)

    assert report["status"] == "completed"
    assert report["completed_companies"] == 3
    assert set(report["completed_tickers"]) == {"ANTM", "BIKE", "BIRD"}
    # A legacy company summary has no input fingerprint, so v0.15 refreshes it once.
    assert report["skipped_existing"] == []
    assert "<blank ticker>" in report["skipped_invalid"]
    assert fake.max_active == 0  # three single-announcement windows promoted deterministically
    assert (data_dir / "companies" / "BIKE" / "latest_window_summary.json").exists()
    assert (data_dir / "companies" / "BIRD" / "latest_window_summary.json").exists()
    assert (data_dir / "share" / "latest-all-companies.md").exists()
    assert (data_dir / "share" / "latest-all-companies.txt").exists()
    combined = (data_dir / "share" / "latest-all-companies.md").read_text(encoding="utf-8")
    assert "## ANTM" in combined and "## BIKE" in combined and "## BIRD" in combined
    assert report["share_exports"]["markdown"].endswith("latest-all-companies.md")
    summaries = db.company_window_summary_map(start, end)
    assert set(summaries) == {"ANTM", "BIKE", "BIRD"}
    assert summaries["BIKE"]["_checkpoint"]["legacy_announcement_summaries_used"] == 1


def test_reducer_force_rebuilds_existing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, openrouter_api_key="unused", llm_concurrency=1)
    settings.ensure_directories()
    db = Database(settings.database_path)
    start = "2026-08-01T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    seed_announcement(db, "a1", "ANTM", "2026-08-02T10:00:00+07:00")
    db.save_company_summary(
        "ANTM", start, end, existing_company_payload("ANTM", start, end), "old", "old"
    )
    fake = FakeSummarizer()
    report = CachedCompanyReducer(settings, summarizer=fake).reduce(
        start_at=start, end_at=end, force=True
    )
    assert report["completed_tickers"] == ["ANTM"]
    assert report["skipped_existing"] == []
    assert db.company_window_summary_map(start, end)["ANTM"]["overview"] == "Digest ANTM"


def test_reduce_cached_cli_is_registered() -> None:
    from typer.testing import CliRunner
    from idx_digest.cli import app

    result = CliRunner().invoke(app, ["reduce-cached", "--help"])
    assert result.exit_code == 0
    assert "Finish company digests from committed announcement summaries only" in result.output
