from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


_ROUTINE_TITLE_RE = re.compile(r"\blaporan\s+bulanan\s+registrasi\s+pemegang\s+efek\b", re.I)
_HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("control-change", re.compile(r"perubahan\s+(?:pemegang\s+saham\s+)?pengendali|pengambilalihan|menjadi\s+pengendali|tidak\s+lagi\s+menjadi\s+pengendali", re.I)),
    ("director-transaction", re.compile(r"(?:direksi|direktur|komisaris).{0,120}(?:membeli|menjual|pembelian|penjualan|transaksi\s+saham|bertambah|berkurang)", re.I | re.S)),
    ("share-transaction", re.compile(r"(?:transaksi\s+saham|pengalihan\s+saham|penjualan\s+saham|pembelian\s+saham)", re.I)),
    ("ownership-increase", re.compile(r"(?:penambahan|kenaikan|bertambahnya)\s+(?:jumlah\s+)?kepemilikan", re.I)),
    ("ownership-decrease", re.compile(r"(?:pengurangan|penurunan|berkurangnya)\s+(?:jumlah\s+)?kepemilikan", re.I)),
    ("free-float-breach", re.compile(r"free\s*float.{0,120}(?:tidak\s+memenuhi|di\s+bawah|kurang\s+dari|pelanggaran)", re.I | re.S)),
    ("treasury-change", re.compile(r"(?:saham\s+treasuri|treasury\s+stock).{0,120}(?:bertambah|berkurang|dialihkan|dijual|dibeli)", re.I | re.S)),
)
_OWNERSHIP_WORD_RE = re.compile(r"pemegang|kepemilikan|pengendali|direksi|direktur|komisaris|free\s*float|treasuri", re.I)
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d{1,4})?)\s*%")


@dataclass(frozen=True)
class RoutineEvidence:
    filename: str
    text: str
    extraction_method: str | None = None


@dataclass(frozen=True)
class TriageDecision:
    mode: str
    reason: str
    signals: tuple[str, ...]
    total_characters: int
    document_count: int
    signal_details: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "signals": list(self.signals),
            "total_characters": self.total_characters,
            "document_count": self.document_count,
            "signal_details": [dict(item) for item in self.signal_details],
        }


def is_routine_registration_report(title: str) -> bool:
    return bool(_ROUTINE_TITLE_RE.search(title or ""))


def _numeric_delta_signals(text: str, *, threshold_pct: float) -> tuple[list[str], list[dict[str, Any]]]:
    signals: list[str] = []
    details: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if len(line) > 1200 or not _OWNERSHIP_WORD_RE.search(line):
            continue
        values: list[float] = []
        for match in _PERCENT_RE.finditer(line):
            try:
                values.append(float(match.group(1).replace(",", ".")))
            except ValueError:
                continue
        if len(values) >= 2:
            low, high = min(values), max(values)
            delta = high - low
            if delta >= threshold_pct:
                signals.append("ownership-percentage-delta")
                details.append({
                    "signal": "ownership-percentage-delta",
                    "min_pct": round(low, 6),
                    "max_pct": round(high, 6),
                    "absolute_delta_pct_points": round(delta, 6),
                    "threshold_pct_points": round(float(threshold_pct), 6),
                    "evidence": line[:500],
                })
                break
    return signals, details


def evaluate_routine_disclosure(
    title: str,
    evidence: Iterable[RoutineEvidence],
    *,
    max_characters: int = 70_000,
    ownership_delta_threshold_pct: float = 0.10,
) -> TriageDecision:
    docs = list(evidence)
    total = sum(len(item.text) for item in docs)
    if not is_routine_registration_report(title):
        return TriageDecision("full", "not a supported routine disclosure", (), total, len(docs))
    if not docs:
        return TriageDecision("full", "no extracted evidence available", ("no-evidence",), total, 0)
    if total > max_characters:
        return TriageDecision("full", "routine evidence exceeds direct-analysis size guard", ("large-evidence",), total, len(docs))
    if any(len(item.text.strip()) < 120 for item in docs):
        return TriageDecision("full", "one or more routine sources are too sparse for safe direct analysis", ("sparse-evidence",), total, len(docs))

    combined = "\n".join(item.text for item in docs)
    signals: list[str] = []
    for name, pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(combined):
            signals.append(name)
    delta_signals, signal_details = _numeric_delta_signals(
        combined, threshold_pct=max(0.0, float(ownership_delta_threshold_pct))
    )
    signals.extend(delta_signals)
    signals = sorted(set(signals))
    if signals:
        return TriageDecision(
            "full",
            "deterministic routine scan found material-change indicators; use full document pipeline",
            tuple(signals),
            total,
            len(docs),
            tuple(signal_details),
        )
    return TriageDecision(
        "routine_direct",
        "routine registration report passed conservative deterministic scan; analyze all raw evidence in one structured call",
        (),
        total,
        len(docs),
    )
