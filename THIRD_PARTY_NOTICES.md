# Third-party assets and models

Application source uses [Apache-2.0](LICENSE). Model licenses, font licenses, and trademark rights are separate.

Instrument Sans and JetBrains Mono are distributed with their original license files in `src/retail_voice/web/fonts`. Akamai logos are trademarks; this repository does not grant trademark rights. Preserve their proportions and colors.

Catalog images are generated depictions of fictional products. `src/retail_voice/data/assets.json` records their generator, hashes, provenance, and review status. No real customer or reference-person photos are included. Generated images still marked pending review should be reviewed before publication.

The bundled try-on person at `src/retail_voice/data/demo/fictional-person.png` was generated from text for this demo, without source photographs. It is a fictional adult. `person-provenance.json` in the same directory records the prompt and file hash. Private operator-supplied photos and their generated previews are excluded from the public repository and release archive.

Model weights are downloaded separately and retain their own licenses:

- [Nemotron Speech](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b)
- [Magpie TTS](https://huggingface.co/nvidia/magpie_tts_multilingual_357m)
- [NeMo Nano Codec](https://huggingface.co/nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps)
- [Nemotron 3 Nano Omni FP8](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8)
- [ByT5 tokenizer](https://huggingface.co/google/byt5-small/tree/68377bdc18a2ffec8a0533fef03b1c513a4dd49d) (Apache-2.0; tokenizer files only)
- [FLUX.2 klein 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)

Model revisions are pinned in `src/retail_voice/profiles/nvidia-one-gpu.json`. Container bases are pinned in the build recipes under `deploy/`; final local image IDs are recorded after their builds and scans pass. The optional image model revision is pinned in `src/retail_voice/tryon.py`. Review the license at the pinned revision before accepting a download.
