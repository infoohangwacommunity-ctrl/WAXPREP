import pytest

from waxprep.settings import load_settings


def test_whatsapp_enabled_requires_secrets() -> None:
    with pytest.raises(ValueError, match="WHATSAPP_TOKEN"):
        load_settings({"WAXPREP_WHATSAPP_ENABLED": "true"})


def test_voice_provider_requires_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        load_settings({"WAXPREP_VOICE_PROVIDER": "gemini"})


def test_defaults_ok() -> None:
    s = load_settings({})
    assert not s.whatsapp_enabled
    assert s.voice_provider is None
