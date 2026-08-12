from __future__ import annotations

from typing import Any


class SummaryError(RuntimeError):
    pass


DOCUMENT_SCHEMA_VERSION = "document-v3"
ANNOUNCEMENT_SCHEMA_VERSION = "announcement-v3"
COMPANY_SCHEMA_VERSION = "company-window-v4-provenance"

NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
STRING_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

FINANCIAL_FIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {"type": "string"},
        "value": {"type": "string"},
        "period": NULLABLE_STRING,
    },
    "required": ["metric", "value", "period"],
    "additionalProperties": False,
}

DATE_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"date": {"type": "string"}, "event": {"type": "string"}},
    "required": ["date", "event"],
    "additionalProperties": False,
}

ANALYTICAL_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["explicit_fact", "derived_calculation", "analyst_hypothesis"],
        },
        "topic": {"type": "string", "minLength": 1},
        "analysis": {"type": "string", "minLength": 1},
        "basis": STRING_ARRAY,
        "assumptions": STRING_ARRAY,
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "caveats": STRING_ARRAY,
    },
    "required": ["classification", "topic", "analysis", "basis", "assumptions", "confidence", "caveats"],
    "additionalProperties": False,
}

COMPANY_CLAIM_SOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {"type": "string", "minLength": 1},
        "category": {
            "type": "string",
            "enum": [
                "overview",
                "material_change",
                "financial_figure",
                "corporate_action",
                "expansion_project",
                "management_or_control_change",
                "capital_structure_event",
                "listing_or_regulatory_event",
                "analytical_scenario",
                "risk_or_uncertainty",
                "item_to_monitor",
            ],
        },
        "classification": {
            "type": "string",
            "enum": ["explicit_fact", "derived_calculation", "analyst_hypothesis"],
        },
        "announcement_ids": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["claim", "category", "classification", "announcement_ids", "rationale"],
    "additionalProperties": False,
}

DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_type": NULLABLE_STRING,
        "summary": {"type": "string", "minLength": 1},
        "material_facts": STRING_ARRAY,
        "financial_figures": {"type": "array", "items": FINANCIAL_FIGURE_SCHEMA},
        "dates_and_deadlines": {"type": "array", "items": DATE_EVENT_SCHEMA},
        "parties": STRING_ARRAY,
        "corporate_action_signals": STRING_ARRAY,
        "expansion_or_capex": STRING_ARRAY,
        "management_or_control_changes": STRING_ARRAY,
        "capital_structure_or_ownership": STRING_ARRAY,
        "listing_or_regulatory_events": STRING_ARRAY,
        "analytical_observations": {"type": "array", "items": ANALYTICAL_ITEM_SCHEMA},
        "risks_or_uncertainties": STRING_ARRAY,
        "explicit_market_relevance": STRING_ARRAY,
        "source_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["fact", "evidence"],
                "additionalProperties": False,
            },
        },
        "missing_or_unclear": STRING_ARRAY,
    },
    "required": [
        "document_type",
        "summary",
        "material_facts",
        "financial_figures",
        "dates_and_deadlines",
        "parties",
        "corporate_action_signals",
        "expansion_or_capex",
        "management_or_control_changes",
        "capital_structure_or_ownership",
        "listing_or_regulatory_events",
        "analytical_observations",
        "risks_or_uncertainties",
        "explicit_market_relevance",
        "source_evidence",
        "missing_or_unclear",
    ],
    "additionalProperties": False,
}

ANNOUNCEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string", "minLength": 1},
        "announcement_id": {"type": "string", "minLength": 1},
        "announced_at": {"type": "string", "minLength": 1},
        "title": {"type": "string", "minLength": 1},
        "executive_summary": {"type": "string", "minLength": 1},
        "category": NULLABLE_STRING,
        "material_facts": STRING_ARRAY,
        "financial_figures": {"type": "array", "items": FINANCIAL_FIGURE_SCHEMA},
        "corporate_actions": STRING_ARRAY,
        "expansion_projects": STRING_ARRAY,
        "management_or_control_changes": STRING_ARRAY,
        "capital_structure_events": STRING_ARRAY,
        "listing_or_regulatory_events": STRING_ARRAY,
        "analytical_scenarios": {"type": "array", "items": ANALYTICAL_ITEM_SCHEMA},
        "dates_and_deadlines": {"type": "array", "items": DATE_EVENT_SCHEMA},
        "risks_or_uncertainties": STRING_ARRAY,
        "possible_investor_relevance": STRING_ARRAY,
        "source_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["filename", "url"],
                "additionalProperties": False,
            },
        },
        "limitations": STRING_ARRAY,
    },
    "required": [
        "ticker",
        "announcement_id",
        "announced_at",
        "title",
        "executive_summary",
        "category",
        "material_facts",
        "financial_figures",
        "corporate_actions",
        "expansion_projects",
        "management_or_control_changes",
        "capital_structure_events",
        "listing_or_regulatory_events",
        "analytical_scenarios",
        "dates_and_deadlines",
        "risks_or_uncertainties",
        "possible_investor_relevance",
        "source_files",
        "limitations",
    ],
    "additionalProperties": False,
}

COMPANY_WINDOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string", "minLength": 1},
        "period": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        "announcement_count": {"type": "integer", "minimum": 0},
        "overview": {"type": "string", "minLength": 1},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "announced_at": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["announced_at", "title", "summary"],
                "additionalProperties": False,
            },
        },
        "material_changes": STRING_ARRAY,
        "key_financial_figures": {"type": "array", "items": FINANCIAL_FIGURE_SCHEMA},
        "corporate_actions": STRING_ARRAY,
        "expansion_projects": STRING_ARRAY,
        "management_or_control_changes": STRING_ARRAY,
        "capital_structure_events": STRING_ARRAY,
        "listing_or_regulatory_events": STRING_ARRAY,
        "analytical_scenarios": {"type": "array", "items": ANALYTICAL_ITEM_SCHEMA},
        "risks_or_uncertainties": STRING_ARRAY,
        "items_to_monitor": STRING_ARRAY,
        "claim_sources": {"type": "array", "items": COMPANY_CLAIM_SOURCE_SCHEMA},
        "limitations": STRING_ARRAY,
    },
    "required": [
        "ticker",
        "period",
        "announcement_count",
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
        "claim_sources",
        "limitations",
    ],
    "additionalProperties": False,
}


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_against_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, candidate) for candidate in expected):
            raise SummaryError(f"Structured output field {path} has invalid type")
        if value is None:
            return
    elif isinstance(expected, str) and not _matches_type(value, expected):
        raise SummaryError(f"Structured output field {path} has invalid type")

    if "enum" in schema and value not in schema["enum"]:
        raise SummaryError(f"Structured output field {path} is not one of the allowed values")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value.strip()) < minimum:
            raise SummaryError(f"Structured output field {path} must not be empty")

    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        raise SummaryError(f"Structured output field {path} is below minimum")

    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            raise SummaryError(f"Structured output {path} is missing keys: {', '.join(missing)}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unexpected = [key for key in value if key not in properties]
            if unexpected:
                raise SummaryError(f"Structured output {path} has unexpected keys: {', '.join(unexpected)}")
        for key, child_schema in properties.items():
            if key in value:
                validate_against_schema(value[key], child_schema, f"{path}.{key}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_against_schema(item, schema["items"], f"{path}[{index}]")
