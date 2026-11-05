"""Tests for the liveness, readiness and error-handling endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import SERVICE_NAME, Settings
from app.main import create_app

HEALTH_CONTRACT = {"status": "ok", "service": "clipforge-api"}


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_returns_the_exact_contract(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json() == HEALTH_CONTRACT


def test_ready_reports_a_loaded_configuration(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == SERVICE_NAME
    assert body["config"] == "loaded"
    assert body["version"]


def test_ready_reports_exactly_the_capabilities_that_exist(
    client: TestClient,
) -> None:
    body = client.get("/ready").json()
    capabilities = body["capabilities"]

    # The flags follow the implementation. Ingestion, the job database,
    # local storage, audio extraction and offline transcription arrived in
    # Phase 3; the highlight detection, the clip rendering and the vertical
    # formatting in Phases 4 to 6.
    #
    # The test's intent has not changed: it must not advertise a
    # capability that does not exist. It now asserts that *every* flag
    # matches the feature it names, in both directions.
    assert capabilities["video_ingestion"] is True
    assert capabilities["background_worker"] is True
    assert capabilities["database"] is True
    assert capabilities["persistent_storage"] is True
    assert capabilities["audio_extraction"] is True
    assert capabilities["transcription"] is True
    assert capabilities["llm_highlight_detection"] is True
    assert capabilities["clip_rendering"] is True
    assert capabilities["vertical_formatting"] is True
    assert capabilities["subtitle_generation"] is True

    # And there is no capability outside the known set, so a client cannot
    # be told about something that does not exist.
    assert set(capabilities) == {
        "video_ingestion",
        "background_worker",
        "database",
        "persistent_storage",
        "audio_extraction",
        "transcription",
        "llm_highlight_detection",
        "clip_rendering",
        "vertical_formatting",
        "subtitle_generation",
    }


def test_ready_reports_whether_the_local_model_is_cached(
    client: TestClient,
) -> None:
    body = client.get("/ready").json()
    # The flag says whether the model files are already on this machine. It
    # must be a boolean either way: it is never a path and never a leak.
    assert isinstance(body["transcription_model_cached"], bool)


def test_unknown_route_returns_404(client: TestClient) -> None:
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404


def test_cors_allows_the_configured_frontend_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_does_not_allow_an_unknown_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={
            "Origin": "https://not-clipforge.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_responses_never_leak_configured_secrets(client: TestClient) -> None:
    secret = "super-secret-llm-key"

    with TestClient(create_app(Settings(_env_file=None, llm_api_key=secret))) as app_client:
        assert secret not in app_client.get("/health").text
        assert secret not in app_client.get("/ready").text
        assert secret not in app_client.get("/api/v1").text
