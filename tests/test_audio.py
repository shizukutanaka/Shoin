from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import wave
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from shoin.cli import main
from shoin.store import Store


class TestAudioCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.root / "notebooks.sqlite3"
        self.model = self.root / "voice with spaces.onnx"
        self.model.touch()
        self.config = self.model.with_suffix(".onnx.json")
        self.config.touch()
        self.output = self.root / "briefing.wav"
        with Store(self.db) as store:
            self.notebook = store.create_notebook("Local research").id

    def invoke(
        self, notebook_id: int | None = None, *, voice: list[str] | None = None,
    ) -> tuple[int, str, str]:
        args = [
            "--db", str(self.db), "audio", str(notebook_id or self.notebook),
            *(["--model", str(self.model)] if voice is None else voice),
            "--output", str(self.output),
        ]
        with (
            patch("sys.stdout", new_callable=io.StringIO) as out,
            patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            rc = main(args)
        return rc, out.getvalue(), err.getvalue()

    def save_briefing(self, body: str) -> None:
        with Store(self.db) as store:
            store.add_studio_output(self.notebook, "briefing", body, "{}")

    def test_narrates_only_latest_briefing_locally(self) -> None:
        self.save_briefing("Old briefing.")
        self.save_briefing("新しい研究 ［Ｓ１、S2］\nUseful result [S2 and S3].")
        with Store(self.db) as store:
            store.add_studio_output(self.notebook, "faq", "Not an overview.", "{}")

        def synthesize(argv: list[str], **kwargs: object) -> None:
            self.assertEqual(argv[:4], ["piper", "-m", str(self.model), "-f"])
            self.assertEqual(kwargs["input"], "新しい研究 \nUseful result .\n")
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            with wave.open(argv[4], "wb") as audio:
                audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x01\x00")

        with patch("shoin.audio.subprocess.run", side_effect=synthesize) as piper:
            rc, out, err = self.invoke()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(str(self.output), out)
        piper.assert_called_once()
        with wave.open(str(self.output), "rb") as audio:
            self.assertEqual(audio.readframes(1), b"\x01\x00")
        self.assertEqual(list(self.root.glob(".shoin-audio-*")), [])

    def test_missing_notebook_and_briefing_do_not_run_piper(self) -> None:
        with Store(self.db) as store:
            other = store.create_notebook("Other notebook").id
            store.add_studio_output(other, "briefing", "Not this notebook.", "{}")
        with patch("shoin.audio.subprocess.run") as piper:
            for nb, code in (
                (self.notebook, "AUDIO_BRIEFING_NOT_FOUND"),
                (other + 1, "NOTEBOOK_NOT_FOUND"),
            ):
                with self.subTest(code=code):
                    rc, out, err = self.invoke(nb)
                    self.assertEqual((rc, out), (1, ""))
                    self.assertIn(code, err)
        piper.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_rejects_missing_voice_config_and_existing_output(self) -> None:
        self.save_briefing("A briefing.")
        self.config.unlink()
        with patch("shoin.audio.subprocess.run") as piper:
            rc, _, err = self.invoke()
            self.assertEqual(rc, 1)
            self.assertIn("AUDIO_VOICE_NOT_FOUND", err)
            self.config.touch()
            self.output.write_bytes(b"existing recording")
            rc, _, err = self.invoke()
            self.assertEqual(rc, 1)
            self.assertIn("AUDIO_OUTPUT_ALREADY_EXISTS", err)
        piper.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"existing recording")

    def test_citation_only_briefing_does_not_run_piper(self) -> None:
        self.save_briefing("[S1] [S2, S3]")
        with patch("shoin.audio.subprocess.run") as piper:
            rc, out, err = self.invoke()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("AUDIO_BRIEFING_EMPTY", err)
        piper.assert_not_called()

    def test_failed_piper_run_leaves_no_partial_output(self) -> None:
        self.save_briefing("A private briefing.")

        def fail(argv: list[str], **kwargs: object) -> None:
            Path(argv[4]).write_bytes(b"partial")
            raise subprocess.CalledProcessError(2, argv, stderr=b"A private briefing.")

        with patch("shoin.audio.subprocess.run", side_effect=fail):
            rc, out, err = self.invoke()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("AUDIO_PIPER_FAILED", err)
        self.assertNotIn("A private briefing.", err)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".shoin-audio-*")), [])

    def test_piper_missing_timeout_and_invalid_wav(self) -> None:
        self.save_briefing("A briefing.")
        for error, code in (
            (FileNotFoundError(), "AUDIO_PIPER_NOT_FOUND"),
            (subprocess.TimeoutExpired("piper", 180), "AUDIO_PIPER_TIMEOUT"),
            (None, "AUDIO_INVALID_WAV"),
        ):
            with self.subTest(code=code), patch("shoin.audio.subprocess.run", side_effect=error):
                rc, out, err = self.invoke()
                self.assertEqual((rc, out), (1, ""))
                self.assertIn(code, err)
                self.assertFalse(self.output.exists())

    @staticmethod
    def voicevox_wav() -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 2)
        return buffer.getvalue()

    def test_voicevox_narrates_japanese_using_only_the_local_engine(self) -> None:
        first = "研究結果" * 250 + "。"
        second = "次の調査 & 実験。"
        self.save_briefing("Older briefing.")
        self.save_briefing(f"{first}［Ｓ１］\n{second} [S2]")
        calls: list[tuple[str, dict[str, list[str]], bytes, str | None]] = []
        wav = self.voicevox_wav()

        class Engine(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                calls.append((parsed.path, params, body, self.headers.get("Content-Type")))
                reply = wav if parsed.path == "/synthesis" else json.dumps({
                    "accent_phrases": [{"id": len(calls)}], "kana": "カナ", "speedScale": 1,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        with HTTPServer(("127.0.0.1", 0), Engine) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def connection(host: str, port: int, *, timeout: float) -> HTTPConnection:
                self.assertEqual((host, port), ("127.0.0.1", 50021))
                return HTTPConnection(host, server.server_port, timeout=timeout)

            try:
                with (
                    patch("shoin.audio.http.client.HTTPConnection", side_effect=connection),
                    patch("shoin.audio.subprocess.run") as piper,
                ):
                    rc, out, err = self.invoke(voice=["--voicevox-speaker", "0"])
                piper.assert_not_called()
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn(str(self.output), out)
        self.assertEqual([path for path, *_ in calls], ["/audio_query", "/synthesis"])
        self.assertEqual(calls[0][1], {"text": [first + "\n" + second], "speaker": ["0"]})
        self.assertEqual(calls[1][1], {"speaker": ["0"]})
        self.assertEqual(calls[1][3], "application/json")
        self.assertEqual(json.loads(calls[1][2])["accent_phrases"], [{"id": 1}])
        self.assertEqual(self.output.read_bytes(), wav)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".shoin-audio-*")), [])

    def test_long_briefing_combines_voicevox_queries_and_validates_wav(self) -> None:
        first = "研究結果" * 250 + "。"
        second = "次の実験" * 20 + "。"
        self.save_briefing(f"{first}\n{second}")
        for truncate in (False, True):
            wav = self.voicevox_wav()
            phrases = [{"id": 1}, {"id": 2}]
            replies = [
                json.dumps({"accent_phrases": [p], "kana": "カナ"}).encode() for p in phrases
            ]
            with (
                self.subTest(truncate=truncate),
                patch("shoin.audio._voicevox_post", side_effect=[
                    *replies, wav[:-2] if truncate else wav,
                ]) as post,
            ):
                rc, _, err = self.invoke(voice=["--voicevox-speaker", "3"])
                self.assertEqual(post.call_count, 3)
                parts = [
                    urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)["text"][0]
                    for call in post.call_args_list[:2]
                ]
                self.assertEqual(parts, [first + "\n", second])
                self.assertEqual(
                    json.loads(post.call_args.args[2]), {"accent_phrases": phrases, "kana": None}
                )
                if truncate:
                    self.assertEqual(rc, 1)
                    self.assertIn("AUDIO_INVALID_WAV", err)
                    self.assertFalse(self.output.exists())
                else:
                    self.assertEqual((rc, err), (0, ""))
                    self.assertEqual(self.output.read_bytes(), wav)
                    self.output.unlink()

    def test_voicevox_errors_do_not_publish_audio_or_disclose_text(self) -> None:
        self.save_briefing("private text")
        for status, body, code in (
            (307, b"private text", "AUDIO_VOICEVOX_FAILED"),
            (200, b"private text", "AUDIO_VOICEVOX_FAILED"),
            (200, b"{}", "AUDIO_VOICEVOX_FAILED"),
            (200, b"x" * (1024 * 1024 + 1), "AUDIO_VOICEVOX_FAILED"),
            (None, b"", "AUDIO_VOICEVOX_UNAVAILABLE"),
        ):
            with self.subTest(status=status), patch("shoin.audio.http.client.HTTPConnection") as c:
                conn = c.return_value
                resp = conn.getresponse.return_value.__enter__.return_value
                resp.status = status
                resp.read.return_value = body
                if status is None:
                    conn.request.side_effect = ConnectionRefusedError("private text")
                rc, out, err = self.invoke(voice=["--voicevox-speaker", "3"])
                self.assertEqual((rc, out), (1, ""))
                self.assertIn(code, err)
                self.assertNotIn("private text", err)
                self.assertFalse(self.output.exists())
                conn.request.assert_called_once()
                conn.close.assert_called_once()
        self.assertEqual(list(self.root.glob(".shoin-audio-*")), [])

    def test_audio_engine_options_are_mutually_exclusive(self) -> None:
        for voice in ([], ["--voicevox-speaker", "-1"], [
            "--model", str(self.model), "--voicevox-speaker", "3",
        ]):
            with self.subTest(voice=voice), self.assertRaises(SystemExit) as err:
                self.invoke(voice=voice)
            self.assertEqual(err.exception.code, 2)

    def test_serve_accepts_local_audio_engine_options(self) -> None:
        for args, voice in (
            ([], None),
            (["--piper-model", str(self.model)], self.model),
            (["--voicevox-speaker", "0"], 0),
        ):
            with self.subTest(voice=voice), patch("shoin.server.serve") as serve:
                self.assertEqual(main(["--db", str(self.db), "serve", "--port", "0", *args]), 0)
                serve.assert_called_once_with(0, str(self.db), audio_voice=voice)
