from idx_digest.attachment_dedup import AttachmentEvidence, deduplicate_attachments


def _long(label: str) -> str:
    return " ".join([label, "pemegang saham perusahaan tercatat jumlah kepemilikan 123456789"] * 180)


def test_exact_duplicate_suppressed_but_cross_format_financial_pair_survives():
    text = _long("laporan")
    items = [
        AttachmentEvidence("main", "main.pdf", text, sha256="same", is_attachment=False),
        AttachmentEvidence("copy", "lamp1.pdf", text, sha256="same", is_attachment=True),
        AttachmentEvidence("sheet", "FinancialStatement.xlsx", text, sha256="different", is_attachment=True),
    ]
    decisions = {d.url: d for d in deduplicate_attachments(items)}
    assert decisions["main"].keep is True
    assert decisions["copy"].keep is False
    assert decisions["copy"].category == "exact_duplicate"
    assert decisions["copy"].duplicate_of_url == "main"
    assert decisions["sheet"].keep is True


def test_high_confidence_near_duplicate_same_format_is_suppressed():
    base = _long("registrasi")
    near = base + " catatan administratif"
    items = [
        AttachmentEvidence("a", "main.pdf", base, is_attachment=False),
        AttachmentEvidence("b", "lamp.pdf", near, is_attachment=True),
    ]
    decisions = {d.url: d for d in deduplicate_attachments(items, near_threshold=0.98)}
    assert decisions["a"].keep is True
    assert decisions["b"].keep is False
    assert decisions["b"].category == "near_duplicate"
