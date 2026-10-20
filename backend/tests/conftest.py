"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    """A test client bound to a freshly built application."""
    with TestClient(create_app()) as test_client:
        yield test_client
