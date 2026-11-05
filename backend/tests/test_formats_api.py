"""The formatted-clip endpoints."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import (
    create_clip,
    create_job,
    create_format,
    get_format,
    session,
    update_clip,
    update_job,
)
from app.errors import ErrorCode
from app.jobs import JobStage, JobStatus, SourceType
from app.storage import Storage

FFMPEG = shutil.which("ffmpeg")

SETTINGS = {
    "width": 720,
    "height": 1280,
    "layout": "center_crop",
    "padding_colour": "#000000",
    "captions": {
        "enabled": True,
        "font_family": "Arial",
        "font_size": 42,
        "text_colour": "#FFFFFF",
        "outline_colour": "#000000",
        "outline_thickness": 3,
        "shadow": 0,
        "alignment": 2,
        "margin_vertical": 90,
        "max_characters_per_line": 28,
        "max_lines": 3,
    },
}


def _job_and_clip(settings: Settings) -> str:
    """A completed job with a clip that has a file."""
    storage = Storage(settings.storage_root)
    storage.ensure_layout()

    # A file for the clip to point at. It is not a real video, but the
    # endpoint only checks that the clip has been rendered.
    (storage.clips_dir / f"{'c' * 32}.mp4").write_bytes(b"x" * 4096)

    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="a" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        for status, stage in (
            (JobStatus.VALIDATING, JobStage.VALIDATING),
            (JobStatus.INSPECTING, JobStage.INSPECTING),
            (JobStatus.EXTRACTING_AUDIO, JobStage.EXTRACTING_AUDIO),
            (JobStatus.TRANSCRIBING, JobStage.TRANSCRIBING),
            (JobStatus.COMPLETED, JobStage.READY),
        ):
            update_job(conn, "a" * 32, status=status, stage=stage)
        create_clip(
            conn,
            job_id="a" * 32,
            analysis_id=None,
            candidate_id=None,
            start_seconds=0.0,
            end_seconds=4.0,
            clip_id="c" * 32,
        )
        # The clip walks the whole lifecycle: a clip is not completed
        # without passing through the rendering and the verification.
        for status, stage in (
            ("preparing", "preparing"),
            ("rendering", "rendering"),
            ("verifying", "verifying"),
        ):
            update_clip(conn, "c" * 32, status=status, stage=stage)
        update_clip(
            conn,
            "c" * 32,
            status="completed",
            stage="completed",
            output_path=f"clips/{'c' * 32}.mp4",
            output_size_bytes=4096,
        )
    return "c" * 32


# --- requesting -------------------------------------------------------------


def test_a_format_is_queued(client: TestClient, settings: Settings) -> None:
    clip_id = _job_and_clip(settings)

    response = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["clip_id"] == clip_id
    assert body["media_url"] is None
    assert body["captions_enabled"] is True


def test_the_boundaries_cannot_be_chosen_by_the_client(
    client: TestClient, settings: Settings
) -> None:
    """The request carries the settings and nothing else."""
    clip_id = _job_and_clip(settings)

    response = client.post(
        f"/api/v1/clips/{clip_id}/formats",
        json={"settings": SETTINGS, "start_seconds": 100.0, "end_seconds": 200.0},
    )

    assert response.status_code == 202
    assert "start_seconds" not in response.json()


def test_a_repeated_request_returns_the_same_artifact(
    client: TestClient, settings: Settings
) -> None:
    """One clip with one configuration is one artifact."""
    clip_id = _job_and_clip(settings)

    first = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    )
    second = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    with session(settings.resolved_database_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM clip_formats").fetchone()[0]
    assert rows == 1


def test_a_different_configuration_creates_a_different_artifact(
    client: TestClient, settings: Settings
) -> None:
    """Changing a setting does not replace the finished artifact."""
    clip_id = _job_and_clip(settings)

    first = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    )
    other = dict(SETTINGS)
    other["height"] = 1920
    other["width"] = 1080
    second = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": other}
    )

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]

    listed = client.get(f"/api/v1/clips/{clip_id}/formats").json()
    assert listed["total"] == 2


def test_an_unknown_clip_is_refused(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/clips/{'f' * 32}/formats", json={"settings": SETTINGS}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.CLIP_NOT_FOUND.value


def test_a_clip_that_has_not_been_rendered_cannot_be_formatted(
    client: TestClient, settings: Settings
) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="b" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        create_clip(
            conn,
            job_id="b" * 32,
            analysis_id=None,
            candidate_id=None,
            start_seconds=0.0,
            end_seconds=4.0,
            clip_id="d" * 32,
        )

    response = client.post(
        f"/api/v1/clips/{'d' * 32}/formats", json={"settings": SETTINGS}
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "settings_payload,expected",
    [
        ({"width": 0, "height": 1280}, 400),
        ({"width": -720, "height": 1280}, 400),
        ({"width": 721, "height": 1280}, 400),
        ({"width": 1920, "height": 1080}, 400),
        ({"width": 4000, "height": 8000}, 400),
        # An unknown enum value is a request validation error.
        ({"layout": "sideways"}, 422),
        ({"padding_colour": "not-a-colour"}, 400),
        ({"captions": {"font_size": -5}}, 400),
        ({"captions": {"font_family": "Arial; rm -rf /"}}, 400),
        ({"captions": {"text_colour": "red"}}, 400),
    ],
)
def test_an_unusable_setting_is_refused(
    client: TestClient, settings: Settings, settings_payload: dict, expected: int
) -> None:
    clip_id = _job_and_clip(settings)
    payload = dict(SETTINGS)
    payload.update(settings_payload)

    response = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": payload}
    )

    assert response.status_code == expected
    if expected == 400:
        assert (
            response.json()["error"]["code"]
            == ErrorCode.FORMAT_INVALID_SETTINGS.value
        )


def test_a_request_without_settings_uses_the_defaults(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)

    response = client.post(f"/api/v1/clips/{clip_id}/formats", json={})

    assert response.status_code == 202
    assert response.json()["settings"]["width"] == 720


# --- reading ----------------------------------------------------------------


def test_a_format_is_read_back(client: TestClient, settings: Settings) -> None:
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    response = client.get(f"/api/v1/formats/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_an_unknown_format_is_refused(client: TestClient) -> None:
    response = client.get(f"/api/v1/formats/{'f' * 32}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.FORMAT_NOT_FOUND.value


def test_the_formats_of_a_clip_are_listed(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)
    client.post(f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS})

    response = client.get(f"/api/v1/clips/{clip_id}/formats")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_the_pagination_is_bounded(client: TestClient, settings: Settings) -> None:
    clip_id = _job_and_clip(settings)

    assert client.get(f"/api/v1/clips/{clip_id}/formats?limit=0").status_code == 422
    assert client.get(f"/api/v1/clips/{clip_id}/formats?limit=1000").status_code == 422
    assert client.get(f"/api/v1/clips/{clip_id}/formats?offset=-1").status_code == 422


def test_the_response_never_exposes_a_path(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    response = client.get(f"/api/v1/formats/{created['id']}")

    assert "output_path" not in response.json()
    assert "C:\\" not in response.text
    assert "/home/" not in response.text


# --- retrying ---------------------------------------------------------------


def test_a_failed_format_can_be_rendered_again(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    with session(settings.resolved_database_path) as conn:
        from app.db import update_format

        update_format(
            conn, created["id"], status="failed", error_code="format_render_failed"
        )

    response = client.post(f"/api/v1/formats/{created['id']}/retry")

    assert response.status_code == 200
    assert response.json()["format"]["status"] == "queued"
    assert response.json()["format"]["error"] is None


def test_a_format_that_has_not_failed_is_not_retried(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    response = client.post(f"/api/v1/formats/{created['id']}/retry")

    assert response.status_code == 409


# --- the media route --------------------------------------------------------


def test_the_media_route_refuses_a_format_without_a_file(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    response = client.get(f"/api/v1/formats/{created['id']}/media")

    assert response.status_code == 404


def test_the_media_route_serves_a_finished_format(
    client: TestClient, settings: Settings
) -> None:
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    with session(settings.resolved_database_path) as conn:
        from app.db import update_format

        # The artifact walks the whole lifecycle before it is completed.
        for status, stage in (
            ("preparing", "preparing"),
            ("rendering", "rendering"),
            ("verifying", "verifying"),
        ):
            update_format(conn, created["id"], status=status, stage=stage)
        update_format(
            conn,
            created["id"],
            status="completed",
            stage="completed",
            output_path=f"formats/{created['id']}.mp4",
            output_size_bytes=4096,
        )
    storage = Storage(settings.storage_root)
    (storage.formats_dir / f"{created['id']}.mp4").write_bytes(b"x" * 4096)

    response = client.get(f"/api/v1/formats/{created['id']}/media")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/mp4")
    assert len(response.content) == 4096


def test_the_media_route_refuses_a_path_outside_the_root(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """A stored path that escapes the root serves nothing."""
    clip_id = _job_and_clip(settings)
    created = client.post(
        f"/api/v1/clips/{clip_id}/formats", json={"settings": SETTINGS}
    ).json()

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x" * 4096)
    with session(settings.resolved_database_path) as conn:
        from app.db import update_format

        for status, stage in (
            ("preparing", "preparing"),
            ("rendering", "rendering"),
            ("verifying", "verifying"),
        ):
            update_format(conn, created["id"], status=status, stage=stage)
        update_format(
            conn,
            created["id"],
            status="completed",
            stage="completed",
            output_path="../outside.mp4",
        )

    response = client.get(f"/api/v1/formats/{created['id']}/media")

    assert response.status_code == 404


# --- the fonts --------------------------------------------------------------


def test_the_fonts_of_this_machine_are_listed(client: TestClient) -> None:
    response = client.get("/api/v1/fonts")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)
    assert body["default"]


def test_the_font_list_never_breaks_the_filter(client: TestClient) -> None:
    """A font name in the list cannot break the style string."""
    from app.formatting import CaptionSettings

    for name in client.get("/api/v1/fonts").json()["items"]:
        # The validation that protects the filter accepts every listed font.
        CaptionSettings(font_family=name).validate()
