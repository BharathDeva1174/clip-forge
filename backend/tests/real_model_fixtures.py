"""Fixtures for the real-model tests.

Every fixture is generated on this machine: the speech comes from the
Windows speech synthesiser and the audio is assembled with FFmpeg. No
third-party media is used, nothing is downloaded, and the phrases are
chosen so that the assertions can be about structure rather than about
getting every word exactly right.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

#: The phrase the clear-speech fixture says.
CLEAR_PHRASE = "The quick brown fox jumps over the lazy dog."

#: The phrase the second voice says.
ALTERNATE_PHRASE = "Every good job starts with a clear plan."

#: The phrase that is spoken with pauses between the clauses.
PAUSE_PHRASE = "Wait for it, and then, one moment later, it arrives."

#: The short phrase that is followed by silence.
SHORT_PHRASE = "That is all."

#: The sample rate the speech engine expects.
SAMPLE_RATE = 16000


def ffmpeg() -> str:
    """The FFmpeg binary, or an empty string when it is not installed."""
    return shutil.which("ffmpeg") or ""


def speak(
    target: Path,
    phrase: str,
    *,
    voice: str | None = None,
    rate: int = 0,
) -> bool:
    """Speak ``phrase`` into a WAV file with the Windows speech synthesiser.

    Returns whether the fixture could be produced.
    """
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    )
    if voice:
        script += f"$s.SelectVoice('{voice}'); "
    script += f"$s.Rate = {int(rate)}; "
    script += (
        f"$s.SetOutputToWaveFile('{target}'); "
        f"$s.Speak('{phrase}'); "
        "$s.Dispose();"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


def to_pcm16k(source: Path, target: Path) -> bool:
    """Convert a WAV file to 16 kHz mono PCM, the format the engine reads."""
    binary = ffmpeg()
    if not binary:
        return False
    completed = subprocess.run(
        [
            binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


def duration_of(path: Path) -> float:
    """The duration of a WAV file, in seconds."""
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def pad_with_silence(source: Path, target: Path, seconds: float) -> bool:
    """Add silence after the audio."""
    binary = ffmpeg()
    if not binary:
        return False
    completed = subprocess.run(
        [
            binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-af",
            f"apad=pad_dur={seconds}",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


def pad_before(source: Path, target: Path, seconds: float) -> bool:
    """Add silence before the audio."""
    binary = ffmpeg()
    if not binary:
        return False
    completed = subprocess.run(
        [
            binary,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            # The silence has a length: an endless source would make the
            # concat filter run for ever.
            f"anullsrc=r={SAMPLE_RATE}:cl=mono:d={seconds:.3f}",
            "-i",
            str(source),
            "-filter_complex",
            "[0][1]concat=n=2:v=0:a=1",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


def mix_with_tone(source: Path, target: Path, frequency: int = 220) -> bool:
    """Mix the speech with a steady tone, so the speech is not clean."""
    binary = ffmpeg()
    if not binary:
        return False
    length = duration_of(source)
    completed = subprocess.run(
        [
            binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration={length:.3f}",
            "-filter_complex",
            "amix=inputs=2:duration=first:weights=1 0.6",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0


def write_silence(target: Path, seconds: float) -> bool:
    """Write a silent WAV file."""
    binary = ffmpeg()
    if not binary:
        return False
    completed = subprocess.run(
        [
            binary,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono:d={seconds:.3f}",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return completed.returncode == 0 and target.exists() and target.stat().st_size > 0
