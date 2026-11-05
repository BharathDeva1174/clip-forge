"""Tests for configuration loading, validation and secret handling."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, is_loopback_host
from app.transcription import TranscriptionSettings


def test_defaults_are_safe_for_local_development() -> None:
    settings = Settings(_env_file=None)

    assert settings.port == 8000
    assert settings.environment == "development"
    assert settings.cors_origin_list == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_an_invalid_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, port="not-a-port")


def test_the_ai_request_timeout_defaults_to_sixty_seconds() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_request_timeout_seconds == 60.0


def test_the_ai_request_timeout_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPFORGE_AI_REQUEST_TIMEOUT_SECONDS", "300")

    settings = Settings(_env_file=None)

    assert settings.ai_request_timeout_seconds == 300.0


def test_a_non_positive_ai_request_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_request_timeout_seconds=0)


def test_the_ai_retry_budget_defaults_to_two() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_max_retries == 2


def test_the_ai_retry_budget_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPFORGE_AI_MAX_RETRIES", "6")

    settings = Settings(_env_file=None)

    assert settings.ai_max_retries == 6


def test_an_out_of_range_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, port=70000)


def test_a_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins="*")


def test_environment_variables_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORGE_PORT", "9123")
    monkeypatch.setenv("CLIPFORGE_CORS_ORIGINS", "http://localhost:3000")

    settings = Settings(_env_file=None)

    assert settings.port == 9123
    assert settings.cors_origin_list == ["http://localhost:3000"]


def test_the_settings_hold_no_ai_credential() -> None:
    """The provider configuration is not an environment setting any more.

    It lives in the database, and the key for it lives in the operating
    system's credential store. There is therefore no secret in the settings
    that could be printed, logged or returned.
    """
    settings = Settings(_env_file=None)

    assert not any(
        "key" in name.lower() and "secret" in name.lower()
        for name in type(settings).model_fields
    )
    assert "llm_api_key" not in type(settings).model_fields
    assert "api_key" not in str(settings.model_dump_json()).lower()


# --- local-only binding ----------------------------------------------------


def test_the_api_binds_to_the_loopback_address_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.allow_lan_binding is False


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_a_loopback_address_is_accepted(host: str) -> None:
    settings = Settings(_env_file=None, host=host)
    assert settings.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_a_non_loopback_address_is_refused_by_default(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, host=host)


def test_a_non_loopback_address_needs_an_explicit_opt_in() -> None:
    settings = Settings(_env_file=None, host="0.0.0.0", allow_lan_binding=True)
    assert settings.host == "0.0.0.0"


def test_is_loopback_host_recognises_the_loopback_addresses() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("LOCALHOST") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("not-an-address") is False


# --- the Phase 3 settings --------------------------------------------------


def test_the_audio_settings_default_to_mono_16khz() -> None:
    settings = Settings(_env_file=None)

    assert settings.audio_sample_rate == 16000
    assert settings.audio_channels == 1
    assert settings.ffmpeg_path == "ffmpeg"
    assert settings.ffmpeg_timeout_seconds > 0
    assert settings.audio_temp_retention == "delete"


def test_the_transcription_settings_default_to_a_small_local_model() -> None:
    settings = TranscriptionSettings(_env_file=None)

    assert settings.model == "tiny.en"
    assert settings.device == "cpu"
    assert settings.compute_type is None
    assert settings.language is None
    assert settings.offline is False
    assert settings.max_concurrency == 1
    assert settings.timeout_seconds > 0


def test_the_transcription_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_MODEL", "base.en")
    monkeypatch.setenv("CLIPFORGE_TRANSCRIPTION_OFFLINE", "true")

    settings = TranscriptionSettings(_env_file=None)

    assert settings.model == "base.en"
    assert settings.offline is True


def test_the_transcription_device_has_to_be_a_real_one() -> None:
    with pytest.raises(ValidationError):
        TranscriptionSettings(_env_file=None, device="quantum")
