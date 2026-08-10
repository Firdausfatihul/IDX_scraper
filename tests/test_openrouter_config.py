from idx_digest.config import Settings


def test_openrouter_defaults_pin_deepinfra_without_fallbacks() -> None:
    settings = Settings(_env_file=None)

    assert settings.openrouter_model == "deepseek/deepseek-v4-flash-0731"
    assert settings.openrouter_provider_preferences == {
        "only": ["deepinfra"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_openrouter_provider_can_be_overridden() -> None:
    settings = Settings(
        _env_file=None,
        openrouter_provider="fireworks",
        openrouter_allow_fallbacks=True,
        openrouter_require_parameters=False,
    )

    assert settings.openrouter_provider_preferences == {
        "only": ["fireworks"],
        "allow_fallbacks": True,
        "require_parameters": False,
    }
