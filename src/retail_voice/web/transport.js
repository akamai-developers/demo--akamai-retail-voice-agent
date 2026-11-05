// HTTP calls, NDJSON event streaming and retry state. No DOM access.

export class HttpError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.http = status;
    this.detail = detail || "";
  }
}

export async function request(path, { method = "GET", token, body, headers = {}, keepalive, signal } = {}) {
  const auth = token ? { Authorization: `Bearer ${token}` } : {};
  const response = await fetch(path, { method, headers: { ...auth, ...headers }, body, keepalive, signal });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new HttpError(response.status, typeof data.detail === "string" ? data.detail : "");
  }
  return response;
}

// `mark(name)` records a browser observation (N3): "firstByte" when the first chunk of the event
// stream arrives and "terminal" when the turn's last event is read.
export async function readEvents(response, onEvent, signal, mark = () => {}, onReply = () => {}) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const events = [];
  let buffer = "";
  let first = true;
  const take = line => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === "reply") {
      onReply(event);
      return;
    }
    events.push(event);
    if (event.result_path) mark("terminal");
    onEvent(event);
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      signal?.throwIfAborted();
      if (first && value && value.length) {
        first = false;
        mark("firstByte");
      }
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      lines.forEach(take);
      if (done) break;
    }
    take(buffer);
    return events;
  } finally {
    reader.releaseLock();
  }
}

// A turn attempt keeps its idempotency key and its exact body until the server answers.
// Retrying after a lost response then returns the same turn instead of running a new one.
export function newAttempt(path, body, headers = {}) {
  return { key: crypto.randomUUID(), path, body, headers, runs: 0 };
}

export async function blob(path, token, signal) {
  return (await request(path, { token, signal })).blob();
}

// One attempt: the events as they stream, then the result. When the terminal event names the
// reply audio, its fetch starts together with the result's instead of after it (N3), and is
// returned as `audio`: { path, promise } resolving to the Blob, or to null if that fetch failed.
// Marks: "request" before the POST, "firstByte", "terminal", "result", and "audio" with the
// byte count once the audio arrives.
export async function sendAttempt(session, attempt, onEvent, signal, mark = () => {}) {
  attempt.runs += 1;
  mark("request");
  const response = await request(`/v1/sessions/${session.session_id}${attempt.path}`, {
    method: "POST",
    signal,
    token: session.session_token,
    body: attempt.body,
    headers: { ...attempt.headers, "Idempotency-Key": attempt.key, "X-Inline-Reply": "1" },
  });
  let inline = null;
  const events = await readEvents(response, onEvent, signal, mark, frame => { inline = frame; });
  const terminal = events.at(-1);
  if (!terminal || !terminal.result_path) throw new Error("incomplete_event_stream");
  if (inline && inline.result.turn_id === terminal.turn_id) {
    mark("result");
    let audio = null;
    if (inline.audio_base64 && inline.result.audio_path) {
      const bytes = Uint8Array.from(atob(inline.audio_base64), c => c.charCodeAt(0));
      const data = new Blob([bytes], { type: "audio/wav" });
      mark("audio", data.size);
      audio = { path: inline.result.audio_path, promise: Promise.resolve(data) };
    }
    return { events, result: inline.result, audio };
  }
  let audio = null;
  if (terminal.audio_path) {
    const promise = blob(terminal.audio_path, session.session_token, signal).then(
      data => {
        mark("audio", data.size);
        return data;
      },
      () => null,
    );
    audio = { path: terminal.audio_path, promise };
  }
  const result = await (await request(terminal.result_path, { token: session.session_token, signal })).json();
  mark("result");
  return { events, result, audio };
}
