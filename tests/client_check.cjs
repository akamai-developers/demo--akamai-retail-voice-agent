/* Runs the real browser modules (app.js and everything it imports) inside a Node vm, with a
   fake DOM and a scripted fetch that loses responses on purpose. Needs
   node --experimental-vm-modules. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = path.join(__dirname, '../src/retail_voice/web');

class ClassList {
  constructor() { this.names = new Set(); }
  toggle(name, force) {
    const on = force === undefined ? !this.names.has(name) : Boolean(force);
    if (on) this.names.add(name); else this.names.delete(name);
    return on;
  }
  contains(name) { return this.names.has(name); }
}

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.classList = new ClassList();
    this.className = '';
    this.value = '';
    this.files = [];
    this.hidden = false;
    this.disabled = false;
    this.history = [];
    this.text = '';
    this.src = '';
    this.muted = false;
    this.currentTime = 0;
    this.plays = 0;
    this.paused = true;
    this.style = {};
    this.dataset = {};
  }
  set textContent(value) { this.text = String(value); this.children = []; this.history.push(this.text); }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(' '); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'src') this.src = ''; }
  pause() { this.paused = true; }
  load() {}
  play() { this.plays += 1; this.paused = false; return Promise.resolve(); }
  scrollIntoView() {}
  focus() { document.activeElement = this; }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
}

const elements = new Map();
const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
// Document listeners are kept so the test can press Escape.
const listeners = {};
const document = {
  getElementById: byId, createElement: tag => new Element(tag), activeElement: null,
  addEventListener: (type, handler) => { listeners[type] = handler; },
};

const created = [];
const revoked = [];
const FakeURL = {
  createObjectURL() { const url = `blob:fake/${created.length}`; created.push(url); return url; },
  revokeObjectURL(url) { revoked.push(url); },
};

const card = (id, name, price) => ({
  product_id: id, name, category: 'jackets', description: 'Synthetic.', price_minor: price,
  price_display: `$${price / 100}.00`, image_path: `/v1/catalog/images/img-${id}-navy`,
  image_alt: `Product photo of the ${name}`, colors: ['navy'], sizes: ['M'],
  variants: [{ variant_id: `${id}-navy-m`, color: 'navy', size: 'M', image_path: `/v1/catalog/images/img-${id}-navy` }],
});
const storefront = {
  tenant: 'retailer-a', retailer_name: 'Northline Outfitters', catalog_version: '2026.11.1',
  currency: 'USD', inventory_provenance: 'synthetic', asset_review: ["pending review"],
  default_store_id: 'store-a', stores: [{ store_id: 'store-a', name: 'Northline Downtown' }],
  products: [card('jacket-a', 'Ridgeline Rain Shell', 12900), card('a-drizzle-anorak', 'Drizzle Anorak', 6900)],
};
const event = (sequence, type, extra = {}) => ({
  schema_version: 1, event_id: `event${sequence}`, event_type: type, sequence, status: 'complete',
  session_id: 'session1', trace_id: 'trace1', turn_id: 'turn1', mode: 'mock', tenant: 'retailer-a',
  site_id: 'local-mock', region: 'local', profile_digest: 'mock-fixture-v1', pipeline: 'pipecat', ...extra,
});
const stream = [
  event(0, 'turn.started', { status: 'running', input_source: 'text' }),
  event(1, 'turn.waiting', { status: 'waiting' }),
  event(2, 'turn.waiting', { queued_ms: 812 }),
  event(3, 'model.completed', { decision: 'tool_call', duration_ms: 3, model_id: 'mock-model', model_revision: 'fixture-v1' }),
  event(4, 'tool.completed', { tool_name: 'search_products', tool_call_id: 'mock-call-1', result_count: 1, catalog_version: '2026.11.1' }),
  event(5, 'model.completed', { decision: 'answer', duration_ms: 2, model_id: 'mock-model', model_revision: 'fixture-v1' }),
  event(6, 'tts.completed', { duration_ms: 1, model_id: 'mock-tts', model_revision: 'fixture-v1' }),
  event(7, 'turn.completed', { result_path: '/v1/sessions/session1/turns/turn1/result', input_source: 'text', queued_ms: 812 }),
];
const result = {
  turn_id: 'turn1', trace_id: 'trace1', mode: 'mock', site: 'local-mock', status: 'complete',
  answer: 'Fixture answer: I found 1 option. The first is the Drizzle Anorak at $69.00.',
  transcript: 'rain jacket', input_source: 'text', product_cards: [storefront.products[1]],
  selected_product_id: null, display_revision: 1, catalog_version: '2026.11.1', clarification: false,
  recommended_product_ids: ['a-drizzle-anorak'],
  stock_checks: [{ catalog_version: '2026.11.1', product_id: 'a-drizzle-anorak', product_name: 'Drizzle Anorak', variant_id: 'a-drizzle-anorak-navy-m', color: 'navy', size: 'M', store_id: 'store-a', store_name: 'Northline Downtown', quantity: 2, in_stock: true, provenance: 'synthetic' }],
  audio_path: '/v1/sessions/session1/turns/turn1/audio', audio_kind: 'test_tone', queued_ms: 812,
};

// A recorded walkthrough this fake gateway serves without a key.
const walkthroughManifest = {
  schema_version: 1, build: 'abc1234', recorded: '2026-11-05', captions: 'walkthrough.vtt',
  chapters: [{ start: 0, title: 'Conversation: start, speak, automatic stop, reply, reopen' }, { start: 75, title: 'Product focus' }],
};
const walkthroughVtt = "WEBVTT\n\nNOTE written from the recording script\n\n00:00.000 --> 00:04.000\nPress Start conversation once.\n\n01:15.000 --> 01:19.500\n<v Presenter>The second one comes forward.</v>\n";

const requests = [];
const lose = { setup: true, message: true, result: false };
// Tests swap these to script a voice or failed turn; null means the default text turn.
let messageStream = null;
let messageResult = null;
let sessionMaxTurns = 16;
let refuseSetup = false;
let intercept = null;
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const delayNext = predicate => {
  const gate = deferred();
  gate.seen = false;
  intercept = (url, options) => {
    if (!predicate(url, options)) return null;
    gate.seen = true;
    gate.signal = options.signal;
    intercept = null;
    return gate.promise; // Deliberately ignore abort: already-arriving callbacks still need guards.
  };
  return gate;
};
const harbor = { ...storefront, tenant: 'retailer-b', retailer_name: 'Harbor Supply',
  products: [card('jacket-b', 'Harbor Deck Jacket', 9900)] };

const fetch = async (url, options = {}) => {
  requests.push({ url, ...options });
  const held = intercept?.(url, options);
  if (held) return held;
  if (url === '/v1/info') return Response.json({
    mode: 'mock', site: 'local-mock', region: 'local', pipeline: 'pipecat', fixture_enabled: true,
    profile_digest: 'mock-fixture-v1', context_tokens: 16384,
    stages: Object.fromEntries(['asr', 'model', 'tts'].map(s => [s, { model_id: `mock-${s}`, model_revision: 'fixture-v1' }])),
    walkthrough: true,
  });
  if (url === '/app/walkthrough/walkthrough.json') return Response.json(walkthroughManifest);
  if (url === '/app/walkthrough/walkthrough.vtt') return new Response(walkthroughVtt);
  if (url === '/v1/catalog') return Response.json(options.headers.Authorization.endsWith('tenant-b-fixture') ? harbor : storefront);
  if (url.startsWith('/v1/catalog/images/')) return new Response(new Uint8Array([137, 80, 78, 71]));
  if (url.startsWith('/v1/catalog/products/')) {
    const id = decodeURIComponent(url.split('/').pop());
    const found = [...storefront.products, ...harbor.products].find(p => p.product_id === id);
    if (!found) return Response.json({ detail: 'product_not_found' }, { status: 404 });
    return Response.json({ ...found, tags: ['rain', 'hooded-jacket'], images: [{ image_path: found.image_path, alt: `Detail photo of the ${found.name}`, color: 'navy' }], catalog_version: '2026.11.1' });
  }
  if (url === '/v1/sessions') {
    if (refuseSetup) return Response.json({ detail: 'tenant_session_active' }, { status: 409 });
    if (lose.setup) { lose.setup = false; throw new Error('lost setup response'); }
    return Response.json({ session_id: 'session1', session_token: 'private-session-token', tenant: 'retailer-a', site: 'local-mock', mode: 'mock', max_turns: sessionMaxTurns });
  }
  if (url.endsWith('/messages')) {
    if (lose.message) { lose.message = false; throw new Error('lost message response'); }
    return new Response((messageStream || stream).map(e => JSON.stringify(e)).join('\n') + '\n');
  }
  if (url.endsWith('/result')) {
    if (lose.result) { lose.result = false; throw new Error('lost result response'); }
    return Response.json(messageResult || result);
  }
  if (url.endsWith('/audio')) return new Response(new Uint8Array([82, 73, 70, 70]));
  if (options.method === 'DELETE') return Response.json({ ended: true });
  return Response.json({ detail: 'unexpected' }, { status: 404 });
};

const nodes = [];
class FakeAudioContext {
  constructor() {
    this.sampleRate = 16000;
    this.state = 'running';
    this.destination = {};
    this.audioWorklet = { addModule: async () => {} };
  }
  async resume() { this.state = 'running'; }
  async close() { this.state = 'closed'; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} }; }
}
class FakeWorkletNode {
  constructor() { this.port = { onmessage: null }; nodes.push(this); }
  connect() {}
  disconnect() {}
}
// One recorded block, as the worklet posts it: samples plus their RMS level.
const feed = (rms, ms, node = nodes.at(-1)) => node.port.onmessage?.({
  data: { samples: new Float32Array(Math.round(16 * ms)).fill(rms), rms },
});
const speak = (rms, ms, step = 20) => { for (let t = 0; t < ms; t += step) feed(rms, step); };
// Each expected text appears in the list after the previous one.
const inOrder = (list, expected, label) => {
  let at = -1;
  for (const text of expected) {
    const found = list.findIndex((item, index) => index > at && (text instanceof RegExp ? text.test(item) : item === text));
    assert.ok(found > at, `${label}: missing "${text}" after position ${at} in ${JSON.stringify(list)}`);
    at = found;
  }
};

const context = vm.createContext({
  document, fetch, URL: FakeURL, URLSearchParams, FormData, Blob, Response, TextDecoder, console, AbortController, performance,
  crypto: globalThis.crypto, setTimeout, clearTimeout, setInterval, clearInterval,
});
const modules = new Map();
const load = file => {
  if (!modules.has(file)) {
    modules.set(file, new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { context, identifier: file }));
  }
  return modules.get(file);
};
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); };
const submit = form => byId(form).onsubmit({ preventDefault() {} });

(async () => {
  const entry = load(path.join(WEB, 'app.js'));
  await entry.link((specifier, referencing) => load(path.resolve(path.dirname(referencing.identifier), specifier)));
  await entry.evaluate();
  await settle();
  assert.equal(byId('mode-badge').textContent, 'LOCAL · MOCK');
  assert.equal(byId('signin-button').disabled, false);

  // The recorded walkthrough: labeled recorded with its build and date, captions on, no
  // autoplay, chapters as buttons that play from their start, a transcript from the captions.
  assert.equal(byId('walkthrough-player').hidden, false);
  assert.equal(byId('walkthrough-status').hidden, true);
  assert.equal(byId('walkthrough-meta').textContent, 'on 2026-11-05 from build abc1234');
  const walkVideo = byId('walkthrough-video');
  assert.equal(walkVideo.getAttribute('src'), '/app/walkthrough/walkthrough.mp4');
  assert.equal(walkVideo.getAttribute('autoplay'), null, 'no autoplay');
  assert.equal(walkVideo.plays, 0, 'nothing plays on load');
  assert.equal(byId('walkthrough-track').getAttribute('src'), '/app/walkthrough/walkthrough.vtt');
  assert.equal(byId('walkthrough-track').getAttribute('default'), '', 'captions on by default');
  const walkRequests = requests.filter(r => r.url.startsWith('/app/walkthrough/'));
  assert.equal(walkRequests.length, 2);
  assert.ok(walkRequests.every(r => !r.headers.Authorization), 'the walkthrough needs no key');
  const chapterButtons = byId('walkthrough-chapters').children.map(item => item.children[0]);
  assert.deepEqual(chapterButtons.map(b => b.getAttribute('aria-label')), [
    'Play from 0:00, Conversation: start, speak, automatic stop, reply, reopen', 'Play from 1:15, Product focus',
  ]);
  chapterButtons[1].onclick();
  await settle();
  assert.equal(walkVideo.currentTime, 75);
  assert.equal(walkVideo.plays, 1, 'a chapter press plays from its start');
  walkVideo.ontimeupdate();
  assert.equal(chapterButtons[1].getAttribute('aria-current'), 'true');
  assert.equal(chapterButtons[0].getAttribute('aria-current'), null);
  walkVideo.currentTime = 10;
  walkVideo.ontimeupdate();
  assert.equal(chapterButtons[0].getAttribute('aria-current'), 'true');
  assert.equal(chapterButtons[1].getAttribute('aria-current'), null);
  assert.match(byId('walkthrough-transcript').textContent, /0:00 Press Start conversation once\./);
  assert.match(byId('walkthrough-transcript').textContent, /1:15 The second one comes forward\./);
  assert.doesNotMatch(byId('walkthrough-transcript').textContent, /NOTE|-->|<v/);
  // Without a recording: the chapter plan and a transcript placeholder, and no video request.
  const { renderWalkthrough, NOT_RECORDED, CHAPTERS } = modules.get(path.join(WEB, 'walkthrough.js')).namespace;
  const bare = Object.fromEntries(['status', 'player', 'video', 'track', 'meta', 'chapters', 'heading', 'transcript'].map(k => [k, new Element()]));
  renderWalkthrough(bare, null);
  assert.equal(bare.status.textContent, NOT_RECORDED);
  assert.equal(bare.heading.textContent, 'Planned chapters', 'the unrecorded list is labeled as the plan');
  assert.equal(NOT_RECORDED, 'Not recorded yet. Recorded footage will show the build and date it was made from.');
  assert.equal(bare.status.hidden, false);
  assert.equal(bare.player.hidden, true);
  assert.deepEqual(bare.chapters.children.map(item => item.textContent), [...CHAPTERS]);
  assert.equal(CHAPTERS.length, 5);
  assert.match(bare.transcript.textContent, /available with the recording/);
  assert.equal(bare.video.getAttribute('src'), null, 'no recording, no video request');
  renderWalkthrough(bare, walkthroughManifest, '');
  assert.equal(bare.heading.textContent, 'Chapters');
  assert.match(bare.transcript.textContent, /captions file could not be read/);
  console.log('PASS: video walkthrough: a recording is labeled recorded with build and date, no autoplay, captions on, chapters play from their start and mark the current one, transcript from the captions, no key; without one, the chapter plan and a transcript placeholder');

  // How it works reads before sign-in: the toggle shows, the diagram's details work, and the
  // live parts ask for a signed-in turn.
  const NODE_IDS = ['mic', 'gateway', 'orchestrator', 'asr', 'llm', 'tools', 'result', 'tts', 'delivery', 'playback'];
  const arch = id => byId(`arch-node-${id}`);
  const mark = id => ['done', 'skipped', 'failed', 'denied', 'waiting', 'missing'].find(state => arch(id).classList.contains(state)) || 'none';
  const marks = () => Object.fromEntries(NODE_IDS.map(id => [id, mark(id)]));
  const key = name => ({ key: name, preventDefault() {} });
  assert.equal(byId('how-toggle').hidden, false, 'How it works is available before sign-in');
  assert.equal(byId('how-live-note').textContent, 'Sign in and send a message to see a live turn.');
  assert.equal(byId('how-timings').hidden, true, 'no timings table before a turn');
  assert.ok(NODE_IDS.every(id => mark(id) === 'none' && !arch(id).classList.contains('current')), 'no marks before a turn');
  byId('how').hidden = true; // as index.html starts it; the fake DOM does not read the markup
  byId('how-toggle').onclick();
  assert.equal(byId('how').hidden, false);
  assert.equal(byId('signin').hidden, true, 'dedicated architecture view hides sign-in');
  assert.equal(byId('store').hidden, true, 'dedicated architecture view hides shopping');
  assert.equal(byId('how-toggle').getAttribute('aria-pressed'), 'true');
  byId('how-tab-live').onclick();
  arch('asr').onclick();
  assert.equal(byId('arch-detail-title').textContent, 'NVIDIA speech recognition');
  assert.match(byId('arch-detail-facts').textContent, /mock-asr @ fixture-v1 \(mock fixture, no model runs\)/);
  assert.match(byId('arch-detail-facts').textContent, /nothing observed yet/);
  assert.equal(arch('asr').getAttribute('aria-pressed'), 'true');
  arch('llm').onkeydown(key('Enter'));
  assert.equal(byId('arch-detail-title').textContent, 'Nemotron through vLLM');
  assert.equal(arch('asr').getAttribute('aria-pressed'), 'false');
  assert.match(byId('arch-detail-facts').textContent, /16,384 tokens per prompt/);
  arch('gateway').onkeydown(key(' '));
  assert.match(byId('arch-detail-facts').textContent, /SSH tunnel.*Caddy on 443/);
  arch('gateway').onkeydown(key('x'));
  assert.equal(byId('arch-detail-title').textContent, 'Gateway and admission');
  // Tabs: a click or the arrow keys select; only the selected tab is in the Tab order.
  byId('how-tab-video').onclick();
  assert.equal(byId('how-panel-video').hidden, false);
  assert.equal(byId('how-panel-arch').hidden, true);
  assert.equal(byId('how-tab-video').getAttribute('aria-selected'), 'true');
  assert.equal(byId('how-tab-arch').getAttribute('tabindex'), '-1');
  byId('how-tab-video').onkeydown(key('ArrowRight'));
  assert.equal(byId('how-panel-arch').hidden, false, 'ArrowRight wraps to the first tab');
  assert.equal(document.activeElement, byId('how-tab-arch'));
  byId('how-tab-arch').onkeydown(key('End'));
  assert.equal(byId('how-tab-video').getAttribute('aria-selected'), 'true');
  byId('how-tab-video').onkeydown(key('ArrowLeft'));
  assert.equal(byId('how-tab-live').getAttribute('aria-selected'), 'true');
  byId('how-tab-live').onkeydown(key('ArrowLeft'));
  assert.equal(byId('how-tab-arch').getAttribute('aria-selected'), 'true');
  byId('how-tab-arch').onkeydown(key('Home'));
  byId('how-tab-arch').onkeydown(key('a'));
  assert.equal(byId('how-tab-arch').getAttribute('aria-selected'), 'true', 'other keys change nothing');
  assert.equal(byId('how-tab-arch').getAttribute('tabindex'), '0');
  // The live highlight is the component of the latest observation, gateway or browser.
  const { flowState } = modules.get(path.join(WEB, 'architecture.js')).namespace;
  assert.equal(flowState([]).current, null);
  assert.equal(flowState(stream.slice(0, 2)).current, 'gateway');
  assert.equal(flowState(stream.slice(0, 2)).nodes.gateway.state, 'waiting');
  assert.equal(flowState(stream.slice(0, 4)).current, 'llm');
  assert.equal(flowState(stream.slice(0, 5)).current, 'tools');
  assert.equal(flowState(stream.slice(0, 7)).current, 'tts');
  assert.equal(flowState(stream.slice(0, 7)).nodes.result, undefined, 'nothing is marked before its event');
  assert.equal(flowState(stream).current, 'result');
  assert.equal(flowState(stream, { marks: { result: 1 } }).current, 'delivery');
  assert.equal(flowState(stream, { marks: { result: 1, audio: 2, playing: 3 } }).current, 'playback');
  assert.equal(flowState(stream, { marks: { result: 1, audio: 2, playing: 3 } }).nodes.playback.text, '✓ playing');
  // The end of the reply is observed too, so the mark never stays "playing" after the sound stops.
  assert.equal(flowState(stream, { marks: { result: 1, audio: 2, playing: 3, ended: 4 } }).nodes.playback.text, '✓ played');
  assert.equal(flowState(stream, { marks: { result: 1, audio: 2, playing: 3, ended: 4 } }).current, 'playback');

  // Sign in: the catalog loads with the key, the field clears, cards render without a session.
  byId('how-toggle').onclick();
  const presentingSignin = delayNext(url => url === '/v1/catalog');
  byId('key').value = 'tenant-key-fixture';
  const signingIn = submit('signin-form');
  await settle();
  byId('how-toggle').onclick();
  presentingSignin.resolve(Response.json(storefront));
  await signingIn;
  await settle();
  assert.equal(byId('store').hidden, true, 'a late sign-in cannot reveal the shop on the technical page');
  assert.equal(byId('how').hidden, false);
  byId('how-toggle').onclick();
  const catalog = requests.find(r => r.url === '/v1/catalog');
  assert.equal(catalog.headers.Authorization, 'Bearer tenant-key-fixture');
  assert.equal(byId('key').value, '');
  assert.equal(byId('store').hidden, false);
  assert.equal(byId('cards').children.length, 2);
  assert.match(byId('cards').textContent, /Ridgeline Rain Shell/);
  assert.ok(!requests.some(r => r.url === '/v1/sessions'), 'browsing must not reserve a session');
  assert.equal(created.length, 2, 'two product images loaded as object URLs');

  // First message: the reservation response is lost; the typed text stays.
  byId('text').value = 'rain jacket';
  await submit('text-form');
  assert.match(byId('state-line').textContent, /Connection lost/);
  assert.equal(byId('text').value, 'rain jacket');
  // Second try: same reservation key; then the message response is lost.
  await submit('text-form');
  const setups = requests.filter(r => r.url === '/v1/sessions');
  assert.equal(setups.length, 2);
  assert.equal(setups[0].headers['Idempotency-Key'], setups[1].headers['Idempotency-Key']);
  assert.match(byId('state-line').textContent, /Connection lost/);
  assert.equal(byId('retry').hidden, false);
  assert.equal(byId('text').value, 'rain jacket');
  // Retry: the same key and the same body, then cards and transcript from the result.
  const typedFrom = byId('state-line').history.length;
  await byId('retry').onclick();
  await settle();
  const typedStages = byId('state-line').history.slice(typedFrom);
  inOrder(typedStages, [
    'Sending.', 'Sent. Thinking.', /^Waiting for the GPU/, 'Running after 1 s in the queue. Thinking.',
    'Checking the catalog.', 'Catalog: search products, 1 result.', 'Writing the reply.', 'Reply ready.',
    'Playing the test tone.',
  ], 'typed turn stages');
  assert.ok(!typedStages.some(text => /Transcribing/.test(text)), 'a typed message skips transcription');
  const messages = requests.filter(r => r.url.endsWith('/messages'));
  assert.equal(messages.length, 2);
  assert.equal(messages[0].headers['Idempotency-Key'], messages[1].headers['Idempotency-Key']);
  assert.equal(messages[0].body, messages[1].body);
  assert.equal(messages[0].body.get('text'), 'rain jacket');
  assert.equal(messages[0].body.get('display_revision'), '0');
  assert.ok(byId('state-line').history.some(text => /Waiting for the GPU/.test(text)), 'waiting state shown');
  assert.equal(byId('retry').hidden, true);
  assert.equal(byId('cards').children.length, 1);
  assert.match(byId('cards').textContent, /Drizzle Anorak/);
  assert.match(byId('products-label').textContent, /RESULTS/);
  assert.equal(byId('transcript').children.length, 2);
  assert.match(byId('transcript').textContent, /FIXTURE REPLY/);
  assert.match(byId('stock-line').textContent, /2 in stock at Northline Downtown/);
  assert.equal(byId('text').value, '');
  assert.ok(byId('audio').src.startsWith('blob:'));
  assert.equal(byId('audio').plays, 1, 'reply plays after the user gesture');
  assert.match(byId('how-steps').textContent, /search products/);
  assert.match(byId('how-steps').textContent, /skipped: typed message/);
  // The diagram follows the observed events, then the browser's own result, audio and playback.
  assert.deepEqual(marks(), {
    mic: 'skipped', gateway: 'done', orchestrator: 'done', asr: 'skipped', llm: 'done',
    tools: 'done', result: 'done', tts: 'done', delivery: 'done', playback: 'done',
  });
  assert.ok(arch('playback').classList.contains('current'), 'playback is the latest observation');
  assert.equal(NODE_IDS.filter(id => arch(id).classList.contains('current')).length, 1);
  assert.equal(byId('arch-st-gateway').textContent, '✓ waited 812 ms');
  assert.equal(byId('arch-st-llm').textContent, '✓ 2 decisions · 5.0 ms');
  assert.equal(byId('arch-st-asr').textContent, '○ typed message');
  assert.match(byId('how-steps').textContent, /Catalog tool: search products/);
  assert.match(byId('how-steps').textContent, /requested by the model/);
  assert.match(byId('how-steps').textContent, /Queue wait for the GPU/);
  assert.equal(byId('how-timings').hidden, false);
  assert.equal(byId('how-live-note').textContent, 'Latest turn: typed message, complete.');
  assert.match(arch('gateway').getAttribute('aria-label'), /completed: waited 812 ms/);
  assert.match(byId('arch-detail-facts').textContent, /completed: waited 812 ms/, 'the open details card shows this turn');
  const raw = byId('raw').textContent;
  assert.match(raw, /turn.completed/);
  assert.ok(!raw.includes('private-session-token') && !raw.includes('tenant-key-fixture'));
  assert.match(byId('turns-left').textContent, /15 of 16/);

  // Browser rows: waits between the browser's own marks on its clock, never a round trip.
  const typedTimings = context.__timings;
  assert.ok(Object.isFrozen(typedTimings) && Object.isFrozen(typedTimings.marks) && Object.isFrozen(typedTimings.durations));
  assert.equal(typedTimings.turn_id, 'turn1');
  assert.equal(typedTimings.input_source, 'text');
  assert.equal(typedTimings.capture, null);
  assert.equal(typedTimings.audio_fetch, 'after the result', 'no audio_path on the terminal event: fetched after the result');
  assert.equal(typedTimings.audio_bytes, 4);
  const order = ['request', 'firstByte', 'terminal', 'result', 'audio', 'playing'];
  for (const name of order) assert.equal(typeof typedTimings.marks[name], 'number', name);
  order.slice(1).forEach((name, index) => assert.ok(typedTimings.marks[order[index]] <= typedTimings.marks[name], `${order[index]} before ${name}`));
  for (const label of ['request start to first event', 'request start to turn end', 'turn end to result received', 'turn end to audio received', 'request start to playback start']) {
    assert.match(byId('how-steps').textContent, new RegExp(`Browser wait from ${label}`));
  }
  assert.match(byId('how-steps').textContent, /4 KB WAV|0 KB WAV/);
  assert.doesNotMatch(byId('how-steps').textContent, /round trip|RTT|network/i);
  context.__timings = { forged: true };
  assert.equal(context.__timings.turn_id, 'turn1', 'window.__timings is read-only');
  assert.equal(context.__timings.marks.decoded, undefined);
  byId('audio').oncanplay();
  byId('audio').oncanplay();
  assert.equal(typeof context.__timings.marks.decoded, 'number', 'canplay marks the audio ready');
  assert.match(byId('how-steps').textContent, /Browser wait from audio received to ready to play/);

  // With audio_path on the terminal event, the audio fetch starts with the result's (N3).
  const withAudio = stream.map(e => e.event_type === 'turn.completed'
    ? { ...e, audio_path: '/v1/sessions/session1/turns/turn1/audio', audio_kind: 'test_tone' } : e);
  messageStream = withAudio;
  const audioCount = () => requests.filter(r => r.url.endsWith('/audio')).length;
  const resultGate = delayNext(url => url.endsWith('/result'));
  const audioBefore = audioCount();
  byId('text').value = 'rain jacket again';
  const parallelTurn = submit('text-form');
  await settle();
  assert.ok(resultGate.seen, 'the result fetch is held');
  assert.equal(audioCount(), audioBefore + 1, 'the audio fetch started while the result was on its way');
  resultGate.resolve(Response.json(result));
  await parallelTurn;
  await settle();
  assert.equal(context.__timings.audio_fetch, 'with the result');
  assert.equal(audioCount(), audioBefore + 1, 'no second audio fetch');
  assert.match(byId('how-steps').textContent, /fetched with the result/);
  assert.equal(typeof context.__timings.marks.playing, 'number');

  // A late audio response from an attempt whose result was lost marks nothing on the retry.
  const lateAudio = delayNext(url => url.endsWith('/audio'));
  lose.result = true;
  byId('text').value = 'lost result';
  await submit('text-form');
  await settle();
  assert.ok(lateAudio.seen);
  assert.match(byId('state-line').textContent, /Connection lost/);
  await byId('retry').onclick();
  await settle();
  assert.equal(context.__timings.audio_bytes, 4);
  lateAudio.resolve(new Response(new Uint8Array(10)));
  await settle();
  assert.equal(context.__timings.audio_bytes, 4, "the lost attempt's audio marks nothing");
  messageStream = null;

  // Sign out: the session ends and keys, cards, transcript, URLs and raw events are cleared.
  await byId('sign-out').onclick();
  const deleted = requests.find(r => r.method === 'DELETE');
  assert.equal(deleted.url, '/v1/sessions/session1');
  assert.equal(deleted.headers.Authorization, 'Bearer private-session-token');
  assert.equal(byId('signin').hidden, false);
  assert.equal(byId('store').hidden, true);
  assert.equal(byId('cards').children.length, 0);
  assert.equal(byId('transcript').children.length, 0);
  assert.equal(byId('raw').textContent, '[]');
  assert.ok(created.every(url => revoked.includes(url)), 'every object URL revoked');

  // A capacity refusal keeps the typed question and explains it.
  byId('key').value = 'tenant-key-fixture';
  await submit('signin-form');
  await settle();
  refuseSetup = true;
  byId('text').value = 'warm hat';
  await submit('text-form');
  assert.match(byId('state-line').textContent, /Another conversation for this store/);
  assert.equal(byId('text').value, 'warm hat');
  await byId('sign-out').onclick();
  refuseSetup = false;
  const signin = async key => { byId('key').value = key; await submit('signin-form'); await settle(); };
  const cleanHarbor = () => {
    assert.equal(byId('retailer-name').textContent, 'Harbor Supply');
    assert.match(byId('cards').textContent, /Harbor Deck Jacket/);
    assert.doesNotMatch(byId('cards').textContent, /Drizzle|Ridgeline/);
    assert.equal(byId('transcript').children.length, 0);
    assert.equal(byId('raw').textContent, '[]');
    assert.equal(byId('audio').src, '');
    assert.equal(byId('turns-left').textContent, '');
  };
  // Test every HTTP boundary with a fetch implementation that ignores cancellation.
  for (const phase of ['setup', 'events', 'result', 'audio', 'diagnostic']) {
    await signin('tenant-key-fixture');
    const gate = delayNext(url => phase === 'setup' ? url === '/v1/sessions'
      : phase === 'events' || phase === 'diagnostic' ? url.endsWith(phase === 'events' ? '/messages' : '/fixture-turn')
      : url.endsWith('/' + phase));
    byId('text').value = 'old retailer question';
    const turn = phase === 'diagnostic' ? byId('fixture').onclick() : submit('text-form');
    await settle();
    assert.ok(gate.seen, phase + ' reached its delayed boundary');
    await byId('sign-out').onclick();
    assert.ok(gate.signal.aborted, phase + ' request aborted at sign-out');
    await signin('tenant-b-fixture');
    cleanHarbor();
    const response = phase === 'setup'
      ? Response.json({ session_id: 'late-session', session_token: 'late-token', max_turns: 16 })
      : phase === 'events' || phase === 'diagnostic'
        ? new Response(stream.map(e => JSON.stringify(e)).join('\n') + '\n')
        : phase === 'result' ? Response.json(result) : new Response(new Uint8Array([82, 73]));
    // An obsolete finally must not enable controls while the new generation is busy.
    const busy = delayNext(url => url === '/v1/sessions');
    byId('text').value = 'new retailer question';
    const newTurn = submit('text-form');
    await settle();
    gate.resolve(response);
    await turn;
    await settle();
    cleanHarbor();
    assert.equal(byId('text').disabled, true, phase + ' obsolete finally kept new busy state');
    if (phase === 'setup') assert.ok(requests.some(r => r.method === 'DELETE' && r.url.endsWith('/late-session')));
    await byId('sign-out').onclick();
    busy.resolve(Response.json({ detail: 'cancelled' }, { status: 409 }));
    await newTurn;
  }
  // Late sign-in and same-key images also belong to a generation, not to a credential value.
  const catalogGate = delayNext(url => url === '/v1/catalog');
  byId('key').value = 'tenant-key-fixture';
  const oldSignin = submit('signin-form');
  await settle();
  await byId('sign-out').onclick();
  await signin('tenant-b-fixture');
  catalogGate.resolve(Response.json(storefront));
  await oldSignin;
  cleanHarbor();
  await byId('sign-out').onclick();
  const imageGate = delayNext(url => url.startsWith('/v1/catalog/images/'));
  await signin('tenant-key-fixture');
  await byId('sign-out').onclick();
  await signin('tenant-key-fixture');
  const liveImages = created.filter(url => !revoked.includes(url));
  imageGate.resolve(new Response(new Uint8Array([137, 80])));
  await settle();
  assert.deepEqual(created.filter(url => !revoked.includes(url)), liveImages, 'late image URL disposed even for the same key');
  await byId('sign-out').onclick();

  // Microphone permission can resolve after sign-out; its tracks must be disposed, never submitted.
  const permission = deferred();
  let stopped = 0;
  context.isSecureContext = true;
  context.navigator = { mediaDevices: { getUserMedia: () => permission.promise } };
  await signin('tenant-key-fixture');
  const listening = byId('talk').onclick();
  await settle();
  await byId('sign-out').onclick();
  await signin('tenant-b-fixture');
  permission.resolve({ getTracks: () => [{ stop: () => { stopped += 1; } }] });
  await listening;
  assert.equal(stopped, 1, 'late microphone tracks stopped');
  cleanHarbor();
  assert.equal(byId('talk').textContent, 'Start conversation');
  await byId('sign-out').onclick();
  assert.ok(created.every(url => revoked.includes(url)), 'all URLs disposed after delayed work');

  // Voice: the level meter follows recorded blocks, hides on Stop, and the state line follows
  // the voice turn's events. Transcribing appears only once the gateway has the recording.
  context.AudioContext = FakeAudioContext;
  context.AudioWorkletNode = FakeWorkletNode;
  let micStops = 0;
  context.navigator = { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => { micStops += 1; } }] }) } };
  await signin('tenant-key-fixture');
  assert.equal(byId('mic-level').hidden, true, 'meter hidden before the microphone opens');
  await byId('talk').onclick();
  await settle();
  assert.equal(byId('talk').textContent, 'Stop');
  assert.equal(byId('mic-level').hidden, false, 'meter visible while listening');
  assert.equal(byId('mic-level-fill').style.width, '0%');
  feed(0.2, 100);
  const loud = parseInt(byId('mic-level-fill').style.width, 10);
  assert.ok(loud > 70 && loud < 85, `a 0.2 RMS block fills about 77%, got ${loud}`);
  assert.equal(byId('mic-level').getAttribute('aria-valuenow'), String(loud));
  feed(0, 50);
  const falling = parseInt(byId('mic-level-fill').style.width, 10);
  feed(0, 300);
  const quiet = parseInt(byId('mic-level-fill').style.width, 10);
  assert.ok(loud > falling && falling > quiet, `the bar falls back after sound stops: ${loud}, ${falling}, ${quiet}`);
  const voiceStream = [
    event(0, 'turn.started', { status: 'running', input_source: 'audio' }),
    event(1, 'asr.completed', { duration_ms: 4, model_id: 'mock-asr', model_revision: 'fixture-v1' }),
    event(2, 'model.completed', { decision: 'tool_call', duration_ms: 3, model_id: 'mock-model', model_revision: 'fixture-v1' }),
    event(3, 'tool.denied', { status: 'denied', reason: 'catalog_scope_denied', tool_name: 'get_product_details', tool_call_id: 'mock-call-1' }),
    event(4, 'model.completed', { decision: 'tool_call', duration_ms: 3, model_id: 'mock-model', model_revision: 'fixture-v1' }),
    event(5, 'tool.completed', { tool_name: 'search_products', tool_call_id: 'mock-call-2', result_count: 1, catalog_version: '2026.11.1' }),
    event(6, 'model.completed', { decision: 'answer', duration_ms: 2, model_id: 'mock-model', model_revision: 'fixture-v1' }),
    event(7, 'tts.completed', { duration_ms: 1, model_id: 'mock-tts', model_revision: 'fixture-v1' }),
    event(8, 'turn.completed', { result_path: '/v1/sessions/session1/turns/turn1/result', input_source: 'audio' }),
  ];
  messageStream = voiceStream;
  messageResult = { ...result, input_source: 'audio', transcript: 'Fixture transcript: rain jacket' };
  const voiceFrom = byId('state-line').history.length;
  const sentBefore = requests.filter(r => r.url.endsWith('/messages')).length;
  await byId('talk').onclick();
  await settle();
  assert.equal(byId('mic-level').hidden, true, 'meter hidden once the microphone closes');
  assert.equal(byId('mic-level-fill').style.width, '0%');
  assert.equal(micStops, 1, 'microphone tracks stopped after Stop');
  const voiceRequests = requests.filter(r => r.url.endsWith('/messages'));
  assert.equal(voiceRequests.length, sentBefore + 1);
  const wav = voiceRequests.at(-1).body.get('audio');
  assert.equal(wav.type, 'audio/wav');
  assert.equal(wav.size, 44 + 2 * 16 * 450, 'the WAV holds exactly the recorded blocks');
  const voiceStages = byId('state-line').history.slice(voiceFrom);
  inOrder(voiceStages, [
    'Sending.', 'Sent. Transcribing.', 'Heard you. Thinking.', 'Checking the catalog.',
    'The catalog refused a lookup.', 'Checking the catalog.', 'Catalog: search products, 1 result.',
    'Writing the reply.', 'Reply ready.', 'Playing the test tone.',
  ], 'voice turn stages');
  const transcribing = voiceStages.indexOf('Sent. Transcribing.');
  assert.ok(voiceStages.indexOf('Sending.') < transcribing && transcribing < voiceStages.indexOf('Heard you. Thinking.'));
  assert.match(byId('transcript').textContent, /YOU · VOICE/);
  assert.equal(mark('mic'), 'done');
  assert.equal(mark('asr'), 'done');
  assert.equal(mark('tools'), 'denied', 'a refused lookup marks the tools');
  assert.equal(byId('arch-st-tools').textContent, '× 1 of 2 calls refused');
  assert.match(byId('how-steps').textContent, /Catalog refused: product details/);
  assert.match(byId('how-steps').textContent, /denied: catalog_scope_denied/);

  // A failed turn shows its reason as soon as turn.failed arrives.
  messageStream = [
    event(0, 'turn.started', { status: 'running', input_source: 'text' }),
    event(1, 'turn.failed', { status: 'failed', reason: 'deadline', result_path: '/v1/sessions/session1/turns/turn1/result', input_source: 'text' }),
  ];
  messageResult = { ...result, status: 'failed', reason: 'deadline', answer: '', audio_path: undefined, stock_checks: [] };
  const failedFrom = byId('state-line').history.length;
  byId('text').value = 'slow question';
  await submit('text-form');
  await settle();
  inOrder(byId('state-line').history.slice(failedFrom), ['Sending.', 'Sent. Thinking.', 'The turn ran out of time. Try again.'], 'failed turn stages');
  assert.equal(byId('state-line').textContent, 'The turn ran out of time. Try again.');
  assert.ok(byId('state-line').classList.contains('error'));
  // Failed stages read failed, and stages with no event read not observed.
  assert.equal(mark('result'), 'failed');
  assert.equal(byId('arch-st-result').textContent, '× deadline');
  assert.equal(mark('orchestrator'), 'failed');
  assert.equal(mark('asr'), 'skipped');
  assert.equal(mark('llm'), 'missing');
  assert.equal(mark('tools'), 'missing');
  assert.equal(mark('tts'), 'missing');
  assert.equal(byId('arch-st-llm').textContent, '? not observed');
  assert.equal(byId('arch-st-playback').textContent, '? no reply audio: the turn failed');
  assert.match(byId('how-steps').textContent, /failed: deadline/);
  assert.equal(byId('how-live-note').textContent, 'Latest turn: typed message, failed.');
  messageStream = null;
  messageResult = null;
  await byId('sign-out').onclick();
  assert.equal(byId('mic-level').hidden, true);
  console.log('PASS: microphone level meter from recorded blocks, hidden when closed; stage line for typed, voice, denied and failed turns');

  // Conversation mode. Start once; speech then 0.8 s of quiet submits; the microphone reopens
  // after a played reply; it does not reopen after nothing heard, a failed turn, while muted,
  // at the turn cap, after End, or for a late callback from an older generation.
  let micOpens = 0;
  context.navigator = { mediaDevices: { getUserMedia: async () => {
    micOpens += 1;
    return { getTracks: () => [{ stop: () => { micStops += 1; } }] };
  } } };
  const messagesSent = () => requests.filter(r => r.url.endsWith('/messages'));
  const voiceTurn = () => { messageStream = voiceStream; messageResult = { ...result, input_source: 'audio', transcript: 'Fixture transcript: rain jacket' }; };
  const replyEnds = async () => { byId('audio').paused = true; byId('audio').onended(); await settle(); };
  const MUTED_TEXT = 'Muted. Unmute or type.';
  voiceTurn();
  await signin('tenant-key-fixture');
  assert.equal(byId('talk').textContent, 'Start conversation');
  assert.equal(byId('mute-mic').disabled, true, 'no microphone to mute before the conversation');
  assert.equal(byId('end').disabled, true, 'no conversation to end yet');
  await byId('talk').onclick();
  await settle();
  let opens = micOpens;
  assert.equal(byId('talk').textContent, 'Stop');
  assert.equal(byId('mute-mic').disabled, false);
  assert.equal(byId('end').disabled, false);
  assert.equal(byId('state-line').textContent, 'Listening.');
  let sent = messagesSent().length;
  speak(0.1, 400);
  speak(0, 500);
  assert.equal(messagesSent().length, sent, 'still listening inside the 0.8 s hangover');
  speak(0, 400);
  await settle();
  assert.equal(messagesSent().length, sent + 1, 'speech then 0.8 s of quiet submits without a press');
  assert.equal(messagesSent().at(-1).body.get('audio').size, 44 + 2 * 16 * 1200, 'the WAV ends where the gate ended it');
  // The recording's own numbers: its length and the gate's quiet at its end, measured in
  // recorded audio, then the browser waits from capture end to playback on the page's clock.
  const voiceTimings = context.__timings;
  assert.equal(voiceTimings.input_source, 'audio');
  assert.equal(voiceTimings.capture.auto, true);
  assert.equal(voiceTimings.capture.audioMs, 1200);
  assert.equal(voiceTimings.capture.quietMs, 800, 'the quiet before the gate ended the turn');
  assert.ok(voiceTimings.durations.capture_end_to_playing_ms >= 0);
  assert.ok(voiceTimings.durations.capture_end_to_request_ms >= 0);
  assert.match(byId('how-steps').textContent, /Quiet at the end of the recording/);
  assert.match(byId('how-steps').textContent, /silence gate's wait/);
  assert.match(byId('how-steps').textContent, /Browser wait from capture end to playback start/);
  assert.doesNotMatch(byId('how-steps').textContent, /Browser wait from request start to playback start/);
  assert.equal(byId('state-line').textContent, 'Playing the test tone.');
  assert.equal(micOpens, opens, 'the microphone stays closed while the reply plays');
  await replyEnds();
  assert.equal(micOpens, opens + 1, 'the microphone reopens after the reply');
  assert.equal(byId('talk').textContent, 'Stop');
  assert.equal(byId('mic-level').hidden, false);

  // No speech within 8 s: nothing is sent, the microphone closes and stays closed.
  sent = messagesSent().length;
  let stops = micStops;
  for (let t = 0; t < 8100; t += 100) feed(0, 100);
  await settle();
  assert.equal(messagesSent().length, sent, 'nothing heard sends no request');
  assert.equal(byId('state-line').textContent, 'Nothing heard. Try again.');
  assert.equal(micStops, stops + 1);
  assert.equal(byId('mic-level').hidden, true);
  assert.equal(byId('talk').textContent, 'Talk');
  assert.equal(micOpens, opens + 1, 'no reopen after nothing heard');

  // A failed turn shows its reason and waits for a press.
  messageStream = [
    event(0, 'turn.started', { status: 'running', input_source: 'audio' }),
    event(1, 'turn.failed', { status: 'failed', reason: 'model_rejected_request', result_path: '/v1/sessions/session1/turns/turn1/result', input_source: 'audio' }),
  ];
  messageResult = { ...result, status: 'failed', reason: 'model_rejected_request', answer: '', audio_path: undefined, stock_checks: [], input_source: 'audio', transcript: '' };
  await byId('talk').onclick();
  await settle();
  opens = micOpens;
  speak(0.1, 400);
  speak(0, 1600);
  await settle();
  assert.equal(byId('state-line').textContent, 'Nothing heard. Try again or type.');
  assert.ok(!byId('state-line').classList.contains('error'), 'nothing heard is not styled as an error');
  const lastEntry = byId('transcript').children[byId('transcript').children.length - 1];
  assert.equal(lastEntry.className, 'note', 'a quiet note, not a FAILED entry');
  assert.doesNotMatch(byId('transcript').textContent, /FAILED|\(nothing heard\)/);
  assert.equal(micOpens, opens, 'no reopen after a failed turn');
  assert.equal(byId('talk').textContent, 'Talk');

  // Muted during the reply: it finishes, the microphone stays closed, unmute reopens it.
  voiceTurn();
  await byId('talk').onclick();
  await settle();
  speak(0.1, 400);
  speak(0, 1600);
  await settle();
  opens = micOpens;
  await byId('mute-mic').onclick();
  assert.equal(byId('mute-mic').getAttribute('aria-pressed'), 'true');
  assert.equal(byId('mute-mic').textContent, 'Unmute microphone');
  assert.equal(byId('state-line').textContent, 'Playing the test tone.', 'muting keeps the speaking state');
  await replyEnds();
  assert.equal(micOpens, opens, 'no reopen while muted');
  assert.equal(byId('state-line').textContent, MUTED_TEXT);
  await byId('mute-mic').onclick();
  await settle();
  assert.equal(micOpens, opens + 1, 'unmute opens the microphone');
  assert.equal(byId('mute-mic').textContent, 'Mute microphone');
  // Muting while listening discards the recording; nothing is sent.
  sent = messagesSent().length;
  speak(0.1, 400);
  await byId('mute-mic').onclick();
  await settle();
  assert.equal(messagesSent().length, sent, 'muting does not send what was recorded');
  assert.equal(byId('mic-level').hidden, true);
  assert.equal(byId('state-line').textContent, MUTED_TEXT);

  // Typing while the microphone listens replaces the recording; the typed turn also reopens it.
  await byId('mute-mic').onclick();
  await settle();
  assert.equal(byId('talk').textContent, 'Stop');
  messageStream = null;
  messageResult = null;
  stops = micStops;
  byId('text').value = 'warm hat';
  await submit('text-form');
  await settle();
  assert.equal(micStops, stops + 1, 'the open microphone closed for the typed message');
  assert.equal(messagesSent().at(-1).body.get('text'), 'warm hat');
  assert.equal(messagesSent().at(-1).body.get('audio'), null);
  opens = micOpens;
  await replyEnds();
  assert.equal(micOpens, opens + 1, 'a typed turn reopens the microphone in conversation mode');

  // Presenting the architecture discards live capture and requires an explicit unmute on return.
  sent = messagesSent().length;
  stops = micStops;
  opens = micOpens;
  speak(0.1, 400);
  byId('how-toggle').onclick();
  await settle();
  assert.equal(byId('store').hidden, true);
  assert.equal(byId('how').hidden, false);
  assert.equal(micStops, stops + 1, 'How it works closes the active microphone');
  assert.equal(byId('mic-level').hidden, true);
  assert.equal(messagesSent().length, sent, 'navigation never submits the partial recording');
  byId('how-toggle').onclick();
  await settle();
  assert.equal(byId('store').hidden, false);
  assert.equal(byId('how').hidden, true);
  assert.equal(byId('state-line').textContent, MUTED_TEXT);
  assert.equal(micOpens, opens, 'returning to the store does not silently reopen capture');
  await byId('mute-mic').onclick();
  await settle();
  assert.equal(micOpens, opens + 1, 'explicit unmute resumes the same conversation');
  voiceTurn();
  speak(0.1, 400);
  speak(0, 1600);
  await settle();
  assert.equal(byId('state-line').textContent, 'Playing the test tone.', 'resumed conversation still plays replies');
  await replyEnds();

  // End conversation: microphone, playback and session go; sign-in and storefront stay.
  const staleEnded = byId('audio').onended;
  const deletes = requests.filter(r => r.method === 'DELETE').length;
  stops = micStops;
  await byId('end').onclick();
  assert.equal(requests.filter(r => r.method === 'DELETE').length, deletes + 1);
  assert.equal(requests.at(-1).url, '/v1/sessions/session1');
  assert.equal(micStops, stops + 1, 'End closes the microphone');
  assert.equal(byId('audio').src, '', 'End stops playback');
  assert.equal(byId('state-line').textContent, 'Conversation ended. Start again or type.');
  assert.equal(byId('talk').textContent, 'Start conversation');
  assert.equal(byId('mute-mic').disabled, true);
  assert.equal(byId('end').disabled, true);
  assert.equal(byId('transcript').children.length, 0);
  assert.equal(byId('turns-left').textContent, '');
  assert.equal(byId('store').hidden, false, 'End keeps the storefront');
  assert.equal(byId('retailer-name').textContent, 'Northline Outfitters');
  assert.match(byId('products-label').textContent, /STOREFRONT/);
  assert.equal(byId('cards').children.length, 2);
  opens = micOpens;
  staleEnded();
  await settle();
  assert.equal(micOpens, opens, 'a late onended from an ended conversation does nothing');
  assert.equal(byId('state-line').textContent, 'Conversation ended. Start again or type.');

  // The next Start reserves a fresh session. With a two-turn cap, the second reply ends it.
  sessionMaxTurns = 2;
  voiceTurn();
  const setupsBeforeStart = requests.filter(r => r.url === '/v1/sessions').length;
  await byId('talk').onclick();
  await settle();
  assert.equal(requests.filter(r => r.url === '/v1/sessions').length, setupsBeforeStart + 1, 'a new session after End');
  speak(0.1, 400);
  speak(0, 1600);
  await settle();
  await replyEnds();
  opens = micOpens;
  speak(0.1, 400);
  speak(0, 1600);
  await settle();
  await replyEnds();
  assert.equal(micOpens, opens, 'no reopen after the last turn');
  assert.equal(byId('state-line').textContent, '2 turns used. End the conversation to start over.');
  sessionMaxTurns = 16;
  await byId('end').onclick();

  // The microphone fails to open: the page leaves conversation mode and offers Start again, and
  // a typed reply afterwards neither retries the microphone nor repaints the error.
  const getUserMedia = context.navigator.mediaDevices.getUserMedia;
  context.navigator.mediaDevices.getUserMedia = async () => { throw new Error('NotAllowedError'); };
  voiceTurn();
  await byId('talk').onclick();
  await settle();
  assert.match(byId('state-line').textContent, /Microphone blocked/);
  assert.equal(byId('talk').textContent, 'Start conversation', 'a failed microphone leaves conversation mode');
  assert.equal(byId('mute-mic').disabled, true);
  context.navigator.mediaDevices.getUserMedia = getUserMedia;
  messageStream = null;
  messageResult = null;
  opens = micOpens;
  byId('text').value = 'warm hat';
  await submit('text-form');
  await settle();
  await replyEnds();
  assert.equal(micOpens, opens, 'a typed reply does not retry the failed microphone');
  assert.equal(byId('state-line').textContent, 'Ready. Talk or type.');
  voiceTurn();
  await byId('end').onclick();

  // A presenter can raise the speech level: below it, the gate hears no speech.
  context.location = { search: '?speech_threshold=0.05' };
  await byId('talk').onclick();
  await settle();
  sent = messagesSent().length;
  speak(0.03, 400);
  speak(0, 1600);
  await settle();
  assert.equal(messagesSent().length, sent, '0.03 RMS is not speech at a 0.05 threshold');
  assert.equal(byId('talk').textContent, 'Stop');
  delete context.location;

  // A reply still playing at sign-out cannot reopen the microphone for the next sign-in.
  await byId('end').onclick();
  await byId('talk').onclick();
  await settle();
  speak(0.1, 400);
  speak(0, 1600);
  await settle();
  const staleSignout = byId('audio').onended;
  await byId('sign-out').onclick();
  await signin('tenant-b-fixture');
  opens = micOpens;
  staleSignout();
  await settle();
  assert.equal(micOpens, opens, 'a late onended from a previous sign-in does nothing');
  cleanHarbor();
  assert.equal(byId('talk').textContent, 'Start conversation');
  messageStream = null;
  messageResult = null;
  await byId('sign-out').onclick();
  console.log('PASS: conversation mode stops on silence, reopens after replies and typed turns, and stays closed after nothing heard, failure, mute, the turn cap, End and a stale sign-in; a failed microphone leaves conversation mode');

  // Product focus: opens on a click, a positional result or verified stock of the selected
  // product; never on a plain search or a clarifying answer; closes on Back, Escape, a new search
  // without the product, an explicit id for something else, End and sign-out.
  const [ridgeline, drizzle] = storefront.products;
  const focusResult = extra => ({
    ...result, product_cards: storefront.products, selected_product_id: null, stock_checks: [],
    display_revision: 1, clarification: false, ...extra,
  });
  const say = async (text, extra = {}) => {
    messageResult = focusResult({ transcript: text, ...extra });
    byId('text').value = text;
    await submit('text-form');
    await settle();
  };
  const cardButton = index => byId('cards').children[index].children[0];
  const focusOpen = () => !byId('focus').hidden;
  await signin('tenant-key-fixture');
  assert.equal(focusOpen(), false);

  // A greeting before any search leaves the storefront on screen: the display revision did not
  // change and no search ran. A search with no matches still becomes an empty result list.
  const noSearch = stream.filter(e => e.event_type !== 'tool.completed' && e.decision !== 'tool_call');
  messageStream = noSearch;
  await say('Hi there', { product_cards: [], display_revision: 0, answer: 'Fixture answer: hello, welcome to Northline Downtown. Tell me what you are shopping for.' });
  assert.match(byId('products-label').textContent, /STOREFRONT/, 'a greeting keeps the storefront');
  assert.equal(mark('tools'), 'skipped', 'a greeting needed no catalog lookup');
  assert.equal(byId('arch-st-tools').textContent, '○ no lookup needed');
  assert.match(byId('how-steps').textContent, /skipped: no catalog lookup was needed/);
  assert.doesNotMatch(byId('how-steps').textContent, /Catalog tool|unavailable/);
  assert.match(byId('how-meta').textContent, /not read in this turn/);
  assert.equal(byId('cards').children.length, 2);
  messageStream = null;
  await say('any hats under 5 dollars', { product_cards: [], display_revision: 0, answer: 'Fixture answer: I found no matches.' });
  assert.match(byId('products-label').textContent, /RESULTS/, 'a search with no matches shows the empty results');
  assert.equal(byId('cards').children[0].textContent, 'No products matched that request.');
  await byId('end').onclick();

  await say('rain jacket');
  assert.equal(focusOpen(), false, 'a plain search opens nothing');
  assert.ok(!byId('cards').classList.contains('compact'));

  // A card click: photo, name, price, description, colors and sizes, then tags from the detail.
  cardButton(1).onclick();
  await settle();
  assert.equal(focusOpen(), true, 'a card click opens the panel');
  assert.equal(byId('focus-name').textContent, 'Drizzle Anorak');
  assert.equal(byId('focus-price').textContent, '$69.00');
  assert.equal(byId('focus-label').textContent, 'RESULT 2 OF 2');
  assert.equal(byId('focus-description').textContent, 'Synthetic.');
  assert.equal(byId('focus-colors').textContent, 'navy');
  assert.equal(byId('focus-sizes').textContent, 'M');
  assert.match(byId('focus-tags').textContent, /hooded jacket/, 'tags come from the product detail');
  assert.ok(requests.some(r => r.url === '/v1/catalog/products/a-drizzle-anorak' && r.headers.Authorization === 'Bearer tenant-key-fixture'));
  const photo = byId('focus-photo').children[0];
  assert.equal(photo.tagName, 'IMG');
  assert.ok(photo.src.startsWith('blob:'), 'the photo loads through the authenticated image route');
  assert.equal(photo.alt, drizzle.image_alt);
  assert.ok(byId('cards').classList.contains('compact'));
  assert.equal(byId('cards').children.length, 2, 'the numbered cards stay on screen');
  assert.ok(cardButton(1).children[0].children.some(child => child.className === 'position' && child.text === '2'), 'card 2 keeps its number');
  assert.equal(cardButton(1).getAttribute('aria-pressed'), 'true');
  assert.equal(document.activeElement, byId('focus-name'), 'keyboard focus moves to the product name');

  // Back closes it, keeps the selection and returns keyboard focus to the card.
  byId('focus-back').onclick();
  assert.equal(focusOpen(), false);
  assert.ok(!byId('cards').classList.contains('compact'));
  assert.equal(cardButton(1).getAttribute('aria-pressed'), 'true', 'Back keeps the selection');
  assert.equal(document.activeElement, cardButton(1), 'keyboard focus returns to the card');
  assert.equal(byId('focus-photo').children.length, 0);
  await say('which one is lighter', { selected_product_id: 'a-drizzle-anorak' });
  assert.equal(focusOpen(), false, 'no automatic reopen after Back without a new focus action');

  // "The first one" opens the product the server selected; Escape closes it.
  await say('Does the first one come in navy?', { selected_product_id: 'jacket-a' });
  assert.equal(focusOpen(), true, 'a positional result opens the panel');
  assert.equal(byId('focus-name').textContent, 'Ridgeline Rain Shell');
  assert.equal(byId('focus-label').textContent, 'RESULT 1 OF 2');
  listeners.keydown({ key: 'Enter' });
  assert.equal(focusOpen(), true);
  listeners.keydown({ key: 'Escape' });
  assert.equal(focusOpen(), false, 'Escape closes the panel');
  await say('the second one', { selected_product_id: 'jacket-a', clarification: true, answer: 'The products on your screen changed. Which one do you mean?' });
  assert.equal(focusOpen(), false, 'a clarifying answer opens nothing');
  await say('compare them', { selected_product_id: 'jacket-a', answer: 'Fixture answer: the Ridgeline Rain Shell is warmer than the Drizzle Anorak.' });
  assert.equal(focusOpen(), false, 'a reply naming several products moves nothing and opens nothing');
  await say('The Drizzle Anorak is lighter, right?', { selected_product_id: 'a-drizzle-anorak', answer: 'Fixture answer: the Drizzle Anorak is lighter.' });
  assert.equal(focusOpen(), true, 'the selection following the one product the reply is about opens it');
  assert.equal(byId('focus-name').textContent, 'Drizzle Anorak');
  byId('focus-back').onclick();
  assert.equal(focusOpen(), false);

  // Verified stock for the selected product opens it, with the stock line and that variant's photo.
  const ridgelineCheck = { ...result.stock_checks[0], product_id: 'jacket-a', product_name: 'Ridgeline Rain Shell', variant_id: 'jacket-a-navy-m', quantity: 3 };
  await say('Is medium available here?', { selected_product_id: 'jacket-a', stock_checks: [ridgelineCheck] });
  assert.equal(focusOpen(), true, 'a stock check of the selected product opens the panel');
  assert.equal(byId('focus-name').textContent, 'Ridgeline Rain Shell');
  assert.equal(byId('stock-line').hidden, false);
  assert.match(byId('stock-line').textContent, /Ridgeline Rain Shell, navy M: 3 in stock/);
  assert.equal(byId('focus-photo').dataset.path, ridgeline.variants[0].image_path);
  cardButton(1).onclick();
  await settle();
  assert.equal(byId('focus-name').textContent, 'Drizzle Anorak');
  assert.equal(byId('stock-line').hidden, true, 'no stock line for a product that was not checked');
  cardButton(0).onclick();
  await settle();
  assert.equal(byId('stock-line').hidden, false, 'the verified line returns with its product');

  // The session cart renders from the committed line items only.
  assert.equal(byId('cart').hidden, true, 'no cart until something is added');
  const line = { product_id: 'jacket-a', product_name: 'Ridgeline Rain Shell', variant_id: 'jacket-a-navy-m', color: 'navy', size: 'M', price_minor: 12900, price_display: '$129.00', quantity: 1 };
  await say('add it to the cart', { selected_product_id: 'jacket-a', cart: [line], answer: 'Fixture answer: Added the Ridgeline Rain Shell in navy M, $129.00, to your cart. You have 1 item totaling $129.00.' });
  assert.equal(byId('cart').hidden, false, 'an added item shows the cart');
  assert.equal(byId('cart-count').textContent, '1 ITEM');
  assert.match(byId('cart-items').textContent, /Ridgeline Rain Shell, navy M/);
  assert.equal(byId('cart-total').textContent, 'Total $129.00');
  await say('one more', { selected_product_id: 'jacket-a', cart: [{ ...line, quantity: 2 }], answer: 'Fixture answer: Added the Ridgeline Rain Shell in navy M, $129.00, to your cart. You have 2 items totaling $258.00.' });
  assert.equal(byId('cart-count').textContent, '2 ITEMS');
  assert.match(byId('cart-items').textContent, /2 × \$129\.00/);
  assert.equal(byId('cart-total').textContent, 'Total $258.00');

  // A committed new search without the product closes the panel; selection follows the server.
  await say('warm hats', { product_cards: [drizzle], selected_product_id: null, display_revision: 2 });
  assert.equal(focusOpen(), false, 'a new search without the product closes it');
  assert.equal(byId('stock-line').hidden, true);

  // An explicit id for another product closes it, typed or spoken, even when the server keeps the
  // old selection. The focused product's own id keeps it open.
  for (const text of ['Look up product id jacket-b', 'Look up product ID jacket B']) {
    cardButton(0).onclick();
    await settle();
    assert.equal(focusOpen(), true);
    await say(text, { product_cards: [drizzle], selected_product_id: 'a-drizzle-anorak', display_revision: 2, answer: 'Fixture answer: I could not find product id jacket-b in this store.' });
    assert.equal(focusOpen(), false, `${text} leaves no misleading focus`);
  }
  cardButton(0).onclick();
  await settle();
  await say('Tell me more about a-drizzle-anorak', { product_cards: [drizzle], selected_product_id: 'a-drizzle-anorak', display_revision: 2 });
  assert.equal(focusOpen(), true, 'the focused product\'s own id keeps the panel');
  // A small-talk turn keeps the cards, the selection and the open panel.
  await say("What's the date today?", { product_cards: [drizzle], selected_product_id: 'a-drizzle-anorak', display_revision: 2, answer: 'Fixture answer: it is Monday, November 2, 2026, 3:15 PM at the store (America/Denver).' });
  assert.equal(focusOpen(), true, 'small talk keeps the focus panel');
  assert.equal(byId('focus-name').textContent, 'Drizzle Anorak');

  // The selection following the reply opens the panel on that product. A search turn's
  // reply and a clarifying question move the selection without opening it.
  messageStream = noSearch;
  await say('tell me about the ridgeline', { product_cards: storefront.products, selected_product_id: 'jacket-a', display_revision: 2, answer: 'Fixture answer: the Ridgeline Rain Shell costs $129.00 and comes in green.' });
  assert.equal(focusOpen(), true, 'a reply about one shown product opens the panel on it');
  assert.equal(byId('focus-name').textContent, 'Ridgeline Rain Shell');
  // a numeric suffix keeps the whole id, so "jacket-a-123" is unknown, not the focused jacket-a.
  const { namedIds } = modules.get(path.join(WEB, 'conversation.js')).namespace;
  assert.deepEqual([...namedIds('Tell me about jacket-a-123.')], ['jacket-a-123']);
  assert.deepEqual([...namedIds('Is a-downpour-packable-navy-m here?')], ['a-downpour-packable-navy-m']);
  assert.deepEqual([...namedIds('Look up product ID jacket B')], ['jacket-b']);
  assert.deepEqual([...namedIds('jacket-a please')], ['jacket-a']);
  await say('Tell me about jacket-a-123.', { product_cards: storefront.products, selected_product_id: 'jacket-a', display_revision: 2, answer: 'Fixture answer: I could not find product id jacket-a-123 in this store.' });
  assert.equal(focusOpen(), false, 'an unknown id with a numeric suffix closes the panel');
  byId('focus-back').onclick();
  assert.equal(focusOpen(), false);
  messageStream = null;
  await say('rain jackets', { product_cards: storefront.products, selected_product_id: null, display_revision: 3, answer: 'Fixture answer: I found 2 options from $69.00.' });
  assert.equal(focusOpen(), false, 'a search naming no product selects and opens nothing');
  await say('the cheap green one', { product_cards: storefront.products, selected_product_id: 'a-drizzle-anorak', display_revision: 3, answer: 'Fixture answer: the Drizzle Anorak is $69.00 and comes in green.' });
  assert.equal(focusOpen(), true, 'a search whose reply singles out one product opens it');
  assert.equal(byId('focus-name').textContent, 'Drizzle Anorak');
  byId('focus-back').onclick();
  messageStream = noSearch;
  await say('the green one please', { product_cards: storefront.products, selected_product_id: 'jacket-a', display_revision: 3, clarification: true, answer: 'Fixture question: the Ridgeline Rain Shell is $129.00. Does that work for you?' });
  assert.equal(focusOpen(), true, 'a reply about one product opens it even when it ends with a question');
  byId('focus-back').onclick();
  messageStream = null;

  // the photo keeps the variant the gateway committed (a verified check or an added line)
  // through a turn without a stock check, such as the date. Its default photo is yellow.
  const downpour = {
    ...card('a-downpour-packable', 'Downpour Packable Shell', 8900), colors: ['yellow', 'navy'],
    image_path: '/v1/catalog/images/img-a-downpour-packable-yellow', image_alt: 'Product photo of the Downpour Packable Shell in yellow',
    variants: ['yellow', 'navy'].map(color => ({ variant_id: `a-downpour-packable-${color}-m`, color, size: 'M', image_path: `/v1/catalog/images/img-a-downpour-packable-${color}` })),
  };
  const navyPath = downpour.variants[1].image_path;
  const downpourCheck = { ...result.stock_checks[0], product_id: 'a-downpour-packable', product_name: 'Downpour Packable Shell', variant_id: 'a-downpour-packable-navy-m', quantity: 4 };
  const settled = { product_cards: [downpour], selected_product_id: 'a-downpour-packable', selected_variant_id: 'a-downpour-packable-navy-m', display_revision: 4 };
  // The model answered without a check, so the gateway ran it; the view says whose call it was.
  messageStream = [
    event(0, 'turn.started', { status: 'running', input_source: 'text' }),
    event(1, 'model.completed', { decision: 'answer', duration_ms: 2 }),
    event(2, 'tool.completed', { tool_name: 'check_availability', tool_call_id: 'gateway-stock', result_count: 1, catalog_version: '2026.11.1', duration_ms: 0.4 }),
    event(3, 'tts.completed', { duration_ms: 1 }),
    event(4, 'turn.completed', { result_path: '/v1/sessions/session1/turns/turn1/result', input_source: 'text', duration_ms: 9 }),
  ];
  await say('Is the Downpour in navy medium available?', { ...settled, stock_checks: [downpourCheck] });
  messageStream = null;
  assert.match(byId('how-steps').textContent, /Catalog tool: check stock/);
  assert.match(byId('how-steps').textContent, /the gateway's own exact stock check/);
  assert.doesNotMatch(byId('how-steps').textContent, /requested by the model/);
  assert.match(byId('how-steps').textContent, /Gateway turn total/);
  assert.equal(mark('tools'), 'done');
  assert.equal(focusOpen(), true);
  assert.equal(byId('focus-photo').dataset.path, navyPath, 'the checked variant photo');
  messageStream = noSearch;
  await say("What's the date today?", { ...settled, stock_checks: [], answer: 'Fixture answer: it is Monday, November 2, 2026, 3:15 PM at the store (America/Denver).' });
  messageStream = null;
  assert.equal(focusOpen(), true, 'small talk keeps the panel');
  assert.equal(byId('focus-photo').dataset.path, navyPath, 'small talk keeps the committed variant photo');
  assert.equal(byId('focus-photo').children[0].alt, 'Product photo of the Downpour Packable Shell in navy.');
  assert.equal(byId('stock-line').hidden, true, 'the stock line still shows only the latest result');
  await say('Add it to the cart.', { ...settled, stock_checks: [], cart: [] });
  assert.equal(byId('focus-photo').dataset.path, navyPath, 'an added line keeps the photo too');
  await say('What about the yellow one?', { ...settled, selected_variant_id: null, stock_checks: [] });
  assert.equal(byId('focus-photo').dataset.path, downpour.image_path, 'no committed variant shows the default photo');
  byId('focus-back').onclick();

  // End conversation closes it. In the storefront a click opens it as SELECTED; sign-out closes it.
  await byId('end').onclick();
  assert.equal(byId('how-live-note').textContent, 'Send a message to see a live turn.');
  assert.ok(NODE_IDS.every(id => mark(id) === 'none'), 'End clears the marks');
  assert.equal(byId('how-timings').hidden, true);
  assert.equal(focusOpen(), false, 'End closes the panel');
  assert.equal(byId('cart').hidden, true, 'End empties the cart with the session');
  cardButton(0).onclick();
  await settle();
  assert.equal(byId('focus-label').textContent, 'SELECTED');
  await byId('sign-out').onclick();
  assert.equal(focusOpen(), false, 'sign-out closes the panel');
  assert.equal(byId('how-live-note').textContent, 'Sign in and send a message to see a live turn.');
  assert.equal(byId('how-toggle').hidden, false, 'How it works stays available after sign-out');
  assert.equal(byId('focus-photo').children.length, 0);

  // A product detail that arrives after sign-out draws nothing.
  const detailGate = delayNext(url => url.startsWith('/v1/catalog/products/'));
  await signin('tenant-key-fixture');
  cardButton(0).onclick();
  await settle();
  assert.ok(detailGate.seen);
  await byId('sign-out').onclick();
  await signin('tenant-b-fixture');
  detailGate.resolve(Response.json({ ...ridgeline, tags: ['stale-tag'], images: [], catalog_version: '2026.11.1' }));
  await settle();
  cleanHarbor();
  assert.equal(focusOpen(), false);
  assert.doesNotMatch(byId('focus-tags').textContent, /stale/);
  messageResult = null;
  await byId('sign-out').onclick();
  console.log('PASS: How it works before sign-in: details for each box, tabs by click and arrow keys; the diagram follows observed events with one current mark; completed, skipped, refused, failed and not-observed states; a greeting skips the catalog; the gateway\'s own stock check is labeled; End and sign-out clear the live parts');
  console.log('PASS: a first greeting keeps the storefront; product focus opens on a click, a positional result and verified stock; stays closed for plain searches, clarifications, reply mentions and after Back; stays open through small talk; closes on Back, Escape, a new search, another explicit id, End and sign-out; late details are dropped');
  console.log('PASS: generation isolation for delayed setup, events, result, audio, diagnostics, sign-in, same-key images and microphone permission');
  console.log('PASS: client sign-in, catalog cards, reservation and message retry with the same key and body, waiting state, result cards, playback, token redaction, capacity refusal and sign-out cleanup');
})().catch(error => { console.error(error); process.exitCode = 1; });
