// Illustrative target architecture only. Timings are presentation pacing, never live telemetry.
// The app controls visibility; this module owns playback and local SVG highlights.
export function createArchitectureFlow(root) {
  'use strict';
  const svg = root.querySelector?.('#platform-diagram');
  if (!svg) return { setVisible() {}, setInfo() {} };
  const ns = 'http://www.w3.org/2000/svg';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const $ = (id) => root.querySelector(`#flow-${id}`);
  const edges = {
    shopper: 'M176 650H292', entry: 'M330 610V295H565',
    policy: 'M650 295H800', selector: 'M880 295H1070',
    candidateB: 'M1150 295H1380V255H1450', candidateC: 'M1150 295H1380V365H1450',
    reserve: 'M1150 295H1320V425H630V570H740V600',
    firewall: 'M368 650H700', https: 'M780 650H842', gateway: 'M900 650H992',
    asr: 'M1168 650H1302', reasoning: 'M1168 650H1258V605H1630V615',
    tools: 'M1105 700V755', speech: 'M1168 650H1258V800H1302',
    tryon: 'M1168 650H1258V860H1630V845',
  };
  const boxes = {
    shopper: [94, 603, 82, 90], router: [290, 604, 80, 84],
    edge: [560, 250, 90, 90], policy: [795, 250, 90, 90], selector: [1065, 250, 90, 90],
    candidateB: [1450, 218, 340, 75], candidateC: [1450, 328, 340, 75],
    firewall: [697, 597, 86, 86], https: [837, 610, 68, 72],
    gateway: [992, 615, 176, 85], tools: [992, 755, 232, 90],
    asr: [1302, 615, 180, 88], reasoning: [1540, 615, 180, 88],
    speech: [1302, 757, 180, 88], tryon: [1540, 757, 180, 88],
  };
  const step = (label, nodes, routes, duration = 2) => ({label, nodes, routes, duration});
  const setup = [
    step('Start the session', ['shopper', 'edge'], ['shopper', 'entry']),
    step('Apply identity and policy', ['policy', 'selector'], ['policy', 'selector']),
    step('Compare eligible sites', ['selector', 'candidateB', 'candidateC'], ['candidateB', '-candidateB', 'candidateC', '-candidateC'], 2.8),
    step('Reserve the selected site', ['firewall', 'gateway'], ['reserve', 'https', 'gateway'], 2.8),
    step('Return the site endpoint', ['policy', 'shopper'], ['-gateway', '-https', '-reserve', '-selector', '-policy', '-entry', '-shopper'], 3.2),
    step('Send voice to the gateway', ['shopper', 'gateway'], ['shopper', 'firewall', 'https', 'gateway'], 2.4),
    step('Turn speech into text', ['gateway', 'asr'], ['asr', '-asr']),
  ];
  const scenarios = {
    voice: [...setup,
      step('Understand the request', ['gateway', 'reasoning'], ['reasoning', '-reasoning']),
      step('Check catalog and stock', ['gateway', 'tools'], ['tools', '-tools']),
      step('Prepare the answer', ['gateway', 'reasoning'], ['reasoning', '-reasoning']),
      step('Generate spoken audio', ['gateway', 'speech'], ['speech', '-speech']),
      step('Return the answer', ['gateway', 'shopper'], ['-gateway', '-https', '-firewall', '-shopper'], 2.8),
    ],
    tryon: [...setup,
      step('Resolve the selected item', ['gateway', 'tools'], ['tools', '-tools']),
      step('Confirm stock and reference', ['gateway', 'tools'], ['tools', '-tools']),
      step('Generate the try-on preview', ['gateway', 'tryon'], ['tryon', '-tryon'], 2.8),
      step('Generate spoken audio', ['gateway', 'speech'], ['speech', '-speech']),
      step('Return audio and preview', ['gateway', 'shopper'], ['-gateway', '-https', '-firewall', '-shopper'], 2.8),
    ],
  };

  function element(tag, attrs, parent) {
    const node = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    parent.append(node);
    return node;
  }
  let visible = false, visited = false;
  const overlay = element('g', {'aria-hidden': 'true', id: 'platform-flow-overlay'}, svg);
  const paths = Object.fromEntries(Object.entries(edges).map(([key, d]) => {
    const path = element('path', {d, class: 'flow-route', visibility: 'hidden'}, overlay);
    return [key, {path, length: path.getTotalLength()}];
  }));
  const highlights = Object.fromEntries(Object.entries(boxes).map(([key, box]) => [key,
    element('rect', {x: box[0] - 4, y: box[1] - 4, width: box[2] + 8, height: box[3] + 8, rx: 8, class: 'flow-focus', visibility: 'hidden'}, overlay),
  ]));
  const marker = element('circle', {r: 9, class: 'flow-marker', visibility: 'hidden'}, overlay);
  const caption = element('g', {'aria-hidden': 'true'}, svg);
  const kicker = element('text', {x: 64, y: 188, class: 'flow-kicker'}, caption);
  const heading = element('text', {x: 64, y: 223, class: 'flow-heading'}, caption);
  const detail = element('text', {x: 64, y: 252, class: 'flow-detail'}, caption);
  detail.textContent = 'Illustrative flow · not live';

  let scenario = 'voice', steps = scenarios.voice, elapsed = 0, playing = false, frame = 0, previousTime = 0, activeIndex = -1;
  const duration = () => steps.reduce((sum, item) => sum + item.duration, 0);
  function locate(seconds) {
    let start = 0;
    for (let i = 0; i < steps.length; i++) {
      if (seconds < start + steps[i].duration || i === steps.length - 1) return {index: i, local: Math.min(1, (seconds - start) / steps[i].duration)};
      start += steps[i].duration;
    }
  }
  function render() {
    const {index, local} = locate(elapsed);
    const current = steps[index];
    if (activeIndex !== index) {
      activeIndex = index;
      kicker.textContent = `STEP ${String(index + 1).padStart(2, '0')} / ${steps.length}`;
      heading.textContent = current.label;
      $('flow-status').textContent = `${index + 1} of ${steps.length}: ${current.label}`;
      Object.values(highlights).forEach((node) => node.setAttribute('visibility', 'hidden'));
      current.nodes.forEach((key) => highlights[key].setAttribute('visibility', 'visible'));
    }
    Object.values(paths).forEach(({path}) => path.setAttribute('visibility', 'hidden'));
    const segments = current.routes.map((name) => ({...paths[name.replace(/^-/, '')], reverse: name.startsWith('-')}));
    const total = segments.reduce((sum, item) => sum + item.length, 0);
    let distance = Math.max(0, Math.min(1, (local - 0.08) / 0.78)) * total;
    let selected = segments[segments.length - 1];
    for (const segment of segments) {
      if (distance <= segment.length) { selected = segment; break; }
      distance -= segment.length;
    }
    if (reduced.matches) {
      segments.forEach(({path}) => path.setAttribute('visibility', 'visible'));
      marker.setAttribute('visibility', 'hidden');
    } else {
      selected.path.setAttribute('visibility', 'visible');
      const p = selected.path.getPointAtLength(selected.reverse ? selected.length - distance : distance);
      marker.setAttribute('cx', p.x);
      marker.setAttribute('cy', p.y);
      marker.setAttribute('visibility', 'visible');
    }
    $('previous').disabled = index === 0;
    $('next').disabled = index === steps.length - 1;
    $('play').textContent = playing ? 'Pause' : 'Play';
    root.dataset.playing = String(playing);
    root.dataset.step = String(index + 1);
    root.dataset.scenario = scenario;
    $('play').setAttribute('aria-label', playing ? 'Pause flow' : 'Play flow');
    $('play').disabled = reduced.matches;
    $('play').title = reduced.matches ? 'Reduced motion is on. Use Previous and Next.' : '';
  }
  function tick(now) {
    if (!playing) return;
    if (previousTime) elapsed = (elapsed + Math.min((now - previousTime) / 1000, 0.15)) % duration();
    previousTime = now;
    render();
    frame = requestAnimationFrame(tick);
  }
  function pause() { playing = false; cancelAnimationFrame(frame); previousTime = 0; render(); }
  function play() {
    if (reduced.matches || !visible) { pause(); return; }
    if (playing) return;
    playing = true; previousTime = 0; render(); frame = requestAnimationFrame(tick);
  }
  function seek(seconds) { pause(); elapsed = Math.max(0, Math.min(duration() - 0.0001, seconds)); render(); }
  function setScenario(name) {
    if (!scenarios[name]) return;
    const resume = playing;
    pause(); scenario = name; steps = scenarios[name]; elapsed = 0; activeIndex = -1;
    $('scenario').value = name; render(); if (resume) play();
  }
  function move(delta) {
    const target = Math.max(0, Math.min(steps.length - 1, activeIndex + delta));
    seek(steps.slice(0, target).reduce((sum, item) => sum + item.duration, 0) + 0.001);
  }
  $('play').addEventListener('click', () => playing ? pause() : play());
  $('replay').addEventListener('click', () => { seek(0); play(); });
  $('previous').addEventListener('click', () => move(-1));
  $('next').addEventListener('click', () => move(1));
  $('scenario').addEventListener('change', (event) => setScenario(event.target.value));
  $('fullscreen').hidden = !document.fullscreenEnabled;
  $('fullscreen').addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await root.requestFullscreen();
    } catch { $('flow-status').textContent = 'Use your browser full-screen command.'; }
  });
  document.addEventListener('fullscreenchange', () => { $('fullscreen').textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen'; });
  root.addEventListener('keydown', (event) => {
    if (/INPUT|SELECT|BUTTON/.test(event.target.tagName) || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.code === 'Space') { event.preventDefault(); playing ? pause() : play(); }
    if (event.key === 'ArrowRight') { event.preventDefault(); move(1); }
    if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
  reduced.addEventListener('change', () => { pause(); render(); });
  render();
  return {
    setVisible(on) {
      visible = on;
      if (!on) pause();
      else if (!visited) { visited = true; play(); }
    },
    setInfo(info) {
      const current = info.mode === 'mock'
        ? 'Current session: local mock, no GPU inference.'
        : `Configured demo: ${info.site} (${info.region}), shared GPU services and application-level tenant separation.`;
      root.querySelector('#platform-current').textContent = `* Proposed platform layers. ${current}`;
    },
  };
}
