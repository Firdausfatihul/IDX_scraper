from idx_digest.financial_refiner import CachedFinancialRefiner


class FakeDB:
    def announcement_attachments(self, announcement_id):
        assert announcement_id == "ANN-1"
        return [
            {
                "url": "https://idx.test/lk.pdf",
                "original_filename": "LK PJAA 30 Juni 2026.pdf",
                "is_attachment": 1,
            },
            {
                "url": "https://idx.test/fs.pdf",
                "original_filename": "FinancialStatement-2026-II-PJAA.pdf",
                "is_attachment": 0,
            },
        ]


def test_cached_refiner_merges_raw_json_and_attachment_table():
    refiner = CachedFinancialRefiner.__new__(CachedFinancialRefiner)
    refiner.db = FakeDB()
    merged = refiner._cached_attachment_candidates(
        {"id2": "ANN-1"},
        [
            {
                "FullSavePath": "https://idx.test/fs.xlsx",
                "OriginalFilename": "FinancialStatement-2026-II-PJAA.xlsx",
                "IsAttachment": True,
            }
        ],
    )
    names = [item["OriginalFilename"] for item in merged]
    assert names == [
        "FinancialStatement-2026-II-PJAA.xlsx",
        "LK PJAA 30 Juni 2026.pdf",
        "FinancialStatement-2026-II-PJAA.pdf",
    ]
