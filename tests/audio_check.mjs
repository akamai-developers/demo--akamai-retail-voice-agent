import assert from "node:assert/strict";
import { toWav } from "../src/retail_voice/web/audio.mjs";
for (const rate of [16000, 44100, 48000]) {
  const input = new Float32Array(rate).fill(0.5);
  const blob = toWav([input.subarray(0, 100), input.subarray(100)], rate);
  const buffer = await blob.arrayBuffer();
  const view = new DataView(buffer);
  assert.equal(buffer.byteLength, 32044);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 32000);
  assert.equal(view.getInt16(44, true), 16383);
}
const clipped = new DataView(await toWav([new Float32Array([-2, 2])], 16000).arrayBuffer());
assert.equal(clipped.getInt16(44, true), -32768);
assert.equal(clipped.getInt16(46, true), 32767);
console.log("PASS: WAV encoding at 16/44.1/48 kHz, frame sizes and clipping");
