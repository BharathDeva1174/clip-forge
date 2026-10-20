"""Development entry point.

Starts the API on the host and port declared in the configuration
(``CLIPFORGE_HOST`` / ``CLIPFORGE_PORT``) with auto-reload enabled.

    python run.py
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
