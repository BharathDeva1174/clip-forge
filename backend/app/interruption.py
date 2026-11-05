"""A step that was interrupted on purpose.

Long-running steps -- an FFmpeg conversion, a speech-to-text pass -- can be
told to stop. The exception lives in its own module so that every layer can
raise it without importing the worker, and so that the layers which wrap
unexpected exceptions can let it through untouched.
"""

from __future__ import annotations


class WorkInterrupted(Exception):
    """Raised when a long-running step is interrupted deliberately.

    This is never a failure of the work itself. The caller is expected to
    release the job back to the queue rather than to mark it as failed.
    """


class WorkCancelled(WorkInterrupted):
    """Raised when the parent job of a piece of work was cancelled.

    Unlike a plain interruption, this one is terminal on purpose: the job
    can never run again, so the artifact is recorded as failed with the
    stable ``job_cancelled`` code instead of being queued for a retry that
    can never be wanted.
    """
