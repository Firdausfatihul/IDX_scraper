from __future__ import annotations

import importlib
from importlib.resources import files

MODULES = [
    "idx_digest",
    "idx_digest.cli",
    "idx_digest.pipeline",
    "idx_digest.db",
    "idx_digest.downloader",
    "idx_digest.extractors",
    "idx_digest.idx_client",
    "idx_digest.browser_transport",
    "idx_digest.summarizer",
    "idx_digest.observability",
    "idx_digest.config",
    "idx_digest.timeutils",
    "idx_digest.gui",
    "idx_digest.prompts",
    "idx_digest.cached_reducer",
    "idx_digest.share_export",
    "idx_digest.audit_view",
    "idx_digest.attachment_selector",
    "idx_digest.financial_refiner",
    "idx_digest.profiles",
    "idx_digest.llm_scheduler",
    "idx_digest.extraction_scheduler",
    "idx_digest.attachment_dedup",
    "idx_digest.routine_triage",
    "idx_digest.provider_gate",
    "idx_digest.performance_advisor",
]

for name in MODULES:
    module = importlib.import_module(name)
    print(f"OK {name}: {module.__file__}")

import idx_digest
assert idx_digest.__version__ == "0.15.5", idx_digest.__version__
print(f"VERSION {idx_digest.__version__}")

asset = files("idx_digest.web").joinpath("index.html")
assert asset.is_file(), asset
asset_text = asset.read_text(encoding="utf-8")
assert "IDX Signal Desk" in asset_text
assert "Prompt Studio" in asset_text
assert "Resume interrupted run" in asset_text
assert "Open cached checkpoints" in asset_text
assert "Saved Intelligence" in asset_text
assert "Activity Archive" in asset_text
assert "New profile" in asset_text
assert "Delete profile" in asset_text
assert "Blank = ALL companies" in asset_text
assert "Save run snapshot" in asset_text
assert "Recovery JSON" in asset_text
assert "Finish cached company digests" in asset_text
assert "Refine cached financial reports" in asset_text
assert "Smart primary attachments" in asset_text
assert "Listed stocks only" in asset_text
assert "Global LLM slots" in asset_text
assert "Per disclosure cap" in asset_text
assert "Long-document chunks" in asset_text
assert "documentChunks: new Map()" in asset_text
assert "chunkAwareRemaining" in asset_text
assert "Extraction workers" in asset_text
assert "Extraction backlog" in asset_text
assert "Adaptive provider slots" in asset_text
assert "Routine filing triage" in asset_text
assert "Safe duplicate suppression" in asset_text
assert "Pipeline Observatory" in asset_text
assert "Apply next-run tuning" in asset_text
assert "v0.14.1 responsive containment" in asset_text
assert "llmRequests: new Map()" in asset_text
assert "requestLatencyStats" in asset_text
assert ".ledger-wrap" in asset_text
assert "@media(max-width:520px)" in asset_text
assert "Copy / export all" in asset_text
assert "Share company digests" in asset_text
assert "Inspect ticker" in asset_text
assert "Ticker inspector" in asset_text
print(f"OK GUI asset: {asset}")

from idx_digest.config import Settings
from idx_digest.prompts import PromptStore
settings = Settings(_env_file=None)
assert settings.idx_wide_page_probe_size >= settings.idx_page_size
assert settings.idx_wide_page_probe_max_size >= settings.idx_wide_page_probe_size
prompt_bundle = PromptStore(settings.prompt_config_path).load()
expected_prompts = {
    "system",
    "document",
    "public_expose_document",
    "document_combine",
    "announcement",
    "routine_announcement",
    "company",
}
missing_prompts = expected_prompts - set(prompt_bundle.prompts)
assert not missing_prompts, f"Missing prompts: {sorted(missing_prompts)}"
print(f"OK prompt profile: {prompt_bundle.profile_name}")
