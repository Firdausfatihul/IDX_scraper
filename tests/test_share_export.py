from __future__ import annotations

from idx_digest.db import Database
from idx_digest.share_export import (
    DEFAULT_SHARE_SECTIONS,
    SIGNALS_ONLY_SECTIONS,
    load_share_bundle,
    load_share_bundle_range,
    render_markdown,
    render_text,
    select_saved_windows,
    share_filename_dates,
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


JULY = ("2026-07-06T00:00:00+07:00", "2026-08-06T23:59:59.999999+07:00")
EARLY_AUGUST = ("2026-08-10T00:00:00+07:00", "2026-08-11T23:59:00+07:00")
MID_AUGUST = ("2026-08-10T00:00:00+07:00", "2026-08-15T19:59:00+07:00")


def _seed_multi_window(db_path):
    """Three saved windows, mirroring how real profiles accumulate overlapping runs."""
    db = Database(db_path)
    db.save_company_summary("ANTM", *JULY, _summary("ANTM", "July action", "July expansion"), "model")
    db.save_company_summary("ANTM", *EARLY_AUGUST, _summary("ANTM", "Early action", "Early expansion"), "model")
    db.save_company_summary("ANTM", *MID_AUGUST, _summary("ANTM", "Mid action", "Mid expansion"), "model")
    db.save_company_summary("BIRD", *EARLY_AUGUST, _summary("BIRD", "BIRD action", "BIRD expansion"), "model")
    return db


def test_select_saved_windows_uses_overlap_and_unbounded_sides(tmp_path) -> None:
    _seed_multi_window(tmp_path / "db.sqlite3")
    index = Database(tmp_path / "db.sqlite3").saved_window_index()
    assert len(index) == 3

    august = select_saved_windows(index, start_at="2026-08-12T00:00:00+07:00", end_at="2026-08-15T23:59:59+07:00")
    assert [(item["start_at"], item["end_at"]) for item in august] == [MID_AUGUST]

    both = select_saved_windows(index, start_at="2026-08-08T00:00:00+07:00", end_at="2026-08-31T23:59:59+07:00")
    assert {(item["start_at"], item["end_at"]) for item in both} == {EARLY_AUGUST, MID_AUGUST}

    assert len(select_saved_windows(index)) == 3
    assert len(select_saved_windows(index, end_at="2026-08-06T23:59:59+07:00")) == 1
    assert not select_saved_windows(index, start_at="2026-09-01T00:00:00+07:00", end_at="2026-09-30T00:00:00+07:00")


def test_range_export_keeps_one_digest_per_ticker_by_default(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _seed_multi_window(db_path)

    bundle = load_share_bundle_range(
        db_path,
        start_at="2026-08-08T00:00:00+07:00",
        end_at="2026-08-31T23:59:59+07:00",
        sections=DEFAULT_SHARE_SECTIONS,
    )
    text = render_markdown(bundle)

    assert bundle.date_mode == "range"
    assert bundle.company_count == 2
    assert len(bundle.digest_windows) == 2
    assert text.count("## ANTM") == 1
    assert "Mid action" in text and "Early action" not in text
    assert "July action" not in text
    assert "**Saved windows:** 2" in text


def test_range_export_can_keep_every_saved_window(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _seed_multi_window(db_path)

    bundle = load_share_bundle_range(
        db_path,
        start_at="2026-08-08T00:00:00+07:00",
        end_at="2026-08-31T23:59:59+07:00",
        sections=DEFAULT_SHARE_SECTIONS,
        per_ticker="all",
    )
    text = render_markdown(bundle)

    assert bundle.company_count == 2
    assert len(bundle.digest_windows) == 3
    assert text.count("## ANTM") == 1
    assert "Mid action" in text and "Early action" in text
    assert "**Window:** 2026-08-10 → 2026-08-15 19:59" in text
    assert "**Window:** 2026-08-10 → 2026-08-11" in text


def test_all_dates_export_covers_every_saved_window(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _seed_multi_window(db_path)

    bundle = load_share_bundle_range(db_path, sections=DEFAULT_SHARE_SECTIONS, per_ticker="all")
    text = render_markdown(bundle)

    assert bundle.date_mode == "all"
    assert bundle.start_at == JULY[0] and bundle.end_at == MID_AUGUST[1]
    assert len(bundle.window_keys) == 3
    assert len(bundle.digest_windows) == 4
    assert "all saved dates · 2026-07-06 → 2026-08-15 19:59" in text
    assert "July action" in text and "Early action" in text and "Mid action" in text
    assert share_filename_dates(bundle) == "all-dates"


def test_range_export_respects_ticker_filter_and_filename_dates(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _seed_multi_window(db_path)

    bundle = load_share_bundle_range(
        db_path,
        start_at="2026-08-08T00:00:00+07:00",
        end_at="2026-08-31T23:59:59+07:00",
        ticker="bird",
        sections=DEFAULT_SHARE_SECTIONS,
    )

    assert bundle.ticker_filter == "BIRD"
    assert list(bundle.companies) == ["BIRD"]
    # BIRD only exists in the early-August window, so the export advertises what it
    # actually covers rather than the wider range that was requested.
    assert bundle.window_keys == (EARLY_AUGUST,)
    assert (bundle.start_at, bundle.end_at) == EARLY_AUGUST
    assert (bundle.requested_start, bundle.requested_end) == (
        "2026-08-08T00:00:00+07:00",
        "2026-08-31T23:59:59+07:00",
    )
    assert share_filename_dates(bundle) == "20260810-to-20260811"


def test_exact_export_output_is_unchanged_by_the_range_feature(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite3"
    _seed_multi_window(db_path)
    bundle = load_share_bundle(db_path, start_at=JULY[0], end_at=JULY[1], sections=DEFAULT_SHARE_SECTIONS)
    text = render_markdown(bundle)

    assert bundle.date_mode == "exact"
    assert f"**Window:** {JULY[0]} → {JULY[1]}" in text
    assert "**Saved windows:**" not in text
    assert share_filename_dates(bundle) == ""


def test_export_all_cli_exports_every_saved_date(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner
    from idx_digest.cli import app

    monkeypatch.chdir(tmp_path)
    _seed_multi_window(tmp_path / "data" / "idx_digest.sqlite3")
    target = tmp_path / "everything.md"
    result = CliRunner().invoke(
        app,
        ["export-all", "--all-dates", "--every-window", "--format", "md", "--destination", str(target)],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text(encoding="utf-8")
    assert "July action" in text and "Early action" in text and "Mid action" in text
    assert '"date_mode": "all"' in result.stdout
    assert '"window_count": 3' in result.stdout


def test_export_all_cli_range_flag_selects_overlapping_windows(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner
    from idx_digest.cli import app

    monkeypatch.chdir(tmp_path)
    _seed_multi_window(tmp_path / "data" / "idx_digest.sqlite3")
    target = tmp_path / "august.md"
    result = CliRunner().invoke(
        app,
        [
            "export-all", "--range",
            "--start", "2026-08-12", "--end", "2026-08-20",
            "--format", "md", "--destination", str(target),
        ],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text(encoding="utf-8")
    assert "Mid action" in text
    assert "July action" not in text and "Early action" not in text
    assert '"window_count": 1' in result.stdout


def test_export_all_cli_rejects_missing_dates_without_all_dates(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner
    from idx_digest.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["export-all", "--format", "md"])
    assert result.exit_code != 0


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
