"""Rendering a clip with the real FFmpeg, and proving the result.

These tests use the real FFmpeg and the real FFprobe. Nothing is mocked:
a clip is rendered from a generated fixture, probed again, and measured
against what was asked for.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.clip_render import (
    ClipRenderError,
    build_render_args,
    probe_source,
    render_clip,
    verify_clip,
)
from app.errors import ErrorCode

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg and ffprobe are not installed, so no clip can be rendered",
)


# --- the command ------------------------------------------------------------


def test_the_command_never_uses_a_shell() -> None:
    """The arguments are a list, and the source is one argument."""
    source = Path("/tmp/a clip; rm -rf /.mp4")
    args = build_render_args(
        ffmpeg_path="ffmpeg",
        source=source,
        output=Path("/tmp/out.mp4"),
        start_seconds=1.5,
        duration_seconds=4.0,
    )

    assert args[0] == "ffmpeg"
    assert args.count("-i") == 1
    # The path is one argument, whatever is in it.
    assert str(source) in args
    # Nothing from the path reaches a filter expression.
    assert not any(
        ";" in argument for argument in args if argument != str(source)
    )


def test_the_command_maps_explicitly_and_re_encodes() -> None:
    args = build_render_args(
        ffmpeg_path="ffmpeg",
        source=Path("/tmp/in.mp4"),
        output=Path("/tmp/out.mp4"),
        start_seconds=1.0,
        duration_seconds=2.0,
    )

    joined = " ".join(args)
    assert "-map 0:v:0" in joined
    assert "-map 0:a:0?" in joined
    # A re-encode, not a stream copy: the boundary guarantee depends on it.
    assert "-c:v libx264" in joined
    assert "-c:a aac" in joined
    assert "-pix_fmt yuv420p" in joined
    assert "-movflags +faststart" in joined
    assert "copy" not in [argument for argument in args]


def test_a_source_without_audio_does_not_ask_for_audio() -> None:
    args = build_render_args(
        ffmpeg_path="ffmpeg",
        source=Path("/tmp/in.mp4"),
        output=Path("/tmp/out.mp4"),
        start_seconds=1.0,
        duration_seconds=2.0,
        has_audio=False,
    )

    joined = " ".join(args)
    assert "0:a:0?" not in joined
    assert "-c:a" not in joined
    assert "-map 0:v:0" in joined


# --- probing ----------------------------------------------------------------


@requires_ffmpeg
def test_the_source_is_probed(clip_source_video: Path) -> None:
    probe = probe_source(clip_source_video, ffprobe_path=FFPROBE)

    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.width == 320
    assert probe.height == 240
    assert probe.duration_seconds == pytest.approx(10.0, abs=0.5)
    assert probe.video_codec == "h264"


@requires_ffmpeg
def test_a_source_without_audio_is_probed(clip_source_without_audio: Path) -> None:
    probe = probe_source(clip_source_without_audio, ffprobe_path=FFPROBE)

    assert probe.has_video is True
    assert probe.has_audio is False
    assert probe.audio_codec is None


@requires_ffmpeg
def test_a_source_with_a_nonzero_start_time_is_probed(
    clip_source_with_offset: Path,
) -> None:
    probe = probe_source(clip_source_with_offset, ffprobe_path=FFPROBE)

    # The file's own start time is not zero.
    assert probe.start_time_seconds > 0.5
    # The content still runs for its own length.
    assert probe.duration_seconds == pytest.approx(8.0, abs=0.5)


def test_a_broken_source_is_refused(clip_source_broken: Path) -> None:
    with pytest.raises(ClipRenderError) as failure:
        probe_source(clip_source_broken, ffprobe_path=FFPROBE or "ffprobe")

    assert failure.value.code == ErrorCode.CLIP_SOURCE_INVALID.value


def test_a_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ClipRenderError) as failure:
        probe_source(tmp_path / "gone.mp4", ffprobe_path=FFPROBE or "ffprobe")

    assert failure.value.code == ErrorCode.CLIP_SOURCE_INVALID.value


# --- rendering --------------------------------------------------------------


@requires_ffmpeg
def test_a_clip_is_rendered_and_verified(
    clip_source_video: Path, tmp_path: Path
) -> None:
    """The whole thing: render a clip, then prove it is a real clip."""
    output = tmp_path / "clip.mp4"
    args = build_render_args(
        ffmpeg_path=FFMPEG,
        source=clip_source_video,
        output=output,
        start_seconds=2.0,
        duration_seconds=3.0,
        has_audio=True,
    )

    render_clip(args, timeout_seconds=120)

    assert output.exists()
    assert output.stat().st_size > 1024

    verification = verify_clip(
        output,
        requested_duration_seconds=3.0,
        expected_has_audio=True,
        tolerance_seconds=0.5,
        output_dir=tmp_path,
        ffprobe_path=FFPROBE,
    )

    assert verification.duration_seconds == pytest.approx(3.0, abs=0.25)
    assert verification.width == 320
    assert verification.height == 240
    assert verification.video_codec == "h264"
    assert verification.audio_codec == "aac"
    assert verification.has_audio is True


@requires_ffmpeg
def test_a_clip_of_a_silent_source_has_no_audio(
    clip_source_without_audio: Path, tmp_path: Path
) -> None:
    output = tmp_path / "clip.mp4"
    args = build_render_args(
        ffmpeg_path=FFMPEG,
        source=clip_source_without_audio,
        output=output,
        start_seconds=1.0,
        duration_seconds=2.0,
        has_audio=False,
    )

    render_clip(args, timeout_seconds=120)

    verification = verify_clip(
        output,
        requested_duration_seconds=2.0,
        expected_has_audio=False,
        tolerance_seconds=0.5,
        output_dir=tmp_path,
        ffprobe_path=FFPROBE,
    )

    assert verification.has_audio is False
    assert verification.video_codec == "h264"


@requires_ffmpeg
def test_a_clip_from_a_source_with_a_nonzero_start_time_lands_on_the_right_moment(
    clip_source_with_offset: Path, tmp_path: Path
) -> None:
    """The boundary is on the *media* timeline, not the file timeline.

    The offset source and the plain source it was made from hold the same
    content. A clip taken from the same media moment of each has to be
    identical, or the seek is landing in the wrong place.
    """
    output = tmp_path / "clip.mp4"
    args = build_render_args(
        ffmpeg_path=FFMPEG,
        source=clip_source_with_offset,
        output=output,
        start_seconds=3.0,
        duration_seconds=2.0,
        has_audio=False,
    )

    render_clip(args, timeout_seconds=120)

    verification = verify_clip(
        output,
        requested_duration_seconds=2.0,
        expected_has_audio=False,
        tolerance_seconds=0.5,
        output_dir=tmp_path,
        ffprobe_path=FFPROBE,
    )
    assert verification.duration_seconds == pytest.approx(2.0, abs=0.25)

    # The first frame has to match the first frame of the same moment in
    # the source the offset file was made from.
    plain = clip_source_with_offset.with_name("plain.mp4")
    if not plain.exists():
        pytest.skip("the plain source of the offset fixture is not available")
    reference = tmp_path / "reference.mp4"
    render_clip(
        build_render_args(
            ffmpeg_path=FFMPEG,
            source=plain,
            output=reference,
            start_seconds=3.0,
            duration_seconds=2.0,
            has_audio=False,
        ),
        timeout_seconds=120,
    )

    def _first_frame_hash(path: Path) -> str:
        completed = subprocess.run(
            [FFMPEG, "-v", "error", "-i", str(path), "-f", "framehash", "-hash", "md5", "-"],
            capture_output=True,
            timeout=120,
            check=False,
        )
        lines = [
            line
            for line in completed.stdout.decode("utf-8", "replace").splitlines()
            if line[:1].isdigit()
        ]
        return lines[0].split(",")[-1].strip() if lines else ""

    assert _first_frame_hash(output) == _first_frame_hash(reference)


@requires_ffmpeg
def test_a_source_with_an_odd_name_is_rendered(
    clip_source_odd_name: Path, tmp_path: Path
) -> None:
    output = tmp_path / "clip.mp4"
    args = build_render_args(
        ffmpeg_path=FFMPEG,
        source=clip_source_odd_name,
        output=output,
        start_seconds=1.0,
        duration_seconds=2.0,
        has_audio=True,
    )

    render_clip(args, timeout_seconds=120)

    assert output.exists()
    assert output.stat().st_size > 1024


@requires_ffmpeg
def test_a_render_that_times_out_is_reported(
    clip_source_video: Path, tmp_path: Path
) -> None:
    """A render that cannot finish in time is stopped and reported.

    The deadline is checked before the first wait, so the child is stopped
    while it is still starting: that is the same path a long render takes.
    """
    output = tmp_path / "clip.mp4"
    args = build_render_args(
        ffmpeg_path=FFMPEG,
        source=clip_source_video,
        output=output,
        start_seconds=0.0,
        duration_seconds=1.0,
        has_audio=True,
    )

    with pytest.raises(ClipRenderError) as failure:
        render_clip(args, timeout_seconds=0)

    assert failure.value.code == ErrorCode.CLIP_RENDER_TIMEOUT.value
    # The render was stopped, so it did not finish.
    assert not output.exists() or output.stat().st_size == 0


def test_a_missing_ffmpeg_is_reported(tmp_path: Path) -> None:
    args = build_render_args(
        ffmpeg_path="definitely-not-ffmpeg",
        source=Path("/tmp/in.mp4"),
        output=tmp_path / "out.mp4",
        start_seconds=1.0,
        duration_seconds=2.0,
    )

    with pytest.raises(ClipRenderError) as failure:
        render_clip(args, timeout_seconds=10)

    assert failure.value.code == ErrorCode.MEDIA_TOOL_UNAVAILABLE.value


# --- verification -----------------------------------------------------------


def test_a_missing_output_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ClipRenderError) as failure:
        verify_clip(
            tmp_path / "gone.mp4",
            requested_duration_seconds=2.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE or "ffprobe",
        )

    assert failure.value.code == ErrorCode.CLIP_OUTPUT_INVALID.value


def test_an_output_outside_the_output_directory_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.mp4"
    outside.write_bytes(b"x" * 4096)
    output_dir = tmp_path / "clips"
    output_dir.mkdir()

    with pytest.raises(ClipRenderError) as failure:
        verify_clip(
            outside,
            requested_duration_seconds=2.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=output_dir,
            ffprobe_path=FFPROBE or "ffprobe",
        )

    assert failure.value.code == ErrorCode.CLIP_OUTPUT_INVALID.value
    outside.unlink(missing_ok=True)


def test_an_output_that_is_too_small_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "tiny.mp4"
    target.write_bytes(b"x" * 10)

    with pytest.raises(ClipRenderError) as failure:
        verify_clip(
            target,
            requested_duration_seconds=2.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE or "ffprobe",
            min_output_bytes=1024,
        )

    assert failure.value.code == ErrorCode.CLIP_OUTPUT_INVALID.value


@requires_ffmpeg
def test_a_duration_outside_the_tolerance_is_refused(
    clip_source_video: Path, tmp_path: Path
) -> None:
    output = tmp_path / "clip.mp4"
    render_clip(
        build_render_args(
            ffmpeg_path=FFMPEG,
            source=clip_source_video,
            output=output,
            start_seconds=1.0,
            duration_seconds=2.0,
            has_audio=True,
        ),
        timeout_seconds=120,
    )

    with pytest.raises(ClipRenderError) as failure:
        verify_clip(
            output,
            requested_duration_seconds=9.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE,
        )

    assert failure.value.code == ErrorCode.CLIP_DURATION_MISMATCH.value


@requires_ffmpeg
def test_a_file_that_is_not_a_video_is_refused(
    clip_source_broken: Path, tmp_path: Path
) -> None:
    """A file FFprobe cannot read as media never becomes a clip."""
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"x" * 4096)

    with pytest.raises(ClipRenderError) as failure:
        verify_clip(
            output,
            requested_duration_seconds=2.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE,
        )

    assert failure.value.code == ErrorCode.CLIP_VERIFICATION_FAILED.value
