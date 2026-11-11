#!/usr/bin/env python3
"""Гейт: зов модели из кода не смеет отбирать канал у владельца.

Откуда взят. Правило CLAUDE.md — «НИКОГДА `claude -p` без
`--strict-mcp-config`: обрывает канал с владельцем» — до 13.09.2026 держалось
двумя носителями, и оба мимо демонов. Сторож команд (`guard_bash.py`) висит на
вызовах ОБОЛОЧКИ агента, а демона запускает cron; внимание смены забывает.
Замер того дня: из пяти живых зовов в дереве три шли БЕЗ флага —
`ton-watch.sh`, `devmap-selfheal.sh`, `check-builder.sh`. Каждый из них
ходит по расписанию ежедневно, и каждый запуск мог отобрать канал у живой
сессии владельца. В шапке `ton-watch.sh` при этом написано, что флаг
обязателен: комментарий говорил одно, код делал другое.

Почему по КОМАНДЕ, а не по строке. Первый замер той же смены смотрел построчно
и назвал нарушителями всех пятерых — у двоих флаг стоял на следующей строке,
за переносом `\\`. Ошибка замера родила бы правку, добавляющую флаг второй раз.
Поэтому продолжения строк склеиваются до разбора: команда живёт до конца
команды, а не до перевода строки.

Второе правило (hr.standards-and-policy §3.1, §4.5): модель зовут только через
носитель scripts/claude-run.sh. Личность смены и класс запуска даёт он один;
голый `claude` в коде приходит без них. Судится по языку: .sh — по словам
команды, .py — по ast, .js/.ts/.mjs — по spawn/execFile/exec. Первое правило
действует и на вызов через носитель: он флага сам не добавляет.

Прогон:
    python3 scripts/check-model-call.py [--selftest]
Код возврата: 0 — зовов мимо правил нет, 1 — найден (назван поимённо).
"""
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from config import корень_дерева  # noqa: E402

КОРЕНЬ = корень_дерева(__file__)
# Где ищем первым правилом: ТОЛЬКО файлы оболочки. В bash строка и есть команда, а в Python
# слова «claude -p» живут в текстах проб, в пояснениях и в самих гейтах,
# которые этот зов ищут, — первый прогон 13.09.2026 дал 14 находок, и все до
# одной ложные. Краснеющий гейт отключают в тот же день, поэтому домен узкий
# и честный: из Python модель зовут через scripts/ask-the-model.py, где флаг
# стоит в списке аргументов (строка 143) и виден глазами один раз навсегда.
ГДЕ = ("harness/demons", "scripts", "harness/panel")
РАСШИРЕНИЯ = (".sh",)
# Второе правило судит и другие языки.
РАСШИРЕНИЯ_ВТОРОГО = (".sh", ".py", ".js", ".ts", ".mjs")
ЗОВ = re.compile(r"claude\s+(?:-p|--print)\b")
# Вызов через носитель: он флага не добавляет, поэтому `-p` без флага — тот же зов.
ЗОВ_НОСИТЕЛЯ = re.compile(r"claude-run\.sh[\"']?[^\n;|&]*?\s(?:-p|--print)(?![\w-])")
ФЛАГ = "--strict-mcp-config"

# Белый список второго правила: путь от корня дерева → причина. Строка без
# причины не принимается. Каждая строка — не зов модели, а то, что ast
# не отличает от него; зов модели мимо носителя — незакрытый путь, не исключение; носитель (claude-run.sh) и файлы test_* пропускаются
# по имени, а не списком.
ИСКЛЮЧЕНИЯ: dict = {
    "scripts/extensions.py": "claude plugin …: три вызова выполнить() ходят только с "
                             "подкомандой plugin (управление плагинами, не модель); argv там "
                             "собран звёздочкой, и ast подкоманды не видит",
}
НОСИТЕЛЬ = "claude-run.sh"
# Не зов модели: подкоманды управления и справка.
НЕ_МОДЕЛЬ = ("plugin", "mcp", "auth", "doctor", "update", "install",
             "--version", "-v", "--help", "-h")

# Позиция команды в оболочке и прозрачные префиксы перед ней (exec, timeout, env…).
ПОЗИЦИЯ = (r"(?:^|[;&|(`]|\$\(|\b(?:then|do|else|elif|if|while|until)\s)\s*"
           r"(?:(?:[A-Za-z_]\w*=\S*|exec|command|sudo|nohup|setsid"
           r"|env(?:\s+-\S+)*|timeout(?:\s+-\S+)*\s+[\d.]+[smhd]?)\s+)*")
ГОЛЫЙ_CLAUDE = re.compile(
    ПОЗИЦИЯ + r"(?:(?:[\w.~$/{}\"'-]*/)?claude|npx\s+(?:-\S+\s+)*@anthropic-ai/claude-code)"
    r"(?=\s|;|&|$)(?!\s+(?:" + "|".join(НЕ_МОДЕЛЬ) + r")(?![\w-]))")
JS_ЗОВ = re.compile(
    r"\b(?:spawn|spawnSync|execFile|execFileSync|exec|execSync)\s*\(\s*[`'\"]"
    r"(?:[\w.~$/-]*/)?claude(?![\w.-])"
    r"(?!\s+(?:" + "|".join(НЕ_МОДЕЛЬ) + r")(?![\w-]))"
    r"(?![`'\"]\s*,\s*\[\s*[`'\"](?:" + "|".join(НЕ_МОДЕЛЬ) + r")[`'\"])")


def склеить(текст: str) -> str:
    """Продолжения строк — в одну строку: команда живёт до конца команды."""
    return re.sub(r"\\\n\s*", " ", текст)


def зовы_без_флага(текст: str) -> list:
    """Строки-команды, где модель зовут без флага. Комментарии не в счёт."""
    плохие = []
    for строка in склеить(_без_heredoc(текст)).splitlines():
        if строка.lstrip().startswith("#"):
            continue
        if (ЗОВ.search(строка) or ЗОВ_НОСИТЕЛЯ.search(строка)) and ФЛАГ not in строка:
            плохие.append(строка.strip())
    return плохие


def _без_heredoc(текст: str) -> str:
    """Тела heredoc — не команды: в них лежат документы и чужие скрипты."""
    итог, конец = [], None
    for строка in текст.splitlines():
        if конец is not None:
            if строка.strip() == конец:
                конец = None
            continue
        итог.append(строка)
        m = re.search(r"<<-?\s*[\"']?(\w+)[\"']?", строка)
        if m:
            конец = m.group(1)
    return "\n".join(итог)


def _без_комментария(строка: str) -> str:
    """Хвост `# …` вне кавычек отрезается: в комментарии разбор, а не команда."""
    кавычка = None
    for i, с in enumerate(строка):
        if кавычка:
            кавычка = None if с == кавычка else кавычка
        elif с in "\"'":
            кавычка = с
        elif с == "#" and (i == 0 or строка[i - 1] in " \t;"):
            return строка[:i]
    return строка


def мимо_носителя_sh(текст: str) -> list:
    плохие = []
    for строка in склеить(_без_heredoc(текст)).splitlines():
        команда = _без_комментария(строка)
        if ГОЛЫЙ_CLAUDE.search(команда):
            плохие.append(строка.strip())
    return плохие


def _первое_слово(узел, имена: dict):
    """Список argv или строка → (первый элемент, второй) констант; Name — по присваиванию."""
    if isinstance(узел, ast.Name):
        узел = имена.get(узел.id)
    if isinstance(узел, (ast.List, ast.Tuple)):
        слова = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
                 for e in узел.elts[:2]]
        return (слова + [None, None])[:2]
    if isinstance(узел, ast.Constant) and isinstance(узел.value, str):
        части = узел.value.split()
        return (части + [None, None])[:2] if части else (None, None)
    return (None, None)


def _claude_ли(слово) -> bool:
    return bool(слово) and (слово == "claude" or слово.endswith("/claude"))


ЗОВЫ_ПРОЦЕССА = {"run", "Popen", "call", "check_call", "check_output",
                 "system", "popen", "execv", "execvp", "execve", "execvpe",
                 "execl", "execlp", "execle", "execlpe", "spawnv", "spawnvp", "spawnl", "spawnlp"}


def мимо_носителя_py(текст: str) -> list:
    try:
        дерево = ast.parse(текст)
    except SyntaxError:
        return []
    имена = {}
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Assign) and isinstance(узел.value, (ast.List, ast.Tuple)):
            for цель in узел.targets:
                if isinstance(цель, ast.Name):
                    имена[цель.id] = узел.value
    плохие = []
    for узел in ast.walk(дерево):
        if not (isinstance(узел, ast.Call) and узел.args):
            continue
        имя = (узел.func.attr if isinstance(узел.func, ast.Attribute)
               else узел.func.id if isinstance(узел.func, ast.Name) else "")
        if имя not in ЗОВЫ_ПРОЦЕССА:
            continue
        первое, второе = _первое_слово(узел.args[0], имена)
        if not _claude_ли(первое):
            continue
        # os.execvp("claude", ["claude", …]): подкоманда — во втором аргументе.
        if isinstance(узел.args[0], ast.Constant) and len(узел.args) > 1:
            второе = _первое_слово(узел.args[1], имена)[1]
        if второе in НЕ_МОДЕЛЬ:
            continue
        плохие.append(f"{имя}(… {первое} …) строка {узел.lineno}")
    return плохие


def мимо_носителя_js(текст: str) -> list:
    строки = [с for с in текст.splitlines() if not с.lstrip().startswith(("//", "*", "/*"))]
    return [m.group(0) for m in JS_ЗОВ.finditer("\n".join(строки))]


СУДЫ_ВТОРОГО = {".sh": мимо_носителя_sh, ".py": мимо_носителя_py,
                ".js": мимо_носителя_js, ".ts": мимо_носителя_js, ".mjs": мимо_носителя_js}


def судится_вторым(файл: Path, корень: Path) -> bool:
    return (файл.suffix in РАСШИРЕНИЯ_ВТОРОГО and файл.name != НОСИТЕЛЬ
            and not файл.name.startswith("test_") and "node_modules" not in файл.parts
            and str(файл.relative_to(корень)) not in ИСКЛЮЧЕНИЯ)


def обойти_второе(корень: Path) -> list:
    """(файл, зов) для каждого вызова модели мимо носителя."""
    находки = []
    for каталог in ГДЕ:
        для_обхода = корень / каталог
        if not для_обхода.is_dir():
            continue
        for файл in sorted(для_обхода.rglob("*")):
            if not файл.is_file() or not судится_вторым(файл, корень):
                continue
            текст = файл.read_text(encoding="utf-8", errors="replace")
            for зов in СУДЫ_ВТОРОГО[файл.suffix](текст):
                находки.append((str(файл.relative_to(корень)), зов))
    return находки


def обойти(корень: Path) -> list:
    """(файл, команда) для каждого зова без флага во всём дереве харнеса."""
    находки = []
    for каталог in ГДЕ:
        для_обхода = корень / каталог
        if not для_обхода.is_dir():
            continue
        for файл in sorted(для_обхода.rglob("*")):
            if файл.suffix not in РАСШИРЕНИЯ or файл.name.startswith("test_"):
                continue
            текст = файл.read_text(encoding="utf-8", errors="replace")
            for команда in зовы_без_флага(текст):
                находки.append((str(файл.relative_to(корень)), команда))
    return находки


def selftest() -> int:
    неудач = 0

    def проба(имя, ждём, получено):
        nonlocal неудач
        ок = ждём == получено
        неудач += not ок
        print(f"  {'ок  ' if ок else 'ПЛОХО'} {имя}"
              + ("" if ок else f": ждали {ждём!r}, получили {получено!r}"))

    # БОЛЬНОЙ СЛУЧАЙ 13.09.2026, ради которого гейт написан.
    проба("зов без флага найден", 1,
          len(зовы_без_флага('claude -p --model opus "спроси"')))
    проба("зов с флагом чист", 0,
          len(зовы_без_флага('claude -p --strict-mcp-config --model opus "спроси"')))
    # БОЛЬНОЙ СЛУЧАЙ: флаг за переносом строки. Построчный замер той же смены
    # назвал такую команду нарушителем — и правка добавила бы флаг второй раз.
    проба("флаг за переносом строки виден", 0,
          len(зовы_без_флага('claude -p --model opus \\\n  --strict-mcp-config "спроси"')))
    проба("комментарий про claude -p не считается зовом", 0,
          len(зовы_без_флага('# claude -p без флага отбирает канал')))
    проба("--print судится так же, как -p", 1,
          len(зовы_без_флага('claude --print --model opus "спроси"')))
    # Не ловим чужое слово: `claude` внутри пути или имени переменной.
    проба("путь со словом claude зовом не считается", 0,
          len(зовы_без_флага('cat ~/.claude/settings.json')))

    проба("носитель с -p без флага найден", 1,
          len(зовы_без_флага('bash "$D/scripts/claude-run.sh" --класс прибор -p "x"')))
    проба("носитель с -p и флагом чист", 0,
          len(зовы_без_флага('bash scripts/claude-run.sh --класс прибор -p --strict-mcp-config "x"')))
    # Второе правило: модель только через носитель.
    проба("sh: голый claude найден", 1, len(мимо_носителя_sh('exec claude "$x"')))
    проба("sh: носитель не судится", 0,
          len(мимо_носителя_sh('bash scripts/claude-run.sh --класс прибор "x"')))
    проба("sh: claude --version не модель", 0, len(мимо_носителя_sh('claude --version')))
    проба("sh: heredoc не судится", 0, len(мимо_носителя_sh("cat <<'E'\nclaude -p x\nE\n")))
    проба("py: список claude в subprocess найден", 1,
          len(мимо_носителя_py('import subprocess\nsubprocess.run(["claude", "-p"])')))
    проба("py: список claude без вызова не судится", 0,
          len(мимо_носителя_py('x = ["claude", "-p"]')))
    проба("js: spawn claude найден", 1, len(мимо_носителя_js('spawn("claude", ["-p"])')))

    живые = обойти(КОРЕНЬ)
    print(f"  ок    живое дерево: зовов без флага {len(живые)}")
    print(f"САМОТЕСТ {'ПРОЙДЕН' if not неудач else 'ПРОВАЛЕН'}: 16 путей, "
          f"первым — больной случай")
    return 1 if неудач else 0


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    находки = обойти(КОРЕНЬ)
    мимо = обойти_второе(КОРЕНЬ)
    if мимо:
        print("[зов модели] модель зовут мимо носителя scripts/claude-run.sh:")
        for файл, зов in мимо:
            print(f"  ✗ {файл}: {зов[:110]}")
        print(f"\nнайдено {len(мимо)}; зовите через bash scripts/claude-run.sh --класс … -p --strict-mcp-config")
    if находки:
        print("[зов модели] зов без --strict-mcp-config отбирает канал у владельца:")
        for файл, команда in находки:
            print(f"  ✗ {файл}: {команда[:110]}")
        print(f"\nнайдено {len(находки)}; добавьте --strict-mcp-config в каждый зов")
    if находки or мимо:
        return 1
    print("[зов модели] чисто: модель зовут через носитель и со --strict-mcp-config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
