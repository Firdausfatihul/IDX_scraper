from __future__ import annotations

from idx_digest.db import Database
from idx_digest.share_export import (
    DEFAULT_SHARE_SECTIONS,
    SIGNALS_ONLY_SECTIONS,
    load_share_bundle,
    render_markdown,
    render_text,
    write_share_export,
)


def _summary(ticker: str, action: str, expansion: str) -> dict:
    return {
        "ticker": ticker,
        "period": {"start": "2026-07-06T00:00:00+07:00", "end": "2026-08-06T23:59:59.999999+07:00"},
        "announcement_count": 1,
        "overview": f"{ticker} overview",
        "timeline": [{"announced_at": "2026-08-01T10:00:00+07:00", "title": "Disclosure", "summary": "Timeline detail"}],
        "material_changes": [f"{ticker} material change"],
        "key_financial_figures": [{"metric": "Project value", "value": "USD 10m", "period": None}],
        "corporate_actions": [action],
        "expansion_projects": [expansion],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [{
            "classification": "analyst_hypothesis",
            "topic": "Funding",
            "analysis": "Funding route remains uncertain.",
            "basis": ["Capex disclosed"],
            "assumptions": [],
            "confidence": "low",
            "caveats": ["No financing decision disclosed"],
        }],
        "risks_or_uncertainties": ["Execution risk"],
        "items_to_monitor": ["Funding announcement"],
        "limitations": ["One announcement only"],
    }


def test_share_export_appends_companies_without_cross_company_reduction(tmp_path) -> None:
    db_path = tmp_path / "data" / "idx_digest.sqlite3"
    db = Database(db_path)
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    db.save_company_summary("BIRD", start, end, _summary("BIRD", "BIRD action", "BIRD expansion"), "model")
    db.save_company_summary("ANTM", start, end, _summary("ANTM", "ANTM action", "ANTM expansion"), "model")

    bundle = load_share_bundle(db_path, start_at=start, end_at=end, sections=DEFAULT_SHARE_SECTIONS)
    text = render_markdown(bundle)

    assert bundle.company_count == 2
    assert text.index("## ANTM") < text.index("## BIRD")
    assert "ANTM action" in text and "BIRD action" in text
    assert "No cross-company LLM aggregation" in text
    assert "### Timeline" not in text
    assert "### Limitations" not in text


def test_signals_only_and_ticker_filter(tmp_path) -> None:
    db_path = tmp_path / "data" / "idx_digest.sqlite3"
    db = Database(db_path)
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    db.save_company_summary("ANTM", start, end, _summary("ANTM", "Bonus shares", "New plant"), "model")
    db.save_company_summary("BIRD", start, end, _summary("BIRD", "Other action", "Fleet expansion"), "model")

    bundle = load_share_bundle(
        db_path,
        start_at=start,
        end_at=end,
        ticker="antm",
        sections=SIGNALS_ONLY_SECTIONS,
    )
    text = render_text(bundle)
    assert bundle.company_count == 1
    assert "ANTM" in text and "BIRD" not in text
    assert "Bonus shares" in text and "New plant" in text
    assert "Material changes" not in text
    assert "Risks / uncertainties" not in text


def test_share_export_write_is_atomic_and_friend_ready(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite3"
    db = Database(db_path)
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    db.save_company_summary("PACK", start, end, _summary("PACK", "Action", "Expansion"), "model")
    bundle = load_share_bundle(db_path, start_at=start, end_at=end)
    target = tmp_path / "share" / "latest-all-companies.md"
    write_share_export(bundle, target, fmt="md")
    assert target.exists()
    assert "## PACK" in target.read_text(encoding="utf-8")
    assert not target.with_name(f".{target.name}.tmp").exists()


def test_export_all_cli_writes_combined_file(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner
    from idx_digest.cli import app

    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "data" / "idx_digest.sqlite3")
    start = "2026-07-06T00:00:00+07:00"
    end = "2026-08-06T23:59:59.999999+07:00"
    db.save_company_summary("ANTM", start, end, _summary("ANTM", "Action", "Expansion"), "model")
    target = tmp_path / "friend.md"
    result = CliRunner().invoke(
        app,
        [
            "export-all",
            "--start", "2026-07-06",
            "--end", "2026-08-06",
            "--format", "md",
            "--destination", str(target),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert target.exists()
    assert "## ANTM" in target.read_text(encoding="utf-8")
    assert '"llm_calls": 0' in result.stdout
