#!/usr/bin/env python3
"""Проба: sentinel.sh после перезагрузки в окне ротации — до кода.

Карточка hr.reboot-in-rotation-window, спека
docs/specs/2026-11-02-перезагрузка-в-окне-ротации.md §6.3, §6.4. Владелец 02.11
(входящее 10563): «А после перезагрузки сервера как ты поднимешься? Tmux и все
остальное предусмотрел?»

Sentinel идёт через СВОЙ вход на стенде: свои паспорт, LOG_DIR, PANEL_STATE_DIR
(HARNESS_PANEL_STATE) с меткой «перезагрузка начата», heartbeat со свежими
метками сторожа (иначе sentinel поднимал бы его и говорил лишнее), проект-стенд
с заглушками проверок и пинка, дверь канала и писатель карточек — заглушки,
пишущие каждый зов строкой JSON. В PATH — заглушки systemctl/tmux/uname и
запретные sudo/reboot/shutdown: настоящие юниты не спрашиваются и не трогаются.
Перечень проверок — свой, через HARNESS_BOOT_CHECK_LIST; время загрузки —
дверь HARNESS_BOOT_CHECK_BOOT_TIME.

У agent полный sudo без пароля: ПЕРВЫМ делом REBOOT_CMD ставится на заглушку во
временном каталоге и сверяется — иначе отказ до любого прогона.

    python3 scripts/test_sentinel_after_restart.py

Двери мутаций ([[probe-loads-the-live-mechanism]]):
    HARNESS_SENTINEL     путь sentinel.sh под пробой
    HARNESS_BOOT_CHECK   путь boot_check.py, который кладётся в проект-стенд
Последняя строка: `sentinel после перезагрузки: случаев N, падений F`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

# ── заглушка команды перезагрузки — до всего остального ─────────────────────
GUARD_DIR = Path(tempfile.mkdtemp(prefix="sentinel-restart-guard-"))
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
from selftest_cases import Cases  # noqa: E402


def default_sentinel() -> Path:
    """harness/demons в проекте, demons/ рядом со scripts/ в пакете."""
    for candidate in (HERE.parent / "harness" / "demons" / "sentinel.sh",
                      HERE.parent / "demons" / "sentinel.sh"):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "demons" / "sentinel.sh"


SENTINEL = Path(os.environ.get("HARNESS_SENTINEL") or default_sentinel())
BOOT_CHECK = Path(os.environ.get("HARNESS_BOOT_CHECK") or HERE / "boot_check.py")
# Умолчание следа — под LOG_DIR стенда (корень следов пуст в его harness.conf).
TRACE = Path("живая-проверка") / "hr.reboot-in-rotation-window" / "живьём"
OWNER_LIMIT = 700  # память/TONE.md: отчёт до 700 знаков

cases = Cases()
check = cases.check

STUB_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$STUB/systemctl.calls"
case "$1" in
    is-active)
        if grep -qx "$2" "$STUB/inactive" 2>/dev/null; then echo inactive; exit 3; fi
        echo active ;;
    show) : ;;
    *) exit 1 ;;
esac
"""
STUB_TMUX = """#!/bin/sh
echo "$*" >> "$STUB/tmux.calls"
case "$1" in
    display-message) echo claude ;;
    has-session) exit 0 ;;
    *) exit 0 ;;
esac
"""
STUB_UNAME = """#!/bin/sh
case "$1" in -r) echo 6.8.0-147-generic ;; *) echo Linux ;; esac
"""
STUB_FORBIDDEN = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB/forbidden.calls"
exit 1
"""
# Дверь канала и писатель карточек: каждый зов — строкой JSON со всеми доводами.
STUB_RECORDER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB"] + "/{name}.jsonl", "a", encoding="utf-8") as out:
    out.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
"""
STUB_OK_SH = "#!/bin/sh\necho ок\nexit 0\n"
# Сторож сессий: каждый зов — строкой «SENTINEL_RESCUE|доводы»; спасение — зов без доводов.
STUB_WARDEN = """#!/bin/sh
echo "${SENTINEL_RESCUE:-}|$*" >> "$STUB/warden.calls"
echo ок
"""
STUB_OK_PY = "print('ок')\n"

BOOT_LIST = {
    "проверки": [
        {"имя": "агент", "вид": "юнит", "юнит": "harness-agent",
         "дверь": "sudo systemctl restart harness-agent"},
        {"имя": "проба-панель", "вид": "юнит", "юнит": "proba-panel", "дверь": "дверь-пробы-77"},
        {"имя": "сессия агента", "вид": "tmux"},
        {"имя": "ядро", "вид": "ядро"},
    ]
}


def put(path: Path, text: str, mode: int = 0o755) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


class Stand:
    def __init__(self, base: Path, switch: str = "on", exempt: bool = True):
        self.base = base
        self.stub = base / "stub"
        self.bin = base / "bin"
        self.project = base / "project"
        self.state = base / "state"
        self.panel_state = self.state / "panel-state"
        self.log = base / "log"
        self.heartbeat = self.state / "heartbeat"
        for d in (self.stub, self.bin, self.project, self.log, self.heartbeat, self.panel_state, base / "home"):
            d.mkdir(parents=True, exist_ok=True)
        for name, text in (("systemctl", STUB_SYSTEMCTL), ("tmux", STUB_TMUX), ("uname", STUB_UNAME),
                           ("sudo", STUB_FORBIDDEN), ("reboot", STUB_FORBIDDEN),
                           ("shutdown", STUB_FORBIDDEN), ("poweroff", STUB_FORBIDDEN), ("halt", STUB_FORBIDDEN)):
            put(self.bin / name, text)
        scripts = self.project / "scripts"
        shutil.copytree(HERE / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(HERE / "live-check.sh", scripts / "live-check.sh")
        # Спасение сторожа идёт через обёртку выключателя: без неё зов уходит мимо заглушки.
        shutil.copy(HERE / "run-background.sh", scripts / "run-background.sh")
        if BOOT_CHECK.is_file():
            shutil.copy(BOOT_CHECK, scripts / "boot_check.py")
        put(scripts / "tg_send.sh", STUB_RECORDER.replace("{name}", "sent"))
        put(scripts / "alert-task.py", STUB_RECORDER.replace("{name}", "cards"))
        put(scripts / "check-secrets.sh", STUB_OK_SH)
        put(scripts / "check-login-expiry.py", STUB_OK_PY)
        put(scripts / "hooks" / "test_guard_bash.py", STUB_OK_PY)
        put(self.project / "harness" / "demons" / "session-warden.sh", STUB_WARDEN)
        for mark in ("session-warden.запуск", "session-warden"):
            (self.heartbeat / mark).touch()
        (base / "install.conf").write_text(
            f'PROJECT_DIR="{self.project}"\nLOG_DIR="{self.log}"\nSTATE_DIR="{self.state}"\n'
            f'PANEL_STATE_DIR="{self.panel_state}"\n'
            f'HEARTBEAT_DIR="{self.heartbeat}"\nTMUX_SESSION="proba-agent"\nAGENT_START_CMD="true"\n',
            encoding="utf-8")
        (base / "harness.conf").write_text(
            f'HEARTBEAT_PERIODS=""\nREBOOT_LATE_MIN=15\nREBOOT_CHECK_TIMEOUT_SEC=600\n'
            f'REBOOT_CMD="{REBOOT_STUB}"\nBACKGROUND_PROCESSES={switch}\n', encoding="utf-8")
        # Своя библиотека процессов: живая не читается, исключение задаёт случай.
        self.library = base / "process-library.yaml"
        self.library.write_text(yaml.safe_dump(
            {"версия": 1, "выключатель": {"ключ": "BACKGROUND_PROCESSES", "исключения":
                                         [{"id": "session-warden"}] if exempt else [{"id": "backup"}]}},
            allow_unicode=True), encoding="utf-8")
        self.list = base / "boot-check.yaml"
        self.list.write_text(yaml.safe_dump(BOOT_LIST, allow_unicode=True), encoding="utf-8")
        self.marker = self.panel_state / "reboot-started.json"
        self.last = self.panel_state / "reboot-last.json"
        self.boot_time = int(time.time()) - 3600

    def put_marker(self, at: int | None = None) -> None:
        """at — время метки; по умолчанию за 5 мин ДО загрузки (перезагрузка была)."""
        self.marker.write_text(json.dumps({
            "с": self.boot_time - 300 if at is None else at, "сессия": "proba-sessiya",
            "причина": "окно ≈80% ≥ 75%", "ядро_было": "6.8.0-138-generic",
            "ядро_ожидаемое": "6.8.0-147"}, ensure_ascii=False), encoding="utf-8")

    def sentinel(self, boot: bool = True) -> int:
        env = dict(os.environ)
        for key in ("CLAUDE_CODE_SESSION_ID", "HARNESS_REPORT", "BOOT_CHECK_TRACE_DIR",
                    "HARNESS_LIVE_CHECK_TRACE_DIR", "HARNESS_LIVE_CHECK_TIMEOUT"):
            env.pop(key, None)
        env.update({
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "STUB": str(self.stub),
            "HARNESS_HOME": str(self.base / "home"),
            "HARNESS_INSTALL_CONF": str(self.base / "install.conf"),
            "HARNESS_CONF": str(self.base / "harness.conf"),
            "PROJECT_DIR": str(self.project), "LOG_DIR": str(self.log), "STATE_DIR": str(self.state),
            "HARNESS_PANEL_STATE": str(self.panel_state),
            "HARNESS_PROCESS_LIBRARY": str(self.library),
            "HEARTBEAT_DIR": str(self.heartbeat), "TMUX_SESSION": "proba-agent",
            "SENTINEL_UNITS": "harness-agent", "BOOT_WAIT_SEC": "5",
            "HARNESS_BOOT_CHECK_LIST": str(self.list),
            "HARNESS_BOOT_CHECK_BOOT_TIME": str(self.boot_time),
            "REBOOT_CMD": str(REBOOT_STUB), "PYTHONDONTWRITEBYTECODE": "1",
        })
        argv = ["bash", str(SENTINEL)] + (["--boot"] if boot else [])
        try:
            done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180, cwd=str(self.base))
            with open(self.base / "sentinel.out", "a", encoding="utf-8") as out:
                out.write(done.stdout + done.stderr)
            return done.returncode
        except subprocess.TimeoutExpired:
            return -9

    def rescues(self) -> list[str]:
        """Зовы сторожа без доводов — спасение из раздела 2 sentinel."""
        return [c for c in self.calls("warden") if c.split("|", 1)[1] == ""]

    def process_events(self) -> list[tuple[str, str]]:
        path = self.log / "process-runs.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [(row["event"], row["data"]["id"]) for row in rows]

    def recorded(self, name: str) -> list[list[str]]:
        path = self.stub / f"{name}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def sent(self) -> list[list[str]]:
        return self.recorded("sent")

    def calls(self, name: str) -> list[str]:
        path = self.stub / f"{name}.calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def trace_files(self) -> list[Path]:
        folder = self.log / TRACE
        return sorted(folder.glob("*.log")) if folder.is_dir() else []

    def trace_text(self) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in self.trace_files())


def text_of(message: list[str]) -> str:
    """Текст вести — первый довод, не ключ двери."""
    return next((arg for arg in message if not arg.startswith("--")), "")


def about_reboot(messages: list[list[str]]) -> list[str]:
    return [text_of(m) for m in messages if "перезагру" in text_of(m) or "не провёл" in text_of(m)]


def changing_calls(st: Stand) -> list[str]:
    return [c for c in st.calls("systemctl") if c.split()[:1] not in (["is-active"], ["show"])]


def owner_words(text: str) -> None:
    """Мерило TONE.md для вести после перезагрузки (B-13)."""
    check(f"весть не длиннее {OWNER_LIMIT} знаков (вышло {len(text)})", True, 0 < len(text) <= OWNER_LIMIT)
    check("в вести нет команд («sudo», «systemctl»)", [],
          [word for word in ("sudo", "systemctl") if word in text])


def section_green(tmp: Path) -> None:
    print("── ПЕРЕЗАГРУЗКА В ОКНЕ, ВСЁ ПОДНЯЛОСЬ: одна весть словами ──")
    st = Stand(tmp / "green")
    st.put_marker()
    st.sentinel()
    sent = st.sent()
    check("владельцу ровно одна весть", 1, len(sent))
    text = text_of(sent[0]) if sent else ""
    first = text.splitlines()[0] if text else ""
    check("первая строка — итог «перезагружен при смене сессии»", True, "перезагружен при смене сессии" in first)
    check("итог словами: «поднялось»", True, "поднялось" in first)
    check("в зелёной вести нет красного", False, "КРАСНЫЙ" in text or "НЕ ПОДНЯЛСЯ" in text)
    owner_words(text)
    check("весть с ключом --несмотря-на-лимит (досыл после лимита — молчание, B-18)", True,
          bool(sent) and "--несмотря-на-лимит" in sent[0])
    trace = st.trace_text()
    check("стойкий след live-check в каталоге задачи стенда", True, "поднялся: ок" in trace)
    check("след называет механизм boot_check.py", True, "boot_check.py" in trace)
    check("метка снята", False, st.marker.exists())
    check("итог записан в reboot-last.json (PANEL_STATE_DIR)", True, st.last.exists())
    check("systemctl ничего не поднимал и не гасил", [], changing_calls(st))
    check("sudo/reboot/shutdown не звались", [], st.calls("forbidden"))

    print("── ВТОРАЯ ЗАГРУЗКА В ОКНЕ: у прогона свой след, прошлый цел (B-8) ──")
    time.sleep(1.1)  # имя следа — с секундами; настоящие загрузки так близко не бывают
    st.put_marker()
    st.sentinel()
    trace = st.trace_text()
    check("следов два: по файлу на прогон", 2, len(st.trace_files()))
    check("в следах два прогона boot_check", True, trace.count("--- вывод механизма ---") >= 2
          and trace.count("поднялся: ок") >= 2)


def section_red(tmp: Path) -> None:
    print("── ПЕРЕЗАГРУЗКА В ОКНЕ, ЮНИТ ИЗ ПЕРЕЧНЯ НЕ ПОДНЯЛСЯ: имя в вести, дверь в карточке ──")
    st = Stand(tmp / "red")
    st.put_marker()
    (st.stub / "inactive").write_text("proba-panel\n", encoding="utf-8")
    rc = st.sentinel()
    sent = st.sent()
    check("владельцу ровно одна весть", 1, len(sent))
    text = text_of(sent[0]) if sent else ""
    check("весть называет неподнявшееся по имени", True, "проба-панель" in text)
    check("первая строка — «перезагружен при смене сессии»", True,
          "перезагружен при смене сессии" in (text.splitlines()[0] if text else ""))
    check("двери (команды терминала) в вести нет — владелец видит только канал", False, "дверь-пробы-77" in text)
    owner_words(text)
    cards = st.recorded("cards")
    # Среди зовов писателя бывает и общий --drain из раздела 3в: ищем карточку по ключу.
    card = next((" ".join(c) for c in cards if "--key" in c), "")
    check("красное → карточка sentinel:start-red", True, "sentinel:start-red" in card)
    check("карточка несёт дверь из перечня", True, "дверь-пробы-77" in card)
    check("весть без --task (карточку пишет sentinel сам, с дверями)", False, bool(sent) and "--task" in sent[0])
    check("код sentinel не 0", True, rc != 0)
    check("красный юнит вне SENTINEL_UNITS не перезапускали", [], changing_calls(st))


def section_planned(tmp: Path) -> None:
    print("── ПЛАНОВЫЙ ПРОГОН ПРИ МЕТКЕ: --boot не доложил (B-6) ──")
    base = Stand(tmp / "planned-base")
    base.sentinel(boot=False)
    baseline = len(base.sent())
    check("плановый прогон без метки о перезагрузке молчит (как раньше)", [], about_reboot(base.sent()))

    st = Stand(tmp / "planned-late")
    st.put_marker()
    st.sentinel(boot=False)
    told = about_reboot(st.sent())
    check("загрузка час назад, метка цела — одна весть об итоге", 1, len(told))
    check("весть — «перезагружен при смене сессии»", True, bool(told) and "перезагружен при смене сессии" in told[0])
    check("метка снята", False, st.marker.exists())
    check("итог записан", True, st.last.exists())

    st = Stand(tmp / "planned-not-happened")
    st.boot_time = int(time.time()) - 7200
    st.put_marker(at=int(time.time()) - 1200)
    st.sentinel(boot=False)
    told = about_reboot(st.sent())
    check("метка 20 мин, загрузки после неё нет — одна весть «не провёл»", True,
          len(told) == 1 and "не провёл" in told[0])
    check("метка снята", False, st.marker.exists())
    try:
        result = json.loads(st.last.read_text(encoding="utf-8")).get("итог")
    except (OSError, ValueError):
        result = "итога нет"
    check("итог «не состоялась»", "не состоялась", result)

    st = Stand(tmp / "planned-fresh")
    st.boot_time = int(time.time()) - 60
    st.put_marker()
    st.sentinel(boot=False)
    check("свежая загрузка — плановый прогон молчит (доклад за --boot)", baseline, len(st.sent()))
    check("свежая загрузка — метка цела", True, st.marker.exists())


def section_stuck_marker(tmp: Path) -> None:
    print("── В-4 МЕТКУ НЕ СНЯТЬ: плановый прогон каждые 5 мин не повторяет весть ──")
    # Каталог метки только на чтение: ни замок, ни итог, ни снятие не пишутся.
    st = Stand(tmp / "stuck-ro")
    st.boot_time = int(time.time()) - 7200
    st.put_marker(at=int(time.time()) - 1200)
    st.panel_state.chmod(0o555)
    try:
        st.sentinel(boot=False)
        st.sentinel(boot=False)
    finally:
        st.panel_state.chmod(0o755)
    told = about_reboot(st.sent())
    check("В-4 каталог метки только на чтение — за два плановых прогона одна весть", 1, len(told))

    # Каталог пишется, а итог — нет: на месте его временного файла каталог.
    st = Stand(tmp / "stuck-result")
    st.boot_time = int(time.time()) - 7200
    st.put_marker(at=int(time.time()) - 1200)
    (st.panel_state / "reboot-last.json.tmp").mkdir()
    st.sentinel(boot=False)
    st.sentinel(boot=False)
    told = about_reboot(st.sent())
    check("В-4 итог не пишется — за два плановых прогона одна весть", 1, len(told))


def section_no_marker(tmp: Path) -> None:
    print("── ОБЫЧНАЯ ЗАГРУЗКА (метки нет): доклад прежний ──")
    st = Stand(tmp / "plain")
    st.sentinel()
    sent = st.sent()
    check("владельцу ровно одна весть", 1, len(sent))
    text = text_of(sent[0]) if sent else ""
    check("весть — прежний доклад о загрузке", True, "Сервер загрузился" in text)
    check("о перезагрузке в окне не говорит", False, "при смене сессии" in text)
    check("boot_check звался без метки — след есть", True, "поднялся: ок" in st.trace_text())
    check("весть называет итог подъёма систем", True, "поднялся: ок" in text)
    check("reboot-last.json не записан", False, st.last.exists())

    print("── ВНЕЗАПНАЯ ПЕРЕЗАГРУЗКА (метки нет), ЮНИТ НЕ ПОДНЯЛСЯ: то же слово о неподнявшемся ──")
    red = Stand(tmp / "plain-red")
    (red.stub / "inactive").write_text("proba-panel\n", encoding="utf-8")
    rc = red.sentinel()
    text = " ".join(text_of(m) for m in red.sent())
    check("sentinel вернул красный код", 1, rc)
    check("весть говорит НЕ ПОДНЯЛСЯ", True, "НЕ ПОДНЯЛСЯ" in text)
    check("весть названа с ключом карточки sentinel:start-red", True,
          any("sentinel:start-red" in m for m in red.sent()))
    check("метки нет — reboot-last.json не записан", False, red.last.exists())


def section_switch(tmp: Path) -> None:
    print("── СПАСЕНИЕ СТОРОЖА ЧЕРЕЗ ВЫКЛЮЧАТЕЛЬ: on и off с исключением — идёт, off без исключения — нет ──")
    # Порог 0: окно cron стенда — минуты от рождения каталога, иначе раздел 2 не заговорит.
    os.environ["CRON_STALE_MIN"] = "0"
    try:
        for name, switch, exempt, goes in (("switch-on", "on", True, True),
                                           ("switch-off-exempt", "off", True, True),
                                           ("switch-off-plain", "off", False, False)):
            st = Stand(tmp / name, switch=switch, exempt=exempt)
            (st.stub / "inactive").write_text("cron\n", encoding="utf-8")
            st.sentinel()
            events = st.process_events()
            label = f"{switch}, исключение {'есть' if exempt else 'нет'}"
            check(f"{label}: спасение сторожа {'идёт' if goes else 'не идёт'}", goes, bool(st.rescues()))
            check(f"{label}: спасение помечено SENTINEL_RESCUE=1", True,
                  all(c.startswith("1|") for c in st.rescues()))
            check(f"{label}: журнал обёртки стенда", True,
                  ("process.end", "session-warden") in events if goes
                  else ("process.skip", "session-warden") in events)
            check(f"{label}: cron поднимали заглушкой systemctl, как раньше", True,
                  "restart cron" in st.calls("systemctl"))
            check(f"{label}: пинок --kick отдан, как раньше", True,
                  any(c.split("|", 1)[1] == "--kick" for c in st.calls("warden")))
    finally:
        os.environ.pop("CRON_STALE_MIN", None)


def main() -> int:
    check(f"sentinel на месте: {SENTINEL.name}", True, SENTINEL.is_file())
    check(f"носитель проверки на месте: {BOOT_CHECK.name}", True, BOOT_CHECK.is_file())
    with tempfile.TemporaryDirectory(prefix="sentinel-restart-probe-") as tmp:
        for section in (section_green, section_red, section_planned, section_stuck_marker, section_no_marker,
                        section_switch):
            try:
                section(Path(tmp))
            except Exception as error:  # упавший раздел — неудача, а не тишина
                check(f"раздел {section.__name__} дошёл до конца", "дошёл",
                      f"упал: {type(error).__name__}: {error}")
    check("заглушка REBOOT_CMD не звана ни разу", False, (GUARD_DIR / "reboot.calls").exists())
    print(f"sentinel после перезагрузки: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for leftover in GUARD_DIR.glob("*"):
            leftover.unlink()
        GUARD_DIR.rmdir()
