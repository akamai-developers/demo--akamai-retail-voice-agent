import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from retail_voice.api import fixture_media
from retail_voice.settings import GPUSettings
from retail_voice.speech_service import NoSpeech, SpeechRuntime, create_app


class SpeechServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = GPUSettings(
            site_id="site-a",
            region="us-test",
            model_root=Path(self.temp.name),
            runtime_dir=Path(self.temp.name),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_numpy_asr_input_has_expected_pcm_scale_and_no_file_path(self):
        class ASR:
            def transcribe(self, audio, **kwargs):
                self.audio, self.kwargs = audio, kwargs
                return ["a synthetic question"]

        asr = ASR()
        runtime = SpeechRuntime(self.settings)
        runtime.models["asr"] = asr
        result = runtime.recognize(fixture_media()[0])
        self.assertEqual(result, {"text": "a synthetic question"})
        self.assertEqual(len(asr.audio[0]), 1600)
        self.assertEqual(str(asr.audio[0].dtype), "float32")
        self.assertEqual(asr.kwargs["num_workers"], 0)
        self.assertFalse(asr.kwargs["use_lhotse"])

    def test_private_service_validates_audio_json_and_busy_status(self):
        class Runtime:
            models = {}
            lock = threading.Lock()

            def load(self):
                self.models = {"asr": object(), "tts": object()}

            def run(self, operation, value):
                return (
                    {"text": "synthetic question"}
                    if operation == "recognize"
                    else fixture_media()[0]
                )

        runtime = Runtime()
        with TestClient(create_app(self.settings, runtime)) as client:
            self.assertTrue(client.get("/health").json()["ready"])
            runtime.lock.acquire()
            self.assertTrue(client.get("/health").json()["busy"])
            runtime.lock.release()
            self.assertEqual(client.post("/asr", content=fixture_media()[0]).status_code, 200)
            self.assertEqual(client.post("/asr", content=b"bad").status_code, 400)
            self.assertEqual(client.post("/asr", content=b"x" * 640045).status_code, 413)
            self.assertEqual(client.post("/tts", json={"text": "Hello"}).status_code, 200)
            self.assertEqual(
                client.post("/tts", json={"text": "Hello", "speaker": "other"}).status_code, 400
            )
            self.assertEqual(client.post("/tts", json={"text": "x" * 1201}).status_code, 400)

    def test_audio_without_speech_is_a_rejection_not_a_failure(self):
        """Silence or a tone makes the pinned ASR raise. With CUDA still answering that is a
        400 the gateway treats as a model refusal; a dead device stays a 503."""

        class Silent:
            def transcribe(self, *args, **kwargs):
                raise ValueError("no frames")

        class Empty:
            def transcribe(self, *args, **kwargs):
                return [""]

        runtime = SpeechRuntime(self.settings)
        runtime.device_check = lambda: None
        for asr in (Silent(), Empty()):
            runtime.models["asr"] = asr
            with self.assertRaises(NoSpeech):
                runtime.recognize(fixture_media()[0])
        runtime.models["asr"] = Silent()

        def broken_device():
            raise RuntimeError("CUDA error")

        runtime.device_check = broken_device
        with self.assertRaises(RuntimeError):
            runtime.recognize(fixture_media()[0])

        class Runtime:
            models = {"asr": object(), "tts": object()}
            lock = threading.Lock()

            def load(self):
                pass

            def run(self, operation, value):
                if operation == "recognize":
                    raise NoSpeech("no_speech")
                raise RuntimeError("boom")

        with TestClient(create_app(self.settings, Runtime())) as client:
            response = client.post("/asr", content=fixture_media()[0])
            self.assertEqual((response.status_code, response.json()["detail"]), (400, "no_speech"))
            self.assertEqual(client.post("/tts", json={"text": "Hello"}).status_code, 503)

    def test_gpu_config_requires_explicit_path(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                GPUSettings.from_env()
