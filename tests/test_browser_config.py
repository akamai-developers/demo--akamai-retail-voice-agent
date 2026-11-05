"""Browser preflight must fail before a misconfigured run sends GPU turns."""

import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from browser_check import microphone_settings, tone_wav


class BrowserMicrophoneTests(unittest.TestCase):
    def test_external_run_requires_speech_fixture_before_browser_or_network(self):
        env = {key: value for key, value in os.environ.items() if key != "FAKE_MIC_WAV"}
        env["BASE_URL"] = "http://127.0.0.1:1"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("browser_check.py"))],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BASE_URL runs require FAKE_MIC_WAV", result.stderr)

    def test_isolated_mock_keeps_generated_tone_default(self):
        self.assertEqual(microphone_settings({}), (None, 2.5))

    def test_valid_local_wav_is_resolved_before_chromium(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.wav"
            tone_wav(path)  # Container check only. Real-speech content is operator supplied.
            self.assertEqual(
                microphone_settings(
                    {
                        "BASE_URL": "http://127.0.0.1:1",
                        "FAKE_MIC_WAV": str(path),
                        "FAKE_MIC_SECONDS": "5",
                    }
                ),
                (str(path.resolve()), 5.0),
            )

    def test_missing_invalid_and_empty_wav_fail_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.wav"
            with self.assertRaises(FileNotFoundError):
                microphone_settings({"FAKE_MIC_WAV": str(path)})
            path.write_bytes(b"not a WAV file")
            with self.assertRaises(wave.Error):
                microphone_settings({"FAKE_MIC_WAV": str(path)})
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            with self.assertRaisesRegex(ValueError, "audio frames"):
                microphone_settings({"FAKE_MIC_WAV": str(path)})

    def test_capture_duration_is_bounded_and_finite(self):
        for duration in ("0", "-1", "21", "nan", "inf"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                microphone_settings({"FAKE_MIC_SECONDS": duration})
