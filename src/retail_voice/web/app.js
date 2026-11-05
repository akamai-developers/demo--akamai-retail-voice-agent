// Storefront controller and entry module. The retailer key and session token live in this
// module's memory only: never in storage, URLs or page text. At load time it touches only
// `document` and `fetch`; microphone and audio APIs are used after a click.
import { startCapture, MAX_SECONDS } from "./capture.js";
import {
  LevelMeter, Player, addEntry, byId, namedIds, positional, renderCards, renderCart, renderFocus, stockSentence,
} from "./conversation.js";
import { createArchitectureFlow } from "./architecture-flow.js";
import { NODES, flowState, renderArchitecture, renderDetail } from "./architecture.js";
import { browserDurations, renderHow, renderRaw, toolLabel } from "./diagnostics.js";
import { blob, newAttempt, request, sendAttempt } from "./transport.js";
import { renderWalkthrough } from "./walkthrough.js";

const READY = "Ready. Talk or type.";
const MUTED = "Muted. Unmute or type.";
const ENDED = "Conversation ended. Start again or type.";
const WAITING_FOR_SPEECH = "Listening.";
const NOTHING_HEARD = "Nothing heard. Try again or type.";

const REASONS = {
  deadline: "The turn ran out of time. Try again.",
  cancelled: "The turn was cancelled.",
  backend_failed: "A model step failed. Try again.",
  context_limit: "Message too long for the model's memory. Shorten it or end the conversation.",
  tool_budget_exhausted: "Catalog lookups ran out. Rephrase and try again.",
  model_rejected_request: "Message not accepted. Try again, shorter.",
  catalog_scope_denied: "The catalog refused a product outside this store.",
};
const CONFLICTS = {
  tenant_session_active: "Another conversation for this store is still open. It frees up when that shopper signs out, or after five idle minutes.",
  capacity_full: "Both conversation slots are in use. Try again in a moment.",
  draining: "This site is not taking new conversations right now.",
  queue_full: "The GPU queue is full. Retry the same message in a moment.",
  turn_in_progress: "Your previous message is still running.",
  turn_limit_start_new_session: "This conversation used its 16 turns. Press End conversation to start a new one.",
  idempotency_payload_mismatch: "That retry did not match the original message. Send it again.",
  reservation_expired_use_new_key: "The reservation expired. Send again to start a new conversation.",
  backend_unready_or_quarantined: "The model services are not ready. An operator needs to check the site.",
};

const state = {
  info: null,
  credential: null,
  storefront: null,
  session: null,
  setupKey: null,
  attempt: null,
  attemptText: null,
  busy: false,
  capture: null,
  // Conversation mode: after each complete reply the microphone opens again, unless muted.
  conversation: false,
  micMuted: false,
  view: "storefront",
  results: [],
  displayRevision: 0,
  selected: null,
  // The variant the gateway committed with its selection (a verified check or a cart line). The
  // focus photo keeps it on later turns without a stock check (N1).
  selectedVariant: null,
  // The focus panel shows the selected product while open. It is a view, not a selection.
  focusOpen: false,
  // Verified stock facts from the latest complete result only.
  stockChecks: [],
  // The session cart, as the gateway committed it.
  cart: [],
  turnsUsed: 0,
  audioEnabled: false,
  events: [],
  // The browser's own observations of the latest turn: when the result, the audio and
  // playback arrived, on the page's clock, and why a reply did not play.
  browser: { marks: {}, playback: null },
  // The How it works view: the selected tab and the component whose details are shown.
  howTab: "arch",
  detail: null,
  images: new Map(),
  details: new Map(),
};
const architectureFlow = createArchitectureFlow(byId("request-stage"));
const player = new Player(byId("audio"));
let previewUrl = null;
let previewVersion = 0;
function clearPreview() {
  previewVersion += 1;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  byId("tryon").hidden = true;
  byId("tryon-photo").replaceChildren();
}

async function showPreview(preview, scope) {
  clearPreview();
  const version = previewVersion;
  if (!preview) return;
  byId("tryon").hidden = false;
  byId("tryon-label").textContent = `${preview.label} · ${preview.color}`;
  byId("tryon-note").textContent = "Loading preview.";
  try {
    const data = await blob(preview.image_path, state.session.session_token, signalFor(scope));
    if (!current(scope) || version !== previewVersion) return;
    previewUrl = URL.createObjectURL(data);
    const img = document.createElement("img");
    img.src = previewUrl;
    img.alt = `AI clothing preview in ${preview.color}. ${preview.note}`;
    byId("tryon-photo").replaceChildren(img);
    byId("tryon-note").textContent = preview.note;
  } catch {
    if (current(scope) && version === previewVersion) byId("tryon-note").textContent = "The preview could not be loaded. Try again.";
  }
}
const meter = new LevelMeter(byId("mic-level"), byId("mic-level-fill"));
const cartParts = {
  section: byId("cart"), badge: byId("cart-count"), items: byId("cart-items"), total: byId("cart-total"),
};
const focusParts = {
  label: byId("focus-label"), name: byId("focus-name"), price: byId("focus-price"),
  description: byId("focus-description"), colors: byId("focus-colors"), sizes: byId("focus-sizes"),
  tags: byId("focus-tags"), photo: byId("focus-photo"),
};
const howParts = {
  steps: byId("how-steps"), meta: byId("how-meta"), trace: byId("how-trace"), note: byId("how-live-note"),
  table: byId("how-timings"), tableNote: byId("how-timings-note"),
};
const archParts = { node: id => byId(`arch-node-${id}`), status: id => byId(`arch-st-${id}`) };
const detailParts = {
  kicker: byId("arch-detail-kicker"), title: byId("arch-detail-title"), text: byId("arch-detail-text"),
  facts: byId("arch-detail-facts"),
};
const walkParts = {
  status: byId("walkthrough-status"), player: byId("walkthrough-player"), video: byId("walkthrough-video"),
  track: byId("walkthrough-track"), meta: byId("walkthrough-meta"), chapters: byId("walkthrough-chapters"),
  heading: byId("walkthrough-chapters-heading"), transcript: byId("walkthrough-transcript"),
};
// Scrolling is instant for people who asked for reduced motion.
const still = () => !!globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
// The browser's monotonic clock for its own observations. Never compared with gateway times.
const clock = () => (globalThis.performance ? globalThis.performance.now() : Date.now());
const isMock = () => state.info && state.info.mode === "mock";
let waitTimer = null;
// Every async operation belongs to one generation: a sign-in, or one conversation within it.
// Sign-out and End conversation start a new one, so late callbacks from the old one do nothing.
let generation = 0;
let lifetime = { generation, controller: new AbortController() };
const current = scope => scope.generation === generation;
const signalFor = scope => scope.controller.signal;

async function releaseSession(session) {
  if (!session) return;
  try {
    await request(`/v1/sessions/${session.session_id}`, {
      method: "DELETE", token: session.session_token, keepalive: true,
    });
  } catch {
    // The server also expires unused sessions on its own.
  }
}

function stopWaiting() {
  if (waitTimer) clearInterval(waitTimer);
  waitTimer = null;
}

// Abort the current generation's requests and media, and start a new one.
function invalidate() {
  clearPreview();
  const obsolete = lifetime;
  lifetime = { generation: ++generation, controller: new AbortController() };
  obsolete.controller.abort();
  stopWaiting();
  meter.close();
  byId("audio").onended = null;
  byId("audio").oncanplay = null;
  player.clear();
}

function setPhase(kind, text) {
  const line = byId("state-line");
  line.textContent = text;
  line.classList.toggle("busy", ["listening", "waiting", "thinking", "speaking"].includes(kind));
  line.classList.toggle("waiting", kind === "waiting");
  line.classList.toggle("error", kind === "error");
}

function signinStatus(text, error = false) {
  byId("signin-status").textContent = text;
  byId("signin-status").classList.toggle("error", error);
}

function controls() {
  const signedIn = !!state.credential;
  const listening = !!state.capture;
  byId("signin-button").disabled = !state.info || state.busy;
  byId("key").disabled = state.busy;
  byId("talk").disabled = !signedIn || (state.busy && !listening);
  byId("talk").textContent = listening ? "Stop" : state.conversation ? "Talk" : "Start conversation";
  byId("talk").classList.toggle("listening", listening);
  byId("mute-mic").disabled = !signedIn || !state.conversation;
  byId("mute-mic").textContent = state.micMuted ? "Unmute microphone" : "Mute microphone";
  byId("mute-mic").setAttribute("aria-pressed", String(state.micMuted));
  byId("end").disabled = !signedIn || !(state.conversation || state.session);
  // Typing works while the microphone listens; sending discards that recording.
  byId("send").disabled = !signedIn || state.busy;
  byId("text").disabled = !signedIn || state.busy;
  byId("retry").hidden = !state.attempt || state.busy;
  byId("stop-audio").disabled = !player.url;
  byId("fixture").disabled = state.busy || listening;
  byId("upload").disabled = state.busy || listening;
  byId("tryon-button").disabled = state.busy || listening;
}

function turnsLeft() {
  if (!state.session) return;
  const max = state.session.max_turns;
  byId("turns-left").textContent = `${Math.max(0, max - state.turnsUsed)} of ${max} turns left.`;
}

async function loadImage(path) {
  if (state.images.has(path)) return state.images.get(path);
  const key = state.credential;
  const scope = lifetime;
  try {
    const url = URL.createObjectURL(await blob(path, key, signalFor(scope)));
    if (!current(scope)) {
      URL.revokeObjectURL(url);
      return null;
    }
    state.images.set(path, url);
    return url;
  } catch {
    return null;
  }
}

function shownCards() {
  return state.view === "results" ? state.results : state.storefront.products;
}

// The cards, the focus panel and the stock line always render together from state.
function showCards() {
  const results = state.view === "results";
  const cards = shownCards();
  // Focus needs a selected product among the cards on screen; otherwise the panel closes.
  const focused = state.focusOpen ? cards.find(c => c.product_id === state.selected) : null;
  state.focusOpen = !!focused;
  renderCards(byId("cards"), cards, {
    selected: state.selected,
    numbered: results,
    onSelect: selectCard,
    loadImage,
  });
  byId("cards").classList.toggle("compact", !!focused);
  byId("products-label").textContent = results
    ? `RESULTS · REVISION ${state.displayRevision}`
    : `STOREFRONT · ${cards.length} PRODUCTS`;
  byId("browse-all").hidden = !results;
  showFocus(focused, results ? cards.indexOf(focused) + 1 : 0, cards.length);
  showStock();
  renderCart(cartParts, state.cart);
}

function showFocus(card, position, count) {
  byId("focus").hidden = !card;
  byId("tryon-button").hidden = !card || !state.info?.tryon || state.info.tryon.mode === "disabled";
  if (!card) {
    // Forget the drawn photo, so a reopened panel never shows a revoked image URL.
    focusParts.photo.replaceChildren();
    delete focusParts.photo.dataset.path;
    return;
  }
  const draw = () => renderFocus(focusParts, card, {
    label: position ? `RESULT ${position} OF ${count}` : "SELECTED",
    detail: state.details.get(card.product_id),
    check: state.stockChecks.find(c => c.product_id === card.product_id),
    variantId: state.selectedVariant,
    loadImage,
  });
  draw();
  if (!state.details.has(card.product_id)) {
    loadDetail(card.product_id).then(detail => {
      if (detail && state.focusOpen && state.selected === card.product_id) draw();
    });
  }
}

// Stock shows only from the latest result's verified checks. With the panel open, only a check
// that names the product in focus.
function showStock() {
  const line = byId("stock-line");
  const checks = state.stockChecks;
  const check = state.focusOpen ? checks.find(c => c.product_id === state.selected) : checks.at(-1);
  line.hidden = !check;
  if (!check) return;
  line.textContent = stockSentence(check);
  line.classList.toggle("out", check.quantity === 0);
}

// Tags and image text for the focus panel. Read with the retailer key; reserves nothing.
async function loadDetail(productId) {
  const scope = lifetime;
  try {
    const response = await request(`/v1/catalog/products/${encodeURIComponent(productId)}`, {
      token: state.credential, signal: signalFor(scope),
    });
    const detail = await response.json();
    if (!current(scope)) return null;
    state.details.set(productId, detail);
    return detail;
  } catch {
    return null;
  }
}

function focusCard(productId) {
  for (const item of Array.from(byId("cards").children)) {
    const button = item.children && item.children[0];
    if (button && button.getAttribute("data-product") === productId) return button.focus?.();
  }
}

function closeFocus() {
  if (!state.focusOpen) return;
  const within = byId("focus").contains?.(globalThis.document?.activeElement);
  state.focusOpen = false;
  showCards();
  // Keyboard focus returns to the product's card when it was inside the panel.
  if (within !== false) focusCard(state.selected);
}

function selectCard(card) {
  if (state.selected !== card.product_id) clearPreview();
  state.selected = card.product_id;
  state.focusOpen = true;
  showCards();
  byId("focus-name").focus?.();
  // A running turn or an open microphone owns the state line.
  if (!state.busy && !state.capture) setPhase("idle", `${card.name} selected.`);
}

function enterStore() {
  const store = state.storefront.stores.find(s => s.store_id === state.storefront.default_store_id);
  byId("retailer-name").textContent = state.storefront.retailer_name;
  byId("retailer-name").hidden = false;
  byId("site-label").textContent = `${state.info.site} · ${state.info.region}`;
  byId("site-label").hidden = false;
  byId("sign-out").hidden = false;
  byId("how-toggle").hidden = false;
  byId("store-line").textContent = `${state.storefront.retailer_name.toUpperCase()} · ${store ? store.name.toUpperCase() : ""}`;
  const pending = state.storefront.asset_review.some(s => s.startsWith("pending"));
  byId("products-note").textContent = `AI-generated photos${pending ? ", review pending" : ""}. Demo prices and stock.`;
  byId("audio-kind").textContent = isMock()
    ? "Mock mode: fixture replies and a test tone."
    : "Replies are synthesized on this GPU.";
  state.view = "storefront";
  showCards();
  renderFlow();
  setPhase("idle", READY);
  showPage(byId("how").hidden ? "shop" : "how", false);
}

async function ensureSession(scope) {
  if (state.session) return state.session;
  // Keep the key across a lost response, so a retry returns the same reservation.
  if (!state.setupKey) state.setupKey = crypto.randomUUID();
  try {
    const response = await request("/v1/sessions", {
      method: "POST",
      token: state.credential,
      signal: signalFor(scope),
      body: JSON.stringify({ capability_profile: "retail-voice-v1" }),
      headers: { "Content-Type": "application/json", "Idempotency-Key": state.setupKey },
    });
    const session = await response.json();
    if (!current(scope)) {
      void releaseSession(session);
      throw new Error("obsolete_session");
    }
    state.session = session;
    state.setupKey = null;
    state.turnsUsed = 0;
    turnsLeft();
    return state.session;
  } catch (error) {
    if (current(scope) && error.http) state.setupKey = null;
    throw error;
  }
}

function refusal(error) {
  if (!error.http) return "Connection lost. Try again to recover the same request.";
  return CONFLICTS[error.detail] || `Request refused: ${error.detail || error.http}.`;
}

// The diagram, the timings table and the details card, from observed events and the browser's
// own observations only.
function renderFlow() {
  const flow = flowState(state.events, state.browser);
  renderHow(howParts, state.events, state.info, state.browser, !!state.credential);
  renderArchitecture(archParts, flow);
  if (state.detail) renderDetail(detailParts, state.detail, state.info, flow);
}

function presentation(showing) {
  if (showing) selectTab("arch");
  byId("how").classList.toggle("presenting", showing);
  document.body?.classList.toggle("architecture-presenting", showing);
  byId("stage-toggle").setAttribute("aria-pressed", String(showing));
  byId("stage-toggle").textContent = showing ? "Exit presentation" : "Presentation view";
  byId("stage-toggle").focus?.();
}
byId("stage-toggle").onclick = () => presentation(!byId("how").classList.contains("presenting"));
document.addEventListener?.("keydown", event => {
  if (!byId("how").classList.contains("presenting")) return;
  if (event.key === "Escape") { event.preventDefault(); presentation(false); }
  if (event.key === "Tab") {
    const items = [...byId("how").querySelectorAll("button, select, [tabindex='0']")]
      .filter(node => !node.disabled && node.getClientRects().length);
    const first = items[0], last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

// A step the browser itself observed for one turn, on the browser's clock. `browser` is
// that turn's record: a late callback from an earlier attempt marks nothing.
function observe(mark, scope, bytes, browser = state.browser) {
  if (!current(scope) || state.browser !== browser) return;
  browser.marks[mark] = clock();
  if (typeof bytes === "number") browser.audioBytes = bytes;
  renderFlow();
}

function notPlayed(reason, scope) {
  if (!current(scope)) return;
  state.browser.playback = reason;
  renderFlow();
}

function onEvent(event, scope) {
  if (!current(scope)) return;
  state.events.push(event);
  renderRaw(byId("raw"), state.events);
  renderFlow();
  stopWaiting();
  // The state line changes only on an observed event. turn.started proves the gateway has the
  // whole request, so "Transcribing" never appears before the recording was received.
  const source = (state.events.find(e => e.event_type === "turn.started") || {}).input_source;
  const next = { audio: " Transcribing.", text: " Thinking." }[source] || "";
  switch (event.event_type) {
    case "turn.started":
      if (next) setPhase("thinking", `Sent.${next}`);
      break;
    case "turn.waiting":
      if (event.status === "waiting") {
        // Show the wait as it grows; the queue time also counts toward the turn deadline.
        const started = Date.now();
        const tick = () => current(scope) && setPhase("waiting", `Waiting for the GPU, ${Math.round((Date.now() - started) / 1000)} s.`);
        tick();
        waitTimer = setInterval(tick, 1000);
      } else {
        setPhase("thinking", `Running after ${Math.round(event.queued_ms / 1000)} s in the queue.${next}`);
      }
      break;
    case "asr.completed":
      setPhase("thinking", "Heard you. Thinking.");
      break;
    case "model.completed":
      setPhase("thinking", event.decision === "tool_call" ? "Checking the catalog." : "Writing the reply.");
      break;
    case "tool.completed":
      setPhase("thinking", `Catalog: ${toolLabel(event.tool_name)}, ${event.result_count} result${event.result_count === 1 ? "" : "s"}.`);
      break;
    case "tool.denied":
      // A later model.completed with another tool call switches back to "Checking the catalog."
      setPhase("thinking", "The catalog refused a lookup.");
      break;
    case "tts.completed":
      setPhase("thinking", "Reply ready.");
      break;
    case "preview.started":
      setPhase("thinking", event.cache_hit ? "Loading your previous preview." : event.preview_kind === "prepared" ? "Loading the prepared preview." : "Creating your preview.");
      break;
    case "preview.completed":
      setPhase("thinking", "Preview ready.");
      break;
    case "turn.failed":
      setPhase("error", REASONS[event.reason] || `The turn failed (${event.reason}).`);
      break;
    default:
      break;
  }
}

async function sendMessage({ text, audio, capture }, scope = lifetime) {
  if (!current(scope)) return;
  state.busy = true;
  controls();
  setPhase("thinking", "Starting the conversation.");
  try {
    await ensureSession(scope);
    if (!current(scope)) return;
  } catch (error) {
    if (!current(scope)) return;
    state.busy = false;
    controls();
    return setPhase("error", refusal(error));
  }
  const body = new FormData();
  if (audio) body.append("audio", audio, "question.wav");
  else body.append("text", text);
  body.append("display_revision", String(state.displayRevision));
  if (state.selected) body.append("selected_product_id", state.selected);
  state.attempt = newAttempt("/messages", body);
  // The recording's own numbers stay in the page; they are never sent.
  state.attempt.capture = capture || null;
  state.attemptText = audio ? null : text;
  return runAttempt(scope);
}

async function runAttempt(scope = lifetime) {
  const attempt = state.attempt;
  state.busy = true;
  state.events = [];
  // A retry measures from its own request: the recording ended before the failed attempt.
  const browser = { marks: {}, playback: null, capture: attempt.runs === 0 ? attempt.capture : null };
  state.browser = browser;
  // An earlier reply may still be playing. Only this turn's own reply can reopen the microphone.
  byId("audio").onended = null;
  byId("audio").oncanplay = null;
  controls();
  setPhase("thinking", "Sending.");
  let finished = false;
  try {
    const { result, audio } = await sendAttempt(
      state.session, attempt, event => onEvent(event, scope), signalFor(scope),
      (mark, bytes) => observe(mark, scope, bytes, browser),
    );
    if (!current(scope)) return;
    state.attempt = null;
    finished = await applyResult(result, scope, audio);
  } catch (error) {
    if (!current(scope)) return;
    if (error.http === 403) {
      // The server forgot this conversation after five idle minutes.
      state.session = null;
      state.attempt = null;
      state.displayRevision = 0;
      state.results = [];
      state.stockChecks = [];
      state.cart = [];
      state.selectedVariant = null;
      state.focusOpen = false;
      state.view = "storefront";
      clearPreview();
      showCards();
      addEntry(byId("transcript"), "NOTE", "This conversation expired. Your next message starts a new one.", "failed");
      setPhase("error", "Conversation expired. Send again.");
    } else {
      const keep = !error.http || ["queue_full", "turn_in_progress"].includes(error.detail);
      if (!keep) state.attempt = null;
      setPhase("error", error.http ? refusal(error) : "Connection lost. Retry the message.");
    }
  } finally {
    if (current(scope)) {
      stopWaiting();
      state.busy = false;
      controls();
    }
  }
  // A complete reply that is not going to play continues the conversation now.
  if (finished) replyFinished(scope);
}

// Returns true when the turn completed and its reply will not play, so the conversation can
// continue at once. A played reply continues from audio.onended instead. A failed turn, a
// refusal and expiry return false or throw, so they never reopen the microphone.
async function applyResult(result, scope, early = null) {
  state.browser.turnId = result.turn_id;
  state.browser.traceId = result.trace_id;
  renderFlow();
  const transcript = byId("transcript");
  const voice = result.input_source === "audio";
  if (voice && !result.transcript && result.status !== "complete" && result.reason === "model_rejected_request") {
    // The recording held no words (room noise opened the gate). The turn still counted, but it
    // is not a failure to shout about: one quiet note, and the microphone waits for a press.
    state.turnsUsed += 1;
    turnsLeft();
    addEntry(transcript, "NOTE", NOTHING_HEARD, "note");
    setPhase("idle", NOTHING_HEARD);
    showCards();
    notPlayed("no reply audio: nothing was heard", scope);
    return false;
  }
  addEntry(transcript, voice ? "YOU · VOICE" : "YOU · TYPED", result.transcript || state.attemptText || "(nothing heard)", "user");
  state.turnsUsed += 1;
  turnsLeft();
  if (state.attemptText !== null) byId("text").value = "";
  state.attemptText = null;
  if (Array.isArray(result.cart)) state.cart = result.cart;
  if (result.status !== "complete") {
    const reason = REASONS[result.reason] || `The turn failed (${result.reason}).`;
    addEntry(transcript, "FAILED", reason, "failed");
    setPhase("error", reason);
    showCards();
    notPlayed("no reply audio: the turn failed", scope);
    return false;
  }
  addEntry(transcript, isMock() ? "FIXTURE REPLY" : "ASSISTANT", result.answer, "assistant");
  byId("audio-kind").textContent = result.spoken_answer && result.spoken_answer !== result.answer
    ? "Brief spoken reply. Full details above."
    : isMock() ? "Mock mode: fixture replies and a test tone." : "Replies are synthesized on this GPU.";
  if (result.preview) void showPreview(result.preview, scope);
  else clearPreview();
  // The cards become results once the gateway changed the display or a search ran in this
  // turn: a search with no matches changes nothing, yet must read "no products matched". A
  // greeting or a date question as the first message leaves the storefront on screen.
  const searched = state.events.some(e => e.event_type === "tool.completed" && e.tool_name === "search_products");
  if (state.view === "results" || searched || result.display_revision !== state.displayRevision) {
    state.results = result.product_cards;
    state.view = "results";
  }
  state.displayRevision = result.display_revision;
  // The gateway moved the selection to the one shown product this reply is about.
  const followed = !!result.selected_product_id && result.selected_product_id !== state.selected;
  state.selected = result.selected_product_id;
  state.selectedVariant = result.selected_variant_id || null;
  state.stockChecks = result.stock_checks || [];
  // An explicit id for another product (or one the store does not have) must not leave the old
  // product looking like the answer.
  const named = namedIds(result.transcript);
  const card = state.results.find(c => c.product_id === state.selected);
  if (named.length && !(card && named.some(id => id === card.product_id || card.variants.some(v => v.variant_id === id)))) {
    state.focusOpen = false;
  }
  // Open for "the second one" or verified stock of the selected product, unless the reply is a
  // clarifying question, and whenever the reply moved the selection to the product it is
  // about, even when it ends with a question such as "does that work for you?". A search
  // whose reply names several products or none selects nothing, so it opens nothing.
  const explicit = positional(result.transcript) || state.stockChecks.some(c => c.product_id === state.selected);
  if (state.selected && (followed || (explicit && !result.clarification))) {
    state.focusOpen = true;
  }
  showCards();
  if (!result.audio_path) {
    notPlayed("no reply audio", scope);
    return true;
  }
  setPhase("thinking", "Reply ready.");
  try {
    // The audio fetch started with the result's when the terminal event named it (N3). The
    // result decides whether there is audio; a failed early fetch falls back to fetching now.
    let audio = early && early.path === result.audio_path ? await early.promise : null;
    if (!current(scope)) return false;
    state.browser.audioFetch = audio ? "with the result" : "after the result";
    if (!audio) {
      audio = await blob(result.audio_path, state.session.session_token, signalFor(scope));
      if (!current(scope)) return false;
      observe("audio", scope, audio.size);
    }
    player.load(audio);
    const url = player.url;
    // Playback marks belong to this turn only: a later turn replaces state.browser.
    state.browser.url = url;
    renderFlow();
    byId("audio").oncanplay = () => {
      if (current(scope) && state.browser.url === url && state.browser.marks.decoded === undefined) observe("decoded", scope);
    };
    byId("audio").onended = () => {
      if (!current(scope) || player.url !== url) return;
      // The reply ended: the browser observed that too, so the diagram's playback mark changes.
      if (state.browser.url === url) observe("ended", scope);
      replyFinished(scope);
    };
  } catch {
    if (!current(scope)) return false;
    setPhase("error", "No reply audio. The text is above.");
    return false;
  }
  if (state.audioEnabled && !player.muted) {
    await playReply(scope);
    return false;
  }
  byId("play").hidden = false;
  notPlayed(player.muted ? "not played: replies muted" : "not played yet: press Play", scope);
  return true;
}

async function playReply(scope = lifetime) {
  const url = player.url;
  try {
    const played = await player.play();
    if (!current(scope) || player.url !== url || !played) return;
    byId("play").hidden = true;
    setPhase("speaking", isMock() ? "Playing the test tone." : "Speaking.");
    if (state.browser.url === url) {
      state.browser.playback = null;
      observe("playing", scope);
    }
  } catch {
    if (!current(scope) || player.url !== url) return;
    byId("play").hidden = false;
    setPhase("idle", "Reply ready. Press Play to hear it.");
    if (state.browser.url === url) notPlayed("not played: the browser blocked autoplay", scope);
  }
  controls();
}

function turnCap() {
  return `${state.session.max_turns} turns used. End the conversation to start over.`;
}

// A complete reply finished playing, or will not play. In conversation mode the microphone
// opens again: not while muted, not past the turn cap, not while a turn runs or the microphone
// is already open. Failures, refusals, "Nothing heard" and expiry never call this.
function replyFinished(scope) {
  if (!current(scope) || state.busy || state.capture) return;
  if (state.session && state.turnsUsed >= state.session.max_turns) return setPhase("idle", turnCap());
  if (state.conversation && state.micMuted) return setPhase("idle", MUTED);
  if (state.conversation && state.session) return listen(scope);
  setPhase("idle", READY);
}

// A presenter can raise the speech level for a noisy room: /?speech_threshold=0.05.
function speechThreshold() {
  const value = Number(new URLSearchParams(globalThis.location?.search || "").get("speech_threshold"));
  return value >= 0.002 && value <= 0.2 ? value : undefined;
}

function listeningText(capture, seconds) {
  return capture.heard
    ? `Listening, ${seconds} s of ${MAX_SECONDS}. Pause to send.`
    : WAITING_FOR_SPEECH;
}

// Open the microphone, reserving the session first if needed.
async function listen(scope = lifetime) {
  if (!current(scope) || state.busy || state.capture) return;
  state.busy = true;
  controls();
  try {
    await ensureSession(scope);
    if (!current(scope)) return;
  } catch (error) {
    if (!current(scope)) return;
    // No conversation started. The button offers Start conversation again.
    state.conversation = false;
    state.busy = false;
    controls();
    return setPhase("error", refusal(error));
  }
  try {
    player.stop();
    const capture = await startCapture({
      signal: signalFor(scope),
      threshold: speechThreshold(),
      hangoverMs: (() => {
        const value = Number(new URLSearchParams(globalThis.location?.search || "").get("speech_pause_ms"));
        return value >= 400 && value <= 2000 ? value : undefined;
      })(),
      onTick: seconds => current(scope) && state.capture && setPhase("listening", listeningText(state.capture, seconds)),
      onLimit: () => current(scope) && stopAndSubmit(scope, "limit"),
      onLevel: (rms, audioMs) => current(scope) && state.capture && meter.update(rms, audioMs),
      onAuto: verdict => current(scope) && (verdict === "ended" ? stopAndSubmit(scope, "auto") : nothingHeard(scope)),
    });
    if (!current(scope)) {
      await capture.stop(true);
      return;
    }
    if (state.micMuted) {
      // Muted while the microphone was starting.
      await capture.stop(true);
      setPhase("idle", MUTED);
      return;
    }
    state.capture = capture;
    meter.open();
    setPhase("listening", WAITING_FOR_SPEECH);
  } catch {
    if (!current(scope)) return;
    // Leave conversation mode: typed turns must not retry the microphone after every reply and
    // repaint this error, and the button offers Start conversation again.
    state.conversation = false;
    setPhase("error", "Microphone blocked. Allow it, then try again or type.");
  } finally {
    if (current(scope)) {
      state.busy = false;
      controls();
    }
  }
}

async function stopAndSubmit(scope = lifetime, how = "stop") {
  if (!current(scope)) return;
  const capture = state.capture;
  if (!capture) return;
  // When the recording ended on the page's clock, its length, and the quiet at its end measured
  // in recorded audio: the silence gate's wait when it ended the turn itself.
  const ended = clock();
  const quiet = capture.lastVoiceMs === null ? undefined : Math.max(0, capture.audioMs - capture.lastVoiceMs);
  const numbers = { end: ended, audioMs: capture.audioMs, quietMs: quiet, auto: how === "auto", how };
  state.capture = null;
  meter.close();
  state.busy = true;
  controls();
  const wav = await capture.stop();
  if (!current(scope)) return;
  state.busy = false;
  controls();
  if (!wav) return setPhase("error", "Nothing was recorded. Try again.");
  return sendMessage({ audio: wav, capture: numbers }, scope);
}

// The silence gate heard no speech. Nothing is sent, and the microphone stays closed until a
// press: an empty room must not loop through recordings.
async function nothingHeard(scope) {
  const capture = state.capture;
  if (!current(scope) || !capture) return;
  state.capture = null;
  meter.close();
  controls();
  setPhase("idle", "Nothing heard. Try again.");
  await capture.stop(true);
}

byId("talk").onclick = async () => {
  const scope = lifetime;
  state.audioEnabled = true;
  if (state.capture) return stopAndSubmit(scope);
  if (state.busy) return;
  // Start conversation, or Talk again after a pause: either way the microphone is wanted.
  state.conversation = true;
  state.micMuted = false;
  return listen(scope);
};

byId("mute-mic").onclick = async () => {
  const scope = lifetime;
  if (!state.conversation) return;
  state.micMuted = !state.micMuted;
  if (state.micMuted) {
    // Muting discards what the open microphone recorded so far.
    const capture = state.capture;
    state.capture = null;
    meter.close();
    controls();
    // A running turn or a playing reply keeps its state text; the reply ends on MUTED.
    if (!state.busy && !player.playing) setPhase("idle", MUTED);
    await capture?.stop(true);
    return;
  }
  state.audioEnabled = true;
  controls();
  // A running turn or a playing reply reopens the microphone when it finishes.
  if (!state.busy && !state.capture && !player.playing) return listen(scope);
};

async function endConversation() {
  const capture = state.capture;
  const old = state.session;
  // Invalidate first: late setup, events, results, audio and onended belong to the old one.
  invalidate();
  Object.assign(state, {
    session: null, setupKey: null, attempt: null, attemptText: null, busy: false, capture: null,
    conversation: false, micMuted: false, view: "storefront", results: [], displayRevision: 0,
    selected: null, selectedVariant: null, focusOpen: false, stockChecks: [], cart: [], turnsUsed: 0,
    events: [], browser: { marks: {}, playback: null },
  });
  byId("transcript").replaceChildren();
  renderRaw(byId("raw"), []);
  renderFlow();
  byId("play").hidden = true;
  byId("turns-left").textContent = "";
  showCards();
  setPhase("idle", ENDED);
  controls();
  await Promise.allSettled([capture?.stop(true), releaseSession(old)]);
}

byId("end").onclick = () => endConversation();

byId("text-form").onsubmit = async event => {
  event.preventDefault();
  const scope = lifetime;
  state.audioEnabled = true;
  const text = byId("text").value.trim();
  if (!text || state.busy) return;
  const capture = state.capture;
  if (capture) {
    // Typing while the microphone listens replaces the spoken message.
    state.capture = null;
    meter.close();
    controls();
    await capture.stop(true);
    if (!current(scope)) return;
  }
  return sendMessage({ text }, scope);
};

byId("retry").onclick = () => {
  if (state.attempt && !state.busy) return runAttempt();
};

byId("play").onclick = () => {
  state.audioEnabled = true;
  return playReply();
};

byId("stop-audio").onclick = () => {
  const playing = player.playing;
  player.stop();
  // Stopping a reply that was playing ends it, as if it had finished.
  if (playing) replyFinished(lifetime);
};

byId("mute").onclick = () => {
  player.muted = !player.muted;
  byId("audio").muted = player.muted;
  byId("mute").setAttribute("aria-pressed", String(player.muted));
  byId("mute").textContent = player.muted ? "Unmute replies" : "Mute replies";
};

byId("browse-all").onclick = () => {
  clearPreview();
  state.view = "storefront";
  state.focusOpen = false;
  showCards();
};

byId("focus-back").onclick = () => closeFocus();
byId("tryon-close").onclick = () => clearPreview();
byId("tryon-button").onclick = () => {
  if (!state.busy && !state.capture) return sendMessage({ text: "Show this item on me." });
};

// Escape collapses the focus panel. Back to results does the same.
globalThis.document?.addEventListener?.("keydown", event => {
  if (event.key === "Escape" && state.focusOpen) closeFocus();
});

// Hash-routed views keep the same session and observed trace without showing the shop below
// the technical page. Navigating away suspends capture so narration is never sent as shopping.
function showPage(page, focus = true) {
  const how = page === "how";
  if (!how && byId("how").classList.contains("presenting")) presentation(false);
  byId("how").hidden = !how;
  byId("store").hidden = how || !state.credential;
  byId("signin").hidden = how || !!state.credential;
  byId("mode-notice").hidden = how;
  byId("how-toggle").setAttribute("aria-pressed", String(how));
  byId("how-toggle").textContent = how ? "Back to storefront" : "How it works";
  if (how) {
    if (state.conversation) state.micMuted = true;
    state.audioEnabled = false;
    player.stop();
    const capture = state.capture;
    state.capture = null;
    meter.close();
    void capture?.stop(true);
    if (state.conversation && !state.busy) setPhase("idle", MUTED);
    byId("walkthrough-video").pause?.();
  } else {
    byId("walkthrough-video").pause?.();
  }
  architectureFlow.setVisible(how && state.howTab === "arch");
  controls();
  if (focus) {
    const heading = byId(how ? "how-heading" : state.credential ? "invite" : "signin-heading");
    heading.setAttribute("tabindex", "-1");
    heading.focus?.();
    globalThis.scrollTo?.(0, 0);
  }
}

function navigate(page) {
  if (globalThis.location) globalThis.location.hash = page;
  showPage(page);
}
byId("how-toggle").onclick = () => navigate(byId("how").hidden ? "how" : "shop");
globalThis.addEventListener?.("hashchange", () => showPage(globalThis.location?.hash === "#how" ? "how" : "shop"));

// Tabs with arrow-key navigation: only the selected tab is in the Tab order.
const TABS = { arch: ["how-tab-arch", "how-panel-arch"], live: ["how-tab-live", "how-panel-live"], video: ["how-tab-video", "how-panel-video"] };

function selectTab(name, focus = false) {
  state.howTab = name;
  architectureFlow.setVisible(!byId("how").hidden && name === "arch");
  if (name !== "video") byId("walkthrough-video").pause?.();
  for (const [key, [tab, panel]] of Object.entries(TABS)) {
    const on = key === name;
    byId(tab).setAttribute("aria-selected", String(on));
    byId(tab).setAttribute("tabindex", on ? "0" : "-1");
    byId(panel).hidden = !on;
    if (on && focus) byId(tab).focus?.();
  }
}

for (const [key, [tab]] of Object.entries(TABS)) {
  byId(tab).onclick = () => selectTab(key);
  byId(tab).onkeydown = event => {
    const order = Object.keys(TABS);
    const at = order.indexOf(key);
    const next = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: order.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault?.();
    selectTab(order[(next + order.length) % order.length], true);
  };
}

// A diagram box shows its details card. Boxes are buttons: click, Enter or Space.
function showDetail(id) {
  state.detail = id;
  for (const node of NODES) archParts.node(node).setAttribute("aria-pressed", String(node === id));
  renderDetail(detailParts, id, state.info, flowState(state.events, state.browser));
  // The card sits below the diagram; bring it into view when it is off screen.
  byId("arch-detail").scrollIntoView?.({ behavior: still() ? "auto" : "smooth", block: "nearest" });
}

for (const id of NODES) {
  archParts.node(id).onclick = () => showDetail(id);
  archParts.node(id).onkeydown = event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault?.();
    showDetail(id);
  };
}

byId("signin-form").onsubmit = async event => {
  const scope = lifetime;
  event.preventDefault();
  const key = byId("key").value.trim();
  if (!key || state.busy) return;
  state.busy = true;
  controls();
  signinStatus("Opening the storefront.");
  try {
    const storefront = await (await request("/v1/catalog", { token: key, signal: signalFor(scope) })).json();
    if (!current(scope)) return;
    state.credential = key;
    state.storefront = storefront;
    byId("key").value = "";
    signinStatus("");
    enterStore();
  } catch (error) {
    if (!current(scope)) return;
    signinStatus(error.http === 401 ? "That key did not open a storefront." : "Gateway unreachable. Try again.", true);
  } finally {
    if (current(scope)) {
      state.busy = false;
      controls();
    }
  }
};

function reset() {
  invalidate();
  for (const url of state.images.values()) if (url) URL.revokeObjectURL(url);
  state.images.clear();
  state.details.clear();
  Object.assign(state, {
    credential: null, storefront: null, session: null, setupKey: null, attempt: null,
    attemptText: null, busy: false, capture: null, conversation: false, micMuted: false,
    view: "storefront", results: [], displayRevision: 0, selected: null, selectedVariant: null,
    focusOpen: false, stockChecks: [], cart: [], turnsUsed: 0, events: [], audioEnabled: false,
    browser: { marks: {}, playback: null },
  });
  showFocus(null);
  renderCart(cartParts, []);
  for (const id of ["transcript", "cards"]) byId(id).replaceChildren();
  renderRaw(byId("raw"), []);
  renderFlow();
  for (const id of ["key", "text", "audio-file", "image-file"]) byId(id).value = "";
  for (const id of ["store", "retailer-name", "site-label", "sign-out", "play", "stock-line", "focus"]) byId(id).hidden = true;
  byId("signin").hidden = false;
  navigate("shop");
  setPhase("idle", READY);
  byId("turns-left").textContent = "";
  byId("diag-status").textContent = "";
  signinStatus("Signed out. Keys, conversation and audio cleared.");
  controls();
}

byId("sign-out").onclick = async () => {
  const capture = state.capture;
  const old = state.session;
  // Invalidate first. Server deletion and microphone cleanup may finish after a new sign-in.
  reset();
  await Promise.allSettled([capture?.stop(true), releaseSession(old)]);
};

// Developer diagnostics: the older single-turn route, on the same session.
async function runDiagnostic(path, body, headers) {
  const scope = lifetime;
  if (state.busy || state.capture) return;
  state.busy = true;
  controls();
  const status = byId("diag-status");
  try {
    await ensureSession(scope);
    if (!current(scope)) return;
    state.events = [];
    state.browser = { marks: {}, playback: null };
    status.textContent = "Running the diagnostic turn.";
    const { result } = await sendAttempt(state.session, newAttempt(path, body, headers), event => onEvent(event, scope), signalFor(scope));
    if (!current(scope)) return;
    state.turnsUsed += 1;
    turnsLeft();
    status.textContent = result.status === "complete"
      ? `Diagnostic turn complete${isMock() ? " (fixture output)" : ""}: ${result.answer}`
      : `Diagnostic turn failed: ${result.reason}`;
    setPhase("idle", READY);
  } catch (error) {
    if (!current(scope)) return;
    status.textContent = refusal(error);
  } finally {
    if (current(scope)) {
      state.busy = false;
      controls();
    }
  }
}

byId("fixture").onclick = () => runDiagnostic(
  "/fixture-turn",
  JSON.stringify({ sku: byId("sku").value, store_id: byId("store-id").value }),
  { "Content-Type": "application/json" },
);

byId("upload").onclick = () => {
  const audio = byId("audio-file").files[0];
  const image = byId("image-file").files[0];
  if (!audio || !image) {
    byId("diag-status").textContent = "Choose a WAV file and a photo first.";
    return;
  }
  const form = new FormData();
  form.append("audio", audio, "question.wav");
  form.append("image", image);
  form.append("sku", byId("sku").value);
  form.append("store_id", byId("store-id").value);
  return runDiagnostic("/turns", form, {});
};

// Best effort: release the conversation slot when the page goes away.
globalThis.addEventListener?.("pagehide", () => {
  const old = state.session;
  if (!old) return;
  fetch(`/v1/sessions/${old.session_id}`, {
    method: "DELETE",
    keepalive: true,
    headers: { Authorization: `Bearer ${old.session_token}` },
  }).catch(() => {});
});

// A read-only view of the latest turn's browser timings for the browser check and for measuring
//: a frozen copy of the marks on the page's monotonic clock and the durations the table
// shows. Assigning to it does nothing, and nothing in the page reads it.
function timingsSnapshot() {
  const browser = state.browser;
  const started = state.events.find(e => e.event_type === "turn.started");
  return Object.freeze({
    clock: "performance.now() milliseconds on this page",
    turn_id: browser.turnId || null,
    trace_id: browser.traceId || null,
    input_source: (started && started.input_source) || null,
    marks: Object.freeze({ ...browser.marks }),
    capture: browser.capture ? Object.freeze({ ...browser.capture }) : null,
    audio_bytes: typeof browser.audioBytes === "number" ? browser.audioBytes : null,
    audio_fetch: browser.audioFetch || null,
    playback: browser.playback || null,
    durations: Object.freeze(browserDurations(browser)),
  });
}
Object.defineProperty(globalThis, "__timings", { get: timingsSnapshot, configurable: false, enumerable: false });

// The recorded walkthrough, when this gateway serves one. Its files need no key, so the tab
// works before sign-in. A failure leaves the chapter plan and says so.
async function loadWalkthrough() {
  renderWalkthrough(walkParts, null);
  if (!state.info.walkthrough) return;
  let manifest;
  try {
    manifest = await (await request("/app/walkthrough/walkthrough.json")).json();
  } catch {
    walkParts.status.textContent = "The recorded walkthrough could not be loaded from this gateway.";
    return;
  }
  let captions = "";
  try {
    captions = await (await request("/app/walkthrough/walkthrough.vtt")).text();
  } catch {
    // The video still plays; the transcript says its captions could not be read.
  }
  renderWalkthrough(walkParts, manifest, captions);
}

async function initialize() {
  try {
    const response = await fetch("/v1/info");
    if (!response.ok) throw new Error("info");
    const info = await response.json();
    if (!["mock", "nvidia"].includes(info.mode)) throw new Error("mode");
    state.info = info;
    architectureFlow.setInfo(info);
    const mock = info.mode === "mock";
    byId("mode-badge").textContent = mock ? "LOCAL · MOCK" : "NVIDIA · SINGLE SITE";
    byId("mode-notice").textContent = mock
      ? "Local mock mode: fixture replies, a test tone, no GPU."
      : `NVIDIA mode, one site: ${info.site} (${info.region}). Speech, model and synthesis run on this GPU.`;
    byId("fixture").hidden = !mock;
    byId("backend-footer").textContent = mock ? "Pipecat · mock adapters" : "Pipecat · NVIDIA ASR, Nemotron and TTS";
    // How it works reads before sign-in: the diagram and details need no retailer key.
    byId("arch-site").textContent = `SITE ${info.site} · ${info.region}`.toUpperCase();
    byId("how-toggle").hidden = false;
    renderFlow();
    void loadWalkthrough();
    signinStatus("Paste a retailer key.");
  } catch {
    signinStatus("Gateway not ready. Reload in a moment.", true);
  }
  controls();
}

showPage(globalThis.location?.hash === "#how" ? "how" : "shop", false);
initialize();
