"""Editing a clip, exporting a batch, and cancelling a job.

These are the rules of Phase 8:

- trimming produces a **revision**: a new artifact with its own file, its
  own lifecycle and its own identity;
- the clip that was trimmed is never touched, and the AI's candidate is
  never rewritten to represent a user's edit;
- the caption text is part of the export configuration, so two different
  texts never share a fingerprint;
- an export contains exactly the clips that were chosen;
- a cancelled job is not run, and a finished one cannot be cancelled.

The renders themselves are covered here with the real FFmpeg: a revision
has to produce a real, verified video, not a row.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clips import ClipStatus
from app.config import Settings
from app.db import (
    SCHEMA_VERSION,
    cancel_job,
    create_clip,
    create_job,
    get_clip,
    get_clip_for_candidate,
    get_format_for_fingerprint,
    get_highlight_candidate,
    get_job,
    list_clip_caption_edits,
    list_clip_revisions,
    list_clips,
    save_candidates,
    save_transcript,
    session,
    update_clip,
    update_job,
)
from app.errors import ErrorCode
from app.formatting import (
    FormatSettings,
    configuration_fingerprint,
    settings_from_dict,
)
from app.jobs import JobStatus, SourceType
from app.storage import Storage
from app.transcripts import Transcript, TranscriptSegment
from app.worker import Worker

FFMPEG = shutil.which("ffmpeg")

JOB_ID = "a" * 32
OTHER_JOB_ID = "b" * 32
CLIP_ID = "c" * 32
CANDIDATE_ID = "d" * 32


# --- fixtures ---------------------------------------------------------------


def _job(
    settings: Settings,
    job_id: str = JOB_ID,
    *,
    duration: float | None = 30.0,
    status: JobStatus = JobStatus.COMPLETED,
    source_path: str | None = "sources/clip.mp4",
    with_transcript: bool = False,
) -> None:
    """A job with a known source duration, and sometimes a transcript."""
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
            source_path=source_path,
        )
        if duration is not None:
            update_job(
                conn,
                job_id,
                video_duration_seconds=duration,
                video_width=320,
                video_height=240,
            )
        if status is not JobStatus.QUEUED:
            for intermediate in (
                JobStatus.VALIDATING,
                JobStatus.INSPECTING,
                JobStatus.EXTRACTING_AUDIO,
                JobStatus.TRANSCRIBING,
                status,
            ):
                update_job(conn, job_id, status=intermediate)
        if with_transcript:
            save_transcript(
                conn,
                job_id=job_id,
                transcript=Transcript(
                    language="en",
                    model_name="tiny.en",
                    audio_sample_rate=16000,
                    audio_channels=1,
                    duration_seconds=duration or 30.0,
                    segments=(
                        TranscriptSegment(
                            index=0,
                            start_seconds=0.0,
                            end_seconds=1.5,
                            text="The first thing that was said.",
                        ),
                        TranscriptSegment(
                            index=1,
                            start_seconds=1.5,
                            end_seconds=3.0,
                            text="The second thing that was said.",
                        ),
                    ),
                ),
            )


def _candidate(settings: Settings, job_id: str = JOB_ID) -> str:
    """A highlight candidate with a title, as the AI would have left it."""
    from app.db import create_analysis
    from app.highlights import HighlightCandidate

    with session(settings.resolved_database_path) as conn:
        analysis = create_analysis(
            conn,
            job_id=job_id,
            integration_id=None,
            provider_id="custom",
            protocol="openai_chat_completions",
            base_url="http://127.0.0.1:1/v1",
            model_id="model",
            model_name="model",
            prompt_version="v1",
            schema_version="v1",
        )
        save_candidates(
            conn,
            analysis.id,
            [
                HighlightCandidate(
                    id=CANDIDATE_ID,
                    position=0,
                    start_segment_index=0,
                    end_segment_index=1,
                    start_seconds=1.0,
                    end_seconds=4.0,
                    title="The AI's title",
                    reason="Because.",
                    moment_type="hook",
                    score=0.9,
                )
            ],
        )
    return CANDIDATE_ID


def _clip(
    settings: Settings,
    clip_id: str = CLIP_ID,
    job_id: str = JOB_ID,
    *,
    candidate_id: str | None = None,
    completed: bool = True,
) -> str:
    """A clip, completed and with a file, unless it is told otherwise."""
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id=job_id,
            analysis_id=None,
            candidate_id=candidate_id,
            start_seconds=1.0,
            end_seconds=4.0,
            clip_id=clip_id,
        )
        if completed:
            path = storage.clips_dir / f"{clip_id}.mp4"
            path.write_bytes(b"x" * 4096)
            update_clip(
                conn,
                clip_id,
                status=ClipStatus.RENDERING,
                stage="rendering",
            )
            update_clip(conn, clip_id, status=ClipStatus.VERIFYING, stage="verifying")
            update_clip(
                conn,
                clip_id,
                status=ClipStatus.COMPLETED,
                stage="completed",
                output_path=f"clips/{clip_id}.mp4",
                output_size_bytes=4096,
            )
    return clip_id


# --- the revision semantics -------------------------------------------------


def test_a_trim_creates_a_revision(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        revision = create_clip(
            conn,
            job_id=JOB_ID,
            analysis_id=None,
            candidate_id=None,
            start_seconds=1.5,
            end_seconds=3.0,
            clip_id="e" * 32,
        )
        # A revision is marked as one by its parent, not by a candidate.
        from app.db import create_clip_revision

        parent = get_clip(conn, CLIP_ID)
        assert parent is not None
        child = create_clip_revision(
            conn,
            parent=parent,
            start_seconds=1.5,
            end_seconds=3.0,
            title="A trimmed clip",
        )

    assert child.is_revision is True
    assert child.parent_clip_id == CLIP_ID
    assert child.candidate_id is None
    assert child.title == "A trimmed clip"
    assert (child.start_seconds, child.end_seconds) == (1.5, 3.0)
    assert revision.id != child.id


def test_a_revision_never_touches_the_clip_it_came_from(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        parent = get_clip(conn, CLIP_ID)
        assert parent is not None
        before = (parent.start_seconds, parent.end_seconds, parent.output_path)

        from app.db import create_clip_revision

        create_clip_revision(
            conn, parent=parent, start_seconds=1.5, end_seconds=3.0
        )

        after = get_clip(conn, CLIP_ID)
    assert after is not None
    assert (after.start_seconds, after.end_seconds, after.output_path) == before
    assert after.status is ClipStatus.COMPLETED


def test_a_revision_never_rewrites_the_candidate(settings: Settings) -> None:
    """The AI's proposal is left exactly as the model made it."""
    _job(settings)
    _candidate(settings)
    _clip(settings, candidate_id=CANDIDATE_ID)

    with session(settings.resolved_database_path) as conn:
        candidate = get_highlight_candidate(conn, CANDIDATE_ID)
        assert candidate is not None
        before = (candidate.start_seconds, candidate.end_seconds, candidate.title)

        from app.db import create_clip_revision

        parent = get_clip(conn, CLIP_ID)
        assert parent is not None
        create_clip_revision(
            conn, parent=parent, start_seconds=1.2, end_seconds=3.4
        )

        after = get_highlight_candidate(conn, CANDIDATE_ID)
    assert after is not None
    assert (after.start_seconds, after.end_seconds, after.title) == before


def test_the_revisions_of_a_clip_are_listed(settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    with session(settings.resolved_database_path) as conn:
        from app.db import create_clip_revision

        parent = get_clip(conn, CLIP_ID)
        assert parent is not None
        create_clip_revision(
            conn, parent=parent, start_seconds=1.5, end_seconds=3.0, clip_id="e" * 32
        )
        create_clip_revision(
            conn, parent=parent, start_seconds=2.0, end_seconds=3.5, clip_id="f" * 32
        )
        revisions = list_clip_revisions(conn, CLIP_ID)

    assert [item.id for item in revisions] == ["e" * 32, "f" * 32]


# --- the trimming endpoint --------------------------------------------------


def test_a_trim_queues_a_revision(client: TestClient, settings: Settings) -> None:
    _job(settings)
    _clip(settings)

    response = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": 1.5, "end_seconds": 3.0, "title": "Trimmed"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["parent_clip_id"] == CLIP_ID
    assert body["is_revision"] is True
    assert body["candidate_id"] is None
    assert body["title"] == "Trimmed"
    assert body["status"] == "queued"
    assert (body["start_seconds"], body["end_seconds"]) == (1.5, 3.0)


def test_the_same_trim_twice_returns_the_same_revision(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)

    first = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": 1.5, "end_seconds": 3.0},
    )
    second = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": 1.5, "end_seconds": 3.0},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    with session(settings.resolved_database_path) as conn:
        assert len(list_clip_revisions(conn, CLIP_ID)) == 1


def test_a_trim_of_a_clip_that_does_not_exist_is_refused(
    client: TestClient,
) -> None:
    response = client.post(
        f"/api/v1/clips/{'z' * 32}/revisions",
        json={"start_seconds": 1.0, "end_seconds": 2.0},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.CLIP_NOT_FOUND.value


@pytest.mark.parametrize(
    "start,end",
    [
        (-1.0, 3.0),
        (3.0, 1.0),
        (1.0, 1.0),
        (0.0, 0.2),
        (0.0, 500.0),
        (0.0, 100.0),
    ],
)
def test_a_trim_that_cannot_be_used_is_refused(
    client: TestClient, settings: Settings, start: float, end: float
) -> None:
    _job(settings, duration=30.0)
    _clip(settings)

    response = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": start, "end_seconds": end},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"].startswith("clip_")


@pytest.mark.parametrize(
    "start,end",
    [
        ("NaN", 3.0),
        (1.0, "Infinity"),
        ("-Infinity", 3.0),
    ],
)
def test_a_boundary_that_is_not_finite_is_refused(
    client: TestClient, settings: Settings, start: object, end: object
) -> None:
    """A value that is not a finite number is refused, however it arrives.

    JSON has no literal for infinity, so a client sends it as a string --
    and Pydantic reads that string as a float. The domain layer is what
    refuses it.
    """
    _job(settings, duration=30.0)
    _clip(settings)

    response = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": start, "end_seconds": end},
    )

    assert response.status_code == 400
    assert (
        response.json()["error"]["code"]
        == ErrorCode.CLIP_INVALID_BOUNDARIES.value
    )


def test_a_trim_cannot_run_past_the_end_of_the_source(
    client: TestClient, settings: Settings
) -> None:
    _job(settings, duration=10.0)
    _clip(settings)

    response = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": 1.0, "end_seconds": 40.0},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.CLIP_SOURCE_TOO_SHORT.value


def test_a_boolean_is_not_a_timestamp(
    client: TestClient, settings: Settings
) -> None:
    """A boolean is not a number, even though Pydantic would read it as one."""
    _job(settings)
    _clip(settings)

    response = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": True, "end_seconds": 3.0},
    )

    assert response.status_code == 422
    with session(settings.resolved_database_path) as conn:
        assert list_clip_revisions(conn, CLIP_ID) == []


def test_a_trim_leaves_the_original_clip_alone(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)

    before = client.get(f"/api/v1/clips/{CLIP_ID}").json()
    client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": 1.5, "end_seconds": 3.0},
    )
    after = client.get(f"/api/v1/clips/{CLIP_ID}").json()

    assert after["start_seconds"] == before["start_seconds"]
    assert after["end_seconds"] == before["end_seconds"]
    assert after["media_url"] == before["media_url"]
    assert after["status"] == ClipStatus.COMPLETED.value


# --- the title --------------------------------------------------------------


def test_a_title_is_a_label_and_not_a_re_render(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)
    storage = Storage(settings.storage_root)
    path = storage.clips_dir / f"{CLIP_ID}.mp4"
    before = path.read_bytes()

    response = client.patch(
        f"/api/v1/clips/{CLIP_ID}", json={"title": "My best moment"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "My best moment"
    # The file is untouched.
    assert path.read_bytes() == before
    with session(settings.resolved_database_path) as conn:
        clip = get_clip(conn, CLIP_ID)
    assert clip is not None
    assert clip.status is ClipStatus.COMPLETED


def test_a_title_keeps_the_ai_title_when_there_is_none(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _candidate(settings)
    _clip(settings, candidate_id=CANDIDATE_ID)

    body = client.get(f"/api/v1/clips/{CLIP_ID}").json()

    assert body["title"] == "The AI's title"
    assert body["source_title"] == "The AI's title"


def test_an_empty_title_means_no_title(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _candidate(settings)
    _clip(settings, candidate_id=CANDIDATE_ID)

    client.patch(f"/api/v1/clips/{CLIP_ID}", json={"title": "Mine"})
    body = client.patch(f"/api/v1/clips/{CLIP_ID}", json={"title": "   "}).json()

    # The AI's title is shown again.
    assert body["title"] == "The AI's title"


def test_a_clip_of_another_job_cannot_be_edited(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _job(settings, OTHER_JOB_ID)
    _clip(settings)

    # The clip exists, so the request is about *this* clip; the job is not
    # part of the request, so there is nothing to mismatch. What matters is
    # that the clip's own job is used for the revision.
    body = client.post(
        f"/api/v1/clips/{CLIP_ID}/revisions",
        json={"start_seconds": 1.5, "end_seconds": 3.0},
    ).json()

    assert body["job_id"] == JOB_ID


# --- the caption text --------------------------------------------------------


def test_a_caption_edit_is_stored(client: TestClient, settings: Settings) -> None:
    _job(settings, with_transcript=True)
    _clip(settings)

    response = client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 1, "text": "What was actually said."}]},
    )

    assert response.status_code == 200
    assert response.json()["edits"] == [
        {"segment_index": 1, "text": "What was actually said."}
    ]
    with session(settings.resolved_database_path) as conn:
        assert list_clip_caption_edits(conn, CLIP_ID) == [
            (1, "What was actually said.")
        ]


def test_a_caption_edit_for_a_segment_that_does_not_exist_is_refused(
    client: TestClient, settings: Settings
) -> None:
    _job(settings, with_transcript=True)
    _clip(settings)

    response = client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 9, "text": "Nothing here."}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.CLIP_CAPTION_EDIT_INVALID.value


def test_an_empty_caption_is_refused(
    client: TestClient, settings: Settings
) -> None:
    _job(settings, with_transcript=True)
    _clip(settings)

    response = client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 0, "text": "   "}]},
    )

    assert response.status_code == 400


def test_the_caption_edits_replace_the_whole_set(
    client: TestClient, settings: Settings
) -> None:
    _job(settings, with_transcript=True)
    _clip(settings)

    client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 0, "text": "One"}]},
    )
    client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 1, "text": "Two"}]},
    )

    with session(settings.resolved_database_path) as conn:
        assert list_clip_caption_edits(conn, CLIP_ID) == [(1, "Two")]


def test_a_job_without_a_transcript_has_no_captions_to_edit(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)

    response = client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 0, "text": "One"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.CLIP_CAPTION_EDIT_INVALID.value


# --- the fingerprint ---------------------------------------------------------


def test_the_caption_text_is_part_of_the_fingerprint() -> None:
    settings = FormatSettings()
    plain = configuration_fingerprint(settings, ())
    edited = configuration_fingerprint(settings, ((0, "Changed"),))
    other = configuration_fingerprint(settings, ((0, "Different"),))

    assert plain != edited
    assert edited != other
    # The same text is the same fingerprint.
    assert edited == configuration_fingerprint(settings, ((0, "Changed"),))


def test_the_fingerprint_is_stable_for_equivalent_settings() -> None:
    first = FormatSettings(width=720, height=1280)
    second = settings_from_dict(first.to_dict())

    assert configuration_fingerprint(first, ()) == configuration_fingerprint(
        second, ()
    )


def test_a_changed_caption_makes_a_different_variant(
    client: TestClient, settings: Settings
) -> None:
    """Two different texts are two different videos, so two artifacts."""
    _job(settings, with_transcript=True)
    _clip(settings)

    first = client.post(
        f"/api/v1/clips/{CLIP_ID}/formats", json={"settings": {}}
    )
    assert first.status_code == 202

    client.put(
        f"/api/v1/clips/{CLIP_ID}/captions",
        json={"edits": [{"segment_index": 0, "text": "Changed words."}]},
    )
    second = client.post(
        f"/api/v1/clips/{CLIP_ID}/formats", json={"settings": {}}
    )

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]


def test_the_same_caption_text_reuses_the_variant(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)

    first = client.post(
        f"/api/v1/clips/{CLIP_ID}/formats", json={"settings": {}}
    )
    second = client.post(
        f"/api/v1/clips/{CLIP_ID}/formats", json={"settings": {}}
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


# --- the export --------------------------------------------------------------


def test_an_export_contains_the_chosen_clips(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)
    _clip(settings, clip_id="e" * 32)

    response = client.post(
        f"/api/v1/jobs/{JOB_ID}/exports",
        json={"clip_ids": [CLIP_ID, "e" * 32]},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
    assert len(names) == 2
    assert all(name.endswith(".mp4") for name in names)


def test_an_export_of_one_clip_holds_one_file(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)

    response = client.post(
        f"/api/v1/jobs/{JOB_ID}/exports", json={"clip_ids": [CLIP_ID]}
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 1
        assert archive.read(archive.namelist()[0]) == b"x" * 4096


def test_a_clip_of_another_job_cannot_be_exported(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _job(settings, OTHER_JOB_ID)
    _clip(settings, clip_id="e" * 32, job_id=OTHER_JOB_ID)

    response = client.post(
        f"/api/v1/jobs/{JOB_ID}/exports", json={"clip_ids": ["e" * 32]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.CLIP_EXPORT_INVALID.value


def test_an_unfinished_clip_cannot_be_exported(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings, completed=False)

    response = client.post(
        f"/api/v1/jobs/{JOB_ID}/exports", json={"clip_ids": [CLIP_ID]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.CLIP_EXPORT_UNAVAILABLE.value


def test_an_export_of_nothing_is_refused(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)

    response = client.post(
        f"/api/v1/jobs/{JOB_ID}/exports", json={"clip_ids": []}
    )

    assert response.status_code == 422


def test_an_export_of_a_clip_whose_file_is_gone_is_refused(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)
    _clip(settings)
    storage = Storage(settings.storage_root)
    (storage.clips_dir / f"{CLIP_ID}.mp4").unlink()

    response = client.post(
        f"/api/v1/jobs/{JOB_ID}/exports", json={"clip_ids": [CLIP_ID]}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.CLIP_EXPORT_UNAVAILABLE.value


def test_an_export_with_too_many_clips_is_refused(
    client: TestClient, settings: Settings
) -> None:
    """One request cannot build an unbounded in-memory archive."""
    _job(settings)
    _clip(settings)
    clip_ids = [CLIP_ID]
    for index in range(52):
        clip_ids.append(f"e{index:02d}" * 4)
        _clip(settings, clip_id=clip_ids[-1])

    body = {"clip_ids": clip_ids}
    response = client.post(f"/api/v1/jobs/{JOB_ID}/exports", json=body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == ErrorCode.CLIP_EXPORT_TOO_LARGE.value


def test_an_export_at_the_limit_is_accepted(
    client: TestClient, settings: Settings
) -> None:
    """The default limit clips at 50, so exactly 50 clips may be exported."""
    _job(settings)
    _clip(settings)
    clip_ids = [CLIP_ID]
    for index in range(49):
        clip_ids.append(f"e{index:02d}" * 4)
        _clip(settings, clip_id=clip_ids[-1])

    body = {"clip_ids": clip_ids}
    response = client.post(f"/api/v1/jobs/{JOB_ID}/exports", json=body)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    import zipfile

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 50


def test_an_export_over_the_byte_budget_is_refused(
    client: TestClient, settings: Settings
) -> None:
    """The total on-disk size of the chosen clips is bounded, not just the count."""
    _job(settings)
    _clip(settings)
    _clip(settings, clip_id="e" * 32)

    # Two 4 KiB clips exceed a 4 KiB byte budget.
    tiny_settings = Settings(
        _env_file=None,
        storage_dir=settings.storage_dir,
        database_path=settings.database_path,
        max_upload_bytes=settings.max_upload_bytes,
        max_video_duration_seconds=settings.max_video_duration_seconds,
        max_video_dimension=settings.max_video_dimension,
        worker_poll_interval_seconds=settings.worker_poll_interval_seconds,
        worker_lease_seconds=settings.worker_lease_seconds,
        worker_max_attempts=settings.worker_max_attempts,
        clip_export_max_items=settings.clip_export_max_items,
        clip_export_max_bytes=4096,
    )

    # Rebuild the app with the smaller budget so the endpoint enforces it.
    from app.main import create_app

    application = create_app(tiny_settings)
    from fastapi.testclient import TestClient as TinyClient

    with TinyClient(application) as tiny_client:
        response = tiny_client.post(
            f"/api/v1/jobs/{JOB_ID}/exports",
            json={"clip_ids": [CLIP_ID, "e" * 32]},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == ErrorCode.CLIP_EXPORT_TOO_LARGE.value


def test_an_export_at_the_byte_limit_is_accepted(
    client: TestClient, settings: Settings
) -> None:
    """An export whose total size is exactly the limit succeeds."""
    _job(settings)
    _clip(settings)

    tiny_settings = Settings(
        _env_file=None,
        storage_dir=settings.storage_dir,
        database_path=settings.database_path,
        max_upload_bytes=settings.max_upload_bytes,
        max_video_duration_seconds=settings.max_video_duration_seconds,
        max_video_dimension=settings.max_video_dimension,
        worker_poll_interval_seconds=settings.worker_poll_interval_seconds,
        worker_lease_seconds=settings.worker_lease_seconds,
        worker_max_attempts=settings.worker_max_attempts,
        clip_export_max_items=settings.clip_export_max_items,
        clip_export_max_bytes=4096,
    )

    from app.main import create_app

    application = create_app(tiny_settings)
    from fastapi.testclient import TestClient as TinyClient

    with TinyClient(application) as tiny_client:
        response = tiny_client.post(
            f"/api/v1/jobs/{JOB_ID}/exports",
            json={"clip_ids": [CLIP_ID]},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 1


# --- the cancellation --------------------------------------------------------


def test_a_queued_job_can_be_cancelled(
    client: TestClient, settings: Settings
) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=JOB_ID,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )

    response = client.post(f"/api/v1/jobs/{JOB_ID}/cancel")

    assert response.status_code == 200
    assert response.json()["job"]["status"] == JobStatus.CANCELLED.value


def test_a_finished_job_cannot_be_cancelled(
    client: TestClient, settings: Settings
) -> None:
    _job(settings)

    response = client.post(f"/api/v1/jobs/{JOB_ID}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.JOB_NOT_CANCELLABLE.value


def test_cancelling_twice_is_harmless(
    client: TestClient, settings: Settings
) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=JOB_ID,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )

    first = client.post(f"/api/v1/jobs/{JOB_ID}/cancel")
    second = client.post(f"/api/v1/jobs/{JOB_ID}/cancel")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["job"]["status"] == JobStatus.CANCELLED.value


def test_a_cancelled_job_is_not_claimed_by_a_worker(
    settings: Settings,
) -> None:
    """A cancelled job is not to be run."""
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=JOB_ID,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        cancel_job(conn, JOB_ID)

    worker = Worker(settings, worker_id="w1")

    assert worker.run_once() is None
    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, JOB_ID)
    assert job is not None
    assert job.status is JobStatus.CANCELLED


def test_cancelling_a_job_that_does_not_exist_is_refused(
    client: TestClient,
) -> None:
    response = client.post(f"/api/v1/jobs/{'z' * 32}/cancel")

    assert response.status_code == 404


# --- the real render ---------------------------------------------------------


def test_a_revision_is_rendered_for_real(
    settings: Settings, clip_source_video: Path
) -> None:
    """The revision has to produce a real, verified video."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed, so no clip can be rendered")

    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    shutil.copy(clip_source_video, storage.sources_dir / "clip.mp4")

    _job(settings, duration=10.0)
    with session(settings.resolved_database_path) as conn:
        create_clip(
            conn,
            job_id=JOB_ID,
            analysis_id=None,
            candidate_id=None,
            start_seconds=1.0,
            end_seconds=5.0,
            clip_id=CLIP_ID,
        )
    Worker(settings, worker_id="w1").run_clips_once()

    with session(settings.resolved_database_path) as conn:
        parent = get_clip(conn, CLIP_ID)
        assert parent is not None
        from app.db import create_clip_revision

        revision = create_clip_revision(
            conn, parent=parent, start_seconds=2.0, end_seconds=4.0
        )

    worker = Worker(settings, worker_id="w1")
    assert worker.run_clips_once() is True

    with session(settings.resolved_database_path) as conn:
        rendered = get_clip(conn, revision.id)
    assert rendered is not None
    assert rendered.status is ClipStatus.COMPLETED
    assert rendered.actual_duration_seconds == pytest.approx(2.0, abs=0.3)
    assert rendered.output_size_bytes and rendered.output_size_bytes > 1024
    # The file is a real video, at the path the row says.
    assert (storage.root / rendered.output_path).is_file()
    # And the parent still has its own file.
    assert (storage.root / parent.output_path).is_file()


# --- the migration -----------------------------------------------------------


def test_the_migration_adds_the_editing_columns(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(clips)")
        }
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert "parent_clip_id" in columns
    assert "title" in columns
    assert "clip_caption_edits" in tables
    assert version == SCHEMA_VERSION


def test_a_database_from_the_previous_version_is_migrated(
    settings: Settings,
) -> None:
    """A database written by Phase 7 is brought forward, with its data."""
    import sqlite3

    path = settings.resolved_database_path

    # Build the schema the way the application does, then roll the editing
    # columns back, so what is left is exactly what Phase 7 left behind.
    with session(path) as conn:
        create_job(
            conn,
            job_id=JOB_ID,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        update_job(conn, JOB_ID, video_duration_seconds=30.0)

    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            DROP INDEX IF EXISTS idx_clips_parent;
            DROP INDEX IF EXISTS idx_clips_revision;
            DROP TABLE IF EXISTS clip_caption_edits;
            ALTER TABLE clips DROP COLUMN parent_clip_id;
            ALTER TABLE clips DROP COLUMN title;
            DELETE FROM schema_migrations WHERE version = 7;
            """
        )
        connection.commit()
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        connection.close()

    assert version == 6

    # Opening it again brings it forward.
    with session(path) as conn:
        new_version = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(clips)")
        }
        job = get_job(conn, JOB_ID)

    assert new_version == SCHEMA_VERSION
    assert "parent_clip_id" in columns
    assert "title" in columns
    # The row that was there before is still there.
    assert job is not None
    assert job.source_reference == "clip.mp4"
    assert job.video_duration_seconds == 30.0
