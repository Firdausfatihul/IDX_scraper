from idx_digest.config import Settings


def test_browser_transport_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.idx_transport == "auto"
    assert settings.idx_browser_headless is False
    assert str(settings.idx_browser_profile_dir).endswith("data/browser-profile")
