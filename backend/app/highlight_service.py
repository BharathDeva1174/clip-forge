"""The highlight analysis service.

This is the piece that turns a stored transcript into stored candidates. It
is the only part of the application that knows both about the provider and
about highlights, and it is the only part that sends anything anywhere.

The order of operations is deliberate:

1. **The configuration is checked against the snapshot.** The analysis
   records the provider, the protocol, the base URL and the model it was
   queued against. If the integration has since been deleted, disabled or
   edited, the analysis **fails clearly**. It never falls back to another
   provider, and it never quietly uses a different model: the privacy, the
   cost and the output would all change without anybody asking.
2. **The transcript is loaded from the database.** The model never sees
   text the user did not transcribe, and the analysis never depends on
   what a client sends.
3. **The transcript is split into windows** when it does not fit.
4. **Each window is sent once.** There is no automatic second request: a
   model that answers unusably is reported, not nudged.
5. **The answers are validated** against the persisted segments, merged
   and deduplicated.
6. **The candidates are written** in one transaction, and the analysis is
   marked complete.

The service never logs a transcript, a prompt or a key.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai_client import AiClient, AiClientError
from app.config import Settings
from app.credentials import CredentialStore, resolve_secret
from app.db import (
    get_integration,
    list_transcript_segments,
    renew_analysis_lease,
    save_candidates,
    session,
    update_analysis,
)
from app.errors import ErrorCode
from app.highlight_prompt import (
    HIGHLIGHT_PROMPT_VERSION,
    HIGHLIGHT_SCHEMA_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
    estimate_tokens,
    plan_windows,
)
from app.highlights import (
    HIGHLIGHT_JSON_SCHEMA,
    HighlightAnalysis,
    HighlightCandidate,
    HighlightValidationError,
    SegmentReference,
    validate_candidates,
)
from app.integrations import (
    ApiProtocol,
    IntegrationRecord,
    IntegrationRole,
    StructuredOutputMode,
)
from app.protocols import LlmCall
from app.transcripts import Transcript

logger = logging.getLogger("clipforge.highlights")


class _LeaseRenewer:
    """Renews an analysis lease on a background thread while work runs.

    A provider call can take longer than the worker lease, so the lease must
    be renewed *during* the call, not only between windows. This renewer keeps
    the lease alive on a background thread using a separate database
    connection (SQLite connections are not shared across threads), and stops
    as soon as the call returns or the thread is cancelled.

    If renewal reports that this worker no longer owns the analysis, the
    thread stops and the in-progress work is treated as no longer owned: the
    caller must not publish its result. A *transient* renewal error (a busy
    database) is retried after a renewal has succeeded once, until the
    original lease would have run out; a renewal that has never succeeded
    stops immediately, because ownership was never confirmed.
    """

    def __init__(
        self,
        *,
        db_path,
        analysis_id: str,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        self._db_path = db_path
        self._analysis_id = analysis_id
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failed = threading.Event()

    def start(self) -> None:
        """Begin renewing the lease in the background."""
        self._thread = threading.Thread(target=self._renew_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop renewing and wait for the thread to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def failed(self) -> bool:
        """Whether renewal has failed, meaning this worker no longer owns it."""
        return self._failed.is_set()

    def _renew_loop(self) -> None:
        # A renewal can fail transiently -- SQLite under contention returns
        # "database is locked" -- and that is not the same as losing the
        # analysis. Another worker only owns it once the ownership-checked
        # UPDATE matches no row. So once a renewal has succeeded, a transient
        # error is retried until the original lease would have run out; only
        # then is ownership treated as lost. One transient error used to
        # stop renewal permanently while the provider call kept running,
        # which let the lease expire and got healthy work requeued.
        #
        # A renewal that has *never* succeeded is different: ownership was
        # never confirmed, so the work stops rather than risk publishing
        # someone else's analysis.
        started = time.monotonic()
        ever_succeeded = False
        while not self._stop.is_set():
            try:
                from app.db import session

                with session(self._db_path) as conn:
                    renewed = renew_analysis_lease(
                        conn,
                        self._analysis_id,
                        lease_seconds=self._lease_seconds,
                        worker_id=self._worker_id,
                    )
            except Exception:
                if not ever_succeeded:
                    logger.warning(
                        "the first lease renewal for analysis %s failed; "
                        "treating ownership as unconfirmed",
                        self._analysis_id,
                        exc_info=True,
                    )
                    self._failed.set()
                    break
                if time.monotonic() >= started + self._lease_seconds:
                    logger.warning(
                        "lease renewal for analysis %s kept failing past the "
                        "original lease; treating ownership as lost",
                        self._analysis_id,
                        exc_info=True,
                    )
                    self._failed.set()
                    break
                logger.debug(
                    "lease renewal for analysis %s failed transiently; "
                    "retrying",
                    self._analysis_id,
                )
                self._stop.wait(min(2.0, self._interval))
                continue
            if not renewed:
                # Another worker owns this analysis now. Stop renewing so the
                # stale worker cannot publish results.
                self._failed.set()
                break
            ever_succeeded = True
            self._stop.wait(self._interval)


#: A fenced block of JSON, which models like to add.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """What an analysis produced."""

    status: str
    error_code: str | None = None
    error_message: str | None = None
    candidate_count: int = 0


class HighlightRunner:
    """Runs one analysis against one provider."""

    def __init__(
        self,
        *,
        client: AiClient,
        credential_store: CredentialStore,
    ) -> None:
        self.client = client
        self.credential_store = credential_store

    def run(
        self,
        conn,
        analysis: HighlightAnalysis,
        *,
        transcript: Transcript | None,
        segments: list[SegmentReference],
        source_duration_seconds: float | None,
        source_name: str | None,
        on_tick: Callable[[], None] | None = None,
        db_path=None,
        lease_seconds: int = 300,
        renewal_interval_seconds: float = 30.0,
        settings: Settings | None = None,
    ) -> AnalysisOutcome:
        """Run the analysis and store what it produced.

        The transcript is split into segment-safe windows. Each window is sent
        once; on a transient provider failure it is retried with bounded
        exponential backoff and jitter, and on a context-limit rejection it is
        split into smaller windows and retried. All work finishes within the
        configured analysis deadline, and the lease is renewed *during* every
        blocking provider call and every backoff pause.
        """
        settings = settings or Settings()
        max_attempts = max(1, settings.ai_max_request_attempts)
        max_concurrent = max(1, min(settings.ai_max_concurrent_requests, 16))
        deadline = time.monotonic() + max(1.0, settings.ai_analysis_deadline_seconds)

        integration = self._resolve_integration(conn, analysis)
        if integration is None:
            return self._fail(
                conn,
                analysis,
                ErrorCode.INTEGRATION_CHANGED.value,
                "The integration was changed after this analysis was queued. "
                "Start the analysis again to use the new configuration.",
            )
        if not integration.enabled:
            return self._fail(
                conn,
                analysis,
                ErrorCode.INTEGRATION_DISABLED.value,
                "The integration was disabled after this analysis was queued.",
            )

        if transcript is None:
            return self._fail(
                conn,
                analysis,
                ErrorCode.HIGHLIGHT_NO_TRANSCRIPT.value,
                "This job has no transcript to analyse.",
            )
        if not segments:
            return self._fail(
                conn,
                analysis,
                ErrorCode.HIGHLIGHT_TRANSCRIPT_EMPTY.value,
                "The transcript holds no speech, so there is nothing to choose "
                "from.",
            )

        api_key = resolve_secret(self.credential_store, integration.secret_ref)

        windows = plan_windows(
            segments,
            max_segments=settings.ai_max_segments_per_window,
            overlap_segments=settings.ai_window_overlap_segments,
            max_characters=settings.ai_max_transcript_characters,
        )
        if not windows:
            return self._fail(
                conn,
                analysis,
                ErrorCode.HIGHLIGHT_TRANSCRIPT_EMPTY.value,
                "The transcript holds no speech, so there is nothing to choose "
                "from.",
            )

        collected: list[HighlightCandidate] = []
        renewer = _LeaseRenewer(
            db_path=db_path,
            analysis_id=analysis.id,
            worker_id=analysis.worker_id,
            lease_seconds=lease_seconds,
            interval_seconds=renewal_interval_seconds,
        )
        if db_path is not None:
            renewer.start()
        try:
            for window in windows:
                if renewer.failed():
                    return self._fail(
                        conn,
                        analysis,
                        ErrorCode.LEASE_EXPIRED.value,
                        "The worker lost ownership of this analysis while it "
                        "was running. It was queued again for another worker.",
                    )
                if on_tick is not None:
                    on_tick()

                result = self._analyse_window_with_retry(
                    integration,
                    api_key,
                    window=window,
                    segments=segments,
                    source_duration_seconds=source_duration_seconds,
                    source_name=source_name,
                    deadline=deadline,
                    renewer=renewer,
                    settings=settings,
                    max_concurrent=max_concurrent,
                )
                if renewer.failed():
                    # The lease could not be kept alive during this window.
                    return self._fail(
                        conn,
                        analysis,
                        ErrorCode.LEASE_EXPIRED.value,
                        "The worker lost ownership of this analysis while it "
                        "was running. It was queued again for another worker.",
                    )
                if result is None:
                    # Lease lost or deadline exhausted mid-retry; stop.
                    return self._fail(
                        conn,
                        analysis,
                        ErrorCode.LEASE_EXPIRED.value
                        if renewer.failed()
                        else ErrorCode.AI_TIMEOUT.value,
                        "The analysis could not finish within its deadline or "
                        "the worker lost ownership.",
                    )
                if isinstance(result, AiClientError):
                    return self._fail(conn, analysis, result.code, result.message)
                collected.extend(result)

        finally:
            if db_path is not None:
                renewer.stop()

        if db_path is not None and not renewer.failed():
            # One synchronous ownership check before publishing. A fast
            # provider can finish the whole analysis inside the background
            # renewer's first tick, so the thread alone cannot rule out a
            # takeover that happened mid-run.
            try:
                with session(db_path) as fresh:
                    still_owned = renew_analysis_lease(
                        fresh,
                        analysis.id,
                        lease_seconds=lease_seconds,
                        worker_id=analysis.worker_id,
                    )
            except Exception:
                logger.warning(
                    "could not re-check ownership of analysis %s before "
                    "publishing",
                    analysis.id,
                    exc_info=True,
                )
            else:
                if not still_owned:
                    return self._fail(
                        conn,
                        analysis,
                        ErrorCode.LEASE_EXPIRED.value,
                        "The worker lost ownership of this analysis while it "
                        "was running. It was queued again for another worker.",
                    )

        # Merge across the windows, keeping the model's own order and
        # dropping what substantially overlaps something already accepted.
        merged = _merge(collected)

        try:
            save_candidates(conn, analysis.id, merged)
        except Exception:
            logger.exception("could not store the highlight candidates")
            return self._fail(
                conn,
                analysis,
                ErrorCode.TRANSCRIPT_PERSISTENCE_FAILED.value,
                "The candidates could not be saved.",
            )

        published = update_analysis(
            conn,
            analysis.id,
            status="completed",
            clear_error=True,
            expected_worker_id=analysis.worker_id,
        )
        if published is None:
            # The ownership-checked write did not land: another worker owns
            # the analysis now, whatever the check above saw.
            return self._fail(
                conn,
                analysis,
                ErrorCode.LEASE_EXPIRED.value,
                "The worker lost ownership of this analysis while it "
                "was running. It was queued again for another worker.",
            )
        logger.info(
            "analysis %s completed with %s candidates",
            analysis.id,
            len(merged),
        )
        return AnalysisOutcome(status="completed", candidate_count=len(merged))

    # -- internals ----------------------------------------------------------

    def _resolve_integration(
        self, conn, analysis: HighlightAnalysis
    ) -> IntegrationRecord | None:
        """The integration, when it still matches what was queued.

        Returns ``None`` when the integration is gone or has changed, so
        that the analysis fails rather than running against something the
        user did not ask for.
        """
        if not analysis.integration_id:
            return None
        integration = get_integration(conn, analysis.integration_id)
        if integration is None:
            return None
        snapshot = analysis_snapshot(analysis)
        if integration.snapshot() != snapshot:
            return None
        return integration

    def _analyse_window(
        self,
        integration: IntegrationRecord,
        api_key: str | None,
        *,
        window,
        segments: list[SegmentReference],
        source_duration_seconds: float | None,
        source_name: str | None,
    ) -> list[HighlightCandidate]:
        """Send one window and validate the answer."""
        supports_schema = (
            integration.capabilities.structured_output
            is StructuredOutputMode.JSON_SCHEMA
        )
        call = LlmCall(
            base_url=integration.base_url,
            model=integration.model_id or "",
            system=SYSTEM_PROMPT,
            user=build_user_prompt(window, source_name=source_name),
            api_key=api_key,
            capabilities=integration.capabilities,
            reasoning_effort=integration.reasoning_effort,
            max_output_tokens=integration.max_output_tokens,
            temperature=0.2,
            json_schema=HIGHLIGHT_JSON_SCHEMA if supports_schema else None,
            json_schema_name="clipforge_highlights",
        )
        reply = self.client.complete(integration, api_key, call)
        payload = parse_model_json(reply)
        return validate_candidates(
            payload,
            segments,
            source_duration_seconds=source_duration_seconds,
        )

    def _analyse_window_with_retry(
        self,
        integration: IntegrationRecord,
        api_key: str | None,
        *,
        window,
        segments: list[SegmentReference],
        source_duration_seconds: float | None,
        source_name: str | None,
        deadline: float,
        renewer: _LeaseRenewer,
        settings: Settings,
        max_concurrent: int,
    ) -> list[HighlightCandidate] | AiClientError | None:
        """Analyse one window with retries and context-limit recovery.

        Returns the validated candidates, an ``AiClientError`` on permanent
        failure, or ``None`` when the lease was lost or the deadline expired.
        """
        min_segments = max(1, settings.ai_min_segments_per_window)
        max_attempts = max(1, settings.ai_max_request_attempts)
        max_backoff = max(1.0, settings.ai_max_backoff_seconds)

        # Work list of (window, attempt) pairs. A context-limit rejection
        # replaces its window with smaller windows rather than retrying it.
        work: list[tuple] = [(window, 0)]

        while work:
            if renewer.failed():
                return None
            if time.monotonic() >= deadline:
                return None

            current, attempt = work.pop(0)

            try:
                return self._analyse_window(
                    integration,
                    api_key,
                    window=current,
                    segments=segments,
                    source_duration_seconds=source_duration_seconds,
                    source_name=source_name,
                )
            except HighlightValidationError as exc:
                return AiClientError(
                    ErrorCode.AI_INVALID_OUTPUT.value,
                    "The model did not answer in the expected format.",
                )
            except AiClientError as exc:
                if exc.code == ErrorCode.AI_CONTEXT_LIMIT_EXCEEDED.value:
                    # Split the window into smaller segment-safe windows and
                    # retry those instead of repeating the oversized request.
                    smaller = self._split_window(
                        current, min_segments=min_segments
                    )
                    if not smaller:
                        return exc
                    # Re-queue smaller windows ahead of remaining work so they
                    # are analysed before later windows.
                    work = [(w, 0) for w in smaller] + work
                    continue
                if exc.code in (
                    ErrorCode.AI_TIMEOUT.value,
                    ErrorCode.AI_UNAVAILABLE.value,
                    ErrorCode.AI_RATE_LIMITED.value,
                ):
                    if attempt + 1 >= max_attempts:
                        return exc
                    # Renew the lease during the backoff pause so a long
                    # sequence of retries does not let the lease lapse.
                    delay = min(
                        max_backoff,
                        (2.0 ** attempt) + random.uniform(0.0, 0.25 * (2.0 ** attempt)),
                    )
                    self._sleep_with_renewal(renewer, deadline, delay)
                    work.append((current, attempt + 1))
                    continue
                # Permanent failure (auth, invalid request, protocol, etc.).
                return exc

        return None

    def _split_window(
        self, window, *, min_segments: int
    ) -> list:
        """Split a context-limit-rejected window into smaller segment-safe windows.

        Never splits a segment and never creates a window smaller than
        ``min_segments`` segments.
        """
        segments = list(window.segments)
        if len(segments) <= min_segments:
            return []
        half = max(min_segments, len(segments) // 2)
        first = segments[:half]
        second = segments[half:]
        result: list = []
        for group in (first, second):
            if not group:
                continue
            result.append(
                type(window)(
                    segments=tuple(group),
                    first_segment_index=group[0].index,
                    last_segment_index=group[-1].index,
                    start_seconds=group[0].start_seconds,
                    end_seconds=group[-1].end_seconds,
                    character_count=sum(len(s.text) for s in group),
                )
            )
        return result

    @staticmethod
    def _sleep_with_renewal(
        renewer: _LeaseRenewer, deadline: float, delay: float
    ) -> None:
        """Sleep for ``delay`` seconds, renewing the lease along the way.

        Returns early when the deadline passes or renewal fails.
        """
        step = min(0.5, delay)
        elapsed = 0.0
        while elapsed < delay and time.monotonic() < deadline and not renewer.failed():
            renewer._stop.wait(min(step, delay - elapsed))
            elapsed += step

    def _fail(
        self, conn, analysis: HighlightAnalysis, code: str, message: str
    ) -> AnalysisOutcome:
        """Record a failure on the analysis."""
        logger.info("analysis %s failed with %s", analysis.id, code)
        update_analysis(
            conn,
            analysis.id,
            status="failed",
            error_code=code,
            error_message=message,
            expected_worker_id=analysis.worker_id,
        )
        return AnalysisOutcome(status="failed", error_code=code, error_message=message)


def analysis_snapshot(analysis: HighlightAnalysis) -> dict[str, object]:
    """The configuration an analysis was queued against."""
    return {
        "provider_id": analysis.provider_id,
        "protocol": analysis.protocol,
        "base_url": analysis.base_url,
        "model_id": analysis.model_id,
    }


def _window_characters(integration: IntegrationRecord) -> int:
    """How much transcript text one request may carry."""
    from app.highlight_prompt import DEFAULT_MAX_TRANSCRIPT_CHARACTERS

    return DEFAULT_MAX_TRANSCRIPT_CHARACTERS


def _merge(candidates: list[HighlightCandidate]) -> list[HighlightCandidate]:
    """Merge the candidates from every window, in order.

    A candidate that repeats, or that substantially overlaps one already
    accepted, is dropped. The rest keep the order the model gave them,
    which is its own ranking.
    """
    from app.highlights import _overlaps_an_accepted  # local, deterministic

    accepted: list[HighlightCandidate] = []
    for candidate in candidates:
        if _overlaps_an_accepted(candidate, accepted):
            continue
        accepted.append(
            HighlightCandidate(
                id=candidate.id,
                position=len(accepted),
                start_segment_index=candidate.start_segment_index,
                end_segment_index=candidate.end_segment_index,
                start_seconds=candidate.start_seconds,
                end_seconds=candidate.end_seconds,
                title=candidate.title,
                reason=candidate.reason,
                moment_type=candidate.moment_type,
                score=candidate.score,
                decision=candidate.decision,
            )
        )
    return accepted


def parse_model_json(text: str) -> Any:
    """Read the JSON out of a model's answer.

    Models wrap their JSON in different ways: bare, in a fenced block, or
    with a sentence in front of it. All of those are handled. Anything
    else is a failure, not something to be guessed at.
    """
    candidate = text.strip()
    if not candidate:
        raise HighlightValidationError("The model returned nothing.")

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # The answer may have something in front of or after the JSON.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            pass

    start = candidate.find("[")
    end = candidate.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise HighlightValidationError("The model's answer was not JSON.")


def segment_references(
    segments: list, duration_seconds: float | None
) -> list[SegmentReference]:
    """Build the references the model is given."""
    return [
        SegmentReference(
            index=segment.segment_index,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            text=segment.text,
        )
        for segment in segments
    ]
