// The Video walkthrough tab. A recording, when the gateway serves one, is shown as recorded
// footage with the build and date it was made from, never as live activity: no autoplay, captions
// on, chapters as buttons. Without a recording, the chapter plan and a transcript placeholder.
// Browser media objects are touched only through the elements passed in.
import { el } from "./conversation.js";

export const NOT_RECORDED = "Not recorded yet. Recorded footage will show the build and date it was made from.";

// The chapter plan, the same titles as docs/walkthrough.md.
export const CHAPTERS = [
  "Conversation: start, speak, automatic stop, reply, reopen",
  "Product focus",
  "Stock and cart",
  "The architecture tab",
  "The same request's observed flow",
];

export function clockTime(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// Cue text from a WebVTT file, in order, with each cue's start in seconds. Timing lines, the header
// and NOTE blocks are dropped; voice and style tags are reduced to their text.
export function parseVtt(text) {
  const cues = [];
  for (const block of String(text || "").replace(/\r\n?/g, "\n").split(/\n{2,}/)) {
    const lines = block.split("\n").filter(Boolean);
    const at = lines.findIndex(line => line.includes("-->"));
    if (at < 0) continue;
    const match = lines[at].match(/^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})/);
    if (!match) continue;
    const start = Number(match[1] || 0) * 3600 + Number(match[2]) * 60 + Number(match[3]) + Number(match[4]) / 1000;
    const said = lines.slice(at + 1).join(" ").replace(/<[^>]*>/g, "").trim();
    if (said) cues.push({ start, text: said });
  }
  return cues;
}

// `parts`: status, player (the figure), video, track, meta, chapters (the list), transcript.
// `manifest` is walkthrough.json, or null when there is no recording; `captions` is the VTT text.
export function renderWalkthrough(parts, manifest, captions = "") {
  // Without a recording the chapter list is the plan, and its heading says so.
  if (parts.heading) parts.heading.textContent = manifest ? "Chapters" : "Planned chapters";
  if (!manifest) {
    parts.status.hidden = false;
    parts.status.textContent = NOT_RECORDED;
    parts.player.hidden = true;
    parts.chapters.replaceChildren(...CHAPTERS.map(title => el("li", "", title)));
    parts.transcript.replaceChildren(el("p", "hint", "Transcript: available with the recording."));
    return;
  }
  parts.status.hidden = true;
  parts.player.hidden = false;
  parts.meta.textContent = `on ${manifest.recorded} from build ${manifest.build}`;
  parts.video.setAttribute("preload", "metadata");
  parts.video.removeAttribute("autoplay");
  parts.track.setAttribute("src", "/app/walkthrough/walkthrough.vtt");
  parts.track.setAttribute("default", "");
  parts.video.setAttribute("src", "/app/walkthrough/walkthrough.mp4");
  // Captions start on; the shopper can turn them off in the player.
  const tracks = parts.video.textTracks;
  if (tracks && tracks[0]) tracks[0].mode = "showing";
  const buttons = manifest.chapters.map(chapter => {
    const button = el("button", "chapter");
    button.type = "button";
    button.append(el("span", "at", clockTime(chapter.start)), el("span", "", chapter.title));
    button.setAttribute("aria-label", `Play from ${clockTime(chapter.start)}, ${chapter.title}`);
    // A chapter is the shopper's own choice to watch, so it plays from there.
    button.onclick = () => {
      parts.video.scrollIntoView?.({ block: "nearest" });
      parts.video.currentTime = chapter.start;
      Promise.resolve(parts.video.play?.()).catch(() => {});
    };
    const item = el("li");
    item.append(button);
    return { item, button, start: chapter.start };
  });
  parts.chapters.replaceChildren(...buttons.map(entry => entry.item));
  // The chapter being watched, from the player's own time updates.
  parts.video.ontimeupdate = () => {
    const now = parts.video.currentTime;
    const showing = buttons.filter(entry => entry.start <= now).at(-1);
    for (const entry of buttons) {
      if (entry === showing) entry.button.setAttribute("aria-current", "true");
      else entry.button.removeAttribute("aria-current");
    }
  };
  const cues = parseVtt(captions);
  parts.transcript.replaceChildren(...(cues.length
    ? cues.map(cue => {
      const line = el("p");
      line.append(el("span", "at", clockTime(cue.start)), el("span", "", cue.text));
      return line;
    })
    : [el("p", "hint", "Transcript unavailable: the captions file could not be read.")]));
}
