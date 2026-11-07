#!/usr/bin/env python3
"""test_consent_button_only.py — приёмка hr.consent-buttons-doors (spec.md §8, spec-review.md MJ1-MJ3, m9).

Владелец 05.11.2026: «все вопросы по поводу моего Да должны приходить кнопками в телегу»,
«нужно не как правило, а кодом». Текст владельца не открывает ни одной необратимой двери:
выкат, публикация, пуш, удаление (protected-delete и rm-сторож guard_bash), снос базы,
перепись истории. Открывает только «Да» кнопкой: конверт consent.yes в consent-buttons.jsonl
на вид и предмет двери, с парным consent.sent (nonce и message_id), как пишет consent_buttons.judge.

Стенд свой на каждый случай (temp-каталог): HARNESS_OWNER_WORD_STEND=1, свой LOG_DIR,
harness.conf (DEPLOY_CONSENT_STANDING=0, *_NEEDS_OWNER=1), install.conf (AUTONOMY=semi),
свои git-репо и голые удалённые (file://), заглушки docker, СУБД, gates.sh, tg_send.sh.
Выкат и публикация — стенд test_consent_doors.py (m9): проект стенда со ссылками на настоящие
скрипты, deploy_guard.py проекта — обёртка над настоящим без «занятости машины»; «открыто»
мерится строкой за воротами (заглушка check-secrets.sh у выката, «замок 2 · …» у публикации),
а не завершением выката. Слово владельца — как его кладёт tg-dispatcher.sh: inbox.jsonl,
inbox/<ts>.txt и confirmations.jsonl по вердикту patterns.вердикт_согласия.
Тексты больных случаев — дословно из стенда /var/log/harness/stand-p3-prikaz.log.
Механизмы берутся РЯДОМ с пробой: мутация ставится в копию дерева, и зовётся проба копии.
Машинный журнал запросов только читается: счёт строк и mtime до и после.

    python3 scripts/test_consent_button_only.py
Итог — «ИТОГО: путей N, неудач K»; код 1 при K>0.
"""
from __future__ import annotations

import ast
import atexit
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
LIB = SCRIPTS / "lib"
sys.path.insert(0, str(LIB))
import owner_word  # noqa: E402
import patterns  # noqa: E402

# Проект: scripts/hooks; пакет: harness/scripts рядом с harness/hooks.
HOOKS = next(p for p in (SCRIPTS / "hooks", SCRIPTS.parent / "hooks") if (p / "guard_bash.py").is_file())
GUARD_HOOK = HOOKS / "guard_bash.py"
HOOK_SETTINGS = next((p for p in (SCRIPTS.parent / ".claude" / "settings.json", HOOKS / "project-settings.json")
                      if p.is_file()), None)
DEPLOY_GUARD = SCRIPTS / "deploy_guard.py"
PROTECTED_DELETE = SCRIPTS / "protected-delete.py"
ASK = SCRIPTS / "ask-consent.py"
PUBLISH = SCRIPTS / "publish.sh"
DROP_DB = SCRIPTS / "drop-database.sh"
DEPLOY_SH = SCRIPTS / "deploy.sh"

OWNER = 777000
TTL_MIN = 15
FORBIDDEN = "ProbaShopXyz"
DB = "proba_db1"
REAL_GIT = shutil.which("git") or "/usr/bin/git"
GIT_ENV = {"GIT_AUTHOR_NAME": "proba", "GIT_AUTHOR_EMAIL": "proba@stand.invalid",
           "GIT_COMMITTER_NAME": "proba", "GIT_COMMITTER_EMAIL": "proba@stand.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_ALLOW_PROTOCOL": "file"}
# Ключи окружения сессии, которые загрузчик конфига предпочёл бы файлу стенда.
FOREIGN_ENV = re.compile(r"^(HARNESS_.*|PROJECT_DIR|PRODUCT_.*|PUBLIC_.*|STARTER_DIR|INSTALL_CONF|PACKAGE_DIR"
                         r"|LOG_DIR|SECRETS_DIR|BACKUP_DIR|STATE_DIR|HEARTBEAT_DIR|PANEL_STATE_DIR|STACK_.*"
                         r"|DEPLOY_.*|PUSH_.*|PUBLISH_.*|CONFIRM_MAX_AGE_MIN|CONSENT_.*|DROPPABLE_DB_PREFIX"
                         r"|DB_ADMIN_.*|AUTONOMY|AGENT_USER|SECRET_DELETE_.*|GIT_.*)$")
YES, NO = "consent.yes", "consent.no"
LOCK2 = "замок 2 · согласие владельца на публикацию есть"
# Просьбы, которые стенд кладёт к своим нажатиям: счёт запросов судит только то, что положила дверь.
STAND_ASK = "proba-stand"
PAST_GATE = "PROBA-ЗА-ВОРОТАМИ"

PATHS = 0
FAILS: list[str] = []


def verdict(name: str, ok: bool, why: str = "", detail: str = "") -> bool:
    global PATHS
    PATHS += 1
    tail = f" [{detail}]" if detail else ""
    if ok:
        print(f"ЗЕЛ   {name}{tail}")
    else:
        FAILS.append(name)
        print(f"КРАСН {name} — {why}{tail}")
    return ok


def short(text: str, n: int = 260) -> str:
    return " | ".join(line for line in text.splitlines() if line.strip())[-n:]


def reason(text: str) -> str:
    """Причина отказа хука — первые строки после шапки, а не хвост-памятка."""
    lines = [line.strip() for line in text.splitlines() if line.strip() and "остановлена хуком" not in line]
    return " | ".join(lines[:3])[:260]


def iso(when: float) -> str:
    return dt.datetime.fromtimestamp(when).astimezone().isoformat(timespec="seconds")


def next_second() -> None:
    """ts диспетчера — секунды: слово обязано лечь позже вопроса двери."""
    mark = int(time.time())
    while int(time.time()) <= mark:
        time.sleep(0.1)


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run([REAL_GIT, *args], cwd=cwd, capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV), timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git("add", "-A", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


# deploy_guard.py проекта стенда: настоящие ворота без «двух смен» и занятости машины —
# это состояние машины, а не предмет пробы.
GATE_WRAPPER = """import os, sys
sys.path.insert(0, os.environ["PROBA_REAL_SCRIPTS"])
import deploy_guard as g
g.две_смены = lambda: None
g.лок_файлы = lambda: []
g.долгие_процессы = lambda: []
sys.argv = [g.__file__, *sys.argv[1:]]
sys.exit(g.main())
"""
GATE_SLEEP = "import time\ntime.sleep(30)\n"
GATE_CRASH = "raise RuntimeError('ворота стенда упали')\n"
DB_STUB = """#!/bin/bash
# Заглушка DB_ADMIN_EXEC: pg_dump печатает дамп, psql -tAc отвечает «база есть», psql со входом — снос.
echo "$*" >> "{calls}"
case "$1" in
  pg_dump) echo "-- дамп стенда"; exit 0 ;;
  psql) case "$*" in *-tAc*) [ -e "{mark}" ] && echo 1; exit 0 ;; esac
        cat > /dev/null; rm -f "{mark}"; exit 0 ;;
esac
exit 0
"""
# Хук с подставным исключением в судье (случай 19): любая функция суда owner_word бросает.
HOOK_BOOM = """import importlib.util, io, sys
spec = importlib.util.spec_from_file_location("guard_bash_proba", sys.argv[1])
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
def boom(*a, **k):
    raise RuntimeError("подставное исключение судьи")
for name in ("судить_кнопкой", "разрешение", "согласие_кнопкой", "последнее_решение", "слова_владельца"):
    if hasattr(g.слово_вл, name):
        setattr(g.слово_вл, name, boom)
sys.exit(g.main())
"""


class Stand:
    """Свой LOG_DIR, конфиги, проект с заглушками, продукт, пуш-репо, публичный удалённый, секреты, СУБД."""

    def __init__(self, root: Path, gate: str = "wrapper") -> None:
        self.root = root
        self.log = root / "log"
        self.secrets = root / "secrets"
        self.backups = root / "backups"
        self.proj = root / "proj"
        self.bin = root / "bin"
        for d in (self.log, self.secrets, self.backups, self.bin, root / "home", root / "state"):
            d.mkdir(parents=True, exist_ok=True)
        # Журнал входящих на машине есть всегда; пустой — «владелец молчал», а не «носителя нет».
        write(self.log / "inbox.jsonl", "")
        old = time.time() - 30 * 86400
        self.target = write(self.secrets / "stand-old-snapshot.env", "STAND=1\n", 0o600)
        self.other_target = write(self.secrets / "stand-other-snapshot.env", "STAND=2\n", 0o600)
        for p in (self.target, self.other_target):
            os.utime(p, (old, old))
        self.db_calls = root / "db-calls.log"
        self.db_mark = write(root / "db-exists", "1\n")
        self.db_stub = write(self.bin / "db-admin", DB_STUB.format(calls=self.db_calls, mark=self.db_mark), 0o755)
        write(self.bin / "docker", f'#!/bin/sh\necho "docker $*" >> "{root}/docker-calls.log"\nexit 0\n', 0o755)
        self._product()
        self._push_repo()
        self._public()
        self._project(gate)
        self._configs()

    # ── стенд ──────────────────────────────────────────────────────────────
    def _product(self) -> None:
        self.prod = self.root / "prod"
        self.prod.mkdir()
        git("init", "-q", "-b", "master", cwd=self.prod)
        write(self.prod / "app.py", "print('база')\n")
        self.prod_old = commit_all(self.prod, "база")
        write(self.prod / "app.py", "print('один')\n")
        commit_all(self.prod, "коммит 1")

    def _push_repo(self) -> None:
        self.push_remote = self.root / "push-remote.git"
        git("init", "-q", "--bare", "-b", "master", str(self.push_remote), cwd=self.root)
        self.push_url = f"file://{self.push_remote}"
        self.work = self.root / "work"
        self.work.mkdir()
        git("init", "-q", "-b", "master", cwd=self.work)
        write(self.work / "a.txt", "a\n")
        self.push_base = commit_all(self.work, "база пуша")
        git("remote", "add", "origin", self.push_url, cwd=self.work)
        git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=self.work)
        git("fetch", "-q", "origin", cwd=self.work)
        write(self.work / "a.txt", "b\n")
        self.push_other = commit_all(self.work, "пуш 1")
        write(self.work / "a.txt", "c\n")
        self.push_head = commit_all(self.work, "пуш 2")

    def _public(self) -> None:
        """Как test_publish_rewrite_door.sh (стенд clean): сборка опубликована, копия — та же
        итоговая дерево с другой историей и теми же тегами, бэкап удалённого — bundle."""
        self.remote = self.root / "remote.git"
        git("init", "-q", "--bare", "-b", "master", str(self.remote), cwd=self.root)
        self.url = f"file://{self.remote}"
        self.pkg = self.root / "pkg"
        for rel, text in (("harness/CLAUDE.md", "# харнес\n@STATE.md\n"), ("harness/STATE.md", "состояние\n"),
                          ("harness/память/TONE.md", "# ТОН\nзамер речи\n"),
                          ("harness/шаблоны-задач/ТОН-заготовка.md",
                           "# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n"),
                          ("harness/config/public-build.yaml",
                           "исключить: []\nзаменить_шаблоном:\n  harness/память/TONE.md: "
                           "harness/шаблоны-задач/ТОН-заготовка.md\nобезличить_ключи: [PROJECT_DIR]\n"
                           "оставить: [harness/CLAUDE.md]\n"),
                          ("ЧТО-НОВОГО.md", "# Что нового\n\n## 2026-11-06 — Кнопки\n\n### Исправлено\n\n"
                           "- Сессия больше не обрывается.\n\n### Что изменилось для вас\n\n- Ничего.\n")):
            write(self.pkg / rel, text)
        git("init", "-q", "-b", "master", cwd=self.pkg)
        self.pkg_old = commit_all(self.pkg, "пакет стенда")
        git("remote", "add", "jig", self.url, cwd=self.pkg)
        write(self.pkg / "door.md", "дверь\n")
        pkg_head = commit_all(self.pkg, "пакет 2")

        def final_tree(d: Path) -> None:
            write(d / "README.md", "# jig-harness\nСтавится куда угодно.\n")
            write(d / "harness" / "kod.py", "ПОРОГ = 15\n")
            write(d / ".публичная-сборка", f"стенд\nпакет: {pkg_head}\nсобрано: 2026-11-03T00:00:00+05:00\n")

        self.build = self.root / "build"
        self.build.mkdir()
        git("init", "-q", "-b", "master", cwd=self.build)
        write(self.build / "README.md", f"старая версия, магазин {FORBIDDEN}\n")
        commit_all(self.build, "версия от 2026-09-12")
        git("tag", "-a", "v0.1.0", "-m", "релиз", cwd=self.build)
        final_tree(self.build)
        commit_all(self.build, "версия от 2026-11-01")
        git("tag", "-a", "v0.2.0", "-m", "релиз", cwd=self.build)
        git("remote", "add", "jig", self.url, cwd=self.build)
        git("push", "-q", "jig", "refs/heads/master", "refs/tags/*:refs/tags/*", cwd=self.build)
        git("fetch", "-q", "jig", cwd=self.build)

        self.copy = self.root / "copy"
        self.copy.mkdir()
        git("init", "-q", "-b", "master", cwd=self.copy)
        write(self.copy / "README.md", "первая версия\n")
        commit_all(self.copy, "версия от 2026-09-12 (переписано)")
        git("tag", "-a", "v0.1.0", "-m", "релиз", cwd=self.copy)
        final_tree(self.copy)
        commit_all(self.copy, "версия от 2026-11-01 (переписано)")
        git("tag", "-a", "v0.2.0", "-m", "релиз", cwd=self.copy)
        self.bundle = self.root / "old.bundle"
        self.new_bundle()

    def new_bundle(self) -> Path:
        git("bundle", "create", str(self.bundle), "--all", cwd=self.remote)
        return self.bundle

    def _project(self, gate: str) -> None:
        """Проект стенда: ссылки на настоящие скрипты; ворота согласия — обёртка; прочее заглушено."""
        scripts = self.proj / "scripts"
        scripts.mkdir(parents=True)
        own = {"deploy_guard.py", "gates.sh", "tg_send.sh", "deploy.sh", "check-secrets.sh"}
        for f in SCRIPTS.iterdir():
            if f.name not in own and not f.name.startswith("."):
                (scripts / f.name).symlink_to(f)
        gate_text = {"wrapper": GATE_WRAPPER, "sleep": GATE_SLEEP, "crash": GATE_CRASH}.get(gate)
        if gate_text is not None:
            write(scripts / "deploy_guard.py", gate_text)
        write(scripts / "gates.sh", f'#!/bin/sh\necho "gates $*" >> "{self.root}/gates-called"\nexit 0\n', 0o755)
        write(scripts / "tg_send.sh", f'#!/bin/sh\necho "$*" >> "{self.root}/tg-sent"\n', 0o755)
        write(scripts / "check-secrets.sh", f'#!/bin/sh\necho "{PAST_GATE}"\nexit 1\n', 0o755)
        # deploy.sh — копия, а не ссылка: свой каталог он берёт по readlink -f.
        shutil.copy2(DEPLOY_SH, scripts / "deploy.sh")
        write(self.proj / "README.md", "проект стенда\n")
        git("init", "-q", "-b", "master", cwd=self.proj)
        commit_all(self.proj, "проект стенда")

    def _configs(self) -> None:
        self.install_conf = write(self.root / "install.conf", (
            f'PROJECT_DIR="{self.proj}"\nLOG_DIR="{self.log}"\nAUTONOMY="semi"\nAGENT_USER="podstavnoj"\n'
            f'PROJECT_NAME="{FORBIDDEN}"\nTG_CHAT_ID="{OWNER}"\nSECRETS_DIR="{self.secrets}"\n'
            f'BACKUP_DIR="{self.backups}"\nSTARTER_DIR="{self.pkg}"\n'))
        self.harness_conf = write(self.root / "harness.conf", self.harness_text())
        self.env = {k: v for k, v in os.environ.items() if not FOREIGN_ENV.match(k)}
        self.env.update(GIT_ENV, HOME=str(self.root / "home"), PATH=f"{self.bin}:{os.environ.get('PATH', '')}",
                        HARNESS_HOME=str(self.root), HARNESS_INSTALL_CONF=str(self.install_conf),
                        HARNESS_CONF=str(self.harness_conf), HARNESS_LOG_DIR=str(self.log), LOG_DIR=str(self.log),
                        HARNESS_OWNER_WORD_STEND="1", HARNESS_TASK="-", CONSENT_BUTTON_TTL_MIN=str(TTL_MIN),
                        CONFIRM_MAX_AGE_MIN="60", SECRETS_DIR=str(self.secrets), BACKUP_DIR=str(self.backups),
                        PROJECT_DIR=str(self.proj), STARTER_DIR=str(self.pkg), PUBLIC_REMOTE="jig",
                        PUBLIC_BRANCH="master", PUBLIC_BUILD_DIR=str(self.build),
                        PUBLIC_COMMIT_NAME="proba-harness", PUBLIC_COMMIT_EMAIL="proba@harness.local",
                        PUBLIC_PASPORT=str(self.install_conf), DROPPABLE_DB_PREFIX="proba_",
                        DB_ADMIN_EXEC=str(self.db_stub), STATE_DIR=str(self.root / "state"),
                        HEARTBEAT_DIR=str(self.root / "state" / "hb"),
                        PANEL_STATE_DIR=str(self.root / "state" / "panel"),
                        PROBA_REAL_SCRIPTS=str(SCRIPTS), PYTHONDONTWRITEBYTECODE="1")

    def harness_text(self, **override: str) -> str:
        keys = {"DEPLOY_NEEDS_OWNER": "1", "PUBLISH_NEEDS_OWNER": "1", "PUSH_NEEDS_OWNER": "1",
                "DEPLOY_CONSENT_STANDING": "0", "CONFIRM_MAX_AGE_MIN": "60", "CONSENT_BUTTON_TTL_MIN": str(TTL_MIN),
                "SERVICE_LOCKS_SUBDIR": "services", "PRODUCT_REPO": str(self.prod), "STACK_BUILD_CMD": "true",
                "STACK_UP_CMD": "true", "STACK_HEALTH_CMD": "true", "PUBLIC_BUILD_DIR": str(self.build),
                "PUBLIC_REMOTE": "jig", "PUBLIC_BRANCH": "master", "PUBLIC_COMMIT_NAME": "proba-harness",
                "PUBLIC_COMMIT_EMAIL": "proba@harness.local", "STARTER_DIR": str(self.pkg),
                "DROPPABLE_DB_PREFIX": "proba_", "DB_ADMIN_EXEC": str(self.db_stub), **override}
        return "".join(f'{k}="{v}"\n' for k, v in keys.items())

    # ── предметы, посчитанные пробой независимо от механизма ───────────────
    def deploy_subject(self, sha: str | None = None) -> str:
        return f"{sha or git('rev-parse', 'HEAD', cwd=self.prod)} {os.path.realpath(self.prod)}"

    def publish_subject(self, pkg_sha: str | None = None) -> str:
        tip = git("ls-remote", self.url, "refs/heads/master", cwd=self.pkg).split("\t")[0]
        return " ".join([pkg_sha or git("rev-parse", "HEAD", cwd=self.pkg), self.url, "refs/heads/master", tip,
                         git("rev-parse", "refs/heads/master", cwd=self.build),
                         os.path.realpath(self.pkg), os.path.realpath(self.build)])

    def push_subject(self, head: str | None = None) -> str:
        return (f"{self.push_base}..{head or self.push_head} {self.push_url} refs/heads/master "
                f"{os.path.realpath(self.work)}")

    def remote_refs(self) -> str:
        return git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags",
                   cwd=self.remote)

    def rewrite_guess(self) -> str:
        """Предмет переписи по §5 — запасной, пока дверь сама не назвала его в запросе."""
        lines = "\n".join(sorted(self.remote_refs().splitlines())) + "\n"
        tip = git("rev-parse", "refs/heads/master", cwd=self.remote)
        new = git("rev-parse", "refs/heads/master", cwd=self.copy)
        return f"{self.url} refs/heads/master {tip}..{new} {hashlib.sha256(lines.encode()).hexdigest()[:12]}"

    def rewrite_subject(self) -> tuple[str, bool]:
        """(предмет, назван дверью): цель последнего запроса вида «перепись», иначе запасной."""
        got = self.requests("перепись")
        return (got[-1].get("цель", ""), True) if got else (self.rewrite_guess(), False)

    # ── слова владельца ────────────────────────────────────────────────────
    def button(self, event: str, kind: str, target: str, ago_s: float, sender: int | None = OWNER,
               asked_ago: float | None = None) -> None:
        """Живой порядок: просьба ask-consent (asked_ago, по умолчанию раньше «отправлено»), «отправлено»,
        решение с from_id нажавшего. asked_ago=-1 — просьбы нет."""
        nonce = secrets.token_urlsafe(16)
        message_id = random.randint(4000, 999999)
        if asked_ago != -1:
            self.request_row(kind, target, ago_s + 60 if asked_ago is None else asked_ago, nonce)
        sent = {"nonce": nonce, "вид": kind, "цель": target, "message_id": message_id, "from_id": None}
        decided = {"nonce": nonce, "вид": kind, "цель": target, "message_id": message_id}
        if sender is not None:
            decided["from_id"] = sender
        with open(self.log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
            for name, data, ago in (("consent.sent", sent, ago_s + 30), (event, decided, ago_s)):
                fh.write(json.dumps({"v": 1, "ts": iso(time.time() - ago), "src": "scripts/lib/consent_buttons.py",
                                     "event": name, "outcome": "ok", "data": data}, ensure_ascii=False) + "\n")

    def request_row(self, kind: str, target: str, ago_s: float, nonce: str = "") -> None:
        """Просьба кнопки, как её кладёт ask-consent; src стенда — Stand.requests её не считает."""
        data = {"nonce": nonce or secrets.token_urlsafe(16), "вид": kind, "цель": target, "почему": "проба"}
        with open(self.log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": iso(time.time() - ago_s), "src": STAND_ASK, "event": "consent.request",
                                 "outcome": "ok", "data": data}, ensure_ascii=False) + "\n")

    def yes(self, kind: str, target: str, ago_s: float = 60) -> None:
        self.button(YES, kind, target, ago_s)

    def no(self, kind: str, target: str, ago_s: float = 60) -> None:
        self.button(NO, kind, target, ago_s)

    def say(self, text: str, ago_s: float = 0) -> None:
        """Как tg-dispatcher.sh: inbox.jsonl, файл очереди inbox/<ts>.txt, confirmations по вердикту судьи."""
        when = time.time() - ago_s
        message_id = str(random.randint(100000, 999999))
        with open(self.log / "inbox.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": iso(when), "message_id": message_id, "chat_id": "stand", "text": text},
                                ensure_ascii=False) + "\n")
        queue = write(self.log / "inbox" / f"{when:.9f}.txt", text + "\n")
        os.utime(queue, (when, when))
        for line in text.splitlines():
            line = line.strip().lower()
            kind = patterns.вердикт_согласия(line) if line else "нет"
            if kind in ("deploy", "any", "адресовано", "текст"):
                row = {"ts": iso(when), "text": line, "message_id": message_id}
                if kind != "текст":
                    row["kind"] = kind
                with open(self.log / "confirmations.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def requests(self, kind: str, target: str | None = None) -> list[dict]:
        try:
            rows = [json.loads(line) for line in
                    (self.log / "consent-requests.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []
        found = [r.get("data") or {} for r in rows if r.get("src") != STAND_ASK]
        return [d for d in found if d.get("вид") == kind and (target is None or d.get("цель") == target)]

    # ── вызовы ─────────────────────────────────────────────────────────────
    def run(self, argv: list[str], stdin: str | None = None, env: dict | None = None,
            timeout: int = 180) -> tuple[int | None, str, float]:
        start = time.monotonic()
        try:
            done = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env or self.env,
                                  timeout=timeout, cwd=self.root)
        except subprocess.TimeoutExpired:
            return None, f"висит дольше {timeout} с", time.monotonic() - start
        return done.returncode, (done.stdout + done.stderr).strip(), time.monotonic() - start

    def hook(self, command: str, agent_id: str | None = None, boom: bool = False,
             timeout: int = 60) -> tuple[int | None, str, float]:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "proba-button-only"}
        if agent_id:
            payload["agent_id"] = agent_id
        argv = ["python3", "-c", HOOK_BOOM, str(GUARD_HOOK)] if boom else ["python3", str(GUARD_HOOK)]
        return self.run(argv, stdin=json.dumps(payload, ensure_ascii=False), timeout=timeout)

    def gate(self, *args: str) -> tuple[int | None, str, float]:
        return self.run(["python3", str(self.proj / "scripts" / "deploy_guard.py"), *args])


class Yard:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="proba-button-only-"))
        atexit.register(shutil.rmtree, self.root, True)
        self.counter = 0

    def stand(self, gate: str = "wrapper") -> Stand:
        self.counter += 1
        return Stand(self.root / f"s{self.counter}", gate)


# ── двери ────────────────────────────────────────────────────────────────────
class Door:
    """Одна необратимая дверь: как её позвать, что значит «открыто», её предмет и тексты."""
    name = kind = sick = revoke = ""
    ttl_min = TTL_MIN
    asks = True          # дверь-скрипт просит кнопку сама; хук-дверь не просит никогда
    waits_code = False   # вида кнопки на HEAD нет — случай «Да» ждёт код

    def question(self, st: Stand) -> tuple[bool, str]:
        return self.call(st)

    def call(self, st: Stand) -> tuple[bool, str]:
        raise NotImplementedError

    def subject(self, st: Stand) -> str:
        raise NotImplementedError

    def other(self, st: Stand) -> str:
        raise NotImplementedError


class DeployDoor(Door):
    name, kind = "выкат (deploy.sh)", "выкат"
    sick, revoke = "выкатывай, но сначала прогони гейты", "стоп, не выкатывай"

    def call(self, st):
        _rc, out, _ = st.run(["bash", str(st.proj / "scripts" / "deploy.sh")])
        return PAST_GATE in out, out

    def subject(self, st):
        return st.deploy_subject()

    def other(self, st):
        return st.deploy_subject(st.prod_old)


class PublishDoor(Door):
    name, kind, ttl_min = "публикация (publish.sh, замок 2)", "публикация", 60
    sick, revoke = "публикуй к вечеру", "стоп, не публикуй"

    def call(self, st):
        _rc, out, _ = st.run(["bash", str(PUBLISH)])
        return LOCK2 in out, out

    def subject(self, st):
        return st.publish_subject()

    def other(self, st):
        return st.publish_subject(st.pkg_old)


class PushDoor(Door):
    name, kind, asks = "пуш (guard_bash → deploy_guard --пуш)", "пуш", False
    sick, revoke = "запушь на гит к вечеру", "стоп, не пушь"

    def call(self, st):
        rc, out, _ = st.hook(f"git -C {st.work} push origin {st.push_head}:refs/heads/master")
        return rc == 0, out

    def subject(self, st):
        return st.push_subject()

    def other(self, st):
        return st.push_subject(st.push_other)


class ProtectedDeleteDoor(Door):
    name, kind = "удаление (protected-delete.py)", "удаление"
    sick, revoke = "удаляй к вечеру", "стоп, не удаляй этот файл"

    def question(self, st):
        rc, out, _ = st.run(["python3", str(PROTECTED_DELETE), str(st.target), "--спросить"])
        return False, out

    def call(self, st):
        rc, out, _ = st.run(["python3", str(PROTECTED_DELETE), str(st.target)])
        return rc == 0, out

    def subject(self, st):
        return os.path.realpath(st.target)

    def other(self, st):
        return os.path.realpath(st.other_target)


class RmGuardDoor(Door):
    name, kind, asks = "удаление (rm-сторож guard_bash)", "удаление", False
    revoke = "стоп, не удаляй этот файл"

    def __init__(self) -> None:
        self.sick = "удали {цель} к вечеру"

    def call(self, st):
        rc, out, _ = st.hook(f"HARNESS_DATA_DELETE_WHY='владелец велел убрать снимок' rm {st.target}")
        return rc == 0, out

    def subject(self, st):
        return os.path.realpath(st.target)

    def other(self, st):
        return os.path.realpath(st.other_target)


class DropDoor(Door):
    name, kind, waits_code = "снос базы (drop-database.sh)", "снос-базы", True
    sick, revoke = "да", "стоп, базу не удаляй"

    def call(self, st):
        before = st.db_calls.read_text(encoding="utf-8") if st.db_calls.exists() else ""
        _rc, out, _ = st.run(["bash", str(DROP_DB), DB])
        after = st.db_calls.read_text(encoding="utf-8") if st.db_calls.exists() else ""
        return "pg_dump" in after[len(before):], out

    def subject(self, st):
        return DB

    def other(self, st):
        return "proba_db2"


class RewriteDoor(Door):
    name, kind, ttl_min, waits_code = "перепись (publish.sh --переписать-историю)", "перепись", 60, True
    sick, revoke = "перепиши историю jig к вечеру", "стоп, отмени перепись истории"

    def call(self, st):
        rc, out, _ = st.run(["bash", str(PUBLISH), "--переписать-историю", str(st.copy), "--бэкап", str(st.bundle)])
        return rc == 0 and "отправлено одной атомарной отправкой" in out, out

    def subject(self, st):
        return st.rewrite_subject()[0]

    def other(self, st):
        return st.rewrite_subject()[0][:-12] + "000000000000"


DOORS = [DeployDoor(), PublishDoor(), PushDoor(), ProtectedDeleteDoor(), RmGuardDoor(), DropDoor(), RewriteDoor()]


def tag(door: Door) -> str:
    return " (ждёт код: вида кнопки на HEAD нет)" if door.waits_code else ""


def needs_subject_first(door: Door) -> bool:
    """Предмет переписи называет сама дверь (запрос ask-consent): сперва зов без согласия."""
    return isinstance(door, RewriteDoor)


def probe_door(yard: Yard, door: Door) -> None:
    print(f"— дверь: {door.name}")
    # (1) больной текст
    st = yard.stand()
    opened, out = door.question(st)
    if opened:
        verdict(f"{door.name}: 1) стенд — без слова дверь закрыта", False, "открыто без всякого согласия",
                short(out))
    next_second()
    sick = door.sick.format(цель=os.path.realpath(st.target))
    st.say(sick)
    opened, out = door.call(st)
    verdict(f"{door.name}: 1) больной «{sick}» → закрыто, в выводе «кнопк»",
            not opened and "кнопк" in out.lower(),
            "текст владельца ОТКРЫЛ дверь" if opened else "в отказе нет слова «кнопк»", short(out))
    got = st.requests(door.kind) if needs_subject_first(door) else st.requests(door.kind, door.subject(st))
    if door.asks:
        verdict(f"{door.name}: 1з) два зова → ровно 1 запрос «{door.kind}» на предмет двери", len(got) == 1,
                f"запросов {len(got)}")
    else:
        verdict(f"{door.name}: 1з) хук-дверь кнопку не просит → запросов 0", len(st.requests(door.kind)) == 0,
                f"запросов {len(st.requests(door.kind))}")

    # (2) «Да» кнопкой на предмет двери
    st = yard.stand()
    named = True
    if needs_subject_first(door):
        door.call(st)
        named = st.rewrite_subject()[1]
    st.yes(door.kind, door.subject(st))
    opened, out = door.call(st)
    live = verdict(f"{door.name}: 2) «Да» кнопкой на предмет → открыто{tag(door)}", opened,
                   "кнопка «Да» дверь не открыла" + ("" if named else "; дверь не назвала предмет запросом"),
                   short(out))
    if isinstance(door, RewriteDoor):
        verdict(f"{door.name}: 2п) предмет запроса по §5 «<адрес> refs/heads/master <до>..<новый> <12 hex>»"
                f"{tag(door)}",
                named and re.fullmatch(re.escape(st.url) + r" refs/heads/master [0-9a-f]{40}\.\.[0-9a-f]{40} "
                                       r"[0-9a-f]{12}", door.subject(st) or "") is not None
                and git("rev-parse", "refs/heads/master", cwd=st.copy) in door.subject(st),
                "дверь не назвала предмет" if not named else "предмет не по форме", door.subject(st)[:120])

    # (3) записей нет
    st = yard.stand()
    opened, out = door.call(st)
    verdict(f"{door.name}: 3) охранный: записей нет → закрыто", not opened, "открыто без записей", short(out))

    def leaning(case: str, setup, extra=None) -> None:
        """Случай 4-7: закрытие значит что-то, только если кнопка эту дверь вообще открывает (случай 2)."""
        st = yard.stand()
        if needs_subject_first(door):
            door.call(st)
        before = len(st.requests(door.kind))
        setup(st)
        opened, out = door.call(st)
        why = "нет опоры: «Да» на предмет дверь не открыл (случай 2)" if not live else "открыто"
        ok = live and not opened
        if extra and ok:
            ok, why = extra(st, out, before)
        verdict(f"{door.name}: {case}", ok, why, short(out))

    leaning(f"4) «Да» старше срока ({door.ttl_min} мин) → закрыто",
            lambda st: st.yes(door.kind, door.subject(st), ago_s=(door.ttl_min + 5) * 60))
    # Просьба на предмет двери была: закрыть обязана сверка предмета «Да», а не суд парной просьбы.
    leaning("5) «Да» на другой предмет → закрыто",
            lambda st: (st.request_row(door.kind, door.subject(st), 120), st.yes(door.kind, door.other(st))))

    def said_no(st, out, before):
        if "нажал „нет“ в" not in out.lower():
            return False, "в выводе нет «нажал „Нет“ в»"
        door.call(st)
        new = len(st.requests(door.kind)) - before
        return new == 0, f"после «Нет» дверь положила новых запросов: {new}"

    leaning("6) «Нет» кнопкой после «Да» → закрыто, «нажал „Нет“ в», повтор зова — новых запросов 0",
            lambda st: (st.yes(door.kind, door.subject(st), ago_s=180), st.no(door.kind, door.subject(st))),
            said_no)
    leaning(f"7) «Да», затем текст-отзыв «{door.revoke}» → закрыто",
            lambda st: (st.yes(door.kind, door.subject(st), ago_s=120), st.say(door.revoke, ago_s=60)))


# ── сквозные случаи ──────────────────────────────────────────────────────────
def probe_revoke_window(yard: Yard) -> None:
    print("— 7б) отзыв читается от нажатия, а не окном свежести (MJ3)")
    now = time.time()
    t = now - 68 * 60
    args = ["--действие", "публикация", "--только-согласие", "--публикация", None, "--свежесть-на",
            str(int(t + 10 * 60)), "--не-тратить"]
    st = yard.stand()
    args[4] = str(st.pkg)
    st.yes("публикация", st.publish_subject(), ago_s=now - t)
    rc, out, _ = st.gate(*args)
    live = verdict("7б) контроль: «Да» в T, суд в T+68 с --свежесть-на T+10 → открыто", rc == 0,
                   f"rc {rc}", short(out))
    st = yard.stand()
    args[4] = str(st.pkg)
    st.yes("публикация", st.publish_subject(), ago_s=now - t)
    st.say("стоп, не публикуй", ago_s=now - (t + 5 * 60))
    rc, out, _ = st.gate(*args)
    verdict("7б) «Да» в T, отзыв текстом в T+5, суд в T+68 с --свежесть-на T+10 → закрыто",
            live and rc not in (0, None), "нет опоры (контроль красный)" if not live else "отзыв не прочитан, открыто",
            short(out))


def probe_rewrite_repeat(yard: Yard) -> None:
    print("— 8) повторный force-push по тому же «Да» после успешной переписи (M5)")
    door = RewriteDoor()
    st = yard.stand()
    door.call(st)
    subject, named = st.rewrite_subject()
    st.yes("перепись", subject)
    first, out1 = door.call(st)
    rewritten = st.remote_refs() == git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads",
                                        "refs/tags", cwd=st.copy)
    st.new_bundle()
    second, out2 = door.call(st)
    verdict(f"8) перепись по «Да» прошла, повтор по тому же «Да» → закрыто{tag(door)}",
            first and rewritten and not second,
            ("первая перепись не открылась кнопкой" if not (first and rewritten) else "повтор ОТКРЫТ тем же «Да»"),
            short(out2 if first else out1))


def probe_rm_second(yard: Yard) -> None:
    print("— 9) rm-сторож: одно «Да» — одна попытка (m5)")
    door = RmGuardDoor()
    st = yard.stand()
    st.yes("удаление", door.subject(st))
    first, out1 = door.call(st)
    second, out2 = door.call(st)
    verdict("9) первое rm цели по «Да» пропущено, второе rm той же цели → закрыто", first and not second,
            "первое rm не открылось кнопкой" if not first else "второе rm пропущено тем же «Да»",
            short(out2 if first else out1))


def hook_limit() -> float | None:
    if HOOK_SETTINGS is None:
        return None
    data = json.loads(HOOK_SETTINGS.read_text(encoding="utf-8"))
    for groups in (data.get("hooks") or {}).values():
        for group in groups:
            for hook in group.get("hooks") or []:
                if "guard_bash.py" in hook.get("command", ""):
                    return float(hook.get("timeout") or 0) or None
    return None


def probe_gate_in_hook(yard: Yard) -> None:
    print("— 10) ворота в хуке: зависли, нет скрипта, упали → пуш ОТКАЗ (M1, MJ2)")
    limit = hook_limit()
    # Копия дерева прибора мутаций несёт только scripts/ и harness/, настроек хуков в ней нет:
    # там судится лишь отказ (rc 2), предел — в живой раскладке.
    if HOOK_SETTINGS is not None and not verdict(
            "10) предел хука guard_bash прочитан из настроек хуков", limit is not None,
            f"нет записи guard_bash в {HOOK_SETTINGS}"):
        return
    door = PushDoor()
    for gate, label in (("sleep", "ворота висят 30 с"), ("absent", "скрипта ворот нет"), ("crash", "ворота упали")):
        st = yard.stand(gate)
        st.yes("пуш", door.subject(st))
        rc, out, spent = st.hook(f"git -C {st.work} push origin {st.push_head}:refs/heads/master")
        fast = limit is None or spent < limit - 2
        verdict(f"10) {label}, «Да» на пуш есть → ОТКАЗ" + (f" быстрее {limit - 2:.0f} с" if limit else ""),
                rc == 2 and fast, f"rc {rc} за {spent:.1f} с" + ("" if fast else " — хук убили бы раньше"),
                f"{spent:.1f} с; {short(out, 160)}")


def probe_no_spend_no_ask(yard: Yard) -> None:
    print("— 11) ворота с --не-тратить кнопку не просят (M1)")
    st = yard.stand()
    rc, out, _ = st.gate("--действие", "deploy", "--только-согласие", "--не-тратить",
                         "--выкат", str(st.prod), git("rev-parse", "HEAD", cwd=st.prod))
    got = st.requests("выкат")
    verdict("11) охранный: --не-тратить без согласия → закрыто, запросов 0", rc != 0 and not got,
            f"rc {rc}, запросов {len(got)}", short(out))


def probe_ask_subject(yard: Yard) -> None:
    print("— 12) просьба: предмет двери сверяется с пересчётом (M3)")
    st = yard.stand()
    rc, out, _ = st.run(["python3", str(ASK), "--вид", "выкат", "--предмет-двери", st.deploy_subject(st.prod_old),
                         "--почему", "проба"])
    verdict("12) --предмет-двери ≠ пересчёт → код 2 «предмет двери», запроса нет",
            rc == 2 and "предмет двери" in out and not st.requests("выкат"),
            f"rc {rc}, запросов {len(st.requests('выкат'))}", short(out))


def probe_dirty(yard: Yard) -> None:
    print("— 13) грязное дерево выката")
    st = yard.stand()
    write(st.prod / "app.py", "print('правка')\n")
    rc, out, _ = st.gate("--действие", "deploy", "--только-согласие", "--не-тратить")
    verdict("13) правленое дерево → закрыто, отказ называет «не закоммичено»", rc != 0 and "не закоммичено" in out,
            f"rc {rc}", short(out))


def probe_helper(yard: Yard) -> None:
    print("— 15) помощник не просит кнопку (m14)")
    st = yard.stand()
    rc, out, _ = st.hook(f"python3 {ASK} --вид выкат --почему проба", agent_id="proba-helper")
    verdict("15) agent_id во входе хука + ask-consent.py → ОТКАЗ", rc == 2, f"rc {rc}", short(out))


def probe_deploy_and_push(yard: Yard) -> None:
    print("— 20) «выкат + пуш» одной командой: у пуша свой суд (ревью M1)")
    st = yard.stand()
    st.yes("выкат", st.deploy_subject())
    deploy = f"bash {st.proj}/scripts/deploy.sh"
    push = f"git -C {st.work} push origin {st.push_head}:refs/heads/master"
    rc, out, _ = st.hook(deploy)
    live = verdict("20) охранный: «Да» на выкат, один deploy.sh → хук пропустил", rc == 0, f"rc {rc}", short(out))
    # Пуш в составной команде предмета не получает (--пуш только у одного звена), поэтому и «Да» на пуш
    # её не открыло бы: судится, что отказ дал именно суд пуша, а не выката.
    for glue in (" && ", "; "):
        rc, out, _ = st.hook(deploy + glue + push)
        verdict(f"20) «Да» только на выкат, «deploy.sh{glue.rstrip()} git push» → ОТКАЗ судом пуша",
                live and rc == 2 and "на отправку в репозиторий" in out,
                "нет опоры (контроль красный)" if not live else f"rc {rc}: пуш прошёл без суда пуша", reason(out))


def probe_return_flag(yard: Yard) -> None:
    print("— 21) --вернуть-согласие — пустышка: без «Да» ворота закрыты (ревью M2)")

    def keys(st: Stand) -> dict:
        return {"выкат": ("deploy", ["--выкат", str(st.prod), git("rev-parse", "HEAD", cwd=st.prod)],
                          st.deploy_subject()),
                "пуш": ("push", ["--пуш", "origin", f"{st.push_head}:refs/heads/master", str(st.work)],
                        st.push_subject()),
                "публикация": ("публикация", ["--публикация", str(st.pkg)], st.publish_subject()),
                "снос-базы": ("dropdb", ["--база", DB], DB),
                "перепись": ("перепись", ["--перепись", str(st.copy)], None)}

    for kind in ("выкат", "пуш", "публикация", "снос-базы", "перепись"):
        st = yard.stand()
        action, extra, subject = keys(st)[kind]
        argv = ["--действие", action, *extra, "--только-согласие", "--вернуть-согласие"]
        rc, out, _ = st.gate(*argv)
        closed = rc not in (0, None)
        if subject is None:
            verdict(f"21) {kind}: «Да» нет, --вернуть-согласие → закрыто", closed, f"rc {rc}: открыто", short(out))
            continue
        st.yes(kind, subject)
        rc2, out2, _ = st.gate(*argv)
        verdict(f"21) {kind}: «Да» нет, --вернуть-согласие → закрыто; с «Да» на предмет → открыто",
                closed and rc2 == 0,
                f"rc {rc}: открыто без «Да»" if not closed else f"нет опоры: с «Да» rc {rc2}",
                short(out if not closed else out2))


def probe_helper_asks(yard: Yard) -> None:
    print("— 22) помощник не просит кнопку в обход: флаги интерпретатора, --просить, --сухо --спросить (ревью m2)")
    st = yard.stand()
    helper = "proba-helper"
    for label, command in (
            ("python3 -u ask-consent.py", f"python3 -u {ASK} --вид выкат --почему проба"),
            ("deploy_guard.py --просить",
             f"python3 {DEPLOY_GUARD} --действие deploy --только-согласие --просить")):
        rc, out, _ = st.hook(command, agent_id=helper)
        verdict(f"22) помощник: {label} → ОТКАЗ «помощник зовёт»", rc == 2 and "помощник зовёт" in out,
                f"rc {rc}: пропущено" if rc != 2 else "отказ не про помощника", reason(out))
    rc, out, _ = st.hook(f"python3 {DEPLOY_GUARD} --действие deploy --только-согласие --не-тратить", agent_id=helper)
    verdict("22) контроль: помощник, deploy_guard.py без --просить → хук пропустил", rc == 0, f"rc {rc}", short(out))
    rc, out, _ = st.hook(f"python3 {PROTECTED_DELETE} {st.target} --сухо --спросить", agent_id=helper)
    _rc, out2, _ = st.run(["python3", str(PROTECTED_DELETE), str(st.target), "--сухо", "--спросить"])
    got = st.requests("удаление")
    # Хук сухой прогон помощнику пропускает (он безвреден), поэтому держит сама дверь: --сухо сильнее --спросить.
    verdict("22) protected-delete --сухо --спросить вне хука → запроса нет", not got,
            f"запросов {len(got)} (хук помощнику: rc {rc})", short(out2))
    st = yard.stand()
    st.run(["python3", str(PROTECTED_DELETE), str(st.target), "--спросить"])
    got = st.requests("удаление")
    verdict("22) контроль: protected-delete --спросить без --сухо → ровно 1 запрос", len(got) == 1,
            f"запросов {len(got)}")


def probe_drops_journal(yard: Yard) -> None:
    print("— 23) след сносов db-drops.jsonl — носитель погашения: правка из сессии закрыта (ревью m4)")
    st = yard.stand()
    drops = write(st.log / owner_word.ЖУРНАЛ_СНОСОВ, json.dumps({"база": DB}) + "\n")
    other = write(st.log / "proba-free.jsonl", "{}\n")
    rc, out, _ = st.hook(f"truncate -s 0 {other}")
    live = verdict("23) контроль: усечение соседнего журнала LOG_DIR → хук пропустил", rc == 0, f"rc {rc}",
                   short(out))
    for label, command in (("усечение", f"truncate -s 0 {drops}"), ("вырезка строки", f"sed -i 1d {drops}"),
                           ("перезапись", f"echo '{{}}' > {drops}")):
        rc, out, _ = st.hook(command)
        verdict(f"23) {label} db-drops.jsonl → ОТКАЗ", live and rc == 2,
                "нет опоры (контроль красный)" if not live else f"rc {rc}: пропущено", reason(out))


def probe_parallel_ask(yard: Yard) -> None:
    print("— 16) два параллельных ask-consent одного предмета (m13)")
    st = yard.stand()
    results: list = []

    def ask() -> None:
        results.append(st.run(["python3", str(ASK), "--вид", "выкат", "--почему", "проба"]))

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    got = st.requests("выкат", st.deploy_subject())
    verdict("16) два параллельных ask-consent → ровно 1 запрос", len(got) == 1, f"запросов {len(got)}",
            " || ".join(short(r[1], 100) for r in results))


def probe_env_keys(yard: Yard) -> None:
    print("— 18) ключи согласия и стенд из окружения команды (MJ1)")
    door = DeployDoor()
    st = yard.stand()
    loose = write(st.root / "loose.conf", st.harness_text(DEPLOY_NEEDS_OWNER="0", DEPLOY_CONSENT_STANDING="1"))
    rc, out, _ = st.hook(f"HARNESS_CONF={loose} bash {st.proj}/scripts/deploy.sh")
    verdict("18а) охранный: HARNESS_CONF команды с DEPLOY_NEEDS_OWNER=0, кнопки нет → хук закрыл выкат",
            rc == 2, f"rc {rc}", short(out))

    st = yard.stand()
    st.yes("выкат", door.subject(st))
    rc, out, _ = st.hook(f"bash {st.proj}/scripts/deploy.sh")
    live = verdict("18в) охранный: «Да» на выкат, чистая команда deploy.sh → хук пропустил", rc == 0,
                   f"rc {rc}", short(out))
    fake = st.root / "fake-log"
    fake.mkdir()
    for journal in ("consent-buttons.jsonl", "consent-requests.jsonl"):
        shutil.copy2(st.log / journal, fake / journal)
    rc, out, _ = st.hook(f"HARNESS_OWNER_WORD_STEND=1 HARNESS_LOG_DIR={fake} bash {st.proj}/scripts/deploy.sh")
    verdict("18б) команда с HARNESS_OWNER_WORD_STEND=1 и поддельным журналом → хук отказал",
            live and rc == 2, "нет опоры (18в красный)" if not live else f"rc {rc}: пропущено", short(out))


def probe_judge_crash(yard: Yard) -> None:
    print("— 19) исключение в судье хука на необратимом (MJ2)")
    st = yard.stand()
    rc, out, _ = st.hook(f"HARNESS_DATA_DELETE_WHY='владелец велел убрать снимок' rm {st.target}", boom=True)
    verdict("19) подставное исключение в судье → rm секрета ОТКАЗ", rc == 2, f"rc {rc}: сторож пропустил",
            short(out))


def probe_forged_yes(yard: Yard) -> None:
    print("— 24) подделка «Да» в журнале кнопок: отправитель и парная просьба (безопасность F1)")

    def judged(setup) -> tuple[int | None, str]:
        st = yard.stand()
        setup(st)
        return st.gate("--действие", "deploy", "--выкат", str(st.prod), git("rev-parse", "HEAD", cwd=st.prod),
                       "--только-согласие", "--не-тратить")

    rc, out = judged(lambda st: st.yes("выкат", st.deploy_subject()))[:2]
    live = verdict("24) контроль: «Да» владельца после просьбы → открыто", rc == 0, f"rc {rc}", short(out))
    for label, setup in (
            ("«Да» без from_id", lambda st: st.button(YES, "выкат", st.deploy_subject(), 60, sender=None)),
            ("«Да» с чужим from_id", lambda st: st.button(YES, "выкат", st.deploy_subject(), 60, sender=OWNER + 1)),
            ("«Да» без парной просьбы", lambda st: st.button(YES, "выкат", st.deploy_subject(), 60, asked_ago=-1)),
            ("«Да» с просьбой ПОСЛЕ кнопки",
             lambda st: st.button(YES, "выкат", st.deploy_subject(), 60, asked_ago=30)),
            ("«Да» с просьбой на другой предмет",
             lambda st: (st.request_row("выкат", st.deploy_subject(st.prod_old), 120),
                         st.button(YES, "выкат", st.deploy_subject(), 60, asked_ago=-1)))):
        rc, out = judged(setup)[:2]
        verdict(f"24) {label} → закрыто", live and rc not in (0, None),
                "нет опоры (контроль красный)" if not live else f"rc {rc}: подделка открыла", short(out))
    rc, out = judged(lambda st: (st.yes("выкат", st.deploy_subject(), ago_s=180),
                                 st.button(NO, "выкат", st.deploy_subject(), 60, sender=None, asked_ago=-1)))[:2]
    verdict("24) «Нет» без from_id и без просьбы после «Да» → закрыто «нажал „Нет“»",
            live and rc not in (0, None) and "нажал „нет“" in out.lower(),
            "нет опоры (контроль красный)" if not live else f"rc {rc}: «Нет» без пары не закрыло", short(out))


def probe_journal_append(yard: Yard) -> None:
    print("— 25) дописка в журналы согласия по куску имени: переменная, python (безопасность F1)")
    st = yard.stand()
    buttons = st.log / "consent-buttons.jsonl"
    write(buttons, "")
    row = json.dumps({"v": 1, "event": YES, "data": {"from_id": OWNER}})
    for want, label, command in (
            (2, "echo >> \"$HARNESS_LOG_DIR/consent-buttons.jsonl\"",
             f"echo '{row}' >> \"$HARNESS_LOG_DIR/consent-buttons.jsonl\""),
            (2, "D=…; tee -a \"$D\"/consent-buttons.jsonl", f"D={st.log}; echo x | tee -a \"$D\"/consent-buttons.jsonl"),
            (2, "echo >> ${LOG_DIR}/consent-requests.jsonl", "echo x >> ${LOG_DIR}/consent-requests.jsonl"),
            (2, "python3 -c open(…/consent-buttons.jsonl, 'a')",
             f"python3 -c \"open('{buttons}', 'a').write('x')\""),
            (2, "python3 -c open(os.environ[…] + '/consent-buttons.jsonl', 'a')",
             "python3 -c \"import os; open(os.environ['LOG_DIR'] + '/consent-buttons.jsonl', 'a').write('x')\""),
            (0, "cat журнала кнопок", f"cat {buttons}"),
            (0, "grep по журналу кнопок", f"grep consent.yes {buttons}"),
            (0, "tail журнала кнопок", f"tail -n 5 {buttons}")):
        rc, out, _ = st.hook(command)
        verdict(f"25) {label} → {'ОТКАЗ' if want else 'пропущено'}", rc == want, f"rc {rc}", reason(out))


def probe_guard_import_crash(yard: Yard) -> None:
    print("— 26) копия сторожа со сломанным импортом судьи: двери закрыты, прочее идёт (безопасность F2)")
    st = yard.stand()
    copy = st.root / "guard-copy"
    shutil.copytree(LIB, copy / "lib")
    write(copy / "lib" / "owner_word.py", 'raise ImportError("проба: судья согласия сломан")\n')
    (copy / "hooks").mkdir()
    guard = Path(shutil.copy2(GUARD_HOOK, copy / "hooks" / "guard_bash.py"))
    for want, command in ((2, "git push origin master"), (2, "./scripts/deploy.sh"), (0, "ls")):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "proba-button-only"}
        rc, out, _ = st.run(["python3", str(guard)], stdin=json.dumps(payload, ensure_ascii=False), timeout=60)
        verdict(f"26) импорт owner_word упал: «{command}» → rc {want}", rc == want, f"rc {rc}", reason(out))


# ── один судья (AST) ─────────────────────────────────────────────────────────
JUDGE_FILES = {"выкат": [DEPLOY_GUARD], "пуш": [DEPLOY_GUARD], "публикация": [DEPLOY_GUARD],
               "снос-базы": [DEPLOY_GUARD], "перепись": [DEPLOY_GUARD],
               "удаление": [PROTECTED_DELETE, GUARD_HOOK]}
TEXT_NAMES = {"это_голое_согласие", "это_разрешение_цели", "_слова_владельца", "_согласие", "_приказ_переписи"}
REVERSIBLE = {"это_разрешение_перезапуска", "это_разрешение_чтения"}


def consent_kinds() -> tuple:
    tree = ast.parse((LIB / "owner_word.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ВИДЫ_СОГЛАСИЯ"
                                                for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    return ()


def name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def probe_one_judge() -> None:
    print("— один судья (AST, spec §8 m9)")
    kinds = set(consent_kinds()) - {"спека"}
    verdict("AST) таблица вид → файл = owner_word.ВИДЫ_СОГЛАСИЯ без «спеки»", bool(kinds) and kinds == set(JUDGE_FILES),
            f"ВИДЫ_СОГЛАСИЯ−спека = {sorted(kinds)}, таблица = {sorted(JUDGE_FILES)}")
    for path in sorted({p for files in JUDGE_FILES.values() for p in files}):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        bad_calls = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            found = name_of(node)
            if found:
                names.add(found)
            if isinstance(node, ast.Call) and name_of(node.func) == "разрешение":
                sign = node.args[2] if len(node.args) > 2 else next(
                    (k.value for k in node.keywords if k.arg not in ("окно_мин", "каталог_логов")), None)
                if name_of(sign) not in REVERSIBLE:
                    bad_calls.append(node.lineno)
        doors = sorted(k for k, files in JUDGE_FILES.items() if path in files)
        verdict(f"AST) {path.name}: двери {doors} спрашивают судить_кнопкой", "судить_кнопкой" in names,
                "в файле двери нет судить_кнопкой")
        text = sorted(names & TEXT_NAMES)
        verdict(f"AST) {path.name}: текстовых имён ({', '.join(sorted(TEXT_NAMES))}) нет", not text,
                f"остались: {text}")
        verdict(f"AST) {path.name}: разрешение() только с признаком перезапуска/чтения", not bad_calls,
                f"строки {bad_calls}")
    for path in (DEPLOY_SH, PUBLISH, DROP_DB):
        code = [line for line in path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
                if not line.lstrip().startswith("#")]
        asks = any("deploy_guard.py" in line and "--просить" in line for line in code)
        reads = [line.strip()[:80] for line in code if "confirmations.jsonl" in line]
        verdict(f"AST) {path.name}: зов deploy_guard с --просить, confirmations.jsonl не читается",
                asks and not reads, ("нет зова с --просить" if not asks else "") + (f"; {reads[:2]}" if reads else ""))


def live_requests() -> tuple[int, float] | None:
    base = owner_word.боевой_каталог_логов()
    if base is None:
        return None
    journal = base / "consent-requests.jsonl"
    try:
        return len(journal.read_text(encoding="utf-8").splitlines()), journal.stat().st_mtime
    except FileNotFoundError:
        return 0, 0.0


def main() -> int:
    print(f"=== test_consent_button_only: механизмы из {SCRIPTS}, хук {GUARD_HOOK} ===")
    print(f"начало {iso(time.time())}")
    before = live_requests()
    yard = Yard()
    for door in DOORS:
        probe_door(yard, door)
    probe_revoke_window(yard)
    probe_rewrite_repeat(yard)
    probe_rm_second(yard)
    probe_gate_in_hook(yard)
    probe_no_spend_no_ask(yard)
    probe_ask_subject(yard)
    probe_dirty(yard)
    probe_helper(yard)
    probe_parallel_ask(yard)
    probe_deploy_and_push(yard)
    probe_return_flag(yard)
    probe_helper_asks(yard)
    probe_drops_journal(yard)
    probe_env_keys(yard)
    probe_judge_crash(yard)
    probe_forged_yes(yard)
    probe_journal_append(yard)
    probe_guard_import_crash(yard)
    probe_one_judge()
    after = live_requests()
    verdict("17) машинный consent-requests.jsonl не изменился (строки и mtime)", before is not None and before == after,
            f"до {before}, после {after}")
    print(f"конец {iso(time.time())}")
    print(f"ИТОГО: путей {PATHS}, неудач {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
