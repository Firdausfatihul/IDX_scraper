from __future__ import annotations

from contextlib import contextmanager

from idx_digest.db import Database


def _announcement() -> dict:
    return {
        "pengumuman": {
            "Id2": "ANN-1",
            "Kode_Emiten": "TEST",
            "JudulPengumuman": "Test announcement",
        }
    }


def _attachment(index: int) -> dict:
    return {
        "FullSavePath": f"https://example.test/{index}.pdf",
        "OriginalFilename": f"{index}.pdf",
        "IsAttachment": True,
    }


def test_attachment_batch_uses_one_transaction_and_reports_selection_change(tmp_path):
    database = Database(tmp_path / "digest.sqlite3")
    database.upsert_announcement(_announcement(), "2026-08-10T10:00:00+07:00")

    original_connect = database.connect
    connection_count = 0

    @contextmanager
    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        with original_connect() as connection:
            yield connection

    database.connect = counted_connect
    records = [(_attachment(index), True, "selected", "primary") for index in range(3)]

    assert database.upsert_attachments("ANN-1", records) is False
    assert connection_count == 1

    changed = [
        (_attachment(0), False, "excluded", "supporting"),
        *records[1:],
    ]
    assert database.upsert_attachments("ANN-1", changed) is True
    assert connection_count == 2
