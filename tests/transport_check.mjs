import assert from "node:assert/strict";
import { sendAttempt } from "../src/retail_voice/web/transport.js";

const events = [{ event_type: "turn.started", turn_id: "one" }, { event_type: "turn.completed", turn_id: "one", result_path: "/result" }];
const result = { turn_id: "one", status: "complete", audio_path: "/audio" };
const reply = { type: "reply", result, audio_base64: btoa("RIFFtest") };
const calls = [];
globalThis.fetch = async (path, options) => {
  calls.push(path);
  assert.equal(options.headers["X-Inline-Reply"], "1");
  // Split across arbitrary network chunks, including inside the media frame.
  const text = [...events, reply].map(e => JSON.stringify(e) + "\n").join("");
  const encoded = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < encoded.length; i += 13) controller.enqueue(encoded.slice(i, i + 13));
    controller.close();
  } }));
};
const seen = [], marks = [];
const sent = await sendAttempt({ session_id: "s", session_token: "secret" },
  { path: "/messages", key: "key", runs: 0 }, e => seen.push(e), undefined, m => marks.push(m));
assert.equal(calls.length, 1, "no result/audio round trip");
assert.deepEqual(seen, events, "private reply media never reaches trace rendering");
assert.deepEqual(sent.result, result);
assert.equal(await (await sent.audio.promise).text(), "RIFFtest");
assert.deepEqual(marks, ["request", "firstByte", "terminal", "result", "audio"]);
console.log("PASS: inline reply delivery, chunk boundaries, audio decoding and trace isolation");
