// Microphone capture through an AudioWorklet, encoded to mono 16 kHz PCM16 WAV in the browser.
// Browser objects are touched only inside startCapture, never when the module loads.
import { toWav } from "./audio.mjs";
import { SilenceGate } from "./vad.js";

export const MAX_SECONDS = 20;
// The microphone reopens after a reply without a click. If the browser will not start audio
// without one, fail clearly instead of waiting forever.
const RESUME_TIMEOUT_MS = 3000;

// onLevel(rms, audioMs) receives every recorded block's level, timed by the samples captured
// so far. It only ever reports audio that was actually recorded. onAuto("ended") fires once
// when speech was followed by the silence gate's hangover, onAuto("nothing") when no speech
// came within its no-input limit; onLimit fires at MAX_SECONDS. Only one of the three fires,
// and blocks after it are not recorded. `threshold` overrides the gate's speech level.
export async function startCapture({ onTick, onLimit, onLevel, onAuto, signal, threshold, hangoverMs }) {
  if (!globalThis.isSecureContext || !globalThis.navigator?.mediaDevices?.getUserMedia) {
    throw new Error("microphone_requires_https_or_localhost");
  }
  const gate = new SilenceGate({ ...(threshold === undefined ? {} : { threshold }), ...(hangoverMs === undefined ? {} : { hangoverMs }) });
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true },
    video: false,
  });
  let context;
  let disposed = false;
  const dispose = async () => {
    if (disposed) return;
    disposed = true;
    stream.getTracks().forEach(track => track.stop());
    if (context) await context.close();
  };
  // Permission itself cannot be cancelled. Once granted, sign-out immediately stops tracks,
  // including while the worklet or AudioContext resume promise is still pending.
  const abort = () => { void dispose().catch(() => {}); };
  signal?.addEventListener("abort", abort, { once: true });
  try {
    signal?.throwIfAborted();
    context = new AudioContext();
    await context.audioWorklet.addModule("/app/recorder-worklet.js");
    signal?.throwIfAborted();
    let timer;
    await Promise.race([
      context.resume(),
      new Promise(resolve => { timer = setTimeout(resolve, RESUME_TIMEOUT_MS); }),
    ]);
    clearTimeout(timer);
    signal?.throwIfAborted();
    if (context.state !== "running") throw new Error("audio_context_not_running");
  } catch (error) {
    signal?.removeEventListener("abort", abort);
    await dispose();
    throw error;
  }
  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "pcm-recorder");
  const silent = context.createGain();
  silent.gain.value = 0;
  source.connect(node);
  node.connect(silent);
  silent.connect(context.destination);
  // audioMs: recorded audio so far. lastVoiceMs: when the last block at or above the speech level
  // ended, in the same recorded-audio time, so the quiet before the end is measured, not assumed.
  const capture = {
    chunks: [], samples: 0, rate: context.sampleRate, started: Date.now(), stopped: false,
    done: false, heard: false, audioMs: 0, lastVoiceMs: null,
  };
  const limit = MAX_SECONDS * capture.rate;
  node.port.onmessage = event => {
    if (capture.stopped || capture.done) return;
    const { samples, rms } = event.data;
    const chunk = samples.subarray(0, Math.max(0, limit - capture.samples));
    capture.chunks.push(chunk);
    capture.samples += chunk.length;
    const audioMs = (capture.samples / capture.rate) * 1000;
    onLevel?.(rms, audioMs);
    const verdict = gate.feed(rms, audioMs);
    capture.heard = gate.speech;
    capture.audioMs = audioMs;
    capture.lastVoiceMs = gate.lastVoice;
    if (capture.samples >= limit) {
      capture.done = true;
      onLimit();
    } else if (onAuto && (verdict === "ended" || verdict === "nothing")) {
      capture.done = true;
      onAuto(verdict);
    }
  };
  const ticker = setInterval(() => onTick(Math.floor((Date.now() - capture.started) / 1000)), 250);
  capture.stop = async (discard = false) => {
    if (capture.stopped) return null;
    capture.stopped = true;
    clearInterval(ticker);
    node.port.onmessage = null;
    signal?.removeEventListener("abort", abort);
    node.disconnect();
    source.disconnect();
    await dispose();
    if (discard || capture.samples === 0) return null;
    return toWav(capture.chunks, capture.rate);
  };
  return capture;
}
