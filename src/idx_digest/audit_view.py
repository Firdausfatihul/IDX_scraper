from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .db import Database
from .prompts import PromptStore


CLAIM_FIELDS = (
    ("material_changes", "material_change"),
    ("key_financial_figures", "financial_figure"),
    ("corporate_actions", "corporate_action"),
    ("expansion_projects", "expansion_project"),
    ("management_or_control_changes", "management_or_control_change"),
    ("capital_structure_events", "capital_structure_event"),
    ("listing_or_regulatory_events", "listing_or_regulatory_event"),
    ("analytical_scenarios", "analytical_scenario"),
    ("risks_or_uncertainties", "risk_or_uncertainty"),
    ("items_to_monitor", "item_to_monitor"),
)

_STOP = {
    "yang", "dan", "atau", "dari", "pada", "untuk", "dengan", "dalam", "ini", "itu",
    "the", "and", "for", "from", "with", "this", "that", "sebagai", "akan", "telah",
    "oleh", "serta", "atas", "ke", "di", "a", "an", "of", "to", "is", "are",
}


def _claim_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if value.get("analysis"):
            return str(value["analysis"]).strip()
        if value.get("metric") or value.get("value"):
            return " — ".join(str(x) for x in (value.get("metric"), value.get("value"), value.get("period")) if x)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9._%/-]*", text.casefold())
        if len(token) > 2 and token not in _STOP
    }


def _match_score(claim: str, summary: dict[str, Any] | None) -> float:
    if not summary:
        return 0.0
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return 0.0
    source_text = json.dumps(summary, ensure_ascii=False)
    source_tokens = _tokens(source_text)
    shared = claim_tokens & source_tokens
    return len(shared) / max(1, len(claim_tokens))


def _source_locator(text_path: str | None, evidence: str) -> dict[str, Any] | None:
    if not text_path or not evidence:
        return None
    path = Path(text_path)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    pos = text.casefold().find(evidence.strip().casefold())
    if pos < 0:
        # Evidence is model-extracted and can differ slightly in spacing. Fall back
        # to a distinctive prefix, but do not pretend it is an exact match.
        prefix = " ".join(evidence.split())[:80].casefold()
        compact = " ".join(text.split()).casefold()
        compact_pos = compact.find(prefix) if len(prefix) >= 20 else -1
        if compact_pos < 0:
            return {"match": "not_located"}
        return {"match": "normalized_text", "page": None, "line": None}
    line = text.count("\n", 0, pos) + 1
    before = text[:pos]
    pages = list(re.finditer(r"===== PAGE (\d+) =====", before))
    sheets = list(re.finditer(r"===== SHEET: (.*?) =====", before))
    locator: dict[str, Any] = {"match": "exact_text", "line": line}
    if pages:
        locator["page"] = int(pages[-1].group(1))
    if sheets:
        locator["sheet"] = sheets[-1].group(1).strip()
    return locator


def _enrich_attachment(attachment: dict[str, Any]) -> dict[str, Any]:
    item = dict(attachment)
    summary = item.get("document_summary") or {}
    evidence_rows = []
    for evidence in summary.get("source_evidence") or []:
        if not isinstance(evidence, dict):
            continue
        row = dict(evidence)
        row["locator"] = _source_locator(item.get("extracted_text_path"), str(evidence.get("evidence") or ""))
        evidence_rows.append(row)
    item["source_evidence_located"] = evidence_rows
    return item


def _claim_traces(company_summary: dict[str, Any] | None, announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not company_summary:
        return []
    by_id = {str(a.get("id2")): a for a in announcements}
    explicit = company_summary.get("claim_sources") or []
    explicit_map: dict[str, dict[str, Any]] = {}
    for ref in explicit:
        if isinstance(ref, dict) and ref.get("claim"):
            explicit_map[str(ref["claim"]).strip()] = ref

    traces: list[dict[str, Any]] = []
    for field, category in CLAIM_FIELDS:
        values = company_summary.get(field) or []
        if not isinstance(values, list):
            continue
        for value in values:
            claim = _claim_text(value)
            if not claim:
                continue
            classification = value.get("classification") if isinstance(value, dict) else "explicit_fact"
            ref = explicit_map.get(claim)
            ids: list[str] = []
            attribution = "unresolved"
            score: float | None = None
            rationale = ""
            if ref:
                ids = [str(x) for x in ref.get("announcement_ids") or [] if str(x) in by_id]
                attribution = "exact_model_reference" if ids else "unresolved"
                classification = ref.get("classification") or classification
                rationale = str(ref.get("rationale") or "")
            elif len(announcements) == 1:
                ids = [str(announcements[0].get("id2"))]
                attribution = "deterministic_single_announcement"
                score = 1.0
            elif announcements:
                scored = sorted(
                    [(_match_score(claim, a.get("announcement_summary")), str(a.get("id2"))) for a in announcements],
                    reverse=True,
                )
                if scored and scored[0][0] >= 0.20:
                    score = scored[0][0]
                    ids = [scored[0][1]]
                    if len(scored) > 1 and scored[1][0] >= max(0.20, score - 0.08):
                        ids.append(scored[1][1])
                    attribution = "inferred_from_saved_summaries"
            sources = []
            for announcement_id in ids:
                a = by_id.get(announcement_id)
                if not a:
                    continue
                sources.append({
                    "announcement_id": announcement_id,
                    "announced_at": a.get("announced_at"),
                    "title": a.get("title"),
                    "attachments": [
                        {
                            "url": x.get("url"),
                            "filename": x.get("original_filename"),
                            "is_attachment": bool(x.get("is_attachment")),
                            "content_type": x.get("content_type"),
                        }
                        for x in a.get("attachments") or []
                        if bool(x.get("selected_for_analysis", 1))
                    ],
                })
            traces.append({
                "field": field,
                "category": category,
                "claim": claim,
                "classification": classification or "explicit_fact",
                "attribution": attribution,
                "match_score": score,
                "rationale": rationale,
                "sources": sources,
            })
    return traces


def build_company_audit_view(
    database_path: Path,
    prompt_config_path: Path,
    *,
    ticker: str,
    start_at: str,
    end_at: str,
) -> dict[str, Any]:
    db = Database(database_path)
    data = db.company_audit_bundle(ticker, start_at, end_at)
    data["announcements"] = [
        {**a, "attachments": [_enrich_attachment(x) for x in a.get("attachments") or []]}
        for a in data["announcements"]
    ]
    data["claim_traces"] = _claim_traces(data.get("company_summary"), data["announcements"])

    exact_company_audits = [
        row for row in data.get("llm_audits") or []
        if row.get("stage") == "company" and row.get("status") == "succeeded"
    ]
    if exact_company_audits:
        latest_meta = exact_company_audits[-1]
        latest = db.llm_audit(int(latest_meta["audit_id"])) or latest_meta
        data["company_prompt"] = {
            "status": "exact_audit",
            "audit_id": latest.get("audit_id"),
            "system_prompt": latest.get("system_prompt"),
            "user_prompt": latest.get("user_prompt"),
            "raw_response": latest.get("raw_response"),
            "started_at": latest.get("started_at"),
            "finished_at": latest.get("finished_at"),
            "elapsed_seconds": latest.get("elapsed_seconds"),
            "prompt_tokens": latest.get("prompt_tokens"),
            "completion_tokens": latest.get("completion_tokens"),
            "total_tokens": latest.get("total_tokens"),
            "prompt_version": latest.get("prompt_version"),
            "prompt_profile": latest.get("prompt_profile"),
        }
    else:
        prompt_bundle = PromptStore(prompt_config_path).load()
        legacy_v3 = prompt_bundle.layer_version("company", schema_version="company-window-v3")
        metadata = data.get("company_metadata") or {}
        records = []
        for a in data["announcements"]:
            if a.get("announcement_summary"):
                records.append({
                    "announcement_id": a.get("id2"),
                    "announced_at": a.get("announced_at"),
                    "title": a.get("title"),
                    "summary": a.get("announcement_summary"),
                    "source_model": a.get("summary_model"),
                    "source_prompt_version": a.get("summary_prompt_version"),
                })
        reconstructed = prompt_bundle.render(
            "company",
            ticker=data["ticker"],
            start_at=start_at,
            end_at=end_at,
            announcements_json=json.dumps(records, ensure_ascii=False),
        )
        status = "reconstructed_legacy_template" if metadata.get("prompt_version") == legacy_v3 else "reconstructed_current_template"
        data["company_prompt"] = {
            "status": status,
            "notice": "Historical raw prompts were not stored. This prompt is reconstructed from committed checkpoint inputs.",
            "system_prompt": prompt_bundle.prompts["system"],
            "user_prompt": reconstructed,
            "raw_response": json.dumps(data.get("company_summary"), ensure_ascii=False, indent=2) if data.get("company_summary") else None,
            "prompt_version": metadata.get("prompt_version"),
            "prompt_profile": prompt_bundle.profile_name,
        }

    # Build a compact, database-derived ticker process timeline. Exact new LLM
    # audits augment this with request start/end and token usage.
    process: list[dict[str, Any]] = []
    for a in data["announcements"]:
        process.append({"time": a.get("announced_at"), "stage": "idx-announced", "label": a.get("title"), "announcement_id": a.get("id2")})
        process.append({"time": a.get("fetched_at"), "stage": "metadata-saved", "label": a.get("title"), "announcement_id": a.get("id2")})
        for attachment in a.get("attachments") or []:
            if attachment.get("downloaded_at"):
                process.append({"time": attachment.get("downloaded_at"), "stage": "downloaded", "label": attachment.get("original_filename"), "announcement_id": a.get("id2"), "url": attachment.get("url")})
            if attachment.get("extracted_at"):
                process.append({"time": attachment.get("extracted_at"), "stage": "extracted", "label": attachment.get("original_filename"), "announcement_id": a.get("id2"), "url": attachment.get("url")})
            if attachment.get("document_summary_updated_at"):
                process.append({
                    "time": attachment.get("document_summary_updated_at"),
                    "stage": "document-summary",
                    "label": attachment.get("original_filename"),
                    "announcement_id": a.get("id2"),
                    "url": attachment.get("url"),
                })
        if a.get("summary_updated_at"):
            process.append({"time": a.get("summary_updated_at"), "stage": "announcement-summary", "label": a.get("title"), "announcement_id": a.get("id2")})
    if data.get("company_metadata"):
        process.append({"time": data["company_metadata"].get("updated_at"), "stage": "company-summary", "label": f"{data['ticker']} company digest"})
    for audit in data.get("llm_audits") or []:
        process.append({
            "time": audit.get("started_at"), "end_time": audit.get("finished_at"),
            "stage": f"llm-{audit.get('stage')}",
            "label": audit.get("filename") or audit.get("announcement_id") or data["ticker"],
            "status": audit.get("status"), "elapsed_seconds": audit.get("elapsed_seconds"),
            "prompt_tokens": audit.get("prompt_tokens"), "completion_tokens": audit.get("completion_tokens"),
            "audit_id": audit.get("audit_id"),
        })
    data["process_timeline"] = sorted(process, key=lambda x: str(x.get("time") or ""))
    return data
