"""A worker that runs as its own operating-system process.

The process-level tests launch this instead of ``worker.py`` so that the
inference process can be pointed at the stub host. Everything else -- the
settings, the database, the storage layout, the pipeline -- is the real
thing, so the test exercises the production code path.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.transcription import (  # noqa: E402
    TranscriptionSettings,
    resolve_cache_dir,
)
from app.transcription_process import ProcessTranscriptionEngine  # noqa: E402
from app.worker import Worker  # noqa: E402

HOST_STUB = Path(__file__).resolve().parent / "host_stub.py"


def main() -> None:
    """Run the worker until it is asked to stop."""
    settings = get_settings()
    configure_logging(settings.log_level)

    transcription_settings = TranscriptionSettings()
    engine = ProcessTranscriptionEngine(
        transcription_settings,
        cache_dir=resolve_cache_dir(transcription_settings, settings.storage_root),
        sample_rate=settings.audio_sample_rate,
        channels=settings.audio_channels,
        host_path=HOST_STUB,
    )

    worker = Worker(settings, transcriber=engine)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.request_stop()


if __name__ == "__main__":
    main()
