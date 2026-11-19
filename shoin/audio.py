"""Turn a saved briefing into a local WAV with Piper or VOICEVOX."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_BRACKETS = re.compile(r"[\[［]([^\[［\]］\n]+)[\]］]")
_SOURCE_NUMBER = re.compile(r"[SsＳｓ]\s*[0-9０-９]+")
_CITATION_SEPARATOR = re.compile(r"[,，、;；]|and", re.IGNORECASE)
_PIPER_TIMEOUT_SEC = 180
_VOICEVOX_TIMEOUT_SEC = 180
_VOICEVOX_QUERY_LIMIT = 1024 * 1024
_VOICEVOX_WAV_LIMIT = 64 * 1024 * 1024
_VOICEVOX_SEGMENT_CHARS = 1024
_VOICEVOX_MAX_CHARS = 16 * _VOICEVOX_SEGMENT_CHARS


class AudioError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _omit_citation(match: re.Match[str]) -> str:
    labels = _CITATION_SEPARATOR.split(match[1])
    return "" if all(_SOURCE_NUMBER.fullmatch(label.strip()) for label in labels) else match[0]


def _run_piper(text: str, model: Path, wav_path: Path) -> None:
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
        raise AudioError("AUDIO_PIPER_TIMEOUT", "Piper timed out while generating audio") from exc
    except subprocess.CalledProcessError as exc:
        raise AudioError(
            "AUDIO_PIPER_FAILED", f"Piper exited with status {exc.returncode}"
        ) from exc


def _voicevox_post(path: str, limit: int, body: bytes | None = None) -> bytes:
    # The fixed loopback address and direct HTTP connection bypass proxies and redirects.
    conn = http.client.HTTPConnection("127.0.0.1", 50021, timeout=_VOICEVOX_TIMEOUT_SEC)
    try:
        conn.request("POST", path, body, {"Content-Type": "application/json"} if body else {})
        with conn.getresponse() as resp:
            if resp.status != 200:
                raise AudioError("AUDIO_VOICEVOX_FAILED", f"VOICEVOX returned HTTP {resp.status}")
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise AudioError(
                    "AUDIO_VOICEVOX_FAILED", "VOICEVOX response exceeds the size limit"
                )
            return data
    except (OSError, http.client.HTTPException) as exc:
        raise AudioError(
            "AUDIO_VOICEVOX_UNAVAILABLE", "check the local VOICEVOX engine at 127.0.0.1:50021"
        ) from exc
    finally:
        conn.close()


def _voicevox_segments(text: str) -> Iterator[str]:
    while len(text) > _VOICEVOX_SEGMENT_CHARS:
        end = _VOICEVOX_SEGMENT_CHARS
        for index in range(end - 1, end // 2 - 1, -1):
            if text[index] in "\n。！？!? ":
                end = index + 1
                break
        yield text[:end]
        text = text[end:]
    if text.strip():
        yield text


def _run_voicevox(text: str, speaker: int, wav_path: Path) -> None:
    if len(text) > _VOICEVOX_MAX_CHARS:
        raise AudioError("AUDIO_VOICEVOX_FAILED", "briefing exceeds the VOICEVOX narration limit")
    query: dict[str, Any] = {}
    for segment in _voicevox_segments(text):
        params = urllib.parse.urlencode({"text": segment, "speaker": speaker})
        data = _voicevox_post(f"/audio_query?{params}", _VOICEVOX_QUERY_LIMIT)
        try:
            part = json.loads(data)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise AudioError("AUDIO_VOICEVOX_FAILED", "VOICEVOX returned an invalid query") from exc
        if not isinstance(part, dict) or not isinstance(part.get("accent_phrases"), list):
            raise AudioError("AUDIO_VOICEVOX_FAILED", "VOICEVOX returned an invalid query")
        if not query:
            query = part
        else:
            query["accent_phrases"].extend(part["accent_phrases"])
            query["kana"] = None
    wav_path.write_bytes(
        _voicevox_post(
            f"/synthesis?speaker={speaker}", _VOICEVOX_WAV_LIMIT, json.dumps(query).encode()
        )
    )


def write_briefing_audio(body: str, voice: Path | int, output: Path) -> None:
    text = _BRACKETS.sub(_omit_citation, body).strip()
    if not text:
        raise AudioError("AUDIO_BRIEFING_EMPTY", "the saved briefing has no text to narrate")
    if isinstance(voice, Path):
        voice = voice.expanduser().absolute()
        config = voice.with_name(voice.name + ".json")
        if not voice.is_file() or not config.is_file():
            raise AudioError(
                "AUDIO_VOICE_NOT_FOUND", "provide a local Piper .onnx voice and its .onnx.json"
            )
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise AudioError("AUDIO_OUTPUT_ALREADY_EXISTS", "choose a new output path")

    with tempfile.TemporaryDirectory(prefix=".shoin-audio-", dir=output.parent) as staging:
        wav_path = Path(staging) / "briefing.wav"
        if isinstance(voice, Path):
            _run_piper(text, voice, wav_path)
        else:
            _run_voicevox(text, voice, wav_path)

        try:
            with wave.open(str(wav_path), "rb") as wav:
                if wav.getnframes():
                    wav.setpos(wav.getnframes() - 1)
                if len(wav.readframes(1)) != wav.getsampwidth() * wav.getnchannels():
                    raise AudioError("AUDIO_INVALID_WAV", "the engine produced incomplete audio")
        except (FileNotFoundError, EOFError, wave.Error) as exc:
            raise AudioError("AUDIO_INVALID_WAV", "the engine did not produce a valid WAV") from exc
        wav_path.chmod(0o600)
        try:
            os.link(wav_path, output)
        except FileExistsError as exc:
            raise AudioError("AUDIO_OUTPUT_ALREADY_EXISTS", "choose a new output path") from exc
