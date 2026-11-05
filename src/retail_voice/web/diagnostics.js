// Presenter and developer views of the latest turn: the timings table, the turn's labels and the
// raw redacted trace. Gateway rows come from the events the gateway emitted, on the gateway's
// clock. Browser rows come from the browser's own clock. Nothing is estimated or simulated, and
// browser and gateway durations are never subtracted from each other.
import { el } from "./conversation.js";
import { ms } from "./architecture.js";

const TOOL_LABELS = {
  search_products: "search products",
  get_product_details: "product details",
  check_availability: "check stock",
  add_to_cart: "add to cart",
  lookup_inventory: "inventory lookup",
  unknown: "unknown tool",
};

export function toolLabel(name) {
  return TOOL_LABELS[name] || "tool";
}

// The tool_call_id the gateway uses for its own exact stock check.
export const GATEWAY_STOCK = "gateway-stock";

function stageModel(event) {
  return event.model_id ? `${event.model_id} @ ${String(event.model_revision).slice(0, 12)}` : "";
}

function tokens(event) {
  if (typeof event.prompt_tokens !== "number") return "";
  return `${event.prompt_tokens.toLocaleString("en-US")} prompt, ${(event.completion_tokens || 0).toLocaleString("en-US")} reply tokens`;
}

const DECISIONS = { tool_call: "call a tool", answer: "answer", malformed: "malformed reply" };

// One row: what ran and where, its status and its measured duration.
function row(stage, where, status, duration, detail = "", total = false) {
  return { stage, where, status, duration, detail, total };
}

const seconds = value => (typeof value === "number" ? `${(value / 1000).toFixed(1)} s` : "");
const kilobytes = bytes => (typeof bytes === "number" ? `${Math.round(bytes / 1024).toLocaleString("en-US")} KB` : "");

// The latest turn's browser-side durations, each between two of the browser's own marks on
// its monotonic clock. None of them is a network round trip, and none is ever compared with or
// subtracted from a gateway duration. `capture` holds the recording's own numbers: its length
// and the quiet at its end, both measured in recorded audio, and when it ended.
export function browserDurations(browser = {}) {
  const marks = browser.marks || {};
  const capture = browser.capture;
  const span = (from, to) => (typeof from === "number" && typeof to === "number" ? Math.max(0, to - from) : undefined);
  const found = {
    recorded_audio_ms: capture ? capture.audioMs : undefined,
    trailing_quiet_ms: capture ? capture.quietMs : undefined,
    capture_end_to_request_ms: capture ? span(capture.end, marks.request) : undefined,
    request_to_first_event_ms: span(marks.request, marks.firstByte),
    request_to_turn_end_ms: span(marks.request, marks.terminal),
    turn_end_to_result_ms: span(marks.terminal, marks.result),
    turn_end_to_audio_ms: span(marks.terminal, marks.audio),
    audio_to_ready_ms: span(marks.audio, marks.decoded),
    request_to_playing_ms: span(marks.request, marks.playing),
    capture_end_to_playing_ms: capture ? span(capture.end, marks.playing) : undefined,
    reply_playing_ms: span(marks.playing, marks.ended),
  };
  return Object.fromEntries(Object.entries(found).filter(([, value]) => typeof value === "number"));
}

// Browser rows, labeled as browser waits between two observed moments.
export function browserRows(browser = {}) {
  const found = browserDurations(browser);
  const capture = browser.capture;
  const rows = [];
  const add = (key, stage, format, detail = "") => {
    if (key in found) rows.push(row(stage, "browser", "completed", format(found[key]), detail));
  };
  add("recorded_audio_ms", "Recorded audio", seconds, "length of the recording that was sent");
  add("trailing_quiet_ms", "Quiet at the end of the recording", ms,
    capture && capture.auto ? "the silence gate's wait after the last sound above the speech level" : "from the last sound above the speech level to the end");
  add("capture_end_to_request_ms", "Browser wait from capture end to request start", ms, "encode the WAV, then send it");
  add("request_to_first_event_ms", "Browser wait from request start to first event", ms);
  add("request_to_turn_end_ms", "Browser wait from request start to turn end", ms);
  add("turn_end_to_result_ms", "Browser wait from turn end to result received", ms);
  add("turn_end_to_audio_ms", "Browser wait from turn end to audio received", ms,
    [kilobytes(browser.audioBytes) && `${kilobytes(browser.audioBytes)} WAV`, browser.audioFetch && `fetched ${browser.audioFetch}`].filter(Boolean).join(" · "));
  add("audio_to_ready_ms", "Browser wait from audio received to ready to play", ms);
  add("reply_playing_ms", "Reply playing", seconds, "from playback start to the end of the reply audio");
  const key = capture ? "capture_end_to_playing_ms" : "request_to_playing_ms";
  if (key in found) {
    rows.push(row(`Browser wait from ${capture ? "capture end" : "request start"} to playback start`, "browser", "completed", ms(found[key]),
      capture ? "from the end of the recording until the reply starts playing" : "from sending the typed message until the reply starts playing", true));
  } else if (browser.playback) {
    rows.push(row("Playback", "browser", browser.playback, ""));
  }
  return rows;
}

// The latest turn's stages in order. While the turn runs only observed stages appear; once it
// ends, a stage with no event reads "not observed", or "skipped" with the reason when the turn did
// not need it.
export function timingRows(events, browser = {}) {
  const rows = [];
  const started = events.find(e => e.event_type === "turn.started");
  if (!started) return rows;
  const terminal = events.find(e => e.event_type === "turn.completed" || e.event_type === "turn.failed");
  const completed = !!terminal && terminal.event_type === "turn.completed";
  const source = started.input_source;

  rows.push(source === "audio"
    ? row("Microphone capture", "browser", "completed", "")
    : row("Microphone capture", "browser", source === "text" ? "skipped: typed message" : "skipped: diagnostic turn", ""));

  const waits = events.filter(e => e.event_type === "turn.waiting");
  const waited = waits.find(e => e.status !== "waiting");
  if (waited) rows.push(row("Queue wait for the GPU", "gateway", "completed", ms(waited.queued_ms), "another turn held the one execution slot"));
  else if (waits.length) rows.push(row("Queue wait for the GPU", "gateway", terminal ? "not observed" : "waiting", ""));
  else rows.push(row("Queue wait for the GPU", "gateway", "skipped: no turn was ahead", ""));

  const asr = events.find(e => e.event_type === "asr.completed");
  if (asr) rows.push(row("Speech recognition", "GPU", "completed", ms(asr.duration_ms), stageModel(asr)));
  else if (source === "text") rows.push(row("Speech recognition", "GPU", "skipped: typed message", ""));
  else if (terminal) rows.push(row("Speech recognition", "GPU", "not observed", ""));

  let decisions = 0;
  let lookups = 0;
  for (const event of events) {
    switch (event.event_type) {
      case "model.completed": {
        decisions += 1;
        const kind = DECISIONS[event.decision] || "step";
        const status = event.decision === "malformed" ? "withheld: tool markup, retried once" : "completed";
        const detail = [stageModel(event), tokens(event)].filter(Boolean).join(" · ");
        rows.push(row(`Model decision ${decisions}: ${kind}`, "GPU", status, ms(event.duration_ms), detail));
        break;
      }
      case "tool.completed":
      case "tool.allowed": {
        lookups += 1;
        const gateway = event.tool_call_id === GATEWAY_STOCK;
        const name = event.event_type === "tool.allowed" ? "inventory lookup" : toolLabel(event.tool_name);
        const who = gateway ? "the gateway's own exact stock check"
          : event.tool_call_id === "gateway-search" ? "the gateway refreshed the shopper's budget"
          : event.tool_call_id === "gateway-details" ? "the gateway checked the requested catalog options"
          : event.tool_call_id === "gateway-cart" ? "the gateway handled the shopper's cart request"
          : "requested by the model";
        const count = typeof event.result_count === "number" ? `${event.result_count} result${event.result_count === 1 ? "" : "s"}` : "";
        const catalog = event.catalog_version ? `catalog ${event.catalog_version}` : "";
        rows.push(row(`Catalog tool: ${name}`, "CPU", "completed", typeof event.duration_ms === "number" ? ms(event.duration_ms) : "untimed",
          [who, count, catalog, `scoped to ${event.tenant}`].filter(Boolean).join(" · ")));
        break;
      }
      case "tool.denied":
        lookups += 1;
        rows.push(row(`Catalog refused: ${toolLabel(event.tool_name)}`, "CPU", `denied: ${event.reason || "denied"}`,
          typeof event.duration_ms === "number" ? ms(event.duration_ms) : "untimed", `requested by the model · scoped to ${event.tenant}`));
        break;
      case "context.trimmed":
        rows.push(row("Memory trimmed", "CPU", "completed", "", `dropped ${event.dropped_exchanges}, kept ${event.kept_exchanges} exchanges`));
        break;
      case "preview.completed":
        rows.push(row("Clothing preview", event.preview_kind === "live" && !event.cache_hit ? "GPU" : "CPU",
          event.result_count ? "completed" : "unavailable", ms(event.duration_ms),
          event.cache_hit ? "reused this session's image after a fresh stock check" : event.preview_kind === "live" ? "local image worker" : "prepared image, no generation in this turn"));
        break;
      default:
        break;
    }
  }
  if (terminal && !decisions) rows.push(row("Model decision", "GPU", events.some(e => e.event_type === "preview.started") ? "skipped: direct preview request" : events.some(e => e.tool_call_id?.startsWith("gateway-")) ? "skipped: gateway handled the request" : "not observed", ""));
  if (terminal && !lookups) {
    const answered = decisions && events.every(e => e.event_type !== "model.completed" || e.decision !== "tool_call");
    rows.push(row("Catalog lookup", "CPU", completed && answered ? "skipped: no catalog lookup was needed" : "not observed", ""));
  }

  const tts = events.find(e => e.event_type === "tts.completed");
  if (tts) rows.push(row("Speech synthesis", "GPU", "completed", ms(tts.duration_ms), stageModel(tts)));
  else if (terminal) rows.push(row("Speech synthesis", "GPU", "not observed", ""));

  if (terminal) {
    rows.push(row("Gateway turn total", "gateway", completed ? "completed" : `failed: ${terminal.reason || "unknown"}`,
      typeof terminal.duration_ms === "number" ? ms(terminal.duration_ms) : "", "turn start to result, queue included, upload not included"));
  }
  return rows.concat(browserRows(browser));
}

const STATUS_CLASS = [
  [/^completed/, "ok"], [/^(denied|failed|withheld)/, "bad"], [/^skipped/, "skip"], [/^waiting/, "wait"],
  [/^(not observed|not played|no reply audio)/, "missing"],
];

function statusClass(status) {
  const found = STATUS_CLASS.find(([pattern]) => pattern.test(status));
  return found ? found[1] : "";
}

function tableRow(item) {
  const tr = el("tr", [statusClass(item.status), item.total ? "total" : ""].filter(Boolean).join(" "));
  const stage = el("th");
  stage.setAttribute("scope", "row");
  stage.append(el("span", "stage", item.stage), el("span", `where where-${item.where.toLowerCase()}`, item.where));
  if (item.detail) stage.append(el("span", "detail", item.detail));
  tr.append(stage, el("td", "status", item.status), el("td", "duration", item.duration || ""));
  return tr;
}

// `parts`: the table body, the meta list, the trace badge and the note. `signedIn` picks the empty
// text: the diagram works before sign-in, a live turn needs a signed-in conversation.
export function renderHow(parts, events, info, browser = {}, signedIn = false) {
  const rows = timingRows(events, browser);
  parts.steps.replaceChildren(...rows.map(tableRow));
  parts.table.hidden = !rows.length;
  parts.tableNote.hidden = !rows.length;
  const first = events[0] || {};
  const started = events.find(e => e.event_type === "turn.started");
  const terminal = events.find(e => e.event_type === "turn.completed" || e.event_type === "turn.failed");
  if (!started) {
    parts.note.textContent = signedIn ? "Send a message to see a live turn." : "Sign in and send a message to see a live turn.";
  } else {
    const kind = { audio: "voice message", text: "typed message" }[started.input_source] || "diagnostic turn";
    const state = !terminal ? "running" : terminal.event_type === "turn.completed" ? "complete" : "failed";
    parts.note.textContent = `Latest turn: ${kind}, ${state}.`;
  }
  const pairs = info ? [
    ["Mode", info.mode === "mock" ? "local mock, fixture output" : "NVIDIA, single site"],
    ["Site", `${first.site_id || info.site} · ${first.region || info.region}`],
    // A turn without a lookup read no catalog: say so instead of "unavailable".
    ["Catalog", (events.find(e => e.catalog_version) || {}).catalog_version || (started ? "not read in this turn" : "no turn yet")],
    ["Profile", String(first.profile_digest || info.profile_digest || "unavailable").slice(0, 16)],
    ["Pipeline", first.pipeline || info.pipeline || "unavailable"],
  ] : [];
  parts.meta.replaceChildren(...pairs.flatMap(([term, value]) => [el("dt", "", term), el("dd", "", value)]));
  parts.trace.textContent = first.trace_id ? `TRACE ${String(first.trace_id).slice(0, 12)}` : "NO TURN YET";
}

// Events carry no tokens, media or transcripts, so the raw view can show them as received.
export function renderRaw(pre, events) {
  pre.textContent = JSON.stringify(events, null, 2);
}
