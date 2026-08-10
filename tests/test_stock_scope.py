from datetime import datetime
from types import SimpleNamespace

from idx_digest.idx_client import IDXClient


def test_idx_client_sends_stock_emiten_type_when_requested():
    client = IDXClient.__new__(IDXClient)
    client.settings = SimpleNamespace(idx_page_size=50, idx_request_delay_seconds=0)
    client.observer = None
    seen = []

    def fake_get_json(params):
        seen.append(dict(params))
        return {"ResultCount": 0, "Replies": []}

    client._get_json = fake_get_json
    list(client.iter_announcements(
        datetime(2026, 7, 1), datetime(2026, 7, 2), emiten_type="s"
    ))
    assert seen[0]["emitenType"] == "s"


def test_idx_client_can_fall_back_to_all_instruments():
    client = IDXClient.__new__(IDXClient)
    client.settings = SimpleNamespace(idx_page_size=50, idx_request_delay_seconds=0)
    client.observer = None
    seen = []
    client._get_json = lambda params: seen.append(dict(params)) or {"ResultCount": 0, "Replies": []}
    list(client.iter_announcements(
        datetime(2026, 7, 1), datetime(2026, 7, 2), emiten_type="*"
    ))
    assert seen[0]["emitenType"] == "*"
