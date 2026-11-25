#!/usr/bin/env node
'use strict';
// Проба рисунка живой карты: harness/panel/live_map.js под node, без браузера.
//
// Спека: спека живой карты (.tasks/<задача>/спека.md) §3.1 (что на экране),
// §5.6 (состояния), §6 (опрос, анимация, безопасность рисунка), §9 (эскиз),
// К-8. План — Task 6. jsdom на машине нет, поэтому проба несёт СВОЙ подставной
// DOM: он считает созданные элементы и ловит innerHTML, insertAdjacentHTML и
// document.write как нарушение К-8.
//
// Контракт модуля (его судит проба):
//   под node — module.exports = {render, stateText, nextDelay, newEvents}, и
//   при require ничего не запускается; в браузере (нет module) — стартует сам:
//   корень — document.getElementById("karta"), опрос GET /puls/snimok.
//   render(doc, root, snapshot, now) — рисует в root (now — мс эпохи, как
//     Date.now()); повторный вызов
//     перерисовывает, а не дописывает. Ширина — root.clientWidth (стенд ставит
//     ту же ширину и окну): ≤ 600 → сетка 2×4, иначе 4×2 (телефон 412 — тоже узкий).
//   Узлы рисунка помечены атрибутами (данные, а не вид — проба судит по ним):
//     блок части       data-part="<id>" data-state="error|refused|active|quiet"
//                      data-row, data-col — место в сетке по порядку `части`;
//     ребро            data-edge="<индекс в рёбра>" data-kind="dashed|idle|live";
//                      у dashed — stroke-dasharray (на узле или внутри);
//     внешний          data-external="<id из внешние>";
//     исполнитель      data-agent="<вызов>" data-parent="<родитель или пусто>".
//   stateText({status, body}) -> строка | null; nextDelay(prev, status) -> мс;
//   newEvents(snapshot, lastTs) -> события с ts позже lastTs (по времени).
//
// Двери пробы: PANEL_SERVER_DIR — каталог, где лежит live_map.js (по
// умолчанию — каталог пробы; мутации судятся на копии); LIVE_MAP_SNAPSHOT —
// путь образца снимка (по умолчанию scripts/fixtures/live-map/snapshot-v1.json
// проекта или пакета).
//
// Прогон:
//     node harness/panel/test_live_map_js.js

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const childProcess = require('child_process');

const SERVER_DIR = process.env.PANEL_SERVER_DIR || __dirname;
const SCRIPT = path.join(SERVER_DIR, 'live_map.js');
const SNAPSHOT_CANDIDATES = [
  process.env.LIVE_MAP_SNAPSHOT,
  path.join(__dirname, '..', '..', 'scripts', 'fixtures', 'live-map', 'snapshot-v1.json'),
  path.join(__dirname, '..', 'scripts', 'fixtures', 'live-map', 'snapshot-v1.json'),
].filter(Boolean);
const LOGIN_TEXT = 'Войдите: нажмите «Карта» в чате ещё раз';
const NOT_BUILT_TEXT = 'Карта ещё не собрана: сборщик не запускался';
const NO_REGISTRY_TEXT = 'Реестра частей нет: деление на части ещё не влито';
const XSS = '<script>alert(1)</script>';

let paths = 0;
let failures = 0;
function pathOk(text) { paths += 1; console.log('ок  · ' + text); }
function pathBad(text) { paths += 1; failures += 1; console.log('ПЛОХО · ' + text); }
function judge(condition, name, otherwise) {
  if (condition) pathOk(name); else pathBad(`${name}: ${otherwise}`);
}
function finish() {
  console.log(`ЖИВАЯ-КАРТА-РИСУНОК: путей ${paths}, неудач ${failures}`);
  process.exit(failures ? 1 : 0);
}

// ── подставной DOM ─────────────────────────────────────────────────────────
// Ровно то, что нужно рисунку из спеки, плюс ловушки К-8. Не хватает API для
// честной реализации — дописать его СЮДА, а не обходить пробу.

class FakeNode {
  constructor(doc, nodeType, name, namespaceURI) {
    this.ownerDocument = doc;
    this.nodeType = nodeType;            // 1 — элемент, 3 — текст, 11 — фрагмент
    this.localName = name;
    this.namespaceURI = namespaceURI || null;
    this.childNodes = [];
    this.parentNode = null;
    this.data = '';
    this.attrs = new Map();
    this.listeners = {};
    this.style = { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } };
    const self = this;
    this.classList = {
      _list() { return (self.getAttribute('class') || '').split(/\s+/).filter(Boolean); },
      add(...names) { self.setAttribute('class', [...new Set([...this._list(), ...names])].join(' ')); },
      remove(...names) { self.setAttribute('class', this._list().filter((n) => !names.includes(n)).join(' ')); },
      toggle(n, force) {
        const on = force === undefined ? !this.contains(n) : force;
        if (on) this.add(n); else this.remove(n);
        return on;
      },
      contains(n) { return this._list().includes(n); },
    };
    this.dataset = new Proxy({}, {
      get: (_, key) => self.getAttribute('data-' + String(key).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase())) ?? undefined,
      set: (_, key, value) => { self.setAttribute('data-' + String(key).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()), value); return true; },
    });
  }
  get tagName() { return this.namespaceURI === HTML_NS ? this.localName.toUpperCase() : this.localName; }
  get nodeName() { return this.nodeType === 3 ? '#text' : this.tagName; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const list = this.parentNode.childNodes;
    return list[list.indexOf(this) + 1] || null;
  }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === this.ownerDocument.documentElement; }
  get nodeValue() { return this.nodeType === 3 ? this.data : null; }
  set nodeValue(v) { if (this.nodeType === 3) this.data = String(v); }
  get textContent() {
    return this.nodeType === 3 ? this.data : this.childNodes.map((n) => n.textContent).join('');
  }
  set textContent(v) {
    if (this.nodeType === 3) { this.data = String(v); return; }
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    const text = v === null || v === undefined ? '' : String(v);
    if (text !== '') this.appendChild(this.ownerDocument.createTextNode(text));
  }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get innerHTML() { this.ownerDocument.violation('чтение innerHTML'); return ''; }
  set innerHTML(v) { this.ownerDocument.violation('innerHTML = …'); throw new Error('innerHTML запрещён (К-8)'); }
  get outerHTML() { this.ownerDocument.violation('чтение outerHTML'); return ''; }
  set outerHTML(v) { this.ownerDocument.violation('outerHTML = …'); throw new Error('outerHTML запрещён (К-8)'); }
  insertAdjacentHTML() { this.ownerDocument.violation('insertAdjacentHTML'); throw new Error('insertAdjacentHTML запрещён (К-8)'); }
  insertAdjacentText(_, text) { this.appendChild(this.ownerDocument.createTextNode(text)); }
  _adopt(node) {
    if (typeof node === 'string') return [this.ownerDocument.createTextNode(node)];
    if (node.nodeType === 11) { const moved = node.childNodes.slice(); node.childNodes = []; return moved; }
    if (node.parentNode) node.parentNode.removeChild(node);
    return [node];
  }
  appendChild(node) { for (const n of this._adopt(node)) { n.parentNode = this; this.childNodes.push(n); } return node; }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  prepend(...nodes) { for (const n of nodes.reverse()) this.insertBefore(n, this.firstChild); }
  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    for (const n of this._adopt(node)) {
      n.parentNode = this;
      this.childNodes.splice(this.childNodes.indexOf(ref), 0, n);
    }
    return node;
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) { this.childNodes.splice(i, 1); node.parentNode = null; }
    return node;
  }
  replaceChild(node, old) { this.insertBefore(node, old); return this.removeChild(old); }
  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    for (const n of nodes) parent.insertBefore(n, this);
    parent.removeChild(this);
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  cloneNode(deep) {
    const copy = new FakeNode(this.ownerDocument, this.nodeType, this.localName, this.namespaceURI);
    copy.data = this.data;
    for (const [k, v] of this.attrs) copy.attrs.set(k, v);
    if (deep) for (const c of this.childNodes) copy.appendChild(c.cloneNode(true));
    this.ownerDocument.created.push(copy);
    return copy;
  }
  setAttribute(k, v) { this.attrs.set(String(k), String(v)); }
  setAttributeNS(_, k, v) { this.setAttribute(k, v); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  getAttributeNS(_, k) { return this.getAttribute(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  removeAttribute(k) { this.attrs.delete(k); }
  toggleAttribute(k, force) {
    const on = force === undefined ? !this.attrs.has(k) : force;
    if (on) this.attrs.set(k, ''); else this.attrs.delete(k);
    return on;
  }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatchEvent(event) { for (const fn of (this.listeners[event.type] || []).slice()) fn.call(this, event); return true; }
  get clientWidth() { return this.ownerDocument.width; }
  get clientHeight() { return 800; }
  get offsetWidth() { return this.ownerDocument.width; }
  getBoundingClientRect() { const w = this.ownerDocument.width; return { x: 0, y: 0, left: 0, top: 0, width: w, height: 800, right: w, bottom: 800 }; }
  getBBox() { return { x: 0, y: 0, width: 100, height: 20 }; }
  getTotalLength() { return 100; }
  getPointAtLength(l) { return { x: l, y: 0 }; }
  focus() {}
  blur() {}
  getElementsByTagName(tag) { return walk(this).filter((n) => n !== this && n.nodeType === 1 && (tag === '*' || n.localName.toLowerCase() === tag.toLowerCase())); }
  getElementsByClassName(name) { return walk(this).filter((n) => n !== this && n.nodeType === 1 && n.classList.contains(name)); }
  querySelectorAll(selector) { return walk(this).filter((n) => n !== this && n.nodeType === 1 && matches(n, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return matches(this, selector); }
  closest(selector) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (matches(n, selector)) return n; return null; }
}

const HTML_NS = 'http://www.w3.org/1999/xhtml';

function walk(node, out = []) {
  out.push(node);
  for (const child of node.childNodes) walk(child, out);
  return out;
}

// Простые селекторы через запятую: tag, #id, .class, [attr], [attr="v"].
function matches(node, selector) {
  return selector.split(',').some((part) => {
    const s = part.trim();
    const re = /^([a-zA-Z][\w-]*)?((?:#[\w-]+|\.[\w-]+|\[[\w-]+(?:="?[^\]"]*"?)?\])*)$/;
    const m = s.match(re);
    if (!m) throw new Error(`подставной DOM не знает селектора «${s}» — дописать в matches()`);
    if (m[1] && node.localName.toLowerCase() !== m[1].toLowerCase()) return false;
    for (const piece of (m[2] || '').match(/#[\w-]+|\.[\w-]+|\[[^\]]+\]/g) || []) {
      if (piece[0] === '#' && node.id !== piece.slice(1)) return false;
      if (piece[0] === '.' && !node.classList.contains(piece.slice(1))) return false;
      if (piece[0] === '[') {
        const [k, v] = piece.slice(1, -1).split('=');
        if (!node.hasAttribute(k)) return false;
        if (v !== undefined && node.getAttribute(k) !== v.replace(/"/g, '')) return false;
      }
    }
    return true;
  });
}

function makeDocument(width) {
  const doc = {
    width,
    created: [],
    violations: [],
    listeners: {},
    hidden: false,
    visibilityState: 'visible',
    readyState: 'complete',
    violation(what) { this.violations.push(what); },
    createElement(tag) { return this._make(String(tag).toLowerCase(), HTML_NS); },
    createElementNS(ns, tag) { return this._make(String(tag), ns); },
    _make(tag, ns) { const n = new FakeNode(doc, 1, tag, ns); doc.created.push(n); return n; },
    createTextNode(text) { const n = new FakeNode(doc, 3, '#text'); n.data = String(text); return n; },
    createDocumentFragment() { return new FakeNode(doc, 11, '#document-fragment'); },
    createComment() { return new FakeNode(doc, 8, '#comment'); },
    getElementById(id) { return walk(doc.documentElement).find((n) => n.nodeType === 1 && n.id === id) || null; },
    querySelector(s) { return doc.documentElement.querySelector(s); },
    querySelectorAll(s) { return doc.documentElement.querySelectorAll(s); },
    getElementsByTagName(t) { return doc.documentElement.getElementsByTagName(t); },
    write() { doc.violation('document.write'); throw new Error('document.write запрещён (К-8)'); },
    writeln() { doc.violation('document.writeln'); throw new Error('document.writeln запрещён (К-8)'); },
    addEventListener(type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); },
    removeEventListener(type, fn) { doc.listeners[type] = (doc.listeners[type] || []).filter((f) => f !== fn); },
    dispatchEvent(event) { for (const fn of (doc.listeners[event.type] || []).slice()) fn.call(doc, event); return true; },
  };
  doc.documentElement = doc._make('html', HTML_NS);
  doc.head = doc._make('head', HTML_NS);
  doc.body = doc._make('body', HTML_NS);
  doc.documentElement.append(doc.head, doc.body);
  doc.defaultView = { innerWidth: width, innerHeight: 800 };
  doc.created = [];
  return doc;
}

function makeRoot(doc, loginText) {
  const root = doc.createElement('div');
  root.id = 'karta';
  if (loginText) {
    const p = doc.createElement('p');
    p.textContent = loginText;
    root.appendChild(p);
  }
  doc.body.appendChild(root);
  doc.created = [];
  return root;
}

const texts = (node) => walk(node).filter((n) => n.nodeType === 3).map((n) => n.data);
const allText = (node) => texts(node).join('\n');
const withAttr = (node, attr) => walk(node).filter((n) => n.nodeType === 1 && n.hasAttribute(attr));
const scriptsCreated = (doc) => doc.created.filter((n) => n.nodeType === 1 && n.localName.toLowerCase() === 'script').length;

// ── 0. Файл, синтаксис, запреты в тексте ───────────────────────────────────
if (!fs.existsSync(SCRIPT)) {
  pathBad(`live_map.js нет: ${SCRIPT}`);
  finish();
}
const source = fs.readFileSync(SCRIPT, 'utf-8');
const check = childProcess.spawnSync(process.execPath, ['--check', SCRIPT], { encoding: 'utf-8' });
judge(check.status === 0, 'node --check: синтаксис live_map.js цел', (check.stderr || '').slice(0, 200));
for (const [name, re] of [['innerHTML', /innerHTML/g], ['outerHTML', /outerHTML/g],
  ['insertAdjacentHTML', /insertAdjacentHTML/g], ['document.write', /document\s*\.\s*write/g]]) {
  const found = (source.match(re) || []).length;
  judge(found === 0, `в live_map.js нет ${name} (К-8, §6)`, `встречается ${found} раз`);
}
const urls = (source.match(/(?:https?:)?\/\/[a-z0-9.-]+\.[a-z]{2,}[^\s'"`)]*/gi) || [])
  .filter((u) => !/^https?:\/\/www\.w3\.org\//.test(u));
judge(urls.length === 0 && !/telegram-web-app\.js/.test(source),
  'live_map.js ничего не грузит извне (нет CDN и telegram-web-app.js)', `адреса: ${urls.slice(0, 3).join(', ')}`);
judge(/requestAnimationFrame/.test(source), 'анимация — requestAnimationFrame (§6)', 'requestAnimationFrame не встречается');

// ── 1. Под node: экспорт и тишина при require ──────────────────────────────
const realFetch = globalThis.fetch;
let nodeFetches = 0;
globalThis.fetch = () => { nodeFetches += 1; return new Promise(() => {}); };
let lib = null;
try {
  lib = require(SCRIPT);
} catch (error) {
  pathBad(`require(live_map.js) бросил: ${error.message}`);
  finish();
}

async function main() {
  await new Promise((r) => setImmediate(r));
  globalThis.fetch = realFetch;
  judge(nodeFetches === 0, 'под node require ничего не запускает (нет опроса)', `fetch звали ${nodeFetches} раз`);
  const missing = ['render', 'stateText', 'nextDelay', 'newEvents'].filter((f) => typeof lib[f] !== 'function');
  judge(missing.length === 0, 'module.exports несёт render, stateText, nextDelay, newEvents', `нет: ${missing.join(', ')}`);
  if (missing.length) finish();

  checkStateText();
  checkNextDelay();
  checkNewEvents();
  const fixture = loadFixture();
  if (fixture) checkRender(fixture);
  if (fixture) checkReadable(fixture);
  await checkBrowserStart(fixture);
  finish();
}

// ── 2. stateText: что сказать вместо карты (§5.6) ──────────────────────────
function isoAgo(seconds) { return new Date(Date.now() - seconds * 1000).toISOString(); }
function safe(fn, ...args) {
  try { return fn(...args); } catch (error) { return `исключение: ${error.message}`; }
}

function checkStateText() {
  const fresh = { v: 1, снято: isoAgo(3), период_с: 5, части: [{ id: 'channel' }], устарел: false };
  const said = (response) => safe(lib.stateText, response);
  let got = said({ status: 401, body: { беда: 'нужен пропуск' } });
  judge(got === LOGIN_TEXT, `401 — «${LOGIN_TEXT}»`, `вышло ${JSON.stringify(got)}`);
  got = said({ status: 401, body: { устарел: true, снято: isoAgo(900) } });
  judge(got === LOGIN_TEXT && !String(got).includes('устарел'),
    'БОЛЬНОЙ: 401 даже при «устарел» в теле — «Войдите», не «устарели» (К-8)', `вышло ${JSON.stringify(got)}`);
  got = said({ status: 503, body: { 'нет данных': NOT_BUILT_TEXT } });
  judge(got === NOT_BUILT_TEXT, `503 — «${NOT_BUILT_TEXT}»`, `вышло ${JSON.stringify(got)}`);
  got = said({ status: 200, body: fresh });
  judge(got === null, 'свежий снимок 200 — текста нет (null)', `вышло ${JSON.stringify(got)}`);
  got = said({ status: 200, body: Object.assign({}, fresh, { снято: isoAgo(200), устарел: true }) });
  const m = /данные устарели: (\d+) с/.exec(String(got));
  judge(m && Number(m[1]) >= 195 && Number(m[1]) <= 400,
    '«устарел» — «данные устарели: N с», N — возраст снимка', `вышло ${JSON.stringify(got)}`);
  got = said({ status: 200, body: Object.assign({}, fresh, { беда: 'нет architecture.yaml', части: [] }) });
  judge(got === NO_REGISTRY_TEXT, `реестра нет — «${NO_REGISTRY_TEXT}»`, `вышло ${JSON.stringify(got)}`);
}

// ── 3. nextDelay: пауза опроса (§6, ревью М-14) ────────────────────────────
function checkNextDelay() {
  const seq = [];
  let delay = 5000;
  for (let i = 0; i < 5; i += 1) { delay = safe(lib.nextDelay, delay, 429); seq.push(delay); }
  judge(JSON.stringify(seq) === JSON.stringify([10000, 20000, 40000, 60000, 60000]),
    '429 подряд — 10 000 → 20 000 → 40 000 → 60 000 → 60 000 (вдвое, потолок 60 с)', `вышло ${JSON.stringify(seq)}`);
  let got = safe(lib.nextDelay, 45000, 429);
  judge(got === 60000, '429 после 45 000 — 60 000, а не 90 000', `вышло ${got}`);
  got = safe(lib.nextDelay, 60000, 200);
  judge(got === 5000, 'первый 200 после паузы 60 000 — снова 5 000', `вышло ${got}`);
  got = safe(lib.nextDelay, 5000, 200);
  judge(got === 5000, '200 при 5 000 — 5 000', `вышло ${got}`);
}

// ── 4. newEvents: повтор ts не мигает дважды (§6) ──────────────────────────
function checkNewEvents() {
  const ev = (ts, src) => ({ ts, часть: 'quality', src, событие: 'gate.verdict', исход: 'ok', ребро: null });
  const snap = {
    события: [ev('2026-11-22T00:05:08+05:00', 'a'), ev('2026-11-22T00:05:09+05:00', 'b'),
      ev('2026-11-22T00:05:10+05:00', 'c')],
  };
  const tsOf = (list) => (Array.isArray(list) ? list.map((e) => e.src).join('') : String(list));
  let got = safe(lib.newEvents, snap, '2026-11-22T00:05:09+05:00');
  judge(tsOf(got) === 'c', 'новые — только позже последнего виденного', `вышло ${tsOf(got)}`);
  got = safe(lib.newEvents, snap, '2026-11-22T00:05:10+05:00');
  judge(Array.isArray(got) && got.length === 0, 'тот же снимок второй раз — новых 0 (не мигает дважды)', `вышло ${tsOf(got)}`);
  // Время RFC 3339 со смещением: сравнивается момент, а не строка.
  const mixed = { события: [ev('2026-11-21T19:05:09+00:00', 'x'), ev('2026-11-21T19:05:11+00:00', 'y')] };
  got = safe(lib.newEvents, mixed, '2026-11-22T00:05:10+05:00');
  judge(tsOf(got) === 'y', 'ts с другим смещением сравнивается как момент времени', `вышло ${tsOf(got)}`);
}

// ── 5. render по образцу снимка ────────────────────────────────────────────
function loadFixture() {
  const file = SNAPSHOT_CANDIDATES.find((p) => fs.existsSync(p));
  if (!file) {
    pathBad(`образца снимка нет (Task 0): ${SNAPSHOT_CANDIDATES.join(' | ')}`);
    return null;
  }
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf-8'));
  } catch (error) {
    pathBad(`образец снимка не JSON: ${file}: ${error.message}`);
    return null;
  }
  const parts = data['части'] || [];
  const edges = data['рёбра'] || [];
  const dashed = edges.filter((e) => e['источник'] === false).length;
  judge(parts.length === 8 && edges.length === 22 && dashed === 10,
    'образец: частей 8, рёбер 22, пунктирных 10 (§5.4)', `частей ${parts.length}, рёбер ${edges.length}, пунктирных ${dashed}`);
  judge((data['события'] || []).some((e) => JSON.stringify(e).includes(XSS)),
    'в образце есть событие с задачей <script>alert(1)</script> (К-8)', 'такого события нет');
  return parts.length ? data : null;
}

function expectedState(part) {
  const outcomes = part['исходы'] || {};
  if ((outcomes.error || 0) > 0) return 'error';
  if ((outcomes.refused || 0) > 0) return 'refused';
  return (part['событий'] || 0) > 0 ? 'active' : 'quiet';
}

function expectedKind(edge) {
  if (!edge['источник']) return 'dashed';
  return (edge['событий'] || 0) > 0 ? 'live' : 'idle';
}

function hasDash(node) {
  return walk(node).some((n) => n.nodeType === 1 && (n.hasAttribute('stroke-dasharray') || n.style.strokeDasharray || n.style['stroke-dasharray']));
}

function drawn(snapshot, width) {
  const doc = makeDocument(width);
  const root = makeRoot(doc, '');
  let error = null;
  try {
    lib.render(doc, root, snapshot, Date.parse(snapshot['снято']) || Date.now());
  } catch (e) {
    error = e;
  }
  return { doc, root, error };
}

function checkRender(fixture) {
  const parts = fixture['части'];
  const edges = fixture['рёбра'];
  // Строки данных — и в дереве исполнителей (§3.1: «задача» у каждого узла),
  // и в событии образца: обе обязаны прийти текстовым узлом.
  const snapshot = JSON.parse(JSON.stringify(fixture));
  const agents = snapshot['исполнители'] || [];
  if (agents.length) {
    agents[0]['задача'] = XSS;
    agents[agents.length - 1]['часть'] = null;
  }

  const wide = drawn(snapshot, 1280);
  judge(!wide.error, 'render по образцу не бросает', wide.error && wide.error.message);
  judge(wide.doc.violations.length === 0, 'render не трогает innerHTML/insertAdjacentHTML/document.write (К-8)',
    wide.doc.violations.join(', '));
  judge(scriptsCreated(wide.doc) === 0 && wide.root.getElementsByTagName('script').length === 0,
    'БОЛЬНОЙ: задача <script>alert(1)</script> — элементов script создано 0 (К-8)',
    `создано script: ${scriptsCreated(wide.doc)}`);
  judge(texts(wide.root).some((t) => t.includes(XSS)),
    'задача <script>alert(1)</script> пришла в DOM текстовым узлом (К-8)', 'текстового узла с этой строкой нет');

  // Части: число, порядок, имя, число событий, цвет рамки.
  const blocks = withAttr(wide.root, 'data-part');
  judge(blocks.length === parts.length, `блоков частей — ${parts.length}, как в части[] образца`, `нарисовано ${blocks.length}`);
  const order = blocks.map((b) => b.getAttribute('data-part')).join(',');
  judge(order === parts.map((p) => p.id).join(','), 'блоки частей идут в порядке реестра', `порядок ${order}`);
  for (const part of parts) {
    const block = blocks.find((b) => b.getAttribute('data-part') === part.id);
    if (!block) { pathBad(`блока части ${part.id} нет`); continue; }
    const text = allText(block);
    const count = String(part['событий']);
    judge(text.includes(part['имя']) && new RegExp(`(^|\\D)${count}(\\D|$)`).test(text),
      `часть ${part.id}: имя «${part['имя']}» и событий ${count}`, `текст блока ${JSON.stringify(text.slice(0, 80))}`);
    judge(block.getAttribute('data-state') === expectedState(part),
      `часть ${part.id}: рамка ${expectedState(part)} (error > refused > события > тишина)`,
      `data-state=${block.getAttribute('data-state')}`);
  }
  for (const state of ['error', 'refused']) {
    judge(parts.some((p) => expectedState(p) === state), `в образце есть часть в состоянии ${state}`, 'нет — путь цвета не судится');
  }

  // Сетка: 4×2 на широком экране, 2×4 на 360 px (§6).
  const grid = (root, cols) => withAttr(root, 'data-part').every((b, i) =>
    b.getAttribute('data-row') === String(Math.floor(i / cols)) && b.getAttribute('data-col') === String(i % cols));
  judge(grid(wide.root, 4), 'ширина 1280 — сетка 4×2 по порядку частей', 'data-row/data-col не те');
  const narrow = drawn(snapshot, 360);
  judge(!narrow.error && grid(narrow.root, 2), 'ширина 360 — сетка 2×4 по порядку частей',
    narrow.error ? narrow.error.message : 'data-row/data-col не те');
  const phone = drawn(snapshot, 412);
  judge(!phone.error && grid(phone.root, 2), 'ширина 412 (телефон) — тоже сетка 2×4, а не 4×2',
    phone.error ? phone.error.message : 'data-row/data-col не те');

  // Рёбра: все 22, вид — по полю источник у самого ребра, а не по своему списку.
  const lines = withAttr(wide.root, 'data-edge');
  const indices = lines.map((l) => Number(l.getAttribute('data-edge'))).sort((a, b) => a - b);
  judge(JSON.stringify(indices) === JSON.stringify(edges.map((_, i) => i)),
    `рёбер нарисовано ${edges.length}, по одному на каждый индекс`, `индексы ${JSON.stringify(indices)}`);
  const kindsBad = edges.map((e, i) => [i, expectedKind(e), lines.find((l) => l.getAttribute('data-edge') === String(i))])
    .filter(([, want, line]) => !line || line.getAttribute('data-kind') !== want);
  judge(kindsBad.length === 0, 'вид ребра: dashed без источника, idle без событий, live с событиями',
    kindsBad.slice(0, 3).map(([i, want, l]) => `#${i} ждали ${want}, вышло ${l && l.getAttribute('data-kind')}`).join('; '));
  const dashedLines = lines.filter((l) => l.getAttribute('data-kind') === 'dashed');
  judge(dashedLines.length === edges.filter((e) => !e['источник']).length && dashedLines.every(hasDash),
    `пунктирных рёбер ${edges.filter((e) => !e['источник']).length}, у каждого stroke-dasharray`,
    `dashed ${dashedLines.length}, с пунктиром ${dashedLines.filter(hasDash).length}`);
  const ray = edges.findIndex((e) => e['к'] === '*');
  judge(ray >= 0 && allText(wide.root).includes('всем'),
    'выход machine → * — одно ребро-луч с подписью «всем»', ray < 0 ? 'в образце нет ребра к "*"' : 'подписи «всем» нет');

  // Внешние, полоса нагрузки.
  for (const ext of fixture['внешние'] || []) {
    const node = withAttr(wide.root, 'data-external').find((n) => n.getAttribute('data-external') === ext.id);
    judge(node && allText(node).includes(ext['имя']), `внешний «${ext['имя']}» нарисован`, 'узла data-external нет или нет имени');
  }
  const machine = fixture['машина'] || {};
  const seats = machine['места'] || {};
  const whole = allText(wide.root);
  judge(whole.includes(`${humanLoad(machine.load1)} из ${machine['ядер']}`),
    `полоса нагрузки: load1/ядер (${machine.load1}/${machine['ядер']})`, 'чисел нагрузки нет');
  judge(whole.includes(`${seats['занято']}/${seats['всего']}`),
    `полоса нагрузки: места ${seats['занято']}/${seats['всего']}`, 'мест нет');
  if (fixture['токены']) {
    const hhmm = localTime(fixture['токены']['до']).slice(0, 5);
    judge(whole.includes(`за час до ${hhmm}`), `полоса нагрузки: токены «за час до ${hhmm}»`, 'подписи токенов нет');
  } else {
    pathBad('в образце токены: null — путь полосы токенов не судится');
  }

  // Дерево исполнителей и «без места».
  const nodes = withAttr(wide.root, 'data-agent');
  judge(nodes.length === agents.length && agents.length > 0, `узлов дерева исполнителей — ${agents.length}`, `нарисовано ${nodes.length}`);
  for (const agent of agents.filter((a) => a['родитель'])) {
    const node = nodes.find((n) => n.getAttribute('data-agent') === agent['вызов']);
    judge(node && node.getAttribute('data-parent') === agent['родитель'],
      `исполнитель ${agent['вызов']}: родитель ${agent['родитель']}`, `data-parent=${node && node.getAttribute('data-parent')}`);
  }
  const placeless = agents.find((a) => a['часть'] === null);
  const placelessNode = placeless && nodes.find((n) => n.getAttribute('data-agent') === placeless['вызов']);
  judge(placelessNode && allText(placelessNode).includes('без места'),
    'узел без части подписан «без места» (Н-2)', 'подписи «без места» у узла нет');

  // Перерисовка, а не дописывание.
  let again = null;
  try { lib.render(wide.doc, wide.root, snapshot, Date.now()); } catch (e) { again = e; }
  judge(!again && withAttr(wide.root, 'data-part').length === parts.length,
    'повторный render не плодит блоки', again ? again.message : `блоков ${withAttr(wide.root, 'data-part').length}`);

  // Реестра нет: частей не выдумано.
  const empty = drawn(Object.assign({}, snapshot, { беда: 'нет architecture.yaml', части: [], рёбра: [] }), 1280);
  judge(!empty.error && withAttr(empty.root, 'data-part').length === 0,
    'реестра нет — ни одного выдуманного блока части (§5.6 п. 6)', empty.error ? empty.error.message : 'блоки есть');
}

// ── 5а. Читаемость (§3.1, §5.6, ревью lviz6s11 п. 1–4, 6) ──────────────────
// Время — местное время зрителя: снимок несёт смещение сборщика (+00:00 у
// токенов), и срез строки показал бы чужие часы.
function pad2(n) { return String(n).padStart(2, '0'); }
function localTime(iso) {
  const d = new Date(Date.parse(iso));
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
function humanLoad(x) { return Number(x).toFixed(1).replace('.', ','); }
function isoBefore(iso, seconds) { return new Date(Date.parse(iso) - seconds * 1000).toISOString(); }
function rectOf(node) {
  const r = node && walk(node).find((n) => n.nodeType === 1 && n.localName === 'rect');
  return r && { x: +r.getAttribute('x'), y: +r.getAttribute('y'), w: +r.getAttribute('width'), h: +r.getAttribute('height') };
}
function strictlyInside(p, r) { return p.x > r.x + 1 && p.x < r.x + r.w - 1 && p.y > r.y + 1 && p.y < r.y + r.h - 1; }
function pathEnds(d) {
  const nums = String(d).match(/-?\d+(?:\.\d+)?/g).map(Number);
  return { a: { x: nums[0], y: nums[1] }, b: { x: nums[nums.length - 2], y: nums[nums.length - 1] } };
}

function checkReadable(fixture) {
  const base = JSON.parse(JSON.stringify(fixture));
  const shot = base['снято'];

  // Числа — человеческие, время снимка — на экране.
  const human = JSON.parse(JSON.stringify(base));
  human['токены'] = { за_час: 3007351, до: isoBefore(shot, 600), источник: 'tokens-2026-11' };
  human['машина'] = Object.assign({}, human['машина'], { load1: 13.99, ядер: 8 });
  const h = drawn(human, 1280);
  const ht = allText(h.root);
  judge(ht.includes('3,0 млн') && !ht.includes('3007351'), 'токены 3007351 — «3,0 млн», не сырое число', `текст ${JSON.stringify(ht.slice(0, 160))}`);
  judge(ht.includes('14,0 из 8') && !ht.includes('13.99'), 'нагрузка 13.99/8 — «14,0 из 8»', `текст ${JSON.stringify(ht.slice(0, 160))}`);
  judge(ht.includes(`снимок ${localTime(shot)}`), `время снимка «снимок ${localTime(shot)}» (§3.1)`, 'времени снимка нет');

  // Давность токенов: свежие — «10 мин назад» без тревоги; старые — с пометкой.
  judge(ht.includes('10 мин назад') && !ht.includes('токены устарели'), 'токены 10 мин назад — давность без пометки', `текст ${JSON.stringify(ht.slice(0, 200))}`);
  const old = JSON.parse(JSON.stringify(human));
  old['токены']['до'] = isoBefore(shot, 5 * 3600);
  const ot = allText(drawn(old, 1280).root);
  judge(ot.includes('5 ч назад') && ot.includes('токены устарели'), 'токены 5 ч назад — давность и пометка «токены устарели»', `текст ${JSON.stringify(ot.slice(0, 200))}`);

  // Возраст исполнителя (поле «с»).
  const aged = JSON.parse(JSON.stringify(base));
  const agents = aged['исполнители'] || [];
  if (agents.length < 2) { pathBad('в образце меньше двух исполнителей — возраст не судится'); } else {
    agents.forEach((a) => { a['с'] = isoBefore(shot, 12 * 60); });
    agents[0]['с'] = isoBefore(shot, 95 * 60);
    const nodes = withAttr(drawn(aged, 1280).root, 'data-agent');
    const byId = (id) => nodes.find((n) => n.getAttribute('data-agent') === id);
    const first = byId(agents[0]['вызов']);
    const second = byId(agents[1]['вызов']);
    judge(first && second && allText(first).includes('1 ч 35 мин') && allText(second).includes('12 мин'),
      'у исполнителя возраст: «1 ч 35 мин», «12 мин» (§3.1)', `узлы ${JSON.stringify([first, second].map((n) => n && allText(n)))}`);
  }

  // Телефон 390: задача — то, по чему владелец узнаёт исполнителя; тип и модель
  // одинаковы у всех и уходят во вторую строку, а не съедают имя задачи.
  const phoneTask = JSON.parse(JSON.stringify(base));
  const phoneAgents = phoneTask['исполнители'] || [];
  if (!phoneAgents.length) { pathBad('в образце нет исполнителей — строка телефона не судится'); } else {
    phoneAgents[0]['родитель'] = null;
    phoneAgents[0]['задача'] = 'x.long-task-name-that-must-fit-a-phone-row-1';
    const node = withAttr(drawn(phoneTask, 390).root, 'data-agent')
      .find((n) => n.getAttribute('data-agent') === phoneAgents[0]['вызов']);
    judge(node && allText(node).includes('x.long-task-name-that-must-fit-a-phone-row-1'),
      'ширина 390: имя задачи исполнителя (44 знака) целиком, без «…»', `узел ${JSON.stringify(node && allText(node))}`);
  }

  // Беда источника — значок у полосы, строка беды в подсказке (§5.6 п. 4).
  const clean = drawn(base, 1280);
  judge(withAttr(clean.root, 'data-trouble').length === 0, 'источники без беды — значка нет', 'значок есть');
  const sick = JSON.parse(JSON.stringify(base));
  sick['источники'] = (sick['источники'] || []).concat([{ имя: 'tokens-2026-11', возраст_с: null, беда: 'нет файла' }]);
  const icon = withAttr(drawn(sick, 1280).root, 'data-trouble')[0];
  const tip = icon && walk(icon).find((n) => n.nodeType === 1 && n.localName === 'title');
  judge(icon && tip && tip.textContent.includes('tokens-2026-11: нет файла'),
    'беда источника — значок data-trouble, в подсказке «tokens-2026-11: нет файла»', icon ? `подсказка ${tip && tip.textContent}` : 'значка нет');

  // Тишина (§5.6 п. 5) и счётчик без места (§5.5).
  const quiet = JSON.parse(JSON.stringify(base));
  quiet['события'] = [];
  quiet['части'].forEach((p) => { p['событий'] = 0; p['исходы'] = { ok: 0, refused: 0, error: 0 }; });
  quiet['без_места'] = { событий: 7, src: ['unknown'] };
  const q = drawn(quiet, 1280);
  judge(allText(q.root).includes('за минуту событий не было'), 'тишина — «за минуту событий не было»', 'надписи нет');
  judge((base['события'] || []).length > 0 && !allText(clean.root).includes('за минуту событий не было'),
    'есть события — надписи тишины нет', 'надпись тишины при событиях (или в образце нет событий)');
  const placeless = withAttr(q.root, 'data-placeless')[0];
  judge(placeless && placeless.getAttribute('data-placeless') === '7' && allText(placeless).includes('7'),
    'счётчик «без места» — 7 событий', placeless ? allText(placeless) : 'счётчика нет');

  // Рёбра: стрелка-маркер, концы не в центре блока, а на его краю.
  const wide = drawn(base, 1280);
  const svg = wide.root.getElementsByTagName('svg')[0];
  const markers = new Set(walk(svg).filter((n) => n.nodeType === 1 && n.localName === 'marker').map((n) => n.getAttribute('id')));
  const lines = withAttr(wide.root, 'data-edge');
  const noArrow = lines.filter((l) => { const m = /^url\(#(.+)\)$/.exec(l.getAttribute('marker-end') || ''); return !m || !markers.has(m[1]); });
  judge(lines.length > 0 && noArrow.length === 0, 'у каждого ребра стрелка: marker-end на свой <marker>',
    `без стрелки ${noArrow.length} из ${lines.length}`);
  const boxes = new Map();
  for (const node of withAttr(wide.root, 'data-part')) boxes.set(node.getAttribute('data-part'), rectOf(node));
  for (const node of withAttr(wide.root, 'data-external')) boxes.set(node.getAttribute('data-external'), rectOf(node));
  const edges = base['рёбра'] || [];
  const buried = lines.filter((l) => {
    const e = edges[Number(l.getAttribute('data-edge'))];
    const ends = pathEnds(l.getAttribute('d'));
    const from = boxes.get(e['от']);
    const to = boxes.get(e['к']);
    return (from && strictlyInside(ends.a, from)) || (to && strictlyInside(ends.b, to));
  });
  judge(lines.length > 0 && buried.length === 0, 'концы рёбер на краю блоков, не под ними (центр-в-центр)',
    `концов внутри блока: ${buried.length} из ${lines.length}`);

  // Внешние: «Владелец» слева, «Копии» справа (§3.1); на узком — над и под сеткой.
  const ext = base['внешние'] || [];
  if (ext.length < 2) { pathBad('в образце нет двух внешних — место не судится'); return; }
  const place = (root) => {
    const parts = withAttr(root, 'data-part').map(rectOf);
    const box = (id) => rectOf(withAttr(root, 'data-external').find((n) => n.getAttribute('data-external') === id));
    return {
      left: Math.min(...parts.map((r) => r.x)), right: Math.max(...parts.map((r) => r.x + r.w)),
      top: Math.min(...parts.map((r) => r.y)), bottom: Math.max(...parts.map((r) => r.y + r.h)),
      owner: box(ext[0].id), copies: box(ext[1].id),
    };
  };
  const w = place(wide.root);
  judge(w.owner && w.owner.x + w.owner.w <= w.left && w.copies && w.copies.x >= w.right,
    `ширина 1280: «${ext[0]['имя']}» слева от сетки, «${ext[1]['имя']}» справа`,
    `владелец ${JSON.stringify(w.owner)}, копии ${JSON.stringify(w.copies)}, сетка ${w.left}–${w.right}`);
  const n = place(drawn(base, 390).root);
  judge(n.owner && n.owner.y + n.owner.h <= n.top && n.copies && n.copies.y >= n.bottom,
    `ширина 390: «${ext[0]['имя']}» над сеткой, «${ext[1]['имя']}» под ней`,
    `владелец ${JSON.stringify(n.owner)}, копии ${JSON.stringify(n.copies)}, сетка ${n.top}–${n.bottom}`);
}

// ── 6. Браузер: самостарт, вход заголовком, опрос, пауза, видимость ────────
function jsonResponse(status, body) {
  return {
    status, ok: status >= 200 && status < 300,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function headerOf(options, name) {
  const headers = options && options.headers;
  if (!headers) return null;
  if (typeof headers.get === 'function') return headers.get(name);
  const key = Object.keys(headers).find((k) => k.toLowerCase() === name.toLowerCase());
  return key ? headers[key] : null;
}

// Отказ промиса внутри опроса (async) иначе роняет node мимо пробы: он
// приписывается текущему стенду браузера и судится как исключение скрипта.
let asyncSink = null;
process.on('unhandledRejection', (error) => {
  if (asyncSink) asyncSink.push(error); else pathBad(`необработанный отказ промиса: ${error && error.message}`);
});

async function flush() {
  for (let i = 0; i < 30; i += 1) await new Promise((r) => setImmediate(r));
}

function browser({ hash, loginText, reply }) {
  const doc = makeDocument(1280);
  const root = makeRoot(doc, loginText);
  const timers = [];
  const frames = [];
  const fetches = [];
  let nextId = 1;
  const errors = [];
  asyncSink = errors;
  const storage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; };
  const sandbox = {
    document: doc,
    location: { hash, search: '', pathname: '/puls', href: `https://panel.stend.example/puls${hash}`, origin: 'https://panel.stend.example' },
    fetch: (url, options) => { fetches.push({ url: String(url), options }); return Promise.resolve(reply(fetches.length)); },
    setTimeout: (fn, delay) => { const id = nextId++; timers.push({ id, fn, delay: Number(delay) || 0 }); return id; },
    clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    setInterval: (fn, delay) => { const id = nextId++; timers.push({ id, fn, delay: Number(delay) || 0, every: true }); return id; },
    clearInterval: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: () => {},
    performance: { now: () => Date.now() },
    sessionStorage: storage(),
    localStorage: storage(),
    innerWidth: 1280,
    innerHeight: 800,
    console,
    URLSearchParams, URL, Date, Math, JSON, Promise, Map, Set, Number, String, Array, Object, Error,
    encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN,
    Headers: globalThis.Headers, AbortController: globalThis.AbortController, queueMicrotask,
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  vm.createContext(sandbox);
  sandbox.window = vm.runInContext('this', sandbox);
  sandbox.self = sandbox.window;
  doc.defaultView = sandbox.window;
  try {
    vm.runInContext(source, sandbox, { filename: SCRIPT });
    doc.dispatchEvent({ type: 'DOMContentLoaded' });
  } catch (error) {
    errors.push(error);
  }
  return {
    doc, root, timers, fetches, errors,
    async runTimers() {
      const due = timers.splice(0, timers.length);
      for (const t of due) {
        if (t.every) timers.push(t);
        try { t.fn(); } catch (error) { errors.push(error); }
      }
      await flush();
    },
    runFrames(count) {
      for (let i = 0; i < count && frames.length; i += 1) {
        const fn = frames.shift();
        try { fn(Date.now() + i * 16); } catch (error) { errors.push(error); }
      }
    },
    setHidden(hidden) {
      doc.hidden = hidden;
      doc.visibilityState = hidden ? 'hidden' : 'visible';
      doc.dispatchEvent({ type: 'visibilitychange' });
    },
  };
}

async function checkBrowserStart(fixture) {
  const snap = fixture || {
    v: 1, снято: new Date().toISOString(), период_с: 5, окно_с: 60, сборка_мс: 1,
    машина: { load1: 1, ядер: 8, места: { занято: 0, всего: 4 } }, токены: null,
    части: [{ id: 'channel', имя: 'Канал', механизмов: 1, событий: 0, исходы: { ok: 0, refused: 0, error: 0 }, доля_ядра: 0, последнее: null }],
    внешние: [], рёбра: [], события: [], исполнители: [], без_места: { событий: 0, src: [] }, источники: [],
  };
  const fresh = Object.assign({}, snap, { снято: new Date().toISOString(), устарел: false });

  // Н-4: страница входа открыта из Telegram — скрипт входит заголовком tma,
  // получает 200 и снимает «Войдите».
  const initData = 'query_id=AAH&user=%7B%22id%22%3A100000001%7D&auth_date=1700000000&signature=c2ln&hash=abc';
  const tg = browser({
    hash: `#tgWebAppData=${encodeURIComponent(initData)}&tgWebAppVersion=7.10&tgWebAppPlatform=ios`,
    loginText: LOGIN_TEXT,
    reply: () => jsonResponse(200, fresh),
  });
  await flush();
  judge(tg.errors.length === 0, 'в браузере скрипт стартует без исключений', tg.errors.map((e) => e.message).join('; '));
  const first = tg.fetches[0];
  judge(first && /\/puls\/snimok$/.test(first.url.split('?')[0]), 'в браузере скрипт сам запрашивает /puls/snimok',
    first ? `запрошено ${first.url}` : 'запроса нет');
  judge(first && headerOf(first.options, 'Authorization') === `tma ${initData}`,
    'при #tgWebAppData — заголовок Authorization: tma <initData> (§7)',
    `вышло ${JSON.stringify(first && headerOf(first.options, 'Authorization'))}`);
  judge(!allText(tg.doc.documentElement).includes('Войдите'),
    'БОЛЬНОЙ: tgWebAppData и ответ 200 — «Войдите» снят (Н-4)', 'текст «Войдите» остался на странице');
  judge(withAttr(tg.root, 'data-part').length === (snap['части'] || []).length,
    'после 200 карта нарисована в #karta', `блоков ${withAttr(tg.root, 'data-part').length}`);
  judge(tg.timers.some((t) => t.delay === 5000), 'после 200 следующий опрос через 5 000 мс',
    `таймеры ${JSON.stringify(tg.timers.map((t) => t.delay))}`);
  tg.runFrames(120);
  judge(tg.errors.length === 0 && tg.doc.violations.length === 0, 'кадры анимации идут без исключений и без innerHTML',
    tg.errors.map((e) => e.message).concat(tg.doc.violations).join('; ') +
    ' (не хватает API подставного DOM — дописать в FakeNode)');

  // Видимость: в фоне опроса нет, на возврате — есть.
  const before = tg.fetches.length;
  tg.setHidden(true);
  await tg.runTimers();
  await tg.runTimers();
  judge(tg.fetches.length === before, 'вкладка скрыта (visibilitychange) — опрос стоит',
    `запросов в фоне: ${tg.fetches.length - before}`);
  tg.setHidden(false);
  await flush();
  await tg.runTimers();
  judge(tg.fetches.length > before, 'вкладка снова видна — опрос возобновлён', 'запросов после возврата нет');

  // §5.6 п. 7: открытая карта получила 401 — «Войдите», не «устарели».
  const expired = browser({ hash: '', loginText: '', reply: () => jsonResponse(401, { беда: 'нужен пропуск' }) });
  await flush();
  const expiredFirst = expired.fetches[0];
  judge(expiredFirst && !String(headerOf(expiredFirst.options, 'Authorization') || '').startsWith('tma'),
    'без #tgWebAppData заголовка tma нет (вход по куке)', expiredFirst ? 'заголовок tma послан' : 'запроса нет');
  const expiredText = allText(expired.doc.documentElement);
  judge(expiredText.includes(LOGIN_TEXT) && !expiredText.includes('устарел'),
    `БОЛЬНОЙ: 401 на /puls/snimok — на странице «${LOGIN_TEXT}», не «устарели» (К-8)`,
    `текст страницы ${JSON.stringify(expiredText.slice(0, 120))}`);

  // §5.6 п. 6 и Общие ограничения плана: реестра нет — полоса нагрузки,
  // токены и исполнители видны сразу, текст беды — рядом, а не вместо.
  const noRegistry = browser({ hash: '', loginText: '', reply: () => jsonResponse(200,
    Object.assign({}, fresh, { беда: 'нет architecture.yaml', части: [], рёбра: [] })) });
  await flush();
  const nrText = allText(noRegistry.doc.documentElement);
  judge(nrText.includes(NO_REGISTRY_TEXT) && withAttr(noRegistry.root, 'data-load').length === 1,
    'реестра нет — текст беды И полоса нагрузки на месте',
    `полос ${withAttr(noRegistry.root, 'data-load').length}, текст ${JSON.stringify(nrText.slice(0, 100))}`);
  judge(withAttr(noRegistry.root, 'data-agent').length === (snap['исполнители'] || []).length,
    'реестра нет — дерево исполнителей нарисовано', `узлов ${withAttr(noRegistry.root, 'data-agent').length}`);

  // §5.6 п. 3: устаревший снимок — карта серая, плашка не сдвигает её.
  const old = Object.assign({}, snap, { снято: new Date(Date.now() - 400000).toISOString(), устарел: true });
  const stale = browser({ hash: '', loginText: '', reply: () => jsonResponse(200, old) });
  await flush();
  const staleSvg = stale.root.getElementsByTagName('svg')[0];
  judge(staleSvg && staleSvg.classList.contains('stale'), 'устарел: true — у svg класс stale (карта серая)',
    staleSvg ? `class=${staleSvg.getAttribute('class')}` : 'svg нет');
  const banner = stale.root.getElementsByTagName('p')[0];
  judge(banner && banner.classList.contains('banner') && /данные устарели/.test(banner.textContent),
    'устарел — плашка «данные устарели» с классом banner', banner ? `class=${banner.getAttribute('class')}` : 'плашки нет');
  const freshSvg = tg.root.getElementsByTagName('svg')[0];
  judge(freshSvg && !freshSvg.classList.contains('stale'), 'свежий снимок — svg без класса stale', 'класс stale на свежем');

  // 503 — «ещё не собрана».
  const notBuilt = browser({ hash: '', loginText: '', reply: () => jsonResponse(503, { 'нет данных': NOT_BUILT_TEXT }) });
  await flush();
  judge(allText(notBuilt.doc.documentElement).includes(NOT_BUILT_TEXT),
    `503 — на странице «${NOT_BUILT_TEXT}»`, 'текста нет');

  // 429 подряд — пауза растёт вдвое (опрос живой, а не только nextDelay).
  const busy = browser({ hash: '', loginText: '', reply: () => jsonResponse(429, { беда: 'слишком часто' }) });
  await flush();
  const delays = [];
  for (let i = 0; i < 2; i += 1) {
    const pending = busy.timers.filter((t) => !t.every).map((t) => t.delay);
    delays.push(Math.max(0, ...pending));
    await busy.runTimers();
  }
  judge(JSON.stringify(delays) === JSON.stringify([10000, 20000]),
    'опрос на 429 — пауза 10 000, затем 20 000', `паузы ${JSON.stringify(delays)}`);
  const thrown = [expired, notBuilt, busy, noRegistry, stale].flatMap((b) => b.errors.concat(b.doc.violations));
  judge(thrown.length === 0, 'на 401, 503 и 429 скрипт не бросает и не трогает innerHTML',
    thrown.map((e) => (e && e.message) || String(e)).join('; '));
}

main().catch((error) => { pathBad(`проба упала: ${error.stack}`); finish(); });
