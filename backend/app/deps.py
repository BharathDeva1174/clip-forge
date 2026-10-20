"""FastAPI dependencies shared by the routers.

The settings and the storage layout are attached to the application state in
:func:`app.main.create_app`, so tests can build an app with a temporary
storage root and a temporary database without touching the environment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from app.config import Settings
from app.db import session
from app.storage import Storage


def get_settings(request: Request) -> Settings:
    """The settings the application was built with."""
    return request.app.state.settings


def get_storage(request: Request) -> Storage:
    """The storage layout for the application."""
    return request.app.state.storage


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """A database connection with the schema applied."""
    settings: Settings = request.app.state.settings
    with session(
        settings.resolved_database_path,
        busy_timeout_seconds=settings.database_busy_timeout_seconds,
        wal_mode=settings.database_wal_mode,
    ) as conn:
        yield conn
