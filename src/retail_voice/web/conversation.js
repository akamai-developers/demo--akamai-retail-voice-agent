// Conversation display: product cards, transcript, stock facts, the microphone level and reply
// playback. It renders server-resolved data and recorded audio levels only. Network calls stay
// in transport.js.
import { meterLevel, smoothLevel } from "./vad.js";

export const byId = id => document.getElementById(id);

// Redraw the level bar at most this often, in milliseconds of captured audio.
const METER_DRAW_MS = 33;

// The microphone level bar. Every value comes from a recorded block's RMS; nothing animates on
// its own. Hidden whenever the microphone is closed.
export class LevelMeter {
  constructor(meter, fill) {
    this.meter = meter;
    this.fill = fill;
    this.close();
  }

  open() {
    this.close();
    this.meter.hidden = false;
  }

  update(rms, audioMs) {
    const elapsed = this.at === null ? 0 : audioMs - this.at;
    this.value = smoothLevel(this.value, meterLevel(rms), elapsed);
    this.at = audioMs;
    if (audioMs - this.drawn < METER_DRAW_MS) return;
    this.drawn = audioMs;
    this.draw(Math.round(this.value * 100));
  }

  draw(percent) {
    this.fill.style.width = `${percent}%`;
    this.meter.setAttribute("aria-valuenow", String(percent));
  }

  close() {
    this.value = 0;
    this.at = null;
    this.drawn = -Infinity;
    this.draw(0);
    this.meter.hidden = true;
  }
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function variantSummary(card) {
  const sizes = card.sizes.join(", ");
  return `${card.colors.join(", ")} · ${sizes}`;
}

// Cards come from GET /v1/catalog or from a turn result. Images load through `loadImage`,
// which fetches with the retailer key and returns an object URL, or null on failure.
export function renderCards(list, cards, { selected, numbered, onSelect, loadImage }) {
  const items = cards.map((card, index) => {
    const item = el("li");
    const button = el("button", "card");
    button.type = "button";
    button.setAttribute("aria-pressed", String(card.product_id === selected));
    button.setAttribute("data-product", card.product_id);
    const frame = el("span", "frame");
    const placeholder = () => frame.replaceChildren(el("span", "placeholder", card.name));
    placeholder();
    loadImage(card.image_path).then(url => {
      if (!url) return;
      const img = el("img");
      img.alt = card.image_alt;
      img.onerror = placeholder;
      img.src = url;
      frame.replaceChildren(img);
      if (numbered) frame.append(el("span", "position", String(index + 1)));
    });
    if (numbered) frame.append(el("span", "position", String(index + 1)));
    button.append(
      frame,
      el("span", "name", card.name),
      el("span", "price", card.price_display),
      el("span", "variants", variantSummary(card)),
    );
    if (card.product_id === selected) button.append(el("span", "chosen", "SELECTED"));
    button.onclick = () => onSelect(card);
    item.append(button);
    return item;
  });
  if (!items.length) {
    const empty = el("li", "empty-cards", "No products matched that request.");
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...items);
}

// A positional reference to the cards on screen, the same patterns conversation.py resolves
// on the server: "the second one", "the first jacket", "option 2". A bare "first" is not one.
const NOUNS = "one|ones|item|option|product|card|choice|jacket|shell|anorak|fleece|hoodie|vest|hat|cap|beanie|glove|gloves|mitts|pair";
const ORDINAL = new RegExp(`\\b(?:first|second|third|last|1st|2nd|3rd)\\s+(?:${NOUNS})\\b`, "i");
const NUMBERED = /\b(?:option|number|item|choice)\s+(?:one|two|three|1|2|3)\b/i;

export function positional(text) {
  return ORDINAL.test(text || "") || NUMBERED.test(text || "");
}

// Product and variant ids the shopper typed or spoke, read the way stock.py reads them: a whole
// id token ("jacket-b", "a-drizzle-anorak-navy-m", the unknown "jacket-a-123"), or speech
// recognition's "product ID jacket B".
const PRODUCT_ID = /\b(?:jacket-[ab](?:-[a-z0-9]+)*|[ab]-[a-z0-9]+(?:-[a-z0-9]+)+)\b/gi;
const SPOKEN_ID = /(\b(?:product\s+)?id\s+|\bproduct\s+)jacket[\s-]*([ab])\b/gi;

export function namedIds(text) {
  const typed = String(text || "").replace(SPOKEN_ID, (_, lead, letter) => `${lead}jacket-${letter.toLowerCase()}`);
  return [...new Set((typed.match(PRODUCT_ID) || []).map(id => id.toLowerCase()))];
}

// The large in-page view of one catalog product. `parts` are the panel's elements.
// It shows the card's facts at once and the tags once the product detail arrives. The photo is
// the checked variant's when a verified stock fact names one, otherwise the variant the gateway
// committed for this product (`variantId`, from a check or a cart line in an earlier turn), so a
// greeting or a date question keeps the navy photo (N1), otherwise the product's first.
export function renderFocus(parts, card, { label, detail, check, variantId, loadImage }) {
  parts.label.textContent = label;
  parts.name.textContent = card.name;
  parts.price.textContent = card.price_display;
  parts.description.textContent = (detail && detail.description) || card.description;
  parts.colors.textContent = card.colors.join(", ");
  parts.sizes.textContent = card.sizes.join(", ");
  parts.tags.replaceChildren(...((detail && detail.tags) || []).map(tag => el("li", "", tag.replace(/-/g, " "))));
  const chosen = (check && check.variant_id) || variantId;
  const variant = chosen ? card.variants.find(v => v.variant_id === chosen) : null;
  const path = (variant && variant.image_path) || card.image_path;
  if (parts.photo.dataset.path === path) return;
  parts.photo.dataset.path = path;
  const shown = ((detail && detail.images) || []).find(image => image.image_path === path);
  const alt = shown ? shown.alt : path === card.image_path ? card.image_alt : `Product photo of the ${card.name} in ${variant.color}.`;
  const placeholder = () => parts.photo.replaceChildren(el("span", "placeholder", card.name));
  placeholder();
  loadImage(path).then(url => {
    if (!url || parts.photo.dataset.path !== path) return;
    const img = el("img");
    img.alt = alt;
    img.onerror = placeholder;
    img.src = url;
    parts.photo.replaceChildren(img);
  });
}

export function addEntry(list, who, text, kind) {
  const item = el("li", kind);
  item.append(el("span", "who", who), el("span", "said", text));
  list.append(item);
  // Scroll the transcript box itself, never the page, so the buttons, the state line and the
  // level bar above it stay where they are.
  if (typeof list.scrollHeight === "number") list.scrollTop = list.scrollHeight;
}

export function stockSentence(check) {
  const where = check.store_name;
  const variant = `${check.color} ${check.size}`;
  return check.quantity > 0
    ? `${check.product_name}, ${variant}: ${check.quantity} in stock at ${where}.`
    : `${check.product_name}, ${variant}: out of stock at ${where}.`;
}

// The session cart, rendered from the gateway's committed line items only. Hidden when
// empty. `parts` are the cart section, its badge, its list and its total line.
export function renderCart(parts, cart) {
  const count = cart.reduce((sum, item) => sum + item.quantity, 0);
  parts.section.hidden = count === 0;
  if (count === 0) {
    parts.items.replaceChildren();
    parts.badge.textContent = "";
    parts.total.textContent = "";
    return;
  }
  parts.badge.textContent = `${count} ${count === 1 ? "ITEM" : "ITEMS"}`;
  parts.items.replaceChildren(...cart.map(item => {
    const line = el("li");
    line.append(
      el("span", "name", `${item.product_name}, ${item.color} ${item.size}`),
      el("span", "price", item.quantity > 1 ? `${item.quantity} × ${item.price_display}` : item.price_display),
    );
    return line;
  }));
  const cents = cart.reduce((sum, item) => sum + item.price_minor * item.quantity, 0);
  parts.total.textContent = `Total $${Math.floor(cents / 100).toLocaleString("en-US")}.${String(cents % 100).padStart(2, "0")}`;
}

// One reply at a time. The object URL is revoked whenever the reply is replaced or cleared.
export class Player {
  constructor(audio) {
    this.audio = audio;
    this.url = null;
    this.muted = false;
  }

  load(blobValue) {
    this.clear();
    this.url = URL.createObjectURL(blobValue);
    this.audio.src = this.url;
  }

  async play() {
    if (!this.url) return false;
    this.audio.muted = this.muted;
    await this.audio.play();
    return true;
  }

  get playing() {
    return !!this.url && !this.audio.paused && !this.audio.ended;
  }

  stop() {
    this.audio.pause();
    this.audio.currentTime = 0;
  }

  clear() {
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null;
  }
}
