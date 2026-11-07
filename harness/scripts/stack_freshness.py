#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свежесть стека: что из ПО харнеса отстало, насколько и есть ли среди нового заплатки.

Владелец 23.10.2026: «Автоматически регулярно проверяет все патчи, новые версии по,
и рекомендует обновиться». Эта часть (Ф1–Ф2) только МЕРИТ и РЕКОМЕНДУЕТ: ничего не
ставит, `/etc` не правит, модель не зовёт. Реестр частей — данные
`harness/config/stack.yaml`; здесь код, в котором нет ветви «если часть такая-то».

Прогон:
    stack_freshness.py --замер [--печать]   # переписывает $LOG_DIR/stack/замер.json; код 0 | 2
    stack_freshness.py --печать             # по строке на часть из последнего замера
    stack_freshness.py --сводка             # решение: «шлю: N» + текст | «отложено: …» | «молчу: …» (коды 0 | 3)
    stack_freshness.py --отправлено --неделя N [--вне-очереди]   # после кода 0 от tg_send.sh
    stack_freshness.py --ответы             # «стек да/нет N» из inbox.jsonl; печатает несказанные отказы
    stack_freshness.py --сказано <текст>    # после кода 0 от tg_send.sh: отказ доставлен, второй раз не печатать
    stack_freshness.py --selftest           # пробы рядом (фоном)

Двери стенда (окружение): HARNESS_STACK_REGISTRY, HARNESS_STACK_SOURCES (каталог
записанных ответов), HARNESS_STACK_NOW, HARNESS_STACK_BOOT_DIR,
HARNESS_STACK_REBOOT_FILE, HARNESS_APT_HISTORY, HARNESS_STACK_APT_LISTS_STAMP.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402 — один читатель паспорта установки на весь харнес

TIMEOUT_SEC = 20
RESPONSE_LIMIT = 2 * 1024 * 1024      # nodejs.org 335 КБ, pypi 50 КБ; больше — не тот ответ
ANSWER_WINDOW_DAYS = 7
OUT_OF_BAND_MIN_DAYS = 3
ITEMS_MAX = 5
CHANNEL_CEILING = 1400                  # жёсткий потолок канала из память/TONE.md: сверх него tg_send отказывает навсегда
EVENT_NAMES_MAX = 160                   # длина перечня имён в строке «мимо двери»
UNKNOWN_DAYS = 21                       # столько суток подряд «не узнать свежую» — часть считается немеряемой
APT_LISTS_MAX_AGE = dt.timedelta(days=1)
MARKER = "стек"                         # единственный носитель метки вопроса; ключ данных заведёт З-1
QUESTIONS_LOG = "stack-вопросы.jsonl"

# Образец версии — по ИСТОЧНИКУ: apt-версии (`1:2.43.0-1ubuntu7.4`) под semver не подходят.
SHAPES = {"semver": re.compile(r"[0-9]+(\.[0-9]+){1,3}"),
          "debian": re.compile(r"([0-9]+:)?[0-9][A-Za-z0-9.+~-]*"),
          "kernel": re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+")}
SOURCE_SHAPE = {"apt": "debian", "apt-ждущие": "debian", "npm": "semver", "nodejs-dist": "semver",
                "pypi": "semver", "перезагрузка": "kernel"}
# Смена версии этих частей между замерами — не «мимо двери»: она ожидаема.
DOOR_NOT_WATCHED = {"перезагрузка", "apt-ждущие"}
STATE_OF_ATTENTION = ("отстала", "отстала_с_заплаткой")


class Current(NamedTuple):
    version: Optional[str]


class NotInstalled(NamedTuple):
    reason: str


class Unreadable(NamedTuple):
    reason: str


class Latest(NamedTuple):
    version: str
    newer: list
    security: int
    since: Optional[dt.datetime]      # выход самого раннего из новых: с какого дня мы в отставании
    phased: bool = False


class Unknown(NamedTuple):
    reason: str


class Registry(NamedTuple):
    parts: list
    general_os: list
    not_judged: dict


class Broken(NamedTuple):
    reason: str


class Answer(NamedTuple):
    yes: list
    no: list
    refusal: Optional[str]
    message_id: object = None
    hint: bool = False


def run_argv(argv, path=None):
    """(код, stdout, stderr); без shell. 127 — команды нет, а не «пустой ответ»."""
    env = dict(os.environ, LC_ALL="C")
    if path:
        env["PATH"] = path
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=TIMEOUT_SEC, env=env)
    except FileNotFoundError:
        return 127, "", f"нет команды {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: нет ответа за {TIMEOUT_SEC} с"
    return done.returncode, done.stdout, done.stderr


@dataclass
class Ctx:
    run: Callable
    now: dt.datetime
    sources: Optional[Path]
    state_dir: Path
    work_dir: Path
    boot_dir: Path = Path("/boot")
    reboot_file: Path = Path("/var/run/reboot-required")
    apt_history: Path = Path("/var/log/apt/history.log")
    apt_lists_stamp: Path = Path("/var/lib/apt/periodic/update-success-stamp")
    env_path: Optional[str] = None
    conf: dict = field(default_factory=dict)

    def exec(self, argv):
        return self.run(argv, self.env_path)


# ── версии ──────────────────────────────────────────────────────────────────
def valid_version(shape, text):
    # fullmatch, не match с `$`: `$` пропускает хвостовой перевод строки.
    return isinstance(text, str) and SHAPES[shape].fullmatch(text) is not None


def numeric_key(text):
    return tuple(int(x) for x in re.findall(r"[0-9]+", text))


def compare(shape, a, b, ctx):
    """1 если a новее b, -1 если старше, 0 если равны. apt — только dpkg: свой разбор врёт на `~` и эпохах."""
    if shape != "debian":
        return (numeric_key(a) > numeric_key(b)) - (numeric_key(a) < numeric_key(b))
    for relation, result in (("gt", 1), ("lt", -1)):
        code, _, err = ctx.exec(["dpkg", "--compare-versions", a, relation, b])
        if code == 0:
            return result
        if code not in (1,):
            raise RuntimeError(f"dpkg --compare-versions не отработал: код {code} {err[:60]}")
    return 0


def too_wide(text):
    return text[:40].replace("\n", "\\n")


# ── источники: имя → функция чтения ─────────────────────────────────────────
def http_get(url, ctx):
    cache = ctx.work_dir / "кэш"
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    body_file, etag_file = cache / f"{key}.body", cache / f"{key}.etag"
    headers = {"User-Agent": "harness-stack-freshness"}
    if body_file.exists() and etag_file.exists():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()
    etag = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SEC) as answer:
            raw = answer.read(RESPONSE_LIMIT + 1)
            etag = answer.headers.get("ETag")
    except urllib.error.HTTPError as error:
        if error.code != 304 or not body_file.exists():
            return Unknown(f"источник ответил {error.code}")
        raw = body_file.read_bytes()
    except (OSError, http.client.HTTPException, ValueError) as error:
        # IncompleteRead и BadStatusLine — не OSError, InvalidURL — ValueError: без них обрыв валит весь замер.
        return Unknown(f"источник недоступен: {error}")
    if len(raw) > RESPONSE_LIMIT:
        return Unknown(f"ответ больше {RESPONSE_LIMIT} байт")
    if etag:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            body_file.write_bytes(raw)
            etag_file.write_text(etag, encoding="utf-8")
        except OSError:
            pass                    # кэш — ускорение, а не данные: без него замер верен, только дороже
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return Unknown("ответ не в UTF-8")


def recorded_or_live(ctx, label, live):
    """Стенд кормит записанными ответами: файл на источник, нет файла — источник недоступен."""
    if ctx.sources is None:
        return live()
    path = ctx.sources / label
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return Unknown(f"источник недоступен: нет записи {label}")


def command_text(ctx, argv):
    code, out, err = ctx.exec(argv)
    return out if code == 0 else Unknown(f"{argv[0]} вернула {code}: {err.strip()[:60]}")


def parse_json(text, what):
    try:
        return json.loads(text)
    except ValueError:
        return Unknown(f"{what} не разобран")


def aware_date(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")) if "T" in text else \
        dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)


def newer_result(shape, current, versions, security, dates, ctx):
    """Общая сборка Latest: новые = строго новее текущей; самая ранняя дата — начало отставания."""
    newer = [v for v in versions if current is None or compare(shape, v, current, ctx) > 0]
    top = max(newer, key=numeric_key) if newer else current
    since = min((dates[v] for v in newer if v in dates), default=None)
    return Latest(top, sorted(newer, key=numeric_key), security, since)


def source_nodejs_dist(part, current, ctx):
    text = recorded_or_live(ctx, "nodejs-dist-index.json",
                            lambda: http_get("https://nodejs.org/dist/index.json", ctx))
    if isinstance(text, Unknown):
        return text
    index = parse_json(text, "индекс nodejs.org")
    if isinstance(index, Unknown):
        return index
    branch = str(part["свежая"]["ветка"])
    entries = {}
    for entry in index:
        version = entry.get("version") if isinstance(entry, dict) else None
        if not isinstance(version, str) or not valid_version("semver", version[1:]):
            return Unknown(f"версия не той формы: {too_wide(str(version))!r}")
        if version[1:].split(".")[0] == branch:
            entries[version[1:]] = entry
    if not entries:
        return Unknown(f"в индексе нет ветки {branch}")
    result = newer_result("semver", current, list(entries), 0,
                          {v: aware_date(e["date"]) for v, e in entries.items() if isinstance(e.get("date"), str)}, ctx)
    security = sum(1 for v in result.newer if entries[v].get("security") is True)
    return result._replace(security=security)


def source_npm(part, current, ctx):
    package = part["свежая"]["пакет"]
    slug = package.split("/")[-1]
    version = recorded_or_live(ctx, f"npm-{slug}-version.txt",
                               lambda: command_text(ctx, ["npm", "view", package, "version"]))
    times = recorded_or_live(ctx, f"npm-{slug}-time.json",
                             lambda: command_text(ctx, ["npm", "view", package, "time", "--json"]))
    for answer in (version, times):
        if isinstance(answer, Unknown):
            return answer
    latest = version[:-1] if version.endswith("\n") else version
    if not valid_version("semver", latest):
        return Unknown(f"версия не той формы: {too_wide(latest)!r}")
    table = parse_json(times, "npm time")
    if isinstance(table, Unknown):
        return table
    # Только выпуски вида N.N.N: предварительные (-beta) выпуском для владельца не считаются.
    stable = {v: aware_date(t) for v, t in table.items() if valid_version("semver", v) and isinstance(t, str)}
    versions = [v for v in stable if numeric_key(v) <= numeric_key(latest)]
    return newer_result("semver", current, versions, 0, stable, ctx)


def source_pypi(part, current, ctx):
    package = part["свежая"]["пакет"]
    text = recorded_or_live(ctx, f"pypi-{package}.json",
                            lambda: http_get(f"https://pypi.org/pypi/{package}/json", ctx))
    if isinstance(text, Unknown):
        return text
    body = parse_json(text, "ответ pypi")
    if isinstance(body, Unknown):
        return body
    latest = (body.get("info") or {}).get("version")
    if not valid_version("semver", latest):
        return Unknown(f"версия не той формы: {too_wide(str(latest))!r}")
    versions = [v for v in (body.get("releases") or {}) if valid_version("semver", v)
                and numeric_key(v) <= numeric_key(latest)]
    return newer_result("semver", current, versions, 0, {}, ctx)


POLICY_VERSION = re.compile(r"^\s{1,5}(?:\*\*\*\s)?(\S+) \d+(?: \(phased [^)]*\))?\s*$")
POLICY_SOURCE = re.compile(r"^\s+\d+ https?://\S+ (\S+?)/")


def parse_policy(text, package):
    """(кандидат, поэтапный, карман кандидата) из блока `apt-cache policy <пакет>`; None — блока нет."""
    lines = text.split("\n")
    try:
        start = lines.index(f"{package}:")
    except ValueError:
        return None
    block = []
    for line in lines[start + 1:]:
        if line and not line[0].isspace():
            break
        block.append(line)
    candidate = next((l.split(":", 1)[1].strip() for l in block if l.strip().startswith("Candidate:")), None)
    if not candidate or candidate == "(none)":
        return None
    phased, pocket, inside = False, "", False
    for line in block:
        found = POLICY_VERSION.match(line)
        if found:
            inside = found.group(1) == candidate
            phased = phased or (inside and "(phased" in line)
            continue
        source = POLICY_SOURCE.match(line)
        if inside and source and not pocket:
            pocket = source.group(1)
    return candidate, phased, pocket


def stale_apt_lists(ctx):
    """Unknown, если списки apt не обновлялись больше суток: кандидат из старых списков равен установленной версии, и часть молча «свежая»."""
    try:
        moment = dt.datetime.fromtimestamp(ctx.apt_lists_stamp.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return Unknown("apt-списки: штампа обновления нет")
    return Unknown("apt-списки старше суток") if ctx.now - moment > APT_LISTS_MAX_AGE else None


def source_apt(part, current, ctx):
    stale = stale_apt_lists(ctx)
    if stale:
        return stale
    package = part["свежая"]["пакет"]
    text = recorded_or_live(ctx, "apt-cache-policy.txt", lambda: command_text(ctx, ["apt-cache", "policy", package]))
    if isinstance(text, Unknown):
        return text
    parsed = parse_policy(text, package)
    if parsed is None:
        return Unknown(f"apt-cache не назвал кандидата для {package}")
    candidate, phased, pocket = parsed
    if not valid_version("debian", candidate):
        return Unknown(f"версия не той формы: {too_wide(candidate)!r}")
    newer = [candidate] if compare("debian", candidate, current, ctx) > 0 else []
    return Latest(candidate if newer else current, newer, int(bool(newer) and pocket.endswith("-security")),
                  None, phased and bool(newer))


def source_apt_pending(part, current, ctx):
    """Общая строка ОС: сколько пакетов Ubuntu ждёт; чужие источники (sury, nginx.org) не считаются."""
    stale = stale_apt_lists(ctx)
    if stale:
        return stale
    text = recorded_or_live(ctx, "apt-list-upgradable.txt",
                            lambda: command_text(ctx, ["apt", "list", "--upgradable"]))
    if isinstance(text, Unknown):
        return text
    pockets = re.compile(part["свежая"].get("карман", "-(updates|security)$"))
    names, security = [], 0
    for line in text.split("\n"):
        found = re.match(r"^([^/\s]+)/(\S+)", line)
        if found and any(pockets.search(p) for p in found.group(2).split(",")):
            names.append(found.group(1))
            security += int(any(p.endswith("-security") for p in found.group(2).split(",")))
    return Latest("-", names, security, None)


def source_reboot(part, current, ctx):
    kernels = {}
    for path in ctx.boot_dir.glob("vmlinuz-*"):
        release = re.match(r"vmlinuz-([0-9]+\.[0-9]+\.[0-9]+-[0-9]+)", path.name)
        if release:
            kernels[release.group(1)] = path
    if not kernels:
        return Unknown(f"ядер в {ctx.boot_dir} нет")
    newest = max(kernels, key=numeric_key)
    waiting = ctx.reboot_file.exists()
    behind = numeric_key(newest) > numeric_key(current) or waiting
    marker = ctx.reboot_file if waiting else kernels[newest]
    since = dt.datetime.fromtimestamp(marker.stat().st_mtime, dt.timezone.utc) if behind else None
    # Заплатки ядра внутри этой перезагрузки, но «с заплаткой» она не называется:
    # вне очереди о ней не пишем, решение о перезагрузке всегда за владельцем.
    return Latest(newest, [newest] if behind else [], 0, since)


SOURCES = {"apt": source_apt, "apt-ждущие": source_apt_pending, "npm": source_npm,
           "nodejs-dist": source_nodejs_dist, "pypi": source_pypi, "перезагрузка": source_reboot}


def read_latest(part, current, ctx):
    try:
        return SOURCES[part["свежая"]["источник"]](part, current, ctx)
    except RuntimeError as error:
        return Unknown(str(error))
    except Exception as error:  # noqa: BLE001 — граница с чужими данными: любой разбор ответа не должен ронять замер остальных частей
        return Unknown(f"ответ источника не разобран: {type(error).__name__}: {too_wide(str(error))}")


# ── текущая версия ──────────────────────────────────────────────────────────
def presence_package(value, ctx):
    code, _, err = ctx.exec(["dpkg-query", "-W", "-f=${Version}", value])
    if code == 0:
        return None
    if code == 1 and "no packages found" in err:
        return NotInstalled(f"пакета {value} нет")
    return Unreadable(f"dpkg-query: код {code} {err.strip()[:60]}")


def presence_file(value, ctx):
    path = Path(value.replace("$STATE_DIR", str(ctx.state_dir)))
    return None if path.exists() else NotInstalled(f"файла {path} нет")


def presence_key(value, ctx):
    key, _, wanted = value.partition("=")
    return None if str(ctx.conf.get(key, "")) == wanted else NotInstalled(f"ключ {key} не {wanted}")


PRESENCE = {"пакет": presence_package, "файл": presence_file, "ключ": presence_key}


def read_current(part, ctx):
    for kind, value in (part.get("есть_если") or {}).items():
        verdict = PRESENCE[kind](value, ctx)
        if verdict is not None:
            return verdict
    spec = part.get("текущая")
    if spec is None:
        return Current(None)
    argv = [a.replace("$STATE_DIR", str(ctx.state_dir)) for a in spec["команда"]]
    code, out, err = ctx.exec(argv)
    if code != 0:
        return Unreadable(f"{argv[0]} вернула {code}: {err.strip()[:60]}")
    text = out.strip()
    if spec.get("образец"):
        found = re.match(spec["образец"], text)
        if not found:
            return Unreadable(f"вывод не по образцу: {too_wide(text)!r}")
        text = found.group(1) if found.groups() else found.group(0)
    if not valid_version(SOURCE_SHAPE[part["свежая"]["источник"]], text):
        return Unreadable(f"версия не той формы: {too_wide(text)!r}")
    return Current(text)


# ── состояние части ─────────────────────────────────────────────────────────
def is_deferred(deferred, part, latest):
    return isinstance(latest, Latest) and deferred.get(part["имя"]) == latest.version


def needs_patch_alarm(part, current, latest):
    return latest.security > 0 and part.get("критичность") == "высокая"


# Порядок строк — приоритет: первое сработавшее правило называет состояние.
STATE_RULES = [
    (lambda p, c, l, d: p.get("чьё") == "чужое", "чужое"),
    (lambda p, c, l, d: isinstance(c, NotInstalled), "не_стоит"),
    (lambda p, c, l, d: isinstance(c, Unreadable), "не_узнать_текущую"),
    (lambda p, c, l, d: isinstance(l, Unknown), "не_узнать_свежую"),
    (lambda p, c, l, d: not l.newer, "свежая"),
    (lambda p, c, l, d: l.phased, "поэтапно"),
    (lambda p, c, l, d: is_deferred(d, p, l), "отложено_владельцем"),
    (lambda p, c, l, d: needs_patch_alarm(p, c, l), "отстала_с_заплаткой"),
]


def classify(part, current, latest, deferred=None, prev=None):
    for rule, state in STATE_RULES:
        if rule(part, current, latest, deferred or {}):
            return state
    return "отстала"


# ── «обновлено мимо двери» ──────────────────────────────────────────────────
def apt_history_text(ctx):
    """Текст history.log (+ прошлая ротация); None — основной журнал не прочитан."""
    texts = []
    for path in (ctx.apt_history, ctx.apt_history.with_name(ctx.apt_history.name + ".1.gz")):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                texts.append(handle.read())
        except OSError:
            if path == ctx.apt_history:
                return None
    return "\n".join(texts)


def door_verdict(part, previous, current, ctx):
    """('законно'|'мимо', заметка). До Ф3 двери установки нет, законна лишь ночная работа unattended-upgrade."""
    package = (part["свежая"] if part["свежая"]["источник"] == "apt" else {}).get("пакет")
    if not package:
        return "мимо", ""
    history = apt_history_text(ctx)
    if history is None:
        return "мимо", "журнал apt не прочитан"
    upgrade = re.compile(rf"(^|[ ,]){re.escape(package)}(:[a-z0-9]+)? \([^,)]+, {re.escape(current)}\)")
    for stanza in history.split("\n\n"):
        if "unattended-upgrade" in stanza and any(upgrade.search(l) for l in stanza.split("\n") if l.startswith("Upgrade:")):
            return "законно", "обновлено ночным обновлением Ubuntu"
    return "мимо", ""


# ── замер ───────────────────────────────────────────────────────────────────
def days_between(now, since):
    return None if since is None else max(0, (now - since).days)


def measure_part(part, ctx, deferred, prev_row):
    current = read_current(part, ctx)
    latest = None
    if isinstance(current, Current):
        latest = read_latest(part, current.version, ctx)
    state = classify(part, current, latest, deferred)
    row = {"имя": part["имя"], "текущая": getattr(current, "version", None), "свежая": None,
           "выпусков_новее": 0, "безопасности": 0, "суток_отставания": None, "состояние": state,
           "когда": ctx.now.isoformat()}
    if isinstance(latest, Latest):
        row.update({"свежая": latest.version, "выпусков_новее": len(latest.newer),
                    "безопасности": latest.security, "суток_отставания": days_between(ctx.now, latest.since)})
    reason = next((x.reason for x in (current, latest) if isinstance(x, (Unknown, Unreadable, NotInstalled))), "")
    if reason:
        row["причина"] = reason
    if state == "не_узнать_свежую":
        row["не_узнать_с"] = (prev_row or {}).get("не_узнать_с") or row["когда"]
    moved = (prev_row and prev_row.get("текущая") and row["текущая"] and prev_row["текущая"] != row["текущая"]
             and part["свежая"]["источник"] not in DOOR_NOT_WATCHED)
    if moved:
        verdict, note = door_verdict(part, prev_row["текущая"], row["текущая"], ctx)
        row["заметка"] = note
        if verdict == "мимо":
            row["состояние"] = "обновлено_мимо_двери"
            row["было"] = prev_row["текущая"]
    return row


def measure(parts, ctx, prev=None, deferred=None):
    prev = prev or {}
    return [measure_part(part, ctx, deferred or {}, prev.get(part["имя"])) for part in parts]


def judged(rows):
    return [r for r in rows if r["состояние"] != "чужое"]


def exit_code(rows):
    if not judged(rows) or any(r["состояние"] == "не_узнать_текущую" for r in rows):
        return 2
    return 0


def load_registry(path):
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return Broken(f"реестр не прочитан: {error}")
    if not isinstance(data, dict) or not isinstance(data.get("части"), list):
        return Broken("в реестре нет списка «части»")
    for part in data["части"]:
        if not (isinstance(part, dict) and isinstance(part.get("имя"), str)
                and isinstance(part.get("свежая"), dict) and part["свежая"].get("источник") in SOURCES):
            return Broken(f"часть без имени или с неизвестным источником: {str(part)[:60]}")
    return Registry(data["части"], data.get("в_общей_строке_ОС") or [], data.get("не_судим") or {})


def format_rows(rows):
    lines = []
    for r in rows:
        days = "?" if r["суток_отставания"] is None else r["суток_отставания"]
        line = (f"{r['имя']} {r['состояние']} {r['текущая'] or '?'} → {r['свежая'] or '?'} "
                f"выпусков_новее={r['выпусков_новее']} безопасности={r['безопасности']} суток={days}")
        lines.append(line + (f" причина={r['причина']}" if r.get("причина") else ""))
    count = len(judged(rows))
    lines.append(f"судимых частей {count}" if count else "судить нечего: 0")
    return lines


# ── файлы состояния ─────────────────────────────────────────────────────────
@dataclass
class Env:
    ctx: Ctx
    log_dir: Path
    project_dir: Path
    owner_tz: str
    neighbours: list
    registry_path: Path

    @property
    def stack_dir(self):
        return self.log_dir / "stack"


def build_env():
    install = config.конфиг()
    conf = dict(config.из_файла(config.путь_harness_conf()))
    for key in ("OWNER_TZ", "STACK_NEIGHBOURS", "AUTO_REBOOT_IN_ROTATION"):
        conf[key] = os.environ.get(key) or conf.get(key, "")
    owner_tz = conf["OWNER_TZ"]
    if owner_tz:
        os.environ["TZ"] = owner_tz
        time.tzset()
    stamp = os.environ.get("HARNESS_STACK_NOW")
    now = (dt.datetime.fromisoformat(stamp) if stamp else dt.datetime.now(dt.timezone.utc)).astimezone()
    here = Path(__file__).resolve().parent
    registry = os.environ.get("HARNESS_STACK_REGISTRY")
    if not registry:
        registry = next((str(c) for c in (here.parent / "harness" / "config" / "stack.yaml",
                                          here.parent / "config" / "stack.yaml") if c.is_file()), "")
    sources = os.environ.get("HARNESS_STACK_SOURCES")
    ctx = Ctx(run=run_argv, now=now, sources=Path(sources) if sources else None,
              state_dir=Path(install["STATE_DIR"]),
              work_dir=Path(install["LOG_DIR"]) / "stack",
              boot_dir=Path(os.environ.get("HARNESS_STACK_BOOT_DIR", "/boot")),
              reboot_file=Path(os.environ.get("HARNESS_STACK_REBOOT_FILE", "/var/run/reboot-required")),
              apt_history=Path(os.environ.get("HARNESS_APT_HISTORY", "/var/log/apt/history.log")),
              apt_lists_stamp=Path(os.environ.get("HARNESS_STACK_APT_LISTS_STAMP",
                                                  "/var/lib/apt/periodic/update-success-stamp")),
              conf=dict(install, **conf))
    return Env(ctx, Path(install["LOG_DIR"]), Path(install.get("PROJECT_DIR") or here.parent),
               owner_tz, conf["STACK_NEIGHBOURS"].split(), Path(registry))


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def read_state_file(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def log_line(env, text):
    env.log_dir.mkdir(parents=True, exist_ok=True)
    with open(env.log_dir / "stack-watch.log", "a", encoding="utf-8") as out:
        out.write(f"{env.ctx.now.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")


def recommended_count(rows, deferred):
    return sum(1 for r in rows if r["состояние"] in STATE_OF_ATTENTION + ("обновлено_мимо_двери",)
               and deferred.get(r["имя"]) != r["свежая"])


def command_measure(env):
    registry = load_registry(env.registry_path)
    if isinstance(registry, Broken):
        print(f"реестр не разобран: {registry.reason}")
        return 2, []
    old = read_state_file(env.stack_dir / "замер.json") or {}
    prev = {r["имя"]: r for r in old.get("части", [])}
    deferred = old.get("отложено", {})
    rows = measure(registry.parts, env.ctx, prev, deferred)
    code = exit_code(rows)
    if not judged(rows):
        return code, rows
    events = [e for e in old.get("события", []) if (env.ctx.now - dt.datetime.fromisoformat(e["когда"])).days < 8]
    events += [{"имя": r["имя"], "было": r["было"], "стало": r["текущая"], "когда": r["когда"]}
               for r in rows if r["состояние"] == "обновлено_мимо_двери"]
    snapshot = {"когда": env.ctx.now.isoformat(), "части": rows, "отложено": deferred, "события": events,
                "рекомендовать": recommended_count(rows, deferred)}
    if old.get("сводка_отложена"):
        snapshot["сводка_отложена"] = old["сводка_отложена"]
    write_json_atomic(env.stack_dir / "замер.json", snapshot)
    log_line(env, f"замер: частей {len(judged(rows))}, к рекомендации {snapshot['рекомендовать']}, код {code}")
    for r in rows:
        if r.get("причина"):
            log_line(env, f"{r['имя']} {r['состояние']}: {r['причина']}")
    return code, rows


# ── сводка ──────────────────────────────────────────────────────────────────
def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def week_key(moment):
    year, week, _ = moment.isocalendar()
    return f"{year}-{week:02d}"


def part_map(env):
    registry = load_registry(env.registry_path)
    return {} if isinstance(registry, Broken) else {p["имя"]: p for p in registry.parts}


def rows_for_summary(measurement, parts, deferred):
    """(пункты с номерами, строки вне номеров, ещё не влезшие) — харнес нумеруется, машина только высокой критичности."""
    numbered, outside = [], []
    for row in measurement["части"]:
        part = parts.get(row["имя"], {})
        if row["состояние"] not in STATE_OF_ATTENTION or deferred.get(row["имя"]) == row["свежая"]:
            continue
        if part.get("чьё") == "харнес":
            numbered.append(row)
        elif part.get("чьё") == "машина" and part.get("критичность") == "высокая":
            outside.append(row)
    numbered.sort(key=lambda r: (parts[r["имя"]].get("критичность") != "высокая", -r["безопасности"], r["имя"]))
    return numbered, outside


def item_line(number, row, part):
    patches = f", {row['безопасности']} с заплатками безопасности" if row["безопасности"] else ""
    words = plural(row["выпусков_новее"], "выпуск", "выпуска", "выпусков")
    line = f"{number}. {row['имя']} {row['текущая']} → {row['свежая']}: {row['выпусков_новее']} {words}{patches}. Рекомендую."
    door = part.get("установка") or {}
    if door.get("дверь") == "нет" and door.get("причина"):
        line += f" Сейчас не поставить: {door['причина']}."
    return line


def outside_line(row, part, neighbours, conf=None):
    days = row["суток_отставания"]
    waiting = f" ждёт {days} {plural(days, 'сутки', 'суток', 'суток')}" if days else " ждёт"
    touches = [n for item in part.get("задевает", []) for n in (neighbours if item == "$STACK_NEIGHBOURS" else [item])]
    effect = f"задевает {' и '.join(touches)}" if touches else part.get("риск", "")
    door = part.get("установка") or {}
    own = door.get("дверь") == "окно-ротации" and (conf or {}).get(door.get("ключ", ""), "") == "1"
    decision = "встанет сама при ближайшей смене сессии без помощников" if own else "решаешь ты"
    return f"Вне номеров: {row['имя']}{waiting} ({row['текущая']} → {row['свежая']}), {effect} — {decision}."


def long_unknown(measurement, now):
    """Имена частей, которые UNKNOWN_DAYS суток и дольше не удаётся измерить: молчащий источник не должен молчать вечно."""
    return [r["имя"] for r in measurement["части"] if r["состояние"] == "не_узнать_свежую"
            and (now - dt.datetime.fromisoformat(r["не_узнать_с"])).days >= UNKNOWN_DAYS]


def events_lines(events):
    """Одна строка на событие, пока их мало; много — одна строка с перечнем, иначе ручной apt upgrade раздувает текст сверх потолка канала."""
    if len(events) <= 2:
        return [f"Обновлено мимо двери: {e['имя']} {e['было']} → {e['стало']}." for e in events]
    names = ", ".join(e["имя"] for e in events)
    if len(names) > EVENT_NAMES_MAX:
        names = names[:EVENT_NAMES_MAX].rsplit(", ", 1)[0] + " и другие"
    return [f"Обновлено мимо двери: {len(events)} частей — {names}."]


def summary_text(measurement, parts, deferred, week, neighbours, env_now, out_of_band=False, only=None, conf=None):
    numbered, outside = rows_for_summary(measurement, parts, deferred)
    if only is not None:
        numbered = [r for r in numbered if r["имя"] in only]
        outside = []
    shown, rest = numbered[:ITEMS_MAX], len(numbered) - ITEMS_MAX
    when = f"вне очереди, неделя {week}" if out_of_band else f"неделя {week}"
    lines = [f"Свежесть стека, {when}." + (f" Отстало {len(numbered)}:" if numbered else "")]
    lines += [item_line(i, r, parts[r["имя"]]) for i, r in enumerate(shown, 1)]
    if rest > 0:
        lines.append(f"Отстало ещё {rest}, в список не влезли.")
    lines += [outside_line(r, parts[r["имя"]], neighbours, conf) for r in outside]
    lines += events_lines(measurement.get("события", []))
    if not out_of_band:
        silent = long_unknown(measurement, env_now)
        if silent:
            lines.append(f"Не мерится {len(silent)} {plural(len(silent), 'часть', 'части', 'частей')} "
                         f"больше {UNKNOWN_DAYS} суток: {', '.join(silent)}.")
        os_row = next((r for r in measurement["части"] if parts.get(r["имя"], {}).get("свежая", {}).get("источник") == "apt-ждущие"
                       and r["состояние"] in STATE_OF_ATTENTION), None)
        if os_row:
            phased = sum(1 for r in measurement["части"] if r["состояние"] == "поэтапно")
            lines.append(f"Ещё {os_row['выпусков_новее']} обычных обновлений ОС" + (f" (поэтапно: {phased})" if phased else "") + ".")
    if shown:
        lines.append(f"Не напоминать про пункт: «стек нет {len(shown)}».")
    return "\n".join(lines), {str(i): {"имя": r["имя"], "версия": r["свежая"]} for i, r in enumerate(shown, 1)}


def due(now, last_sent_key, pending):
    """Срок недельной сводки: понедельник с 10:00 по поясу владельца, либо лежит метка «отложена»; раз в ISO-неделю."""
    if last_sent_key == week_key(now):
        return False
    return bool(pending) or (now.weekday() == 0 and now.hour >= 10)


def record_question(log_dir, ts, week, items, out_of_band, text):
    line = json.dumps({"ts": ts.isoformat(), "неделя": week, "пункты": items, "вне_очереди": out_of_band,
                       "текст": text}, ensure_ascii=False)
    # Читатель с splitlines() считает эти знаки концом строки; JSON-escape возвращает их дословно.
    for sep in (" ", " ", "\x85"):
        line = line.replace(sep, "\\u%04x" % ord(sep))
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / QUESTIONS_LOG, "a", encoding="utf-8") as out:
        out.write(line + "\n")


def read_questions(log_dir):
    """Список строк журнала по времени; битая строка — ошибка, а не пустота: пропуск был бы тихим."""
    try:
        text = (log_dir / QUESTIONS_LOG).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            row["_ts"] = dt.datetime.fromisoformat(row["ts"])
        except (ValueError, KeyError) as error:
            raise ValueError(f"{QUESTIONS_LOG}:{number}: {error}")
        rows.append(row)
    return sorted(rows, key=lambda r: r["_ts"])


def budget_ok(questions, now, n_items):
    """≤1 вне очереди в сутки, и пунктов за сутки не больше потолка (спека 4.5)."""
    today = [q for q in questions if q["_ts"].astimezone().date() == now.date()]
    return not any(q["вне_очереди"] for q in today) and sum(len(q["пункты"]) for q in today) + n_items <= ITEMS_MAX


def out_of_band_candidates(measurement, parts, deferred, questions):
    named = {(item["имя"], item["версия"]) for q in questions for item in q["пункты"].values()}
    numbered, _ = rows_for_summary(measurement, parts, deferred)
    return [r for r in numbered if r["состояние"] == "отстала_с_заплаткой"
            and (r["суток_отставания"] or 0) > OUT_OF_BAND_MIN_DAYS and (r["имя"], r["свежая"]) not in named]


def hold_reason(env, pause_script, limit_script):
    """'пауза' | 'лимит' | None. Код вне 0/1 — ошибка двери, а не «не стоит»."""
    for script, args, word in ((pause_script, "--идёт-ли", "пауза"), (limit_script, "--лимит-держится", "лимит")):
        code, _, err = run_argv([sys.executable, str(env.project_dir / "scripts" / script), args])
        if code not in (0, 1):
            raise RuntimeError(f"{script} {args}: код {code} {err.strip()[:60]}")
        if code == 0:
            return word
    return None


def command_summary(env):
    measurement = read_state_file(env.stack_dir / "замер.json")
    if measurement is None:
        print("молчу: замера нет")
        return 3
    parts, deferred, now = part_map(env), measurement.get("отложено", {}), env.ctx.now
    questions = read_questions(env.log_dir)
    last_asked = questions[-1]["_ts"] if questions else None
    # Событие уже названо в прошлом сообщении — второй раз не повторяем.
    measurement["события"] = [e for e in measurement.get("события", [])
                              if last_asked is None or dt.datetime.fromisoformat(e["когда"]) > last_asked]
    pending = measurement.get("сводка_отложена")
    sent_key = max((week_key(q["_ts"].astimezone()) for q in questions if not q["вне_очереди"]), default=None)
    week = now.isocalendar()[1]
    numbered, outside = rows_for_summary(measurement, parts, deferred)
    has_news = bool(numbered or outside or measurement.get("события") or long_unknown(measurement, now))
    kind, only = None, None
    if has_news and due(now, sent_key, pending):
        kind = "weekly"
    else:
        candidates = out_of_band_candidates(measurement, parts, deferred, questions)
        if candidates and budget_ok(questions, now, len(candidates[:ITEMS_MAX])):
            kind, only = "out_of_band", {r["имя"] for r in candidates[:ITEMS_MAX]}
    if kind is None:
        if pending and not has_news:
            measurement.pop("сводка_отложена")
            write_json_atomic(env.stack_dir / "замер.json", measurement)
        print("молчу: " + ("нечего рекомендовать" if not has_news else
                          "уже ушла на этой неделе" if sent_key == week_key(now) else "не срок"))
        return 3
    held = hold_reason(env, "pause.py", "limit_mark.py")
    if kind == "weekly":
        measurement["сводка_отложена"] = {"с": (pending or {}).get("с", now.isoformat()),
                                          "причина": held or "не доставлена", "неделя": week}
        write_json_atomic(env.stack_dir / "замер.json", measurement)
    if held:
        print(f"отложено: {held}")
        return 3
    text, items = summary_text(measurement, parts, deferred, week, env.neighbours, now, kind == "out_of_band", only,
                               env.ctx.conf)
    write_json_atomic(env.stack_dir / "сводка-готова.json",
                      {"неделя": week, "пункты": items, "текст": text, "вне_очереди": kind == "out_of_band"})
    print(f"шлю: {week}" + (" вне_очереди" if kind == "out_of_band" else ""))
    print(text)
    return 0


def command_sent(env, week, out_of_band):
    prepared = read_state_file(env.stack_dir / "сводка-готова.json")
    if prepared is None:
        print("нечего записывать: сводка не готовилась")
        return 2
    record_question(env.log_dir, env.ctx.now, week, prepared["пункты"], out_of_band, prepared["текст"])
    (env.stack_dir / "сводка-готова.json").unlink()
    if not out_of_band:
        measurement = read_state_file(env.stack_dir / "замер.json") or {}
        if measurement.pop("сводка_отложена", None) is not None:
            write_json_atomic(env.stack_dir / "замер.json", measurement)
    log_line(env, f"сводка недели {week} доставлена" + (" (вне очереди)" if out_of_band else ""))
    return 0


# ── ответы владельца ────────────────────────────────────────────────────────
_MARKED_LINE = re.compile(r"^\s*" + MARKER + r"(?!\w)[\s:,.\-—]*(.*)$", re.I | re.S)
_BARE_YES_NO = re.compile(r"^\s*(да|нет)(?:[\s,.;]+\d+)+\s*$", re.I)


def parse_line(rest, items, week):
    """({номер: 'да'|'нет'}, отказ) для одной строки после метки."""
    verdicts, last_word = {}, None
    segments = [s for s in re.split(r"[,;]", rest) if s.strip()] or [""]
    for segment in segments:
        words = re.findall(r"[^\W\d_]+|\d+", segment.lower())
        marks = [w for w in words if w in ("да", "нет")]
        numbers = [int(w) for w in words if w.isdigit()]
        everything = "все" in words
        word = marks[0] if len(set(marks)) == 1 else (last_word if not marks else None)
        if len(set(marks)) > 1:
            return {}, "в одном куске и «да», и «нет»: раздели запятой"
        if word is None and numbers:
            return {}, "нужно слово «да» или «нет»: «стек да 1 2»"
        if not numbers and not everything:
            return {}, "назови номера: «стек да 1 2»"
        wanted = [int(k) for k in items] if everything else numbers
        for number in wanted:
            if str(number) not in items:
                return {}, f"пункта {number} в сводке недели {week} нет"
            verdicts[number] = word
        last_word = word
    return verdicts, None


def parse_answer(text, items, week=None):
    verdicts = {}
    marked = False
    for line in text.split("\n"):
        found = _MARKED_LINE.match(line)
        if not found:
            continue
        marked = True
        got, refusal = parse_line(found.group(1), items, week)
        if refusal:
            return Answer([], [], refusal)
        verdicts.update(got)
    if not marked:
        return Answer([], [], None, hint=bool(_BARE_YES_NO.match(text)))
    return Answer(sorted(n for n, w in verdicts.items() if w == "да"),
                  sorted(n for n, w in verdicts.items() if w == "нет"), None)


def answers_since(inbox_rows, question, next_ts=None):
    last = question["_ts"] + dt.timedelta(days=ANSWER_WINDOW_DAYS)
    if next_ts is not None:
        last = min(last, next_ts)
    found = []
    for row in inbox_rows:
        try:
            moment = dt.datetime.fromisoformat(row["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        # message_id обязателен: по нему видно, что слово пришло из канала, а не записано рукой.
        if not row.get("message_id") or not question["_ts"] < moment <= last:
            continue
        answer = parse_answer(str(row.get("text", "")), question["пункты"], question.get("неделя"))
        if answer.yes or answer.no or answer.refusal or answer.hint:
            found.append(answer._replace(message_id=row["message_id"]))
    return found


def read_inbox(env):
    try:
        text = (env.log_dir / "inbox.jsonl").read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0
    rows, broken = [], 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            broken += 1
    return rows, broken


def command_answers(env):
    questions = read_questions(env.log_dir)
    measurement = read_state_file(env.stack_dir / "замер.json") or {}
    if not questions:
        print("ответов 0, отложено []")
        return 0
    question = questions[-1]
    rows, broken = read_inbox(env)
    handled_path = env.stack_dir / "ответы.json"
    handled = read_state_file(handled_path) or {}
    if handled.get("вопрос") != question["ts"]:
        handled = {"вопрос": question["ts"], "обработано": [], "сказано": [], "ждут": []}
    deferred, notices, count = measurement.get("отложено", {}), [], 0
    for answer in answers_since(rows, question):
        if answer.message_id in handled["обработано"]:
            continue
        handled["обработано"].append(answer.message_id)
        count += 1
        items = question["пункты"]
        for number in answer.no:
            deferred[items[str(number)]["имя"]] = items[str(number)]["версия"]
        for number in answer.yes:
            deferred.pop(items[str(number)]["имя"], None)
        if answer.yes:
            notices.append("установка не включена: сводка только рекомендует")
        if answer.hint:
            notices.append("для стека начни со слова «стек»: «стек да 1 2»")
        if answer.refusal:
            notices.append(answer.refusal)
    # Отказ владельцу «сказан» только после доставки (--сказано): до неё он лежит в «ждут» и печатается снова.
    waiting = handled.setdefault("ждут", [])
    for notice in notices:
        if notice not in handled["сказано"] and notice not in waiting:
            waiting.append(notice)
    held = hold_reason(env, "pause.py", "limit_mark.py") if waiting else None
    measurement["отложено"] = deferred
    if measurement.get("части"):
        write_json_atomic(env.stack_dir / "замер.json", measurement)
    write_json_atomic(handled_path, handled)
    print(f"ответов {count}, отложено {sorted(deferred)}" + (f", битых строк входящих {broken}" if broken else ""))
    if held:
        print(f"отложено: {held}")
    else:
        for notice in waiting:
            print(f"отказ: {notice}")
    return 0


def command_said(env, text):
    path = env.stack_dir / "ответы.json"
    handled = read_state_file(path) or {}
    if text in handled.get("ждут", []):
        handled["ждут"].remove(text)
        handled.setdefault("сказано", []).append(text)
        write_json_atomic(path, handled)
    return 0


# ── вход ────────────────────────────────────────────────────────────────────
def selftest():
    here = Path(__file__).resolve().parent
    code = 0
    for name in ("test_stack_freshness.py", "test_stack_watch.py"):
        code |= subprocess.run([sys.executable, "-B", str(here / name)]).returncode
    return code


def command_reboot(env):
    """Нужна ли перезагрузка и какое ядро ждём: слово и код для сторожа окна ротации (0 нужна, 1 не нужна, 3 неизвестно)."""
    registry = load_registry(env.registry_path)
    if isinstance(registry, Broken):
        print(f"неизвестно: {registry.reason}")
        return 3
    part = next((p for p in registry.parts if p["свежая"]["источник"] == "перезагрузка"), None)
    if part is None:
        print("неизвестно: в реестре нет части с источником «перезагрузка»")
        return 3
    current = read_current(part, env.ctx)
    if not isinstance(current, Current) or not current.version:
        print(f"неизвестно: {getattr(current, 'reason', 'текущее ядро не прочитано')}")
        return 3
    latest = source_reboot(part, current.version, env.ctx)
    if isinstance(latest, Unknown):
        print(f"неизвестно: {latest.reason}")
        return 3
    if not latest.newer:
        print("не нужна")
        return 1
    # Одно reboot-required без нового ядра: ждём то же ядро, что сейчас.
    expected = latest.version if numeric_key(latest.version) > numeric_key(current.version) else current.version
    print(f"нужна: {expected}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Свежесть стека харнеса")
    for flag, dest in (("--замер", "measure"), ("--печать", "show"), ("--сводка", "summary"),
                       ("--отправлено", "sent"), ("--ответы", "answers"), ("--вне-очереди", "oob"),
                       ("--selftest", "selftest"), ("--перезагрузка", "reboot")):
        parser.add_argument(flag, dest=dest, action="store_true")
    parser.add_argument("--неделя", dest="week", type=int)
    parser.add_argument("--сказано", dest="said")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    env = build_env()
    try:
        if args.reboot:
            return command_reboot(env)
        if args.said is not None:
            return command_said(env, args.said)
        if args.sent:
            return command_sent(env, args.week or env.ctx.now.isocalendar()[1], args.oob)
        if args.summary:
            return command_summary(env)
        if args.answers:
            return command_answers(env)
        code, rows = 0, None
        if args.measure:
            code, rows = command_measure(env)
        elif args.show:
            rows = (read_state_file(env.stack_dir / "замер.json") or {}).get("части", [])
        if args.show:
            print("\n".join(format_rows(rows)))
        return code
    except (RuntimeError, ValueError, OSError) as error:
        print(f"сбой: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
