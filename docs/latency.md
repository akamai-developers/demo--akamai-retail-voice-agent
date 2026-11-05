# Measure latency

Measure real inference from the intended client location. Mock timing checks validate the measurement code only. They cannot predict speech response time or compare GPU regions.

## Prepare an isolated run

Use an idle GPU or an independently allocated GPU with enough free memory for the complete stack. Inspect existing listeners, containers, services, and GPU processes before starting anything. Do not stop or reconfigure an existing deployment, reuse its service names or ports, or change existing firewall rules to make a test work. Use an approved private management route when public SSH is restricted.

Supply a permitted spoken request as mono 16 kHz PCM16 WAV, no longer than 20 seconds. Keep it outside version control. Use the same audio file, source version, model revisions, context limit, and client location for comparisons. The report records the audio hash, duration, and byte count, not its transcript.

## Warm up and measure

Connect the test gateway through an approved SSH tunnel or its own HTTPS endpoint. Load the test tenant key into the environment from a private file. Do not paste it into a shell command or recording.

Run one warm-up trial, then a separate measured run:

```bash
python tools/measure_latency.py --base-url http://127.0.0.1:18765 \
  --audio /private/path/request.wav --trials 1 \
  --location "ACTUAL_CLIENT_LOCATION" --build "TESTED_COMMIT" \
  --output .local/latency-warmup.json
python tools/measure_latency.py --base-url http://127.0.0.1:18765 \
  --audio /private/path/request.wav --trials 10 \
  --location "ACTUAL_CLIENT_LOCATION" --build "TESTED_COMMIT" \
  --output .local/latency-measured.json
```

The fixed workload includes a greeting, product search, product follow-up, stock check, and spoken request. Each trial creates and closes its own session. If the tenant's slot is occupied, the tool stops rather than ending someone else's conversation.

## Read the results

`wall_audio_ms` is the median time from submitting a turn until its reply audio has arrived at the client. The summary includes attempted, completed, and failed turns. Failed turns are excluded from latency medians, so a fast error cannot make the app appear faster.

`asr_ms`, `model_ms`, and `tts_ms` measure speech recognition, language-model work, and speech synthesis on the gateway's clock. Queue time and individual tool calls remain in the per-turn records. Browser and gateway clocks are never subtracted from each other.

These HTTP measurements exclude the time spent speaking, the browser's silence-detection delay, audio-device startup, and speaker playback. Inspect the browser's live turn timings and listen to full replies to evaluate conversational responsiveness. A fast first event is only an acknowledgement, not the start of the spoken answer.

For an Amsterdam versus another-region comparison, keep the client in the same location, preferably the actual presentation site, and repeat the same warm-up and measured workload. Compare completed-turn medians alongside failure counts, model timings, reply sizes, and GPU load. Do not attribute a latency difference to geography when models, GPU types, or settings differ.

Measure try-on separately using its `preview.completed` duration and client image-arrival timing. Image generation has a different workload from a voice reply. No GPU-region performance claim should be made until the corresponding hardware run is recorded.
