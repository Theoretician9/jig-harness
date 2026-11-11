#!/usr/bin/env python3
"""Проба гейта check-model-call.py: модель зовут только через носитель.

Спека hr.standards-and-policy §3.1, §4.5. Гейт держал одно правило — `claude -p`
в .sh без --strict-mcp-config. Второе правило: вызов модели МИМО
scripts/claude-run.sh — красный в .sh (словом команды, полным путём, после
exec/timeout/env, через npx), в .py (список с первым элементом «claude» или
«…/claude», в том числе через переменную) и в .js панели (spawn/execFile/exec).
Первое правило распространяется и на носитель (Н-4 контракта, K28): носитель
флага не добавляет, и `claude-run.sh … -p` без --strict-mcp-config в .sh
отбирает канал у владельца так же, как голый claude.

Чтобы находка второго правила не пришла от первого, во всех вызовах мимо
носителя флаг --strict-mcp-config СТОИТ.

Каждый случай — свой стенд-дерево (копия гейта и scripts/lib, один файл в
scripts/ или harness/panel/): судится код возврата гейта на одном файле, а не
разбор его вывода. Дверь мутаций: CHECK_MODEL_CALL_UNDER_TEST — путь к копии.

    python3 scripts/test_check_model_call.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = Path(os.environ.get("CHECK_MODEL_CALL_UNDER_TEST") or HERE / "check-model-call.py")

STRICT = "--strict-mcp-config"

# (имя случая, путь в стенде, текст файла)
FINDS = [
    ("sh: интерактивный claude без -p", "scripts/a.sh", 'claude "$x"\n'),
    ("sh: полный путь /usr/bin/claude", "scripts/a.sh", f"/usr/bin/claude -p {STRICT} x\n"),
    ("sh: путь ~/.local/bin/claude", "scripts/a.sh", f"~/.local/bin/claude -p {STRICT} x\n"),
    ("sh: exec claude", "scripts/a.sh", f"exec claude -p {STRICT} x\n"),
    ("sh: timeout 5 claude", "scripts/a.sh", f"timeout 5 claude -p {STRICT} x\n"),
    ("sh: env A=1 claude", "scripts/a.sh", f"env A=1 claude -p {STRICT} x\n"),
    ("sh: npx @anthropic-ai/claude-code", "scripts/a.sh",
     f"npx @anthropic-ai/claude-code -p {STRICT} x\n"),
    ("sh: claude с флагом, но мимо носителя", "scripts/a.sh", f'claude -p {STRICT} "x"\n'),
    ("sh: в демоне — тот же суд", "harness/demons/a.sh", f"claude -p {STRICT} x\n"),
    ("Н-4: носитель с -p без --strict-mcp-config", "scripts/a.sh",
     'bash "$PROJECT_DIR/scripts/claude-run.sh" --класс прибор -p "x"\n'),
    ("Н-4: носитель с --print без --strict-mcp-config", "scripts/a.sh",
     'bash scripts/claude-run.sh --класс прибор --print "x"\n'),
    ("py: subprocess.run([\"claude\", …])", "scripts/a.py",
     f'import subprocess\nsubprocess.run(["claude", "-p", "{STRICT}", "x"])\n'),
    ("py: список в переменной, путь …/claude, Popen", "scripts/a.py",
     f'import subprocess\ncmd = ["/x/claude", "-p", "{STRICT}"]\nsubprocess.Popen(cmd)\n'),
    ("py: os.execvp(\"claude\", …)", "scripts/a.py",
     f'import os\nos.execvp("claude", ["claude", "-p", "{STRICT}"])\n'),
    ("py: check_output", "scripts/a.py",
     f'import subprocess\nsubprocess.check_output(["claude", "-p", "{STRICT}", "x"])\n'),
    ("js: spawn(\"claude\", …) в панели", "harness/panel/a.js",
     f'const {{ spawn }} = require("child_process");\nspawn("claude", ["-p", "{STRICT}"]);\n'),
    ("js: execFile(\"claude\", …) в панели", "harness/panel/a.js",
     f'const cp = require("child_process");\ncp.execFile("claude", ["-p", "{STRICT}"]);\n'),
]

PASSES = [
    ("sh: носитель с -p и --strict-mcp-config", "scripts/a.sh",
     f'bash "$PROJECT_DIR/scripts/claude-run.sh" --класс прибор -p {STRICT} "x"\n'),
    ("sh: носитель, флаг за переносом строки", "scripts/a.sh",
     f'bash scripts/claude-run.sh --класс демон -p \\\n  {STRICT} "x"\n'),
    ("sh: claude plugin list", "scripts/a.sh", "claude plugin list\n"),
    ("sh: claude mcp list", "scripts/a.sh", "claude mcp list\n"),
    ("sh: claude auth status", "scripts/a.sh", "claude auth status\n"),
    ("sh: claude --version", "scripts/a.sh", "claude --version\n"),
    ("sh: claude --help", "scripts/a.sh", "claude --help\n"),
    ("sh: claude -p в комментарии", "scripts/a.sh", "# claude -p x — так звать нельзя\necho ок\n"),
    ("sh: claude -p в тексте heredoc", "scripts/a.sh",
     "cat <<'EOF'\nclaude -p x\nEOF\necho ок\n"),
    ("sh: путь со словом claude", "scripts/a.sh", "cat ~/.claude/settings.json\n"),
    ("py: строка «claude -p» в тексте", "scripts/a.py", 'print("claude -p x")\n'),
    ("py: claude в комментарии", "scripts/a.py", '# subprocess.run(["claude", "-p"])\nx = 1\n'),
    ("py: вызов через носитель", "scripts/a.py",
     'import subprocess\nsubprocess.run(["bash", "/x/scripts/claude-run.sh", "--класс", "прибор",'
     f' "-p", "{STRICT}", "x"])\n'),
    ("py: список claude, но не вызов процесса", "scripts/a.py", 'names = ["claude", "-p"]\nprint(names)\n'),
    ("js: spawn носителя в панели", "harness/panel/a.js",
     'const { spawn } = require("child_process");\n'
     f'spawn("bash", ["scripts/claude-run.sh", "--класс", "прибор", "-p", "{STRICT}"]);\n'),
    ("файл test_* не судится (подставные claude проб)", "scripts/test_a.sh",
     f"claude -p {STRICT} x\n"),
]


def stand_run(rel: str, text: str) -> tuple[int, str]:
    """Стенд-дерево с одним файлом: (код гейта, вывод)."""
    with tempfile.TemporaryDirectory(prefix="sap-f2-cmc-") as tmp:
        root = Path(tmp)
        (root / "scripts" / "lib").mkdir(parents=True)
        (root / "harness" / "config").mkdir(parents=True)
        (root / "harness" / "demons").mkdir(parents=True)
        (root / "harness" / "panel").mkdir(parents=True)
        shutil.copy(GATE, root / "scripts" / "check-model-call.py")
        for lib in (HERE / "lib").glob("*.py"):
            shutil.copy(lib, root / "scripts" / "lib" / lib.name)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        p = subprocess.run([sys.executable, str(root / "scripts" / "check-model-call.py")],
                           capture_output=True, text=True, timeout=60, cwd=tmp)
        return p.returncode, p.stdout + p.stderr


def main() -> int:
    if not GATE.is_file():
        print(f"ПРОБА СЛОМАНА: нет {GATE}")
        return 1
    failed = total = 0

    # Пустой стенд обязан быть чистым: иначе красное ниже пришло бы от стенда.
    total += 1
    rc, out = stand_run("scripts/empty.sh", "echo ок\n")
    if rc == 0:
        print("  ок    стенд без вызовов модели — гейт чист")
    else:
        failed += 1
        print(f"  ПЛОХО стенд без вызовов модели красный (код {rc}): {out.strip()[:200]}")

    for name, rel, text in FINDS:
        total += 1
        rc, out = stand_run(rel, text)
        named = Path(rel).name in out
        if rc == 1 and named:
            print(f"  ок    находит: {name}")
        else:
            failed += 1
            print(f"  ПЛОХО не находит: {name} — код {rc}, файл в выводе назван: {named}")

    for name, rel, text in PASSES:
        total += 1
        rc, out = stand_run(rel, text)
        if rc == 0:
            print(f"  ок    пропускает: {name}")
        else:
            failed += 1
            print(f"  ПЛОХО ложная находка: {name} — код {rc}: {out.strip()[:200]}")

    print(f"\nитог test_check_model_call: случаев {total}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
