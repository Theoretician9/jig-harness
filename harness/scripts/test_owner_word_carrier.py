#!/usr/bin/env python3
"""Судья слова владельца не слушает окружение: П-12, П-11 (без аргумента), П-6 (сигнатуры).

Спека §3.2, §6; план, часть А1. Путь M замера:
python-файл сам ставит HARNESS_OWNER_WORD_STEND=1, свой паспорт, свой каталог журналов, своё окно
и срок — и настоящая дверь открывается его же записью «Да». Карточка А закрывает это: окно, срок и
папку судья берёт только у носителя, а носитель — только у машины или аргументом в процессе пробы.

Случаи:
  П-12      перечень дверей — РАЗБОРОМ кода (ast по scripts/**/*.py, текст по *.sh): файл, который
            импортирует owner_word/layout_history и обращается через модуль к судье. Каждая дверь из
            разбора зовётся на предмет стенда при окружении стенда → отказ, предмет на месте. Дверь из
            разбора без вызова в этой пробе — провал: новая дверь не проходит молча.
  П-12 срок только CONSENT_BUTTON_TTL_MIN=999999999 в окружении и «Да» старше боевого срока → отказ.
  П-12 userns  в чужом пространстве имён пользователей носитель() → НетНосителя «…пространств…».
  П-11      носитель() без аргумента: граница "/", папка — LOG_DIR паспорта машины, а не окружения.
  П-6       у судей нет каталог_логов/окно_мин, есть носитель; у модуля нет стенд/КЛЮЧ_СТЕНДА.
  здоровый  свежее «Да» через Носитель стенда → True: без него зелень отказов ничего не стоит.
  П-12 bash-ворота (АР-1)  у каждого зова deploy_guard.py из bash (разбор) путь — переменная от
            readlink -f скрипта; сама bash-дверь на копии scripts/ с PROJECT_DIR на заглушку ворот
            «exit 0» отказывает, предмет цел, заглушка не звана (deploy.sh не запускается: И-2).
  П-12 свежесть (АР-1)  HARNESS_CONF с потолком 999999 и --свежесть-на двое суток назад не оживляют
            старое «Да» на публикацию; здоровый близнец со свежим «Да» открывает.
  П-12 писатели просьбы (АР-1)  spec_approval и self-set просят кнопку только через
            owner_word.ЗАПРОС_КНОПКИ: зов настоящего ask-consent.py из двери перехватывается (код 3).
  П-12 publish.sh пути (АР-2)  каждая цель «bash|python3 "$VAR/…"» из разбора publish.sh — от места
            скрипта: копия publish.sh (перепись, --по-слову, пропуск ворот) с PROJECT_DIR на подлог с
            заглушками; зов подлога или ни одного зова цели рядом с копией — провал; push перехвачен.
  П-12 ключи двери (АР-4)  копия drop-database.sh с законным «Да» в журнале паспорта стенда:
            DB_ADMIN_EXEC, BACKUP_DIR, LOG_DIR подлогом в окружении → снос заглушкой паспорта, дамп и
            след у паспорта, метки подлога нет. Каждая bash-дверь режима двери из разбора: её ключи
            (паспорт стенда, умолчания «${KEY:-» двери, доводы и умолчания загрузчика) подлогом в
            окружении после konf_zagruzit_dveri равны значению без подлога — при паспорте стенда,
            машины и паспорте стенда без ключей двери (в нём ключ KEY="${KEY:-…}"). publish.sh: --паспорт сборщику и гейту чистоты —
            паспорт двери при PUBLIC_PASPORT/HARNESS_HOME на подлог. Обратный: обычный konf_zagruzit
            ставит окружение старше файла.

Двери мутаций: OWNER_WORD_UNDER_TEST — какой файл судьи грузить (и в процесс пробы, и в процесс
каждой двери, через sys.modules); LAYOUT_DOOR_UNDER_TEST — какой файл layout-door.py звать;
DOOR_UNDER_TEST — файл двери вместо одноимённого настоящего в случаях АР-1 (drop-database.sh,
publish.sh, stop_reminder.sh, deploy_guard.py, spec_approval.py, self-set.py).

Стенд — только tempfile.mkdtemp(prefix="nrp-stand-"); журналы стенда пишет сама проба. Двери, которые
просят кнопку, зовутся так, что просьбы нет (--сухо, без --просить, заглушка программы просьбы,
заглушка events.emit у ask-consent): даже ошибка двери не положит просьбу в боевой журнал. Сверх
того проба читает приращение боевых журналов согласия за свой прогон: строка с предметом стенда
там — провал.

Код выхода: 0 — неудач 0; 1 — есть неудачи; 77 — неудач нет, но userns не судится ни одной формой.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime
import hashlib
import importlib.util
import inspect
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
LIB = SCRIPTS / "lib"
JUDGE_PATH = Path(os.environ.get("OWNER_WORD_UNDER_TEST") or LIB / "owner_word.py").resolve()
LAYOUT_DOOR_PATH = Path(os.environ.get("LAYOUT_DOOR_UNDER_TEST") or SCRIPTS / "layout-door.py").resolve()
# В проекте хуки и демоны лежат иначе, чем в пакете (scripts/hooks | harness/hooks).
HOOKS = next((d for d in (SCRIPTS / "hooks", SCRIPTS.parent / "hooks") if d.is_dir()), SCRIPTS / "hooks")
DEMONS = next((d for d in (SCRIPTS.parent / "harness" / "demons", SCRIPTS.parent / "demons") if d.is_dir()),
              None)

# Метка строки результата из дочернего процесса: всё прочее в выводе двери — её речь.
MARK = "РЕЗУЛЬТАТ-ПРОБЫ "
OWNER = "4242"
KEY = "HARNESS_OWNER_WORD_STEND"
# Судьи, чьё чтение и есть слово владельца (спека §6 П-12 плюс читатели слов из контракта А0 шаг 4).
WORD_JUDGES = {"судить_кнопкой", "согласие_кнопкой", "разрешение", "последнее_решение", "последнее_нет",
               "consent_after", "слова_владельца", "слова_владельца_с", "открытый_запрос", "отзыв_после"}
JUDGE_MODULES = {"owner_word", "layout_history"}
# Сами судьи — не двери: в них определение, а не вызов.
NOT_DOORS = {"owner_word.py", "layout_history.py"}
# Страж разбора: без них перечень заведомо неполон (план А1 шаг 1).
MUST_BE_DOORS = {"self-set.py", "layout-door.py", "tg-dispatcher.sh", "deploy_guard.py"}
# Сигнатуры интерфейса карточки А (контракт А0, шаг 4).
P6_JUDGES = ("судить_кнопкой", "согласие_кнопкой", "разрешение", "последнее_решение", "последнее_нет",
             "слова_владельца", "слова_владельца_с", "открытый_запрос", "отзыв_после", "попросить_кнопку",
             "конфиг_права", "срок_кнопки_мин")
FORBIDDEN_PARAMS = {"каталог_логов", "окно_мин", "log_dir"}
CARRIER_FIELDS = {"папка", "uid", "паспорт", "граница", "окно_мин", "срок_кнопки_мин"}
# Ключи окружения стенда: «чистое» окружение — без них.
STAND_ENV_KEYS = (KEY, "HARNESS_LOG_DIR", "LOG_DIR", "HARNESS_INSTALL_CONF", "HARNESS_CONF", "HARNESS_HOME",
                  "CONFIRM_MAX_AGE_MIN", "SECRET_DELETE_FRESH_MIN", "CONSENT_BUTTON_TTL_MIN",
                  "HARNESS_EVENTS_STATE", "STATE_DIR", "SECRETS_DIR")
STAND_DB = "proba_nrp_stand"
ASK_DB = "proba_nrp_stand_ask"
SPEC_TASK = "nrp.stand-spec"
SELF_SUBJECT = ("цель", "проба nrp стенд")
FULL_UID_MAP = "0 0 4294967295"

results: list[tuple[str, str, str]] = []   # (исход, имя, подробность)


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append(("ок" if ok else "ПЛОХО", name, detail))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + (f": {detail}" if detail and not ok else ""), flush=True)


def not_judged(name: str, why: str) -> None:
    results.append(("НЕ СУДИТСЯ", name, why))
    print(f"  НЕ СУДИТСЯ {name}: {why}", flush=True)


# ── загрузка судьи и стенд ───────────────────────────────────────────────────

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_judge():
    """Судья под пробой в процессе пробы: его каталог первым, чтобы соседи шли из той же копии."""
    sys.path.insert(0, str(JUDGE_PATH.parent))
    return load("owner_word", JUDGE_PATH)


def new_stand() -> Path:
    # Единственный литерал префикса задачи: его знает уборщик /tmp (tmp-sweep.yaml, ЧР1).
    return Path(tempfile.mkdtemp(prefix="nrp-stand-"))


def stamp(ago_s: float) -> str:
    return datetime.datetime.fromtimestamp(time.time() - ago_s).astimezone().isoformat(timespec="seconds")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def envelope(event: str, kind: str, target: str, ago_s: float, nonce: str) -> dict:
    data = {"nonce": nonce, "вид": kind, "цель": target, "message_id": 700}
    if event != "consent.request":
        data["from_id"] = int(OWNER)
    return {"v": 1, "ts": stamp(ago_s), "src": "test_owner_word_carrier", "event": event, "outcome": "ok",
            "data": data}


def press(log: Path, kind: str, target: str, ago_s: float, answer: str = "consent.yes") -> None:
    """Две строки замера: просьба двери и нажатие владельца (from_id) — живой порядок."""
    nonce = hashlib.sha256(f"{kind} {target} {ago_s}".encode()).hexdigest()[:22]
    append_jsonl(log / "consent-requests.jsonl", envelope("consent.request", kind, target, ago_s + 60, nonce))
    append_jsonl(log / "consent-buttons.jsonl", envelope(answer, kind, target, ago_s, nonce))


def write_conf(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()), encoding="utf-8")


def stand_carrier(ow, log: Path, term_min: int | None):
    """Носитель стенда — аргумент судье в процессе пробы (единственный законный путь стенда)."""
    cls = getattr(ow, "Носитель", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise LookupError("в судье нет dataclass Носитель")
    known = {"папка": log, "uid": None, "паспорт": {"TG_CHAT_ID": OWNER, "LOG_DIR": str(log)},
             "граница": log, "окно_мин": 60, "срок_кнопки_мин": term_min, "ручки": {}}
    fields = dataclasses.fields(cls)
    unknown = [f.name for f in fields if f.name not in known
               and f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
    if unknown:
        raise LookupError(f"у Носитель обязательные поля, которых проба не знает: {unknown}")
    return cls(**{f.name: known[f.name] for f in fields if f.name in known})


def machine_conf(name: str) -> dict:
    import config
    return config.из_файла(f"{config.корень_машины()}/{name}")


def machine_term_min() -> int:
    raw = str(machine_conf("harness.conf").get("CONSENT_BUTTON_TTL_MIN") or "").strip()
    return int(raw) if raw.isdigit() else 15


# ── разбор дверей (П-12, П-6) ────────────────────────────────────────────────

def python_door_hits(path: Path) -> list[tuple[int, str]]:
    """Обращения к судьям ЧЕРЕЗ импортированный модуль судьи; одноимённая своя функция — не в счёт."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    aliases, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name in JUDGE_MODULES}
        elif isinstance(node, ast.ImportFrom) and node.module in JUDGE_MODULES:
            names |= {a.asname or a.name for a in node.names if a.name in WORD_JUDGES}
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases and node.attr in WORD_JUDGES):
            hits.append((node.lineno, node.attr))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
            hits.append((node.lineno, node.func.id))
    return sorted(hits)


# Флаги интерпретатора (-I и т. п.) — своя группа: доводы вставки от них не зависят.
INSERT = re.compile(r"python3((?:\s+-[A-Za-z]+)*)\s+-(\s[^<\n]*)<<'PY'[^\n]*\n(.*?)\nPY\n", re.S)
GUARD_CALL = re.compile(r"python[\d.]*\s[^\n]*?(?<![\w-])deploy_guard\.py")
# Выражение каталога перед /deploy_guard.py в кавычках: «$X», «${X}», «$X/scripts», «${a:-$B}/scripts».
GUARD_PATH = re.compile(r'"([^"\s]*)/deploy_guard\.py"')
INSERT_JUDGE = re.compile(r"owner_word\.(" + "|".join(sorted(WORD_JUDGES)) + r")\(")


def shell_door_hits(path: Path) -> list[dict]:
    """Двери в bash: python-вставка с import owner_word и судьёй либо зов deploy_guard.py."""
    text = door_file(path.name, path).read_text(encoding="utf-8", errors="replace")
    hits = []
    for found in INSERT.finditer(text):
        body = found[3]
        if re.search(r"^\s*import owner_word\b", body, re.M) and INSERT_JUDGE.search(body):
            hits.append({"как": "вставка", "строка": text.count("\n", 0, found.start()) + 1,
                         "доводы": found[2].split(), "текст": body,
                         "зов": found[0].split("<<'PY'")[0].strip()})
    for number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#") and GUARD_CALL.search(line):
            found = GUARD_PATH.search(line)
            hits.append({"как": "через deploy_guard", "строка": number,
                         "выражение": found[1] if found else line.strip()})
    return hits


def shell_files() -> list[Path]:
    # Хуки пакета лежат вне scripts/: без них перечень пакета короче перечня проекта.
    return (sorted(SCRIPTS.rglob("*.sh")) + (sorted(DEMONS.glob("*.sh")) if DEMONS else [])
            + (sorted(HOOKS.glob("*.sh")) if SCRIPTS not in HOOKS.parents and HOOKS != SCRIPTS else []))


def parse_doors() -> dict[str, dict]:
    """{имя файла: {"путь", "питон" | "баш"}} — перечень дверей по коду, а не по списку."""
    doors: dict[str, dict] = {}
    roots = [SCRIPTS] + ([HOOKS] if SCRIPTS not in HOOKS.parents else [])
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or path.name.startswith("test_") or path.name in NOT_DOORS:
                continue
            hits = python_door_hits(path)
            if hits:
                doors[path.name] = {"путь": path, "питон": hits}
    for path in shell_files():
        if path.name.startswith("test_"):
            continue
        hits = shell_door_hits(path)
        if hits:
            doors[path.name] = {"путь": path, "баш": hits}
    return doors


# ── раннер: дверь в дочернем процессе с судьёй под пробой ────────────────────

def intercept_requests(spec: dict) -> list[dict]:
    """Просьба кнопки мимо заглушки стенда не уходит из процесса двери никогда: зов настоящего
    ask-consent.py или tg_send.sh записывается и получает код 3 вместо запуска. Так красный код
    писателя просьбы не пошлёт владельцу ни одной кнопки, а проба увидит, что он шёл мимо."""
    allowed = Path(spec["заглушка_просьбы"]).resolve() if spec.get("заглушка_просьбы") else None
    bypass: list[dict] = []
    real_run = subprocess.run

    def run(command, *rest, **options):
        words = [str(w) for w in command] if isinstance(command, (list, tuple)) else str(command).split()
        for word in words:
            if Path(word).name in ("ask-consent.py", "tg_send.sh") and Path(word).resolve() != allowed:
                env = options.get("env") or os.environ
                bypass.append({"мимо": word, "HARNESS_LOG_DIR": env.get("HARNESS_LOG_DIR")})
                as_text = any(options.get(k) for k in ("text", "universal_newlines", "encoding", "errors"))
                said = "перехвачено пробой\n"
                return subprocess.CompletedProcess(command, 3, said if as_text else said.encode(), "" if as_text else b"")
        return real_run(command, *rest, **options)
    subprocess.run = run
    return bypass


def runner(spec: dict) -> int:
    """Дочерний процесс: судья под пробой встаёт в sys.modules ДО двери, дверь берёт его же."""
    sys.path.insert(0, str(Path(spec["судья"]).parent))
    ow = load("owner_word", Path(spec["судья"]))
    if spec.get("носитель_стенда"):
        # Машина стенда: у неё в папке лежит «Да», которого в боевом журнале нет и быть не должно.
        given = spec["носитель_стенда"]
        carrier = dataclasses.replace(stand_carrier(ow, Path(given["папка"]), None), ручки=given["ручки"])
        ow.носитель = lambda: carrier
    if spec.get("заглушка_просьбы"):
        ow.ЗАПРОС_КНОПКИ = Path(spec["заглушка_просьбы"])
    bypass = intercept_requests(spec)
    record: list[dict] = []
    if spec.get("след_emit"):
        import events

        def emit_stub(src, event, outcome, journal=None, data=None, **rest):
            # Просьба не пишется никуда: ask-consent — единственный писатель журнала запросов.
            record.append({"каталог": str(events.log_dir()), "журнал": journal, "событие": event})
            return True
        events.emit = emit_stub
    code = 0
    try:
        if spec["как"] == "запуск":
            sys.argv = [spec["файл"], *spec["доводы"]]
            try:
                runpy.run_path(spec["файл"], run_name="__main__")
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        else:
            module = load(spec["модуль"], Path(spec["файл"]))
            for name, value in (spec.get("заплатки") or {}).items():
                setattr(module, name, lambda *_a, _value=value, **_k: _value)
            result = getattr(module, spec["функция"])(**spec["доводы"])
            print(MARK + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    finally:
        if spec.get("след_emit"):
            Path(spec["след_emit"]).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if spec.get("след_мимо"):
            Path(spec["след_мимо"]).write_text(json.dumps(bypass, ensure_ascii=False), encoding="utf-8")
        elif bypass:
            print(f"ПЕРЕХВАТ-ПРОСЬБЫ: {bypass}", file=sys.stderr, flush=True)
    return code


def run_door(spec: dict, env: dict, stdin: str = "") -> tuple[int, str, object]:
    spec = {"судья": str(JUDGE_PATH), **spec}
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--раннер",
                           json.dumps(spec, ensure_ascii=False)],
                          input=stdin, capture_output=True, text=True, env=env, timeout=120,
                          cwd=env.get("HARNESS_LOG_DIR") or "/")
    output = done.stdout + done.stderr
    result = None
    for line in done.stdout.splitlines():
        if line.startswith(MARK):
            result = json.loads(line[len(MARK):])
    return done.returncode, output, result


def tail(text: str, size: int = 300) -> str:
    return " ".join(text.split())[-size:]


# ── П-12: стенд и вызов каждой двери ─────────────────────────────────────────

class P12Stand:
    """Один стенд на все двери: у каждой свой предмет, и на каждый лежит «Да» владельца стенда."""

    def __init__(self) -> None:
        self.root = new_stand()
        self.log, self.etc = self.root / "log", self.root / "etc"
        self.secrets, self.tree = self.root / "secrets", self.root / "tree"
        for folder in (self.log, self.secrets, self.root / "backup", self.tree, self.root / "events"):
            folder.mkdir(parents=True)
        write_conf(self.etc / "install.conf", {
            "LOG_DIR": self.log, "TG_CHAT_ID": OWNER, "SECRETS_DIR": self.secrets,
            "BACKUP_DIR": self.root / "backup", "STATE_DIR": self.root / "state",
            "PROJECT_DIR": self.tree, "AUTONOMY": "semi"})
        write_conf(self.etc / "harness.conf", {
            "CONFIRM_MAX_AGE_MIN": "999999", "CONSENT_BUTTON_TTL_MIN": "999999999",
            "DROPPABLE_DB_PREFIX": "proba_", "DEPLOY_NEEDS_OWNER": "1", "PUSH_NEEDS_OWNER": "1",
            "DEPLOY_CONSENT_STANDING": "0"})
        self.guard_target = self.secrets / "g.txt"
        self.delete_target = self.secrets / "p.txt"
        for target in (self.guard_target, self.delete_target):
            target.write_text("предмет стенда\n", encoding="utf-8")
        (self.log / "inbox.jsonl").write_text(json.dumps(
            {"ts": stamp(30), "chat_id": OWNER, "message_id": "551", "text": "Удали дамп старого продукта из копии"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        press(self.log, "удаление", os.path.realpath(self.guard_target), 60)
        press(self.log, "удаление", os.path.realpath(self.delete_target), 60)
        press(self.log, "снос-базы", STAND_DB, 60)
        press(self.log, "снос-базы", ASK_DB, 60, answer="consent.no")
        self.instance = self.root / "instance.yaml"
        self.instance.write_text("копия: стенд\nцель: прежняя\n", encoding="utf-8")
        press(self.log, "слой-человека", "=".join(SELF_SUBJECT), 60)
        self.ask_stub = self.root / "ask_stub.py"
        self.ask_stub.write_text('print("кнопка уже ушла (заглушка стенда)")\nraise SystemExit(3)\n',
                                 encoding="utf-8")
        spec_file = self.tree / "spec.md"
        spec_file.write_text("# спека стенда\n", encoding="utf-8")
        (self.tree / "dev-map.yaml").write_text(json.dumps(
            {"задачи": [{"id": SPEC_TASK, "status": "plan", "spec": "spec.md"}]}, ensure_ascii=False),
            encoding="utf-8")
        append_jsonl(self.log / "спеки-владельцу.jsonl",
                     {"ts": stamp(600), "событие": "отправлена", "задача": SPEC_TASK, "файл": "spec.md"})
        press(self.log, "спека", f"{SPEC_TASK} {hashlib.sha256(spec_file.read_bytes()).hexdigest()}", 60)
        append_jsonl(self.log / "раскладка-спрошено.jsonl", {"ts": stamp(3600), "пути": ["x.txt"]})
        append_jsonl(self.log / "confirmations.jsonl",
                     {"ts": stamp(60), "message_id": "552", "chat_id": OWNER, "text": "да, убрать"})
        self.memo: dict[str, tuple[bool, str]] = {}

    def once(self, name: str, call) -> tuple[bool, str]:
        """Дверь зовётся один раз: bash-двери через deploy_guard.py делят его вердикт."""
        if name not in self.memo:
            self.memo[name] = call()
        return self.memo[name]

    def env(self) -> dict:
        """Путь M целиком: ключ стенда, свой паспорт, свой каталог, своё окно и срок."""
        base = {k: v for k, v in os.environ.items() if k not in STAND_ENV_KEYS}
        return {**base, KEY: "1", "HARNESS_LOG_DIR": str(self.log), "LOG_DIR": str(self.log),
                "HARNESS_INSTALL_CONF": str(self.etc / "install.conf"),
                "HARNESS_CONF": str(self.etc / "harness.conf"), "HARNESS_HOME": str(self.etc),
                "CONFIRM_MAX_AGE_MIN": "999999", "CONSENT_BUTTON_TTL_MIN": "999999999",
                "HARNESS_EVENTS_STATE": str(self.root / "events"), "STATE_DIR": str(self.root / "state"),
                "SECRETS_DIR": str(self.secrets), "SELF_INSTANCE": str(self.instance),
                "SELF_SET_ASK_CONSENT": str(self.ask_stub), "PYTHONDONTWRITEBYTECODE": "1"}

    def clean_env(self) -> dict:
        return {k: v for k, v in os.environ.items() if k not in STAND_ENV_KEYS}

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def door_guard(stand: P12Stand, path: Path) -> tuple[bool, str]:
    """Сторож судит команду: вердикт — код выхода (2 — отменил, 0 — пропустил)."""
    command = f"HARNESS_DATA_DELETE_WHY='проба nrp' rm -f {stand.guard_target}"
    code, out, _ = run_door({"как": "запуск", "файл": str(path), "доводы": []}, stand.env(),
                            stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    if code == 0:
        return False, "сторож ПРОПУСТИЛ rm по «Да» стенда (путь M)"
    return code == 2 and "Traceback" not in out, f"код {code}: {tail(out)}"


def door_protected_delete(stand: P12Stand, path: Path) -> tuple[bool, str]:
    # --сухо: дверь ничего не удаляет и кнопку не просит; открытие видно по коду 0 «сухо: удалил бы».
    code, out, _ = run_door({"как": "запуск", "файл": str(path),
                             "доводы": [str(stand.delete_target), "--сухо"]}, stand.env())
    if not stand.delete_target.exists():
        return False, "предмет стенда удалён"
    if code == 0:
        return False, f"дверь ОТКРЫЛАСЬ по «Да» стенда (путь M): {tail(out, 160)}"
    return code == 1 and "ОТКАЗ" in out and "Traceback" not in out, f"код {code}: {tail(out)}"


def door_deploy_guard(stand: P12Stand, path: Path) -> tuple[bool, str]:
    # dropdb: действие без ручки — ни стоящее согласие, ни AUTONOMY машины его не открывают,
    # так что открыть его может только «Да», а «Да» здесь есть только у стенда.
    code, out, _ = run_door({"как": "запуск", "файл": str(path), "доводы": [
        "--действие", "dropdb", "--база", STAND_DB, "--только-согласие"]}, stand.env())
    if code == 0:
        return False, f"ворота ОТКРЫЛИСЬ по «Да» стенда (путь M): {tail(out, 160)}"
    return code == 1 and "нет согласия" in out and "Traceback" not in out, f"код {code}: {tail(out)}"


def door_self_set(stand: P12Stand, path: Path) -> tuple[bool, str]:
    before = stand.instance.read_bytes()
    field, value = SELF_SUBJECT
    code, out, _ = run_door({"как": "запуск", "файл": str(path),
                             "доводы": [field, value, "--почему", "проба nrp"]}, stand.env())
    if stand.instance.read_bytes() != before:
        return False, f"слой человека ЗАПИСАН по «Да» стенда (путь M), код {code}"
    return code in (2, 3) and "Traceback" not in out, f"код {code}: {tail(out)}"


def door_layout(stand: P12Stand, path: Path) -> tuple[bool, str]:
    code, out, result = run_door({"как": "вызов", "файл": str(path), "модуль": "layout_door_under_test",
                                  "функция": "seed_consent",
                                  "доводы": {"candidates": {"x.txt": "0" * 40}, "selftest": False}},
                                 stand.env())
    if result is None:
        return False, f"seed_consent не ответила, код {code}: {tail(out)}"
    agreed = result[0]
    if agreed:
        return False, f"затравка СОГЛАСОВАНА «да, убрать» стенда (путь M): {sorted(agreed)}"
    return True, ""


def door_spec(stand: P12Stand, path: Path) -> tuple[bool, str]:
    code, out, result = run_door({"как": "вызов", "файл": str(path), "модуль": "spec_approval_under_test",
                                  "функция": "найти_одобрение",
                                  "доводы": {"id_задачи": SPEC_TASK, "корень": str(stand.tree)}},
                                 stand.env())
    if code != 0 or "Traceback" in out:
        return False, f"код {code}: {tail(out)}"
    if result:
        return False, f"спека ОДОБРЕНА «Да» стенда (путь M): {str(result)[:120]}"
    return True, ""


def door_ask(stand: P12Stand, path: Path) -> tuple[bool, str]:
    """ask-consent судит «Нет» владельца (тишина сутки): «Нет» стенда его заглушать не вправе."""
    trace = stand.root / "emit.json"
    code, out, _ = run_door({"как": "запуск", "файл": str(path), "след_emit": str(trace), "доводы": [
        "--вид", "снос-базы", "--база", ASK_DB, "--почему", "проба nrp"]}, stand.env())
    emitted = json.loads(trace.read_text(encoding="utf-8")) if trace.exists() else []
    stray = [e for e in emitted if Path(e["каталог"]).resolve() != stand.log.resolve()]
    if stray:
        return False, f"просьба легла бы не в журнал двери: {stray[0]['каталог']}"
    if code == 3:
        return False, f"«Нет» стенда заглушило просьбу (путь M): {tail(out, 160)}"
    return "Traceback" not in out, f"код {code}: {tail(out)}"


def door_insert(stand: P12Stand, hit: dict) -> tuple[bool, str]:
    """Вставка самотеста с судьёй: окружение стенда не вправе менять её вердикт.

    Вставка сама вправе дать судье носитель стенда (проба в своём процессе), поэтому судится
    не «отказ», а разница: тот же вызов с окружением стенда и без него обязан ответить одинаково.
    """
    if len(hit["доводы"]) != 2:
        return False, f"вставка зовётся с доводами {hit['доводы']} — проба знает только (lib, каталог)"
    argv = [str(JUDGE_PATH.parent), str(stand.log)]

    def verdict(env: dict) -> str:
        done = subprocess.run([sys.executable, "-", *argv], input=hit["текст"], capture_output=True,
                              text=True, env=env, timeout=60, cwd=str(stand.root))
        return done.stdout.strip() if done.returncode == 0 else "отказ"
    with_stand, without = verdict(stand.env()), verdict(stand.clean_env())
    return with_stand == without, f"с окружением стенда «{with_stand}», без него «{without}» (путь M)"


# ── П-12 bash-двери ворот: путь к deploy_guard.py и запуск самой двери ───────
# Принцип (АР-1, находки 2 и 5): путь к судье берётся от РЕАЛЬНОГО места скрипта (readlink -f),
# а не из окружения. Вердикт ворот здесь не в счёт: зовётся сама bash-дверь, а PROJECT_DIR и прочее
# окружение показывают на каталог стенда с заглушкой ворот «exit 0», у которой свой след.

# Дверь мутаций: файл двери (bash-дверь, deploy_guard.py, писатель просьбы) вместо одноимённого настоящего.
DOOR_PATH = os.environ.get("DOOR_UNDER_TEST")
GATE_REACHED = "согласи"   # настоящие ворота говорят о согласии всегда; заглушка — никогда
PLAIN_VAR = re.compile(r"\$\{?([A-Za-z_]\w*)\}?(/[^$\"]*)?")
REAL_LOCATION = re.compile(r'readlink -f "?\$(\{BASH_SOURCE\[0\]\}|0)"?')
# Двери, которые проба не запускает, и почему: путь их ворот судит только статический случай.
NOT_LAUNCHED = {"deploy.sh": "выкат запускает только оркестратор (И-2)"}
STAND_BASH_DB = "proba_nrp_stand_bash"
# Явная дверь стенда (АР-3, дыра б): каталог паспорта стенда для КОПИИ двери. Конфиг из
# HARNESS_CONF/HARNESS_INSTALL_CONF/HARNESS_HOME дверь не исполняет; стенд называет свой паспорт
# этой переменной, а дверь верит ей, только если сама лежит вне PROJECT_DIR паспорта машины.
STAND_HOME_KEY = "HARNESS_DOOR_STAND_HOME"


def guard_path_trouble(text: str, expression: str) -> str:
    """Почему путь к воротам может прийти из окружения; пусто — путь от реального места скрипта."""
    found = PLAIN_VAR.fullmatch(expression)
    if not found:
        return f"путь «{expression}» — не одна переменная от readlink -f (в нём умолчание или окружение)"
    name = found[1]
    sets = re.findall(rf"^\s*(?:local |readonly |declare -r |export )?{name}=(.*)$", text, re.M)
    if not sets:
        return f"${name} в файле не присвоена — приходит из окружения или конфига"
    loose = [value for value in sets if not REAL_LOCATION.search(value)]
    return f"${name} присвоена не от readlink -f: {loose[0][:90]}" if loose else ""


def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", "-c", "user.name=nrp", "-c", "user.email=nrp@stand",
                           "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {tail(done.stderr, 200)}")
    return done.stdout.strip()


def repo_with_commit(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    git("init", "-q", "-b", branch, str(path))
    (path / "f.txt").write_text("стенд\n", encoding="utf-8")
    git("add", "f.txt", cwd=path)
    git("commit", "-q", "-m", "стенд", cwd=path)
    return path


# Заглушка программы просьбы: пишет свой зов рядом с собой и отвечает «кнопка уже ушла» (код 3).
REQUEST_STUB = '''import json, os, sys
from pathlib import Path
with open(Path(__file__).resolve().with_name(Path(__file__).name + ".calls.jsonl"), "a", encoding="utf-8") as s:
    s.write(json.dumps({"argv": sys.argv[1:], "HARNESS_LOG_DIR": os.environ.get("HARNESS_LOG_DIR")},
                       ensure_ascii=False) + "\\n")
print("кнопка уже ушла (заглушка стенда)")
raise SystemExit(3)
'''


def request_stub_calls(stub: Path) -> list[dict]:
    calls = stub.with_name(stub.name + ".calls.jsonl")
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]


class BashGateStand:
    """Копия scripts/ в раскладке установки — настоящие двери и ворота; fake/ — то, что даёт окружение.

    В копии ask-consent.py — заглушка с записью зова: после правки настоящие ворота попросят кнопку
    (--просить), и просьба не уйдёт владельцу. owner_word копии — судья под пробой.
    """

    def __init__(self) -> None:
        self.root = new_stand()
        self.real = self.root / "real" / "scripts"
        shutil.copytree(SCRIPTS, self.real, ignore=shutil.ignore_patterns("__pycache__"))
        if SCRIPTS not in HOOKS.parents and HOOKS != SCRIPTS:
            shutil.copytree(HOOKS, self.real / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(JUDGE_PATH, self.real / "lib" / "owner_word.py")
        (self.real / "ask-consent.py").write_text(REQUEST_STUB, encoding="utf-8")
        if DOOR_PATH:
            same = [p for p in self.real.rglob(Path(DOOR_PATH).name) if "__pycache__" not in p.parts]
            if len(same) != 1:
                raise RuntimeError(f"DOOR_UNDER_TEST: в копии {len(same)} файлов {Path(DOOR_PATH).name}")
            shutil.copyfile(DOOR_PATH, same[0])
            shutil.copymode(DOOR_PATH, same[0])   # дверь python запускается своим файлом (АР-3)
        self.fake = self.root / "fake"
        self.trace = self.root / "guard-stub-trace.jsonl"
        (self.fake / "scripts").mkdir(parents=True)
        (self.fake / "scripts" / "deploy_guard.py").write_text(
            "import json, sys\n"
            f"with open({str(self.trace)!r}, 'a', encoding='utf-8') as s:\n"
            "    s.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
            "print('предмет: стенд')\nprint('заглушка ворот стенда: открыто')\n", encoding="utf-8")
        self.log, self.etc = self.root / "log", self.root / "etc"
        self.log.mkdir()
        (self.log / "inbox.jsonl").write_text("", encoding="utf-8")
        self.package = repo_with_commit(self.root / "pkg", "pubb")
        self.bare = self.root / "pub.git"
        git("clone", "-q", "--bare", str(self.package), str(self.bare))
        git("remote", "add", "nrp-stand-pub", str(self.bare), cwd=self.package)
        self.build = self.root / "build"
        git("clone", "-q", "-b", "pubb", str(self.bare), str(self.build))
        write_conf(self.etc / "install.conf", {
            "LOG_DIR": self.log, "TG_CHAT_ID": OWNER, "PROJECT_DIR": self.fake, "AGENT_USER": "nrp",
            "BACKUP_DIR": self.root / "backup", "AUTONOMY": "semi"})
        write_conf(self.etc / "harness.conf", {
            "CONSENT_FRESHNESS_ANCHOR_MAX_MIN": "999999", "CONSENT_BUTTON_TTL_MIN": "999999999",
            "DROPPABLE_DB_PREFIX": "proba_", "PUBLISH_NEEDS_OWNER": "1", "PUSH_NEEDS_OWNER": "1",
            "PUBLIC_BUILD_DIR": self.build, "PUBLIC_REMOTE": "nrp-stand-pub", "PUBLIC_BRANCH": "pubb",
            "PUBLIC_COMMIT_EMAIL": "nrp@stand",
            # Ключи двери — только паспортом (АР-4): окружением дверь их не берёт.
            "DB_ADMIN_EXEC": self.root / "fake-db.sh", "STARTER_DIR": self.package})
        # Снос базы — подставной командой стенда: «база» — файл-метка, настоящей СУБД нет.
        self.db = self.root / "db"
        self.db.mkdir()
        (self.db / STAND_BASH_DB).write_text("база стенда\n", encoding="utf-8")
        self.db_calls = self.root / "db-calls.log"
        self.db_exec = self.root / "fake-db.sh"
        self.db_exec.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {self.db_calls}\n"
            "case \"$1\" in\n"
            "  pg_dump) echo '-- дамп стенда' ;;\n"
            f"  psql) if [[ \"$*\" == *-tAc* ]]; then ls {self.db} | grep -q . && echo 1; true\n"
            f"        else sql=$(cat); [[ \"$sql\" == *DROP* ]] && rm -f {self.db}/*; true; fi ;;\n"
            "esac\n", encoding="utf-8")
        self.db_exec.chmod(0o755)

    def env(self) -> dict:
        base = {k: v for k, v in os.environ.items() if k not in STAND_ENV_KEYS + ("PROJECT_DIR", "STARTER_DIR")}
        return {**base, KEY: "1", "PROJECT_DIR": str(self.fake), "STARTER_DIR": str(self.package),
                "HARNESS_LOG_DIR": str(self.log), "LOG_DIR": str(self.log),
                "HARNESS_INSTALL_CONF": str(self.etc / "install.conf"),
                "HARNESS_CONF": str(self.etc / "harness.conf"), "HARNESS_HOME": str(self.etc),
                "BACKUP_DIR": str(self.root / "backup"), "DB_ADMIN_EXEC": str(self.db_exec),
                "DROPPABLE_DB_PREFIX": "proba_", "CONFIRM_MAX_AGE_MIN": "999999",
                "CONSENT_BUTTON_TTL_MIN": "999999999", "CONSENT_FRESHNESS_ANCHOR_MAX_MIN": "999999",
                STAND_HOME_KEY: str(self.etc), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(self, door: Path, args: list[str], stdin: str = "", cwd: Path | None = None) -> tuple[int, str]:
        self.trace.unlink(missing_ok=True)
        done = subprocess.run(["bash", str(door), *args], input=stdin, capture_output=True, text=True,
                              env=self.env(), timeout=180, cwd=str(cwd or self.root))
        return done.returncode, done.stdout + done.stderr

    def stub_called(self) -> list:
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def gate_verdict(stand: BashGateStand, code: int, out: str, lost: str) -> tuple[bool, str]:
    """Отказ двери засчитан, если заглушка не звана, предмет цел и отказали настоящие ворота."""
    called = stand.stub_called()
    if called:
        return False, f"дверь позвала ЗАГЛУШКУ ворот из PROJECT_DIR {called[0]} (код {code}); {lost}: {tail(out, 200)}"
    if lost:
        return False, f"{lost} (код {code}): {tail(out, 200)}"
    if GATE_REACHED not in out:
        return False, f"до ворот дверь не дошла — отказ не их (код {code}): {tail(out, 200)}"
    return "Traceback" not in out, f"код {code}: {tail(out)}"


def launch_drop_database(stand: BashGateStand, door: Path) -> tuple[bool, str]:
    code, out = stand.run(door, [STAND_BASH_DB])
    lost = []
    if not (stand.db / STAND_BASH_DB).exists():
        lost.append("база стенда СНЕСЕНА")
    if stand.db_calls.exists():
        lost.append(f"СУБД стенда тронута: {stand.db_calls.read_text(encoding='utf-8').split()[:2]}")
    if code == 0:
        lost.append("дверь ответила 0")
    return gate_verdict(stand, code, out, "; ".join(lost))


def launch_publish(stand: BashGateStand, door: Path) -> tuple[bool, str]:
    before = git("for-each-ref", cwd=stand.bare)
    code, out = stand.run(door, ["--проверка"])
    lost = []
    if git("for-each-ref", cwd=stand.bare) != before:
        lost.append("удалённый стенда ИЗМЕНЁН")
    if code == 0:
        lost.append("дверь ответила 0")
    return gate_verdict(stand, code, out, "; ".join(lost))


def launch_stop_reminder(stand: BashGateStand, door: Path) -> tuple[bool, str]:
    """Хук Stop судит воротами «можно ли пушить»: в репозитории стенда коммит ждёт отправки."""
    repo = stand.fake
    if not (repo / ".git").exists():
        git("init", "-q", "-b", "main", str(repo))
        (repo / "f.txt").write_text("1\n", encoding="utf-8")
        git("add", "f.txt", cwd=repo)
        git("commit", "-q", "-m", "1", cwd=repo)
        git("clone", "-q", "--bare", str(repo), str(stand.root / "up.git"))
        git("remote", "add", "origin", str(stand.root / "up.git"), cwd=repo)
        git("fetch", "-q", "origin", cwd=repo)
        git("branch", "-q", "--set-upstream-to", "origin/main", cwd=repo)
        (repo / "f.txt").write_text("2\n", encoding="utf-8")
        git("commit", "-q", "-am", "2", cwd=repo)
    code, out = stand.run(door, [], stdin="{}", cwd=repo)
    called = stand.stub_called()
    if called:
        return False, f"хук позвал ЗАГЛУШКУ ворот из PROJECT_DIR {called[0]} (код {code}): {tail(out, 200)}"
    if "коммит(ов)" not in out:
        return False, f"до ворот хук не дошёл (код {code}): {tail(out, 200)}"
    return "Traceback" not in out, f"код {code}: {tail(out)}"


SHELL_LAUNCHERS = {"drop-database.sh": launch_drop_database, "publish.sh": launch_publish,
                   "stop_reminder.sh": launch_stop_reminder}


def copy_of(stand: BashGateStand, path: Path) -> Path:
    """Та же дверь в копии: хуки пакета в копии лежат в scripts/hooks, как после установки."""
    if SCRIPTS in path.parents:
        return stand.real / path.relative_to(SCRIPTS)
    return stand.real / "hooks" / path.name


def judge_bash_gate_doors(doors: dict) -> None:
    gated = {name: door for name, door in doors.items()
             if any(hit["как"] == "через deploy_guard" for hit in door.get("баш", []))}
    report("П-12 разбор bash-ворот: звенья через deploy_guard.py есть, среди них drop-database.sh и publish.sh",
           {"drop-database.sh", "publish.sh"} <= set(gated), f"звенья у {sorted(gated)}")
    for name, door in sorted(gated.items()):
        text = door["путь"].read_text(encoding="utf-8", errors="replace")
        for hit in door["баш"]:
            if hit["как"] == "через deploy_guard":
                trouble = guard_path_trouble(text, hit["выражение"])
                report(f"П-12 {name}:{hit['строка']} путь к deploy_guard.py — от readlink -f скрипта, "
                       "не из окружения", not trouble, trouble)
    stand = None
    try:
        for name, door in sorted(gated.items()):
            title = (f"П-12 {name}: bash-дверь, PROJECT_DIR — стенд с заглушкой ворот → отказ, "
                     "предмет на месте, заглушка не вызвана")
            if name in NOT_LAUNCHED:
                not_judged(title, NOT_LAUNCHED[name])
                continue
            launch = SHELL_LAUNCHERS.get(name)
            if launch is None:
                report(title, False, "у bash-двери из разбора нет запуска в пробе — допиши")
                continue
            try:
                stand = stand or BashGateStand()
                ok, why = launch(stand, copy_of(stand, door["путь"]))
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as trouble:
                ok, why = False, f"стенд не собран: {type(trouble).__name__}: {trouble}"
            report(title, ok, why)
    finally:
        if stand is not None:
            stand.close()


# ── П-12 publish.sh: исполняемые пути — от места скрипта (АР-2) ──────────────
# Судья у publish.sh уже от readlink -f (АР-1), но ворота, сборщик, сторожа и сверка предмета
# брались от $PROJECT_DIR: подложный PROJECT_DIR с заглушками «exit 0» вёл выкат мимо настоящих
# ворот (И-2). Перечень — РАЗБОРОМ publish.sh: каждый «bash|python3 "$ПЕРЕМЕННАЯ/…"». Копия
# publish.sh идёт по трём путям (перепись, «--по-слову», пропуск ворот по записи); у каждой цели
# две заглушки — рядом с копией («настоящая») и в подлоге PROJECT_DIR. Зов подлога — провал;
# ни одного зова настоящей — тоже провал: путь не достигнут, и зелень ничего бы не стоила.
# Наружу ничего не уходит: удалённый — голый репозиторий стенда, а git push перехватывает
# обёртка git стенда (код 1); tg_send.sh — заглушка с обеих сторон, токенов в окружении нет.

EXEC_CALL = re.compile(r'(?:^|[\s;(|&!{])(?:bash|python3(?:\s+-I)?)\s+"\$\{?([A-Za-z_]\w*)\}?/([^"$]+\.(?:sh|py))"')
STAND_TASK = "задача-стенд"
STUB_ANSWERS = {   # что цель печатает настоящему вызывающему: без ответа путь дальше не идёт
    "build-public.py": "собрано", "version.py": json.dumps({"следующая": "0.0.1", "почему": "стенд"}),
    "vitrina_status.py": f"{STAND_TASK} (статус test)", "deploy_guard.py": "предмет: стенд"}
EXEC_STUB = '''import json, sys, uuid
from pathlib import Path
key, side, trace, args = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
with open(trace, "a", encoding="utf-8") as s:
    s.write(json.dumps({"цель": key, "сторона": side, "argv": args}, ensure_ascii=False) + "\\n")
if "--selftest" in args:
    raise SystemExit(0)
answer = json.loads(%r).get(key.rsplit("/", 1)[-1], "")
if answer == "собрано":
    куда = Path(args[args.index("--куда") + 1])
    (куда / f"стенд-{uuid.uuid4().hex}.txt").write_text("сборка стенда\\n", encoding="utf-8")
    answer = json.dumps({"пакет": "стенд"})
if answer:
    print(answer)
'''
GIT_WRAPPER = '''#!/usr/bin/env bash
# git стенда: всё, кроме push, — настоящий git; push не уходит никуда.
for a in "$@"; do
    [ "$a" = push ] && { printf '%%s\\n' "$*" >> %s; echo "push перехвачен стендом" >&2; exit 1; }
done
exec %s "$@"
'''


def publish_exec_targets(text: str) -> dict[str, list[int]]:
    """Цель (путь под scripts/) → строки зова. Переменная любая: судится не имя, а место."""
    targets: dict[str, list[int]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for _var, rest in EXEC_CALL.findall(line):
            key = rest.lstrip("/").removeprefix("scripts/")
            # Зов самой двери — её самотест гоняет копию в своём стенде; заглушка на её месте
            # подменила бы предмет пробы, и ни одна цель не была бы достигнута.
            if key != "publish.sh":
                targets.setdefault(key, []).append(number)
    return targets


class PublishPathStand:
    """real/ — раскладка установки: копия publish.sh, lib/config.sh и заглушки целей; fake/ — подлог."""

    def __init__(self, door: Path, targets: dict[str, list[int]]) -> None:
        self.root = new_stand()
        self.real, self.fake = self.root / "real", self.root / "fake"
        self.trace, self.pushes = self.root / "exec-trace.jsonl", self.root / "push-attempts.log"
        scripts = self.real / "scripts"
        (scripts / "lib").mkdir(parents=True)
        shutil.copyfile(door, scripts / "publish.sh")
        shutil.copyfile(LIB / "config.sh", scripts / "lib" / "config.sh")
        helper = self.root / "exec-stub.py"
        helper.write_text(EXEC_STUB % json.dumps(STUB_ANSWERS, ensure_ascii=False), encoding="utf-8")
        for side, top in (("настоящая", self.real), ("подлог", self.fake)):
            for key in targets:
                self.stub(top / "scripts" / key, helper, key, side)
            repo_with_trailer(top)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "git").write_text(GIT_WRAPPER % (self.pushes, shutil.which("git")), encoding="utf-8")
        (self.bin / "git").chmod(0o755)
        self.log, self.etc = self.root / "log", self.root / "etc"
        self.log.mkdir()
        self.package = repo_with_commit(self.root / "pkg", "pubb")
        self.bare = self.root / "pub.git"
        git("clone", "-q", "--bare", str(self.package), str(self.bare))
        git("remote", "add", "nrp-stand-pub", str(self.bare), cwd=self.package)
        self.build = self.root / "build"
        git("clone", "-q", "-b", "pubb", str(self.bare), str(self.build))
        write_conf(self.etc / "install.conf", {"LOG_DIR": self.log, "TG_CHAT_ID": OWNER,
                                               "PROJECT_DIR": self.fake, "AGENT_USER": "nrp"})
        write_conf(self.etc / "harness.conf", {
            "PUBLIC_BUILD_DIR": self.build, "PUBLIC_REMOTE": "nrp-stand-pub", "PUBLIC_BRANCH": "pubb",
            "PUBLIC_COMMIT_EMAIL": "nrp@stand", "PUBLIC_COMMIT_NAME": "nrp-stand",
            "STARTER_DIR": self.package})   # ключ двери — только паспортом (АР-4)

    def stub(self, path: Path, helper: Path, key: str, side: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        call = [str(helper), key, side, str(self.trace)]
        if path.suffix == ".sh":
            path.write_text("#!/usr/bin/env bash\nexec python3 " + " ".join(f"'{a}'" for a in call)
                            + ' "$@"\n', encoding="utf-8")
        else:
            path.write_text(f"import runpy, sys\nsys.argv[1:1] = {call[1:]!r}\n"
                            f"runpy.run_path({call[0]!r}, run_name='__main__')\n", encoding="utf-8")

    def env(self) -> dict:
        base = {k: v for k, v in os.environ.items()
                if k not in STAND_ENV_KEYS + ("PROJECT_DIR", "STARTER_DIR") and "TOKEN" not in k
                and not k.startswith(("TG_", "PUBLIC_"))}
        return {**base, "PATH": f"{self.bin}:{base.get('PATH', '/usr/bin:/bin')}",
                "PROJECT_DIR": str(self.fake), "STARTER_DIR": str(self.package),
                "HARNESS_LOG_DIR": str(self.log), "LOG_DIR": str(self.log),
                "HARNESS_INSTALL_CONF": str(self.etc / "install.conf"),
                # Паспорт сборщику и гейту чистоты — от паспорта двери (АР-4): окружение называет подлог.
                "HARNESS_CONF": str(self.etc / "harness.conf"), "HARNESS_HOME": str(self.fake / "etc"),
                "PUBLIC_PASPORT": str(self.fake / "etc" / "install.conf"), STAND_HOME_KEY: str(self.etc),
                "PYTHONDONTWRITEBYTECODE": "1"}

    def run(self, args: list[str]) -> tuple[int, str]:
        done = subprocess.run(["bash", str(self.real / "scripts" / "publish.sh"), *args],
                              capture_output=True, text=True, env=self.env(), timeout=180,
                              cwd=str(self.root))
        return done.returncode, done.stdout + done.stderr

    def calls(self) -> list[dict]:
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    def rewritten_copy(self) -> tuple[Path, Path]:
        """Копия переписанной истории (то же дерево, другой коммит) и бэкап удалённого."""
        copy = self.root / "kopiya"
        git("clone", "-q", "-b", "pubb", str(self.bare), str(copy))
        tree = git("rev-parse", "HEAD^{tree}", cwd=copy)
        fresh = git("commit-tree", tree, "-m", "переписано стендом", cwd=copy)
        git("update-ref", "refs/heads/pubb", fresh, cwd=copy)
        bundle = self.root / "backup.bundle"
        git("bundle", "create", str(bundle), "--all", cwd=self.bare)
        return copy, bundle

    def waiver(self) -> None:
        now = int(time.time())
        (self.log / "publish-gates-waiver.json").write_text(json.dumps(
            {"слово": "без прогона полного", "id_входящего": "553", "причина": "стенд",
             "выдана": now - 60, "до": now + 3600}, ensure_ascii=False), encoding="utf-8")

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def repo_with_trailer(path: Path) -> None:
    """Корень проекта стенда: репозиторий с трейлером задачи позже публичной ветки (замку 3 есть что судить)."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", str(path))
    later = datetime.datetime.now().astimezone() + datetime.timedelta(hours=1)
    when = later.isoformat(timespec="seconds")
    done = subprocess.run(["git", "-c", "user.name=nrp", "-c", "user.email=nrp@stand", "commit", "-q",
                           "--allow-empty", "-m", f"стенд\n\nDev-Map: {STAND_TASK}"], cwd=path,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
    if done.returncode != 0:
        raise RuntimeError(f"git commit: {tail(done.stderr, 200)}")


def case_publish_exec_paths() -> None:
    door = door_file("publish.sh", SCRIPTS / "publish.sh")
    targets = publish_exec_targets(door.read_text(encoding="utf-8"))
    named = {"gates.sh", "build-public.py", "lib/door_subjects.py"}
    report("П-12 publish.sh разбор: исполняемые цели найдены, среди них gates.sh, build-public.py, "
           "lib/door_subjects.py", named <= set(targets), f"разбор дал {sorted(targets)}")
    if not targets:
        return
    stand, runs = None, []
    try:
        stand = PublishPathStand(door, targets)
        heads_before = git("for-each-ref", cwd=stand.bare)
        copy, bundle = stand.rewritten_copy()
        runs.append(("перепись", *stand.run(["--переписать-историю", str(copy), "--бэкап", str(bundle)])))
        runs.append(("по слову", *stand.run(["--по-слову"])))
        stand.waiver()
        runs.append(("пропуск ворот", *stand.run([])))
        # Самотест двери сам называет паспорт своих дворов — паспорт судится по зовам до него.
        door_calls = stand.calls()
        runs.append(("самотест", *stand.run(["--selftest"])))
        calls = stand.calls()
        passport = str(stand.etc / "install.conf")
        pushed = stand.pushes.read_text(encoding="utf-8").count("\n") if stand.pushes.exists() else 0
        heads_after = git("for-each-ref", cwd=stand.bare)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as trouble:
        report("П-12 publish.sh стенд путей собран", False, f"{type(trouble).__name__}: {trouble}")
        return
    finally:
        if stand is not None:
            stand.close()
    report("П-12 publish.sh стенд путей: удалённый стенда не тронут (push перехвачен обёрткой git)",
           heads_after == heads_before, f"перехвачено push {pushed}; ссылки удалённого изменились")
    said = "; ".join(f"{name}: код {code}, {tail(out, 160)}" for name, code, out in runs)
    for key, lines in sorted(targets.items()):
        fake = [c["argv"] for c in calls if c["цель"] == key and c["сторона"] == "подлог"]
        real = [c for c in calls if c["цель"] == key and c["сторона"] == "настоящая"]
        title = (f"П-12 publish.sh: {key} (стр. {', '.join(map(str, lines))}) — от места скрипта, "
                 "подложный PROJECT_DIR: заглушка подлога не вызвана")
        if fake:
            report(title, False, f"зван ПОДЛОГ из PROJECT_DIR {len(fake)} раз(а), первый {fake[0][:4]}")
        elif not real:
            report(title, False, f"цель не достигнута ни одним прогоном — допиши прогон ({said})")
        else:
            report(title, True)
    # АР-4: PUBLIC_PASPORT и HARNESS_HOME окружения называют подлог; паспорт сборщику (замок 6) и гейту
    # чистоты переписи — паспорт двери. Ни одного зова с паспортом — цель не достигнута, тоже провал.
    for key in ("build-public.py", "check-public-clean.py"):
        given = [c["argv"][c["argv"].index("--паспорт") + 1] for c in door_calls if c["цель"] == key
                 and c["сторона"] == "настоящая" and "--паспорт" in c["argv"][:-1]]
        report(f"П-12 АР-4 publish.sh: {key} получает паспорт двери, не PUBLIC_PASPORT/HARNESS_HOME окружения",
               bool(given) and all(p == passport for p in given),
               f"паспорта зовов: {given or 'зовов с --паспорт нет'} ({said})")


# ── П-12 срок: --свежесть-на и потолок из окружения (АР-1, находка 1) ────────

def door_file(name: str, path: Path) -> Path:
    """Настоящая дверь — или её копия из DOOR_UNDER_TEST, если имя то же."""
    return Path(DOOR_PATH) if DOOR_PATH and Path(DOOR_PATH).name == name else path


def guard_publish(stand: BashGateStand, env: dict, anchor_ago_s: float) -> tuple[int, str]:
    carrier = {"папка": str(stand.log), "ручки": {
        "PUBLIC_BUILD_DIR": str(stand.build), "PUBLIC_REMOTE": "nrp-stand-pub", "PUBLIC_BRANCH": "pubb",
        "PUBLISH_NEEDS_OWNER": "1"}}
    code, out, _ = run_door({"как": "запуск", "файл": str(door_file("deploy_guard.py", SCRIPTS / "deploy_guard.py")), "носитель_стенда": carrier,
                             "заглушка_просьбы": str(stand.root / "request_stub.py"), "доводы": [
                                 "--действие", "публикация", "--только-согласие", "--публикация",
                                 str(stand.package), "--свежесть-на", str(int(time.time() - anchor_ago_s))]}, env)
    return code, out


def fresh_log(stand: BashGateStand) -> None:
    shutil.rmtree(stand.log)
    stand.log.mkdir()
    (stand.log / "inbox.jsonl").write_text("", encoding="utf-8")


def case_freshness_anchor() -> None:
    """Старое неиспользованное «Да» на публикацию лежит у машины (носитель стенда в процессе двери).

    Окружение двери — HARNESS_CONF с потолком 999999 и момент свежести двое суток назад: ворота
    обязаны отказать. Здоровый близнец — свежее «Да» и момент две минуты назад без подставного
    HARNESS_CONF — обязан открыть: иначе отказ мог прийти от несошедшегося предмета.
    """
    sick = ("П-12 deploy_guard публикация: HARNESS_CONF с потолком 999999 и --свежесть-на <2 сут> "
            "не оживляют старое «Да»")
    healthy = "здоровый: deploy_guard публикация, свежее «Да» и --свежесть-на <2 мин> → открыто"
    stand = None
    try:
        stand = BashGateStand()
        (stand.root / "request_stub.py").write_text(REQUEST_STUB, encoding="utf-8")
        import door_subjects
        subject = door_subjects.publish_subject(str(stand.package), str(stand.build), "nrp-stand-pub", "pubb")
        machine_env = {k: v for k, v in stand.env().items()
                       if k not in ("HARNESS_CONF", "CONSENT_FRESHNESS_ANCHOR_MAX_MIN")}
        press(stand.log, "публикация", subject, 300)
        code, out = guard_publish(stand, machine_env, 120)
        report(healthy, code == 0 and "согласие владельца есть" in out, f"код {code}: {tail(out)}")
        fresh_log(stand)
        old = 2 * 86400
        press(stand.log, "публикация", subject, old)
        code, out = guard_publish(stand, stand.env(), old - 60)
        if code == 0:
            report(sick, False, f"ворота ОТКРЫЛИСЬ старым «Да» по потолку из окружения: {tail(out, 200)}")
        else:
            report(sick, code == 1 and "Traceback" not in out, f"код {code}: {tail(out)}")
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ImportError) as trouble:
        report(sick, False, f"стенд не собран: {type(trouble).__name__}: {trouble}")
    finally:
        if stand is not None:
            stand.close()


# ── писатели просьбы: только через owner_word.ЗАПРОС_КНОПКИ (АР-1, находка 6) ─

def combat_requests_lines() -> int:
    path = Path(str(machine_conf("install.conf").get("LOG_DIR") or "/nonexistent")) / "consent-requests.jsonl"
    return len(path.read_bytes().splitlines()) if path.exists() else 0


def writer_verdict(stand: P12Stand, stub: Path, code: int, out: str, lines_before: int,
                   env: dict) -> tuple[bool, str]:
    trace = stand.root / "bypass.json"
    bypass = json.loads(trace.read_text(encoding="utf-8")) if trace.exists() else None
    calls = request_stub_calls(stub)
    lines_after = combat_requests_lines()
    if lines_after != lines_before:
        return False, f"боевой журнал запросов вырос: {lines_before} → {lines_after}"
    if bypass is None:
        return False, f"дверь не дописала след перехвата, код {code}: {tail(out, 200)}"
    if bypass:
        return False, (f"писатель позвал {bypass[0]['мимо']} сам, мимо owner_word.попросить_кнопку "
                       f"(перехвачено пробой, HARNESS_LOG_DIR={bypass[0]['HARNESS_LOG_DIR']})")
    if len(calls) != 1:
        return False, f"owner_word.ЗАПРОС_КНОПКИ позван {len(calls)} раз, ждали 1; код {code}: {tail(out, 200)}"
    own = env.get("HARNESS_LOG_DIR")
    if own and calls[0]["HARNESS_LOG_DIR"] != own:
        return False, f"просьба легла бы в {calls[0]['HARNESS_LOG_DIR']}, а журнал двери {own}"
    return "Traceback" not in out, f"код {code}: {tail(out)}"


def writer_env(stand: P12Stand, with_stand: bool) -> dict:
    """Окружение стенда — или «проба забыла HARNESS_LOG_DIR»: ни одного ключа стенда."""
    if with_stand:
        env = stand.env()
    else:
        env = {**stand.clean_env(), "SELF_INSTANCE": str(stand.instance), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("SELF_SET_ASK_CONSENT", None)
    return env


def writer_spec(name: str, path: Path) -> dict:
    if name == "spec_approval.py":
        # Хэш спеки подменён: судится только, КАК писатель просит кнопку, а не карта задач.
        return {"как": "вызов", "файл": str(path), "модуль": "spec_approval_under_test",
                "функция": "запросить_кнопку", "заплатки": {"_хэш_спеки": "0" * 64},
                "доводы": {"id_задачи": SPEC_TASK, "название": "проба nrp", "хэш_снимка": "0" * 64}}
    return {"как": "запуск", "файл": str(path), "доводы": ["цель", "проба nrp писатель", "--почему", "проба nrp"]}


REQUEST_WRITERS = ("spec_approval.py", "self-set.py")


def case_request_writers(doors: dict) -> None:
    stand = P12Stand()
    stub = stand.root / "request_stub.py"
    stub.write_text(REQUEST_STUB, encoding="utf-8")
    try:
        for name in REQUEST_WRITERS:
            for with_stand, label in ((True, "окружение стенда"), (False, "без HARNESS_LOG_DIR")):
                title = (f"П-12 писатель просьбы {name} ({label}): только через owner_word.ЗАПРОС_КНОПКИ, "
                         "боевой журнал запросов не растёт")
                door = doors.get(name)
                if door is None:
                    report(title, False, f"{name} нет в разборе дверей")
                    continue
                stub.with_name(stub.name + ".calls.jsonl").unlink(missing_ok=True)
                (stand.root / "bypass.json").unlink(missing_ok=True)
                env = writer_env(stand, with_stand)
                before = combat_requests_lines()
                code, out, _ = run_door({**writer_spec(name, door_file(name, door["путь"])), "заглушка_просьбы": str(stub),
                                         "след_мимо": str(stand.root / "bypass.json")}, env)
                ok, why = writer_verdict(stand, stub, code, out, before, env)
                report(title, ok, why)
    finally:
        stand.close()


PY_CALLERS = {"hooks/guard_bash.py": door_guard, "guard_bash.py": door_guard,
              "protected-delete.py": door_protected_delete, "deploy_guard.py": door_deploy_guard,
              "self-set.py": door_self_set, "layout-door.py": door_layout,
              "spec_approval.py": door_spec, "ask-consent.py": door_ask}


def judge_door(stand: P12Stand, name: str, door: dict, doors: dict) -> None:
    if "питон" in door:
        caller = PY_CALLERS.get(name)
        if caller is None:
            report(f"П-12 {name}: у двери из разбора есть вызов в пробе", False,
                   f"судьи {door['питон'][:3]} — допиши вызов двери на предмет стенда")
            return
        path = LAYOUT_DOOR_PATH if name == "layout-door.py" else door["путь"]
        ok, why = stand.once(name, lambda: caller(stand, path))
        report(f"П-12 {name}: окружение стенда → отказ, предмет на месте", ok, why)
        return
    for hit in door["баш"]:
        if hit["как"] == "вставка":
            ok, why = door_insert(stand, hit)
            report(f"П-12 {name}:{hit['строка']} вставка судьи: окружение стенда не меняет вердикт", ok, why)
            continue
        guard = doors.get("deploy_guard.py")
        if guard is None:
            report(f"П-12 {name}:{hit['строка']} через deploy_guard.py", False, "deploy_guard.py нет в разборе")
            continue
        ok, why = stand.once("deploy_guard.py", lambda: door_deploy_guard(stand, guard["путь"]))
        report(f"П-12 {name}:{hit['строка']} через deploy_guard.py → отказ", ok, why)


def case_p12_doors() -> None:
    try:
        doors = parse_doors()
    except (OSError, SyntaxError) as trouble:
        report("П-12 разбор дверей", False, f"разбор упал: {trouble}")
        return
    report("П-12 разбор: перечень дверей не пуст", bool(doors), "ни одной двери — разбор слеп")
    missing = sorted(MUST_BE_DOORS - set(doors))
    report("П-12 страж разбора: в перечне self-set.py, layout-door.py, tg-dispatcher.sh, deploy_guard.py",
           not missing, f"нет {missing}")
    print(f"  перечень дверей ({len(doors)}): {', '.join(sorted(doors))}", flush=True)
    stand = P12Stand()
    try:
        for name in sorted(doors):
            judge_door(stand, name, doors[name], doors)
    finally:
        stand.close()
    judge_bash_gate_doors(doors)
    case_request_writers(doors)
    case_interpreter_isolated(doors)
    case_conf_not_from_env(doors)
    case_door_conf_root()
    case_door_text(doors)
    case_door_keys_not_from_env()


# ── П-12 интерпретатор и конфиг двери не из окружения (АР-3) ─────────────────
# Живая проверка АЖ: PYTHONPATH на чужой каталог с sitecustomize.py исполняет чужой код в
# интерпретаторе двери ДО её первой строки — подменённый носитель открыл deploy_guard чужим «Да».
# Той же природы bash: config.sh исполняет `source` файла из HARNESS_CONF / HARNESS_INSTALL_CONF /
# HARNESS_HOME, и чужой конфиг определяет функцию python3(), которая отвечает «согласие есть».
# Судится поведение, а не текст: дверь python — запуском СВОЕГО файла (интерпретатор и его флаги
# выбирает shebang, `python3 файл` их не видит), bash-дверь — запуском самой двери; файл-метка
# чужого кода и предмет стенда — вердикт. Перечень дверей — тот же разбор, что у П-12.

SITE_STUB = r'''import json, os, sys
_cfg = json.loads(%r)
with open(_cfg["метка"], "a", encoding="utf-8") as _s:
    _s.write(json.dumps({"чужой": "sitecustomize", "argv": list(getattr(sys, "orig_argv", sys.argv))},
                        ensure_ascii=False) + "\n")
try:   # подмена судьи, как в АЖ: носитель — чужой журнал, где «Да» на каждый предмет стенда
    import dataclasses, importlib.util, pathlib
    sys.path.insert(0, _cfg["lib"])
    _spec = importlib.util.spec_from_file_location("owner_word", os.path.join(_cfg["lib"], "owner_word.py"))
    _ow = importlib.util.module_from_spec(_spec)
    sys.modules["owner_word"] = _ow
    _spec.loader.exec_module(_ow)
    _log = pathlib.Path(_cfg["журнал"])
    _known = {"папка": _log, "uid": None, "паспорт": {"TG_CHAT_ID": _cfg["владелец"], "LOG_DIR": str(_log)},
              "граница": _log, "окно_мин": 60, "срок_кнопки_мин": None, "ручки": {}}
    _carrier = _ow.Носитель(**{f.name: _known[f.name] for f in dataclasses.fields(_ow.Носитель)
                               if f.name in _known})
    _ow.носитель = lambda: _carrier
except Exception as _trouble:
    with open(_cfg["метка"], "a", encoding="utf-8") as _s:
        _s.write(json.dumps({"подмена не встала": repr(_trouble)}, ensure_ascii=False) + "\n")
'''
# Чужой конфиг bash: его исполнение видно меткой; python3() отвечает за ворота «согласие есть».
FOREIGN_CONF = '''printf '%%s\\n' "чужой конфиг исполнен: $0" >> %(mark)s
python3() { printf '%%s\\n' "python3() чужого конфига: $*" >> %(mark)s; echo 'согласие владельца есть'; return 0; }
'''
TG_STUB = "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$(dirname \"$0\")/tg_send.calls\"\nexit 0\n"
# Формы дыры (б): переменная окружения, которая называет конфиг двери.
CONF_FORMS = ("HARNESS_CONF", "HARNESS_INSTALL_CONF", "HARNESS_HOME")


class EnvStand(BashGateStand):
    """Копия scripts/ (как у bash-ворот) плюс чужой каталог: sitecustomize.py и конфиги с меткой."""

    def __init__(self) -> None:
        super().__init__()
        self.mark = self.root / "МЕТКА-чужой-код.log"
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.guard_target, self.delete_target = self.secrets / "g.txt", self.secrets / "p.txt"
        self.instance = self.root / "instance.yaml"
        self.flog = self.root / "foreign" / "log"
        self.flog.mkdir(parents=True)
        append_jsonl(self.flog / "inbox.jsonl", {"ts": stamp(30), "chat_id": OWNER, "message_id": "551",
                                                 "text": "Удали дамп старого продукта из копии"})
        self.reset()
        for target in (self.guard_target, self.delete_target):
            press(self.flog, "удаление", os.path.realpath(target), 60)
        for db in (STAND_DB, STAND_BASH_DB):
            press(self.flog, "снос-базы", db, 60)
        press(self.flog, "слой-человека", "=".join(SELF_SUBJECT), 60)
        self.site = self.root / "foreign" / "site"
        self.site.mkdir()
        (self.site / "sitecustomize.py").write_text(SITE_STUB % json.dumps(
            {"метка": str(self.mark), "lib": str(self.real / "lib"), "журнал": str(self.flog),
             "владелец": OWNER}, ensure_ascii=False), encoding="utf-8")
        self.foreign_etc = self.root / "foreign" / "etc"
        self.foreign_etc.mkdir()
        for name in ("install.conf", "harness.conf"):
            (self.foreign_etc / name).write_text(FOREIGN_CONF % {"mark": shlex.quote(str(self.mark))},
                                                 encoding="utf-8")
        # Канал наружу закрыт: tg_send копии — заглушка, прокси — в никуда.
        (self.real / "tg_send.sh").write_text(TG_STUB, encoding="utf-8")
        # ask-consent копии — заглушка просьбы (её зовут ворота); сама дверь ask-consent — рядом.
        source = door_file("ask-consent.py", SCRIPTS / "ask-consent.py")
        self.ask_door = self.real / "ask-consent-door.py"
        shutil.copyfile(source, self.ask_door)
        shutil.copymode(source, self.ask_door)
        (self.real / "ask-consent.py").write_text(REQUEST_STUB, encoding="utf-8")
        self.extra: dict = {}
        self.drop: tuple = ()

    def reset(self) -> None:
        """Предметы стенда — на место, метки нет: каждый запуск судится с чистого листа."""
        self.mark.unlink(missing_ok=True)
        for target in (self.guard_target, self.delete_target):
            target.write_text("предмет стенда\n", encoding="utf-8")
        self.instance.write_text("копия: стенд\nцель: прежняя\n", encoding="utf-8")
        (self.db / STAND_BASH_DB).write_text("база стенда\n", encoding="utf-8")
        self.db_calls.unlink(missing_ok=True)

    def env(self) -> dict:
        base = {k: v for k, v in super().env().items() if k not in self.drop}
        return {**base, "SECRETS_DIR": str(self.secrets), "SELF_INSTANCE": str(self.instance),
                "SELF_SET_ASK_CONSENT": str(self.real / "ask-consent.py"),
                "https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9", **self.extra}

    def marks(self) -> list[str]:
        return self.mark.read_text(encoding="utf-8").splitlines() if self.mark.exists() else []


def marks_trouble(stand: EnvStand) -> str:
    found = stand.marks()
    return f"исполнен ЧУЖОЙ код ({len(found)} меток), первая: {found[0][:200]}" if found else ""


# Запуск двери python своим файлом: доводы, ввод и признак «открыта». None — вход без побочек
# (у двери нет пути до судьи без боевой просьбы или вызова функции): судится только метка.
def exec_guard(stand: EnvStand):
    command = f"HARNESS_DATA_DELETE_WHY='проба nrp' rm -f {stand.guard_target}"
    return ([], json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            lambda code, out: code == 0)


def exec_protected_delete(stand: EnvStand):
    return ([str(stand.delete_target), "--сухо"], "",
            lambda code, out: code == 0 or not stand.delete_target.exists())


def exec_deploy_guard(stand: EnvStand):
    return ["--действие", "dropdb", "--база", STAND_DB, "--только-согласие"], "", lambda code, out: code == 0


def exec_self_set(stand: EnvStand):
    before = stand.instance.read_bytes()
    field, value = SELF_SUBJECT
    return [field, value, "--почему", "проба nrp"], "", lambda code, out: stand.instance.read_bytes() != before


PY_EXEC = {"guard_bash.py": exec_guard, "protected-delete.py": exec_protected_delete,
           "deploy_guard.py": exec_deploy_guard, "self-set.py": exec_self_set,
           "layout-door.py": lambda stand: (["--help"], "", None),
           "spec_approval.py": lambda stand: ([], "", None),
           "ask-consent.py": lambda stand: (["--help"], "", None)}


def exec_python_door(stand: EnvStand, name: str, door: Path) -> tuple[bool, str]:
    args, stdin, opened = PY_EXEC[name](stand)
    try:
        done = subprocess.run([str(door), *args], input=stdin, capture_output=True, text=True,
                              env=stand.env(), timeout=120, cwd=str(stand.root))
    except PermissionError as trouble:
        return False, f"дверь не запускается своим файлом (нет +x) — флаги интерпретатора не её: {trouble}"
    code, out = done.returncode, done.stdout + done.stderr
    lost = [marks_trouble(stand)]
    if opened is not None and opened(code, out):
        lost.append(f"дверь ОТКРЫТА (код {code})")
    lost = [item for item in lost if item]
    return not lost, "; ".join(lost) + f": {tail(out, 160)}"


def run_insert(stand: EnvStand, hit: dict) -> tuple[bool, str]:
    """Вставка зовётся ТЕМ ЖЕ зовом интерпретатора, что в файле двери (флаги — её)."""
    script = (f"LIB_PROBY={shlex.quote(str(stand.real / 'lib'))}; T={shlex.quote(str(stand.flog))}\n"
              f"{hit['зов']} <<'PY'\n{hit['текст']}\nPY\n")
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=stand.env(),
                          timeout=60, cwd=str(stand.root))
    trouble = marks_trouble(stand)
    return not trouble, f"{trouble}: {tail(done.stdout + done.stderr, 160)}"


def bash_door_run(stand: EnvStand, name: str, door: dict) -> tuple[bool, str]:
    """Bash-дверь целиком: ни один её python не исполнил чужой код, предмет цел."""
    if name in SHELL_LAUNCHERS:
        launched, why = SHELL_LAUNCHERS[name](stand, copy_of(stand, door["путь"]))
        trouble = marks_trouble(stand)
        return launched and not trouble, "; ".join(x for x in (trouble, "" if launched else why) if x)
    inserts = [hit for hit in door["баш"] if hit["как"] == "вставка"]
    if inserts:
        verdicts = [run_insert(stand, hit) for hit in inserts]
        return all(ok for ok, _ in verdicts), "; ".join(why for ok, why in verdicts if not ok)
    return False, "у bash-двери из разбора нет запуска в пробе — допиши"


def case_interpreter_isolated(doors: dict) -> None:
    """Дыра (а): PYTHONPATH с sitecustomize.py не исполняется ни в одной двери."""
    stand = None
    try:
        stand = EnvStand()
        stand.extra = {"PYTHONPATH": str(stand.site)}
        for name, door in sorted(doors.items()):
            if "питон" in door:
                title = f"П-12 {name}: запуск своим файлом с PYTHONPATH на чужой sitecustomize → метки нет"
                if name not in PY_EXEC:
                    report(title, False, "у двери python из разбора нет запуска в пробе — допиши")
                    continue
                title += ", дверь закрыта" if PY_EXEC[name](stand)[2] is not None else " (вход без побочек)"
                path = stand.ask_door if name == "ask-consent.py" else copy_of(stand, door["путь"])
                stand.reset()
                report(title, *exec_python_door(stand, name, path))
                continue
            title = f"П-12 {name}: bash-дверь с PYTHONPATH на чужой sitecustomize → ни одной метки, предмет цел"
            if name in NOT_LAUNCHED:
                not_judged(title, NOT_LAUNCHED[name])
                continue
            stand.reset()
            report(title, *bash_door_run(stand, name, door))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as trouble:
        report("П-12 АР-3 (а): стенд интерпретатора", False, f"стенд не собран: {type(trouble).__name__}: {trouble}")
    finally:
        if stand is not None:
            stand.close()


# Демон, чья дверь — вставка самотеста: самотест долог (tmux, свой стенд) и в пробе не идёт.
CONF_NOT_LAUNCHED = {"tg-dispatcher.sh": "диспетчер — демон; его вставка — часть самотеста, "
                                         "запуск диспетчера с чужим конфигом в пробе не идёт"}


def case_conf_not_from_env(doors: dict) -> None:
    """Дыра (б): bash-дверь не исполняет конфиг, названный окружением."""
    stand = None
    try:
        stand = EnvStand()
        install, harness = stand.foreign_etc / "install.conf", stand.foreign_etc / "harness.conf"
        forms = {"HARNESS_CONF": ({"HARNESS_CONF": str(harness)}, ()),
                 "HARNESS_INSTALL_CONF": ({"HARNESS_INSTALL_CONF": str(install), "INSTALL_CONF": str(install)}, ()),
                 "HARNESS_HOME": ({"HARNESS_HOME": str(stand.foreign_etc)},
                                  ("HARNESS_CONF", "HARNESS_INSTALL_CONF", "INSTALL_CONF"))}
        for name, door in sorted(doors.items()):
            if "баш" not in door:
                continue
            for form in CONF_FORMS:
                title = (f"П-12 {name}: {form} на чужой конфиг с python3() → конфиг не исполнен, "
                         "дверь закрыта")
                skip = NOT_LAUNCHED.get(name) or CONF_NOT_LAUNCHED.get(name)
                if skip:
                    not_judged(title, skip)
                    continue
                stand.extra, stand.drop = forms[form]
                stand.reset()
                report(title, *bash_door_run(stand, name, door))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as trouble:
        report("П-12 АР-3 (б): стенд конфига", False, f"стенд не собран: {type(trouble).__name__}: {trouble}")
    finally:
        if stand is not None:
            stand.close()


# ── П-12 АР-3 разбором и на функции config.sh ────────────────────────────────
# deploy.sh проба не запускает (И-2), и запуск копий не видит двух вещей: зова python3 без -I
# в deploy.sh и konf_zagruzit вместо режима двери там, где konf_dveri_puti уже поставил пути.
# Разбор судит текст каждой bash-двери с воротами. Функцию konf_dveri_puti судит прямой зов:
# стенд копий всегда называет HARNESS_DOOR_STAND_HOME, поэтому без него (и для настоящего
# дерева) путь паспорта судится только здесь.

# Зов интерпретатора в позиции команды (с присваиваниями окружения перед ним); подсказка в
# тексте сообщения («— python3 scripts/…») — не зов.
PY_CALL = re.compile(r"(?:^|[;&|(!]|\b(?:if|then|elif|while|nohup|exec))\s*"
                     r"(?:[A-Za-z_]\w*=\S*\s+)*python3(?=\s)(?!\s+-I\b)")
LOOSE_LOADER = re.compile(r"^\s*konf_zagruzit\b(?!_dveri)")


def case_door_text(doors: dict) -> None:
    gated = {name: door for name, door in doors.items()
             if any(hit["как"] == "через deploy_guard" for hit in door.get("баш", []))}
    report("П-12 АР-3 разбор: bash-двери с воротами есть, среди них deploy.sh", "deploy.sh" in gated,
           f"двери с воротами: {sorted(gated)}")
    for name, door in sorted(gated.items()):
        lines = door_file(name, door["путь"]).read_text(encoding="utf-8", errors="replace").splitlines()
        code = [(n, line) for n, line in enumerate(lines, 1) if not line.lstrip().startswith("#")]
        bare = [n for n, line in code if PY_CALL.search(line)]
        report(f"П-12 {name}: каждый зов python3 — с -I (разбор)", not bare, f"без -I: строки {bare[:6]}")
        loose = [n for n, line in code if LOOSE_LOADER.search(line)]
        own = any("konf_zagruzit_dveri" in line for _, line in code)
        report(f"П-12 {name}: конфиг — konf_zagruzit_dveri, не konf_zagruzit (разбор)", own and not loose,
               f"konf_zagruzit: строки {loose[:6]}" if loose else "konf_zagruzit_dveri не зван")


def door_conf_paths(lib: Path, door: Path, extra: dict) -> list[str]:
    script = ('source "$1"; konf_dveri_puti "$2"; '
              'printf "%s\\n" "$KONF_DVERI_KOREN" "$INSTALL_CONF" "$HARNESS_CONF"')
    keys = (*CONF_FORMS, "INSTALL_CONF", STAND_HOME_KEY)
    env = {**{k: v for k, v in os.environ.items() if k not in keys}, **extra}
    done = subprocess.run(["bash", "-c", script, "konf", str(lib), str(door)], capture_output=True,
                          text=True, env=env, timeout=30)
    return done.stdout.splitlines() + ([f"код {done.returncode}: {tail(done.stderr, 160)}"] if done.returncode else [])


def case_door_conf_root() -> None:
    """konf_dveri_puti: окружение не называет паспорт двери; стенду верит только копия вне PROJECT_DIR машины."""
    lib = door_file("config.sh", LIB / "config.sh")
    root = "/etc/harness" if os.path.isdir("/etc/harness") else f"{os.environ.get('HOME') or '/root'}/.harness"
    machine = [root, f"{root}/install.conf", f"{root}/harness.conf"]
    foreign = new_stand()
    try:
        copy = foreign / "kopiya" / "drop-database.sh"
        copy.parent.mkdir()
        copy.write_text("", encoding="utf-8")
        named = {"HARNESS_HOME": str(foreign), "HARNESS_CONF": str(foreign / "harness.conf"),
                 "HARNESS_INSTALL_CONF": str(foreign / "install.conf"), "INSTALL_CONF": str(foreign / "install.conf")}
        stand = [str(foreign), f"{foreign}/install.conf", f"{foreign}/harness.conf"]
        got = door_conf_paths(lib, copy, named)
        report("П-12 config.sh konf_dveri_puti: HARNESS_HOME/HARNESS_CONF/HARNESS_INSTALL_CONF без двери стенда → "
               "паспорт машины", got == machine, f"дверь взяла {got}")
        got = door_conf_paths(lib, copy, {STAND_HOME_KEY: str(foreign)})
        report("здоровый: config.sh konf_dveri_puti, копия вне PROJECT_DIR с дверью стенда → паспорт стенда",
               got == stand, f"дверь взяла {got}")
        title = ("П-12 config.sh konf_dveri_puti: настоящее дерево (внутри PROJECT_DIR машины) с "
                 f"{STAND_HOME_KEY} и HARNESS_* → паспорт машины, не стенда")
        project = str(machine_conf("install.conf").get("PROJECT_DIR") or "").strip()
        if not project or not Path(project).is_dir():
            not_judged(title, f"в паспорте машины нет каталога PROJECT_DIR («{project}»)")
            return
        got = door_conf_paths(lib, Path(project) / "scripts" / "drop-database.sh", {**named, STAND_HOME_KEY: str(foreign)})
        report(title, got == machine, f"дверь взяла {got}")
    finally:
        shutil.rmtree(foreign, ignore_errors=True)


# ── П-12 АР-4: ключи двери — из паспорта, окружение их не перекрывает ────────
# Пути конфигов дверь берёт от паспорта машины (АР-3), но _konf_chitat возвращал ОКРУЖЕНИЕ
# поверх значений файла, а ключ, которого в файле нет, дверь брала из окружения умолчанием
# `X="${X:-…}"`. После законного «Да» кнопкой drop-database.sh исполнил бы подложный
# DB_ADMIN_EXEC, положил дамп в подложный BACKUP_DIR, след сноса — в подложный LOG_DIR.
# Судится поведение: копия двери сноса с законным «Да» в журнале стенда и загрузчик режима
# двери по ключам каждой bash-двери из разбора — при паспорте стенда и при паспорте машины
# (поправка «только для стенда» оставила бы машину открытой). Обратный случай: обычный
# konf_zagruzit по-прежнему ставит окружение старше файла — на этом стоят пробы (улика 12.09).

FORGED = "подлог-"
SELF_DEFAULT_KEY = "NRP_SELF_DEFAULT"   # ключ паспорта стенда вида KEY="${KEY:-…}"
DOOR_LOADER_CALL = re.compile(r"^\s*(?:konf_zagruzit_dveri|konf_dveri_puti)\b")
DOOR_LOADER_ARGS = re.compile(r"^\s*konf_zagruzit_dveri\b(.*)$")
DOOR_DEFAULT = re.compile(r"\$\{([A-Z][A-Z0-9_]*):[-=]")
LOADER_DEFAULT = re.compile(r'^\s*:\s*"\$\{([A-Z][A-Z0-9_]*):=', re.M)
CONF_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=", re.M)
# Пути паспорта ставит konf_dveri_puti сам, а дверь стенда называет паспорт: их не подделываем.
PATH_KEYS = (*CONF_FORMS, "INSTALL_CONF", STAND_HOME_KEY)
DROP_KEYS = ("LOG_DIR", "HARNESS_LOG_DIR", "BACKUP_DIR", "DB_ADMIN_EXEC", "DB_ADMIN_USER", "DROPPABLE_DB_PREFIX")
# Загрузчик зовётся из файла рядом с копией двери: BASH_SOURCE[1] лежит вне PROJECT_DIR машины,
# как у самой копии. Печатаются ключи после загрузки.
KEYS_PROBE = 'source "$1"; shift\n"$NRP_LOADER" "$@"\nfor k in $NRP_KEYS; do printf "%s=%s\\n" "$k" "${!k-}"; done\n'
# Ворота копии двери сноса — настоящий deploy_guard.py, но в раннере пробы: носитель — журнал
# стенда с законным «Да», просьба кнопки — заглушка (канал не зовётся).
GUARD_WRAPPER = '''import json, os, sys
spec = json.loads(%r)
spec["доводы"] = sys.argv[1:]
os.execv(sys.executable, [sys.executable, %r, "--раннер", json.dumps(spec, ensure_ascii=False)])
'''
# Подложный исполнитель СУБД: оставляет метку в чужом каталоге и отвечает, как СУБД.
FOREIGN_DB_EXEC = '''#!/usr/bin/env bash
printf '%%s\\n' "МЕТКА-DB_ADMIN_EXEC-подменён: $*" >> %(mark)s
case "$1" in pg_dump) echo '-- дамп подлога' ;; psql) [[ "$*" == *-tAc* ]] || cat >/dev/null ;; esac
'''


class DropStand(BashGateStand):
    """Копия scripts/ с законным «Да» на базу стенда в журнале паспорта стенда и чужим каталогом.

    Паспорт стенда называет DB_ADMIN_EXEC — заглушку СУБД стенда; BACKUP_DIR и LOG_DIR — свои.
    """

    def __init__(self) -> None:
        super().__init__()
        with open(self.etc / "harness.conf", "a", encoding="utf-8") as conf:
            conf.write(f'DB_ADMIN_EXEC="{self.db_exec}"\n')
        door = self.real / "deploy_guard-door.py"
        os.replace(self.real / "deploy_guard.py", door)
        spec = {"судья": str(JUDGE_PATH), "как": "запуск", "файл": str(door),
                "носитель_стенда": {"папка": str(self.log), "ручки": {}},
                "заглушка_просьбы": str(self.real / "ask-consent.py")}
        (self.real / "deploy_guard.py").write_text(
            GUARD_WRAPPER % (json.dumps(spec, ensure_ascii=False), str(Path(__file__).resolve())), encoding="utf-8")
        self.foreign = self.root / "foreign"
        self.mark = self.foreign / "МЕТКА-DB_ADMIN_EXEC.log"
        self.foreign_exec = self.foreign / "db.sh"
        (self.foreign / "log").mkdir(parents=True)
        self.foreign_exec.write_text(FOREIGN_DB_EXEC % {"mark": shlex.quote(str(self.mark))}, encoding="utf-8")
        self.foreign_exec.chmod(0o755)

    def drop(self, forged: dict) -> tuple[int, str]:
        """Свежее законное «Да» (одно да — одно действие) и предметы на месте; затем снос копией двери."""
        fresh_log(self)
        press(self.log, "снос-базы", STAND_BASH_DB, 60)
        (self.db / STAND_BASH_DB).write_text("база стенда\n", encoding="utf-8")
        self.db_calls.unlink(missing_ok=True)
        self.mark.unlink(missing_ok=True)
        for place in (self.root / "backup", self.foreign / "backup", self.foreign / "log"):
            shutil.rmtree(place, ignore_errors=True)
            place.mkdir(parents=True)
        env = {k: v for k, v in self.env().items() if k not in DROP_KEYS}
        done = subprocess.run(["bash", str(self.real / "drop-database.sh"), STAND_BASH_DB], capture_output=True,
                              text=True, env={**env, **forged}, timeout=180, cwd=str(self.root))
        return done.returncode, done.stdout + done.stderr

    def trouble(self, code: int) -> list[str]:
        found = []
        if code != 0 or (self.db / STAND_BASH_DB).exists():
            found.append(f"база стенда не снесена (код {code})")
        if "pg_dump" not in (self.db_calls.read_text(encoding="utf-8") if self.db_calls.exists() else ""):
            found.append("заглушка СУБД паспорта не звана")
        if self.mark.exists():
            found.append(f"исполнен ПОДЛОЖНЫЙ DB_ADMIN_EXEC: {self.mark.read_text(encoding='utf-8').splitlines()[0][:120]}")
        if not list((self.root / "backup").glob(f"{STAND_BASH_DB}-*.sql")):
            found.append("дампа нет в BACKUP_DIR паспорта")
        if any((self.foreign / "backup").iterdir()):
            found.append("дамп — в ПОДЛОЖНОМ BACKUP_DIR")
        if not (self.log / "db-drops.jsonl").exists():
            found.append("следа сноса нет в LOG_DIR паспорта")
        if (self.foreign / "log" / "db-drops.jsonl").exists():
            found.append("след сноса — в ПОДЛОЖНОМ LOG_DIR")
        return found


def code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if not line.lstrip().startswith("#")]


def door_mode_shells() -> list[Path]:
    """Bash-двери режима двери — РАЗБОРОМ: файлы, что зовут konf_zagruzit_dveri или konf_dveri_puti."""
    loader = (LIB / "config.sh").resolve()
    return [path for path in shell_files() if not path.name.startswith("test_") and path.resolve() != loader
            and any(DOOR_LOADER_CALL.search(line) for line in code_lines(door_file(path.name, path)))]


def template_keys() -> set[str]:
    """Ключи шаблона harness.conf (проект: harness/config, пакет: config рядом со scripts)."""
    template = next((d / "harness.conf" for d in (SCRIPTS.parent / "harness" / "config", SCRIPTS.parent / "config")
                     if (d / "harness.conf").is_file()), None)
    if template is None:
        raise RuntimeError("шаблон harness.conf не найден — разбор ключей двери слеп")
    return set(CONF_KEY.findall(template.read_text(encoding="utf-8")))


def door_env_keys(door: Path, conf_keys: set[str], loader_keys: set[str]) -> tuple[list[str], list[str]]:
    """(ключи, что дверь берёт после загрузки конфига; доводы её konf_zagruzit_dveri)."""
    lines = code_lines(door)
    args = [word for line in lines if (found := DOOR_LOADER_ARGS.match(line))
            for word in found[1].split() if re.fullmatch(r"[A-Z][A-Z0-9_]*", word)]
    defaults = {key for line in lines for key in DOOR_DEFAULT.findall(line)}
    # Своя переменная двери — присвоена в ней не своим же умолчанием (HOLDER=$(…)) и не объявлена в
    # шаблоне harness.conf: самотест deploy.sh присваивает STACK_DB_DUMP_CMD, но это ключ конфига.
    own = {key for key in defaults - template_keys() if any(
        re.match(rf'\s*(?:export\s+|local\s+|readonly\s+)?{key}=(?!"?\$\{{{key}:)', line) for line in lines)}
    return sorted(((defaults - own) | conf_keys | loader_keys | set(args)) - set(PATH_KEYS)), args


def loaded_keys(probe: Path, lib: Path, loader: str, args: list[str], keys: list[str], extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (*keys, *PATH_KEYS, *STAND_ENV_KEYS)}
    env.update({"NRP_LOADER": loader, "NRP_KEYS": " ".join(keys), **extra})
    done = subprocess.run(["bash", str(probe), str(lib), *args], capture_output=True, text=True, env=env,
                          timeout=60, cwd="/")
    got = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    if done.returncode or set(keys) - set(got):
        raise RuntimeError(f"{loader} не отработал (код {done.returncode}): {tail(done.stderr, 160)}")
    return got


def case_door_keys_not_from_env() -> None:
    doors = door_mode_shells()
    report("П-12 АР-4 разбор: bash-двери режима двери есть, среди них drop-database.sh",
           any(path.name == "drop-database.sh" for path in doors), f"двери режима двери: {[p.name for p in doors]}")
    stand = None
    try:
        stand = DropStand()
        judge_drop_keys(stand)
        judge_loader_keys(stand, doors)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as trouble:
        report("П-12 АР-4: стенд ключей двери", False, f"стенд не собран: {type(trouble).__name__}: {trouble}")
    finally:
        if stand is not None:
            stand.close()


def judge_drop_keys(stand: DropStand) -> None:
    """Копия drop-database.sh с законным «Да»: подлог ключа в окружении не меняет, чем и куда она сносит."""
    code, out = stand.drop({})
    found = stand.trouble(code)
    report("здоровый: копия drop-database.sh, законное «Да» в журнале стенда, ключи только в паспорте → "
           "база снесена заглушкой паспорта, дамп и след — у паспорта", not found, f"{'; '.join(found)}: {tail(out, 200)}")
    forged = {"DB_ADMIN_EXEC": stand.foreign_exec, "BACKUP_DIR": stand.foreign / "backup",
              "LOG_DIR": stand.foreign / "log"}
    for key, value in forged.items():
        code, out = stand.drop({key: str(value)})
        found = stand.trouble(code)
        report(f"П-12 АР-4 drop-database.sh: законное «Да», {key} подлогом в окружении → снос заглушкой паспорта, "
               "дамп и след у паспорта, подлог не тронут", not found, f"{'; '.join(found)}: {tail(out, 160)}")


def judge_loader_keys(stand: DropStand, doors: list[Path]) -> None:
    """Каждая дверь из разбора: ключ подлогом в окружении после konf_zagruzit_dveri равен значению без подлога."""
    lib = stand.real / "lib" / "config.sh"
    conf_keys = {key for name in ("install.conf", "harness.conf")
                 for key in CONF_KEY.findall((stand.etc / name).read_text(encoding="utf-8"))}
    loader_keys = set(LOADER_DEFAULT.findall(lib.read_text(encoding="utf-8")))
    # Паспорт без ключей двери — установка, чей паспорт ключа двери не называет: ключ, выпавший из
    # доводов загрузчика, при паспорте стенда и машины перекрыл бы файл, а здесь его берёт окружение.
    # Паспорт — код bash: ключ с умолчанием от самого себя берёт окружение, если загрузчик не снял
    # ключ файла до source. Сроки согласия (CONSENT_*) паспорт не называет — их снимает имя, не файл.
    bare_home = stand.root / "паспорт-без-ключей"
    bare_home.mkdir(exist_ok=True)
    (bare_home / "install.conf").write_text("", encoding="utf-8")
    (bare_home / "harness.conf").write_text(f'{SELF_DEFAULT_KEY}="${{{SELF_DEFAULT_KEY}:-из-паспорта}}"\n',
                                            encoding="utf-8")
    bare_extra = [SELF_DEFAULT_KEY, *sorted(key for key in conf_keys if key.startswith("CONSENT_"))]
    every = set(conf_keys)
    for path in doors:
        keys, args = door_env_keys(door_file(path.name, path), conf_keys, loader_keys)
        own_keys, _ = door_env_keys(door_file(path.name, path), set(), loader_keys)
        every |= set(keys)
        print(f"  ключи двери {path.name} ({len(keys)}): {' '.join(keys)}", flush=True)
        probe = copy_of(stand, path).parent / "nrp-door-keys.sh"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(KEYS_PROBE, encoding="utf-8")
        # (где, окружение, судимые ключи, LOG_DIR — улика, что этот паспорт прочитан)
        for where, extra, judged, witness in (
                ("паспорт стенда", {STAND_HOME_KEY: str(stand.etc)}, keys, str(stand.log)),
                ("паспорт машины", {}, keys, None),
                ("паспорт стенда без ключей двери", {STAND_HOME_KEY: str(bare_home)}, [*own_keys, *bare_extra],
                 str(bare_home / "log"))):
            forged = {key: FORGED + key for key in judged}
            title = (f"П-12 АР-4 {path.name}: {len(judged)} ключей подлогом в окружении → после konf_zagruzit_dveri "
                     f"({where}) значение файла или умолчание, не окружение")
            try:
                clean = loaded_keys(probe, lib, "konf_zagruzit_dveri", args, judged, extra)
                got = loaded_keys(probe, lib, "konf_zagruzit_dveri", args, judged, {**extra, **forged})
            except RuntimeError as trouble:
                report(title, False, str(trouble))
                continue
            # Значения паспорта машины не печатаются (И-3): только имена ключей.
            taken = [key for key in judged if got[key] != clean[key]]
            blind = witness is not None and clean.get("LOG_DIR") != witness
            report(title, not taken and not blind,
                   ("паспорт стенда не прочитан; " if blind else "") + f"окружение взяло {len(taken)}: {' '.join(taken)}")
    # Обратный: вне двери окружение старше файла. TG_CHAT_ID без подлога — улика, что файл прочитан.
    keys = sorted(every - {"TG_CHAT_ID"})
    probe = stand.real / "nrp-door-keys.sh"
    probe.write_text(KEYS_PROBE, encoding="utf-8")
    title = (f"обратный: обычный konf_zagruzit (не дверь), {len(keys)} ключей подлогом в окружении → "
             "окружение старше файла (улика 12.09)")
    try:
        got = loaded_keys(probe, lib, "konf_zagruzit", [], [*keys, "TG_CHAT_ID"],
                          {"HARNESS_HOME": str(stand.etc), **{key: FORGED + key for key in keys}})
    except RuntimeError as trouble:
        report(title, False, str(trouble))
        return
    lost = [key for key in keys if got[key] != FORGED + key]
    report(title, not lost and got["TG_CHAT_ID"] == OWNER,
           f"файл перекрыл окружение у {lost}; TG_CHAT_ID={got['TG_CHAT_ID']}")


# ── судья в процессе пробы: срок, здоровый, П-11, П-6 ────────────────────────

class EnvPatch:
    def __init__(self, **values: str) -> None:
        self.values, self.saved = values, {}

    def __enter__(self):
        for key in (*STAND_ENV_KEYS, *self.values):
            self.saved.setdefault(key, os.environ.get(key))
            os.environ.pop(key, None)
        os.environ.update(self.values)

    def __exit__(self, *_):
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


def button_stand(ago_s: float) -> tuple[Path, Path, str]:
    root = new_stand()
    log = root / "log"
    log.mkdir()
    (log / "inbox.jsonl").write_text("", encoding="utf-8")
    target = root / "h.txt"
    target.write_text("предмет\n", encoding="utf-8")
    press(log, "удаление", os.path.realpath(target), ago_s)
    return root, log, os.path.realpath(target)


def case_healthy(ow) -> None:
    root, log, target = button_stand(60)
    try:
        with EnvPatch():
            opened, why = ow.судить_кнопкой("удаление", target, носитель=stand_carrier(ow, log, None))
        report("здоровый: свежее «Да» через Носитель стенда → True", opened is True, why)
    except (LookupError, TypeError, AttributeError) as trouble:
        report("здоровый: свежее «Да» через Носитель стенда → True", False, f"{type(trouble).__name__}: {trouble}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_term_from_env(ow) -> None:
    name = "П-12 срок: CONSENT_BUTTON_TTL_MIN=999999999 в окружении не продлевает «Да» старше боевого срока"
    term = machine_term_min()
    root, log, target = button_stand((term + 60) * 60)
    try:
        with EnvPatch(CONSENT_BUTTON_TTL_MIN="999999999"):
            opened, why = ow.судить_кнопкой("удаление", target, носитель=stand_carrier(ow, log, None))
        # Причина обязана быть сроком: отказ по другой причине мутацию срока не поймал бы.
        report(name, opened is False and "истек" in why, f"открыто={opened}: {why}")
    except (LookupError, TypeError, AttributeError) as trouble:
        report(name, False, f"{type(trouble).__name__}: {trouble}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_p11_no_argument(ow) -> None:
    import config
    root = new_stand()
    passport_log, decoy = root / "log", root / "decoy"
    write_conf(root / "etc" / "install.conf", {"LOG_DIR": passport_log, "TG_CHAT_ID": OWNER})
    write_conf(decoy / "install.conf", {"LOG_DIR": decoy, "TG_CHAT_ID": "1"})
    real_root = config.корень_машины
    # Чтение паспорта машины подменено в процессе пробы; окружение показывает на приманку.
    config.корень_машины = lambda: str(root / "etc")
    try:
        носитель = getattr(ow, "носитель", None)
        if носитель is None:
            report("П-11 носитель() без аргумента — граница /", False, "в судье нет функции носитель()")
            return
        required = [p for p in inspect.signature(носитель).parameters.values()
                    if p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
        report("П-11 носитель() зовётся без аргументов", not required, f"обязательные: {[p.name for p in required]}")
        with EnvPatch(**{KEY: "1", "HARNESS_LOG_DIR": str(decoy), "LOG_DIR": str(decoy),
                         "HARNESS_INSTALL_CONF": str(decoy / "install.conf")}):
            carrier = носитель()
        report("П-11 носитель() без аргумента — граница /", Path(carrier.граница) == Path("/"),
               f"граница {carrier.граница}")
        report("П-11 носитель() — папка из паспорта машины, а не из окружения",
               Path(carrier.папка).resolve() == passport_log.resolve(), f"папка {carrier.папка}")
    except Exception as trouble:     # любая беда судьи здесь — провал случая, а не падение пробы
        report("П-11 носитель() без аргумента", False, f"{type(trouble).__name__}: {trouble}")
    finally:
        config.корень_машины = real_root
        shutil.rmtree(root, ignore_errors=True)


def params_trouble(function) -> str:
    names = set(inspect.signature(function).parameters)
    bad = sorted(names & FORBIDDEN_PARAMS)
    if bad:
        return f"есть {bad}"
    return "" if "носитель" in names else "нет параметра носитель"


def case_p6_signatures(ow) -> None:
    for name in ("стенд", "КЛЮЧ_СТЕНДА"):
        report(f"П-6 у судьи нет {name}", not hasattr(ow, name), "атрибут есть — ключ стенда жив")
    cls = getattr(ow, "Носитель", None)
    fields = {f.name for f in dataclasses.fields(cls)} if cls and dataclasses.is_dataclass(cls) else set()
    report("П-6 Носитель — dataclass с папка, uid, паспорт, граница, окно_мин, срок_кнопки_мин",
           CARRIER_FIELDS <= fields, f"нет {sorted(CARRIER_FIELDS - fields)}")
    for name in P6_JUDGES:
        function = getattr(ow, name, None)
        trouble = "функции нет" if function is None else params_trouble(function)
        report(f"П-6 {name}: без каталог_логов/окно_мин, с носитель", not trouble, trouble)
    history_path = next(p for p in (JUDGE_PATH.parent / "layout_history.py", LIB / "layout_history.py")
                        if p.exists())
    try:
        history = load("layout_history", history_path)
        trouble = params_trouble(history.consent_after)
    except Exception as error:     # падение импорта — тоже ответ П-6
        trouble = f"{type(error).__name__}: {error}"
    report("П-6 layout_history.consent_after: без каталога, с носитель", not trouble, trouble)


# ── П-12 userns ──────────────────────────────────────────────────────────────

def inside_userns(judge: str) -> int:
    """Внутри чужого пространства имён: что скажет носитель() машины."""
    sys.path.insert(0, str(Path(judge).parent))
    uid_map = " ".join(Path("/proc/self/uid_map").read_text().split())
    try:
        ow = load("owner_word", Path(judge))
        носитель = getattr(ow, "носитель", None)
        if носитель is None:
            answer = {"исход": "нет носителя()"}
        else:
            try:
                answer = {"исход": "открыт", "папка": str(носитель().папка)}
            except ow.НетНосителя as refusal:
                answer = {"исход": "НетНосителя", "причина": str(refusal)}
    except Exception as trouble:
        answer = {"исход": "ошибка", "причина": f"{type(trouble).__name__}: {trouble}"}
    print(MARK + json.dumps({**answer, "uid_map": uid_map}, ensure_ascii=False), flush=True)
    return 0


def userns_run(command: list[str]) -> tuple[dict | None, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.TimeoutExpired) as trouble:
        return None, f"{type(trouble).__name__}: {trouble}"
    for line in done.stdout.splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):]), ""
    return None, f"код {done.returncode}: {tail(done.stdout + done.stderr, 200)}"


def case_userns() -> None:
    if not shutil.which("unshare"):
        not_judged("П-12 userns", "нет unshare")
        return
    me = [sys.executable, str(Path(__file__).resolve()), "--внутри", str(JUDGE_PATH)]
    root = new_stand()
    write_conf(root / "etc" / "install.conf", {"LOG_DIR": root / "log", "TG_CHAT_ID": OWNER})
    forms = (
        # Полная форма отступления 1: свой паспорт привязкой поверх /etc/harness.
        ("unshare -rm + mount --bind паспорта стенда на /etc/harness",
         ["unshare", "-rm", "sh", "-c", 'mount --bind "$1" /etc/harness && shift && exec "$@"', "sh",
          str(root / "etc"), *me]),
        # Без карты uid (ядро запрещает писать uid_map): uid_map пуст, паспорт машины читается.
        ("unshare -U без карты uid", ["unshare", "-U", *me]))
    judged = 0
    try:
        for label, command in forms:
            name = f"П-12 userns ({label}): носитель() → НетНосителя «пространств»"
            answer, why = userns_run(command)
            if answer is None:
                not_judged(name, f"ядро не дало: {why}")
                continue
            judged += 1
            ok = answer["исход"] == "НетНосителя" and "пространств" in answer.get("причина", "")
            report(name, ok, f"uid_map «{answer['uid_map']}», исход {answer['исход']}: {answer.get('причина', answer.get('папка', ''))}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if not judged:
        results.append(("НЕ СУДИТСЯ", "П-12 userns целиком", "ни одна форма не запустилась"))


# ── боевые журналы согласия: проба в них не пишет ───────────────────────────

def combat_journals() -> dict[Path, int]:
    log = Path(str(machine_conf("install.conf").get("LOG_DIR") or "/nonexistent"))
    return {p: (p.stat().st_size if p.exists() else 0)
            for p in (log / "consent-requests.jsonl", log / "consent-buttons.jsonl")}


def case_combat_untouched(before: dict[Path, int]) -> None:
    stray = []
    for path, size in before.items():
        if not path.exists():
            continue
        with open(path, "rb") as stream:
            stream.seek(size)
            grown = stream.read().decode("utf-8", errors="replace")
        stray += [line for line in grown.splitlines() if "nrp-stand-" in line or STAND_DB in line]
    report("боевые журналы согласия: ни строки с предметом стенда за прогон", not stray,
           f"{len(stray)} строк, первая: {stray[0][:160] if stray else ''}")


def main() -> int:
    print(f"судья под пробой: {JUDGE_PATH}; layout-door: {LAYOUT_DOOR_PATH}", flush=True)
    sys.path.insert(0, str(LIB))
    before = combat_journals()
    try:
        ow = load_judge()
    except Exception as trouble:
        report("судья грузится", False, f"{type(trouble).__name__}: {trouble}")
        ow = None
    case_p12_doors()
    case_freshness_anchor()
    case_publish_exec_paths()
    if ow is not None:
        case_term_from_env(ow)
        case_healthy(ow)
        case_p11_no_argument(ow)
        case_p6_signatures(ow)
    case_userns()
    case_combat_untouched(before)
    bad = sum(1 for verdict, *_ in results if verdict == "ПЛОХО")
    skipped = [name for verdict, name, _ in results if verdict == "НЕ СУДИТСЯ"]
    cases = sum(1 for verdict, *_ in results if verdict != "НЕ СУДИТСЯ")
    print(f"ИТОГ: случаев {cases}, неудач {bad}, не судится {len(skipped)}", flush=True)
    if bad:
        return 1
    return 77 if "П-12 userns целиком" in skipped else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--раннер"]:
        sys.exit(runner(json.loads(sys.argv[2])))
    if sys.argv[1:2] == ["--внутри"]:
        sys.exit(inside_userns(sys.argv[2]))
    sys.exit(main())
