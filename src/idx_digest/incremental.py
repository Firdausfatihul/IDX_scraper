from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .summarizer import COMPANY_SCHEMA_VERSION, COMPANY_WINDOW_SCHEMA, validate_against_schema


@dataclass(frozen=True)
class CoverageRange:
    """One exact, inclusive interval proven complete by an IDX metadata poll."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        try:
            invalid = self.end < self.start
        except TypeError as exc:
            raise ValueError("coverage range boundaries must use compatible timezones") from exc
        if invalid:
            raise ValueError("coverage range end must be greater than or equal to start")

    def as_dict(self) -> dict[str, str]:
        return {"start_at": self.start.isoformat(), "end_at": self.end.isoformat()}


def normalize_coverage_ranges(ranges: Iterable[CoverageRange]) -> list[CoverageRange]:
    """Sort and merge overlapping or exactly touching coverage intervals."""

    ordered = sorted(ranges, key=lambda item: (item.start, item.end))
    if not ordered:
        return []
    merged = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = CoverageRange(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return merged


def subtract_coverage(
    requested: CoverageRange,
    covered: Iterable[CoverageRange],
) -> list[CoverageRange]:
    """Return the exact gaps in *requested* that are not already covered.

    Gap boundaries may touch a covered boundary because intervals are inclusive.
    Callers must reject rows that are already contained in ``covered``. Keeping
    the shared boundary makes the ranges practical for IDX's calendar-day API
    without inventing an artificial microsecond offset.
    """

    normalized = normalize_coverage_ranges(covered)
    missing: list[CoverageRange] = []
    cursor = requested.start
    cursor_is_covered = False
    for existing in normalized:
        if existing.end < requested.start:
            continue
        if existing.start > requested.end:
            break
        if existing.end < cursor:
            continue
        if existing.start > cursor:
            missing.append(CoverageRange(cursor, min(existing.start, requested.end)))
        cursor = max(cursor, existing.end)
        cursor_is_covered = existing.start <= cursor <= existing.end
        if cursor >= requested.end:
            return missing
    if cursor < requested.end:
        missing.append(CoverageRange(cursor, requested.end))
    elif cursor == requested.end and not cursor_is_covered:
        missing.append(CoverageRange(cursor, cursor))
    return missing


def coverage_contains(ranges: Iterable[CoverageRange], value: datetime) -> bool:
    return any(item.start <= value <= item.end for item in ranges)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def company_input_fingerprint(
    *,
    ticker: str,
    start_at: str,
    end_at: str,
    announcements: list[dict[str, Any]],
    model: str,
    prompt_version: str,
) -> str:
    payload = {
        "ticker": ticker.strip().upper(),
        "start_at": start_at,
        "end_at": end_at,
        "model": model,
        "prompt_version": prompt_version,
        "schema_version": COMPANY_SCHEMA_VERSION,
        "announcements": [
            {
                "announcement_id": str(item.get("announcement_id") or ""),
                "announced_at": str(item.get("announced_at") or ""),
                "title": str(item.get("title") or ""),
                "source_model": item.get("model") or item.get("source_model"),
                "source_prompt_version": item.get("prompt_version") or item.get("source_prompt_version"),
                "summary": item.get("summary") or {},
            }
            for item in announcements
        ],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def promote_single_announcement(
    *,
    ticker: str,
    start_at: str,
    end_at: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Promote one already-validated announcement summary into company schema.

    No analytical information is removed and no new interpretation is invented.
    This avoids a redundant LLM call when the company window contains exactly one
    valid announcement summary.
    """
    summary = dict(record.get("summary") or {})
    announcement_id = str(record.get("announcement_id") or summary.get("announcement_id") or "")
    announced_at = str(record.get("announced_at") or summary.get("announced_at") or "")
    title = str(record.get("title") or summary.get("title") or "")
    executive = str(summary.get("executive_summary") or "").strip()
    if not executive:
        raise ValueError("single-announcement promotion requires executive_summary")

    claim_sources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_claim(claim: Any, category: str, classification: str = "explicit_fact", rationale: str = "Promoted from the sole announcement summary in this company window.") -> None:
        text = str(claim or "").strip()
        if not text or not announcement_id:
            return
        key = (category, text)
        if key in seen:
            return
        seen.add(key)
        claim_sources.append({
            "claim": text,
            "category": category,
            "classification": classification if classification in {"explicit_fact", "derived_calculation", "analyst_hypothesis"} else "explicit_fact",
            "announcement_ids": [announcement_id],
            "rationale": rationale,
        })

    mapping = (
        ("material_facts", "material_change"),
        ("corporate_actions", "corporate_action"),
        ("expansion_projects", "expansion_project"),
        ("management_or_control_changes", "management_or_control_change"),
        ("capital_structure_events", "capital_structure_event"),
        ("listing_or_regulatory_events", "listing_or_regulatory_event"),
        ("risks_or_uncertainties", "risk_or_uncertainty"),
        ("possible_investor_relevance", "item_to_monitor"),
    )
    for field, category in mapping:
        for value in summary.get(field) or []:
            add_claim(value, category)
    for item in summary.get("analytical_scenarios") or []:
        if isinstance(item, dict):
            add_claim(
                item.get("statement"),
                "analytical_scenario",
                str(item.get("classification") or "explicit_fact"),
                str(item.get("basis") or "Promoted from the sole announcement analytical scenario."),
            )

    result = {
        "ticker": ticker.strip().upper(),
        "period": {"start": start_at, "end": end_at},
        "announcement_count": 1,
        "overview": executive,
        "timeline": [{"announced_at": announced_at, "title": title, "summary": executive}],
        "material_changes": list(summary.get("material_facts") or []),
        "key_financial_figures": list(summary.get("financial_figures") or []),
        "corporate_actions": list(summary.get("corporate_actions") or []),
        "expansion_projects": list(summary.get("expansion_projects") or []),
        "management_or_control_changes": list(summary.get("management_or_control_changes") or []),
        "capital_structure_events": list(summary.get("capital_structure_events") or []),
        "listing_or_regulatory_events": list(summary.get("listing_or_regulatory_events") or []),
        "analytical_scenarios": list(summary.get("analytical_scenarios") or []),
        "risks_or_uncertainties": list(summary.get("risks_or_uncertainties") or []),
        "items_to_monitor": list(summary.get("possible_investor_relevance") or []),
        "claim_sources": claim_sources,
        "limitations": list(summary.get("limitations") or []),
    }
    validate_against_schema(result, COMPANY_WINDOW_SCHEMA)
    return result
