'use strict';
// Рисунок живой карты работы (страница /puls). Ванильный JS без внешних загрузок.
// Строки данных кладутся ТОЛЬКО текстовыми узлами (textContent) и createElementNS:
// имя задачи или путь файла приходят из журналов и могут нести разметку.
// Под node модуль отдаёт функции для пробы и ничего не запускает; в браузере
// стартует сам и рисует в #karta.
(function (factory) {
  const lib = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = lib;
  else lib.start(window, document);
}(function () {
  const SVG = 'http://www.w3.org/2000/svg';
  const LOGIN = 'Войдите: нажмите «Карта» в чате ещё раз';
  const NOT_BUILT = 'Карта ещё не собрана: сборщик не запускался';
  const NO_REGISTRY = 'Реестра частей нет: деление на части ещё не влито';
  const STATE_COLOR = { error: '#ef4444', refused: '#eab308', active: '#22c55e', quiet: '#4b5563' };
  const EDGE_COLOR = { dashed: '#6b7280', idle: '#4b5563', live: '#22c55e' };
  const POLL_MS = 5000;
  const MAX_PAUSE_MS = 60000;
  const DOT_MS = 1500;
  const FLASH_MS = 600;
  // Ширина корня, а не экрана: телефон 412 даёт ~376, планшет боком — больше 600.
  const NARROW_PX = 600;
  // Внешние по бокам сетки — только там, где блоки частей не сожмутся до нечитаемых.
  const SIDES_PX = 1000;
  // Сборщик токенов пишет раз в час, строки отстают до ~70 мин (спека §4):
  // давность больше — уже не задержка, а остановка источника.
  const TOKENS_STALE_S = 75 * 60;
  const PAD = 12;
  const GRID_TOP = 112;
  const EXT_W = 96;
  const EXT_H = 34;
  const ARROW_GAP = 3;

  // ── разметка ───────────────────────────────────────────────────────────
  function el(doc, tag, attrs, text) {
    const node = doc.createElementNS(SVG, tag);
    for (const key of Object.keys(attrs || {})) node.setAttribute(key, attrs[key]);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function label(doc, x, y, text, attrs) {
    return el(doc, 'text', Object.assign({ x, y }, attrs), text);
  }

  function clip(text, max) {
    const s = String(text === null || text === undefined ? '' : text);
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  }

  // ── числа и время для человека ─────────────────────────────────────────
  function pad2(n) { return String(n).padStart(2, '0'); }

  // Часы зрителя, а не строки: смещение у снимка и у токенов бывает разным.
  function localTime(iso) {
    const d = new Date(Date.parse(iso));
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function decimal(x) { return Number(x || 0).toFixed(1).replace('.', ','); }

  function humanCount(n) {
    if (n >= 1e6) return `${decimal(n / 1e6)} млн`;
    if (n >= 1e3) return `${Math.round(n / 1e3)} тыс`;
    return String(n);
  }

  function humanAge(seconds) {
    const minutes = Math.floor(seconds / 60);
    if (minutes < 1) return 'меньше минуты';
    if (minutes < 60) return `${minutes} мин`;
    const rest = minutes % 60;
    return rest ? `${Math.floor(minutes / 60)} ч ${rest} мин` : `${Math.floor(minutes / 60)} ч`;
  }

  function secondsSince(iso, now) { return (now - Date.parse(iso)) / 1000; }

  // ── что сказать, когда карты нет ───────────────────────────────────────
  function staleSeconds(body) {
    return Math.round((Date.now() - Date.parse(body['снято'])) / 1000);
  }

  function stateText(response) {
    if (response.status === 401) return LOGIN;
    if (response.status === 503) return NOT_BUILT;
    const body = response.body || {};
    if (body['беда'] === 'нет architecture.yaml') return NO_REGISTRY;
    if (body['устарел']) return `данные устарели: ${staleSeconds(body)} с`;
    return null;
  }

  // Пауза опроса: 429 удваивает её до потолка, любой другой ответ — обычный ритм.
  function nextDelay(prev, status) {
    return status === 429 ? Math.min(prev * 2, MAX_PAUSE_MS) : POLL_MS;
  }

  // Сравнивается момент времени, а не строка: смещение пояса у событий может быть разным.
  function newEvents(snapshot, lastTs) {
    const last = lastTs ? Date.parse(lastTs) : -Infinity;
    return (snapshot['события'] || []).filter((e) => Date.parse(e.ts) > last);
  }

  // Самое позднее ts среди событий (порядок в снимке не гарантирован).
  function latest(events, fallback) {
    return events.reduce((best, e) => (Date.parse(e.ts) > Date.parse(best) ? e.ts : best), fallback);
  }

  // ── состояние части и раскладка ────────────────────────────────────────
  function partState(part) {
    const outcomes = part['исходы'] || {};
    if (outcomes.error > 0) return 'error';
    if (outcomes.refused > 0) return 'refused';
    return part['событий'] > 0 ? 'active' : 'quiet';
  }

  function edgeKind(edge) {
    if (!edge['источник']) return 'dashed';
    return edge['событий'] > 0 ? 'live' : 'idle';
  }

  function box(x, y, w, h) { return { x, y, w, h, cx: x + w / 2, cy: y + h / 2 }; }

  // Сетка 2×4 на узком экране, 4×2 на широком; inset — место внешних по бокам.
  function layoutParts(parts, width, top, inset) {
    const narrow = width <= NARROW_PX;
    const cols = narrow ? 2 : 4;
    const gapX = narrow ? 16 : 40;
    const gapY = 44;
    const cellH = 84;
    const left = PAD + inset;
    const cellW = (width - 2 * left - gapX * (cols - 1)) / cols;
    const cells = parts.map((part, i) => {
      const row = Math.floor(i / cols);
      const col = i % cols;
      return Object.assign({ part, row, col, narrow }, box(left + col * (cellW + gapX), top + row * (cellH + gapY), cellW, cellH));
    });
    const rows = Math.ceil(parts.length / cols);
    return { cells, bottom: top + Math.max(rows, 1) * (cellH + gapY) - gapY };
  }

  // Часть, с которой внешний связан ребром: рядом с ней он и стоит.
  function neighbourCell(snapshot, id, cells) {
    for (const edge of snapshot['рёбра'] || []) {
      const other = edge['от'] === id ? edge['к'] : edge['к'] === id ? edge['от'] : null;
      const cell = cells.find((c) => c.part.id === other);
      if (cell) return cell;
    }
    return cells[0] || null;
  }

  // Порядок внешних в снимке — вход и выход: чётные (Владелец) — слева или
  // над сеткой, нечётные (Копии) — справа или под ней (§3.1, §9).
  function layoutExternals(snapshot, cells, width, sides, aboveY, belowY) {
    return (snapshot['внешние'] || []).map((ext, i) => {
      const first = i % 2 === 0;
      const cell = neighbourCell(snapshot, ext.id, cells);
      if (sides) {
        const cy = cell ? cell.cy : GRID_TOP + 42;
        return Object.assign({ ext }, box(first ? PAD : width - PAD - EXT_W, cy - EXT_H / 2, EXT_W, EXT_H));
      }
      const w = Math.min(150, width / 2 - 2 * PAD);
      const cx = cell ? cell.cx : width / 2;
      return Object.assign({ ext }, box(cx - w / 2, first ? aboveY : belowY, w, EXT_H));
    });
  }

  function layout(snapshot, width) {
    const sides = width >= SIDES_PX;
    const hasExternals = (snapshot['внешние'] || []).length > 0;
    const top = sides || !hasExternals ? GRID_TOP : GRID_TOP + EXT_H + 24;
    const grid = layoutParts(snapshot['части'] || [], width, top, sides ? EXT_W + 24 : 0);
    const baselineY = grid.bottom + 14;
    const externals = layoutExternals(snapshot, grid.cells, width, sides, GRID_TOP, baselineY + 16);
    const bottom = Math.max(baselineY, ...externals.map((b) => b.y + b.h));
    return { cells: grid.cells, externals, baselineY, bottom, inset: sides ? EXT_W + 24 : 0 };
  }

  // ── полоса нагрузки ────────────────────────────────────────────────────
  function drawStrip(doc, svg, snapshot, width, now) {
    const machine = snapshot['машина'] || {};
    const seats = machine['места'] || {};
    const ratio = (machine.load1 || 0) / (machine['ядер'] || 1);
    const barW = width - 2 * PAD;
    svg.appendChild(label(doc, PAD, 16, `Нагрузка ${decimal(machine.load1)} из ${machine['ядер']}`));
    if (snapshot['снято']) svg.appendChild(label(doc, width - PAD, 16, `снимок ${localTime(snapshot['снято'])}`, { 'text-anchor': 'end', class: 'тихо' }));
    svg.appendChild(el(doc, 'rect', { x: PAD, y: 22, width: barW, height: 10, rx: 5, fill: '#252932' }));
    const color = ratio > 1 ? '#ef4444' : ratio > 0.7 ? '#eab308' : '#22c55e';
    svg.appendChild(el(doc, 'rect', { x: PAD, y: 22, width: Math.max(2, barW * Math.min(ratio, 1)), height: 10, rx: 5, fill: color, 'data-load': ratio.toFixed(2) }));
    svg.appendChild(label(doc, PAD, 52, `Места ${seats['занято']}/${seats['всего']}`));
    for (let i = 0; i < (seats['всего'] || 0); i += 1) {
      svg.appendChild(el(doc, 'rect', { x: 100 + i * 16, y: 42, width: 12, height: 12, rx: 3, fill: i < seats['занято'] ? '#7c5cff' : '#252932' }));
    }
    drawTrouble(doc, svg, snapshot, width);
    svg.appendChild(label(doc, PAD, 74, tokensText(snapshot['токены'], now), { class: 'тихо' }));
  }

  function tokensText(tokens, now) {
    if (!tokens) return 'Токены: нет данных';
    const age = secondsSince(tokens['до'], now);
    const mark = age > TOKENS_STALE_S ? ' · токены устарели' : '';
    return `Токены ${humanCount(tokens['за_час'])} за час до ${localTime(tokens['до']).slice(0, 5)} · ${humanAge(age)} назад${mark}`;
  }

  // Значок беды источников у полосы; что именно сломано — в подсказке (§5.6 п. 4).
  function drawTrouble(doc, svg, snapshot, width) {
    const sick = (snapshot['источники'] || []).filter((s) => s['беда']);
    if (!sick.length) return;
    const g = el(doc, 'g', { 'data-trouble': sick.length });
    g.appendChild(label(doc, width - PAD, 52, `⚠ источников с бедой: ${sick.length}`, { 'text-anchor': 'end', style: 'fill:#eab308' }));
    g.appendChild(el(doc, 'title', {}, sick.map((s) => `${s['имя']}: ${s['беда']}`).join('\n')));
    svg.appendChild(g);
  }

  // Строка под полосой: тишина окна (§5.6 п. 5) и события без места (§5.5).
  function drawStatus(doc, svg, snapshot, width) {
    if (!(snapshot['события'] || []).length) svg.appendChild(label(doc, PAD, 94, 'за минуту событий не было', { class: 'тихо' }));
    const placeless = (snapshot['без_места'] || {})['событий'] || 0;
    const g = el(doc, 'g', { 'data-placeless': placeless });
    g.appendChild(label(doc, width - PAD, 94, `событий без места: ${placeless}`, { 'text-anchor': 'end', class: 'тихо' }));
    svg.appendChild(g);
  }

  // ── части, рёбра, внешние ──────────────────────────────────────────────
  function drawPart(doc, svg, cell) {
    const part = cell.part;
    const state = partState(part);
    const g = el(doc, 'g', { 'data-part': part.id, 'data-state': state, 'data-row': cell.row, 'data-col': cell.col });
    g.appendChild(el(doc, 'rect', { x: cell.x, y: cell.y, width: cell.w, height: cell.h, rx: 12, fill: '#17191f', stroke: STATE_COLOR[state], 'stroke-width': 2 }));
    g.appendChild(label(doc, cell.x + 12, cell.y + 24, part['имя'], { 'font-weight': 600, 'font-size': 14 }));
    // На узком экране «механизмов» не влезает в блок: оставляем главное — число событий.
    const counts = cell.narrow ? `событий ${part['событий']}` : `событий ${part['событий']} · механизмов ${part['механизмов']}`;
    g.appendChild(label(doc, cell.x + 12, cell.y + 44, counts, { class: 'тихо' }));
    const last = part['последнее'];
    g.appendChild(label(doc, cell.x + 12, cell.y + 64, last ? clip(last['событие'], 28) : 'тишина', { class: 'тихо' }));
    svg.appendChild(g);
  }

  function drawExternal(doc, svg, ext) {
    const g = el(doc, 'g', { 'data-external': ext.ext.id });
    g.appendChild(el(doc, 'rect', { x: ext.x, y: ext.y, width: ext.w, height: ext.h, rx: 17, fill: '#1d2028', stroke: '#7c5cff', 'stroke-dasharray': '2 3' }));
    g.appendChild(label(doc, ext.cx, ext.cy + 4, ext.ext['имя'], { 'text-anchor': 'middle' }));
    svg.appendChild(g);
  }

  // Стрелка на конце ребра — по маркеру на каждый вид (цвет маркера — цвет ребра).
  function drawMarkers(doc, svg) {
    const defs = el(doc, 'defs', {});
    for (const kind of Object.keys(EDGE_COLOR)) {
      const marker = el(doc, 'marker', { id: `arrow-${kind}`, viewBox: '0 0 8 8', refX: 8, refY: 4, markerWidth: 7, markerHeight: 7, markerUnits: 'userSpaceOnUse', orient: 'auto' });
      marker.appendChild(el(doc, 'path', { d: 'M0 0 L8 4 L0 8 z', fill: EDGE_COLOR[kind] }));
      defs.appendChild(marker);
    }
    svg.appendChild(defs);
  }

  // Точка выхода из блока к цели p: край прямоугольника плюс зазор под стрелку.
  // Цель внутри блока (соседи вплотную) — тогда ведём к центру другого конца.
  function exitPoint(from, p, fallback) {
    let dx = p.x - from.cx;
    let dy = p.y - from.cy;
    let t = Math.min(dx ? from.w / 2 / Math.abs(dx) : Infinity, dy ? from.h / 2 / Math.abs(dy) : Infinity);
    if (t >= 1 && fallback) return exitPoint(from, fallback, null);
    if (!Number.isFinite(t)) t = 0;
    const len = Math.hypot(dx, dy) || 1;
    return { x: from.cx + dx * t + (dx / len) * ARROW_GAP, y: from.cy + dy * t + (dy / len) * ARROW_GAP };
  }

  // Кривая Безье с изгибом вбок: прямая центр-в-центр легла бы на соседние блоки.
  function curve(from, to) {
    const dx = to.cx - from.cx;
    const dy = to.cy - from.cy;
    const bend = 0.18;
    const c = { x: (from.cx + to.cx) / 2 - dy * bend, y: (from.cy + to.cy) / 2 + dx * bend };
    const a = exitPoint(from, c, { x: to.cx, y: to.cy });
    const b = exitPoint(to, c, { x: from.cx, y: from.cy });
    return { a, c, b };
  }

  function curvePath(k) {
    const r = (n) => Math.round(n * 10) / 10;
    return `M${r(k.a.x)} ${r(k.a.y)} Q${r(k.c.x)} ${r(k.c.y)} ${r(k.b.x)} ${r(k.b.y)}`;
  }

  function curvePoint(k, t) {
    const u = 1 - t;
    return {
      x: u * u * k.a.x + 2 * u * t * k.c.x + t * t * k.b.x,
      y: u * u * k.a.y + 2 * u * t * k.c.y + t * t * k.b.y,
    };
  }

  function drawEdge(doc, svg, edge, index, geometry) {
    const kind = edgeKind(edge);
    const attrs = { d: curvePath(geometry), fill: 'none', stroke: EDGE_COLOR[kind], 'marker-end': `url(#arrow-${kind})`, 'data-edge': index, 'data-kind': kind };
    if (kind === 'dashed') Object.assign(attrs, { 'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: 0.6 });
    else if (kind === 'idle') Object.assign(attrs, { 'stroke-width': 1, opacity: 0.8 });
    else Object.assign(attrs, { 'stroke-width': 1 + Math.min(edge['событий'], 4) * 0.6 });
    const path = el(doc, 'path', attrs);
    path.appendChild(el(doc, 'title', {}, edge['что'] || ''));
    svg.appendChild(path);
  }

  // Луч «всем» (к: «*») идёт от источника вниз к общей линии под сеткой.
  function edgeGeometries(snapshot, cells, externals, baselineY) {
    const anchors = new Map();
    for (const cell of cells) anchors.set(cell.part.id, cell);
    for (const ext of externals) anchors.set(ext.ext.id, ext);
    return (snapshot['рёбра'] || []).map((edge) => {
      const from = anchors.get(edge['от']);
      if (!from) return null;
      if (edge['к'] === '*') return curve(from, box(from.cx, baselineY, 0, 0));
      const to = anchors.get(edge['к']);
      return to ? curve(from, to) : null;
    });
  }

  // ── дерево исполнителей ────────────────────────────────────────────────
  // Порядок обхода: корни, затем дети каждого; родитель не из списка — корень.
  function agentRows(agents) {
    const known = new Set(agents.map((a) => a['вызов']));
    const children = new Map();
    for (const agent of agents) {
      const parent = known.has(agent['родитель']) ? agent['родитель'] : '';
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(agent);
    }
    const rows = [];
    const walk = (parent, depth) => {
      for (const agent of children.get(parent) || []) {
        rows.push({ agent, depth });
        walk(agent['вызов'], depth + 1);
      }
    };
    walk('', 0);
    return rows;
  }

  // Возраст и место исполнителя: справа на широком, второй строкой на узком.
  function agentMeta(agent, partNames, now) {
    const age = agent['с'] ? humanAge(secondsSince(agent['с'], now)) : '';
    const place = agent['часть'] ? partNames.get(agent['часть']) || agent['часть'] : 'без места';
    return [age, place].filter(Boolean).join(' · ');
  }

  function drawAgents(doc, svg, snapshot, y, width, now) {
    const narrow = width <= NARROW_PX;
    const partNames = new Map((snapshot['части'] || []).map((p) => [p.id, p['имя']]));
    svg.appendChild(label(doc, PAD, y, 'Исполнители', { 'font-weight': 600, 'font-size': 14 }));
    let rowY = y + 24;
    for (const row of agentRows(snapshot['исполнители'] || [])) {
      const a = row.agent;
      const g = el(doc, 'g', { 'data-agent': a['вызов'], 'data-parent': a['родитель'] || '' });
      const x = PAD + row.depth * 18;
      const meta = agentMeta(a, partNames, now);
      const room = narrow ? width - x - 14 - PAD : width - x - 14 - PAD - meta.length * 7 - 24;
      // На узком задача одна в первой строке: тип и модель у всех одинаковы и
      // съедали её имя, а по имени задачи владелец узнаёт исполнителя.
      const head = narrow ? a['задача'] : `${a['тип']} · ${a['модель']} · ${a['задача']}`;
      const tail = narrow ? `${a['тип']} · ${a['модель']} · ${meta}` : meta;
      g.appendChild(el(doc, 'circle', { cx: x + 4, cy: rowY - 4, r: 4, fill: '#7c5cff' }));
      g.appendChild(label(doc, x + 14, rowY, clip(head, Math.floor(room / 7))));
      if (narrow) g.appendChild(label(doc, x + 14, rowY + 16, tail, { class: 'тихо' }));
      else g.appendChild(label(doc, width - PAD, rowY, tail, { 'text-anchor': 'end', class: 'тихо' }));
      svg.appendChild(g);
      rowY += narrow ? 38 : 22;
    }
    return rowY;
  }

  // ── сцена: то, по чему потом бегут точки ───────────────────────────────
  let scene = { geometries: [], cells: new Map(), svg: null, doc: null };

  function render(doc, root, snapshot, now) {
    const at = now === undefined ? Date.now() : now;
    root.textContent = '';
    const width = root.clientWidth || 1200;
    const svg = el(doc, 'svg', { xmlns: SVG });
    if (snapshot['устарел']) svg.setAttribute('class', 'stale');
    const plan = layout(snapshot, width);
    const geometries = edgeGeometries(snapshot, plan.cells, plan.externals, plan.baselineY);

    drawMarkers(doc, svg);
    drawStrip(doc, svg, snapshot, width, at);
    drawStatus(doc, svg, snapshot, width);
    (snapshot['рёбра'] || []).forEach((edge, i) => { if (geometries[i]) drawEdge(doc, svg, edge, i, geometries[i]); });
    if ((snapshot['рёбра'] || []).some((e) => e['к'] === '*')) {
      const left = PAD + plan.inset;
      const right = width - PAD - plan.inset;
      svg.appendChild(el(doc, 'line', { x1: left, y1: plan.baselineY, x2: right, y2: plan.baselineY, stroke: '#4b5563', 'stroke-dasharray': '2 3' }));
      svg.appendChild(label(doc, right, plan.baselineY - 4, 'всем', { 'text-anchor': 'end', class: 'тихо' }));
    }
    plan.cells.forEach((cell) => drawPart(doc, svg, cell));
    plan.externals.forEach((ext) => drawExternal(doc, svg, ext));
    const end = drawAgents(doc, svg, snapshot, plan.bottom + 40, width, at);
    svg.setAttribute('viewBox', `0 0 ${width} ${end + 8}`);
    root.appendChild(svg);
    scene = { geometries, cells: new Map(plan.cells.map((c) => [c.part.id, c])), svg, doc };
  }

  // ── анимация: точка по ребру ~1,5 с и вспышка части 600 мс ──────────────
  function actorsFor(events) {
    const { geometries, cells, svg, doc } = scene;
    const actors = [];
    for (const event of events) {
      const path = geometries[event['ребро']];
      if (path) {
        const dot = el(doc, 'circle', { r: 4, fill: '#22c55e', cx: path.a.x, cy: path.a.y });
        svg.appendChild(dot);
        actors.push({ kind: 'dot', node: dot, curve: path, ms: DOT_MS });
      }
      const cell = cells.get(event['часть']);
      if (cell) {
        const color = STATE_COLOR[event['исход'] === 'error' ? 'error' : 'active'];
        const glow = el(doc, 'rect', { x: cell.x, y: cell.y, width: cell.w, height: cell.h, rx: 12, fill: color, opacity: 0.5 });
        svg.appendChild(glow);
        actors.push({ kind: 'flash', node: glow, ms: FLASH_MS });
      }
    }
    return actors;
  }

  // true — актёр ещё живёт; по окончании узел убирается из рисунка.
  function stepActor(actor, t) {
    if (t >= actor.ms) {
      if (actor.node.parentNode) actor.node.parentNode.removeChild(actor.node);
      return false;
    }
    const k = t / actor.ms;
    if (actor.kind === 'dot') {
      const p = curvePoint(actor.curve, k);
      actor.node.setAttribute('cx', p.x);
      actor.node.setAttribute('cy', p.y);
    } else {
      actor.node.setAttribute('opacity', (0.5 * (1 - k)).toFixed(2));
    }
    return true;
  }

  function startAnimation(win, events) {
    const actors = actorsFor(events);
    if (!actors.length) return;
    const now = () => (win.performance ? win.performance.now() : Date.now());
    const born = now();
    const frame = () => {
      const alive = actors.filter((a) => stepActor(a, now() - born));
      if (alive.length) win.requestAnimationFrame(frame);
    };
    win.requestAnimationFrame(frame);
  }

  // ── браузер: опрос ─────────────────────────────────────────────────────
  function showText(doc, root, text) {
    root.textContent = '';
    const p = doc.createElement('p');
    p.textContent = text;
    root.appendChild(p);
  }

  // Плашка над нарисованной картой: своя узкая строка, карту не сдвигает.
  function showBanner(doc, root, text) {
    const p = doc.createElement('p');
    p.setAttribute('class', 'banner');
    p.textContent = text;
    root.insertBefore(p, root.firstChild);
  }

  function start(win, doc) {
    const root = doc.getElementById('karta');
    const tg = new URLSearchParams((win.location.hash || '').slice(1)).get('tgWebAppData');
    let delay = POLL_MS;
    let timer = null;
    let lastTs = null;

    // Первый снимок не анимируется: «новым» в нём было бы всё окно событий.
    function draw(body, text) {
      render(doc, root, body);
      const fresh = newEvents(body, lastTs);
      if (lastTs !== null) startAnimation(win, fresh);
      lastTs = latest(fresh, lastTs || body['снято']);
      if (text) showBanner(doc, root, text);
    }

    // Любой снимок 200 рисуется: без частей видны полоса, токены и исполнители,
    // а беда — плашкой рядом (план, Общие ограничения).
    function apply(status, body) {
      const text = stateText({ status, body });
      if (status === 200) draw(body, text);
      else showText(doc, root, text || '');
    }

    async function poll() {
      timer = null;
      if (doc.hidden) return;
      const headers = tg ? { Authorization: 'tma ' + tg } : {};
      try {
        const res = await win.fetch('/puls/snimok', { headers, credentials: 'same-origin' });
        const body = await res.json();
        delay = nextDelay(delay, res.status);
        apply(res.status, body);
      } catch (error) {
        delay = nextDelay(delay, 429);
        showText(doc, root, 'Нет связи с панелью — пробую снова');
      }
      timer = win.setTimeout(poll, delay);
    }

    doc.addEventListener('visibilitychange', () => {
      if (doc.hidden) {
        if (timer) win.clearTimeout(timer);
        timer = null;
      } else if (!timer) poll();
    });
    poll();
  }

  return { render, stateText, nextDelay, newEvents, start };
}));
