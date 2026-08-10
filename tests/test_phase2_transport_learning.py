from __future__ import annotations

import base64
from types import SimpleNamespace

from idx_digest.browser_transport import IDXBrowserTransport
from idx_digest.config import Settings


class FakeResponse:
    status = 403
    headers = {"content-type": "text/html; charset=utf-8"}


class FakeRequest:
    def __init__(self):
        self.calls = 0
    def get(self, *_args, **_kwargs):
        self.calls += 1
        return FakeResponse()


def test_browser_transport_learns_in_page_after_context_403(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, idx_browser_verification_timeout_seconds=5, _env_file=None)
    transport = IDXBrowserTransport(settings)
    request = FakeRequest()
    transport.context = SimpleNamespace(request=request)  # type: ignore[assignment]
    transport.page = SimpleNamespace()  # keep start() from launching; fetch is monkeypatched
    payload = base64.b64encode(b"pdf-bytes").decode("ascii")
    monkeypatch.setattr(transport, "_fetch_bytes_in_page", lambda _url: {"status": 200, "contentType": "application/pdf", "bodyBase64": payload})

    first = transport.get_bytes("https://www.idx.co.id/a.pdf")
    second = transport.get_bytes("https://www.idx.co.id/b.pdf")

    assert first[0] == b"pdf-bytes"
    assert second[0] == b"pdf-bytes"
    assert request.calls == 1
    assert transport._attachment_transport_mode == "in-page"
