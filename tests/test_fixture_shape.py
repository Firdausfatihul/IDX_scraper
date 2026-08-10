import json
from pathlib import Path


def test_uploaded_idx_fixture_shape():
    fixture = Path(__file__).parent / "fixtures" / "idx_response.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload["ResultCount"] == 680
    assert len(payload["Replies"]) == 10
    first = payload["Replies"][0]
    assert first["pengumuman"]["Kode_Emiten"].strip() == "EPAC"
    assert first["attachments"][0]["FullSavePath"].startswith("https://www.idx.co.id/")
    assert any(
        attachment["FullSavePath"].lower().endswith(".xlsx")
        for reply in payload["Replies"]
        for attachment in reply["attachments"]
    )
