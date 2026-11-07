#!/usr/bin/env python3
"""Пять «бумажных» гейтов коммита — до `git commit`, на том же индексе.

Зачем. Замер gates.jsonl за октябрь 2026: из ~420 отказов pre-commit ~330 дали
пять гейтов — сверка с пакетом, прополка карты, пайплайн, гигиена контекста,
схема карты. Каждый такой отказ — повторный круг помощника с контекстом в
десятки тысяч токенов. precheck показывает их сразу и все пять, а хук
останавливается на первом.

Один судья. Своей копии правил и даже своей копии команд здесь нет: команда
каждого гейта читается из ЕГО строки в `pre-commit-gate.sh` (по имени гейта,
тому же, что пишется в gates.jsonl) и переводится в вызов дословно —
переменные хука подставляются, валидатор схемы, живущий в хуке строкой,
зовётся через дверь хука `--devmap-only`. Поменяли строку в хуке — precheck
позовёт по-новому; строки не нашлось — precheck отказывается (код 2), а не
молчит.

Судится ИНДЕКС, как у коммита: сначала `git add`, потом precheck, потом commit.
Гейты, которые хук зовёт из снимка индекса ($SNAP_DIR), и здесь идут из снимка.

Дверь обхода пайплайна (HARNESS_PIPELINE_FORCE) здесь не открывается: гейт с
открытой дверью пишет строку в журнал обходов, и она закрыла бы долг задачи без
всякого коммита. Отказ precheck покажет, обход запишет сам коммит.

    python3 scripts/precheck.py [--подробно] [--предел СЕКУНД]
Код возврата: 0 — коммит эти пять гейтов пройдёт; 1 — хотя бы один откажет;
2 — precheck не смог прочесть хук или репозиторий.
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HOOK = SCRIPT_DIR / "pre-commit-gate.sh"

# Имена гейтов — как в хуке и в gates.jsonl. Порядок — порядок в хуке.
GATE_NAMES = ("context_hygiene", "прополка_карты", "пайплайн",
              "devmap_schema", "package_sync")

# Валидатор, который хук держит строкой, и дверь хука, которая зовёт его же.
INLINE_DOORS = {"devmap_check": "--devmap-only"}

# Обход не должен оставить след без коммита (см. шапку).
STRIPPED_ENV = ("HARNESS_PIPELINE_FORCE", "HARNESS_PIPELINE_WHY")

DEFAULT_LIMIT = 30.0


class HookError(Exception):
    """Строку хука не нашли или не смогли перевести в вызов."""


def hook_lines(text: str) -> list[str]:
    return [s.strip() for s in text.splitlines()
            if s.strip() and not s.strip().startswith("#")]


def hook_value(lines: list[str], pattern: str, what: str) -> re.Match:
    for line in lines:
        found = re.match(pattern, line)
        if found:
            return found
    raise HookError(f"в хуке нет строки {what}")


def gate_line(lines: list[str], name: str) -> str:
    marks = (f'fail "{name}"', f'гейт "{name}"')
    found = [s for s in lines if any(m in s for m in marks)]
    if len(found) != 1:
        raise HookError(f"строк гейта «{name}» в хуке {len(found)}, нужна одна")
    return found[0]


def parse_gate(line: str, name: str) -> dict:
    """Строка хука → {argv-шаблон, stdin-переменная, жёлтый разрешён}."""
    stdin_var = None
    piped = re.match(r"""^printf '%s\\n' "\$(\w+)" \| (.*)$""", line)
    if piped:
        stdin_var, line = piped.group(1), piped.group(2)
    # Жёлтый код щадит только гейты, идущие через функцию `гейт`; строка
    # `|| fail` валит коммит на любом ненулевом коде.
    yellow = line.startswith(f'гейт "{name}" ')
    if yellow:
        line = line[len(f'гейт "{name}" '):]
    else:
        line = re.sub(rf'\s*\|\|\s*fail "{re.escape(name)}"$', "", line)
    argv = shlex.split(line)
    for inline, door in INLINE_DOORS.items():
        if argv[:1] == [inline]:
            argv = ["bash", "$SCRIPT_DIR/" + HOOK.name, door] + argv[1:]
    return {"name": name, "argv": argv, "stdin": stdin_var, "yellow": yellow}


def staged_list(lines: list[str], root: Path) -> str:
    found = hook_value(lines, r"^CHANGED=\$\((.*)\)$", "CHANGED=$(…)")
    proc = subprocess.run(shlex.split(found.group(1)), cwd=root,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise HookError(f"список индекса не прочитан: {proc.stderr.strip()}")
    return proc.stdout


def index_snapshot(root: Path, stage_tmp: Path) -> Path:
    snap = stage_tmp / "snapshot"
    proc = subprocess.run(["git", "checkout-index", "-a", f"--prefix={snap}/"],
                          cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HookError(f"снимок индекса не снят: {proc.stderr.strip()}")
    return snap


def expand(argv: list[str], values: dict[str, str], staged: set[str],
           root: Path, stage_tmp: Path) -> list[str] | None:
    """Подставить переменные хука. None — гейт судит файл, которого нет в
    индексе: хук его в этом случае не зовёт, и precheck тоже."""
    out = []
    for token in argv:
        if token.startswith("$STAGE_TMP/"):
            rel = token[len("$STAGE_TMP/"):]
            if rel not in staged:
                return None
            copy = stage_tmp / rel
            copy.parent.mkdir(parents=True, exist_ok=True)
            with open(copy, "wb") as fh:
                if subprocess.run(["git", "show", f":{rel}"], cwd=root,
                                  stdout=fh).returncode != 0:
                    raise HookError(f"{rel} не прочитан из индекса")
            out.append(str(copy))
            continue

        def put(found: re.Match) -> str:
            var = found.group(1)
            if var not in values:
                raise HookError(f"переменная хука ${var} в «{' '.join(argv)}» — не знаю, чем её заменить")
            return values[var]
        out.append(re.sub(r"\$\{?(\w+)\}?", put, token))
    return out


def run_gate(argv: list[str], stdin: str | None, root: Path, limit: float):
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    proc = subprocess.Popen(argv, cwd=root, env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = proc.communicate(stdin or "", timeout=limit)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out


def reason(out: str) -> tuple[str, int]:
    """Первая строка с ✗, иначе первая непустая; и сколько строк осталось."""
    lines = [s.strip() for s in out.splitlines() if s.strip()]
    if not lines:
        return "вывода нет", 0
    marked = [s for s in lines if "✗" in s]
    return (marked or lines)[0], len(lines) - 1


def main(argv: list[str]) -> int:
    verbose = "--подробно" in argv
    limit = DEFAULT_LIMIT
    if "--предел" in argv:
        limit = float(argv[argv.index("--предел") + 1])
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        print("[precheck] не git-репозиторий: судить нечего")
        return 2
    root = Path(top.stdout.strip())
    try:
        lines = hook_lines(HOOK.read_text(encoding="utf-8"))
        yellow_code = int(hook_value(lines, r"^YELLOW_CODE=(\d+)$", "YELLOW_CODE=").group(1))
        gates = [parse_gate(gate_line(lines, n), n) for n in GATE_NAMES]
        changed = staged_list(lines, root)
    except (OSError, HookError) as err:
        print(f"[precheck] ОТКАЗ РАБОТАТЬ: {err} ({HOOK})")
        return 2

    staged = {s for s in changed.splitlines() if s}
    print(f"[precheck] индекс: {len(staged)} файл(ов); пять гейтов коммита, предел {limit:g} с на гейт")
    if any(os.environ.get(k) for k in STRIPPED_ENV):
        print("[precheck] HARNESS_PIPELINE_FORCE задан: здесь обход не открываю — "
              "отказ пайплайна покажу, обход запишет сам коммит")
    refused = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            values = {"SCRIPT_DIR": str(SCRIPT_DIR), "PWD": str(root), "STAGE_TMP": tmp,
                      "SNAP_DIR": str(index_snapshot(root, Path(tmp)))}
            commands = [expand(gate["argv"], values, staged, root, Path(tmp)) for gate in gates]
        except HookError as err:
            print(f"[precheck] ОТКАЗ РАБОТАТЬ: {err}")
            return 2
        for gate, cmd in zip(gates, commands):
            if cmd is None:
                print(f"  пропущен {gate['name']} — его файла нет в индексе, коммит его не судит")
                continue
            t0 = time.monotonic()
            rc, out = run_gate(cmd, changed if gate["stdin"] == "CHANGED" else None,
                               root, limit)
            spent = time.monotonic() - t0
            first, more = reason(out)
            tail = f" (ещё {more} стр.: --подробно)" if more and not verbose else ""
            if rc == 0:
                print(f"  прошёл   {gate['name']}  {spent:.1f} с")
                continue
            if rc is None:
                refused += 1
                print(f"  отказ    {gate['name']}  {spent:.1f} с — не уложился в {limit:g} с")
            elif rc == yellow_code and gate["yellow"]:
                print(f"  жёлтый   {gate['name']}  {spent:.1f} с — {first}{tail}")
            else:
                refused += 1
                print(f"  отказ    {gate['name']}  {spent:.1f} с — {first}{tail}")
            if verbose:
                print("\n".join("      " + s for s in out.splitlines()))
    total = time.monotonic() - started
    if refused:
        print(f"[precheck] ИТОГ: отказ {refused} из {len(gates)} — коммит будет отменён; {total:.1f} с")
        return 1
    print(f"[precheck] ИТОГ: пять гейтов коммита пройдут; {total:.1f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
