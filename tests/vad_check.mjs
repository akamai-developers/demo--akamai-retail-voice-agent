// The silence gate and the meter scale, in plain Node. Blocks are 128 frames at 48 kHz, so
// each lasts 2.67 ms, as the recorder worklet posts them in Chromium.
import assert from "node:assert/strict";
import {
  BRIDGE_MS, HANGOVER_MS, METER_FLOOR_DB, METER_RELEASE_MS, MIN_SPEECH_MS, NO_INPUT_MS,
  SPEECH_THRESHOLD, SilenceGate, meterLevel, smoothLevel,
} from "../src/retail_voice/web/vad.js";

const BLOCK_MS = (128 / 48000) * 1000;

// Feeds [level, milliseconds] segments and returns the first time each verdict appeared.
function run(segments, options) {
  const gate = new SilenceGate(options);
  const first = {};
  let now = 0;
  for (const [level, ms] of segments) {
    for (const end = now + ms; now + BLOCK_MS <= end + 1e-9;) {
      now += BLOCK_MS;
      const verdict = gate.feed(level, now);
      if (!(verdict in first)) first[verdict] = now;
    }
  }
  return { first, gate, verdict: gate.verdict, now };
}

assert.deepEqual(
  [SPEECH_THRESHOLD, MIN_SPEECH_MS, HANGOVER_MS, NO_INPUT_MS, BRIDGE_MS],
  [0.02, 300, 800, 8000, 200],
);

// Silence only: waiting, then nothing at the no-input limit, and nothing stays final.
let r = run([[0, 9000]]);
assert.equal(r.verdict, "nothing");
assert.ok(Math.abs(r.first.nothing - NO_INPUT_MS) < BLOCK_MS, r.first.nothing);
assert.equal(r.gate.feed(0.3, r.now + BLOCK_MS), "nothing", "nothing is final");

// Speech, then quiet: speaking after 300 ms of voice, ended 800 ms after the last voice.
r = run([[0, 500], [0.1, 1200], [0.001, 2000]]);
assert.ok(Math.abs(r.first.speaking - (500 + MIN_SPEECH_MS)) <= 2 * BLOCK_MS, r.first.speaking);
assert.ok(Math.abs(r.first.ended - (1700 + HANGOVER_MS)) <= 2 * BLOCK_MS, r.first.ended);
assert.equal(r.gate.feed(0.3, r.now + BLOCK_MS), "ended", "ended is final");

// A click or a cough shorter than 300 ms is not speech.
r = run([[0, 1000], [0.5, 150], [0, 7000]]);
assert.equal(r.verdict, "nothing");
assert.ok(!("speaking" in r.first));

// Dips shorter than the bridge keep one run going; longer dips start a new run.
r = run(Array.from({ length: 6 }, () => [[0.1, 50], [0, 100]]).flat());
assert.ok("speaking" in r.first, "50 ms syllables with 100 ms gaps are speech");
r = run(Array.from({ length: 6 }, () => [[0.1, 150], [0, 250]]).flat());
assert.ok(!("speaking" in r.first), "150 ms bursts with 250 ms gaps are not");

// Voice inside the hangover restarts it.
r = run([[0.1, 400], [0, 500], [0.1, 100], [0, 700]]);
assert.equal(r.verdict, "speaking");
r = run([[0.1, 400], [0, 500], [0.1, 100], [0, 900]]);
assert.equal(r.verdict, "ended");

// Speech that starts just before the no-input limit is allowed to finish.
r = run([[0, 7900], [0.1, 600], [0, 1600]]);
assert.ok(!("nothing" in r.first), JSON.stringify(r.first));
assert.equal(r.verdict, "ended");

// The threshold is inclusive and adjustable for a quieter device.
assert.equal(run([[SPEECH_THRESHOLD, 400]]).verdict, "speaking");
assert.equal(run([[0.01, 400], [0, 7600]]).verdict, "nothing");
assert.equal(run([[0.01, 400], [0, 1600]], { threshold: 0.005 }).verdict, "ended");
assert.throws(() => new SilenceGate({ threshold: 0 }), RangeError);
assert.throws(() => new SilenceGate({ threshold: 1.5 }), RangeError);

// Meter: decibels from -60 (empty) to 0 dBFS (full), instant rise, 150 ms release.
assert.equal(METER_FLOOR_DB, -60);
assert.equal(METER_RELEASE_MS, 150);
assert.equal(meterLevel(0), 0);
assert.equal(meterLevel(-0.5), 0);
assert.equal(meterLevel(Number.NaN), 0);
assert.equal(meterLevel(0.001), 0);
assert.equal(meterLevel(1), 1);
assert.equal(meterLevel(2), 1);
assert.ok(Math.abs(meterLevel(SPEECH_THRESHOLD) - 0.4330) < 1e-3);
assert.ok(Math.abs(meterLevel(0.1) - 2 / 3) < 1e-9);
assert.equal(smoothLevel(0.2, 0.9, 10), 0.9);
assert.ok(Math.abs(smoothLevel(1, 0, METER_RELEASE_MS) - Math.exp(-1)) < 1e-12);
assert.equal(smoothLevel(0.5, 0.1, 0), 0.5);
console.log("PASS: silence gate thresholds, bridging, hangover, no-input limit and final verdicts; meter scale and release");

assert.equal(run([[0.1, 400], [0, 1000]], { hangoverMs: 1500 }).verdict, "speaking");
assert.throws(() => new SilenceGate({ hangoverMs: NaN }), RangeError);
