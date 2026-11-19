from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
import wave
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

    def invoke(self, notebook_id: int | None = None) -> tuple[int, str, str]:
        args = [
            "--db", str(self.db), "audio", str(notebook_id or self.notebook),
            "--model", str(self.model), "--output", str(self.output),
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
