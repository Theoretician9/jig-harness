#!/usr/bin/env python3
"""П8 (ротация с преемником): обновление доходит до живой сессии без лишней ротации.

Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.15. Владелец
21.11 13:1x: «когда пришло какое-то обновление… чтобы это обновление дошло до
той сессии, которую ещё не нужно ротировать выходом, и когда нужно будет
ротировать выходом, чтобы это не было сильным тормозом». Горячее (хуки, скрипты,
демоны, конфиги — читаются с диска при вызове) ротации не просит; холодное
(CLAUDE.md, личность, settings.json, MCP — грузятся при старте процесса) —
заявка ротации с причиной «обновление», тем же путём преемника.

Судится `scripts/lib/cold_paths.py --после-раскладки <архив>` на стенде: свой
архив с journal.tsv (формат scripts/lib/layout_journal.py), свой каталог
состояния панели (HARNESS_PANEL_STATE). Маски — ЖИВЫЕ данные дерева
(harness/config/cold-paths.yaml; дверь HARNESS_COLD_PATHS). Итог — строка
«ПРОБА ЗЕЛЁНАЯ: …» / «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не годен — «ПРОБА
СЛОМАНА: …», код 2.

Поведение носителей (не только порядок в тексте): заявку ставит, читает,
дописывает и снимает один CLI `rotation_flag.py`; `update.py` при сбое сверки
обновление не откатывает, при откате снимает заявку; сторож читает заявку и
начинает ротацию с причиной «обновление» — стенд П1
(test_rotation_successor_warden.sh) с дверью P1_VIA_REQUEST=1.
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def tree_file(*parts: str) -> Path:
    # Проект: scripts/ рядом с harness/; пакет: harness/scripts/ рядом с config/.
    for candidate in (HERE.parent / "harness" / Path(*parts), HERE.parent / Path(*parts)):
        if candidate.exists():
            return candidate
    return HERE.parent / "harness" / Path(*parts)


TOOL = Path(os.environ.get("HARNESS_COLD_PATHS_TOOL") or HERE / "lib" / "cold_paths.py")
MASKS = Path(os.environ.get("HARNESS_COLD_PATHS") or tree_file("config", "cold-paths.yaml"))
UPDATER = HERE / "update.py"
FLAG_CLI = HERE / "lib" / "rotation_flag.py"
P1 = HERE / "test_rotation_successor_warden.sh"
WARDEN = tree_file("demons", "session-warden.sh")
REQUEST_NAME = "rotation-request.json"
BLOB_A, BLOB_B = "a" * 40, "b" * 40

HOT = ["scripts/hooks/guard_bash.py", "scripts/hooks/stop_reminder.sh", "harness/demons/speed_mode.py",
       "harness/config/harness.conf", "scripts/update.py", "scripts/lib/rotation_flag.py"]

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, got: str = "") -> None:
    checks.append((name, ok, got))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {got}"))


class Stand:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rot-successor-p8-"))
        atexit.register(shutil.rmtree, self.root, True)
        self.panel = self.root / "panel"
        self.panel.mkdir()
        (self.root / "install.conf").write_text(
            f'PANEL_STATE_DIR="{self.panel}"\nLOG_DIR="{self.root}/log"\nTMUX_SESSION="agent"\n', encoding="utf-8")
        (self.root / "harness.conf").write_text("", encoding="utf-8")

    def archive(self, paths: list[str]) -> Path:
        """Архив прогона обновления: «перекрыт» каждого пути и «готово» за ним."""
        folder = Path(tempfile.mkdtemp(prefix="HarnessProba-20261121-130000-", dir=self.root))
        lines = [f"# прогон 1 обновление план={len(paths)} перекрыть={len(paths)}"]
        for p in paths:
            lines += [f"перекрыт\t{BLOB_A}\t{BLOB_B}\t{p}", f"готово\t-\t-\t{p}"]
        (folder / "journal.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder

    def env(self) -> dict:
        return {**os.environ, "HARNESS_PANEL_STATE": str(self.panel), "HARNESS_COLD_PATHS": str(MASKS),
                "HARNESS_INSTALL_CONF": str(self.root / "install.conf"),
                "HARNESS_CONF": str(self.root / "harness.conf"), "PYTHONDONTWRITEBYTECODE": "1"}

    def flag_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(FLAG_CLI), *args], capture_output=True, text=True,
                              env=self.env(), timeout=60)

    def run(self, paths: list[str]) -> tuple[int, str, dict | None]:
        (self.panel / REQUEST_NAME).unlink(missing_ok=True)
        done = subprocess.run([sys.executable, str(TOOL), "--после-раскладки", str(self.archive(paths))],
                              capture_output=True, text=True, env=self.env(), timeout=60)
        request = self.panel / REQUEST_NAME
        try:
            data = json.loads(request.read_text(encoding="utf-8")) if request.exists() else None
        except ValueError:
            data = {"битая": request.read_text(encoding="utf-8")[:200]}
        return done.returncode, (done.stdout + done.stderr).strip(), data


def reason_of(data: dict | None) -> str:
    return str((data or {}).get("причина") or "")


def function_body(text: str, name: str) -> str:
    """Тело функции python верхнего уровня по имени — до следующего `def` с начала строки."""
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^def |\Z)", text, re.S | re.M)
    return m.group(0) if m else ""


def request_cli_checks(stand: Stand) -> None:
    """Заявку ставит, читает, дописывает и снимает один CLI."""
    request = stand.panel / REQUEST_NAME
    request.unlink(missing_ok=True)
    put = stand.flag_cli("--request", "--reason", "обновление", "--tmux", "agent", "--role", "оркестратор",
                         "--detail", "CLAUDE.md")
    check("--request — код 0", put.returncode == 0, f"код {put.returncode}: {put.stderr[-300:]}")
    read = stand.flag_cli("--request-state")
    try:
        data = json.loads(read.stdout) if read.returncode == 0 else None
    except ValueError:
        data = {"битая": read.stdout[:200]}
    check("--request-state читает заявку того же CLI (причина, путь, tmux, роль)",
          bool(data) and data.get("причина") == "обновление" and data.get("подробно") == "CLAUDE.md"
          and data.get("tmux") == "agent" and data.get("роль") == "оркестратор",
          f"код {read.returncode}: {read.stdout[-300:]}")
    stand.flag_cli("--request", "--reason", "обновление", "--detail", ".mcp.json CLAUDE.md")
    second = json.loads(request.read_text(encoding="utf-8")) if request.exists() else {}
    check("вторая заявка дописывает пути, а не теряет прежние",
          str(second.get("подробно") or "").split() == ["CLAUDE.md", ".mcp.json"],
          json.dumps(second, ensure_ascii=False))
    check("вторая заявка сохраняет время первой («с»)",
          bool(data) and second.get("с") == data.get("с"), json.dumps(second, ensure_ascii=False))
    cleared = stand.flag_cli("--request-clear")
    gone = stand.flag_cli("--request-state")
    check("--request-clear снимает заявку", cleared.returncode == 0 and not request.exists(),
          f"код {cleared.returncode}, файл {'есть' if request.exists() else 'нет'}")
    check("--request-state без заявки — код 1 и пустой вывод (как --state без флага)",
          gone.returncode == 1 and not gone.stdout.strip(), f"код {gone.returncode}: {gone.stdout[-200:]}")


def load_updater():
    spec = importlib.util.spec_from_file_location("update_p8", UPDATER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def updater_checks(stand: Stand) -> None:
    """update.py: сбой сверки обновление не откатывает; откат снимает заявку."""
    updater = load_updater()
    broken = stand.root / "broken-root"
    (broken / "scripts" / "lib").mkdir(parents=True)
    (broken / "scripts" / "lib" / "cold_paths.py").write_text(
        "import sys\nprint('сверка стенда упала')\nsys.exit(3)\n", encoding="utf-8")
    archive = stand.archive(["CLAUDE.md"])
    updater.КОРЕНЬ = broken
    try:
        outcome = updater._разложить_и_проверить(
            lambda **_: (True, []), lambda: None, lambda: set(), set(), archive)
    except Exception as trouble:
        outcome = (None, None, f"исключение: {type(trouble).__name__}: {trouble}")
    check("сбой сверки холодных путей обновление не откатывает (причины отката нет)",
          outcome[2] is None, f"причина отката: {outcome[2]}")
    trace = archive / "cold-paths.txt"
    said = trace.read_text(encoding="utf-8") if trace.exists() else ""
    check("сбой сверки записан в архив прогона (cold-paths.txt)", "сверка не удалась (код 3)" in said,
          said[-200:] or "файла нет")

    updater.КОРЕНЬ = FLAG_CLI.parent.parent.parent
    stand.flag_cli("--request", "--reason", "обновление", "--detail", "CLAUDE.md")
    saved = {k: os.environ.get(k) for k in ("HARNESS_PANEL_STATE", "HARNESS_INSTALL_CONF", "HARNESS_CONF")}
    os.environ.update({k: stand.env()[k] for k in saved})
    try:
        updater.clear_rotation_request()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check("откат обновления: clear_rotation_request() снимает заявку", not (stand.panel / REQUEST_NAME).exists(),
          "заявка осталась")
    body = function_body(UPDATER.read_text(encoding="utf-8"), "_поставить_под_замком")
    rollback_at = body.find("rollback_of=forward_archive")
    clear_at = body.find("clear_rotation_request()", max(rollback_at, 0))
    check("_поставить_под_замком зовёт clear_rotation_request() на пути отката (после раскладки отката)",
          0 <= rollback_at < clear_at, f"раскладка отката @{rollback_at}, снятие заявки @{clear_at}")


def warden_request_check() -> None:
    """Сторож читает заявку и начинает ротацию с причиной «обновление» — стенд П1."""
    if not P1.is_file():
        check(f"стенд П1 есть ({P1})", False, "нет файла")
        return
    done = subprocess.run(["bash", str(P1)], capture_output=True, text=True, timeout=600,
                          env={**os.environ, "P1_VIA_REQUEST": "1"})
    last = (done.stdout.strip().splitlines() or ["пусто"])[-1]
    check("сторож по заявке «обновление» поднимает преемника, заявку снимает, причину переносит "
          "(П1, P1_VIA_REQUEST=1)", done.returncode == 0 and last.startswith("ПРОБА ЗЕЛЁНАЯ"),
          f"код {done.returncode}: {last}")


def main() -> int:
    if not UPDATER.is_file() or not WARDEN.is_file():
        print(f"ПРОБА СЛОМАНА: П8 — нет update.py ({UPDATER}) или сторожа ({WARDEN})")
        return 2
    stand = Stand()

    print("── данные: список холодных путей ──")
    check(f"файл масок холодных путей есть ({MASKS})", MASKS.is_file(), "нет файла")
    check(f"разборщик cold_paths.py есть ({TOOL})", TOOL.is_file(), "нет файла")

    if TOOL.is_file() and MASKS.is_file():
        print("── горячее: ротации не просит ──")
        rc, out, data = stand.run(HOT)
        check("только горячие файлы — код 0", rc == 0, f"код {rc}: {out[-300:]}")
        check("только горячие файлы — заявки ротации нет", data is None, json.dumps(data, ensure_ascii=False))

        print("── холодное: заявка с причиной «обновление» ──")
        for cold in ("harness/self/core.json", ".claude/settings.json", "CLAUDE.md", ".mcp.json"):
            rc, out, data = stand.run(HOT + [cold])
            check(f"задет {cold} — код 0", rc == 0, f"код {rc}: {out[-300:]}")
            check(f"задет {cold} — заявка с причиной «обновление…»", reason_of(data).startswith("обновление"),
                  json.dumps(data, ensure_ascii=False))
            check(f"задет {cold} — путь назван в заявке", cold in json.dumps(data or {}, ensure_ascii=False),
                  json.dumps(data, ensure_ascii=False))

    print("── заявка: один CLI ставит, читает, дописывает, снимает ──")
    request_cli_checks(stand)
    print("── update.py: сбой сверки и откат ──")
    updater_checks(stand)
    print("── сторож: заявка → ротация с причиной «обновление» ──")
    warden_request_check()

    print("── носители: обновлятор зовёт сверку, сторож читает заявку ──")
    body = function_body(UPDATER.read_text(encoding="utf-8"), "_разложить_и_проверить")
    if not body:
        print("ПРОБА СЛОМАНА: П8 — в update.py нет функции _разложить_и_проверить")
        return 2
    restart_at, cold_at = body.find("перезапустить()"), body.find("cold_paths")
    check("update.py: сверка с холодными (cold_paths) — после перезапуска служб в _разложить_и_проверить",
          0 <= restart_at < cold_at, f"перезапустить() @{restart_at}, cold_paths @{cold_at}")
    warden = WARDEN.read_text(encoding="utf-8")
    check("сторож читает заявку ротации (rotation-request / --request)",
          REQUEST_NAME in warden or "rotation_flag.py\" --request" in warden or "--request-state" in warden,
          "в session-warden.sh нет читателя заявки")

    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"ПРОБА КРАСНАЯ: П8 обновление — неудач {len(failed)} из {len(checks)}; первая: {failed[0][0]}")
        return 1
    print(f"ПРОБА ЗЕЛЁНАЯ: П8 обновление — путей {len(checks)}, горячее без ротации, холодное — заявка «обновление»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
