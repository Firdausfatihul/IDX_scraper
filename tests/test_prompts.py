from __future__ import annotations

from pathlib import Path

import pytest

from idx_digest.prompts import (
    DEFAULT_PROFILE_NAME,
    PROMPT_KEYS,
    PromptStore,
    render_prompt,
)


def test_default_prompt_profile_contains_corporate_action_examples(tmp_path: Path) -> None:
    snapshot = PromptStore(tmp_path / "prompts.json").snapshot()
    assert snapshot["profile_name"] == DEFAULT_PROFILE_NAME
    assert set(snapshot["prompts"]) == set(PROMPT_KEYS)
    announcement = snapshot["prompts"]["announcement"]
    for marker in ("GTSI", "MEJA", "ALMI", "TPIA", "UNVR", "BUKK", "SAFE"):
        assert marker in announcement
    assert "analyst_hypothesis" in announcement
    assert "derived_calculation" in announcement


def test_saving_one_prompt_changes_only_its_hash(tmp_path: Path) -> None:
    store = PromptStore(tmp_path / "prompts.json")
    before = store.snapshot()
    custom = before["prompts"]["company"] + "\nTambahkan perhatian pada covenant pendanaan."
    store.save({"company": custom}, profile_name="My research lens")
    after = store.snapshot()
    assert after["profile_name"] == "My research lens"
    assert after["hashes"]["company"] != before["hashes"]["company"]
    for key in set(PROMPT_KEYS) - {"company"}:
        assert after["hashes"][key] == before["hashes"][key]


def test_prompt_rendering_rejects_missing_or_unknown_variables() -> None:
    with pytest.raises(ValueError, match="missing required variables"):
        render_prompt("company", "Only {ticker}", {"ticker": "ANTM"})
    with pytest.raises(ValueError, match="unsupported variables"):
        render_prompt(
            "company",
            "{ticker} {start_at} {end_at} {announcements_json} {secret}",
            {
                "ticker": "ANTM",
                "start_at": "a",
                "end_at": "b",
                "announcements_json": "[]",
            },
        )


def test_reset_single_layer_preserves_custom_profile_name(tmp_path: Path) -> None:
    store = PromptStore(tmp_path / "prompts.json")
    company = store.load().prompts["company"] + "\nCustom line."
    store.save({"company": company}, profile_name="Desk preset")
    reset = store.reset(["company"])
    assert reset.profile_name == "Desk preset"
    assert reset.prompts["company"] == store.snapshot()["defaults"]["company"]
