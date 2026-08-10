from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

AttachmentPolicy = Literal["smart", "all_supported"]


@dataclass(frozen=True)
class AttachmentDecision:
    attachment: dict[str, Any]
    selected: bool
    reason: str
    category: str

    @property
    def filename(self) -> str:
        return str(
            self.attachment.get("OriginalFilename")
            or self.attachment.get("PDFFilename")
            or "attachment"
        )


FINANCIAL_TITLE_PATTERNS = (
    "laporan keuangan",
    "financial statement",
    "financial statements",
)

ADMINISTRATIVE_PATTERNS = (
    "checklist",
    "check list",
    "surat pernyataan",
    "pernyataan direksi",
    "pernyataan manajemen",
    "management statement",
    "director statement",
    "pengungkapan lk",
)

PRIMARY_FINANCIAL_PDF_PATTERNS = (
    "laporan keuangan",
    "lap keuangan",
)

GENERIC_FINANCIAL_PDF_PATTERNS = (
    "financial statement",
    "financialstatement",
)

SUPPORTED_SUFFIXES = {".pdf", ".xlsx", ".xlsm", ".docx", ".html", ".htm", ".txt", ".csv", ".json", ".xml"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm"}


def _norm(value: str) -> str:
    value = value.casefold().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", value).strip()


def _filename(attachment: dict[str, Any]) -> str:
    return str(
        attachment.get("OriginalFilename")
        or attachment.get("PDFFilename")
        or PurePosixPath(str(attachment.get("FullSavePath") or "attachment")).name
        or "attachment"
    )


def _suffix(filename: str) -> str:
    return PurePosixPath(filename).suffix.casefold()


def is_financial_report_announcement(title: str) -> bool:
    normalized = _norm(title)
    return any(pattern in normalized for pattern in FINANCIAL_TITLE_PATTERNS)


def _looks_like_primary_financial_pdf(filename: str) -> bool:
    normalized = _norm(filename)
    if any(pattern in normalized for pattern in PRIMARY_FINANCIAL_PDF_PATTERNS):
        return True
    # Common IDX shorthand: "LK PJAA 30 Juni 2026.pdf" or "LK TW II 2026.pdf".
    return bool(re.search(r"(^|\s)lk(?:tt)?(\s|$)", normalized))


def _looks_like_generic_financial_pdf(filename: str) -> bool:
    normalized = _norm(filename)
    return any(pattern in normalized for pattern in GENERIC_FINANCIAL_PDF_PATTERNS)


def classify_attachments(
    title: str,
    attachments: list[dict[str, Any]],
    *,
    policy: AttachmentPolicy = "smart",
) -> list[AttachmentDecision]:
    """Choose source documents before download/extraction/LLM work.

    Financial-report smart mode prefers the structured spreadsheet plus the
    issuer's human-readable LK/report PDF. Generic ``FinancialStatement*.pdf``
    files are treated as duplicate/fallback representations when a better LK PDF
    exists. This avoids feeding two copies of the same statements to the model.
    """
    if policy not in {"smart", "all_supported"}:
        raise ValueError(f"Unsupported attachment policy: {policy}")

    financial = is_financial_report_announcement(title)
    preliminary: list[AttachmentDecision] = []
    primary_pdf_indexes: list[int] = []
    generic_pdf_indexes: list[int] = []

    for attachment in attachments:
        filename = _filename(attachment)
        normalized = _norm(filename)
        suffix = _suffix(filename)

        if suffix == ".zip":
            preliminary.append(AttachmentDecision(attachment, False, "machine/XBRL ZIP package", "machine_package"))
            continue
        if suffix and suffix not in SUPPORTED_SUFFIXES:
            preliminary.append(AttachmentDecision(attachment, False, f"unsupported file type {suffix}", "unsupported"))
            continue

        if policy == "all_supported" or not financial:
            preliminary.append(AttachmentDecision(attachment, True, "supported attachment", "general_source"))
            continue

        if suffix in SPREADSHEET_SUFFIXES:
            preliminary.append(AttachmentDecision(attachment, True, "financial-statement spreadsheet", "financial_statement"))
            continue

        if suffix == ".pdf" and any(pattern in normalized for pattern in ADMINISTRATIVE_PATTERNS):
            preliminary.append(AttachmentDecision(attachment, False, "administrative/supporting financial-report document", "administrative"))
            continue

        if suffix == ".pdf" and _looks_like_primary_financial_pdf(filename):
            primary_pdf_indexes.append(len(preliminary))
            preliminary.append(AttachmentDecision(attachment, True, "primary human-readable financial-statement PDF", "financial_statement"))
            continue

        if suffix == ".pdf" and _looks_like_generic_financial_pdf(filename):
            generic_pdf_indexes.append(len(preliminary))
            preliminary.append(AttachmentDecision(attachment, False, "generic financial-statement PDF held as fallback", "financial_statement_duplicate"))
            continue

        preliminary.append(AttachmentDecision(attachment, False, "not a primary financial statement source", "supporting"))

    if financial and policy == "smart":
        # Use a generic FinancialStatement*.pdf only when the issuer did not
        # provide a clearly named LK/Laporan Keuangan PDF. Prefer a main document
        # if several fallback candidates exist.
        if not primary_pdf_indexes and generic_pdf_indexes:
            preferred = sorted(
                generic_pdf_indexes,
                key=lambda i: (bool(preliminary[i].attachment.get("IsAttachment")), i),
            )[0]
            decision = preliminary[preferred]
            preliminary[preferred] = AttachmentDecision(
                decision.attachment,
                True,
                "fallback financial-statement PDF because no LK/report PDF was found",
                "financial_statement",
            )
        elif primary_pdf_indexes:
            for index in generic_pdf_indexes:
                decision = preliminary[index]
                preliminary[index] = AttachmentDecision(
                    decision.attachment,
                    False,
                    "duplicate financial-statement PDF; preferred LK/report PDF is available",
                    "financial_statement_duplicate",
                )

        if not primary_pdf_indexes and not generic_pdf_indexes:
            # Last-resort opaque filename handling. Accept exactly one
            # non-administrative main PDF so unusual issuers still work.
            candidates: list[int] = []
            for index, decision in enumerate(preliminary):
                filename = decision.filename
                normalized = _norm(filename)
                if _suffix(filename) != ".pdf":
                    continue
                if any(pattern in normalized for pattern in ADMINISTRATIVE_PATTERNS):
                    continue
                if not bool(decision.attachment.get("IsAttachment")):
                    candidates.append(index)
            if len(candidates) == 1:
                index = candidates[0]
                decision = preliminary[index]
                preliminary[index] = AttachmentDecision(
                    decision.attachment,
                    True,
                    "fallback main PDF because no named financial-statement PDF was found",
                    "financial_statement",
                )

    return preliminary

