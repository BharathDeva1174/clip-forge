"""Tests for the FFmpeg audio extraction service.

The tests use real FFmpeg against a small, locally generated video, so they
prove the extraction really produces mono 16 kHz PCM audio. The failure modes
that cannot be produced with real media are driven with a stub executable, so
that they are deterministic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.audio import (
    AudioExtractionError,
    FfmpegAudioExtractor,
    extract_audio,
    ffmpeg_available,
    probe_source_audio,
)
from app.errors import ErrorCode

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not installed, so audio extraction cannot be tested",
)


def _extractor(**kwargs) -> FfmpegAudioExtractor:
    return FfmpegAudioExtractor(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", **kwargs)


# --- success ---------------------------------------------------------------


def test_audio_is_extracted_from_a_valid_video(sample_video: Path, tmp_path: Path) -> None:
    audio = _extractor().extract(sample_video, tmp_path / "out")

    assert audio.path.exists()
    assert audio.path.stat().st_size > 0
    assert audio.duration_seconds > 0


def test_the_extracted_audio_is_mono_16khz_pcm(sample_video: Path, tmp_path: Path) -> None:
    audio = _extractor().extract(sample_video, tmp_path / "out")

    with wave.open(str(audio.path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert handle.getsampwidth() == 2
        frames = handle.getnframes()
        rate = handle.getframerate()

    assert frames > 0
    # The duration has to match the number of frames, within a frame.
    assert abs(frames / rate - audio.duration_seconds) < 0.05


def test_the_extracted_audio_is_a_wav_file(sample_video: Path, tmp_path: Path) -> None:
    audio = _extractor().extract(sample_video, tmp_path / "out")
    assert audio.path.suffix == ".wav"
    assert audio.path.read_bytes()[:4] == b"RIFF"


# --- failures --------------------------------------------------------------


def test_a_missing_ffmpeg_produces_a_stable_error(sample_video: Path, tmp_path: Path) -> None:
    extractor = FfmpegAudioExtractor(
        ffmpeg_path="ffmpeg-that-does-not-exist", ffprobe_path="ffprobe"
    )

    with pytest.raises(AudioExtractionError) as failure:
        extractor.extract(sample_video, tmp_path / "out")

    assert failure.value.code == ErrorCode.FFMPEG_NOT_FOUND.value
    assert "FFmpeg" in failure.value.message


def test_a_missing_source_produces_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(tmp_path / "not-here.mp4", tmp_path / "out")

    assert failure.value.code == ErrorCode.SOURCE_NOT_FOUND.value


def test_invalid_media_fails_safely(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a media file at all")

    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(broken, tmp_path / "out")

    assert failure.value.code == ErrorCode.INVALID_MEDIA.value


def test_a_truncated_source_fails_safely(sample_video: Path, tmp_path: Path) -> None:
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(sample_video.read_bytes()[:64])

    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(truncated, tmp_path / "out")

    assert failure.value.code in (
        ErrorCode.INVALID_MEDIA.value,
        ErrorCode.AUDIO_EXTRACTION_FAILED.value,
    )


def test_a_source_without_an_audio_stream_is_reported_explicitly(
    sample_video_without_audio: Path, tmp_path: Path
) -> None:
    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(sample_video_without_audio, tmp_path / "out")

    assert failure.value.code == ErrorCode.NO_AUDIO_STREAM.value
    assert "audio" in failure.value.message.lower()


def test_the_extraction_timeout_stops_the_process_and_cleans_up(
    sample_video: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"

    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(sample_video, out, timeout_seconds=0)

    assert failure.value.code == ErrorCode.AUDIO_EXTRACTION_TIMEOUT.value
    # No half-written audio is left behind.
    assert not out.exists() or list(out.iterdir()) == []


def test_a_source_that_is_too_long_is_refused(
    sample_video: Path, tmp_path: Path
) -> None:
    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(sample_video, tmp_path / "out", max_duration_seconds=0.1)

    assert failure.value.code == ErrorCode.MEDIA_TOO_LONG.value


def test_not_enough_disk_space_is_reported(
    sample_video: Path, tmp_path: Path
) -> None:
    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(
            sample_video,
            tmp_path / "out",
            min_free_disk_bytes=10**15,
        )

    assert failure.value.code == ErrorCode.INSUFFICIENT_DISK_SPACE.value


# --- stub-driven failures --------------------------------------------------


def _stub_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an executable that behaves the way the test asks it to.

    The stub reads its behaviour from an environment variable, which the
    extractor passes on to the child process, so no argument has to be
    threaded through the production code.
    """
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "import os\n"
        "import sys\n"
        "mode = os.environ.get('CLIPFORGE_TEST_STUB_MODE', 'fail')\n"
        "output = sys.argv[-1]\n"
        "if mode == 'empty':\n"
        "    open(output, 'wb').close()\n"
        "elif mode == 'garbage':\n"
        "    open(output, 'wb').write(b'not a wav file at all')\n"
        "else:\n"
        "    sys.stderr.write('stub failure: ' + 'x' * 5000)\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )

    if os.name == "nt":
        stub = tmp_path / "fake-ffmpeg.cmd"
        stub.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        stub = tmp_path / "fake-ffmpeg.sh"
        stub.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        stub.chmod(0o755)

    monkeypatch.setenv("CLIPFORGE_TEST_STUB_MODE", "fail")
    return stub


def test_a_nonzero_exit_fails_safely(
    sample_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub_ffmpeg(tmp_path, monkeypatch)
    extractor = FfmpegAudioExtractor(ffmpeg_path=str(stub), ffprobe_path="ffprobe")

    with pytest.raises(AudioExtractionError) as failure:
        extractor.extract(sample_video, tmp_path / "out")

    assert failure.value.code == ErrorCode.AUDIO_EXTRACTION_FAILED.value


def test_an_empty_output_fails_safely(
    sample_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub_ffmpeg(tmp_path, monkeypatch)
    monkeypatch.setenv("CLIPFORGE_TEST_STUB_MODE", "empty")
    extractor = FfmpegAudioExtractor(ffmpeg_path=str(stub), ffprobe_path="ffprobe")

    with pytest.raises(AudioExtractionError) as failure:
        extractor.extract(sample_video, tmp_path / "out")

    assert failure.value.code == ErrorCode.AUDIO_OUTPUT_INVALID.value


def test_output_that_is_not_audio_fails_safely(
    sample_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub_ffmpeg(tmp_path, monkeypatch)
    monkeypatch.setenv("CLIPFORGE_TEST_STUB_MODE", "garbage")
    extractor = FfmpegAudioExtractor(ffmpeg_path=str(stub), ffprobe_path="ffprobe")

    with pytest.raises(AudioExtractionError) as failure:
        extractor.extract(sample_video, tmp_path / "out")

    assert failure.value.code == ErrorCode.AUDIO_OUTPUT_INVALID.value


def test_the_diagnostic_output_is_bounded_and_never_leaks(
    sample_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub_ffmpeg(tmp_path, monkeypatch)
    extractor = FfmpegAudioExtractor(ffmpeg_path=str(stub), ffprobe_path="ffprobe")

    with pytest.raises(AudioExtractionError) as failure:
        extractor.extract(sample_video, tmp_path / "out")

    message = failure.value.message
    # The user-facing message is generic and contains no subprocess output.
    assert "stub failure" not in message
    assert "xxxx" not in message
    assert str(sample_video) not in message
    assert len(message) < 200


# --- paths -----------------------------------------------------------------


def test_paths_with_spaces_and_unicode_work(
    sample_video: Path, tmp_path: Path
) -> None:
    source_dir = tmp_path / "videos mit leerzeichen"
    source_dir.mkdir()
    source = source_dir / "clip ünïcodé 2026.mp4"
    shutil.copy(sample_video, source)

    out = tmp_path / "audio ordner mit leerzeichen"
    audio = _extractor().extract(source, out)

    assert audio.path.exists()
    assert audio.path.stat().st_size > 0


def test_a_source_outside_the_storage_root_is_refused(
    sample_video: Path, tmp_path: Path
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    source = outside / "clip.mp4"
    shutil.copy(sample_video, source)

    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(source, tmp_path / "out", source_root=root)

    assert failure.value.code == ErrorCode.SOURCE_PATH_INVALID.value


def test_a_path_that_traverses_out_of_the_root_is_refused(
    sample_video: Path, tmp_path: Path
) -> None:
    root = tmp_path / "storage"
    (root / "sources").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    source = outside / "clip.mp4"
    shutil.copy(sample_video, source)

    # A path that climbs out of the root must not be accepted.
    escaping = root / "sources" / ".." / ".." / "elsewhere" / "clip.mp4"

    with pytest.raises(AudioExtractionError) as failure:
        _extractor().extract(escaping, tmp_path / "out", source_root=root)

    assert failure.value.code == ErrorCode.SOURCE_PATH_INVALID.value


def test_the_output_never_overwrites_a_source_or_another_result(
    sample_video: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    before = sample_video.read_bytes()

    first = _extractor().extract(sample_video, out)
    second = _extractor().extract(sample_video, out)

    assert first.path != second.path
    assert first.path.exists() and second.path.exists()
    # The source is untouched and both results are still there.
    assert sample_video.read_bytes() == before


def test_the_output_is_not_written_into_the_sources_directory(
    sample_video: Path, tmp_path: Path
) -> None:
    out = tmp_path / "sources"
    audio = _extractor().extract(sample_video, out)
    assert audio.path.parent == out
    assert audio.path.suffix == ".wav"


# --- helpers ---------------------------------------------------------------


def test_probe_source_audio_reports_the_audio_track(sample_video: Path) -> None:
    info = probe_source_audio(sample_video)
    assert info.has_audio is True
    assert info.duration_seconds and info.duration_seconds > 0


def test_probe_source_audio_reports_a_missing_track(
    sample_video_without_audio: Path,
) -> None:
    info = probe_source_audio(sample_video_without_audio)
    assert info.has_audio is False


def test_ffmpeg_available_reports_a_missing_binary() -> None:
    assert ffmpeg_available("ffmpeg") is True
    assert ffmpeg_available("ffmpeg-that-does-not-exist") is False


def test_extract_audio_is_usable_as_a_function(sample_video: Path, tmp_path: Path) -> None:
    audio = extract_audio(sample_video, tmp_path / "out")
    assert audio.path.exists()


def test_the_convenience_function_still_honours_the_timeout(
    sample_video: Path, tmp_path: Path
) -> None:
    with pytest.raises(AudioExtractionError) as failure:
        extract_audio(sample_video, tmp_path / "out", timeout_seconds=0)

    assert failure.value.code == ErrorCode.AUDIO_EXTRACTION_TIMEOUT.value


def test_ffmpeg_is_never_run_through_a_shell(sample_video: Path, tmp_path: Path) -> None:
    """A filename with shell metacharacters must not become a command."""
    # `>` is not a legal character in a Windows file name, so the metacharacters
    # that are used here are the ones an attacker could actually get into a
    # stored path.
    tricky = tmp_path / "a & b; echo hacked.mp4"
    shutil.copy(sample_video, tricky)

    audio = _extractor().extract(tricky, tmp_path / "out")

    assert audio.path.exists()
    assert not (tmp_path / "hacked").exists()
    assert not (tmp_path / "hacked.mp4").exists()


def test_a_unicode_source_does_not_break_the_probe(sample_video: Path, tmp_path: Path) -> None:
    source = tmp_path / "日本語 の 動画.mp4"
    shutil.copy(sample_video, source)

    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", str(source)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0
    assert b"audio" in completed.stdout
