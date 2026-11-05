"""Rendering a formatted clip with the real FFmpeg.

These tests use the real FFmpeg and the real FFprobe. A clip is formatted,
probed again, and compared frame by frame against a render of the same input
*without* captions -- which is how the caption burn-in is proved, rather
than taken on trust from the presence of a subtitle file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.captions import CaptionEvent, write_srt
from app.errors import ErrorCode
from app.format_render import (
    FormatRenderError,
    build_filter_chain,
    build_format_args,
    build_scale_filter,
    build_style,
    render_format,
    verify_format_output,
)
from app.formatting import CaptionSettings, FormatSettings, LayoutMode

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg and ffprobe are not installed, so nothing can be formatted",
)


def _frames(path: Path) -> list[str]:
    """The MD5 of every frame of a file."""
    completed = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "framehash", "-hash", "md5", "-"],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return [
        line.split(",")[-1].strip()
        for line in completed.stdout.decode("utf-8", "replace").splitlines()
        if line[:1].isdigit()
    ]


@pytest.fixture(scope="module")
def landscape_source(tmp_path_factory) -> Path:
    """A landscape video with sound, 640x360."""
    target = tmp_path_factory.mktemp("format") / "landscape.mp4"
    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if not target.exists():
        pytest.skip("ffmpeg could not build the landscape fixture")
    return target


@pytest.fixture(scope="module")
def silent_source(tmp_path_factory) -> Path:
    """A landscape video with no sound."""
    target = tmp_path_factory.mktemp("format") / "silent.mp4"
    subprocess.run(
        [
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(target),
        ],
        capture_output=True, timeout=300, check=False,
    )
    if not target.exists():
        pytest.skip("ffmpeg could not build the silent fixture")
    return target


# --- the filters ------------------------------------------------------------


def test_the_center_crop_filter_covers_and_crops() -> None:
    settings = FormatSettings(width=720, height=1280, layout=LayoutMode.CENTER_CROP)

    chain = build_scale_filter(settings)

    # "increase" scales to cover, and the crop takes the excess off.
    assert "force_original_aspect_ratio=increase" in chain
    assert "crop=720:1280" in chain
    # The picture is never stretched.
    assert "scale=720:1280" in chain


def test_the_fit_pad_filter_fits_and_pads() -> None:
    settings = FormatSettings(
        width=720, height=1280, layout=LayoutMode.FIT_PAD, padding_colour="#101010"
    )

    chain = build_scale_filter(settings)

    assert "force_original_aspect_ratio=decrease" in chain
    assert "pad=720:1280" in chain
    assert "0x101010" in chain


def test_the_style_is_built_from_the_settings() -> None:
    settings = FormatSettings(
        captions=CaptionSettings(
            font_family="Verdana",
            font_size=36,
            text_colour="#FFFFFF",
            outline_colour="#000000",
            outline_thickness=4,
            alignment=2,
            margin_vertical=120,
        )
    )

    style = build_style(settings)

    assert "FontName=Verdana" in style
    assert "FontSize=36" in style
    assert "PrimaryColour=&H00FFFFFF" in style
    assert "OutlineColour=&H00000000" in style
    assert "Outline=4" in style
    assert "Alignment=2" in style
    assert "MarginV=120" in style


def test_a_background_box_is_requested_when_one_is_set() -> None:
    settings = FormatSettings(
        captions=CaptionSettings(background_colour="#000000")
    )

    assert "BorderStyle=3" in build_style(settings)


def test_the_filter_chain_has_no_subtitles_when_there_are_none() -> None:
    chain = build_filter_chain(FormatSettings(), subtitle_filename=None)

    assert "subtitles" not in chain
    assert "scale=" in chain


def test_the_subtitle_file_is_referred_to_by_its_plain_name() -> None:
    """The path of the directory never reaches the filter."""
    chain = build_filter_chain(FormatSettings(), subtitle_filename="captions.srt")

    assert "subtitles=filename='captions.srt'" in chain
    assert "C:" not in chain
    assert "\\" not in chain.split("subtitles")[1].split(":force_style")[0]


def test_the_command_never_uses_a_shell() -> None:
    source = Path("/tmp/a clip; rm -rf /.mp4")
    args = build_format_args(
        ffmpeg_path="ffmpeg",
        source=source,
        output=Path("/tmp/out.mp4"),
        settings=FormatSettings(),
    )

    assert args[0] == "ffmpeg"
    # The path is one argument, whatever is in it.
    assert str(source) in args
    assert not any(
        ";" in argument for argument in args if argument != str(source)
    )


# --- rendering --------------------------------------------------------------


@requires_ffmpeg
def test_a_clip_is_formatted_into_a_portrait_video(
    landscape_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "formatted.mp4"
    settings = FormatSettings(width=720, height=1280)

    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=output,
            settings=settings,
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    assert output.exists()

    verification = verify_format_output(
        output,
        expected_width=720,
        expected_height=1280,
        expected_duration_seconds=4.0,
        expected_has_audio=True,
        tolerance_seconds=0.5,
        output_dir=tmp_path,
        ffprobe_path=FFPROBE,
    )

    # The output is exactly the configured portrait canvas.
    assert (verification.width, verification.height) == (720, 1280)
    assert verification.video_codec == "h264"
    assert verification.pixel_format == "yuv420p"
    assert verification.has_audio is True
    assert verification.duration_seconds == pytest.approx(4.0, abs=0.3)
    assert verification.size_bytes > 1024


@requires_ffmpeg
def test_a_clip_without_sound_is_still_formatted(
    silent_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "formatted.mp4"

    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=silent_source,
            output=output,
            settings=FormatSettings(),
            has_audio=False,
        ),
        timeout_seconds=180,
    )

    verification = verify_format_output(
        output,
        expected_width=720,
        expected_height=1280,
        expected_duration_seconds=3.0,
        expected_has_audio=False,
        tolerance_seconds=0.5,
        output_dir=tmp_path,
        ffprobe_path=FFPROBE,
    )

    assert verification.has_audio is False


@requires_ffmpeg
@pytest.mark.parametrize(
    "layout", [LayoutMode.CENTER_CROP, LayoutMode.FIT_PAD]
)
def test_both_layout_modes_produce_the_exact_dimensions(
    landscape_source: Path, tmp_path: Path, layout: LayoutMode
) -> None:
    output = tmp_path / f"{layout.value}.mp4"

    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=output,
            settings=FormatSettings(layout=layout),
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    verification = verify_format_output(
        output,
        expected_width=720,
        expected_height=1280,
        expected_duration_seconds=4.0,
        expected_has_audio=True,
        tolerance_seconds=0.5,
        output_dir=tmp_path,
        ffprobe_path=FFPROBE,
    )

    assert (verification.width, verification.height) == (720, 1280)


@requires_ffmpeg
def test_the_captions_are_actually_burned_in(
    landscape_source: Path, tmp_path: Path
) -> None:
    """The media-level proof: the captioned render differs from the plain one.

    A subtitle file existing on disk proves nothing, so the two renders are
    compared frame by frame. If the captions were not burned in, the frames
    would be identical.
    """
    work = tmp_path / "work"
    work.mkdir()

    plain = work / "plain.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=plain,
            settings=FormatSettings(),
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    write_srt(
        work / "captions.srt",
        [
            CaptionEvent(
                start_ms=500, end_ms=3500, text="CAPTIONS ARE BURNED IN"
            )
        ],
    )

    captioned = work / "captioned.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=captioned,
            settings=FormatSettings(),
            subtitle_filename="captions.srt",
            has_audio=True,
        ),
        timeout_seconds=180,
        cwd=work,
    )

    plain_frames = _frames(plain)
    captioned_frames = _frames(captioned)
    assert plain_frames and captioned_frames

    differing = sum(
        1 for a, b in zip(plain_frames, captioned_frames) if a != b
    )
    # The caption is on screen for three seconds of a four second clip.
    assert differing > 10, f"only {differing} frames differ"


@requires_ffmpeg
def test_a_caption_outside_the_clip_is_not_burned_in(
    landscape_source: Path, tmp_path: Path
) -> None:
    """A caption whose time is outside the video cannot appear in it."""
    work = tmp_path / "work"
    work.mkdir()

    plain = work / "plain.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=plain,
            settings=FormatSettings(),
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    write_srt(
        work / "captions.srt",
        [CaptionEvent(start_ms=9000, end_ms=9500, text="TOO LATE")],
    )
    captioned = work / "captioned.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=captioned,
            settings=FormatSettings(),
            subtitle_filename="captions.srt",
            has_audio=True,
        ),
        timeout_seconds=180,
        cwd=work,
    )

    # Nothing was on screen, so the two renders are the same.
    assert _frames(plain) == _frames(captioned)


@requires_ffmpeg
def test_unicode_captions_are_burned_in(
    landscape_source: Path, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()

    write_srt(
        work / "captions.srt",
        [CaptionEvent(start_ms=500, end_ms=3500, text="Ünïcodé — 你好")],
    )
    captioned = work / "captioned.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=captioned,
            settings=FormatSettings(),
            subtitle_filename="captions.srt",
            has_audio=True,
        ),
        timeout_seconds=180,
        cwd=work,
    )

    verification = verify_format_output(
        captioned,
        expected_width=720,
        expected_height=1280,
        expected_duration_seconds=4.0,
        expected_has_audio=True,
        tolerance_seconds=0.5,
        output_dir=work,
        ffprobe_path=FFPROBE,
    )
    assert verification.size_bytes > 1024


# --- failures ---------------------------------------------------------------


def test_a_missing_ffmpeg_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FormatRenderError) as failure:
        render_format(
            build_format_args(
                ffmpeg_path="definitely-not-ffmpeg",
                source=Path("/tmp/in.mp4"),
                output=tmp_path / "out.mp4",
                settings=FormatSettings(),
            ),
            timeout_seconds=10,
        )

    assert failure.value.code == ErrorCode.MEDIA_TOOL_UNAVAILABLE.value


@requires_ffmpeg
def test_a_render_that_times_out_is_reported(
    landscape_source: Path, tmp_path: Path
) -> None:
    with pytest.raises(FormatRenderError) as failure:
        render_format(
            build_format_args(
                ffmpeg_path=FFMPEG,
                source=landscape_source,
                output=tmp_path / "out.mp4",
                settings=FormatSettings(),
                has_audio=True,
            ),
            timeout_seconds=0,
        )

    assert failure.value.code == ErrorCode.FORMAT_RENDER_TIMEOUT.value


@requires_ffmpeg
def test_a_render_that_fails_is_reported(
    landscape_source: Path, tmp_path: Path
) -> None:
    """A source FFmpeg cannot read is a clear failure."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video" * 100)

    with pytest.raises(FormatRenderError) as failure:
        render_format(
            build_format_args(
                ffmpeg_path=FFMPEG,
                source=broken,
                output=tmp_path / "out.mp4",
                settings=FormatSettings(),
                has_audio=True,
            ),
            timeout_seconds=120,
        )

    assert failure.value.code == ErrorCode.FORMAT_RENDER_FAILED.value


# --- verification -----------------------------------------------------------


def test_a_missing_output_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FormatRenderError) as failure:
        verify_format_output(
            tmp_path / "gone.mp4",
            expected_width=720,
            expected_height=1280,
            expected_duration_seconds=4.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE or "ffprobe",
        )

    assert failure.value.code == ErrorCode.FORMAT_OUTPUT_INVALID.value


def test_an_output_outside_the_output_directory_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.mp4"
    outside.write_bytes(b"x" * 4096)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(FormatRenderError) as failure:
        verify_format_output(
            outside,
            expected_width=720,
            expected_height=1280,
            expected_duration_seconds=4.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=output_dir,
            ffprobe_path=FFPROBE or "ffprobe",
        )

    assert failure.value.code == ErrorCode.FORMAT_OUTPUT_INVALID.value
    outside.unlink(missing_ok=True)


@requires_ffmpeg
def test_a_duration_outside_the_tolerance_is_refused(
    landscape_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "formatted.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=output,
            settings=FormatSettings(),
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    with pytest.raises(FormatRenderError) as failure:
        verify_format_output(
            output,
            expected_width=720,
            expected_height=1280,
            expected_duration_seconds=30.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE,
        )

    assert failure.value.code == ErrorCode.FORMAT_DURATION_MISMATCH.value


@requires_ffmpeg
def test_the_wrong_dimensions_are_refused(
    landscape_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "formatted.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=output,
            settings=FormatSettings(),
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    with pytest.raises(FormatRenderError) as failure:
        verify_format_output(
            output,
            expected_width=1080,
            expected_height=1920,
            expected_duration_seconds=4.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE,
        )

    assert failure.value.code == ErrorCode.FORMAT_OUTPUT_INVALID.value


@requires_ffmpeg
def test_a_file_that_is_not_a_video_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "notavideo.mp4"
    target.write_bytes(b"x" * 4096)

    with pytest.raises(FormatRenderError) as failure:
        verify_format_output(
            target,
            expected_width=720,
            expected_height=1280,
            expected_duration_seconds=4.0,
            expected_has_audio=True,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE,
        )

    assert failure.value.code == ErrorCode.FORMAT_VERIFICATION_FAILED.value


@requires_ffmpeg
def test_a_lost_audio_track_is_refused(
    landscape_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "formatted.mp4"
    render_format(
        build_format_args(
            ffmpeg_path=FFMPEG,
            source=landscape_source,
            output=output,
            settings=FormatSettings(),
            has_audio=True,
        ),
        timeout_seconds=180,
    )

    with pytest.raises(FormatRenderError) as failure:
        verify_format_output(
            output,
            expected_width=720,
            expected_height=1280,
            expected_duration_seconds=4.0,
            expected_has_audio=False,
            tolerance_seconds=0.5,
            output_dir=tmp_path,
            ffprobe_path=FFPROBE,
        )

    assert failure.value.code == ErrorCode.FORMAT_OUTPUT_INVALID.value
