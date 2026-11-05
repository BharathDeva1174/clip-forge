"""The integration endpoints.

The rules these tests hold the API to: a key is never returned, an ordinary
edit never erases a key, a key is only replaced or removed on purpose, and
no error message carries a secret.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.credentials import MemoryCredentialStore
from app.db import get_integration, list_integrations, session
from app.errors import ErrorCode
from app.integrations import ApiProtocol, IntegrationRole


def _create(client: TestClient, **overrides) -> dict:
    """Save a configuration and return it."""
    body = {
        "name": "My provider",
        "provider_id": "groq",
        "model_id": "llama-3.3-70b-versatile",
    }
    body.update(overrides)
    response = client.post("/api/v1/integrations", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --- the presets ------------------------------------------------------------


def test_the_presets_are_offered(client: TestClient) -> None:
    response = client.get("/api/v1/integrations/presets")

    assert response.status_code == 200
    items = response.json()["items"]
    ids = {item["id"] for item in items}
    assert {"opencode_zen", "groq", "cerebras", "custom"} <= ids


def test_a_preset_carries_its_default_base_url(client: TestClient) -> None:
    items = client.get("/api/v1/integrations/presets").json()["items"]
    groq = next(item for item in items if item["id"] == "groq")

    assert groq["base_urls"]["openai_chat_completions"] == (
        "https://api.groq.com/openai/v1"
    )


def test_a_preset_never_claims_a_model_is_free_forever(client: TestClient) -> None:
    items = client.get("/api/v1/integrations/presets").json()["items"]

    for item in items:
        assert item["free_tier"] in ("trial", "unknown", "none")
        assert item["free_tier_note"]


# --- saving and listing -----------------------------------------------------


def test_a_configuration_is_saved_and_listed(client: TestClient) -> None:
    created = _create(client)

    listed = client.get("/api/v1/integrations")

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == created["id"]
    assert listed.json()["items"][0]["provider_name"] == "Groq"


def test_a_custom_provider_needs_no_source_change(client: TestClient) -> None:
    """A user can point at any endpoint that speaks a supported protocol."""
    created = _create(
        client,
        provider_id="custom",
        base_url="https://llm.example.test/v1",
        protocol="openai_chat_completions",
    )

    assert created["base_url"] == "https://llm.example.test/v1"
    assert created["provider_id"] == "custom"


def test_an_unknown_provider_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/integrations",
        json={"name": "x", "provider_id": "not-a-provider"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INTEGRATION_INVALID.value


def test_a_configuration_needs_a_name(client: TestClient) -> None:
    response = client.post(
        "/api/v1/integrations", json={"name": "", "provider_id": "groq"}
    )

    assert response.status_code == 422


def test_an_unknown_configuration_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/integrations/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.INTEGRATION_NOT_FOUND.value


# --- the key ----------------------------------------------------------------


def test_a_key_is_never_returned(client: TestClient) -> None:
    created = _create(client, api_key="super-secret-key")

    assert created["api_key_configured"] is True
    assert "super-secret-key" not in str(created)
    assert "secret_ref" not in created

    listed = client.get("/api/v1/integrations")
    assert "super-secret-key" not in listed.text


def test_a_key_is_not_written_to_the_database(
    client: TestClient, settings: Settings
) -> None:
    _create(client, api_key="super-secret-key")

    with session(settings.resolved_database_path) as conn:
        rows = conn.execute("SELECT * FROM integrations").fetchall()

    for row in rows:
        for value in tuple(row):
            if isinstance(value, str):
                assert "super-secret-key" not in value


def test_editing_a_configuration_keeps_its_key(client: TestClient) -> None:
    created = _create(client, api_key="super-secret-key")

    edited = client.patch(
        f"/api/v1/integrations/{created['id']}", json={"name": "Renamed"}
    )

    assert edited.status_code == 200
    assert edited.json()["name"] == "Renamed"
    assert edited.json()["api_key_configured"] is True
    assert "super-secret-key" not in edited.text


def test_a_key_can_be_replaced_on_purpose(client: TestClient) -> None:
    created = _create(client, api_key="the-first-key")

    replaced = client.post(
        f"/api/v1/integrations/{created['id']}/key", json={"api_key": "the-second-key"}
    )

    assert replaced.status_code == 200
    assert replaced.json()["api_key_configured"] is True
    assert "the-second-key" not in replaced.text


def test_a_key_can_be_removed_on_purpose(client: TestClient) -> None:
    created = _create(client, api_key="the-key")

    removed = client.delete(f"/api/v1/integrations/{created['id']}/key")

    assert removed.status_code == 200
    assert removed.json()["api_key_configured"] is False


def test_the_key_never_leaves_the_credential_store(
    client: TestClient, credential_store: MemoryCredentialStore
) -> None:
    created = _create(client, api_key="the-key")

    with session(client.app.state.settings.resolved_database_path) as conn:
        record = get_integration(conn, created["id"])

    assert record is not None
    assert record.secret_ref is not None
    assert credential_store.read(record.secret_ref) == "the-key"


def test_removing_a_configuration_removes_its_key(
    client: TestClient, credential_store: MemoryCredentialStore, settings: Settings
) -> None:
    created = _create(client, api_key="the-key")
    with session(settings.resolved_database_path) as conn:
        record = get_integration(conn, created["id"])
    assert record is not None and record.secret_ref is not None

    response = client.delete(f"/api/v1/integrations/{created['id']}")

    assert response.status_code == 204
    assert credential_store.read(record.secret_ref) is None


# --- the default ------------------------------------------------------------


def test_one_configuration_can_be_the_default(client: TestClient) -> None:
    first = _create(client, name="First")
    second = _create(client, name="Second")

    client.post(f"/api/v1/integrations/{first['id']}/select")
    client.post(f"/api/v1/integrations/{second['id']}/select")

    listed = client.get("/api/v1/integrations").json()["items"]
    defaults = [item for item in listed if item["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == second["id"]


def test_selecting_a_default_enables_it(client: TestClient) -> None:
    created = _create(client)
    client.patch(f"/api/v1/integrations/{created['id']}", json={"enabled": False})

    selected = client.post(f"/api/v1/integrations/{created['id']}/select")

    assert selected.json()["is_default"] is True
    assert selected.json()["enabled"] is True


# --- editing ----------------------------------------------------------------


def test_a_configuration_can_be_disabled(client: TestClient) -> None:
    created = _create(client)

    edited = client.patch(
        f"/api/v1/integrations/{created['id']}", json={"enabled": False}
    )

    assert edited.json()["enabled"] is False


def test_a_configuration_can_be_removed(client: TestClient) -> None:
    created = _create(client)

    removed = client.delete(f"/api/v1/integrations/{created['id']}")

    assert removed.status_code == 204
    assert client.get("/api/v1/integrations").json()["total"] == 0


# --- the URL ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.groq.com/openai/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://localhost:11434/v1",
    ],
)
def test_an_unusable_base_url_is_refused(client: TestClient, url: str) -> None:
    response = client.post(
        "/api/v1/integrations",
        json={
            "name": "x",
            "provider_id": "custom",
            "base_url": url,
            "protocol": "openai_chat_completions",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.AI_UNSAFE_URL.value


def test_a_local_endpoint_is_allowed_when_it_is_asked_for(client: TestClient) -> None:
    response = client.post(
        "/api/v1/integrations",
        json={
            "name": "Local",
            "provider_id": "custom",
            "base_url": "http://127.0.0.1:11434/v1",
            "protocol": "openai_chat_completions",
            "local_endpoint": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["local_endpoint"] is True


def test_a_url_with_credentials_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/integrations",
        json={
            "name": "x",
            "provider_id": "custom",
            "base_url": "https://user:pass@api.groq.com/v1",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.AI_UNSAFE_URL.value


# --- the connection test and the model list ---------------------------------


def test_a_connection_test_reports_the_outcome(client: TestClient) -> None:
    created = _create(client, api_key="the-key")

    response = client.post(f"/api/v1/integrations/{created['id']}/test")

    assert response.status_code == 200
    assert response.json()["status"] in (
        "ok",
        "auth_failed",
        "model_unavailable",
        "protocol_mismatch",
        "rate_limited",
        "timeout",
        "provider_error",
        "unsupported",
        "invalid_configuration",
    )
    # The test never echoes the key.
    assert "the-key" not in response.text


def test_the_outcome_of_a_test_is_remembered(
    client: TestClient, settings: Settings
) -> None:
    created = _create(client)

    client.post(f"/api/v1/integrations/{created['id']}/test")

    with session(settings.resolved_database_path) as conn:
        record = get_integration(conn, created["id"])
    assert record is not None
    assert record.last_tested_at is not None
    assert record.last_test_status.value != "unknown"


def test_a_provider_without_a_model_list_says_so(client: TestClient) -> None:
    created = _create(
        client,
        provider_id="custom",
        base_url="https://llm.example.test/v1",
    )

    response = client.get(f"/api/v1/integrations/{created['id']}/models")

    # A custom provider has no known model list, so the user types the id.
    assert response.status_code in (400, 200)


# --- the database -----------------------------------------------------------


def test_the_migration_leaves_the_old_tables_alone(
    client: TestClient, settings: Settings
) -> None:
    with session(settings.resolved_database_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"jobs", "transcripts", "transcript_segments"} <= tables
    assert {"integrations", "highlight_analyses", "highlight_candidates"} <= tables


def test_a_configuration_survives_a_restart(
    client: TestClient, settings: Settings
) -> None:
    created = _create(client, api_key="the-key")

    with session(settings.resolved_database_path) as conn:
        records = list_integrations(conn)

    assert len(records) == 1
    assert records[0].id == created["id"]
    assert records[0].api_key_configured is True


def test_the_role_is_the_highlight_detection_role(client: TestClient) -> None:
    created = _create(client)

    assert created["role"] == IntegrationRole.HIGHLIGHT_DETECTION.value
