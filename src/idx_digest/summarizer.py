from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .observability import RunObserver
from .prompts import PromptBundle, PromptStore
from .provider_gate import AdaptiveProviderGate
from .network_watchdog import NetworkWatchdog


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
    "required": [
        "classification", "topic", "analysis", "basis", "assumptions", "confidence", "caveats"
    ],
    "additionalProperties": False,
}

COMPANY_CLAIM_SOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {"type": "string", "minLength": 1},
        "category": {
            "type": "string",
            "enum": [
                "overview", "material_change", "financial_figure", "corporate_action",
                "expansion_project", "management_or_control_change",
                "capital_structure_event", "listing_or_regulatory_event",
                "analytical_scenario", "risk_or_uncertainty", "item_to_monitor"
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
        "document_type", "summary", "material_facts", "financial_figures",
        "dates_and_deadlines", "parties", "corporate_action_signals",
        "expansion_or_capex", "management_or_control_changes",
        "capital_structure_or_ownership", "listing_or_regulatory_events",
        "analytical_observations", "risks_or_uncertainties",
        "explicit_market_relevance", "source_evidence", "missing_or_unclear",
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
        "ticker", "announcement_id", "announced_at", "title", "executive_summary",
        "category", "material_facts", "financial_figures", "corporate_actions",
        "expansion_projects", "management_or_control_changes", "capital_structure_events",
        "listing_or_regulatory_events", "analytical_scenarios", "dates_and_deadlines",
        "risks_or_uncertainties", "possible_investor_relevance", "source_files", "limitations",
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
        "ticker", "period", "announcement_count", "overview", "timeline",
        "material_changes", "key_financial_figures", "corporate_actions",
        "expansion_projects", "management_or_control_changes", "capital_structure_events",
        "listing_or_regulatory_events", "analytical_scenarios", "risks_or_uncertainties",
        "items_to_monitor", "claim_sources", "limitations",
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
                raise SummaryError(
                    f"Structured output {path} has unexpected keys: {', '.join(unexpected)}"
                )
        for key, child_schema in properties.items():
            if key in value:
                validate_against_schema(value[key], child_schema, f"{path}.{key}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_against_schema(item, schema["items"], f"{path}[{index}]")


class OpenRouterSummarizer:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        observer: RunObserver | None = None,
    ):
        if not settings.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required unless --skip-llm is used")
        self.settings = settings
        self.observer = observer
        self.api_model = settings.openrouter_model
        self.model = f"{settings.openrouter_model}@{settings.openrouter_provider}"
        self.prompts: PromptBundle = PromptStore(settings.prompt_config_path).load()
        self.document_prompt_version = self.prompts.document_version(
            schema_version=DOCUMENT_SCHEMA_VERSION
        )
        self.public_expose_document_prompt_version = self.prompts.public_expose_document_version(
            schema_version=DOCUMENT_SCHEMA_VERSION
        )
        self.announcement_prompt_version = self.prompts.announcement_version(
            schema_version=ANNOUNCEMENT_SCHEMA_VERSION
        )
        self.company_prompt_version = self.prompts.layer_version(
            "company", schema_version=COMPANY_SCHEMA_VERSION
        )

        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if settings.openrouter_http_referer:
            headers["HTTP-Referer"] = settings.openrouter_http_referer
        if settings.openrouter_app_title:
            headers["X-OpenRouter-Title"] = settings.openrouter_app_title

        self.client = client or httpx.Client(
            base_url=settings.openrouter_base_url.rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=20.0),
            http2=True,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self.audit_db = Database(settings.database_path)
        self.provider_gate = AdaptiveProviderGate(
            configured_max=settings.llm_concurrency,
            enabled=settings.llm_adaptive_concurrency,
            observer=observer,
        )
        self.network_watchdog = NetworkWatchdog(
            settings.openrouter_base_url, enabled=settings.network_watchdog_enabled, observer=observer,
            probe_interval=settings.network_probe_interval_seconds,
            probe_timeout=settings.network_probe_timeout_seconds,
        )
        self._disclosure_gate_lock = threading.RLock()
        self._disclosure_gates: dict[str, threading.BoundedSemaphore] = {}


    def _disclosure_gate(self, announcement_id: str | None) -> threading.BoundedSemaphore | None:
        key = str(announcement_id or "").strip()
        if not key:
            return None
        with self._disclosure_gate_lock:
            gate = self._disclosure_gates.get(key)
            if gate is None:
                limit = max(1, min(
                    int(self.settings.llm_concurrency),
                    int(self.settings.llm_per_announcement_concurrency),
                ))
                gate = threading.BoundedSemaphore(limit)
                self._disclosure_gates[key] = gate
            return gate

    @staticmethod
    def _retry_reason(exc: BaseException) -> str:
        text = str(exc).lower()
        if isinstance(exc, json.JSONDecodeError):
            return "malformed_json"
        if "429" in text or "rate limit" in text or "throttl" in text:
            return "rate_limited"
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if "5" in text and "openrouter request failed" in text:
            return "provider_error"
        return "structured_output_error"

    def _request_non_streaming(self, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        response = self.client.post("chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummaryError(f"Unexpected OpenRouter response shape: {body}") from exc
        usage = dict(body.get("usage") or {})
        usage["finish_reason"] = choice.get("finish_reason")
        return content, usage

    def _request_streaming(
        self,
        payload: dict[str, Any],
        *,
        stream_label: str,
        request_id: str | None = None,
        audit_context: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}
        chunks: list[str] = []
        usage: dict[str, Any] = {}
        started = time.perf_counter()
        first_token_seconds: float | None = None
        streamed_characters = 0
        streamed_chunks = 0
        last_telemetry = 0.0
        context = dict(audit_context or {})
        if self.observer:
            self.observer.begin_stream(
                stream_label,
                provider=self.settings.openrouter_provider,
                model=self.api_model,
            )
        try:
            with self.client.stream("POST", "chat/completions", json=stream_payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or line.startswith(":"):
                        continue
                    data = line[5:].strip() if line.startswith("data:") else line.strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise SummaryError(f"Invalid OpenRouter stream event: {data[:300]!r}") from exc
                    if isinstance(event.get("error"), dict):
                        raise SummaryError(f"OpenRouter stream error: {event['error']}")
                    if isinstance(event.get("usage"), dict):
                        usage.update(event["usage"] or {})
                    try:
                        choice = event["choices"][0]
                        if choice.get("finish_reason"):
                            usage["finish_reason"] = choice.get("finish_reason")
                        delta = choice["delta"].get("content")
                    except (KeyError, IndexError, TypeError, AttributeError):
                        delta = None
                    if isinstance(delta, list):
                        delta = "".join(
                            str(part.get("text") or "") if isinstance(part, dict) else str(part)
                            for part in delta
                        )
                    if isinstance(delta, str) and delta:
                        if first_token_seconds is None:
                            first_token_seconds = time.perf_counter() - started
                            if self.observer:
                                self.observer.event(
                                    "llm",
                                    "first streamed token received",
                                    always=True,
                                    schema=stream_label,
                                    first_token_seconds=f"{first_token_seconds:.3f}",
                                )
                        chunks.append(delta)
                        streamed_chunks += 1
                        streamed_characters += len(delta)
                        if self.observer:
                            self.observer.stream_chunk(delta)
                            now = time.perf_counter()
                            if request_id and (now - last_telemetry >= 0.5 or streamed_chunks == 1):
                                last_telemetry = now
                                self.observer.event(
                                    "llm-request",
                                    "stream progress",
                                    request_id=request_id,
                                    schema=stream_label,
                                    chunks_received=streamed_chunks,
                                    characters_received=streamed_characters,
                                    elapsed_seconds=f"{now - started:.3f}",
                                    first_token_seconds=(f"{first_token_seconds:.3f}" if first_token_seconds is not None else None),
                                    ticker=context.get("ticker"),
                                    announcement_id=context.get("announcement_id"),
                                    filename=context.get("filename"),
                                )
        finally:
            if self.observer:
                self.observer.end_stream(
                    elapsed_seconds=time.perf_counter() - started,
                    characters=sum(len(chunk) for chunk in chunks),
                )
        return "".join(chunks), usage

    def _prompt_version_for_schema(self, schema_name: str) -> str | None:
        return {
            "idx_document_summary": self.document_prompt_version,
            "idx_announcement_summary": self.announcement_prompt_version,
            "idx_company_window_summary": self.company_prompt_version,
        }.get(schema_name)

    def _completion_once(
        self,
        user_prompt: str,
        *,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        stream: bool | None = None,
        audit_context: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        system_prompt = self.prompts.prompts["system"]
        payload = {
            "model": self.api_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "provider": self.settings.openrouter_provider_preferences,
            "reasoning": {"enabled": False},
        }
        context = dict(audit_context or {})
        request_id = uuid.uuid4().hex[:12]
        prompt_version = context.get("prompt_version_override") or self._prompt_version_for_schema(schema_name)
        started_perf = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        should_stream = bool(self.observer and self.observer.stream_llm) if stream is None else stream
        if self.observer:
            self.observer.event(
                "llm",
                "OpenRouter request",
                schema=schema_name,
                provider=self.settings.openrouter_provider,
                model=self.api_model,
                prompt_characters=len(user_prompt),
                system_prompt_characters=len(system_prompt),
                prompt_profile=self.prompts.profile_name,
                prompt_version=prompt_version,
                max_tokens=max_tokens,
                streaming=should_stream,
                ticker=context.get("ticker"),
                announcement_id=context.get("announcement_id"),
                filename=context.get("filename"),
                attempt=attempt,
                request_id=request_id,
            )
            self.observer.event(
                "llm-request",
                "request queued",
                request_id=request_id,
                state="queued",
                schema=schema_name,
                provider=self.settings.openrouter_provider,
                model=self.api_model,
                prompt_characters=len(user_prompt),
                max_tokens=max_tokens,
                streaming=should_stream,
                ticker=context.get("ticker"),
                announcement_id=context.get("announcement_id"),
                filename=context.get("filename"),
                job_stage=context.get("stage"),
                chunk_index=context.get("chunk_index"),
                chunk_count=context.get("chunk_count"),
                attempt=attempt,
            )

        content: Any = None
        usage: dict[str, Any] = {}
        parsed: dict[str, Any] | None = None
        error: str | None = None
        disclosure_gate = self._disclosure_gate(context.get("announcement_id"))
        disclosure_acquired = False
        try:
            if disclosure_gate is not None:
                disclosure_wait_started = time.perf_counter()
                if disclosure_gate.acquire(blocking=False):
                    disclosure_acquired = True
                else:
                    if self.observer:
                        self.observer.event(
                            "llm-request",
                            "waiting for disclosure slot",
                            request_id=request_id,
                            state="waiting_disclosure",
                            schema=schema_name,
                            ticker=context.get("ticker"),
                            announcement_id=context.get("announcement_id"),
                            filename=context.get("filename"),
                            chunk_index=context.get("chunk_index"),
                            chunk_count=context.get("chunk_count"),
                            attempt=attempt,
                        )
                    disclosure_gate.acquire()
                    disclosure_acquired = True
                    if self.observer:
                        self.observer.event(
                            "llm-request",
                            "disclosure slot acquired",
                            request_id=request_id,
                            schema=schema_name,
                            waited_seconds=f"{time.perf_counter() - disclosure_wait_started:.3f}",
                            ticker=context.get("ticker"),
                            announcement_id=context.get("announcement_id"),
                            filename=context.get("filename"),
                            chunk_index=context.get("chunk_index"),
                            chunk_count=context.get("chunk_count"),
                            attempt=attempt,
                        )
            if self.observer:
                self.observer.event(
                    "llm-request",
                    "waiting for provider slot",
                    request_id=request_id,
                    state="waiting_provider",
                    schema=schema_name,
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    filename=context.get("filename"),
                    attempt=attempt,
                    request_class=("bulk_chunk" if str(context.get("stage") or "") == "document" and int(context.get("chunk_count") or 0) > 1 else "priority"),
                )
            self.network_watchdog.before_request()
            request_class = (
                "bulk_chunk"
                if str(context.get("stage") or "") == "document"
                and int(context.get("chunk_count") or 0) > 1
                else "priority"
            )
            lease = self.provider_gate.acquire(request_class=request_class)
            if self.observer:
                self.observer.event(
                    "llm-request",
                    "provider slot acquired",
                    request_id=request_id,
                    state="sending",
                    schema=schema_name,
                    waited_seconds=f"{lease.waited_seconds:.3f}",
                    provider_limit=lease.limit_at_acquire,
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    filename=context.get("filename"),
                    attempt=attempt,
                    request_class=request_class,
                )
            request_started = time.perf_counter()
            try:
                if self.observer:
                    self.observer.event(
                        "llm-request",
                        "generation active",
                        request_id=request_id,
                        state="generating",
                        schema=schema_name,
                        ticker=context.get("ticker"),
                        announcement_id=context.get("announcement_id"),
                        filename=context.get("filename"),
                        attempt=attempt,
                    )
                if should_stream:
                    content, usage = self._request_streaming(
                        payload,
                        stream_label=schema_name,
                        request_id=request_id,
                        audit_context=context,
                    )
                else:
                    content, usage = self._request_non_streaming(payload)
            except httpx.HTTPStatusError as request_exc:
                self.provider_gate.record_failure(
                    lease,
                    status_code=request_exc.response.status_code,
                    transient=request_exc.response.status_code >= 500,
                    error=str(request_exc),
                )
                raise
            except httpx.HTTPError as request_exc:
                self.provider_gate.record_failure(lease, transient=True, error=str(request_exc))
                self.network_watchdog.record_failure(request_exc)
                raise
            except SummaryError as request_exc:
                self.provider_gate.record_failure(lease, transient=True, error=str(request_exc))
                self.network_watchdog.record_failure(request_exc)
                raise
            else:
                request_elapsed = time.perf_counter() - request_started
                self.provider_gate.record_success(
                    lease, elapsed_seconds=request_elapsed
                )
                self.network_watchdog.record_success()
                if self.observer:
                    raw_chars = (
                        len(content) if isinstance(content, str) else
                        len(json.dumps(content, ensure_ascii=False)) if content is not None else 0
                    )
                    self.observer.event(
                        "llm-request",
                        "response received",
                        request_id=request_id,
                        state="response_received",
                        schema=schema_name,
                        elapsed_seconds=f"{request_elapsed:.3f}",
                        response_characters=raw_chars,
                        ticker=context.get("ticker"),
                        announcement_id=context.get("announcement_id"),
                        filename=context.get("filename"),
                        attempt=attempt,
                    )

            if self.observer:
                self.observer.event(
                    "llm-request",
                    "validating structured response",
                    request_id=request_id,
                    state="validating",
                    schema=schema_name,
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    filename=context.get("filename"),
                    attempt=attempt,
                )
            if isinstance(content, dict):
                parsed = content
            else:
                if isinstance(content, list):
                    content = "".join(
                        str(part.get("text") or "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                if not isinstance(content, str) or not content.strip():
                    raise SummaryError("OpenRouter returned empty content")
                parsed = json.loads(content)

            if not isinstance(parsed, dict):
                raise SummaryError("Expected a JSON object")
            validate_against_schema(parsed, schema)
            if self.observer:
                self.observer.event(
                    "llm",
                    "OpenRouter response validated",
                    schema=schema_name,
                    elapsed_seconds=f"{time.perf_counter() - started_perf:.3f}",
                    output_characters=len(json.dumps(parsed, ensure_ascii=False)),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    finish_reason=usage.get("finish_reason"),
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    filename=context.get("filename"),
                    attempt=attempt,
                    request_id=request_id,
                )
                self.observer.event(
                    "llm-request",
                    "request completed",
                    request_id=request_id,
                    state="completed",
                    schema=schema_name,
                    elapsed_seconds=f"{time.perf_counter() - started_perf:.3f}",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    finish_reason=usage.get("finish_reason"),
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    filename=context.get("filename"),
                    attempt=attempt,
                )
            return parsed
        except (httpx.HTTPError, ValueError, SummaryError, json.JSONDecodeError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f": {exc.response.text[:1000]}"
                error = f"OpenRouter request failed{detail}"
                wrapped: Exception = SummaryError(error)
            elif isinstance(exc, httpx.HTTPError):
                error = f"OpenRouter request failed: {exc}"
                wrapped = SummaryError(error)
            else:
                error = str(exc)
                wrapped = exc
            if self.observer:
                self.observer.event(
                    "llm-request",
                    "request failed",
                    level="WARNING",
                    request_id=request_id,
                    state="failed",
                    schema=schema_name,
                    elapsed_seconds=f"{time.perf_counter() - started_perf:.3f}",
                    error=(error or str(exc))[:500],
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    filename=context.get("filename"),
                    attempt=attempt,
                )
            raise wrapped
        finally:
            if disclosure_acquired and disclosure_gate is not None:
                disclosure_gate.release()
            finished_at = datetime.now(timezone.utc).isoformat()
            elapsed = time.perf_counter() - started_perf
            try:
                if isinstance(content, dict):
                    raw_response = json.dumps(content, ensure_ascii=False)
                elif isinstance(content, list):
                    raw_response = json.dumps(content, ensure_ascii=False)
                elif content is None:
                    raw_response = None
                else:
                    raw_response = str(content)
                self.audit_db.save_llm_audit(
                    stage=str(context.get("stage") or schema_name),
                    schema_name=schema_name,
                    model=self.model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    status="failed" if error else "succeeded",
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed,
                    prompt_version=prompt_version,
                    prompt_profile=self.prompts.profile_name,
                    ticker=context.get("ticker"),
                    announcement_id=context.get("announcement_id"),
                    attachment_url=context.get("attachment_url"),
                    filename=context.get("filename"),
                    window_start=context.get("window_start"),
                    window_end=context.get("window_end"),
                    chunk_index=context.get("chunk_index"),
                    chunk_count=context.get("chunk_count"),
                    attempt=attempt,
                    raw_response=raw_response,
                    parsed=parsed,
                    error=error,
                    usage=usage,
                )
            except Exception as audit_exc:
                if self.observer:
                    self.observer.event(
                        "audit",
                        "LLM audit persistence failed",
                        level="WARNING",
                        error=str(audit_exc),
                        schema=schema_name,
                    )

    def _json_completion(
        self,
        user_prompt: str,
        *,
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int = 4000,
        stream: bool | None = None,
        audit_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = 4
        retry_prompt = user_prompt
        retry_max_tokens = max_tokens
        for attempt in range(1, attempts + 1):
            try:
                return self._completion_once(
                    retry_prompt,
                    schema_name=schema_name,
                    schema=schema,
                    max_tokens=retry_max_tokens,
                    stream=stream,
                    audit_context=audit_context,
                    attempt=attempt,
                )
            except (SummaryError, json.JSONDecodeError) as exc:
                if attempt >= attempts:
                    raise
                malformed_json = isinstance(exc, json.JSONDecodeError)
                if malformed_json:
                    # Do not resend the exact same request after malformed/truncated
                    # structured output. Give the provider more output headroom and
                    # explicitly force a compact complete object on the next attempt.
                    retry_max_tokens = min(max(retry_max_tokens + 2000, 7000), 12000)
                    retry_prompt = user_prompt + (
                        "\n\nRETRY JSON: Respons sebelumnya tidak membentuk JSON lengkap. "
                        "Kembalikan SATU objek JSON lengkap sesuai schema. Ringkas secara agresif: "
                        "maksimal 12 material_facts, 24 financial_figures, 8 source_evidence, "
                        "dan maksimal 6 item pada daftar lain. Jangan memotong string di tengah. "
                        "Gunakan daftar kosong/null daripada memanjangkan respons."
                    )
                delay = min(2 ** (attempt - 1), 20) + random.uniform(0, 1)
                if self.observer:
                    context = dict(audit_context or {})
                    self.observer.event(
                        "llm",
                        "summary request failed; retrying",
                        level="WARNING",
                        always=True,
                        schema=schema_name,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        delay_seconds=f"{delay:.2f}",
                        malformed_json=malformed_json,
                        retry_reason=self._retry_reason(exc),
                        next_max_tokens=retry_max_tokens,
                        ticker=context.get("ticker"),
                        announcement_id=context.get("announcement_id"),
                        filename=context.get("filename"),
                        attachment_url=context.get("attachment_url"),
                        chunk_index=context.get("chunk_index"),
                        chunk_count=context.get("chunk_count"),
                        error=str(exc),
                    )
                time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def is_valid_document_summary(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            candidate = {key: value for key, value in payload.items() if key != "chunk_count"}
            validate_against_schema(candidate, DOCUMENT_SCHEMA)
            return isinstance(payload.get("chunk_count"), int) and payload["chunk_count"] >= 1
        except SummaryError:
            return False

    @staticmethod
    def is_valid_announcement_summary(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            validate_against_schema(payload, ANNOUNCEMENT_SCHEMA)
            return True
        except SummaryError:
            return False

    @staticmethod
    def is_valid_company_summary(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            validate_against_schema(payload, COMPANY_WINDOW_SCHEMA)
            return True
        except SummaryError:
            return False

    @property
    def provider_metrics(self) -> dict[str, Any]:
        metrics = dict(self.provider_gate.metrics)
        metrics["network_watchdog"] = self.network_watchdog.metrics
        return metrics

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _chunks(self, text: str, *, chunk_chars: int | None = None) -> list[str]:
        size = int(chunk_chars or self.settings.llm_chunk_chars)
        if len(text) <= size:
            return [text]
        chunks: list[str] = []
        cursor = 0
        while cursor < len(text):
            end = min(cursor + size, len(text))
            if end < len(text):
                split = text.rfind("\n", cursor, end)
                if split > cursor + size // 2:
                    end = split
            chunks.append(text[cursor:end])
            cursor = end
        return chunks

    @staticmethod
    def _is_valid_chunk_summary(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            validate_against_schema(payload, DOCUMENT_SCHEMA)
            return True
        except SummaryError:
            return False

    def summarize_document(
        self,
        *,
        ticker: str,
        filename: str,
        text: str,
        stream: bool | None = None,
        source_url: str | None = None,
        announcement_id: str | None = None,
        chunk_chars: int | None = None,
        document_profile: str = "general",
    ) -> dict[str, Any]:
        chunks = self._chunks(text, chunk_chars=chunk_chars)
        prompt_key = "public_expose_document" if document_profile == "public_expose" else "document"
        document_prompt_version = (
            self.public_expose_document_prompt_version if document_profile == "public_expose"
            else self.document_prompt_version
        )
        chunk_count = len(chunks)
        chunk_workers = max(1, min(
            int(getattr(self.settings, "llm_document_chunk_concurrency", 2)),
            int(self.settings.llm_concurrency),
            int(self.settings.llm_per_announcement_concurrency),
            chunk_count,
        ))
        hashes = [hashlib.sha256(chunk.encode("utf-8", errors="replace")).hexdigest() for chunk in chunks]
        chunk_results: list[dict[str, Any] | None] = [None] * chunk_count
        cached_chunks = 0

        if source_url:
            for zero_index, chunk_hash in enumerate(hashes):
                cached = self.audit_db.get_document_chunk_summary(
                    source_url,
                    chunk_index=zero_index + 1,
                    chunk_count=chunk_count,
                    chunk_sha256=chunk_hash,
                    model=self.model,
                    prompt_version=document_prompt_version,
                )
                if self._is_valid_chunk_summary(cached):
                    chunk_results[zero_index] = cached
                    cached_chunks += 1
                    if self.observer and chunk_count > 1:
                        self.observer.event(
                            "llm-chunk", "document chunk checkpoint hit",
                            ticker=ticker, announcement_id=announcement_id, filename=filename,
                            attachment_url=source_url, chunk_index=zero_index + 1,
                            chunk_count=chunk_count, cached_chunks=cached_chunks,
                        )

        if self.observer and chunk_count > 1:
            self.observer.event(
                "llm-chunk", "document chunk plan", always=True,
                ticker=ticker, announcement_id=announcement_id, filename=filename,
                attachment_url=source_url, characters=len(text),
                chunk_count=chunk_count, cached_chunks=cached_chunks,
                cached_chunk_indexes=[i + 1 for i, item in enumerate(chunk_results) if isinstance(item, dict)],
                pending_chunks=chunk_count - cached_chunks, chunk_concurrency=chunk_workers,
                straggler=chunk_count >= 4, combine_required=True,
            )

        def run_chunk(zero_index: int) -> tuple[int, dict[str, Any]]:
            index = zero_index + 1
            chunk = chunks[zero_index]
            if self.observer and chunk_count > 1:
                self.observer.event(
                    "llm-chunk", "document chunk started",
                    ticker=ticker, announcement_id=announcement_id, filename=filename,
                    attachment_url=source_url, chunk_index=index, chunk_count=chunk_count,
                    chunk_characters=len(chunk), chunk_concurrency=chunk_workers,
                )
            prompt = self.prompts.render(
                prompt_key, ticker=ticker, filename=filename,
                chunk_index=index, chunk_count=chunk_count, document_text=chunk,
            )
            started = time.perf_counter()
            try:
                result = self._json_completion(
                    prompt,
                    schema_name="idx_document_summary",
                    schema=DOCUMENT_SCHEMA,
                    max_tokens=6500,
                    stream=stream,
                    audit_context={
                        "stage": "document", "ticker": ticker, "filename": filename,
                        "attachment_url": source_url, "announcement_id": announcement_id,
                        "chunk_index": index, "chunk_count": chunk_count,
                        "prompt_version_override": document_prompt_version,
                    },
                )
            except BaseException as exc:
                if self.observer and chunk_count > 1:
                    self.observer.event(
                        "llm-chunk", "document chunk failed", level="ERROR", always=True,
                        ticker=ticker, announcement_id=announcement_id, filename=filename,
                        attachment_url=source_url, chunk_index=index, chunk_count=chunk_count,
                        elapsed_seconds=f"{time.perf_counter() - started:.3f}", error=str(exc),
                    )
                raise
            if source_url:
                self.audit_db.save_document_chunk_summary(
                    source_url, chunk_index=index, chunk_count=chunk_count,
                    chunk_sha256=hashes[zero_index], payload=result, model=self.model,
                    prompt_version=document_prompt_version,
                )
            if self.observer and chunk_count > 1:
                self.observer.event(
                    "llm-chunk", "document chunk completed",
                    ticker=ticker, announcement_id=announcement_id, filename=filename,
                    attachment_url=source_url, chunk_index=index, chunk_count=chunk_count,
                    elapsed_seconds=f"{time.perf_counter() - started:.3f}",
                )
            return zero_index, result

        pending_indexes = [i for i, result in enumerate(chunk_results) if result is None]
        if len(pending_indexes) == 1 or chunk_workers == 1:
            for zero_index in pending_indexes:
                completed_index, result = run_chunk(zero_index)
                chunk_results[completed_index] = result
        elif pending_indexes:
            with ThreadPoolExecutor(max_workers=min(chunk_workers, len(pending_indexes)), thread_name_prefix="idx-doc-chunk") as executor:
                futures = {executor.submit(run_chunk, zero_index): zero_index for zero_index in pending_indexes}
                for future in as_completed(futures):
                    completed_index, result = future.result()
                    chunk_results[completed_index] = result

        completed_results = [result for result in chunk_results if isinstance(result, dict)]
        if len(completed_results) != chunk_count:
            raise SummaryError(f"document chunk completion mismatch for {filename}: {len(completed_results)}/{chunk_count}")

        if chunk_count == 1:
            result = dict(completed_results[0])
            result["chunk_count"] = 1
            return result

        if self.observer:
            self.observer.event(
                "llm-chunk", "document combine started", always=True,
                ticker=ticker, announcement_id=announcement_id, filename=filename,
                attachment_url=source_url, chunk_count=chunk_count, cached_chunks=cached_chunks,
            )
        combine_started = time.perf_counter()
        combine_prompt = self.prompts.render(
            "document_combine", ticker=ticker, filename=filename,
            chunk_summaries_json=json.dumps(completed_results, ensure_ascii=False),
        )
        result = self._json_completion(
            combine_prompt, schema_name="idx_document_summary", schema=DOCUMENT_SCHEMA,
            max_tokens=7000, stream=stream,
            audit_context={
                "stage": "document_combine", "ticker": ticker, "filename": filename,
                "attachment_url": source_url, "announcement_id": announcement_id,
                "chunk_count": chunk_count,
                "prompt_version_override": document_prompt_version,
            },
        )
        result["chunk_count"] = chunk_count
        if self.observer:
            self.observer.event(
                "llm-chunk", "document combine completed", always=True,
                ticker=ticker, announcement_id=announcement_id, filename=filename,
                attachment_url=source_url, chunk_count=chunk_count,
                elapsed_seconds=f"{time.perf_counter() - combine_started:.3f}",
            )
        return result

    def summarize_announcement(
        self, *, announcement: dict[str, Any], documents: list[dict[str, Any]], stream: bool | None = None
    ) -> dict[str, Any]:
        prompt = self.prompts.render(
            "announcement",
            announcement_json=json.dumps(announcement, ensure_ascii=False),
            documents_json=json.dumps(documents, ensure_ascii=False),
        )
        result = self._json_completion(
            prompt,
            schema_name="idx_announcement_summary",
            schema=ANNOUNCEMENT_SCHEMA,
            max_tokens=7000,
            stream=stream,
            audit_context={
                "stage": "announcement",
                "ticker": announcement.get("ticker"),
                "announcement_id": announcement.get("id2"),
            },
        )
        result = dict(result)
        result["ticker"] = str(announcement.get("ticker") or "").strip().upper()
        result["announcement_id"] = str(announcement.get("id2") or "")
        result["announced_at"] = str(announcement.get("announced_at") or "")
        result["title"] = str(announcement.get("title") or "")
        validate_against_schema(result, ANNOUNCEMENT_SCHEMA)
        return result

    def summarize_routine_announcement(
        self,
        *,
        announcement: dict[str, Any],
        raw_documents: list[dict[str, Any]],
        triage: dict[str, Any],
        stream: bool | None = None,
    ) -> dict[str, Any]:
        evidence = [
            {
                "filename": str(item.get("filename") or "attachment"),
                "url": str(item.get("url") or ""),
                "text": str(item.get("text") or ""),
            }
            for item in raw_documents
        ]
        prompt = self.prompts.render(
            "routine_announcement",
            announcement_json=json.dumps(announcement, ensure_ascii=False),
            raw_documents_json=json.dumps(evidence, ensure_ascii=False),
            triage_json=json.dumps(triage, ensure_ascii=False),
        )
        result = self._json_completion(
            prompt,
            schema_name="idx_announcement_summary",
            schema=ANNOUNCEMENT_SCHEMA,
            max_tokens=5500,
            stream=stream,
            audit_context={
                "stage": "routine_announcement",
                "ticker": announcement.get("ticker"),
                "announcement_id": announcement.get("id2"),
            },
        )
        result = dict(result)
        result["ticker"] = str(announcement.get("ticker") or "").strip().upper()
        result["announcement_id"] = str(announcement.get("id2") or "")
        result["announced_at"] = str(announcement.get("announced_at") or "")
        result["title"] = str(announcement.get("title") or "")
        result["source_files"] = [
            {"filename": item["filename"], "url": item["url"]}
            for item in evidence
        ]
        validate_against_schema(result, ANNOUNCEMENT_SCHEMA)
        return result

    def summarize_company_window(
        self, *, ticker: str, start_at: str, end_at: str, announcements: list[dict[str, Any]],
        stream: bool | None = None,
    ) -> dict[str, Any]:
        prompt = self.prompts.render(
            "company",
            ticker=ticker,
            start_at=start_at,
            end_at=end_at,
            announcements_json=json.dumps(announcements, ensure_ascii=False),
        )
        result = self._json_completion(
            prompt,
            schema_name="idx_company_window_summary",
            schema=COMPANY_WINDOW_SCHEMA,
            max_tokens=10000,
            stream=stream,
            audit_context={
                "stage": "company", "ticker": ticker,
                "window_start": start_at, "window_end": end_at,
            },
        )
        # Provenance references must point only to source announcements actually supplied.
        known_ids = {str(item.get("announcement_id") or "") for item in announcements}
        known_ids.discard("")
        for ref in result.get("claim_sources") or []:
            ids = [str(value) for value in ref.get("announcement_ids") or [] if str(value) in known_ids]
            if not ids and len(known_ids) == 1:
                ids = list(known_ids)
            ref["announcement_ids"] = ids

        # These values are deterministic metadata and must not depend on the model.
        result["ticker"] = ticker
        result["announcement_count"] = len(announcements)
        result["period"] = {"start": start_at, "end": end_at}
        validate_against_schema(result, COMPANY_WINDOW_SCHEMA)
        return result

