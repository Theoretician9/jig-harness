#!/usr/bin/env python3
"""Проба проверки «поднялся» после перезагрузки в окне ротации — до кода.

Карточка hr.reboot-in-rotation-window, спека
docs/specs/2026-11-02-перезагрузка-в-окне-ротации.md §3.1, §6.1, §6.2. Владелец
02.11 (входящее 10563): «А после перезагрузки сервера как ты поднимешься? Tmux
и все остальное предусмотрел?» — ответ должен быть проверкой по перечню
ДАННЫХ, а не словами.

`scripts/boot_check.py` гоняется на стенде: свои паспорт и конфиг, свой
PANEL_STATE_DIR с меткой (HARNESS_PANEL_STATE), свой каталог транскриптов,
свой HTTP на своём порту, заглушки systemctl/tmux/docker/uname/sudo/reboot/
shutdown в PATH. Настоящие юниты не спрашиваются и не трогаются; заглушка
systemctl пишет каждый зов, и проба требует, чтобы звали только `is-active`.

У agent полный sudo без пароля: ПЕРВЫМ делом REBOOT_CMD ставится на заглушку во
временном каталоге и сверяется — иначе отказ до любого прогона.

    python3 scripts/test_boot_check.py

Двери мутаций ([[probe-loads-the-live-mechanism]]):
    HARNESS_BOOT_CHECK        путь boot_check.py под пробой
    HARNESS_BOOT_CHECK_DATA   путь настоящего boot-check.yaml (раздел «данные»)
    HARNESS_BOOT_CHECK_CONF   путь harness.conf репозитория (раздел «данные»)
    HARNESS_SYSTEM_JOBS       путь system-jobs.yaml (ключи для новых установок)
Последняя строка: `boot_check: случаев N, падений F`.
"""
from __future__ import annotations

import datetime
import fcntl
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

# ── заглушка команды перезагрузки — до всего остального ─────────────────────
GUARD_DIR = Path(tempfile.mkdtemp(prefix="boot-check-guard-"))
REBOOT_STUB = GUARD_DIR / "reboot-stub.sh"
REBOOT_STUB.write_text(f'#!/bin/sh\necho "$*" >> "{GUARD_DIR}/reboot.calls"\nexit 1\n', encoding="utf-8")
REBOOT_STUB.chmod(0o755)
os.environ["REBOOT_CMD"] = str(REBOOT_STUB)


def stub_guard(value: str) -> None:
    """Действующее REBOOT_CMD — заглушка во временном каталоге пробы, иначе отказ."""
    head = value.split(" ")[0]
    if not (head.startswith(str(GUARD_DIR) + "/") and os.access(head, os.X_OK)):
        print(f"ОТКАЗ: REBOOT_CMD «{value}» не заглушка во временном каталоге пробы — ничего не запускаю")
        sys.exit(2)


stub_guard(os.environ["REBOOT_CMD"])

import yaml  # noqa: E402
from config import из_файла  # noqa: E402
from selftest_cases import Cases  # noqa: E402

BOOT_CHECK = Path(os.environ.get("HARNESS_BOOT_CHECK") or HERE / "boot_check.py")


def default_data() -> Path:
    """harness/config в проекте, config/ рядом со scripts/ в пакете."""
    for candidate in (HERE.parent / "harness" / "config" / "boot-check.yaml",
                      HERE.parent / "config" / "boot-check.yaml"):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "config" / "boot-check.yaml"


def default_conf() -> Path:
    for candidate in (HERE.parent / "harness" / "config" / "harness.conf",
                      HERE.parent / "config" / "harness.conf"):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "config" / "harness.conf"


DATA = Path(os.environ.get("HARNESS_BOOT_CHECK_DATA") or default_data())
CONF = Path(os.environ.get("HARNESS_BOOT_CHECK_CONF") or default_conf())
SYSTEM_JOBS = Path(os.environ.get("HARNESS_SYSTEM_JOBS") or CONF.parent / "system-jobs.yaml")
# Пакет публичен: слова этой машины в его данных — чужой установке чужие соседи.
OWNER_WORDS = ("other-app", "neighbour-1")
SITE_KEYS = {"сайт": "SITE_HEALTH_URL", "сайт — форма заявки": "SITE_FORM_URL"}
KINDS = {"юнит", "tmux", "docker", "http", "команда", "ядро", "ответ_сессии"}
BUSY_KINDS = {"замок", "процесс", "процесс_по_строке"}

cases = Cases()
check = cases.check

# Заглушки читают своё поведение из файлов стенда: случай меняет файл, а не код.
STUB_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$STUB/systemctl.calls"
if [ "$1" = show ]; then
    if grep -qx "${5}" "$STUB/notfound" 2>/dev/null; then echo not-found; else echo loaded; fi
    exit 0
fi
[ "$1" = is-active ] || exit 1
if grep -qx "$2" "$STUB/notfound" 2>/dev/null; then echo inactive; exit 3; fi
if grep -qx "$2" "$STUB/inactive" 2>/dev/null; then echo inactive; exit 3; fi
echo active
"""
# Сессия есть только под ТОЧНЫМ именем: «-t proba-agent» без «=» tmux принял
# бы и «proba-agent-старый» (B-17) — заглушка отвечает на такой зов «нет».
STUB_TMUX = """#!/bin/sh
echo "$*" >> "$STUB/tmux.calls"
case "$*" in *"=proba-agent"*) ;; *) exit 1 ;; esac
exit "$(cat "$STUB/tmux-rc" 2>/dev/null || echo 0)"
"""
STUB_DOCKER = """#!/bin/sh
case "$1" in
    ps) awk '{print NR}' "$STUB/containers" ;;
    inspect) cat "$STUB/containers" ;;
    *) exit 1 ;;
esac
"""
STUB_UNAME = """#!/bin/sh
case "$1" in -r) cat "$STUB/kernel" ;; *) echo Linux ;; esac
"""
STUB_FORBIDDEN = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB/forbidden.calls"
exit 1
"""

GOOD_LIST = {
    "проверки": [
        {"имя": "агент", "вид": "юнит", "юнит": "harness-agent",
         "дверь": "sudo systemctl restart harness-agent"},
        # Юнит, которого нет ни в каком коде: если его спросили — перечень читается
        # из данных, а не зашит.
        {"имя": "проба-юнит-из-данных", "вид": "юнит", "юнит": "proba-unit-from-data",
         "дверь": "дверь-пробы-42"},
        {"имя": "сессия агента", "вид": "tmux"},
        {"имя": "контейнеры", "вид": "docker", "не_меньше": 2},
        {"имя": "панель по сети", "вид": "http", "ключ": "PANEL_LOCAL"},
        {"имя": "продукт", "вид": "http", "ключ": "PRODUCT_HEALTH_URL"},
        {"имя": "срок входа", "вид": "команда",
         "argv": [sys.executable, "-c", "print('вход годен ещё 20 дн')"]},
        {"имя": "ядро", "вид": "ядро"},
        {"имя": "ответ сессии", "вид": "ответ_сессии", "ждать_сек": 1},
    ]
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — имя задано http.server
        self.send_response({"/ok": 200, "/405": 405}.get(self.path, 500))
        self.end_headers()
        self.wfile.write(b"proba")

    def log_message(self, *args):
        return


def iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class Stand:
    """Свой мир одного случая."""

    def __init__(self, base: Path, port: int):
        self.base = base
        self.stub = base / "stub"
        self.bin = base / "bin"
        self.project = base / "project"
        self.state = base / "panel-state"
        self.log = base / "log"
        self.transcripts = base / "transcripts"
        for d in (self.stub, self.bin, self.project, self.state, self.log, self.transcripts, base / "home"):
            d.mkdir(parents=True, exist_ok=True)
        for name, text in (("systemctl", STUB_SYSTEMCTL), ("tmux", STUB_TMUX), ("docker", STUB_DOCKER),
                           ("uname", STUB_UNAME), ("sudo", STUB_FORBIDDEN), ("reboot", STUB_FORBIDDEN),
                           ("shutdown", STUB_FORBIDDEN), ("poweroff", STUB_FORBIDDEN), ("halt", STUB_FORBIDDEN)):
            path = self.bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
        (self.stub / "kernel").write_text("6.8.0-147-generic\n", encoding="utf-8")
        (self.stub / "containers").write_text(
            "/proba-pg unless-stopped true\n/proba-redis always true\n/proba-once no false\n", encoding="utf-8")
        self.boot_time = int(time.time()) - 3600
        self.shift = self.transcripts / "smena.jsonl"
        self.write_shift([("user", 10), ("assistant", 20)])
        (base / "install.conf").write_text(
            f'PROJECT_DIR="{self.project}"\nLOG_DIR="{self.log}"\nSTATE_DIR="{base / "no-state"}"\n'
            f'PANEL_STATE_DIR="{self.state}"\nTMUX_SESSION="proba-agent"\n', encoding="utf-8")
        self.write_conf(f"http://127.0.0.1:{port}/ok")
        self.list = base / "boot-check.yaml"
        self.write_list(GOOD_LIST)
        self.marker = self.state / "reboot-started.json"
        self.last = self.state / "reboot-last.json"

    def write_conf(self, panel: str) -> None:
        (self.base / "harness.conf").write_text(
            f'PANEL_LOCAL="{panel}"\nPRODUCT_HEALTH_URL=""\nREBOOT_LATE_MIN=15\n'
            f'REBOOT_CHECK_TIMEOUT_SEC=600\nREBOOT_CMD="{REBOOT_STUB}"\n', encoding="utf-8")

    def write_list(self, data) -> None:
        self.list.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def write_transcript(self, path: Path, records, entrypoint: str = "cli") -> None:
        """records — (тип, секунд после загрузки[, лишние поля]); пинок — user."""
        lines = []
        for item in records:
            kind, after = item[0], item[1]
            extra = item[2] if len(item) > 2 else {}
            record = {"type": kind, "timestamp": iso(self.boot_time + after), "entrypoint": entrypoint,
                      "sessionId": path.stem, "message": {"role": kind, "content": "проба"}}
            record.update(extra)
            lines.append(json.dumps(record, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_shift(self, records) -> None:
        self.write_transcript(self.shift, records)

    def put_marker(self, ago_boot: int = -300, expected: str = "6.8.0-147",
                   before: str = "6.8.0-138-generic") -> None:
        """ago_boot < 0 — метка ДО загрузки (перезагрузка была), > 0 — после."""
        self.marker.write_text(json.dumps({
            "с": self.boot_time + ago_boot, "сессия": "proba-sessiya", "причина": "окно ≈80% ≥ 75%",
            "ядро_было": before, "ядро_ожидаемое": expected}, ensure_ascii=False), encoding="utf-8")

    def env(self) -> dict:
        env = dict(os.environ)
        for key in ("CLAUDE_CODE_SESSION_ID", "PANEL_LOCAL", "PRODUCT_HEALTH_URL", "HARNESS_LIVE_CHECK_TIMEOUT"):
            env.pop(key, None)
        env.update({
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "STUB": str(self.stub),
            "HARNESS_HOME": str(self.base / "home"),
            "HARNESS_INSTALL_CONF": str(self.base / "install.conf"),
            "HARNESS_CONF": str(self.base / "harness.conf"),
            "PROJECT_DIR": str(self.project), "LOG_DIR": str(self.log),
            "STATE_DIR": str(self.base / "no-state"),
            "HARNESS_PANEL_STATE": str(self.state), "PANEL_STATE_DIR": str(self.state),
            "HARNESS_BOOT_CHECK_TRANSCRIPTS": str(self.transcripts),
            "HARNESS_BOOT_CHECK_BOOT_TIME": str(self.boot_time),
            "REBOOT_CMD": str(REBOOT_STUB), "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

    def run(self, with_marker: bool = True, extra: list[str] | None = None,
            env_extra: dict | None = None) -> tuple[int, list[str]]:
        env = self.env()
        env.update(env_extra or {})
        argv = [sys.executable, str(BOOT_CHECK), "--перечень", str(self.list)]
        if with_marker:
            argv += ["--метка", str(self.marker)]
        argv += extra or []
        try:
            done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90,
                                  cwd=str(self.base))
            return done.returncode, (done.stdout + done.stderr).strip().splitlines()
        except subprocess.TimeoutExpired:
            return -9, ["предел 90 с истёк"]

    def last_record(self) -> dict:
        try:
            return json.loads(self.last.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def calls(self, name: str) -> list[str]:
        path = self.stub / f"{name}.calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def line_of(lines: list[str], name: str) -> str:
    return next((line for line in lines if line.startswith(f"{name}:")), "строки нет")


def tail(lines: list[str]) -> str:
    return lines[-1] if lines else "вывода нет"


def section_green(tmp: Path, port: int) -> None:
    print("── ВСЁ ПОДНЯЛОСЬ: итог «поднялся», метка снята, итог записан ──")
    st = Stand(tmp / "green", port)
    st.put_marker()
    rc, lines = st.run()
    check("код 0 — красных нет", 0, rc)
    check("последняя строка — «поднялся: …»", True, tail(lines).startswith("поднялся:"))
    check("юнит агента — ок", True, line_of(lines, "агент").startswith("агент: ок"))
    check("юнит из ДАННЫХ спрошен у systemctl (перечень не зашит в код)", True,
          "is-active proba-unit-from-data" in st.calls("systemctl"))
    check("tmux спрошен о ТОЧНОМ имени сессии из конфига: has-session -t =proba-agent (B-17)", True,
          any("has-session" in c and "=proba-agent" in c for c in st.calls("tmux")))
    check("сессия агента — ок", True, line_of(lines, "сессия агента").startswith("сессия агента: ок"))
    check("контейнеры — ок (двое с автоподъёмом бегут, «no» не судится)", True,
          line_of(lines, "контейнеры").startswith("контейнеры: ок"))
    check("панель по сети — ок на своём порту", True,
          line_of(lines, "панель по сети").startswith("панель по сети: ок"))
    product = line_of(lines, "продукт")
    check("пустой PRODUCT_HEALTH_URL — «не проверено», не «ок»", True,
          product.startswith("продукт: не проверено"))
    check("«не проверено» называет пустой ключ", True, "PRODUCT_HEALTH_URL" in product)
    check("итог называет непроверенное (честность И-4)", True,
          "не проверено" in tail(lines) and "продукт" in tail(lines))
    check("команда — ок с последней строкой вывода", True, "вход годен" in line_of(lines, "срок входа"))
    check("ядро — ок (6.8.0-147 как ждали)", True, line_of(lines, "ядро").startswith("ядро: ок"))
    check("ответ сессии — ок (ответ модели после пинка)", True,
          line_of(lines, "ответ сессии").startswith("ответ сессии: ок"))
    check("метка снята (следующая загрузка — уже не «в окне»)", False, st.marker.exists())
    record = st.last_record()
    check("reboot-last.json рядом с меткой (PANEL_STATE_DIR): итог «поднялся»", "поднялся", record.get("итог"))
    check("reboot-last.json: ядро сменилось", True, record.get("ядро_сменилось"))
    check("reboot-last.json: поля метки сохранены", "6.8.0-147", record.get("ядро_ожидаемое"))
    check("reboot-last.json: время проверки — число", True, isinstance(record.get("проверено"), int))
    check("в STATE_DIR ничего не писалось (B-1)", False, (st.base / "no-state").exists())
    check("systemctl звали только is-active и show LoadState (ничего не поднимает и не гасит)", [],
          [c for c in st.calls("systemctl") if not c.startswith(("is-active ", "show -p LoadState "))])
    check("sudo/reboot/shutdown не звались", [], st.calls("forbidden"))


def section_red_unit(tmp: Path, port: int) -> None:
    print("── ЮНИТ ИЗ ДАННЫХ НЕ ПОДНЯЛСЯ: КРАСНЫЙ с дверью ──")
    st = Stand(tmp / "red-unit", port)
    st.put_marker()
    (st.stub / "inactive").write_text("proba-unit-from-data\n", encoding="utf-8")
    rc, lines = st.run()
    check("код 1 — есть красный", 1, rc)
    red = line_of(lines, "проба-юнит-из-данных")
    check("строка юнита — КРАСНЫЙ", True, red.startswith("проба-юнит-из-данных: КРАСНЫЙ"))
    check("красная строка несёт дверь из данных", True, "дверь-пробы-42" in red)
    check("итог — «НЕ ПОДНЯЛСЯ» с именем красного", True,
          tail(lines).startswith("НЕ ПОДНЯЛСЯ") and "проба-юнит-из-данных" in tail(lines))
    check("reboot-last.json: итог «красный»", "красный", st.last_record().get("итог"))
    check("метка снята и при красном", False, st.marker.exists())
    check("красный юнит не поднимали (только is-active и show)", [],
          [c for c in st.calls("systemctl") if not c.startswith(("is-active ", "show -p LoadState "))])


def section_unit_not_installed(tmp: Path, port: int) -> None:
    print("── ЮНИТА НЕТ НА УСТАНОВКЕ: необязательный — «не установлен», свой — КРАСНЫЙ ──")
    st = Stand(tmp / "unit-not-found", port)
    st.put_marker()
    optional = json.loads(json.dumps(GOOD_LIST))
    optional["проверки"][1]["необязательный"] = True
    st.write_list(optional)
    (st.stub / "notfound").write_text("proba-unit-from-data\n", encoding="utf-8")
    rc, lines = st.run()
    row = line_of(lines, "проба-юнит-из-данных")
    check("необязательный not-found юнит — не красный (код 0)", 0, rc)
    check("строка необязательного not-found юнита — «не установлен»", True,
          "не установлен" in row and "КРАСНЫЙ" not in row)
    # Раскладка не поставила агента — это не «поднялся».
    st = Stand(tmp / "own-unit-not-found", port)
    st.put_marker()
    (st.stub / "notfound").write_text("harness-agent\n", encoding="utf-8")
    rc, lines = st.run()
    check("свой not-found юнит (без признака) — КРАСНЫЙ, код 1", (1, True),
          (rc, line_of(lines, "агент").startswith("агент: КРАСНЫЙ")))
    # Установленный и остановленный по-прежнему красный: рядом, в другом стенде.
    st = Stand(tmp / "unit-inactive", port)
    st.put_marker()
    (st.stub / "inactive").write_text("proba-unit-from-data\n", encoding="utf-8")
    rc, lines = st.run()
    check("установленный inactive юнит — КРАСНЫЙ, код 1", (1, True),
          (rc, line_of(lines, "проба-юнит-из-данных").startswith("проба-юнит-из-данных: КРАСНЫЙ")))


def section_red_kinds(tmp: Path, port: int) -> None:
    print("── КАЖДЫЙ ВИД УМЕЕТ КРАСНЕТЬ ──")
    st = Stand(tmp / "no-tmux", port)
    st.put_marker()
    (st.stub / "tmux-rc").write_text("1\n", encoding="utf-8")
    rc, lines = st.run()
    check("tmux-сессии нет — КРАСНЫЙ, код 1", (1, True),
          (rc, line_of(lines, "сессия агента").startswith("сессия агента: КРАСНЫЙ")))

    st = Stand(tmp / "pg-down", port)
    st.put_marker()
    (st.stub / "containers").write_text(
        "/proba-pg unless-stopped false\n/proba-redis always true\n", encoding="utf-8")
    rc, lines = st.run()
    docker = line_of(lines, "контейнеры")
    check("контейнер unless-stopped стоит — КРАСНЫЙ, код 1", (1, True), (rc, docker.startswith("контейнеры: КРАСНЫЙ")))
    check("красный называет стоящий контейнер", True, "proba-pg" in docker)

    st = Stand(tmp / "too-few", port)
    st.put_marker()
    (st.stub / "containers").write_text("/proba-pg unless-stopped true\n/proba-once no false\n", encoding="utf-8")
    rc, lines = st.run()
    check("с автоподъёмом бежит 1 при «не_меньше: 2» — КРАСНЫЙ", (1, True),
          (rc, line_of(lines, "контейнеры").startswith("контейнеры: КРАСНЫЙ")))

    st = Stand(tmp / "old-kernel", port)
    st.put_marker()
    (st.stub / "kernel").write_text("6.8.0-138-generic\n", encoding="utf-8")
    rc, lines = st.run()
    check("ядро осталось старым — КРАСНЫЙ, код 1", (1, True), (rc, line_of(lines, "ядро").startswith("ядро: КРАСНЫЙ")))
    check("reboot-last.json: ядро не сменилось (защита от петли)", False, st.last_record().get("ядро_сменилось"))

    # Нужна была только по reboot-required: ожидаемое = было (B-15).
    st = Stand(tmp / "same-kernel", port)
    st.put_marker(expected="6.8.0-147-generic", before="6.8.0-147-generic")
    rc, lines = st.run()
    check("ожидаемое ядро = было (reboot-required) — ядро ок, код 0", (0, True),
          (rc, line_of(lines, "ядро").startswith("ядро: ок")))
    check("ожидаемое = было — ядро_сменилось: true (иначе вечное «нужен владелец»)", True,
          st.last_record().get("ядро_сменилось"))

    st = Stand(tmp / "http-500", port)
    st.put_marker()
    st.write_conf(f"http://127.0.0.1:{port}/bad")
    rc, lines = st.run()
    check("панель отвечает 500 — КРАСНЫЙ, код 1", (1, True),
          (rc, line_of(lines, "панель по сети").startswith("панель по сети: КРАСНЫЙ")))

    st = Stand(tmp / "cmd-fails", port)
    st.put_marker()
    broken = {"проверки": [dict(e) for e in GOOD_LIST["проверки"]]}
    broken["проверки"][6]["argv"] = [sys.executable, "-c", "import sys; print('вход истёк'); sys.exit(4)"]
    st.write_list(broken)
    rc, lines = st.run()
    check("команда вышла 4 — КРАСНЫЙ, код 1", (1, True),
          (rc, line_of(lines, "срок входа").startswith("срок входа: КРАСНЫЙ")))


def section_answer(tmp: Path, port: int) -> None:
    print("── ОТВЕТ СЕССИИ: судится ответ модели после пинка, а не свежесть файла (B-5) ──")
    api_error = {"isApiErrorMessage": True, "message": {"role": "assistant", "content": "Please run /login"}}
    for number, (name, shift, sdk) in enumerate((
            ("после загрузки только пинок", [("user", 10)], None),
            ("после пинка лишь ответ-ошибка API", [("user", 10), ("assistant", 20, api_error)], None),
            ("поле error без isApiErrorMessage", [("user", 10), ("assistant", 20, {"error": "rate_limit"})], None),
            ("ответ старше загрузки, пинка нет", [("user", -600), ("assistant", -590)], None),
            ("смена молчит, а свежий sdk-cli ответил", [("user", 10)], [("user", 15), ("assistant", 25)]),
    )):
        st = Stand(tmp / f"answer-{number}", port)
        st.put_marker()
        st.write_shift(shift)
        if sdk:
            # Headless-прогон пишет в тот же каталог и свежее смены.
            sdk_path = st.transcripts / "headless.jsonl"
            st.write_transcript(sdk_path, sdk, entrypoint="sdk-cli")
            later = time.time() + 5
            os.utime(sdk_path, (later, later))
        rc, lines = st.run()
        check(f"{name} — ответ сессии КРАСНЫЙ, код 1", (1, True),
              (rc, line_of(lines, "ответ сессии").startswith("ответ сессии: КРАСНЫЙ")))
    st = Stand(tmp / "answer-real", port)
    st.put_marker()
    st.write_shift([("user", 10), ("assistant", 15, api_error), ("assistant", 40)])
    rc, lines = st.run()
    check("ошибка API, затем настоящий ответ — ок", True,
          line_of(lines, "ответ сессии").startswith("ответ сессии: ок"))


def section_marker_rule(tmp: Path, port: int) -> None:
    print("── ОДНО ПРАВИЛО МЕТКИ: действительна, только если загрузка позже (B-7, B-6) ──")
    st = Stand(tmp / "not-happened", port)
    st.boot_time = int(time.time()) - 7200
    st.put_marker(ago_boot=7200 - 1200)  # метка 20 мин назад, загрузка 2 ч назад
    rc, lines = st.run()
    text = "\n".join(lines)
    check("метка новее загрузки и старше срока — «перезагрузка не состоялась»", True,
          "не состоялась" in text)
    check("«не состоялась» — проверки перечня не гонялись", [], st.calls("systemctl"))
    check("«не состоялась» — нет слова «поднялся»", False, "поднялся" in text)
    check("«не состоялась» — код 1 (владельцу сказать)", 1, rc)
    check("reboot-last.json: итог «не состоялась»", "не состоялась", st.last_record().get("итог"))
    check("метка снята", False, st.marker.exists())

    st = Stand(tmp / "too-early", port)
    st.boot_time = int(time.time()) - 7200
    st.put_marker(ago_boot=7200 - 60)  # метка минуту назад, загрузки после неё нет
    rc, lines = st.run()
    check("метка новее загрузки, моложе срока — код 5 «рано»", 5, rc)
    check("«рано» — метка цела", True, st.marker.exists())
    check("«рано» — итог не записан", False, st.last.exists())

    st = Stand(tmp / "late-fresh-boot", port)
    st.boot_time = int(time.time()) - 120
    st.write_shift([("user", 10), ("assistant", 20)])
    st.put_marker()
    rc, lines = st.run(extra=["--поздно"])
    check("--поздно при свежей загрузке (её ведёт --boot) — код 5", 5, rc)
    check("--поздно при свежей загрузке — метка цела", True, st.marker.exists())

    st = Stand(tmp / "late-old-boot", port)
    st.put_marker()
    rc, lines = st.run(extra=["--поздно"])
    check("--поздно, загрузка час назад, --boot не доложил — полная проверка, код 0", 0, rc)
    check("--поздно — итог «поднялся», метка снята", ("поднялся", False),
          (st.last_record().get("итог"), st.marker.exists()))


def section_no_marker(tmp: Path, port: int) -> None:
    print("── БЕЗ МЕТКИ: ручной прогон, состояние не трогается ──")
    st = Stand(tmp / "manual", port)
    rc, lines = st.run(with_marker=False)
    check("без метки — код 0 (ядро не с чем сравнить, красных нет)", 0, rc)
    check("ядро без метки — «не проверено»", True, line_of(lines, "ядро").startswith("ядро: не проверено"))
    check("reboot-last.json не записан", False, st.last.exists())


def section_broken_list(tmp: Path, port: int) -> None:
    print("── СЛОМАННЫЙ ПЕРЕЧЕНЬ — отказ, а не «всё зелёное» ──")
    over = {"проверки": [{"имя": "ответ сессии", "вид": "ответ_сессии", "ждать_сек": 590}]}
    for number, (name, data) in enumerate((("пустой перечень", {"проверки": []}),
                                           ("неизвестный вид", {"проверки": [{"имя": "x", "вид": "чудо"}]}),
                                           ("запись без имени", {"проверки": [{"вид": "tmux"}]}),
                                           ("нет ключа «проверки»", {"что-то": 1}),
                                           ("сумма пределов больше предела обёртки (B-8)", over))):
        st = Stand(tmp / f"broken-{number}", port)
        st.put_marker()
        st.write_list(data)
        rc, lines = st.run(env_extra={"HARNESS_LIVE_CHECK_TIMEOUT": "600"})
        check(f"{name} — код 2", 2, rc)
        check(f"{name} — слово «перечень сломан»", True, "перечень сломан" in "\n".join(lines))
        check(f"{name} — метка цела (проверки не было)", True, st.marker.exists())
    st = Stand(tmp / "broken-missing", port)
    st.list.unlink()
    rc, lines = st.run(with_marker=False)
    # Код 2 отдаёт и сам python, когда носителя нет: судится и слово.
    check("файла перечня нет — код 2", 2, rc)
    check("файла перечня нет — слово «перечень сломан»", True, "перечень сломан" in "\n".join(lines))


def section_busy(tmp: Path, port: int) -> None:
    print("── МАШИНА ЗАНЯТА: --занято по перечню данных (B-3) ──")
    st = Stand(tmp / "busy", port)
    lock = st.log / "locks" / "services" / "backup.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    data = dict(GOOD_LIST)
    data["занято"] = [{"имя": "резервная копия", "вид": "замок", "файл": str(lock)},
                      {"имя": "проба-процесс", "вид": "процесс", "имена": ["proba-no-such-proc-42"]}]
    st.write_list(data)
    rc, lines = st.run(with_marker=False, extra=["--занято"])
    check("замок свободен, процесса нет — «свободна», код 0", (0, True), (rc, tail(lines).startswith("свободна")))
    with open(lock, "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, lines = st.run(with_marker=False, extra=["--занято"])
    check("замок backup.lock взят — «занята», код 1", (1, True), (rc, tail(lines).startswith("занята")))
    check("«занята» называет, чем", True, "резервная копия" in tail(lines))
    data["занято"] = [{"имя": "x", "вид": "чудо"}]
    st.write_list(data)
    rc, lines = st.run(with_marker=False, extra=["--занято"])
    check("неизвестный вид занятости — код 2 (сторож прочтёт «?» — нельзя)", 2, rc)
    check("--занято состояния не трогает", False, st.last.exists())


# pgrep стенда видит только свою сессию (-s 0): судится логика данных и вида, а
# не то, что в этот миг бежит на машине (настоящий unattended-upgrade-shutdown,
# ночные ворота). Ключи -x/-f доходят до настоящего pgrep как есть.
STUB_PGREP = f"""#!/bin/sh
exec {shutil.which("pgrep") or "/usr/bin/pgrep"} -s 0 "$@"
"""
LOOP_SH = "trap 'exit 0' TERM\nwhile :; do sleep 1; done\n"
# Шебанг прямой, не через env: env делает второй exec, и comm стал бы «python3».
LOOP_PY = "#!/usr/bin/python3\nimport time\ntime.sleep(120)\n"


class Fakes:
    """Процессы-заглушки с нужными именем (comm) и строкой (cmdline) в сессии пробы."""

    def __init__(self, root: Path):
        self.root, self.alive = root, []

    def script(self, relative: str, text: str) -> Path:
        path = self.root / relative.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def python(self, relative: str, *args: str) -> None:
        """Скрипт с шебангом: comm — имя файла (усечённое до 15), как у настоящего демона."""
        self.start([str(self.script(relative, LOOP_PY)), *args])

    def bash(self, relative: str, *args: str) -> None:
        self.start(["bash", str(self.script(relative, LOOP_SH)), *args])

    def start(self, argv: list[str]) -> None:
        self.alive.append(subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.3)

    def stop(self) -> None:
        for proc in self.alive:
            proc.terminate()
        for proc in self.alive:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.alive = []


def section_busy_real_data(tmp: Path, port: int) -> None:
    print("── ЗАНЯТО ПО НАСТОЯЩЕМУ boot-check.yaml: постоянные демоны не держат, долгие прогоны держат ──")
    st = Stand(tmp / "busy-real", port)
    stub = st.bin / "pgrep"
    stub.write_text(STUB_PGREP, encoding="utf-8")
    stub.chmod(0o755)
    st.list.write_text(DATA.read_text(encoding="utf-8"), encoding="utf-8")
    env = {"SERVICE_LOCKS_SUBDIR": "proba"}
    fakes = Fakes(tmp / "fakes")

    def busy() -> tuple[int, str]:
        rc, lines = st.run(with_marker=False, extra=["--занято"], env_extra=env)
        return rc, tail(lines)

    try:
        rc, word = busy()
        check("пусто в сессии пробы — «свободна», код 0 (настоящие процессы машины не видны)",
              (0, "свободна"), (rc, word))

        fakes.python("/usr/share/unattended-upgrades/unattended-upgrade-shutdown", "--wait-for-signal")
        rc, word = busy()
        check("В-2 постоянный unattended-upgrade-shutdown --wait-for-signal — «свободна», код 0",
              (0, "свободна"), (rc, word))
        fakes.stop()

        fakes.python("/usr/bin/unattended-upgrade")
        rc, word = busy()
        check("В-2 идёт сама установка /usr/bin/unattended-upgrade — «занята», код 1",
              (1, True), (rc, word.startswith("занята:")))
        fakes.stop()

        fakes.python("/usr/bin/dpkg", "--configure", "-a")
        rc, word = busy()
        check("dpkg идёт — «занята», код 1 (как раньше)", (1, True), (rc, word.startswith("занята:")))
        fakes.stop()

        fakes.bash("/gates-snapshots/20261102-200000-cd749b81/scripts/gates-snapshot.sh",
                   "--прогон", "/var/www/project", "/gates-snapshots/20261102-200000-cd749b81", "--всё")
        rc, word = busy()
        check("В-6 идёт снимок ворот gates-snapshot.sh (setsid) — «занята», код 1",
              (1, True), (rc, word.startswith("занята:")))
        fakes.stop()

        # Выкат держит и свой замок, и свой процесс: данные вправе судить любым видом.
        deploy_lock = st.log / "locks" / "deploy.lock"
        deploy_lock.parent.mkdir(parents=True, exist_ok=True)
        fakes.bash("/project/scripts/deploy.sh")
        with open(deploy_lock, "a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            rc, word = busy()
        check("В-6 идёт выкат deploy.sh — «занята», код 1", (1, True), (rc, word.startswith("занята:")))
        fakes.stop()

        # Ночные ворота по решению спеки (3.1) не держат: обрыв ничего не теряет.
        fakes.bash("/project/harness/demons/nightly-gates.sh")
        fakes.bash("/project/scripts/gates.sh", "--всё")
        rc, word = busy()
        check("ночные ворота nightly-gates.sh и их gates.sh --всё — «свободна», код 0",
              (0, "свободна"), (rc, word))
    finally:
        fakes.stop()


def site_entries() -> list:
    """Проверки сайта из НАСТОЯЩЕГО перечня: имя начинается со «сайт»."""
    try:
        data = yaml.safe_load(DATA.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [e for e in data.get("проверки") or [] if str(e.get("имя", "")).startswith("сайт")]


def section_site_keys(tmp: Path, port: int) -> None:
    print("── Р-5 САЙТ ПО КЛЮЧАМ: пусто — «не проверено», задан — адрес из ключа ──")
    site = site_entries()
    check("Р-5 сайт: проверки сайта в настоящем перечне есть", True, bool(site))
    check("Р-5 сайт: у каждой проверки сайта свой ключ, адреса в данных нет",
          SITE_KEYS,
          {e.get("имя"): e.get("ключ") for e in site if not e.get("адрес")})
    st = Stand(tmp / "site-keys", port)
    st.write_list({"проверки": site})
    empty = {key: "" for key in SITE_KEYS.values()}
    _, lines = st.run(with_marker=False, env_extra=empty)
    for name, key in SITE_KEYS.items():
        line = line_of(lines, name)
        check(f"Р-5 {name}: ключ {key} пуст — «не проверено», не провал и не «ок»", True,
              line.startswith(f"{name}: не проверено") and key in line)
    given = {"SITE_HEALTH_URL": f"http://127.0.0.1:{port}/ok", "SITE_FORM_URL": f"http://127.0.0.1:{port}/405"}
    with (st.base / "harness.conf").open("a", encoding="utf-8") as conf:
        conf.writelines(f'{key}="{value}"\n' for key, value in given.items())
    _, lines = st.run(with_marker=False, env_extra=empty)
    for name, key in SITE_KEYS.items():
        line = line_of(lines, name)
        check(f"Р-5 {name}: ключ {key} задан — проверка по адресу из ключа, ок", True,
              line.startswith(f"{name}: ок") and given[key] in line)


def section_containers_real_data(tmp: Path, port: int) -> None:
    print("── СОСЕДНИЕ КОНТЕЙНЕРЫ ПО НАСТОЯЩЕМУ ПЕРЕЧНЮ: чужой упавший контейнер не судится ──")
    try:
        data = yaml.safe_load(DATA.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    entries = data.get("проверки") or []
    check("контейнеры: настоящий перечень разобран и не пуст", True, bool(entries))
    # Слово владельца 02.11: соседи машины не проверяются, сайту контейнеры не нужны.
    check("контейнеры: в перечне нет проверки вида docker, судящей контейнеры", [],
          [e.get("имя") for e in entries if e.get("вид") == "docker"])
    st = Stand(tmp / "neighbour-containers", port)
    st.write_list({"проверки": entries})
    (st.stub / "containers").write_text("/proba-pg always false\n", encoding="utf-8")
    _, lines = st.run(with_marker=False)
    check("стоит чужой контейнер с автоподъёмом — ни одна строка вести его не называет", [],
          [line for line in lines if "proba-pg" in line])


def conf_value(key: str) -> str:
    try:
        text = CONF.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.findall(rf'^{key}="?([^"\n#]*)"?', text, flags=re.M)
    return found[-1].strip() if found else ""


def section_data() -> None:
    print("── ДАННЫЕ: настоящий boot-check.yaml ──")
    check(f"перечень на месте: {DATA}", True, DATA.is_file())
    try:
        data = yaml.safe_load(DATA.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        check("перечень разбирается yaml.safe_load", "разобран", f"{type(error).__name__}")
        return
    entries = data.get("проверки") or []
    check("проверок не меньше десяти", True, len(entries) >= 10)
    check("у каждой записи есть имя", [], [e for e in entries if not e.get("имя")])
    check("все виды известны коду", [], sorted({e.get("вид") for e in entries} - KINDS))
    units = {e.get("юнит") for e in entries if e.get("вид") == "юнит"}
    for unit in ("harness-agent", "harness-dispatcher", "harness-panel", "harness-firewall",
                 "harness-sentinel.timer", "cron", "docker", "nginx"):
        check(f"юнит {unit} в перечне подъёма", True, unit in units)
    optional = sorted(e.get("юнит") for e in entries if e.get("необязательный") is True)
    check("необязательны только чужие системы: стена, docker, nginx", ["docker", "harness-firewall", "nginx"],
          optional)
    kinds = [e.get("вид") for e in entries]
    for kind in ("tmux", "ответ_сессии", "ядро", "http", "команда"):
        check(f"вид «{kind}» в перечне", True, kind in kinds)
    check("продукт проверяется по PRODUCT_HEALTH_URL", True,
          any(e.get("ключ") == "PRODUCT_HEALTH_URL" for e in entries))
    check("срок входа (check-login-expiry.py) в перечне", True,
          any("check-login-expiry.py" in " ".join(map(str, e.get("argv") or [])) for e in entries))
    budget = sum(int(e.get("ждать_сек") or 0) for e in entries) \
        + sum(int(e.get("предел_сек") or 60) for e in entries if e.get("вид") == "команда") \
        + 10 * sum(1 for e in entries if e.get("вид") not in ("ответ_сессии", "команда"))
    limit = conf_value("REBOOT_CHECK_TIMEOUT_SEC")
    check("сумма пределов перечня + 30 с меньше REBOOT_CHECK_TIMEOUT_SEC (B-8)", True,
          limit.isdigit() and budget + 30 <= int(limit))
    busy = data.get("занято") or []
    check("секция «занято» есть (B-3)", True, bool(busy))
    check("виды занятости известны коду", [], sorted({e.get("вид") for e in busy} - BUSY_KINDS))
    text = json.dumps(busy, ensure_ascii=False)
    check("занято: замок резервной копии backup.lock", True, "backup.lock" in text)
    check("занято: проба восстановления restore-probe", True, "restore-probe" in text)
    check("занято: установка пакетов (dpkg)", True, "dpkg" in text)
    check("занято: unattended-upgrades", True, "unattended-upgr" in text)
    # Слово владельца 02.11: после подъёма проверяются только харнес и сайт.
    site = [e for e in entries if str(e.get("имя", "")).startswith("сайт")]
    check("соседи: в перечне есть проверка «сайт»", True, bool(site))
    check("соседи: «сайт» проверяется по сети (вид http)", True, any(e.get("вид") == "http" for e in site))
    check("соседи: ни одна проверка и ни одна занятость не касается neighbour-1", [],
          [e.get("имя") for e in entries + busy if "neighbour-1" in json.dumps(e, ensure_ascii=False).lower()])
    check(f"конфиг на месте: {CONF}", True, CONF.is_file())
    for path in (DATA, CONF):
        text = path.read_text(encoding="utf-8").lower()
        check(f"Р-5 в {path.name} нет слов этой машины ({', '.join(OWNER_WORDS)})", [],
              [word for word in OWNER_WORDS if word in text])
    section_new_install_keys()


def section_new_install_keys() -> None:
    """install-new.py доносит на установку только ключи из system-jobs.yaml «ключи:»."""
    try:
        keys = (yaml.safe_load(SYSTEM_JOBS.read_text(encoding="utf-8")) or {}).get("ключи") or {}
    except (OSError, yaml.YAMLError) as error:
        check(f"Р-5 {SYSTEM_JOBS.name} разбирается", "разобран", type(error).__name__)
        return
    reboot_keys = sorted(key for key in из_файла(str(CONF))
                         if key.startswith(("REBOOT_", "AUTO_REBOOT_")))
    check("Р-5 ключи перезагрузки в harness.conf найдены (список из конфига не пуст)", True, bool(reboot_keys))
    check("Р-5 ключи перезагрузки — в system-jobs.yaml «ключи:» с умолчанием как в harness.conf", {},
          {key: keys.get(key, "НЕТ") for key in reboot_keys if str(keys.get(key, "НЕТ")) != conf_value(key)})
    check("Р-5 system-jobs: AUTO_REBOOT_IN_ROTATION по умолчанию 0", "0", str(keys.get("AUTO_REBOOT_IN_ROTATION")))
    for key in SITE_KEYS.values():
        check(f"Р-5 system-jobs: {key} есть и пуст", "", keys.get(key, "НЕТ"))


def main() -> int:
    check(f"носитель на месте: {BOOT_CHECK.name}", True, BOOT_CHECK.is_file())
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="boot-check-probe-") as tmp:
            for section in (section_green, section_red_unit, section_unit_not_installed, section_red_kinds, section_answer,
                            section_marker_rule, section_no_marker, section_broken_list, section_busy,
                            section_busy_real_data, section_site_keys, section_containers_real_data):
                try:
                    section(Path(tmp), port)
                except Exception as error:  # упавший раздел — неудача, а не тишина
                    check(f"раздел {section.__name__} дошёл до конца", "дошёл",
                          f"упал: {type(error).__name__}: {error}")
    finally:
        server.shutdown()
    section_data()
    calls = GUARD_DIR / "reboot.calls"
    check("заглушка REBOOT_CMD не звана ни разу (проверке перезагружать нечего)", False, calls.exists())
    print(f"boot_check: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for leftover in GUARD_DIR.glob("*"):
            leftover.unlink()
        GUARD_DIR.rmdir()
