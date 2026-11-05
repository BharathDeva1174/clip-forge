"""FastAPI dependencies shared by the routers.

The settings and the storage layout are attached to the application state in
:func:`app.main.create_app`, so tests can build an app with a temporary
storage root and a temporary database without touching the environment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from app.ai_client import AiClient
from app.config import Settings
from app.credentials import CredentialStore
from app.db import session
from app.storage import Storage


def get_settings(request: Request) -> Settings:
    """The settings the application was built with."""
    return request.app.state.settings


def get_storage(request: Request) -> Storage:
    """The storage layout for the application."""
    return request.app.state.storage


def get_ai_client(request: Request) -> AiClient:
    """The client used to talk to AI providers.

    It is on the application state so that a test can put a client with a
    mocked transport in its place, and so that the whole application
    shares one configured client.
    """
    return request.app.state.ai_client


def get_credential_store(request: Request) -> CredentialStore:
    """The credential store the application saves API keys in."""
    return request.app.state.credential_store


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """A database connection with the schema applied."""
    settings: Settings = request.app.state.settings
    with session(
        settings.resolved_database_path,
        busy_timeout_seconds=settings.database_busy_timeout_seconds,
        wal_mode=settings.database_wal_mode,
    ) as conn:
        yield conn
