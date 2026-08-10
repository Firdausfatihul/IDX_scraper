from __future__ import annotations

from pathlib import Path

from idx_digest.db import Database


def test_document_summary_cache_is_invalidated_by_prompt_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "digest.sqlite3")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO announcements(id2,ticker,announced_at,title,raw_json,fetched_at)
            VALUES ('a','ANTM','2026-08-05T00:00:00+07:00','x','{}','now')
            """
        )
        conn.execute(
            """
            INSERT INTO attachments(url,announcement_id,original_filename,is_attachment,updated_at)
            VALUES ('https://example.test/a.pdf','a','a.pdf',0,'now')
            """
        )
    db.save_document_summary(
        "https://example.test/a.pdf",
        "ANTM",
        {"summary": "old"},
        "model-a",
        "prompt-a",
    )
    assert db.get_document_summary(
        "https://example.test/a.pdf", model="model-a", prompt_version="prompt-a"
    ) == {"summary": "old"}
    assert db.get_document_summary(
        "https://example.test/a.pdf", model="model-a", prompt_version="prompt-b"
    ) is None
    assert db.get_document_summary("https://example.test/a.pdf") is None
