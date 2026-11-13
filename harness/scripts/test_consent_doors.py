#!/usr/bin/env python3
"""test_consent_doors.py — пробы задачи hr.consent-buttons-doors: кнопки «Да / Нет» на выкат,
публикацию и одобрение спеки; publish.sh судит замок 2 по коду возврата ворот.

Владелец 05.11.2026: «все вопросы по поводу моего Да должны приходить кнопками в телегу»,
«нужно не как правило, а кодом». Договор — .tasks/hr.consent-buttons-doors/spec.md §1-§4, §6,
ревью-спеки.md Н-1..Н-15 и решения журнала (предмет публикации — 7 полей, погашение выката —
журнал выкатов deploy-releases.jsonl).

Стенд свой на каждый случай: временные репо (file://), свой LOG_DIR, свои harness.conf
(DEPLOY_NEEDS_OWNER=1, PUBLISH_NEEDS_OWNER=1, DEPLOY_CONSENT_STANDING=0, PUBLIC_* на стенд) и
install.conf (AUTONOMY=semi). Судье они приходят НОСИТЕЛЕМ: раннеры пробы подменяют
owner_word.носитель в процессе двери и ask-consent, а owner_word.ЗАПРОС_КНОПКИ — обёрткой
ask-consent с тем же носителем (окружение судья не слушает). Механизмы берутся РЯДОМ с
пробой (её каталог scripts/): мутация ставится в копию дерева, и из копии зовётся сама проба.
Нажатия — записями журнала кнопок стенда или полным путём ask-consent → unsent → sent →
judge с подставным callback. Ворота зовутся процессом; «две смены» и занятость машины сняты
подменой: это состояние машины, а не предмет пробы.
Канал владельца, боевой LOG_DIR, выкат и публикация не трогаются: ask-consent пишет запрос в
журнал стенда, spec_approval --отправить не зовётся вовсе.

    python3 scripts/test_consent_doors.py [--случай <имя>]...
Код возврата: 0 — «падений 0», 1 — красная.
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
LIB = SCRIPTS / "lib"
sys.path.insert(0, str(LIB))
from selftest_cases import Cases  # noqa: E402
import consent_buttons as BUTTONS_LIB  # noqa: E402

ASK = SCRIPTS / "ask-consent.py"
BUTTONS = LIB / "consent_buttons.py"
DOORS = LIB / "door_subjects.py"
PUBLISH = SCRIPTS / "publish.sh"
BUILD_PUBLIC = SCRIPTS / "build-public.py"
SPEC_APPROVAL = SCRIPTS / "spec_approval.py"
# Проект: scripts/hooks; пакет: harness/scripts рядом с harness/hooks.
GUARD_HOOK = next((p for p in (SCRIPTS / "hooks" / "guard_bash.py", SCRIPTS.parent / "hooks" / "guard_bash.py")
                   if p.is_file()), SCRIPTS / "hooks" / "guard_bash.py")

OWNER = 777000
# Просьбы, которые стенд кладёт к своим нажатиям: счёт запросов судит только то, что положила дверь.
STAND_ASK = "proba-stand"
TTL_MIN = 15
REAL_GIT = shutil.which("git") or "/usr/bin/git"
# Глобальный и системный конфиг git машины пробе чужие: url.*.insteadOf в
# ~/.gitconfig агента сделал бы любой предмет «перенаправлением».
GIT_ENV = {"GIT_AUTHOR_NAME": "proba", "GIT_AUTHOR_EMAIL": "proba@stand.invalid",
           "GIT_COMMITTER_NAME": "proba", "GIT_COMMITTER_EMAIL": "proba@stand.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
YES, NO, SENT = "consent.yes", "consent.no", "consent.sent"
TASK = "proba.spec-doors"

cases = Cases()
check = cases.check


def now_iso(ago_s: float = 0) -> str:
    when = dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)
    return when.isoformat(timespec="seconds")


def git(*args: str, cwd: Path) -> str:
    """Настоящий git: подготовка условия, а не предмет пробы."""
    done = subprocess.run([REAL_GIT, *args], cwd=cwd, capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV), timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def commit(repo: Path, message: str, name: str | None = None, text: str | None = None) -> str:
    if name:
        (repo / name).write_text(text if text is not None else message + "\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", message, cwd=repo)
    else:
        git("commit", "-q", "--allow-empty", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def envelope(event: str, ts: str, data: dict) -> str:
    return json.dumps({"v": 1, "ts": ts, "src": "scripts/lib/consent_buttons.py", "event": event,
                       "outcome": "ok", "data": data}, ensure_ascii=False)


def short(out: str) -> str:
    return out.replace("\n", " | ")[:300]


# Носитель стенда в процессе двери: дверь зовёт судью без носителя, судья берёт owner_word.носитель.
# Конфиги стенда читаются при запуске двери — случаи дописывают их между зовами. Просьба кнопки
# двери — ОТДЕЛЬНЫЙ процесс ask-consent (owner_word.ЗАПРОС_КНОПКИ): носитель туда не доходит,
# поэтому ЗАПРОС_КНОПКИ подменён обёрткой стенда, которая ставит тот же носитель в своём процессе.
SET_CARRIER = """
def set_carrier(word, spec):
    import config, json, pathlib
    spec = json.loads(spec)
    knobs = config.из_файла(spec["ручки"])
    carrier = word.Носитель(папка=pathlib.Path(spec["папка"]), uid=None, паспорт=config.из_файла(spec["паспорт"]),
                            граница=pathlib.Path(spec["граница"]),
                            окно_мин=int(knobs.get("CONFIRM_MAX_AGE_MIN") or 60),
                            срок_кнопки_мин=int(knobs.get("CONSENT_BUTTON_TTL_MIN") or 15), ручки=knobs)
    word.носитель = lambda: carrier
    word.ЗАПРОС_КНОПКИ = pathlib.Path(spec["обёртка"])
"""
# Ворота процессом; «две смены» и занятость — состояние машины, на стенде их нет.
RUNNER = SET_CARRIER + """import sys
sys.path.insert(0, sys.argv[1])
import deploy_guard as g
set_carrier(g.слово_вл, sys.argv[2])
g.две_смены = lambda: None
g.лок_файлы = lambda: []
g.долгие_процессы = lambda: []
sys.argv = ["deploy_guard.py", *sys.argv[3:]]
sys.exit(g.main())
"""
# Дверь целиком (spec_approval --состояние): runpy после подмены носителя.
DOOR_RUNNER = SET_CARRIER + """import runpy, sys
sys.path.insert(0, sys.argv[1])
import owner_word
set_carrier(owner_word, sys.argv[2])
sys.argv = sys.argv[3:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
# Обёртка ask-consent стенда: зовётся вместо него (argv — его доводы), носитель — файл рядом.
ASK_WRAPPER = SET_CARRIER + """import json, pathlib, runpy, sys
spec = (pathlib.Path(__file__).parent / "ask-carrier.json").read_text(encoding="utf-8")
lib = json.loads(spec)["lib"]
sys.path.insert(0, lib)
import owner_word
set_carrier(owner_word, spec)
ask = str(pathlib.Path(lib).parent / "ask-consent.py")
sys.argv = [ask, *sys.argv[1:]]
runpy.run_path(ask, run_name="__main__")
"""


class Stand:
    """Свой LOG_DIR, свои конфиги; продукт для выката; пакет, публичный репо и сборка."""

    def __init__(self, root: Path, publish: bool = False) -> None:
        self.root = root
        self.log = root / "log"
        self.secrets = root / "secrets"
        for d in (self.log, self.secrets, root / "project"):
            d.mkdir(parents=True, exist_ok=True)
        # Журнал входящих на машине есть всегда: без него отзыв прочитать нечем — это отказ.
        (self.log / "inbox.jsonl").write_text("", encoding="utf-8")
        self.prod = root / "prod"
        self.prod.mkdir()
        git("init", "-q", "-b", "master", cwd=self.prod)
        commit(self.prod, "база", "app.py", "print('база')\n")
        commit(self.prod, "коммит 1", "app.py", "print('один')\n")
        self.public = root / "public.git"
        self.pkg = root / "pkg"
        self.build = root / "build"
        self.url = f"file://{self.public}"
        if publish:
            self._publish_bench()
        (root / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nAUTONOMY="semi"\nPROJECT_DIR="{root}/project"\n'
            f'SECRETS_DIR="{self.secrets}"\nAGENT_USER="proba"\nTG_CHAT_ID="{OWNER}"\n', encoding="utf-8")
        (root / "harness.conf").write_text(
            f"DEPLOY_NEEDS_OWNER=1\nPUBLISH_NEEDS_OWNER=1\nPUSH_NEEDS_OWNER=1\nDEPLOY_CONSENT_STANDING=0\n"
            f"CONFIRM_MAX_AGE_MIN=60\nCONSENT_BUTTON_TTL_MIN={TTL_MIN}\nSERVICE_LOCKS_SUBDIR=services\n"
            f'PRODUCT_REPO="{self.prod}"\nPUBLIC_BUILD_DIR="{self.build}"\nPUBLIC_REMOTE="origin"\n'
            f'PUBLIC_BRANCH="master"\nSTARTER_DIR="{self.pkg}"\n', encoding="utf-8")
        # Окружение — для своих журналов, секретов и ключей ДВЕРЕЙ; судье стенд даёт носитель.
        self.env = dict(os.environ, **GIT_ENV, HARNESS_HOME=str(root),
                        HARNESS_INSTALL_CONF=str(root / "install.conf"), HARNESS_DOOR_STAND_HOME=str(root),
                        HARNESS_CONF=str(root / "harness.conf"), LOG_DIR=str(self.log),
                        HARNESS_LOG_DIR=str(self.log), SECRETS_DIR=str(self.secrets))
        self.carrier = json.dumps({"папка": str(self.log), "граница": str(root),
                                   "паспорт": str(root / "install.conf"), "ручки": str(root / "harness.conf"),
                                   "lib": str(LIB), "обёртка": str(root / "ask-carrier.py")},
                                  ensure_ascii=False)
        (root / "ask-carrier.json").write_text(self.carrier, encoding="utf-8")
        (root / "ask-carrier.py").write_text(ASK_WRAPPER, encoding="utf-8")
        for key in ("PRODUCT_REPO", "PUBLIC_BUILD_DIR", "PUBLIC_REMOTE", "PUBLIC_BRANCH", "STARTER_DIR",
                    "HARNESS_STARTER", "PROJECT_DIR"):
            self.env.pop(key, None)

    def _publish_bench(self) -> None:
        """Пакет с удалённым origin → публичный голый репо; сборка — его клон с одной версией."""
        git("init", "-q", "--bare", "-b", "master", str(self.public), cwd=self.root)
        self.pkg.mkdir()
        git("init", "-q", "-b", "master", cwd=self.pkg)
        first = commit(self.pkg, "пакет 1", "README.md", "пакет\n")
        git("remote", "add", "origin", self.url, cwd=self.pkg)
        self.build.mkdir()
        git("init", "-q", "-b", "master", cwd=self.build)
        commit(self.build, f"версия от 2026-11-01, пакет {first[:7]}", "README.md", "сборка\n")
        git("remote", "add", "origin", self.url, cwd=self.build)
        git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=self.build)
        commit(self.pkg, "пакет 2: новая дверь", "door.md", "дверь\n")

    # ── предметы, посчитанные пробой независимо от механизма ───────────────
    def prod_head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.prod)

    def deploy_subject(self) -> str:
        return f"{self.prod_head()} {os.path.realpath(self.prod)}"

    def publish_subject(self) -> str:
        pkg_head = git("rev-parse", "HEAD", cwd=self.pkg)
        tip = git("ls-remote", self.url, "refs/heads/master", cwd=self.pkg).split("\t")[0]
        build_tip = git("rev-parse", "refs/heads/master", cwd=self.build)
        return " ".join([pkg_head, self.url, "refs/heads/master", tip, build_tip,
                         os.path.realpath(self.pkg), os.path.realpath(self.build)])

    # ── слова владельца ────────────────────────────────────────────────────
    def button(self, event: str, kind: str, target: str, ago_s: float) -> None:
        """Решение владельца; просьба ask-consent к нему — на минуту раньше (живой порядок)."""
        data = {"nonce": secrets.token_urlsafe(16), "вид": kind, "цель": target,
                "message_id": 5000 + int(ago_s), "from_id": OWNER}
        asked = {"nonce": data["nonce"], "вид": kind, "цель": target, "почему": "проба"}
        with open(self.log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": now_iso(ago_s + 60), "src": STAND_ASK, "event": "consent.request",
                                 "outcome": "ok", "data": asked}, ensure_ascii=False) + "\n")
        with open(self.log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
            fh.write(envelope(event, now_iso(ago_s), data) + "\n")

    def yes(self, kind: str, target: str, ago_s: float = 60) -> None:
        self.button(YES, kind, target, ago_s)

    def no(self, kind: str, target: str, ago_s: float = 60) -> None:
        self.button(NO, kind, target, ago_s)

    def text(self, words: str, ago_s: float) -> None:
        """Текстовое слово владельца — так его кладёт диспетчер: inbox.jsonl и confirmations.jsonl.

        Отзыв читается из inbox.jsonl; confirmations.jsonl — прежний носитель «да», он больше
        ничего не открывает, и проба это видит."""
        line = json.dumps({"ts": now_iso(ago_s), "text": words, "message_id": str(9000 + int(ago_s))},
                          ensure_ascii=False) + "\n"
        for name in ("inbox.jsonl", "confirmations.jsonl"):
            with open(self.log / name, "a", encoding="utf-8") as fh:
                fh.write(line)

    def released(self, sha: str, ago_s: float, tree: Path | None = None) -> None:
        """Строка журнала выкатов — конверт events.emit, как его пишет deploy.sh перед сборкой."""
        line = {"v": 1, "ts": now_iso(ago_s), "src": "scripts/deploy.sh", "event": "deploy.release",
                "outcome": "ok",
                "data": {"цель": f"{sha} {os.path.realpath(tree or self.prod)}",
                         "тег": "deploy-20261105-000000"}}
        with open(self.log / "deploy-releases.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ── вызовы механизмов ──────────────────────────────────────────────────
    def run(self, argv: list[str], stdin: str | None = None, env: dict | None = None,
            timeout: int = 90) -> tuple[int | None, str]:
        try:
            done = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                                  env=env or self.env, timeout=timeout, cwd=self.root)
        except subprocess.TimeoutExpired:
            return None, f"висит дольше {timeout} с"
        return done.returncode, (done.stdout + done.stderr).strip()

    def gate(self, action: str, *extra: str, only_consent: bool = True) -> tuple[int | None, str]:
        flags = ["--только-согласие"] if only_consent else []
        return self.run(["python3", "-c", RUNNER, str(SCRIPTS), self.carrier, "--действие", action, *flags, *extra])

    def gate_deploy(self, *extra: str, only_consent: bool = True) -> tuple[int | None, str]:
        return self.gate("deploy", *extra, only_consent=only_consent)

    def gate_publish(self, *extra: str) -> tuple[int | None, str]:
        return self.gate("публикация", "--публикация", str(self.pkg), *extra)

    def ask(self, *args: str) -> tuple[int | None, str, str | None]:
        rc, out = self.run(["python3", "-c", DOOR_RUNNER, str(LIB), self.carrier, str(ASK), *args])
        lines = [line for line in out.splitlines() if line.strip()]
        nonce = next((line.strip() for line in lines if len(line.strip()) == 22
                      and all(c.isalnum() or c in "-_" for c in line.strip())), None)
        return rc, out, nonce

    def requests(self) -> list[dict]:
        try:
            rows = [json.loads(line) for line in
                    (self.log / "consent-requests.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []
        return [r.get("data") or {} for r in rows if r.get("src") != STAND_ASK]

    def unsent(self, buttons: Path = BUTTONS) -> list[dict]:
        rc, out = self.run(["python3", str(buttons), "unsent", str(self.log), str(self.secrets)])
        rows = []
        for line in out.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def deliver(self, nonce: str, answer: str = "y") -> str:
        """Полный путь нажатия: «отправлено» по строке unsent, затем judge с callback владельца."""
        row = next((r for r in self.unsent() if r.get("nonce") == nonce), None)
        if row is None:
            return "unsent не отдал запрос"
        message_id = 4000 + secrets.randbelow(1000)
        rc, out = self.run(["python3", str(BUTTONS), "sent", str(self.log), nonce, row["target"]],
                           stdin=json.dumps({"ok": True, "result": {"message_id": message_id}}))
        if rc != 0:
            return f"sent отказал: {out}"
        return self.press(nonce, message_id, answer)

    def press(self, nonce: str, message_id: int, answer: str = "y") -> str:
        callback = {"from": {"id": OWNER}, "message": {"chat": {"id": OWNER}, "message_id": message_id},
                    "data": f"c:{nonce}:{answer}"}
        _rc, out = self.run(["python3", str(BUTTONS), "judge", str(self.log), str(OWNER)],
                            stdin=json.dumps(callback))
        try:
            return json.loads(out.splitlines()[-1]).get("answer", out)
        except (ValueError, IndexError):
            return out


def dirty_ways(st: Stand) -> list[tuple[str, callable]]:
    """Три настройки git, прячущие правку от голого `status --porcelain` (Н-2)."""
    def assume(s: Stand) -> None:
        (s.prod / "app.py").write_text("print('спрятано')\n", encoding="utf-8")
        git("update-index", "--assume-unchanged", "app.py", cwd=s.prod)

    def skip(s: Stand) -> None:
        git("update-index", "--skip-worktree", "app.py", cwd=s.prod)
        (s.prod / "app.py").write_text("print('спрятано')\n", encoding="utf-8")

    def untracked(s: Stand) -> None:
        git("config", "status.showUntrackedFiles", "no", cwd=s.prod)
        (s.prod / "extra.py").write_text("print('лишнее')\n", encoding="utf-8")

    return [("assume-unchanged", assume), ("skip-worktree", skip), ("неотслеженный при showUntrackedFiles=no", untracked)]


class Yard:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="proba-consent-doors-"))
        atexit.register(shutil.rmtree, self.root, True)
        self.counter = 0

    def stand(self, publish: bool = False) -> Stand:
        self.counter += 1
        return Stand(self.root / f"s{self.counter}", publish)


# ── 1. запросы ───────────────────────────────────────────────────────────────
def probe_requests(yard: Yard) -> None:
    print("— 1. запрос кнопки: предмет считает код")
    s = yard.stand()
    rc, out, nonce = s.ask("--вид", "выкат", "--почему", "проба выката")
    got = s.requests()
    check(f"1а) --вид выкат → rc 0, nonce [{short(out)}]", (0, True), (rc, bool(nonce)))
    check("1а) строка запроса: вид выкат, цель = «<sha> <realpath дерева>»", ("выкат", s.deploy_subject()),
          (got[-1].get("вид"), got[-1].get("цель")) if got else None)
    rc, out, _ = s.ask("--вид", "выкат", "--предмет", "x", "--почему", "проба")
    check(f"1б) --вид выкат --предмет x → rc 2 [{short(out)}]", 2, rc)
    s = yard.stand()
    (s.prod / "app.py").write_text("print('правка')\n", encoding="utf-8")
    rc, out, _ = s.ask("--вид", "выкат", "--почему", "проба")
    check(f"1в) правленое дерево → rc 2 «не закоммичено» [{short(out)}]", (2, True), (rc, "не закоммичено" in out))
    for label, spoil in dirty_ways(s):
        s = yard.stand()
        spoil(s)
        rc, out, _ = s.ask("--вид", "выкат", "--почему", "проба")
        check(f"1г) {label} → rc 2 «не закоммичено» [{short(out)}]", (2, True), (rc, "не закоммичено" in out))

    s = yard.stand(publish=True)
    rc, out, _ = s.ask("--вид", "публикация", "--репо", str(s.pkg), "--почему", "проба публикации")
    got = s.requests()
    check(f"1д) --вид публикация → rc 0 [{short(out)}]", 0, rc)
    check("1д) цель — 7 полей: пакет, адрес, ветка, вершина, вершина сборки, пакет, сборка",
          s.publish_subject(), got[-1].get("цель") if got else None)
    rc, out, _ = s.ask("--вид", "публикация", "--предмет", "x", "--почему", "проба")
    check(f"1д) --вид публикация --предмет x → rc 2 [{short(out)}]", 2, rc)
    spoilers = (("pushurl в сборке", lambda b: git("config", "remote.origin.pushurl", b.url, cwd=b.build), "перенаправлен"),
                ("url.*.insteadOf в сборке",
                 lambda b: git("config", "url.file:///proba-nowhere/.insteadOf", "proba-zzz:", cwd=b.build), "перенаправлен"),
                ("второй url в сборке",
                 lambda b: git("config", "--add", "remote.origin.url", "file:///proba-nowhere.git", cwd=b.build), "адресов"),
                ("pushurl в пакете", lambda b: git("config", "remote.origin.pushurl", b.url, cwd=b.pkg), "перенаправлен"))
    for label, spoil, reason in spoilers:
        s = yard.stand(publish=True)
        spoil(s)
        rc, out, _ = s.ask("--вид", "публикация", "--репо", str(s.pkg), "--почему", "проба")
        check(f"1е) {label} → rc 2 «{reason}» [{short(out)}]", (2, True), (rc, reason in out))


# ── 2. тексты владельцу ──────────────────────────────────────────────────────
def probe_texts(yard: Yard) -> None:
    print("— 2. текст сообщения с кнопками (unsent)")
    s = yard.stand()
    git("tag", "deploy-20261101-000000", "HEAD~1", cwd=s.prod)
    commit(s.prod, "коммит 2: правка выката")
    rc, _out, nonce = s.ask("--вид", "выкат", "--почему", "проба выката")
    row = next((r for r in s.unsent() if r.get("nonce") == nonce), {})
    text = row.get("text", "")
    head = s.prod_head()
    check(f"2а) выкат: имя дерева, short, заголовок, «С выката deploy-20261101-000000 — 2 коммита» [{short(text)}]",
          (True, True, True, True),
          ("prod" in text, head[:7] in text, "коммит 2: правка выката" in text,
           "С выката deploy-20261101-000000 — 2 коммит" in text))
    check(f"2а) выкат: ≤ 700 знаков и «15 мин» (длина {len(text)})", (True, True),
          (0 < len(text) <= 700, "15 мин" in text))

    s = yard.stand(publish=True)
    rc, _out, nonce = s.ask("--вид", "публикация", "--репо", str(s.pkg), "--почему", "проба публикации")
    row = next((r for r in s.unsent() if r.get("nonce") == nonce), {})
    text = row.get("text", "")
    pkg_head = git("rev-parse", "HEAD", cwd=s.pkg)
    tip = git("rev-parse", "HEAD", cwd=s.build)
    check(f"2б) публикация: short пакета, адрес, ветка, вершина, «С прошлой публикации 1 коммит» [{short(text)}]",
          (True, True, True, True, True),
          (pkg_head[:7] in text, str(s.public) in text, "ветка master" in text, f"сейчас там {tip[:7]}" in text,
           "С прошлой публикации 1 коммит" in text))
    import owner_word
    # Текст строит диспетчер без носителя: срок публикации — из конфига МАШИНЫ (ключ), не цифра в пробе.
    срок_текста = f"Кнопка действует {BUTTONS_LIB.ttl_phrase(owner_word.срок_кнопки_мин('публикация'))}"
    check(f"2б) публикация: ≤ 700 знаков, срок кнопки из конфига ({срок_текста}), без «неотправленн» (длина {len(text)})",
          (True, True, False),
          (0 < len(text) <= 700, срок_текста in text, "неотправленн" in text))
    ключ = owner_word.КЛЮЧ_СРОКА_ВИДА["публикация"]
    носитель = lambda ручки: owner_word.Носитель(папка=s.log, uid=None, паспорт={}, граница=s.log, окно_мин=60,
                                                  срок_кнопки_мин=15, ручки=ручки)
    check("2б) срок кнопки публикации берётся из ключа конфига, нет ключа — запасной 60; удаление ключом не задето",
          (1440, 60, 15),
          (owner_word.срок_кнопки_мин("публикация", носитель({ключ: "1440"})),
           owner_word.срок_кнопки_мин("публикация", носитель({})),
           owner_word.срок_кнопки_мин("удаление", носитель({ключ: "1440"}))))

    s = yard.stand(publish=True)
    commit(s.build, "чужой коммит в сборке")
    rc, _out, nonce = s.ask("--вид", "публикация", "--репо", str(s.pkg), "--почему", "проба публикации")
    row = next((r for r in s.unsent() if r.get("nonce") == nonce), {})
    text = row.get("text", "")
    check(f"2в) чужой коммит в сборке (Н-6): текст называет «В сборке 1 неотправленн» [{short(text)}]",
          True, "В сборке 1 неотправленн" in text)


# ── 3. ворота выката ─────────────────────────────────────────────────────────
def probe_deploy(yard: Yard) -> None:
    print("— 3. ворота --действие deploy судят кнопку")
    s = yard.stand()
    rc, out = s.gate_deploy()
    check(f"3а) нажатия нет → rc 1 «нет согласия» [{short(out)}]", (1, True), (rc, "нет согласия" in out))

    s = yard.stand()
    s.yes("выкат", s.deploy_subject())
    rc, out = s.gate_deploy()
    check(f"3б) «Да» на предмет выката → rc 0 [{short(out)}]", 0, rc)
    check("3б) в выводе «согласие кнопкой» и «предмет: <sha> <дерево>»", (True, True),
          ("согласие кнопкой" in out, f"предмет: {s.deploy_subject()}" in out))
    check("3б) кнопка не пишет согласие.json", False, (s.log / "согласие.json").exists())

    s = yard.stand()
    s.yes("выкат", s.deploy_subject())
    commit(s.prod, "коммит после «Да»")
    rc, out = s.gate_deploy()
    check(f"3в) новый коммит после «Да» → rc 1 «нет согласия» [{short(out)}]", (1, True), (rc, "нет согласия" in out))

    s = yard.stand()
    s.yes("выкат", s.deploy_subject(), ago_s=120)
    s.released(s.prod_head(), ago_s=60)
    rc, out = s.gate_deploy()
    check(f"3г) выкат этого sha начат после «Да» (журнал выкатов) → rc 1, погашено [{short(out)}]",
          (1, False), (rc, "согласие кнопкой" in out))
    s = yard.stand()
    s.yes("выкат", s.deploy_subject(), ago_s=60)
    s.released(s.prod_head(), ago_s=120)
    s.released("0" * 40, ago_s=30)
    rc, out = s.gate_deploy()
    check(f"3д) строка журнала старше «Да» и строка чужого sha → rc 0 [{short(out)}]", (0, True),
          (rc, "согласие кнопкой" in out))
    s = yard.stand()
    s.yes("выкат", s.deploy_subject(), ago_s=120)
    s.released(s.prod_head(), ago_s=60, tree=s.root / "project")
    rc, out = s.gate_deploy()
    check(f"3д2) строка того же sha, но другого дерева → rc 0 [{short(out)}]", 0, rc)

    s = yard.stand()
    s.yes("выкат", s.deploy_subject(), ago_s=120)
    s.text("стоп, не выкатывай", ago_s=60)
    rc, out = s.gate_deploy()
    check(f"3е) «Да», затем текстом «стоп, не выкатывай» → rc 1, «отозвано словами» [{short(out)}]",
          (1, True), (rc, "отозвано словами" in out))

    # Текст владельца выкат не открывает никогда (слово 05.11.2026); стенд 06.11 — дословно.
    for words in ("да, выкатывай", "выкатывай, но сначала прогони гейты"):
        s = yard.stand()
        s.text(words, ago_s=60)
        rc, out = s.gate_deploy()
        check(f"3ж0) БОЛЬНОЙ СЛУЧАЙ: текстовое «{words}» без кнопок → rc 1, отказ называет кнопку "
              f"[{short(out)}]", (1, True), (rc, "кнопк" in out))
    s = yard.stand()
    s.text("да, выкатывай", ago_s=120)
    s.no("выкат", s.deploy_subject(), ago_s=60)
    rc, out = s.gate_deploy()
    check(f"3ж) текстовое «да», затем «Нет» кнопкой → rc 1 «нажал „Нет“» [{short(out)}]",
          (1, True), (rc, "нажал „Нет“" in out))
    s = yard.stand()
    s.no("выкат", s.deploy_subject(), ago_s=120)
    s.text("да, выкатывай", ago_s=60)
    rc, out = s.gate_deploy()
    check(f"3ж2) «Нет» кнопкой, затем текстовое «да» → rc 1: текст «Нет» не перебивает [{short(out)}]",
          (1, True), (rc, "нажал „Нет“" in out))

    s = yard.stand()
    s.yes("пуш", s.deploy_subject())
    rc, out = s.gate_deploy()
    check(f"3з) «Да» вида «пуш» с той же строкой → rc 1 «нет согласия» [{short(out)}]", (1, True),
          (rc, "нет согласия" in out))

    s = yard.stand()
    s.yes("выкат", s.deploy_subject(), ago_s=(TTL_MIN + 5) * 60)
    rc, out = s.gate_deploy()
    check(f"3и) «Да» старше {TTL_MIN} мин → rc 1 [{short(out)}]", (1, True), (rc, "нет согласия" in out))
    s = yard.stand()
    s.yes("выкат", s.deploy_subject(), ago_s=(TTL_MIN - 5) * 60)
    rc, out = s.gate_deploy()
    check(f"3и2) «Да» {TTL_MIN - 5} мин назад → rc 0 [{short(out)}]", 0, rc)

    s = yard.stand()
    s.yes("выкат", s.deploy_subject())
    rc, out = s.gate_deploy(only_consent=False)
    check(f"3к) путь сторожа (без --только-согласие), «Да» → rc 0 [{short(out)}]", 0, rc)

    s = yard.stand()
    s.text("да, выкатывай", ago_s=180)
    s.no("выкат", s.deploy_subject(), ago_s=120)
    commit(s.prod, "обычный новый коммит после «Нет»")
    rc, out = s.gate_deploy()
    check(f"3л) Н-1: текст «да» → «Нет» на X → новый коммит Y → rc 1, кнопки на Y нет [{short(out)}]",
          (1, True), (rc, "кнопки «Да» на этот предмет нет" in out))

    for label, spoil in dirty_ways(s):
        s = yard.stand()
        s.yes("выкат", s.deploy_subject())
        spoil(s)
        rc, out = s.gate_deploy()
        check(f"3м) Н-2: «Да», затем {label} → rc 1 «кнопки нет … не закоммичено» [{short(out)}]",
              (1, True), (rc, "не закоммичено" in out))

    s = yard.stand()
    s.yes("выкат", s.deploy_subject())
    rc, out = s.gate_deploy("--выкат", str(s.prod), s.prod_head())
    check(f"3н0) --выкат <то же дерево> <тот же sha> → rc 0 [{short(out)}]", 0, rc)
    other = s.root / "other"
    other.mkdir()
    git("init", "-q", "-b", "master", cwd=other)
    other_sha = commit(other, "чужое дерево")
    rc, out = s.gate_deploy("--выкат", str(other), other_sha)
    check(f"3н) Н-3: --выкат <чужое дерево> <его sha> при «Да» на дерево конфига → rc 1 «не тот» [{short(out)}]",
          (1, True), (rc, "не тот" in out))
    commit(s.prod, "коммит между sha скрипта и воротами")
    rc, out = s.gate_deploy("--выкат", str(s.prod), git("rev-parse", "HEAD~1", cwd=s.prod))
    check(f"3н2) Н-3: --выкат с прежним sha, HEAD ушёл вперёд → rc 1 [{short(out)}]", 1, rc)

    s = yard.stand()
    rc, out, nonce = s.ask("--вид", "выкат", "--почему", "проба выката")
    answer = s.deliver(nonce) if nonce else f"запроса нет: {short(out)}"
    rc, out = s.gate_deploy()
    check(f"3о) полный путь ask → unsent → sent → judge «Да» → ворота rc 0 (ответ «{answer}») [{short(out)}]",
          ("Принято", 0), (answer, rc))
    s = yard.stand()
    rc, out, nonce = s.ask("--вид", "выкат", "--почему", "проба выката")
    answer = s.deliver(nonce, "n") if nonce else f"запроса нет: {short(out)}"
    rc, out = s.gate_deploy()
    check(f"3п) полный путь, «Нет» → ворота rc 1 [{short(out)}]", ("Отказ записан", 1), (answer, rc))


# ── 4. ворота публикации ─────────────────────────────────────────────────────
def probe_publish(yard: Yard) -> None:
    print("— 4. ворота --действие публикация --публикация <пакет> судят кнопку")
    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject())
    rc, out = s.gate_publish()
    check(f"4а) «Да» на предмет публикации → rc 0 «согласие кнопкой» [{short(out)}]", (0, True),
          (rc, "согласие кнопкой" in out))
    check("4а) ворота печатают «предмет: <7 полей>» для сверки перед пушем", True,
          f"предмет: {s.publish_subject()}" in out)
    rc, out = s.gate("перепись")
    check(f"4а2) то же «Да» на публикацию НЕ открывает перепись → rc 1 [{short(out)}]", 1, rc)

    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject())
    commit(s.pkg, "пакет 3 после «Да»")
    rc, out = s.gate_publish()
    check(f"4б) новый коммит пакета → rc 1 «нет согласия» [{short(out)}]", (1, True), (rc, "нет согласия" in out))

    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject())
    other = s.root / "other"
    git("clone", "-q", s.url, str(other), cwd=s.root)
    commit(other, "чужая версия на публичном")
    git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=other)
    rc, out = s.gate_publish()
    check(f"4в) вершина публичной ветки сдвинута → rc 1 «нет согласия» [{short(out)}]", (1, True),
          (rc, "нет согласия" in out))

    s = yard.stand(publish=True)
    s.yes("выкат", s.publish_subject())
    rc, out = s.gate_publish()
    check(f"4г) «Да» вида «выкат» с той же строкой → rc 1 [{short(out)}]", (1, True), (rc, "нет согласия" in out))

    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject())
    rc, out = s.gate("публикация")
    check(f"4д) без ключа --публикация «Да» → rc 1 «кнопки нет» [{short(out)}]", (1, True), (rc, "кнопки нет" in out))

    # Текст владельца публикацию не открывает никогда; стенд 06.11 — дословно.
    for words in ("да, публикуй", "публикуй к вечеру"):
        s = yard.stand(publish=True)
        s.text(words, ago_s=60)
        rc, out = s.gate_publish()
        check(f"4е0) БОЛЬНОЙ СЛУЧАЙ: текстовое «{words}» без кнопок → rc 1, отказ называет кнопку "
              f"[{short(out)}]", (1, True), (rc, "кнопк" in out))
    s = yard.stand(publish=True)
    s.text("да, публикуй", ago_s=120)
    s.no("публикация", s.publish_subject(), ago_s=60)
    rc, out = s.gate_publish()
    check(f"4е) текстовое «да», затем «Нет» кнопкой → rc 1 «нажал „Нет“» [{short(out)}]", (1, True),
          (rc, "нажал „Нет“" in out))

    s = yard.stand(publish=True)
    s.text("да, публикуй", ago_s=180)
    s.no("публикация", s.publish_subject(), ago_s=120)
    commit(s.pkg, "пакет 3 после «Нет»")
    rc, out = s.gate_publish()
    check(f"4ж) Н-1: текст «да» → «Нет» на P → коммит пакета Q → rc 1, кнопки на Q нет [{short(out)}]",
          (1, True), (rc, "кнопки «Да» на этот предмет нет" in out))

    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject(), ago_s=40 * 60)
    rc, out = s.gate_publish()
    check(f"4л) Н-7: «Да» 40 мин назад → rc 0 [{short(out)}]", 0, rc)
    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject(), ago_s=61 * 60)
    rc, out = s.gate_publish()
    check(f"4л2) Н-7: «Да» 61 мин назад → rc 1 [{short(out)}]", (1, True), (rc, "нет согласия" in out))

    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject(), ago_s=120)
    s.text("стоп, не публикуй", ago_s=60)
    rc, out = s.gate_publish()
    check(f"4н) «Да», затем текстом «стоп, не публикуй» → rc 1 «отозвано словами» [{short(out)}]",
          (1, True), (rc, "отозвано словами" in out))

    s = yard.stand(publish=True)
    s.yes("публикация", s.publish_subject())
    git("config", "remote.origin.pushurl", s.url, cwd=s.build)
    rc, out = s.gate_publish()
    check(f"4о) pushurl в сборке после «Да» → rc 1 «перенаправлен» [{short(out)}]", (1, True),
          (rc, "перенаправлен" in out))

    s = yard.stand(publish=True)
    rc, out, nonce = s.ask("--вид", "публикация", "--репо", str(s.pkg), "--почему", "проба публикации")
    answer = s.deliver(nonce) if nonce else f"запроса нет: {short(out)}"
    rc, out = s.gate_publish()
    check(f"4п) полный путь ask → judge «Да» → ворота публикации rc 0 (ответ «{answer}») [{short(out)}]",
          ("Принято", 0), (answer, rc))


# ── 5. сверка перед пушем (Н-6) ──────────────────────────────────────────────
def probe_verify(yard: Yard) -> None:
    print("— 5. door_subjects.py сверить-публикацию: перед пушем предмет тот же, чужих коммитов нет")

    def verify(s: Stand, locked: str) -> tuple[int | None, str]:
        return s.run(["python3", str(DOORS), "сверить-публикацию", locked, str(s.pkg), str(s.build),
                      "origin", "master"])

    s = yard.stand(publish=True)
    locked = s.publish_subject()
    commit(s.build, "версия от 2026-11-05, пакет свой", "VERSION", "2\n")
    rc, out = verify(s, locked)
    check(f"5а) только свой коммит версии поверх одобренной вершины → rc 0 [{short(out)}]", 0, rc)
    s = yard.stand(publish=True)
    locked = s.publish_subject()
    rc, out = verify(s, locked)
    check(f"5а2) повтор упавшей отправки: HEAD сборки = одобренная вершина → rc 0 [{short(out)}]", 0, rc)

    s = yard.stand(publish=True)
    locked = s.publish_subject()
    commit(s.build, "чужой коммит в сборке после «Да»")
    commit(s.build, "версия от 2026-11-05, пакет свой", "VERSION", "2\n")
    rc, out = verify(s, locked)
    check(f"5б) чужой коммит в сборке после «Да» → rc 1 «не одобрял» [{short(out)}]", (1, True),
          (rc, "не одобрял" in out))

    s = yard.stand(publish=True)
    locked = s.publish_subject()
    commit(s.pkg, "пакет 3 после замка")
    rc, out = verify(s, locked)
    check(f"5в) пакет сдвинут после замка → rc 1 «изменилось: sha» [{short(out)}]", (1, True),
          (rc, "изменилось: sha" in out))

    s = yard.stand(publish=True)
    locked = s.publish_subject()
    git("config", "remote.origin.pushurl", s.url, cwd=s.build)
    rc, out = verify(s, locked)
    check(f"5г) pushurl в сборке после замка 2 → rc 1 «перенаправлен» [{short(out)}]", (1, True),
          (rc, "перенаправлен" in out))


# ── 6. publish.sh: замок 2 по коду возврата ──────────────────────────────────
STUB_GUARD = """import os, sys
with open(os.environ["PROBA_GUARD_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
mode = os.environ.get("PROBA_GUARD_MODE", "")
if mode == "rc0":
    print("предмет: проба-предмет")
    sys.exit(0)
if mode == "rc1":
    print("кнопки нет: проба другого текста отказа")
    sys.exit(1)
if mode == "rc2":
    print("незнакомый ключ --публикация; известны: --силой")
    sys.exit(2)
raise RuntimeError("ворота упали")
"""


def door_project(project: Path) -> Path:
    """Проект стенда для publish.sh: дверь — КОПИЯ файлом, свои заглушки стенда, прочее ссылками.

    Скрипты дверь зовёт рядом с собой (readlink -f, АР-2), конфиг берёт своей дверью (АР-3):
    настоящий publish.sh позвал бы настоящие ворота и tg_send.sh и взял бы паспорт машины.
    Копия вне PROJECT_DIR машины верит HARNESS_DOOR_STAND_HOME (паспорт стенда)."""
    scripts = project / "scripts"
    for entry in SCRIPTS.iterdir():
        target = scripts / entry.name
        if target.exists() or entry.name.startswith("."):
            continue
        if entry.name == "publish.sh":
            shutil.copy2(entry, target)
        elif entry.name == "tg_send.sh":
            target.write_text(f'#!/bin/sh\necho "$*" >> "{project.parent}/tg-sent"\n', encoding="utf-8")
            target.chmod(0o755)
        else:
            target.symlink_to(entry)
    return scripts / "publish.sh"


def probe_lock(yard: Yard) -> None:
    print("— 6. publish.sh: замок 2 судит код возврата ворот (hr.publish-judges-gate-by-text)")
    s = yard.stand(publish=True)
    project = s.root / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    (project / "scripts" / "gates.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (project / "scripts" / "deploy_guard.py").write_text(STUB_GUARD, encoding="utf-8")
    door = door_project(project)
    with open(s.root / "harness.conf", "a", encoding="utf-8") as fh:
        fh.write('PUBLIC_COMMIT_EMAIL="proba@stand.invalid"\nPUBLIC_COMMIT_NAME="proba"\n')
    argv_log = s.root / "guard-argv.txt"
    for mode, label in (("rc1", "rc 1 с другим текстом отказа «кнопки нет: …»"),
                        ("rc2", "rc 2 «незнакомый ключ»"), ("boom", "упавший python (исключение)"),
                        ("rc0", "rc 0")):
        env = dict(s.env, PROJECT_DIR=str(project), STARTER_DIR=str(s.pkg),
                   PROBA_GUARD_MODE=mode, PROBA_GUARD_ARGV=str(argv_log))
        rc, out = s.run(["bash", str(door), "--проверка"], env=env, timeout=120)
        passed = "замок 2 · согласие владельца на публикацию есть" in out
        if mode == "rc0":
            check(f"6г) охранный: ворота {label} → замок 2 пройден [{short(out)}]", True, passed)
        else:
            check(f"6) ворота {label} → publish.sh rc 1, замок 2 не пройден [{short(out)}]", (1, False), (rc, passed))
    calls = argv_log.read_text(encoding="utf-8").splitlines() if argv_log.exists() else []
    # Спека §4: кнопку просит только боевой прогон; --проверка ворота зовёт без --просить.
    check(f"6д) ворота получили --публикация <пакет> и НЕ получили --просить при --проверка "
          f"[{calls[-1:] or calls}]",
          True, bool(calls) and all(f"--публикация {s.pkg}" in c and "--просить" not in c for c in calls))


# ── 7. publish.sh: перед пушем пакет и предмет те же ────────────────────────
def publish_stand(s: Stand) -> dict:
    """Двор как у самотеста publish.sh: пакет с harness/, заметками и данными сборки."""
    pkg = s.pkg
    for rel, text in (("harness/CLAUDE.md", "# харнес\n@STATE.md\n"), ("harness/STATE.md", "состояние\n"),
                      ("harness/память/TONE.md", "# ТОН\nзамер речи\n"),
                      ("harness/шаблоны-задач/ТОН-заготовка.md", "# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n"),
                      ("harness/config/public-build.yaml",
                       "исключить: []\nзаменить_шаблоном:\n  harness/память/TONE.md: harness/шаблоны-задач/ТОН-заготовка.md\n"
                       "обезличить_ключи: [PROJECT_DIR]\nоставить: [harness/CLAUDE.md]\n"),
                      ("ЧТО-НОВОГО.md", "# Что нового\n\n## 2026-11-05 — Кнопки на двери\n\n### Исправлено\n\n"
                       "- Сессия больше не обрывается.\n\n### Что изменилось для вас\n\n- Ничего делать не нужно.\n")):
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git("add", "-A", cwd=pkg)
    git("commit", "-q", "-m", "пакет: двор публикации", cwd=pkg)
    passport = s.root / "паспорт.conf"
    passport.write_text(f'PROJECT_DIR="{SCRIPTS.parent}"\nAGENT_USER="подставной"\nLOG_DIR="{s.log}"\n'
                        f'PROJECT_NAME="Подставной"\nTG_CHAT_ID="100000001"\nPRODUCT_DIR="tovar"\n', encoding="utf-8")
    # Сборка — клон публичного с меткой каталога сборки: коммит версии ляжет поверх вершины.
    (s.build / ".публичная-сборка").write_text("", encoding="utf-8")
    return {"passport": passport}


# Обёртка check-public-clean.py в зеркале проекта: после ВТОРОГО её зова (замок 6б) пакет
# сдвигается — это окно между последним замком и пушем, его видит только сверка перед пушем.
LATE_CLEAN = """import os, runpy, subprocess, sys
counter = os.environ["PROBA_LATE_COUNT"]
with open(counter, "a", encoding="utf-8") as fh:
    fh.write("1\\n")
real = os.environ["PROBA_REAL_CLEAN"]
sys.argv[0] = real
try:
    runpy.run_path(real, run_name="__main__")
finally:
    pkg = os.environ.get("PROBA_LATE_PKG")
    if pkg and len(open(counter, encoding="utf-8").read().split()) == 2:
        subprocess.run(["git", "-C", pkg, "commit", "-q", "--allow-empty", "-m", "пакет: коммит во время публикации"],
                       check=True)
"""
LATE_NAME = "check-public-clean.py"


def mirror_project(s: Stand) -> Path:
    """Проект-зеркало: всё ссылками на дерево пробы, кроме обёртки судьи чистоты."""
    mirror = s.root / "mirror"
    (mirror / "scripts").mkdir(parents=True)
    for entry in SCRIPTS.parent.iterdir():
        if entry.name != SCRIPTS.name:
            (mirror / entry.name).symlink_to(entry)
    for entry in SCRIPTS.iterdir():
        if entry.name != LATE_NAME:
            (mirror / "scripts" / entry.name).symlink_to(entry)
    (mirror / "scripts" / LATE_NAME).write_text(LATE_CLEAN, encoding="utf-8")
    return mirror


PUBLISH_FN = """set -uo pipefail
say() { printf '%s\\n' "$*"; }
eval "$(sed -n '/^опубликовать_сборку() {/,/^}/p' "$1")"
опубликовать_сборку
"""


def probe_before_push(yard: Yard) -> None:
    print("— 7. publish.sh опубликовать_сборку: пакет сдвинут после замка — не отправлено")

    def run(s: Stand, extra: dict) -> tuple[int | None, str, int]:
        mirror = mirror_project(s)
        # Функция зовёт скрипты от места двери ($SCRIPTS_REAL, АР-2): место — зеркало с обёрткой.
        env = dict(s.env, PROJECT_DIR=str(mirror), SCRIPTS_REAL=str(mirror / "scripts"),
                   PROBA_REAL_CLEAN=str(SCRIPTS / LATE_NAME),
                   PROBA_LATE_COUNT=str(s.root / f"clean-calls-{time.time_ns()}"),
                   PACKAGE_DIR=str(s.pkg), PUBLIC_BUILD_DIR=str(s.build),
                   PUBLIC_REMOTE="origin", PUBLIC_BRANCH="master", PUBLIC_COMMIT_NAME="proba",
                   PUBLIC_COMMIT_EMAIL="proba@stand.invalid", PUBLIC_PASPORT=str(s.root / "паспорт.conf"), **extra)
        before = int(git("rev-list", "--count", "master", cwd=s.public))
        rc, out = s.run(["bash", "-c", PUBLISH_FN, "publish-fn", str(PUBLISH)], env=env, timeout=300)
        return rc, out, int(git("rev-list", "--count", "master", cwd=s.public)) - before

    s = yard.stand(publish=True)
    publish_stand(s)
    head = git("rev-parse", "HEAD", cwd=s.pkg)
    rc, out, pushed = run(s, {"PAKET_SHA": head})
    check(f"7а) охранный: PAKET_SHA = HEAD пакета → отправлено [{short(out[-300:])}]", (0, 1), (rc, pushed))

    s = yard.stand(publish=True)
    publish_stand(s)
    approved = git("rev-parse", "HEAD", cwd=s.pkg)
    rc, out, pushed = run(s, {"PAKET_SHA": approved, "PROBA_LATE_PKG": str(s.pkg)})
    check(f"7б) пакет сдвинут после замков 4-6, перед пушем → «пакет сдвинулся», не отправлено [{short(out[-300:])}]",
          (1, True, 0), (rc, "пакет сдвинулся" in out, pushed))

    s = yard.stand(publish=True)
    publish_stand(s)
    head = git("rev-parse", "HEAD", cwd=s.pkg)
    locked = s.publish_subject()
    rc, out, pushed = run(s, {"PAKET_SHA": head, "PREDMET_ZAMKA": locked})
    check(f"7в0) охранный: предмет замка 2 = пересчёт перед пушем → отправлено [{short(out[-300:])}]",
          (0, 1), (rc, pushed))
    s = yard.stand(publish=True)
    publish_stand(s)
    head = git("rev-parse", "HEAD", cwd=s.pkg)
    locked = s.publish_subject()
    commit(s.build, "чужой коммит в сборке после замка 2")
    rc, out, pushed = run(s, {"PAKET_SHA": head, "PREDMET_ZAMKA": locked})
    check(f"7в) Н-6: чужой коммит в сборке после замка 2 → «предмет публикации изменился», "
          f"не отправлено [{short(out[-300:])}]", (1, True, 0), (rc, "предмет публикации изменился" in out, pushed))


# ── 8. build-public: git replace не подменяет содержимое (Н-5) ──────────────
def probe_build(yard: Yard) -> None:
    print("— 8. build-public.py --коммит: архив без подмены объектов")
    s = yard.stand(publish=True)
    publish_stand(s)
    good = commit(s.pkg, "пакет: честный файл", "harness/dver.md", "good\n")
    git("checkout", "-q", "-b", "evil", cwd=s.pkg)
    evil = commit(s.pkg, "пакет: подмена", "harness/dver.md", "EVIL\n")
    git("checkout", "-q", "master", cwd=s.pkg)
    git("replace", good, evil, cwd=s.pkg)
    seen = git("show", "HEAD:harness/dver.md", cwd=s.pkg)
    check("8а) контроль стенда: git replace действует — HEAD показывает EVIL", "EVIL", seen)
    for label, extra in (("--коммит <sha>", ["--коммит", good]), ("без --коммит (HEAD)", [])):
        out_dir = s.root / f"build-{len(extra)}"
        out_dir.mkdir()
        (out_dir / ".публичная-сборка").write_text("", encoding="utf-8")
        env = dict(s.env)
        env.pop("GIT_NO_REPLACE_OBJECTS", None)
        rc, out = s.run(["python3", str(BUILD_PUBLIC), "--пакет", str(s.pkg), "--куда", str(out_dir),
                         "--паспорт", str(s.root / "паспорт.conf"), *extra], env=env, timeout=180)
        try:
            built = (out_dir / "harness" / "dver.md").read_text(encoding="utf-8").strip()
        except OSError:
            built = f"файла нет; rc {rc}: {short(out)}"
        check(f"8б) {label}: в сборке настоящее содержимое «good», не подмена", "good", built)


# ── 9. одобрение спеки кнопкой ───────────────────────────────────────────────
STUB_ASK = """import os, sys
with open(os.environ["PROBA_ASK_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
print("[согласие кнопкой] запрос записан (заглушка стенда)")
"""


class SpecStand:
    """Копия spec_approval.py, ask-consent.py и lib/ рядом со своей картой и спекой:
    карточку и файл спеки механизм ищет от своего корня."""

    def __init__(self, yard: Yard, stub_ask: bool = False) -> None:
        self.s = yard.stand()
        self.tree = self.s.root / "tree"
        scripts = self.tree / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SPEC_APPROVAL, scripts / SPEC_APPROVAL.name)
        if stub_ask:
            (scripts / ASK.name).write_text(STUB_ASK, encoding="utf-8")
        else:
            shutil.copy2(ASK, scripts / ASK.name)
        shutil.copytree(LIB, scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.spec = self.tree / ".tasks" / TASK / "spec.md"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text("# Спека стенда\n\nРедакция 1.\n", encoding="utf-8")
        (self.tree / "dev-map.yaml").write_text(
            f"lines:\n- id: {TASK}\n  title: Проба двери спеки\n  status: plan\n"
            f"  spec: .tasks/{TASK}/spec.md\n", encoding="utf-8")
        self.journal = self.s.log / "спеки-владельцу.jsonl"
        self.argv = self.s.root / "ask-argv.txt"
        self.env = dict(self.s.env, PROBA_ASK_ARGV=str(self.argv))

    def subject(self) -> str:
        return f"{TASK} {hashlib.sha256(self.spec.read_bytes()).hexdigest()}"

    def sent(self, ago_s: float) -> None:
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(ago_s), "событие": "отправлена", "задача": TASK,
                                 "файл": f".tasks/{TASK}/spec.md"}, ensure_ascii=False) + "\n")

    def approved_by_text(self, ago_s: float) -> None:
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(), "событие": "одобрена", "задача": TASK, "слово": "да",
                                 "сказано": now_iso(ago_s)}, ensure_ascii=False) + "\n")

    def inbox(self, words: str, ago_s: float) -> None:
        box = self.s.log / "inbox"
        box.mkdir(exist_ok=True)
        path = box / f"{time.time_ns()}.txt"
        path.write_text(words, encoding="utf-8")
        when = time.time() - ago_s
        os.utime(path, (when, when))

    def ask(self, *args: str) -> tuple[int | None, str]:
        """ask-consent копии дерева раннером с носителем стенда."""
        return self.s.run(["python3", "-c", DOOR_RUNNER, str(self.tree / "scripts" / "lib"), self.s.carrier,
                           str(self.tree / "scripts" / ASK.name), *args], env=self.env)

    def state(self) -> tuple[int | None, str]:
        return self.s.run(["python3", "-c", DOOR_RUNNER, str(self.tree / "scripts" / "lib"), self.s.carrier,
                           str(self.tree / "scripts" / SPEC_APPROVAL.name), "--состояние", TASK], env=self.env)

    def records(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []


def probe_spec(yard: Yard) -> None:
    print("— 9. одобрение спеки кнопкой (spec_approval)")
    st = SpecStand(yard)
    rc, out = st.ask("--вид", "спека", "--предмет", TASK, "--почему", "Проба двери спеки")
    got = st.s.requests()
    check(f"9а0) запрос --вид спека --предмет <id> → rc 0, цель «<id> <sha256>» [{short(out)}]",
          (0, st.subject()), (rc, got[-1].get("цель") if got else None))
    rows = [r for r in st.s.unsent(st.tree / "scripts" / "lib" / BUTTONS.name)
            if r.get("target") == f"спека:{st.subject()}"]
    text = rows[-1]["text"] if rows else ""
    check(f"9а0) текст: id, 8 знаков хэша, «24 ч» [{short(text)}]", (True, True, True),
          (TASK in text, st.subject().split(" ")[1][:8] in text, "24 ч" in text))
    st.spec.unlink()
    rc, out = st.ask("--вид", "спека", "--предмет", TASK, "--почему", "проба")
    check(f"9а0) нет файла спеки → rc 2 [{short(out)}]", 2, rc)

    st = SpecStand(yard)
    st.sent(ago_s=600)
    st.s.yes("спека", st.subject(), ago_s=300)
    rc, out = st.state()
    record = next((r for r in reversed(st.records()) if r.get("событие") == "одобрена"), {})
    check(f"9а) отправлена → «Да» кнопкой → --состояние одобрена [{short(out)}]", 0, rc)
    check("9а) запись: источник «кнопка», хэш текущей редакции, «слово» не пусто",
          ("кнопка", st.subject().split(" ")[1], True),
          (str(record.get("источник", ""))[:6], record.get("хэш"), bool(record.get("слово"))))

    st = SpecStand(yard)
    st.sent(ago_s=600)
    st.s.yes("спека", st.subject(), ago_s=300)
    first, _ = st.state()
    st.spec.write_text("# Спека стенда\n\nРедакция 2, правлена после «Да».\n", encoding="utf-8")
    rc, out = st.state()
    check(f"9б) Н-8: «Да» → проверка записала → правка файла → не одобрена [{short(out)}]", (0, 1), (first, rc))

    st = SpecStand(yard)
    st.s.yes("спека", st.subject(), ago_s=900)
    st.sent(ago_s=600)
    rc, out = st.state()
    check(f"9в) «Да» раньше отправки → не одобрена [{short(out)}]", 1, rc)

    st = SpecStand(yard)
    st.sent(ago_s=600)
    st.approved_by_text(ago_s=300)
    rc, out = st.state()
    check(f"9г0) охранный: текстовое одобрение записано, кнопок нет → одобрена [{short(out)}]", 0, rc)
    st.s.no("спека", st.subject(), ago_s=60)
    rc, out = st.state()
    check(f"9г) Н-8: текстовое одобрение → «Нет» кнопкой новее → не одобрена [{short(out)}]", 1, rc)

    st = SpecStand(yard)
    st.sent(ago_s=600)
    st.inbox("да", ago_s=300)
    rc, out = st.state()
    check(f"9д0) охранный: слово «да» владельца после отправки, кнопок нет → одобрена [{short(out)}]", 0, rc)
    st = SpecStand(yard)
    st.sent(ago_s=600)
    st.inbox("да", ago_s=300)
    st.s.no("спека", st.subject(), ago_s=120)
    rc, out = st.state()
    check(f"9д) слово «да» старше «Нет» кнопкой → не одобрена [{short(out)}]", 1, rc)
    st = SpecStand(yard)
    st.sent(ago_s=600)
    st.inbox("да", ago_s=300)
    st.s.no("спека", st.subject(), ago_s=120)
    st.spec.write_text("# Спека стенда\n\nРедакция 2 после «Нет».\n", encoding="utf-8")
    rc, out = st.state()
    check(f"9ж) Н-1: «Нет» на редакцию 1 → правка файла → слово «да» старше «Нет» → не одобрена [{short(out)}]",
          1, rc)

    for label, minutes, want in (("20 мин", 20, "Принято"), ("25 ч", 25 * 60, "Запрос устарел")):
        st = SpecStand(yard)
        nonce = secrets.token_urlsafe(16)
        with open(st.s.log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
            fh.write(envelope(SENT, now_iso(minutes * 60), {"nonce": nonce, "вид": "спека", "цель": st.subject(),
                                                            "message_id": 4321, "from_id": None}) + "\n")
        answer = st.s.press(nonce, 4321)
        check(f"9е) нажатие «Да» на спеку через {label} после отправки → «{want}…» [{answer}]", True,
              str(answer).startswith(want))

    st = SpecStand(yard, stub_ask=True)
    runner = SET_CARRIER + ("import sys; sys.path.insert(0, sys.argv[1]); import spec_approval as s; "
                            "set_carrier(s.owner_word, sys.argv[4]); "
                            "sys.exit(s.запросить_кнопку(sys.argv[2], 'Проба', sys.argv[3]))")
    digest = st.subject().split(" ")[1]
    rc, out = st.s.run(["python3", "-c", runner, str(st.tree / "scripts"), TASK, "f" * 64, st.s.carrier], env=st.env)
    called = st.argv.exists()
    check(f"9з) Н-15: хэш снимка ≠ хэш диска → rc 1, ask-consent не звался [{short(out)}]", (1, False), (rc, called))
    rc, out = st.s.run(["python3", "-c", runner, str(st.tree / "scripts"), TASK, digest, st.s.carrier], env=st.env)
    calls = st.argv.read_text(encoding="utf-8") if st.argv.exists() else ""
    check(f"9з2) хэш снимка = диска → rc 0, ask-consent --вид спека --предмет <id> [{short(calls)}]", (0, True),
          (rc, f"--вид спека --предмет {TASK}" in calls))


# ── 10. сторож охраняет журнал выкатов ───────────────────────────────────────
def probe_guard(yard: Yard) -> None:
    print("— 10. сторож: журнал выкатов — носитель погашения, правка из оболочки запрещена")
    s = yard.stand()
    journal = s.log / "deploy-releases.jsonl"
    journal.write_text("", encoding="utf-8")
    env = dict(s.env)
    for want, label, cmd in ((2, "sed -i по deploy-releases.jsonl", f"sed -i /a/d {journal}"),
                             (2, "echo > deploy-releases.jsonl", f"echo x > {journal}"),
                             (2, "rm deploy-releases.jsonl", f"rm {journal}"),
                             (0, "cat deploy-releases.jsonl", f"cat {journal}")):
        done = subprocess.run(["python3", str(GUARD_HOOK)], capture_output=True, text=True, env=env, timeout=60,
                              input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}))
        check(f"10) {label} → код {want}", want, done.returncode)


# ── 11. publish.sh: прогон ворот не старит согласие (hr.okno-soglasiya-korotche-progona-vorot) ──
# Час ворот на стенде не прождать — его проживают часы ворот согласия: подставные ворота
# пишут сдвиг в файл часов, обёртка deploy_guard.py стенда прибавляет его к time.time и
# datetime.now процесса настоящих ворот (механизм — рядом с пробой). Сдвиг — ход времени,
# а не правка записи: починка, запомнившая момент запуска, видит то же, что увидела бы вживую.
CLOCK_GUARD = SET_CARRIER + """import datetime, os, sys, time
def shift():
    try:
        return float(open(os.environ["PROBA_CLOCK"], encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        return 0.0
real_time = time.time
time.time = lambda: real_time() + shift()
Real = datetime.datetime
class Shifted(Real):
    @classmethod
    def now(cls, tz=None):
        return Real.now(tz) + datetime.timedelta(seconds=shift())
datetime.datetime = Shifted
sys.path.insert(0, os.environ["PROBA_REAL_SCRIPTS"])
import deploy_guard as g
set_carrier(g.слово_вл, open(os.path.join(os.path.dirname(__file__), "carrier.json"), encoding="utf-8").read())
g.две_смены = lambda: None
g.лок_файлы = lambda: []
g.долгие_процессы = lambda: []
sys.argv = [g.__file__, *sys.argv[1:]]
sys.exit(g.main())
"""
# Ворота стенда: метка запуска, ход времени, слово владельца, пришедшее за время прогона.
WINDOW_GATES = """#!/bin/bash
echo ворота >> "$PROBA_GATES_RAN"
[ -n "${PROBA_GATES_SHIFT:-}" ] && printf '%s\\n' "$PROBA_GATES_SHIFT" > "$PROBA_CLOCK"
[ -n "${PROBA_GATES_APPEND:-}" ] && cat "$PROBA_GATES_APPEND" >> "$PROBA_LOG_DIR/consent-buttons.jsonl"
[ -n "${PROBA_GATES_SAY:-}" ] && cat "$PROBA_GATES_SAY" >> "$PROBA_LOG_DIR/inbox.jsonl"
exit "${PROBA_GATES_RC:-0}"
"""
LOCK2_PASSED = "замок 2 · согласие владельца на публикацию есть"


def window_stand(yard: Yard) -> tuple[Stand, dict]:
    s = yard.stand(publish=True)
    project = s.root / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    (project / "scripts" / "gates.sh").write_text(WINDOW_GATES, encoding="utf-8")
    (project / "scripts" / "deploy_guard.py").write_text(CLOCK_GUARD, encoding="utf-8")
    (project / "scripts" / "carrier.json").write_text(s.carrier, encoding="utf-8")
    door_project(project)
    with open(s.root / "harness.conf", "a", encoding="utf-8") as fh:
        fh.write('PUBLIC_COMMIT_EMAIL="proba@stand.invalid"\nPUBLIC_COMMIT_NAME="proba"\n')
    env = dict(s.env, PROJECT_DIR=str(project), STARTER_DIR=str(s.pkg), PROBA_REAL_SCRIPTS=str(SCRIPTS),
               PROBA_CLOCK=str(s.root / "часы"), PROBA_GATES_RAN=str(s.root / "ворота-запускались"),
               # LOG_DIR дверь берёт паспортом и детям не отдаёт (АР-4): журнал стенда — своим ключом.
               PROBA_LOG_DIR=str(s.log))
    return s, env


def window_publish(s: Stand, env: dict, check_only: bool, **gates: str) -> tuple[int | None, str, bool, bool]:
    """→ (код publish.sh, вывод, замок 2 пройден, ворота запускались)."""
    rc, out = s.run(["bash", str(s.root / "project" / "scripts" / "publish.sh"), *(["--проверка"] if check_only else [])],
                    env=dict(env, **gates), timeout=180)
    return rc, out, LOCK2_PASSED in out, Path(env["PROBA_GATES_RAN"]).exists()


def probe_window(yard: Yard) -> None:
    print("— 11. publish.sh: согласие судится на запуске, прогон ворот его не старит")
    half_hour, fifty_min = "1800", 50 * 60

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=fifty_min)
    guard = ["python3", str(s.root / "project" / "scripts" / "deploy_guard.py"), "--действие", "публикация",
             "--только-согласие", "--публикация", str(s.pkg)]
    rc_now, out_now = s.run(guard, env=env)
    Path(env["PROBA_CLOCK"]).write_text(half_hour, encoding="utf-8")
    rc_later, out_later = s.run(guard, env=env)
    check(f"11а) контроль часов стенда: «Да» 50 мин → сейчас rc 0, через 30 мин rc 1 «нет согласия» "
          f"[{short(out_now)} || {short(out_later)}]", (0, 1, True), (rc_now, rc_later, "нет согласия" in out_later))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=5 * 60)
    rc, out, passed, ran = window_publish(s, env, True)
    check(f"11б) охранный: «Да» 5 мин, ворота быстрые → замок 2 пройден, ворота шли [{short(out[-300:])}]",
          (True, True), (passed, ran))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=fifty_min)
    rc, out, passed, ran = window_publish(s, env, True, PROBA_GATES_SHIFT=half_hour)
    check(f"11в) П1: «Да» кнопкой 50 мин, ворота идут 30 мин, --проверка → замок 2 пройден, "
          f"второго «да» не просит [{short(out[-300:])}]", (True, False), (passed, "попроси кнопку" in out))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=fifty_min)
    rc, out, passed, ran = window_publish(s, env, False, PROBA_GATES_SHIFT=half_hour)
    check(f"11г) П1: то же боевым прогоном → замок 2 пройден [{short(out[-300:])}]",
          (True, False), (passed, "попроси кнопку" in out))

    s, env = window_stand(yard)
    s.text("да, публикуй", ago_s=5 * 60)
    rc, out, passed, ran = window_publish(s, env, False)
    check(f"11д) БОЛЬНОЙ СЛУЧАЙ: текстовое «да, публикуй» 5 мин, боевой прогон → отказ ДО ворот, замок 2 "
          f"не пройден [{short(out[-300:])}]", (1, False, False), (rc, passed, ran))

    s, env = window_stand(yard)
    rc, out, passed, ran = window_publish(s, env, True)
    check(f"11е) П2: согласия нет → отказ ДО ворот, ворота не запускались [{short(out[-300:])}]",
          (1, False, False, True), (rc, passed, ran, "нет согласия" in out))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=61 * 60)
    rc, out, passed, ran = window_publish(s, env, True)
    check(f"11ж) П3: «Да» 61 мин на запуске → отказ ДО ворот, ворота не запускались [{short(out[-300:])}]",
          (1, False, False, True), (rc, passed, ran, "нет согласия" in out))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=5 * 60)
    late_no = s.root / "нет-во-время-ворот.jsonl"
    late_no.write_text(envelope(NO, now_iso(), {"nonce": secrets.token_urlsafe(16), "вид": "публикация",
                                                "цель": s.publish_subject(), "message_id": 6001,
                                                "from_id": OWNER}) + "\n", encoding="utf-8")
    rc, out, passed, ran = window_publish(s, env, True, PROBA_GATES_APPEND=str(late_no))
    check(f"11з) П4: «Да» свежее на запуске, «Нет» того же предмета во время ворот → замок 2 отказал "
          f"[{short(out[-300:])}]", (1, False, True), (rc, passed, ran))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=5 * 60)
    rc, out, passed, ran = window_publish(s, env, False, PROBA_GATES_RC="1")
    rc_after, out_after = s.gate_publish()
    check(f"11и) П5: «Да» кнопкой, ворота красные, боевой прогон → отказ, кнопка не погашена и годна "
          f"следующей попытке [{short(out[-200:])} || {short(out_after)}]",
          (1, True, 0), (rc, ran, rc_after))

    # Слово владельца, пришедшее во время ворот: метка ПОСЛЕ момента попытки (часы ворот
    # сдвинуты на 30 мин, метка — за 10 с до их «сейчас»). Применение момента к отзыву или
    # к «нет» скрыло бы такое слово от замка 2.
    during = now_iso(-(int(half_hour) - 10))

    def said_during(s: Stand, words: str) -> str:
        path = s.root / "слово-во-время-ворот.jsonl"
        path.write_text(json.dumps({"ts": during, "text": words, "message_id": "9777"}, ensure_ascii=False)
                        + "\n", encoding="utf-8")
        return str(path)

    def no_during(s: Stand, target: str) -> str:
        path = s.root / "нет-во-время-ворот.jsonl"
        path.write_text(envelope(NO, during, {"nonce": secrets.token_urlsafe(16), "вид": "публикация",
                                              "цель": target, "message_id": 6002, "from_id": OWNER}) + "\n",
                        encoding="utf-8")
        return str(path)

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=fifty_min)
    rc, out, passed, ran = window_publish(s, env, False, PROBA_GATES_SHIFT=half_hour,
                                          PROBA_GATES_SAY=said_during(s, "стоп, не публикуй"))
    check(f"11к) «Да» кнопкой 50 мин, ворота 30 мин, «стоп, не публикуй» во время ворот (после момента) "
          f"→ замок 2 отказал [{short(out[-300:])}]", (1, False, True), (rc, passed, ran))

    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=fifty_min)
    rc, out, passed, ran = window_publish(s, env, True, PROBA_GATES_SHIFT=half_hour,
                                          PROBA_GATES_APPEND=no_during(s, s.publish_subject()))
    check(f"11л) «Да» кнопкой 50 мин, ворота 30 мин, «Нет» того же предмета во время ворот (после момента) "
          f"→ замок 2 отказал [{short(out[-300:])}]", (1, False, True), (rc, passed, ran))

    # Решение судится по точной цели: «Нет» на чужой предмет кнопку этого предмета не гасит.
    s, env = window_stand(yard)
    s.yes("публикация", s.publish_subject(), ago_s=fifty_min)
    rc, out, passed, ran = window_publish(s, env, True, PROBA_GATES_SHIFT=half_hour,
                                          PROBA_GATES_APPEND=no_during(s, "другой предмет"))
    check(f"11м) «Да» кнопкой 50 мин, ворота 30 мин, «Нет» кнопкой на чужой предмет во время ворот "
          f"→ замок 2 пройден [{short(out[-300:])}]", (True, True), (passed, ran))

    # Момент назначает зовущий, поэтому его пределы — дверь: потолок задан стендом явно
    # (данными, как в harness.conf), его число обязано прозвучать в отказе.
    ceiling = 60

    def anchor(min_ago: float) -> str:
        return str(int(time.time() - min_ago * 60))

    def ceiling_stand() -> Stand:
        s = yard.stand(publish=True)
        with open(s.root / "harness.conf", "a", encoding="utf-8") as fh:
            fh.write(f"CONSENT_FRESHNESS_ANCHOR_MAX_MIN={ceiling}\n")
        return s

    # «Да» кнопкой свежее: без пределов момента каждый из этих зовов дал бы rc 0.
    s = ceiling_stand()
    s.yes("публикация", s.publish_subject(), ago_s=5 * 60)
    rc, out = s.gate_publish("--свежесть-на", anchor(ceiling + 1))
    check(f"11н) момент «потолок {ceiling} + 1 мин» назад → rc 1, отказ называет потолок [{short(out)}]",
          (1, True), (rc, f"{ceiling} мин" in out))

    s = ceiling_stand()
    s.yes("публикация", s.publish_subject(), ago_s=5 * 60)
    rc, out = s.gate_publish("--свежесть-на", anchor(-2))
    check(f"11о) момент через 2 мин (в будущем) → rc 1, отказ «момент попытки в будущем» без «−» перед "
          f"минутами [{short(out)}]", (1, True, False),
          (rc, "момент попытки в будущем" in out, re.search(r"[−-]\s*\d+\s*мин", out) is not None))

    # Сравнения с NaN ложны, поэтому нечисловой момент обязан падать на разборе, а не на границах.
    # «Да» кнопкой свежее: принятый момент открыл бы дверь, rc 0 — это и видно.
    fractional = f"{anchor(10)}.5"
    for bad in ("nan", "inf", "-inf", "12.5", fractional, "abc", ""):
        s = ceiling_stand()
        s.yes("публикация", s.publish_subject(), ago_s=5 * 60)
        rc, out = s.gate_publish("--свежесть-на", bad)
        check(f"11у) --свежесть-на «{bad}» при свежем «Да» → код 1 или 2, ворота не пишут согласие.json "
              f"[{short(out)}]", (True, False), (rc in (1, 2), (s.log / "согласие.json").exists()))

    s = ceiling_stand()
    s.yes("публикация", s.publish_subject(), ago_s=300 * 60)
    rc, out = s.gate_publish("--свежесть-на", "nan")
    check(f"11ф) «Да» кнопкой 300 мин, --свежесть-на nan → не rc 0 [{short(out)}]", True, rc in (1, 2))

    s = ceiling_stand()
    s.yes("публикация", s.publish_subject(), ago_s=100 * 60)
    rc, out = s.gate_publish("--свежесть-на", anchor(ceiling - 1))
    check(f"11п) охранный: момент «потолок {ceiling} − 1 мин», «Да» кнопкой 41 мин от момента → rc 0 "
          f"[{short(out)}]", 0, rc)
    s = ceiling_stand()
    s.text("да, публикуй", ago_s=100 * 60)
    rc, out = s.gate_publish("--свежесть-на", anchor(ceiling - 1))
    check(f"11п2) то же, но «да» текстом → rc 1: момент текст не оживляет [{short(out)}]", 1, rc)

    # Ключ момента нужен только замку 2 публикации; выкату и пушу он открыл бы просроченное «да».
    for action, words in (("deploy", "да, выкатывай"), ("push", "да, пушь в гит")):
        s = ceiling_stand()
        s.text(words, ago_s=5 * 60)
        rc, out = s.gate(action, "--свежесть-на", anchor(10))
        try:
            state = json.loads((s.log / "согласие.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        spent = [k for k in state.get("израсходованы") or [] if k.endswith(f"|{action}")]
        check(f"11р) --действие {action} --свежесть-на → код 2, «да» не израсходовано "
              f"(израсходованы {spent}) [{short(out)}]", (2, []), (rc, spent))

    s = ceiling_stand()
    s.yes("публикация", s.publish_subject(), ago_s=110 * 60)
    pressed = json.loads((s.log / "consent-buttons.jsonl").read_text(encoding="utf-8").splitlines()[-1])["ts"]
    rc_at, out_at = s.gate_publish("--свежесть-на", anchor(ceiling - 5))
    rc_again, out_again = s.gate_publish()
    rc_deploy, out_deploy = s.gate_deploy()
    check(f"11с) «Да» 110 мин (срок 60): на момент −{ceiling - 5} мин открыто, следующий спрос без ключа "
          f"(публикация и выкат) → rc 1 [{short(out_at)} || {short(out_again)} || {short(out_deploy)}]",
          (0, 1, 1), (rc_at, rc_again, rc_deploy))
    check(f"11т) открытие моментом называет настоящее время нажатия ({pressed}), а не момент "
          f"[{short(out_at)}]", True, pressed in out_at)


CASE_NAMES = ("запрос", "текст", "выкат", "публикация", "сверка", "замок", "пуш", "сборка", "спека", "сторож",
              "окно")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--случай", dest="only", action="append", choices=CASE_NAMES)
    only = set(parser.parse_args().only or CASE_NAMES)
    yard = Yard()
    for name, run in zip(CASE_NAMES, (probe_requests, probe_texts, probe_deploy, probe_publish, probe_verify,
                                      probe_lock, probe_before_push, probe_build, probe_spec, probe_guard,
                                      probe_window)):
        if name not in only:
            continue
        try:
            run(yard)
        except Exception as err:          # фикстура упала — проба красная, а не молчит
            check(f"{name}) фикстура стенда отработала", "без исключения", repr(err)[:300])
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
