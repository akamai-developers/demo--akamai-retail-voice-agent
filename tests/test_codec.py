"""A clean offline speech host must not fetch codec training dependencies."""

import json
import tempfile
import unittest
from pathlib import Path

from retail_voice.codec import prepare_codec
from retail_voice.speech_service import bind_local_tokenizers


class OfflineCodec:
    restores = 0

    @classmethod
    def restore_from(cls, path, return_config=False, **kwargs):
        if return_config:
            return {
                "discriminator": {"model": "uncached-training-model"},
                "use_scl_loss": True,
                "use_slm_loss": True,
                "sample_rate": 22050,
            }
        config = kwargs["override_config_path"]
        if config["discriminator"] or config["use_scl_loss"] or config["use_slm_loss"]:
            raise RuntimeError("Offline host cannot download training dependencies")
        if kwargs.get("strict") is not False or kwargs.get("map_location") != "cpu":
            raise RuntimeError("Restore must omit training weights and use CPU")
        cls.restores += 1
        return cls()

    def save_to(self, path):
        Path(path).write_bytes(b"inference weights")


class OfflineCodecTests(unittest.TestCase):
    def test_offline_prepare_preserves_source_and_reuses_verified_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "pinned.nemo"
            source.write_bytes(b"original pinned checkpoint")
            runtime = Path(temporary) / "runtime"
            OfflineCodec.restores = 0
            output = prepare_codec(source, runtime, OfflineCodec, "source-revision")
            self.assertEqual(source.read_bytes(), b"original pinned checkpoint")
            self.assertEqual(output.read_bytes(), b"inference weights")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                prepare_codec(source, runtime, OfflineCodec, "source-revision"), output
            )
            self.assertEqual(OfflineCodec.restores, 1)
            self.assertIn(
                "output_sha256", json.loads((runtime / "inference-codec.json").read_text())
            )

    def test_corrupt_or_different_revision_checkpoint_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "pinned.nemo"
            source.write_bytes(b"original")
            runtime = Path(temporary) / "runtime"
            OfflineCodec.restores = 0
            output = prepare_codec(source, runtime, OfflineCodec, "first")
            output.write_bytes(b"corrupt")
            prepare_codec(source, runtime, OfflineCodec, "first")
            self.assertEqual(output.read_bytes(), b"inference weights")
            prepare_codec(source, runtime, OfflineCodec, "second")
            self.assertEqual(OfflineCodec.restores, 3)


class OfflineTokenizerTests(unittest.TestCase):
    def test_every_external_tokenizer_uses_pinned_local_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "tokenizer_config.json").write_text("{}")
            config = {
                "phoneme": {"_target_": "PackagedPhonemeTokenizer"},
                "context": {"_target_": "AutoTokenizer", "pretrained_model": "pinned-model"},
                "french": {"_target_": "AutoTokenizer", "pretrained_model": "pinned-model"},
            }
            bind_local_tokenizers(config, "pinned-model", directory)
            self.assertEqual(config["context"]["pretrained_model"], str(directory))
            self.assertEqual(config["french"]["pretrained_model"], str(directory))
            self.assertNotIn("pretrained_model", config["phoneme"])

    def test_missing_or_unpinned_tokenizer_fails_before_model_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = {"context": {"_target_": "AutoTokenizer", "pretrained_model": "other-model"}}
            with self.assertRaisesRegex(RuntimeError, "missing"):
                bind_local_tokenizers(config, "pinned-model", directory)
            (directory / "tokenizer_config.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "unpinned"):
                bind_local_tokenizers(config, "pinned-model", directory)


if __name__ == "__main__":
    unittest.main()
