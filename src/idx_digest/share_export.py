from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

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
class ShareBundle:
    start_at: str
    end_at: str
    ticker_filter: str | None
    sections: tuple[str, ...]
    companies: dict[str, dict[str, Any]]

    @property
    def company_count(self) -> int:
        return len(self.companies)


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


def render_markdown(bundle: ShareBundle, *, title: str = "IDX Signal Desk · Company Digests") -> str:
    lines = [
        f"# {title}",
        "",
        f"**Window:** {bundle.start_at} → {bundle.end_at}",
        f"**Companies:** {bundle.company_count}",
        "",
        "> Each ticker below is rendered from its own saved company digest. No cross-company LLM aggregation is performed.",
        "",
    ]
    for ticker, summary in bundle.companies.items():
        lines.extend(["---", "", f"## {ticker}", ""])
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
    payload = {
        "start_at": bundle.start_at,
        "end_at": bundle.end_at,
        "ticker_filter": bundle.ticker_filter,
        "company_count": bundle.company_count,
        "sections": list(bundle.sections),
        "companies": bundle.companies,
    }
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


def default_share_filename(fmt: str, *, ticker: str | None = None) -> str:
    normalized = fmt.strip().lower()
    extension = "md" if normalized in {"md", "markdown"} else "txt" if normalized in {"txt", "text"} else "json"
    scope = (ticker or "all-companies").strip().upper() or "all-companies"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"idx-digest-{scope}-{stamp}.{extension}"
