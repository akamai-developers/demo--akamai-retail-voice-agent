// The How it works diagram: what each component is, which observed event belongs to which
// component, and the marks for the latest turn. Pure apart from the elements passed in. Nothing
// here runs on a timer: a mark appears only when the gateway reported an event or the browser
// observed a step of its own (the result, the audio, playback).
import { el } from "./conversation.js";

export const NODES = [
  "mic", "gateway", "orchestrator", "asr", "llm", "tools", "result", "tts", "delivery", "playback",
];

// Plain words first; each technical word gets a one-line explanation in `term`. No secrets and no
// hostnames: model ids come from /v1/info, everything else is the code layout.
export const DETAILS = {
  mic: {
    step: 1, where: "Browser", title: "Browser capture",
    text: "The page records your voice in the browser and turns it into a 16 kHz mono WAV file. By default, a silence gate ends the recording 0.8 seconds after you stop talking. The pause can be adjusted for the speaker.",
    code: "web/capture.js · web/vad.js · web/recorder-worklet.js",
    storage: "The recording stays in page memory and is sent once. Nothing is saved.",
    term: "AudioWorklet: a browser audio thread that hands the page raw samples as they are recorded.",
  },
  gateway: {
    step: 2, where: "CPU", title: "Gateway and admission",
    text: "One FastAPI process checks the retailer key or session token on every request and decides when a turn may run. It keeps two conversations, one per retailer, runs one whole turn at a time and lets one more wait.",
    code: "api.py · auth.py · admission.py",
    route: "POST /v1/sessions · POST /v1/sessions/{id}/messages",
    storage: "The session lives in gateway memory until End, sign-out, 5 idle minutes or a restart. A retry with the same Idempotency-Key returns the same turn instead of running it again.",
    term: "Admission queue: the gate in front of the one GPU, so a second shopper's turn waits instead of competing.",
    note: "Caddy on 443 stands in front of this gateway: a Let's Encrypt certificate, per-address rate limits and a firewall that admits listed addresses only.",
  },
  orchestrator: {
    step: 3, where: "CPU", title: "Pipecat orchestration and session memory",
    text: "Pipecat runs the turn's stages in order under one deadline: speech recognition, the model and its tool loop, then speech synthesis. Session memory holds the conversation, the cards on screen with their revision, the selected product and variant, and the cart.",
    code: "pipeline.py (run_message) · orchestration.py · conversation.py",
    storage: "Memory only. A conversation ends on End conversation, sign-out or 5 idle minutes, after at most 16 turns. Nothing is written to disk.",
    term: "Pipecat: an open-source framework for voice agent pipelines. Here it orders the stages of one turn.",
  },
  asr: {
    step: 4, where: "GPU", title: "NVIDIA speech recognition", stage: "asr",
    text: "The recording goes to a NeMo speech service on the GPU and comes back as text. A typed message skips this stage.",
    code: "models/nvidia.py (transcribe) · speech_service.py",
    route: "POST /asr on loopback port 8082",
    storage: "The audio exists for this turn only.",
    term: "ASR: automatic speech recognition, speech in and text out.",
  },
  llm: {
    step: 5, where: "GPU", title: "Nemotron through vLLM", stage: "model",
    text: "The model reads the conversation, the cards on screen and the store's clock, then answers or asks for one of four catalog tools. Each message gets at most five decisions and four tool calls, with greedy decoding. Decisions are generated from the current request and tool results; they are not a prerecorded script, and GPU runs can still vary.",
    code: "models/nvidia.py (decide) · prompts.py · models/tools.py",
    route: "POST /v1/chat/completions on loopback port 8081",
    storage: "Model weights remain loaded on the GPU. Conversation state belongs to the gateway, which sends bounded history with each decision.",
    term: "vLLM: an open-source inference server with an OpenAI-compatible API, serving Nemotron here.",
  },
  tools: {
    step: 6, where: "CPU", title: "Catalog tools",
    text: "search_products, get_product_details, check_availability and add_to_cart run on the CPU against this retailer's catalog only. The tenant and store come from the session, never from the model, and a refused call is traced as denied.",
    code: "catalog.py (execute_tool) · catalog_repository.py",
    storage: "The catalog is versioned JSON and JPEG photos inside the package, read only, with synthetic stock. The cart lives in session memory only and is not a reservation.",
    term: "Tool call: the model asks the gateway to run a named function, and the gateway checks the arguments first.",
    note: "When the model skips the exact stock check, the gateway runs it itself and the trace says so.",
  },
  result: {
    step: 7, where: "CPU", title: "Validated result",
    text: "The gateway builds the cards, the stock line and the cart from catalog ids, never from model text. A stock sentence is spoken only when it matches a check of the exact product, variant and store in this turn.",
    code: "pipeline.py (display_after, finish) · stock.py · cart.py",
    storage: "Committed to session memory only after a complete turn. A failed turn changes nothing.",
    term: "Display revision: a counter for the cards on screen, so \"the second one\" means the cards the shopper sees.",
  },
  tts: {
    step: 8, where: "GPU", title: "Magpie speech synthesis", stage: "tts",
    text: "Long search replies use a short catalog summary for speech while the full answer stays on screen. Prices and counts are written as words before Magpie returns a WAV file.",
    code: "speech_text.py · models/nvidia.py (synthesize) · speech_service.py",
    route: "POST /tts on loopback port 8082",
    storage: "The reply audio stays in gateway memory until the session ends.",
    term: "TTS: text to speech.",
  },
  delivery: {
    step: 9, where: "CPU", title: "Result and audio delivery",
    text: "The gateway streams the turn's events to the browser as they happen, one JSON object per line. When the turn ends, the same authenticated response carries the result and audio. Older clients can fetch both separately.",
    code: "api.py · web/transport.js",
    route: "GET /v1/sessions/{id}/turns/{turn}/result · GET …/audio",
    storage: "Events last for this turn only. Results and audio last until the session ends.",
    term: "NDJSON: newline-delimited JSON, one event per line over one HTTP response.",
  },
  playback: {
    step: 10, where: "Browser", title: "Browser playback",
    text: "The page shows the cards and the reply text, then plays the WAV. In conversation mode the microphone opens again when the reply ends; a reply cannot be interrupted yet.",
    code: "web/app.js · web/conversation.js (Player)",
    storage: "The reply's object URL is released when the next reply loads, on End and on sign-out.",
    term: "Barge-in: speaking over the assistant to interrupt it. Not built.",
  },
};

const TOOL_EVENTS = new Set(["tool.completed", "tool.denied", "tool.allowed"]);

// The component an observed gateway event belongs to.
export function componentOf(event) {
  if (!event) return null;
  switch (event.event_type) {
    case "turn.started":
    case "turn.waiting":
      return "gateway";
    case "context.trimmed":
      return "orchestrator";
    case "asr.completed":
      return "asr";
    case "model.completed":
      return "llm";
    case "tool.completed":
    case "tool.denied":
    case "tool.allowed":
      return "tools";
    case "preview.started":
    case "preview.completed":
      return "orchestrator";
    case "tts.completed":
      return "tts";
    case "turn.completed":
    case "turn.failed":
      return "result";
    default:
      return null;
  }
}

export function ms(value) {
  if (typeof value !== "number") return "";
  if (value < 0.1) return "<0.1 ms";
  return value < 10 ? `${value.toFixed(1)} ms` : `${Math.round(value).toLocaleString("en-US")} ms`;
}

const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

// Each component's observed state for the latest turn. `state` is "done", "skipped", "failed",
// "denied", "waiting" or "missing" (not observed); a component with nothing observed yet has no
// entry. `current` is the component of the latest observation. `browser.marks` holds the
// browser's own observations (result, audio, playing, ended) and `browser.playback` why a reply did not
// play.
export function flowState(events, browser = {}) {
  const nodes = {};
  const started = events.find(e => e.event_type === "turn.started");
  if (!started) return { current: null, nodes };
  const of = type => events.filter(e => e.event_type === type);
  const terminal = events.find(e => e.event_type === "turn.completed" || e.event_type === "turn.failed");
  const completed = !!terminal && terminal.event_type === "turn.completed";
  const source = started.input_source;
  const marks = browser.marks || {};

  nodes.mic = source === "audio"
    ? { state: "done", text: "✓ recorded" }
    : { state: "skipped", text: source === "text" ? "○ typed message" : "○ diagnostic turn" };

  const waits = of("turn.waiting");
  const waited = waits.find(e => e.status !== "waiting");
  if (waits.length && !waited && !terminal) nodes.gateway = { state: "waiting", text: "waiting for the GPU" };
  else nodes.gateway = { state: "done", text: waited ? `✓ waited ${ms(waited.queued_ms)}` : "✓ admitted" };

  if (terminal) {
    nodes.orchestrator = completed
      ? { state: "done", text: typeof terminal.duration_ms === "number" ? `✓ ${ms(terminal.duration_ms)} total` : "✓ turn complete" }
      : { state: "failed", text: `× ${terminal.reason || "failed"}` };
  }

  const asr = of("asr.completed")[0];
  if (asr) nodes.asr = { state: "done", text: `✓ ${ms(asr.duration_ms)}` };
  else if (source === "text") nodes.asr = { state: "skipped", text: "○ typed message" };
  else if (terminal) nodes.asr = { state: "missing", text: "? not observed" };

  const decisions = of("model.completed");
  if (decisions.length) {
    const total = decisions.reduce((sum, e) => sum + (e.duration_ms || 0), 0);
    nodes.llm = { state: "done", text: `✓ ${plural(decisions.length, "decision")} · ${ms(total)}` };
  } else if (completed && events.some(e => e.tool_call_id?.startsWith("gateway-") || e.event_type === "preview.started")) {
    nodes.llm = { state: "skipped", text: "○ gateway handled the request" };
  } else if (terminal) {
    nodes.llm = { state: "missing", text: "? not observed" };
  }

  const tools = events.filter(e => TOOL_EVENTS.has(e.event_type));
  if (tools.length) {
    const denied = tools.filter(e => e.event_type === "tool.denied").length;
    const total = tools.reduce((sum, e) => sum + (e.duration_ms || 0), 0);
    nodes.tools = denied
      ? { state: "denied", text: `× ${denied} of ${plural(tools.length, "call")} refused` }
      : { state: "done", text: `✓ ${plural(tools.length, "call")}${total ? ` · ${ms(total)}` : ""}` };
  } else if (completed && decisions.length && decisions.every(e => e.decision !== "tool_call")) {
    nodes.tools = { state: "skipped", text: "○ no lookup needed" };
  } else if (terminal) {
    nodes.tools = { state: "missing", text: "? not observed" };
  }

  if (terminal) {
    nodes.result = completed
      ? { state: "done", text: "✓ validated" }
      : { state: "failed", text: `× ${terminal.reason || "failed"}` };
  }

  const tts = of("tts.completed")[0];
  if (tts) nodes.tts = { state: "done", text: `✓ ${ms(tts.duration_ms)}` };
  else if (terminal) nodes.tts = { state: "missing", text: "? not observed" };

  if (marks.result != null) {
    nodes.delivery = { state: "done", text: marks.audio != null ? "✓ result and audio" : "✓ result" };
  }
  if (marks.ended != null) nodes.playback = { state: "done", text: "✓ played" };
  else if (marks.playing != null) nodes.playback = { state: "done", text: "✓ playing" };
  else if (browser.playback) nodes.playback = { state: "missing", text: `? ${browser.playback}` };

  // Browser observations come after the gateway's last event, so they win when present.
  const current = marks.playing != null ? "playback" : marks.result != null ? "delivery" : componentOf(events.at(-1));
  return { current, nodes };
}

const STATES = ["done", "skipped", "failed", "denied", "waiting", "missing"];
const SPOKEN = {
  done: "completed", skipped: "skipped", failed: "failed", denied: "refused", waiting: "waiting", missing: "not observed",
};

// Marks on the diagram: a class per state, the short status line, and the accessible name.
export function renderArchitecture(parts, flow) {
  for (const id of NODES) {
    const node = parts.node(id);
    const mark = flow.nodes[id];
    for (const state of STATES) node.classList.toggle(state, !!mark && mark.state === state);
    node.classList.toggle("current", flow.current === id);
    parts.status(id).textContent = mark ? mark.text : "";
    const detail = DETAILS[id];
    const heard = mark ? ` ${SPOKEN[mark.state]}: ${mark.text.replace(/^[✓○?×]\s*/, "")}.` : "";
    const latest = flow.current === id ? " Latest event." : "";
    node.setAttribute("aria-label", `Step ${detail.step}, ${detail.title}.${heard}${latest} Show details.`);
  }
}

function model(info, key) {
  const stage = info && info.stages && info.stages[key];
  if (!stage) return "unavailable";
  const label = `${stage.model_id} @ ${String(stage.model_revision).slice(0, 12)}`;
  return info.mode === "mock" ? `${label} (mock fixture, no model runs)` : `${label} (configured profile)`;
}

// The details card for one component, with this turn's observed mark when there is one.
export function renderDetail(parts, id, info, flow) {
  const detail = DETAILS[id];
  if (!detail) return;
  parts.kicker.textContent = `STEP ${detail.step} · ${detail.where.toUpperCase()}`;
  parts.title.textContent = detail.title;
  parts.text.textContent = detail.text;
  const facts = [["Code", detail.code]];
  if (detail.route) facts.push(["Route", detail.route]);
  if (detail.stage) facts.push(["Model", model(info, detail.stage)]);
  if (id === "llm" && info && info.context_tokens) facts.push(["Context", `${info.context_tokens.toLocaleString("en-US")} tokens per prompt`]);
  facts.push(["Data kept", detail.storage]);
  if (detail.note) facts.push(["Note", detail.note]);
  facts.push(["Term", detail.term]);
  const mark = flow && flow.nodes[id];
  facts.push(["This turn", mark ? `${SPOKEN[mark.state]}: ${mark.text.replace(/^[✓○?×]\s*/, "")}` : "nothing observed yet"]);
  parts.facts.replaceChildren(...facts.flatMap(([term, value]) => [el("dt", "", term), el("dd", "", value)]));
}
