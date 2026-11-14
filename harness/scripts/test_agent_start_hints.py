#!/usr/bin/env python3
"""Проба: подсказки запуска агента ведут через носитель личности, а не через голый claude (Р5).

Живой стенд 13.11.2026: первая сессия
чистой установки по подсказке установщика — `cd <проект> && claude …`. Голый
claude стартует без --append-system-prompt-file: ни ядра, ни стандартов, ни роли
оркестратора. Законный запуск — scripts/start-agent.sh (оборачивает команду в
claude-run.sh --класс оркестратор) или сам claude-run.sh.

Судятся тексты, которые человек или агент берут как команду:
  * блок «ОСТАЛОСЬ РУКАМИ» установщика — ВЫВОД функции осталось_руками
    (извлечена разбором USTANOVIT.sh, прогнана для обоих вариантов канала, с
    harness.conf пакета: команда запуска приходит из AGENT_START_CMD);
  * блоки ```bash ИНСТРУКЦИЯ-ВЛАДЕЛЬЦУ.md;
  * строки-сообщения (echo, say, alarm_once, скажи) hooks/session_state.sh и
    demons/session-warden.sh.
Подсказка запуска — строка, где claude стоит командой (начало строки, после
&&, ;, |, exec) и за ним флаг, кавычка, комментарий или конец строки, либо где
назван start-agent.sh / claude-run.sh. `claude -p`/`--print` — разовый вызов
для проверки входа, не запуск агента, и не судится. В каждом источнике
подсказок ≥ 1 (пустой разбор — провал), голых — 0.

Ещё (ревью #5, Н3): подсказка, данная там, где сессия tmux agent УЖЕ есть, обязана
что-то делать. `start-agent.sh` без режима на живой сессии говорит «уже есть — не
трогаю» и выходит.
  * живьём: `start-agent.sh --в-панель` на своей сессии (свой сервер tmux через
    TMUX_TMPDIR стенда, AGENT_START_CMD="sleep 30") — в панели оболочка → команда
    введена, в панели sleep; в панели не оболочка (cat) → ничего не введено.
    Зелёной её оставил бы режим, который поднимает новую сессию под другим
    именем, — судится панель ТОЙ сессии;
  * «сессия есть» в текстах: блок «г)» установщика (агент выпал в оболочку),
    сообщения hooks/session_state.sh (хук идёт в живой сессии) и
    demons/session-warden.sh (агент жив, но не там) — start-agent.sh только с
    режимом --в-панель или --команда (или systemctl restart harness-agent);
  * строки установщика с claude-run.sh содержат ровно команду komanda_agenta
    (lib/config.sh: env -u носителя чужого входа, as-account.sh) — набранная
    руками команда идёт мимо неё. Ожидаемая команда считается самой
    komanda_agenta от AGENT_START_CMD пакета. Перед осталось_руками проба
    подключает lib/config.sh из HARNESS_SRC стенда (там же лежит start-agent.sh).
    Команда komanda_agenta напечатана в каждом варианте (запасная строка
    «start-agent.sh --команда» — только когда её не посчитать);
  * `start-agent.sh --команда` печатает ту же komanda_agenta(AGENT_START_CMD) стенда.

Дверь мутаций: START_HINTS_ROOT — корень пакета под пробой.
Прогон: python3 scripts/test_agent_start_hints.py   Код: 0 — все случаи прошли.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

CARRIER = re.compile(r"start-agent\.sh|claude-run\.sh")
BARE = re.compile(r"(?:^|&&|;|\||\bexec)\s*claude(?=\s+-|\s*$|\s*[\"'`#»])")
ONE_SHOT = re.compile(r"\bclaude\s+(?:-p|--print)\b")
# start-agent.sh без режима, действующего на живую сессию.
IDLE_START = re.compile(r"start-agent\.sh(?!\s+--(?:в-панель|команда)(?:\W|$))")
RESTART_UNIT = re.compile(r"systemctl\s+restart\s+harness-agent")
EMITTER = re.compile(r"^\s*(?:echo|say|alarm_once|скажи|printf)\b|\b(?:alarm_once|say|echo)\s+")

cases = Cases()


def package_root() -> Path:
    if os.environ.get("START_HINTS_ROOT"):
        return Path(os.environ["START_HINTS_ROOT"])
    beside = HERE.parent.parent
    if (beside / "USTANOVIT.sh").is_file():
        return beside
    import config
    return config.katalog_paketa()


def hints(lines: list) -> tuple:
    """(подсказки запуска, из них голые)."""
    found = [l.strip() for l in lines if CARRIER.search(l) or (BARE.search(l) and not ONE_SHOT.search(l))]
    bare = [l for l in found if BARE.search(l) and not ONE_SHOT.search(l)]
    return found, bare


def extract(source: str, name: str) -> str:
    out, on = [], False
    for line in source.splitlines():
        if line.startswith(f"{name}() {{") or line.startswith(f"{name}()     {{"):
            on = True
        if on:
            out.append(line)
            if line == "}" or (line.startswith(f"{name}()") and line.rstrip().endswith("}")):
                break
    return "\n".join(out)


def installer_src(pkg: Path, root: Path) -> Path:
    """HARNESS_SRC стенда: harness.conf, lib/ и start-agent.sh пакета."""
    src = root / "harness"
    if not src.is_dir():
        (src / "config").mkdir(parents=True)
        shutil.copy2(pkg / "harness" / "config" / "harness.conf", src / "config" / "harness.conf")
        shutil.copytree(pkg / "harness" / "scripts" / "lib", src / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(pkg / "harness" / "scripts" / "start-agent.sh", src / "scripts" / "start-agent.sh")
    return src


def installer_env(root: Path, variant: str) -> dict:
    return dict(os.environ, HARNESS_SRC=str(root / "harness"), PROJECT_DIR=str(root / "proj"),
                AGENT_USER="жилец", LOG_DIR=str(root / "log"), TG_CHANNEL_VARIANT=variant)


def installer_variants(pkg: Path, root: Path) -> dict:
    """{вариант канала: строки вывода осталось_руками}."""
    source = (pkg / "USTANOVIT.sh").read_text(encoding="utf-8")
    body = "\n".join(extract(source, f) for f in ("заголовок", "скажи", "осталось_руками"))
    if "осталось_руками()" not in body:
        return {}
    lib = root / "lib.sh"
    lib.write_text(body + "\n", encoding="utf-8")
    config_sh = installer_src(pkg, root) / "scripts" / "lib" / "config.sh"
    out = {}
    for variant in ("A", "B"):
        done = subprocess.run(["bash", "-c", f'MANUAL_LIST=(); . "{config_sh}"; . "{lib}"; осталось_руками'],
                              env=installer_env(root, variant), capture_output=True, text=True, timeout=60)
        out[variant] = done.stdout.splitlines()
    return out


def installer_block(pkg: Path, root: Path) -> list:
    return [l for lines in installer_variants(pkg, root).values() for l in lines]


def doc_code_lines(path: Path) -> list:
    lines, inside = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            inside = not inside and line.strip() in ("```bash", "```sh", "```")
            continue
        if inside:
            lines.append(line)
    return lines


def message_lines(path: Path) -> list:
    return [l for l in path.read_text(encoding="utf-8").splitlines() if EMITTER.search(l)]


def judge(name: str, lines: list) -> None:
    found, bare = hints(lines)
    cases.check(f"{name}: подсказки запуска разобраны (не пусто)", True, len(found) > 0)
    cases.check(f"{name}: голого claude нет — только start-agent.sh / claude-run.sh", [], bare[:5])


def selfcheck() -> None:
    print("── разбор подсказок ──")
    samples = {
        "cd /p && claude --dangerously-skip-permissions": True,
        "cd /p && claude": True,
        "  cd /p && $X && claude   # внутри": True,
        "перезапусти: cd /p && claude\"": True,
        "claude --channels x": True,
        "bash /p/scripts/start-agent.sh": False,
        "claude запускайте только от agent": False,
        "печатать claude или node": False,
        "bash -c 'cd ~ && claude -p \"ответь\"'": False,
    }
    for text, is_bare in samples.items():
        cases.check(f"«{text}» голый={is_bare}", is_bare, bool(hints([text])[1]))


def section_g(lines: list) -> list:
    """Строки блока «г)» (агент жив? выпал в оболочку) — до следующего пункта «д)…» или конца."""
    out, on = [], False
    for line in lines:
        if re.match(r"^\s*[а-яё]\)\s", line):
            on = line.lstrip().startswith("г)")
        if on:
            out.append(line)
    return out


def judge_live_session(name: str, lines: list) -> None:
    """Сессия agent уже есть: подсказка обязана действовать на неё."""
    found = [l.strip() for l in lines if "start-agent.sh" in l or RESTART_UNIT.search(l)]
    cases.check(f"{name} (сессия есть): подсказки разобраны (не пусто)", True, len(found) > 0)
    idle = [l for l in found if IDLE_START.search(l)]
    cases.check(f"{name} (сессия есть): start-agent.sh только с --в-панель / --команда", [], idle[:3])


def agent_command(pkg: Path, root: Path) -> str:
    """komanda_agenta(AGENT_START_CMD пакета) в окружении стенда установщика."""
    src = installer_src(pkg, root)
    (root / "install.conf").write_text("", encoding="utf-8")
    env = installer_env(root, "B")
    env.pop("AGENT_START_CMD", None)   # окружение старше файла: чужое значение подменило бы пакетное
    env.update(HARNESS_INSTALL_CONF=str(root / "install.conf"), HARNESS_CONF=str(src / "config" / "harness.conf"))
    done = subprocess.run(["bash", "-c", f'. "{src}/scripts/lib/config.sh"; konf_zagruzit AGENT_START_CMD; '
                           '[ -n "${AGENT_START_CMD:-}" ] && komanda_agenta "$AGENT_START_CMD"'],
                          env=env, capture_output=True, text=True, timeout=60)
    return done.stdout.strip() if done.returncode == 0 else ""


def check_installer_command(pkg: Path, root: Path) -> None:
    """USTANOVIT.sh не набирает команду запуска мимо komanda_agenta."""
    expected = agent_command(pkg, root)
    cases.check("komanda_agenta посчитана от AGENT_START_CMD пакета (не пусто)", True, bool(expected))
    variants = installer_variants(pkg, root)
    for variant, lines in sorted(variants.items()):
        typed = [l.strip() for l in lines if "claude-run.sh" in l and (not expected or expected not in l)]
        cases.check(f"USTANOVIT.sh вариант {variant}: claude-run.sh только командой komanda_agenta", [], typed[:3])
        # Запасная строка («start-agent.sh --команда») законна только когда komanda_agenta не дошла.
        cases.check(f"USTANOVIT.sh вариант {variant}: команда komanda_agenta напечатана", True,
                    bool(expected) and any(expected in l for l in lines))
        judge_live_session(f"USTANOVIT.sh вариант {variant} блок «г)»", section_g(lines))


def tmux(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True, timeout=30)


def pane_command(env: dict, session: str, want: str) -> str:
    """Команда панели; ждёт want до 5 с (ввод доходит не сразу)."""
    current = ""
    for _ in range(25):
        current = tmux(env, "display-message", "-p", "-t", session, "#{pane_current_command}").stdout.strip()
        if current == want:
            break
        time.sleep(0.2)
    return current


def check_into_pane(pkg: Path, root: Path) -> None:
    """start-agent.sh --в-панель на живой сессии: оболочка → команда введена; не оболочка → не тронута."""
    if not shutil.which("tmux"):
        cases.check("tmux есть — живой случай --в-панель", True, False)
        return
    stand = root / "панель"
    for d in ("tmux", "proj", "log"):
        (stand / d).mkdir(parents=True)
    session = "стенд-подсказки"
    (stand / "install.conf").write_text(
        f'TMUX_SESSION="{session}"\nPROJECT_DIR="{stand / "proj"}"\nLOG_DIR="{stand / "log"}"\n', encoding="utf-8")
    (stand / "harness.conf").write_text('AGENT_START_CMD="sleep 30"\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMUX", "TMUX_PANE", "TMUX_SESSION", "PROJECT_DIR", "LOG_DIR", "AGENT_START_CMD")}
    env.update(TMUX_TMPDIR=str(stand / "tmux"), HARNESS_INSTALL_CONF=str(stand / "install.conf"),
               HARNESS_CONF=str(stand / "harness.conf"), KICK_CMD="true", HARNESS_LOG_DIR=str(stand / "log"))
    script = pkg / "harness" / "scripts" / "start-agent.sh"
    lib = pkg / "harness" / "scripts" / "lib" / "config.sh"
    want = subprocess.run(["bash", "-c", f'. "{lib}"; konf_zagruzit AGENT_START_CMD; komanda_agenta "$AGENT_START_CMD"'],
                          env=env, capture_output=True, text=True, timeout=60).stdout.strip()
    said = subprocess.run(["bash", str(script), "--команда"], env=env, capture_output=True, text=True, timeout=60)
    cases.check("--команда печатает команду komanda_agenta(AGENT_START_CMD) стенда и код 0",
                (0, want), (said.returncode, said.stdout.strip()))
    cases.check("--команда — условие: komanda_agenta стенда не пуста", True, bool(want))
    try:
        tmux(env, "new-session", "-d", "-s", session, "-c", str(stand / "proj"))
        shell = pane_command(env, session, "bash")
        done = subprocess.run(["bash", str(script), "--в-панель"], env=env, capture_output=True, text=True, timeout=60)
        now = pane_command(env, session, "sleep")
        cases.check("--в-панель: в панели живой сессии была оболочка → введена команда агента (sleep)",
                    (shell, "sleep"), (shell, now))
        if now != "sleep":
            print(f"      вывод start-agent.sh: {(done.stdout + done.stderr).strip()[-200:]!r}")
        tmux(env, "kill-session", "-t", session)

        typed = stand / "ввод"
        typed.touch()
        tmux(env, "new-session", "-d", "-s", session, "-c", str(stand / "proj"), f"exec cat > {typed}")
        busy = pane_command(env, session, "cat")
        subprocess.run(["bash", str(script), "--в-панель"], env=env, capture_output=True, text=True, timeout=60)
        time.sleep(1)
        cases.check("--в-панель: в панели не оболочка (cat) → ничего не введено",
                    ("cat", "cat", ""), (busy, pane_command(env, session, "cat"), typed.read_text(encoding="utf-8")))
    finally:
        tmux(env, "kill-server")


def main() -> int:
    pkg = package_root()
    root = Path(tempfile.mkdtemp(prefix="harness-run-start-hints-"))
    try:
        selfcheck()
        print(f"── источники пакета {pkg} ──")
        judge("USTANOVIT.sh «ОСТАЛОСЬ РУКАМИ»", installer_block(pkg, root))
        judge("ИНСТРУКЦИЯ-ВЛАДЕЛЬЦУ.md", doc_code_lines(pkg / "ИНСТРУКЦИЯ-ВЛАДЕЛЬЦУ.md"))
        judge("hooks/session_state.sh", message_lines(pkg / "harness" / "hooks" / "session_state.sh"))
        judge("demons/session-warden.sh", message_lines(pkg / "harness" / "demons" / "session-warden.sh"))
        print("── сессия agent уже есть (Н3) ──")
        judge_live_session("hooks/session_state.sh", message_lines(pkg / "harness" / "hooks" / "session_state.sh"))
        judge_live_session("demons/session-warden.sh",
                           message_lines(pkg / "harness" / "demons" / "session-warden.sh"))
        check_installer_command(pkg, root)
        check_into_pane(pkg, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"итог test_agent_start_hints: случаев {cases.paths}, неудач {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
