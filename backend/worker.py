"""Background worker entry point.

Run it as a separate process, next to the API::

    python worker.py

It uses the same configuration and the same database as the API.
"""

from __future__ import annotations

from app.worker import main

if __name__ == "__main__":
    main()
