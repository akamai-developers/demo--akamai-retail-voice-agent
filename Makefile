.PHONY: help setup run dev-mock dev-nvidia serve-speech preflight smoke-gpu check test-integration test-ui test-tryon-ui test-architecture-ui test-package
help:
	@echo "setup: create private keys and install the locked local environment"
	@echo "run: start the loopback gateway using .local/gateway.env"
	@echo "dev-mock: loopback mock gateway (requires three exported credentials)"
	@echo "dev-nvidia / serve-speech: start loopback services with GPU_CONFIG"
	@echo "preflight: read-only hardware/artifact/service checks; does not start or download models"
	@echo "smoke-gpu: real inference; requires ARGS with --allow-inference --audio --image"
	@echo "check: formatting, lint, gradual types, unit/API/contracts, client checks, silence gate"
	@echo "test-integration: API and lifecycle regression suite"
	@echo "test-ui: Chromium flows and four screenshots (playwright install chromium first)"
	@echo "  BASE_URL runs require FAKE_MIC_WAV=local-speech.wav; use FAKE_MIC_SECONDS=5 for shopping-rain.wav"
	@echo "test-package: clean-venv wheel installation and asset smoke check"
	@echo "test-tryon-ui: private preview and presentation flow in Chromium"
	@echo "test-architecture-ui: illustrative flow, presentation controls and live-tab separation"

setup:
	python3 scripts/demo.py setup

run:
	python3 scripts/demo.py run

dev-mock:
	DEMO_MODE=mock python -m uvicorn retail_voice.api:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log

dev-nvidia:
	DEMO_MODE=nvidia python -m uvicorn retail_voice.api:create_app --factory --host 127.0.0.1 --port 8765 --no-access-log

serve-speech:
	PYTHONPATH=src python -m uvicorn retail_voice.speech_service:create_app --factory --host 127.0.0.1 --port 8082 --no-access-log

preflight:
	python -m retail_voice.cli preflight

smoke-gpu:
	python -m retail_voice.cli smoke-gpu $(ARGS)

check:
	python scripts/public_check.py
	python -m ruff format --check src tests tools scripts
	python -m ruff check src tests tools scripts
	python -m mypy --check-untyped-defs --ignore-missing-imports src
	PYTHONPATH=src python -m unittest discover -s tests -v
	for file in src/retail_voice/web/*.js; do node --check $$file || exit 1; done
	node --no-warnings --experimental-vm-modules tests/client_check.cjs
	node tests/audio_check.mjs
	node tests/vad_check.mjs
	node --no-warnings tests/transport_check.mjs

test-integration:
	PYTHONPATH=src python -m unittest discover -s tests -v

test-ui:
	PYTHONPATH=src python tests/browser_check.py

test-tryon-ui:
	PYTHONPATH=src python tests/tryon_browser_check.py

test-architecture-ui:
	PYTHONPATH=src python tests/architecture_browser_check.py

test-package:
	python tests/install_check.py
