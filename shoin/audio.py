"""Turn a saved briefing into a local WAV with an installed Piper voice."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

_BRACKETS = re.compile(r"[\[［]([^\[［\]］\n]+)[\]］]")
_SOURCE_NUMBER = re.compile(r"[SsＳｓ]\s*[0-9０-９]+")
_CITATION_SEPARATOR = re.compile(r"[,，、;；]|and", re.IGNORECASE)
_PIPER_TIMEOUT_SEC = 180


class AudioError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _omit_citation(match: re.Match[str]) -> str:
    labels = _CITATION_SEPARATOR.split(match[1])
    return "" if all(_SOURCE_NUMBER.fullmatch(label.strip()) for label in labels) else match[0]


def write_briefing_audio(body: str, model: Path, output: Path) -> None:
    text = _BRACKETS.sub(_omit_citation, body).strip()
    if not text:
        raise AudioError("AUDIO_BRIEFING_EMPTY", "the saved briefing has no text to narrate")
    model = model.expanduser().absolute()
    config = model.with_name(model.name + ".json")
    if not model.is_file() or not config.is_file():
        raise AudioError(
            "AUDIO_VOICE_NOT_FOUND", "provide a local Piper .onnx voice and its .onnx.json"
        )
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise AudioError("AUDIO_OUTPUT_ALREADY_EXISTS", "choose a new output path")

    with tempfile.TemporaryDirectory(prefix=".shoin-audio-", dir=output.parent) as staging:
        wav_path = Path(staging) / "briefing.wav"
        try:
            subprocess.run(
                ["piper", "-m", str(model), "-f", str(wav_path)],
                input=text + "\n",
                encoding="utf-8",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=_PIPER_TIMEOUT_SEC,
            )
        except FileNotFoundError as exc:
            raise AudioError("AUDIO_PIPER_NOT_FOUND", "install Piper and add it to PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError(
                "AUDIO_PIPER_TIMEOUT", "Piper timed out while generating audio"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise AudioError(
                "AUDIO_PIPER_FAILED", f"Piper exited with status {exc.returncode}"
            ) from exc

        try:
            with wave.open(str(wav_path), "rb") as wav:
                if not wav.readframes(1):
                    raise AudioError("AUDIO_INVALID_WAV", "Piper produced an empty WAV")
        except (FileNotFoundError, EOFError, wave.Error) as exc:
            raise AudioError("AUDIO_INVALID_WAV", "Piper did not produce a valid WAV") from exc
        wav_path.chmod(0o600)
        try:
            os.link(wav_path, output)
        except FileExistsError as exc:
            raise AudioError("AUDIO_OUTPUT_ALREADY_EXISTS", "choose a new output path") from exc
