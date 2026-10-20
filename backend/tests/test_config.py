"""Tests for configuration loading, validation and secret handling."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


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


def test_secret_values_are_masked_when_dumped() -> None:
    secret = "super-secret-llm-key"
    settings = Settings(_env_file=None, llm_api_key=secret)

    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == secret
    assert secret not in settings.model_dump_json()
    assert secret not in repr(settings)
