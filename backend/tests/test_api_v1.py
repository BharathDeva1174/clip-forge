"""Tests for the versioned API metadata endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_api_v1_returns_200(client: TestClient) -> None:
    response = client.get("/api/v1")
    assert response.status_code == 200


def test_api_v1_returns_the_expected_metadata(client: TestClient) -> None:
    body = client.get("/api/v1").json()

    assert body["name"] == "ClipForge API"
    assert body["version"]
    assert body["stage"] == "phase-2-ingestion"
    assert body["docs"] == "/docs"
