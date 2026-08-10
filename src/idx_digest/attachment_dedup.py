from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class AttachmentEvidence:
    url: str
    filename: str
    text: str
    sha256: str | None = None
    is_attachment: bool = True

    @property
    def suffix(self) -> str:
        return Path(self.filename).suffix.lower()


@dataclass(frozen=True)
class DuplicateDecision:
    url: str
    keep: bool
    category: str
    reason: str
    duplicate_of_url: str | None = None
    similarity: float | None = None


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return _TOKEN_RE.findall(normalized)


def _normalized_digest(tokens: list[str]) -> str:
    return hashlib.sha256(" ".join(tokens).encode("utf-8", errors="ignore")).hexdigest()


def _shingles(tokens: list[str], *, width: int = 5, cap: int = 6000) -> set[int]:
    if len(tokens) < width:
        return set()
    count = len(tokens) - width + 1
    step = max(1, math.ceil(count / cap))
    result: set[int] = set()
    for index in range(0, count, step):
        raw = "\x1f".join(tokens[index:index + width]).encode("utf-8", errors="ignore")
        result.add(int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big"))
    return result


def _jaccard(left: set[int], right: set[int]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return (len(left & right) / union) if union else 0.0


def deduplicate_attachments(
    items: Iterable[AttachmentEvidence],
    *,
    near_threshold: float = 0.985,
    min_tokens: int = 120,
) -> list[DuplicateDecision]:
    """Conservatively suppress exact/near duplicates within one announcement.

    Near-duplicate suppression only compares files with the same suffix. This is
    intentional: a financial XLSX and its human-readable PDF can carry overlapping
    numbers but are complementary evidence and must both remain selected.
    """

    items = list(items)
    records = []
    for item in items:
        tokens = _tokens(item.text)
        records.append({
            "item": item,
            "tokens": tokens,
            "normalized": " ".join(tokens),
            "digest": _normalized_digest(tokens),
            "shingles": _shingles(tokens) if len(tokens) >= min_tokens else set(),
        })

    # Prefer IDX's main document marker, then the richer text representation.
    records.sort(key=lambda row: (
        1 if row["item"].is_attachment else 0,
        -len(row["tokens"]),
        row["item"].filename.lower(),
    ))

    kept: list[dict] = []
    decisions: dict[str, DuplicateDecision] = {}
    for row in records:
        item: AttachmentEvidence = row["item"]
        duplicate: DuplicateDecision | None = None
        for representative in kept:
            other: AttachmentEvidence = representative["item"]
            if item.sha256 and other.sha256 and item.sha256 == other.sha256:
                duplicate = DuplicateDecision(
                    url=item.url,
                    keep=False,
                    category="exact_duplicate",
                    reason=f"exact duplicate of {other.filename}; suppressed after download verification",
                    duplicate_of_url=other.url,
                    similarity=1.0,
                )
                break
            if item.suffix != other.suffix:
                continue
            left_tokens = row["tokens"]
            right_tokens = representative["tokens"]
            if len(left_tokens) < min_tokens or len(right_tokens) < min_tokens:
                continue
            length_ratio = min(len(left_tokens), len(right_tokens)) / max(len(left_tokens), len(right_tokens))
            if length_ratio < 0.94:
                continue
            if row["digest"] == representative["digest"]:
                similarity = 1.0
            else:
                left_norm = row["normalized"]
                right_norm = representative["normalized"]
                shorter, longer = (left_norm, right_norm) if len(left_norm) <= len(right_norm) else (right_norm, left_norm)
                containment = (len(shorter) / len(longer)) if longer and shorter in longer else 0.0
                similarity = max(containment, _jaccard(row["shingles"], representative["shingles"]))
            if similarity >= near_threshold:
                duplicate = DuplicateDecision(
                    url=item.url,
                    keep=False,
                    category="near_duplicate",
                    reason=f"near-duplicate text of {other.filename}; similarity={similarity:.3f}",
                    duplicate_of_url=other.url,
                    similarity=similarity,
                )
                break
        if duplicate is None:
            kept.append(row)
            decisions[item.url] = DuplicateDecision(
                url=item.url,
                keep=True,
                category="analysis_source",
                reason="unique extracted evidence retained for analysis",
            )
        else:
            decisions[item.url] = duplicate

    # Preserve caller order.
    return [decisions[item.url] for item in items]
