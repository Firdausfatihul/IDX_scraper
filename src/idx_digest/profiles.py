from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value[:40] or "profile"


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    description: str
    data_rel: str
    created_at: str
    updated_at: str
    legacy: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "data_rel": self.data_rel,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "legacy": self.legacy,
        }


class ProfileManager:
    """Local workspace registry.

    The pre-profile application data directory remains the `main` profile so an
    upgrade never moves or hides historical data. New profiles get isolated data
    directories beneath `data/profiles/`.
    """

    def __init__(self, base_settings: Settings | None = None) -> None:
        base = base_settings or Settings()
        self.base_settings = base.model_copy(update={"data_dir": base.data_dir.resolve()})
        self.base_data_dir = self.base_settings.data_dir
        self.base_data_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.base_data_dir / "profiles.json"
        self._lock = threading.RLock()
        self._ensure_registry()

    def _default_registry(self) -> dict[str, Any]:
        now = _now()
        return {
            "active_profile_id": "main",
            "profiles": [
                {
                    "id": "main",
                    "name": "Main archive",
                    "description": "Existing IDX Signal Desk history and summaries",
                    "data_rel": ".",
                    "created_at": now,
                    "updated_at": now,
                    "legacy": True,
                }
            ],
        }

    def _ensure_registry(self) -> None:
        with self._lock:
            if self.registry_path.exists():
                try:
                    payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
                    if payload.get("profiles") and payload.get("active_profile_id"):
                        return
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            self._write_registry(self._default_registry())

    def _read_registry(self) -> dict[str, Any]:
        self._ensure_registry()
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def _write_registry(self, payload: dict[str, Any]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.registry_path)

    def list(self) -> list[Profile]:
        with self._lock:
            payload = self._read_registry()
            return [Profile(**item) for item in payload.get("profiles", [])]

    def get(self, profile_id: str) -> Profile:
        for profile in self.list():
            if profile.id == profile_id:
                return profile
        raise KeyError(profile_id)

    @property
    def active_id(self) -> str:
        with self._lock:
            return str(self._read_registry()["active_profile_id"])

    def active(self) -> Profile:
        return self.get(self.active_id)

    def data_dir(self, profile_id: str | None = None) -> Path:
        profile = self.get(profile_id or self.active_id)
        if profile.data_rel == ".":
            return self.base_data_dir
        root = (self.base_data_dir / profile.data_rel).resolve()
        profiles_root = (self.base_data_dir / "profiles").resolve()
        try:
            root.relative_to(profiles_root)
        except ValueError as exc:
            raise ValueError("Profile data directory escapes the profiles root") from exc
        return root

    def settings_for(self, profile_id: str | None = None) -> Settings:
        data_dir = self.data_dir(profile_id)
        settings = self.base_settings.model_copy(
            update={
                "data_dir": data_dir,
                "idx_browser_profile_dir": data_dir / "browser-profile",
            }
        )
        settings.ensure_directories()
        return settings

    def state_path(self, profile_id: str | None = None) -> Path:
        return self.data_dir(profile_id) / "profile_state.json"

    def state(self, profile_id: str | None = None) -> dict[str, Any]:
        path = self.state_path(profile_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def save_state(self, profile_id: str, state: dict[str, Any]) -> dict[str, Any]:
        self.get(profile_id)
        clean = dict(state)
        clean["autosaved_at"] = _now()
        path = self.state_path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return clean

    def activate(self, profile_id: str) -> Profile:
        profile = self.get(profile_id)
        with self._lock:
            payload = self._read_registry()
            payload["active_profile_id"] = profile.id
            self._write_registry(payload)
        self.settings_for(profile.id)
        return profile

    def create(
        self,
        name: str,
        *,
        description: str = "",
        copy_state_from: str | None = None,
        copy_prompts_from: str | None = None,
    ) -> Profile:
        name = name.strip()
        if not name:
            raise ValueError("Profile name is required")
        now = _now()
        profile_id = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
        data_rel = f"profiles/{profile_id}"
        profile = Profile(
            id=profile_id,
            name=name[:80],
            description=description.strip()[:240],
            data_rel=data_rel,
            created_at=now,
            updated_at=now,
            legacy=False,
        )
        with self._lock:
            payload = self._read_registry()
            payload["profiles"].append(profile.as_dict())
            self._write_registry(payload)
        target = self.data_dir(profile.id)
        target.mkdir(parents=True, exist_ok=True)
        self.settings_for(profile.id)
        if copy_state_from:
            source_state = self.state(copy_state_from)
            source_state.pop("autosaved_at", None)
            if source_state:
                self.save_state(profile.id, source_state)
        if copy_prompts_from:
            source = self.data_dir(copy_prompts_from) / "prompts.json"
            if source.exists():
                shutil.copy2(source, target / "prompts.json")
        return profile

    def delete(self, profile_id: str) -> Profile:
        """Permanently delete one non-legacy profile and its isolated data tree.

        The main archive is intentionally protected. Callers must switch away
        from an active profile before deletion so no live manager keeps handles
        into a directory that is about to be removed.
        """
        profile = self.get(profile_id)
        if profile.legacy or profile.id == "main" or profile.data_rel == ".":
            raise ValueError("The Main archive profile cannot be deleted")
        if profile.id == self.active_id:
            raise RuntimeError("Switch away from the active profile before deleting it")

        target = self.data_dir(profile.id)
        profiles_root = (self.base_data_dir / "profiles").resolve()
        resolved = target.resolve()
        try:
            relative = resolved.relative_to(profiles_root)
        except ValueError as exc:
            raise ValueError("Profile data directory escapes the profiles root") from exc
        if not relative.parts or resolved == profiles_root:
            raise ValueError("Refusing to delete the profiles root")

        if resolved.exists():
            shutil.rmtree(resolved)

        with self._lock:
            payload = self._read_registry()
            before = len(payload.get("profiles", []))
            payload["profiles"] = [item for item in payload.get("profiles", []) if item.get("id") != profile.id]
            if len(payload["profiles"]) == before:
                raise KeyError(profile.id)
            self._write_registry(payload)
        return profile

    def rename(self, profile_id: str, *, name: str, description: str | None = None) -> Profile:
        name = name.strip()
        if not name:
            raise ValueError("Profile name is required")
        with self._lock:
            payload = self._read_registry()
            updated: Profile | None = None
            for item in payload["profiles"]:
                if item["id"] == profile_id:
                    item["name"] = name[:80]
                    if description is not None:
                        item["description"] = description.strip()[:240]
                    item["updated_at"] = _now()
                    updated = Profile(**item)
                    break
            if updated is None:
                raise KeyError(profile_id)
            self._write_registry(payload)
            return updated
