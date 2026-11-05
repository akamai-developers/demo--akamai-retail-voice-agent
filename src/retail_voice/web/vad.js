// Voice level math: the silence gate that ends a recording, and the microphone meter's scale.
// Pure: no browser objects, so Node tests import it directly. Levels are the RMS of float
// samples in [-1, 1], one value per recorded block. Times are milliseconds of captured audio
// since the microphone opened, not wall-clock time.

// A block at or above this RMS counts as voice. 0.02 is about -34 dBFS.
export const SPEECH_THRESHOLD = 0.02;
// Voice must last this long before it counts as speech, so a click or a cough does not.
export const MIN_SPEECH_MS = 300;
// After speech, this much time below the threshold ends the utterance.
export const HANGOVER_MS = 800;
// With no speech by this time, nothing was heard and nothing is sent.
export const NO_INPUT_MS = 8000;
// A dip shorter than this does not break a run of voice. Block levels dip between syllables.
export const BRIDGE_MS = 200;

// feed() returns "waiting" (no speech yet), "speaking" (speech heard, not yet over), "ended"
// (speech, then the hangover of quiet) or "nothing" (no speech within NO_INPUT_MS). "ended" and
// "nothing" are final. A run of voice still going at NO_INPUT_MS gets the chance to finish.
export class SilenceGate {
  constructor({
    threshold = SPEECH_THRESHOLD,
    minSpeechMs = MIN_SPEECH_MS,
    hangoverMs = HANGOVER_MS,
    noInputMs = NO_INPUT_MS,
    bridgeMs = BRIDGE_MS,
  } = {}) {
    if (!(threshold > 0 && threshold < 1)) throw new RangeError("threshold must be between 0 and 1");
    if (!Number.isFinite(hangoverMs) || hangoverMs < 400 || hangoverMs > 2000) throw new RangeError("pause must be between 400 and 2000 ms");
    Object.assign(this, { threshold, minSpeechMs, hangoverMs, noInputMs, bridgeMs });
    this.runStart = null;
    this.lastVoice = null;
    this.speech = false;
    this.verdict = "waiting";
  }

  feed(level, nowMs) {
    if (this.verdict === "ended" || this.verdict === "nothing") return this.verdict;
    if (level >= this.threshold) {
      if (this.lastVoice === null || nowMs - this.lastVoice > this.bridgeMs) this.runStart = nowMs;
      this.lastVoice = nowMs;
      if (nowMs - this.runStart >= this.minSpeechMs) this.speech = true;
    }
    if (this.speech) {
      this.verdict = nowMs - this.lastVoice >= this.hangoverMs ? "ended" : "speaking";
    } else {
      const voicing = this.lastVoice !== null && nowMs - this.lastVoice <= this.bridgeMs;
      this.verdict = nowMs >= this.noInputMs && !voicing ? "nothing" : "waiting";
    }
    return this.verdict;
  }
}

// The meter draws a decibel scale: -60 dBFS (near silence) is empty, 0 dBFS is full.
export const METER_FLOOR_DB = -60;
// The bar rises at once and falls back with this time constant.
export const METER_RELEASE_MS = 150;

export function meterLevel(rms) {
  if (!(rms > 0)) return 0;
  const db = 20 * Math.log10(rms);
  return Math.min(1, Math.max(0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB));
}

export function smoothLevel(previous, target, elapsedMs) {
  if (target >= previous) return target;
  return target + (previous - target) * Math.exp(-Math.max(0, elapsedMs) / METER_RELEASE_MS);
}
