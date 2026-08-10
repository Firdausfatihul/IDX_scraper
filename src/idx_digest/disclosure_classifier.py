from __future__ import annotations

import re


def disclosure_class(title: str) -> str:
    value = re.sub(r"\s+", " ", str(title or "").strip().lower())
    public_patterns = (
        "paparan publik",
        "public expose",
        "public exposure",
        "investor meeting",
        "investor presentation",
        "pemaparan kinerja",
        "presentasi investor",
        "materi investor",
    )
    if any(pattern in value for pattern in public_patterns):
        return "public_expose"
    if "laporan keuangan" in value or "financial statement" in value:
        return "financial_report"
    return "general"
