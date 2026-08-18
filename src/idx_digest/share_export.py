from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse

from .db import Database

SECTION_ORDER = (
    "overview",
    "timeline",
    "material_changes",
    "key_financial_figures",
    "corporate_actions",
    "expansion_projects",
    "management_or_control_changes",
    "capital_structure_events",
    "listing_or_regulatory_events",
    "analytical_scenarios",
    "risks_or_uncertainties",
    "items_to_monitor",
    "limitations",
)

SECTION_LABELS = {
    "overview": "Overview",
    "timeline": "Timeline",
    "material_changes": "Material changes",
    "key_financial_figures": "Key financial figures",
    "corporate_actions": "Corporate actions",
    "expansion_projects": "Expansion projects",
    "management_or_control_changes": "Management / control changes",
    "capital_structure_events": "Capital structure",
    "listing_or_regulatory_events": "Listing / regulatory",
    "analytical_scenarios": "Analytical scenarios",
    "risks_or_uncertainties": "Risks / uncertainties",
    "items_to_monitor": "Items to monitor",
    "limitations": "Limitations",
}

DEFAULT_SHARE_SECTIONS = (
    "overview",
    "material_changes",
    "key_financial_figures",
    "corporate_actions",
    "expansion_projects",
    "management_or_control_changes",
    "capital_structure_events",
    "listing_or_regulatory_events",
    "analytical_scenarios",
    "risks_or_uncertainties",
    "items_to_monitor",
)

SIGNALS_ONLY_SECTIONS = (
    "overview",
    "corporate_actions",
    "expansion_projects",
    "management_or_control_changes",
    "capital_structure_events",
    "listing_or_regulatory_events",
    "analytical_scenarios",
    "items_to_monitor",
)


@dataclass(frozen=True)
class CompanyWindow:
    """One saved company digest, tagged with the exact window it was built for."""

    ticker: str
    start_at: str
    end_at: str
    summary: dict[str, Any]
    updated_at: str | None = None


@dataclass(frozen=True)
class ShareBundle:
    start_at: str
    end_at: str
    ticker_filter: str | None
    sections: tuple[str, ...]
    companies: dict[str, dict[str, Any]]
    windows: tuple[CompanyWindow, ...] = ()
    date_mode: str = "exact"
    window_keys: tuple[tuple[str, str], ...] = ()
    # What the caller asked for, when that differs from what the saved windows cover.
    requested_start: str | None = None
    requested_end: str | None = None

    @property
    def company_count(self) -> int:
        return len(self.companies)

    @property
    def digest_windows(self) -> tuple[CompanyWindow, ...]:
        """Every digest to render, in ticker order. Falls back to the exact-window map."""
        if self.windows:
            return self.windows
        return tuple(
            CompanyWindow(ticker=ticker, start_at=self.start_at, end_at=self.end_at, summary=summary)
            for ticker, summary in self.companies.items()
        )


def normalize_sections(sections: Iterable[str] | None) -> tuple[str, ...]:
    if sections is None:
        return DEFAULT_SHARE_SECTIONS
    requested = []
    for raw in sections:
        key = str(raw).strip()
        if not key or key in requested:
            continue
        if key not in SECTION_ORDER:
            raise ValueError(f"Unknown share section: {key}")
        requested.append(key)
    if not requested:
        raise ValueError("At least one share section is required")
    return tuple(key for key in SECTION_ORDER if key in requested)


def load_share_bundle(
    database_path: Path,
    *,
    start_at: str,
    end_at: str,
    ticker: str | None = None,
    sections: Iterable[str] | None = None,
) -> ShareBundle:
    normalized_ticker = (ticker or "").strip().upper() or None
    summaries = Database(database_path).company_window_summary_map(
        start_at,
        end_at,
        ticker=normalized_ticker,
    )
    companies = {
        str(symbol).strip().upper(): payload
        for symbol, payload in summaries.items()
        if str(symbol).strip()
    }
    return ShareBundle(
        start_at=start_at,
        end_at=end_at,
        ticker_filter=normalized_ticker,
        sections=normalize_sections(sections),
        companies=dict(sorted(companies.items())),
    )


def _instant(value: Any, assume_timezone: str = "UTC") -> datetime | None:
    """Parse a stored ISO boundary into an aware datetime, or None when unusable.

    Saved boundaries are written by ``parse_boundary`` and therefore always carry an
    offset; ``assume_timezone`` only covers hand-edited or legacy rows that lost theirs.
    """
    if value in (None, ""):
        return None
    try:
        parsed = isoparse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(assume_timezone))
    return parsed


def select_saved_windows(
    index: Iterable[dict[str, Any]],
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    assume_timezone: str = "UTC",
) -> list[dict[str, Any]]:
    """Saved windows that overlap the requested span. Either bound may be omitted.

    Digests exist only for whole saved windows, so a range selection can never slice a
    window in half — it can only include or exclude one. Overlap (rather than strict
    containment) is used so a picked range still returns the digests that describe it.
    """
    lower = _instant(start_at, assume_timezone)
    upper = _instant(end_at, assume_timezone)
    selected: list[dict[str, Any]] = []
    for window in index:
        window_start = _instant(window.get("start_at"), assume_timezone)
        window_end = _instant(window.get("end_at"), assume_timezone)
        if window_start is None or window_end is None:
            continue
        if upper is not None and window_start > upper:
            continue
        if lower is not None and window_end < lower:
            continue
        selected.append(dict(window))
    return selected


def _latest_coverage_first(row: dict[str, Any]) -> tuple[str, str, str]:
    """Rank a company's saved windows by the period they describe, newest first.

    Coverage, not generation time: a window regenerated later can still describe an
    older period, and a reader wants the most recent period.
    """
    return (
        str(row.get("end_at") or ""),
        str(row.get("start_at") or ""),
        str(row.get("updated_at") or ""),
    )


def load_share_bundle_range(
    database_path: Path,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    ticker: str | None = None,
    sections: Iterable[str] | None = None,
    per_ticker: str = "latest",
    assume_timezone: str = "UTC",
    window_keys: Iterable[tuple[str, str]] | None = None,
    date_mode: str | None = None,
) -> ShareBundle:
    """Bundle saved digests for whole saved windows, never a slice of one.

    Pass ``window_keys`` to export exactly those saved windows. Otherwise every window
    overlapping ``start_at``..``end_at`` is taken, and omitting both bounds takes every
    saved date. With ``per_ticker="latest"`` a company appears once, using the selected
    window that covers the most recent period; ``per_ticker="all"`` keeps them all.
    """
    if per_ticker not in {"latest", "all"}:
        raise ValueError(f"Unknown per_ticker mode: {per_ticker}")
    normalized_ticker = (ticker or "").strip().upper() or None
    normalized_sections = normalize_sections(sections)
    database = Database(database_path)
    index = database.saved_window_index()
    if window_keys is None:
        matched = select_saved_windows(
            index,
            start_at=start_at,
            end_at=end_at,
            assume_timezone=assume_timezone,
        )
    else:
        # Only keys that really exist, so a caller cannot widen the export by inventing one.
        wanted = {(str(start), str(end)) for start, end in window_keys}
        matched = [item for item in index if (str(item["start_at"]), str(item["end_at"])) in wanted]
    selected_keys = tuple((str(item["start_at"]), str(item["end_at"])) for item in matched)
    rows = database.company_summaries_for_windows(selected_keys, ticker=normalized_ticker)

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("ticker") or "").strip().upper()
        if not symbol:
            continue
        by_ticker.setdefault(symbol, []).append(row)

    windows: list[CompanyWindow] = []
    companies: dict[str, dict[str, Any]] = {}
    for symbol in sorted(by_ticker):
        saved = sorted(by_ticker[symbol], key=_latest_coverage_first, reverse=True)
        chosen = saved[:1] if per_ticker == "latest" else saved
        companies[symbol] = chosen[0]["summary"]
        for row in chosen:
            windows.append(
                CompanyWindow(
                    ticker=symbol,
                    start_at=str(row["start_at"]),
                    end_at=str(row["end_at"]),
                    summary=row["summary"],
                    updated_at=row.get("updated_at"),
                )
            )

    # The bundle advertises what the exported digests actually cover, not what was asked
    # for: overlap selection routinely pulls in a window reaching outside the picked range,
    # and a ticker filter can leave some selected windows contributing nothing.
    contributing = tuple(sorted({(window.start_at, window.end_at) for window in windows}))
    covered_start = min((key[0] for key in contributing), default="")
    covered_end = max((key[1] for key in contributing), default="")
    return ShareBundle(
        start_at=covered_start,
        end_at=covered_end,
        ticker_filter=normalized_ticker,
        sections=normalized_sections,
        companies=companies,
        windows=tuple(windows),
        date_mode=date_mode or ("all" if start_at is None and end_at is None else "range"),
        window_keys=contributing,
        requested_start=start_at,
        requested_end=end_at,
    )


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _markdown_list(values: list[Any]) -> list[str]:
    lines: list[str] = []
    for value in values:
        if isinstance(value, dict):
            lines.append(f"- `{json.dumps(value, ensure_ascii=False)}`")
        else:
            text = _scalar_text(value)
            if text:
                lines.append(f"- {text}")
    return lines


def _render_section_markdown(key: str, value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    label = SECTION_LABELS[key]
    lines = [f"### {label}", ""]

    if key == "overview":
        text = _scalar_text(value)
        return lines + ([text, ""] if text else [])

    if key == "timeline":
        items = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            when = _scalar_text(item.get("announced_at"))
            title = _scalar_text(item.get("title"))
            summary = _scalar_text(item.get("summary"))
            head = " · ".join(part for part in (when, title) if part)
            if summary:
                items.append(f"- **{head or 'Announcement'}:** {summary}")
            elif head:
                items.append(f"- **{head}**")
        return lines + items + ([""] if items else [])

    if key == "key_financial_figures":
        items = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            metric = _scalar_text(item.get("metric")) or "Figure"
            amount = _scalar_text(item.get("value"))
            period = _scalar_text(item.get("period"))
            suffix = f" ({period})" if period else ""
            if amount:
                items.append(f"- **{metric}:** {amount}{suffix}")
        return lines + items + ([""] if items else [])

    if key == "analytical_scenarios":
        items: list[str] = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            classification = _scalar_text(item.get("classification"))
            confidence = _scalar_text(item.get("confidence"))
            topic = _scalar_text(item.get("topic")) or "Scenario"
            analysis = _scalar_text(item.get("analysis"))
            header_bits = [bit for bit in (classification, confidence) if bit]
            header = f" [{', '.join(header_bits)}]" if header_bits else ""
            items.append(f"- **{topic}**{header}: {analysis}".rstrip())
            basis = [str(x).strip() for x in (item.get("basis") or []) if str(x).strip()]
            assumptions = [str(x).strip() for x in (item.get("assumptions") or []) if str(x).strip()]
            caveats = [str(x).strip() for x in (item.get("caveats") or []) if str(x).strip()]
            if basis:
                items.append(f"  - Basis: {'; '.join(basis)}")
            if assumptions:
                items.append(f"  - Assumptions: {'; '.join(assumptions)}")
            if caveats:
                items.append(f"  - Caveats: {'; '.join(caveats)}")
        return lines + items + ([""] if items else [])

    if isinstance(value, list):
        items = _markdown_list(value)
        return lines + items + ([""] if items else [])

    if isinstance(value, dict):
        return lines + ["```json", json.dumps(value, ensure_ascii=False, indent=2), "```", ""]

    text = _scalar_text(value)
    return lines + ([text, ""] if text else [])


def boundary_label(value: Any) -> str:
    """Friend-readable boundary: date alone for whole-day edges, else date plus clock."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[10:11] == "T":
        return text[:10] if text[11:16] in {"00:00", "23:59"} else f"{text[:10]} {text[11:16]}"
    return text or "—"


def window_label(start_at: Any, end_at: Any) -> str:
    return f"{boundary_label(start_at)} → {boundary_label(end_at)}"


def _header_lines(bundle: ShareBundle) -> list[str]:
    if bundle.date_mode == "exact":
        return [f"**Window:** {bundle.start_at} → {bundle.end_at}"]
    covered = window_label(bundle.start_at, bundle.end_at)
    scope = f"all saved dates · {covered}" if bundle.date_mode == "all" else covered
    lines = [f"**Covered:** {scope}"]
    if bundle.requested_start or bundle.requested_end:
        requested = window_label(bundle.requested_start, bundle.requested_end)
        if requested != covered:
            lines.append(f"**Dates picked:** {requested} (saved digests cover whole run windows)")
    keys = bundle.window_keys
    if keys:
        shown = ", ".join(window_label(start, end) for start, end in keys[:6])
        overflow = f", +{len(keys) - 6} more" if len(keys) > 6 else ""
        lines.append(f"**Saved windows:** {len(keys)} ({shown}{overflow})")
    return lines


def render_markdown(bundle: ShareBundle, *, title: str = "IDX Signal Desk · Company Digests") -> str:
    lines = [
        f"# {title}",
        "",
        *_header_lines(bundle),
        f"**Companies:** {bundle.company_count}",
        "",
        "> Each ticker below is rendered from its own saved company digest. No cross-company LLM aggregation is performed.",
        "",
    ]
    digests = bundle.digest_windows
    seen: set[str] = set()
    for digest in digests:
        summary = digest.summary
        if digest.ticker not in seen:
            seen.add(digest.ticker)
            lines.extend(["---", "", f"## {digest.ticker}", ""])
        # Outside exact mode a reader cannot assume the header span applies to this ticker,
        # so every digest states the period it was actually built from.
        if bundle.date_mode != "exact":
            lines.extend([f"**Window:** {window_label(digest.start_at, digest.end_at)}", ""])
        announcement_count = summary.get("announcement_count")
        if announcement_count is not None:
            lines.extend([f"**Announcements:** {announcement_count}", ""])
        for key in bundle.sections:
            lines.extend(_render_section_markdown(key, summary.get(key)))
    return "\n".join(lines).rstrip() + "\n"


def render_text(bundle: ShareBundle, *, title: str = "IDX Signal Desk · Company Digests") -> str:
    markdown = render_markdown(bundle, title=title)
    lines: list[str] = []
    in_code = False
    for raw in markdown.splitlines():
        line = raw
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            if line.startswith("# "):
                line = line[2:].upper()
            elif line.startswith("## "):
                line = line[3:].upper()
            elif line.startswith("### "):
                line = line[4:]
            line = line.replace("**", "").replace("`", "")
            if line.startswith("> "):
                line = line[2:]
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def render_json(bundle: ShareBundle) -> str:
    payload: dict[str, Any] = {
        "start_at": bundle.start_at,
        "end_at": bundle.end_at,
        "ticker_filter": bundle.ticker_filter,
        "company_count": bundle.company_count,
        "sections": list(bundle.sections),
        "companies": bundle.companies,
    }
    if bundle.date_mode != "exact":
        payload["date_mode"] = bundle.date_mode
        payload["requested_start"] = bundle.requested_start
        payload["requested_end"] = bundle.requested_end
        payload["saved_windows"] = [
            {"start_at": start, "end_at": end} for start, end in bundle.window_keys
        ]
        payload["digests"] = [
            {
                "ticker": digest.ticker,
                "start_at": digest.start_at,
                "end_at": digest.end_at,
                "updated_at": digest.updated_at,
                "summary": digest.summary,
            }
            for digest in bundle.digest_windows
        ]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_bundle(bundle: ShareBundle, fmt: str) -> str:
    normalized = fmt.strip().lower()
    if normalized in {"md", "markdown"}:
        return render_markdown(bundle)
    if normalized in {"txt", "text"}:
        return render_text(bundle)
    if normalized == "json":
        return render_json(bundle)
    raise ValueError(f"Unsupported share format: {fmt}")


def write_share_export(
    bundle: ShareBundle,
    destination: Path,
    *,
    fmt: str = "md",
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = render_bundle(bundle, fmt)
    temp = destination.with_name(f".{destination.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(destination)
    return destination


def refresh_latest_share_exports(
    database_path: Path,
    data_dir: Path,
    *,
    start_at: str,
    end_at: str,
    ticker: str | None = None,
) -> dict[str, str]:
    """Atomically refresh the standard Markdown and text share snapshots."""

    normalized_ticker = (ticker or "").strip().upper() or None
    bundle = load_share_bundle(
        database_path,
        start_at=start_at,
        end_at=end_at,
        ticker=normalized_ticker,
        sections=DEFAULT_SHARE_SECTIONS,
    )
    if not bundle.companies:
        return {}

    suffix = f"-{normalized_ticker}" if normalized_ticker else "-all-companies"
    share_dir = data_dir / "share"
    paths = {
        "markdown": share_dir / f"latest{suffix}.md",
        "text": share_dir / f"latest{suffix}.txt",
    }
    write_share_export(bundle, paths["markdown"], fmt="md")
    write_share_export(bundle, paths["text"], fmt="txt")
    return {key: str(path) for key, path in paths.items()}


def default_share_filename(
    fmt: str,
    *,
    ticker: str | None = None,
    dates: str | None = None,
) -> str:
    normalized = fmt.strip().lower()
    extension = "md" if normalized in {"md", "markdown"} else "txt" if normalized in {"txt", "text"} else "json"
    scope = (ticker or "all-companies").strip().upper() or "all-companies"
    window = f"-{dates.strip()}" if (dates or "").strip() else ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"idx-digest-{scope}{window}-{stamp}.{extension}"


def share_filename_dates(bundle: ShareBundle) -> str:
    """Filename-safe date scope, e.g. ``20260810-to-20260815`` or ``all-dates``."""
    if bundle.date_mode == "all":
        return "all-dates"
    if bundle.date_mode == "exact":
        return ""
    start = boundary_label(bundle.start_at)[:10].replace("-", "")
    end = boundary_label(bundle.end_at)[:10].replace("-", "")
    if not start.isdigit() or not end.isdigit():
        return ""
    return start if start == end else f"{start}-to-{end}"
