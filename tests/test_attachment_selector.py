from idx_digest.attachment_selector import classify_attachments


def att(name: str, *, is_attachment: bool = True):
    return {
        "OriginalFilename": name,
        "PDFFilename": name,
        "FullSavePath": f"https://idx.test/{name}",
        "IsAttachment": is_attachment,
    }


def selected_names(title: str, names):
    decisions = classify_attachments(title, names, policy="smart")
    return [d.filename for d in decisions if d.selected], decisions


def test_pjaa_financial_bundle_keeps_xlsx_and_lk_pdf_only():
    attachments = [
        att("3. Surat Pernyataan Direksi (28.7.26).pdf"),
        att("Checklist PJAA 30 Juni 2026.pdf"),
        att("FinancialStatement-2026-II-PJAA.xlsx"),
        att("inlineXBRL.zip"),
        att("instance.zip"),
        att("LK PJAA 30 Juni 2026.pdf"),
    ]
    selected, decisions = selected_names(
        "Penyampaian Laporan Keuangan Interim Yang Tidak Diaudit", attachments
    )
    assert selected == ["FinancialStatement-2026-II-PJAA.xlsx", "LK PJAA 30 Juni 2026.pdf"]
    reasons = {d.filename: d.reason for d in decisions}
    assert "administrative" in reasons["Checklist PJAA 30 Juni 2026.pdf"]
    assert "ZIP" in reasons["inlineXBRL.zip"]


def test_abda_financial_bundle_keeps_xlsx_and_report_pdf_only():
    attachments = [
        att("CHECKLIST PENGUNGKAPAN LK TRIWULAN II 2026.pdf"),
        att("FinancialStatement-2026-II-ABDA.xlsx"),
        att("inlineXBRL.zip"),
        att("instance.zip"),
        att("Laporan Keuangan TW II 2026 - ABDA.pdf"),
        att("Surat Pernyataan Manajemen II - 2026.pdf"),
    ]
    selected, _ = selected_names(
        "Penyampaian Laporan Keuangan Interim Yang Tidak Diaudit", attachments
    )
    assert selected == ["FinancialStatement-2026-II-ABDA.xlsx", "Laporan Keuangan TW II 2026 - ABDA.pdf"]


def test_non_financial_announcement_keeps_supported_documents_but_not_zip():
    attachments = [att("main.pdf", is_attachment=False), att("lampiran.xlsx"), att("bundle.zip")]
    selected, _ = selected_names("Perubahan Susunan Pengurus", attachments)
    assert selected == ["main.pdf", "lampiran.xlsx"]


def test_all_supported_policy_keeps_checklist_but_still_skips_unsupported_zip():
    attachments = [att("Checklist.pdf"), att("FinancialStatement.xlsx"), att("inlineXBRL.zip")]
    decisions = classify_attachments("Laporan Keuangan Interim", attachments, policy="all_supported")
    assert [d.filename for d in decisions if d.selected] == ["Checklist.pdf", "FinancialStatement.xlsx"]


def test_pjaa_cached_bundle_prefers_xlsx_and_lk_over_generic_financialstatement_pdf():
    attachments = [
        att("FinancialStatement-2026-II-PJAA.xlsx"),
        att("FinancialStatement-2026-II-PJAA.pdf", is_attachment=False),
        att("LK PJAA 30 Juni 2026.pdf"),
        att("Checklist PJAA 30 Juni 2026.pdf"),
    ]
    selected, decisions = selected_names(
        "Penyampaian Laporan Keuangan Interim Yang Tidak Diaudit", attachments
    )
    assert selected == ["FinancialStatement-2026-II-PJAA.xlsx", "LK PJAA 30 Juni 2026.pdf"]
    duplicate = next(d for d in decisions if d.filename == "FinancialStatement-2026-II-PJAA.pdf")
    assert duplicate.selected is False
    assert "duplicate" in duplicate.reason


def test_generic_financialstatement_pdf_is_used_when_no_lk_pdf_exists():
    attachments = [
        att("FinancialStatement-2026-II-TEST.xlsx"),
        att("FinancialStatement-2026-II-TEST.pdf", is_attachment=False),
    ]
    selected, _ = selected_names("Laporan Keuangan Interim", attachments)
    assert selected == ["FinancialStatement-2026-II-TEST.xlsx", "FinancialStatement-2026-II-TEST.pdf"]


def test_srsn_lktt_pdf_is_primary_and_generic_financialstatement_pdf_is_skipped():
    attachments = [
        att("FinancialStatement-2026-II-SRSN.xlsx"),
        att("FinancialStatement-2026-II-SRSN.pdf", is_attachment=False),
        att("LKTT Q2 2026_Srsn.pdf"),
        att("inlineXBRL.zip"),
    ]
    selected, decisions = selected_names(
        "Penyampaian Laporan Keuangan Interim Yang Tidak Diaudit", attachments
    )
    assert selected == ["FinancialStatement-2026-II-SRSN.xlsx", "LKTT Q2 2026_Srsn.pdf"]
    lktt = next(d for d in decisions if d.filename == "LKTT Q2 2026_Srsn.pdf")
    generic = next(d for d in decisions if d.filename == "FinancialStatement-2026-II-SRSN.pdf")
    assert lktt.selected is True
    assert lktt.category == "financial_statement"
    assert generic.selected is False
    assert "duplicate" in generic.reason
