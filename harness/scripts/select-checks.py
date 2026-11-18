#!/usr/bin/env python3
"""Выбор проверок gates.sh, задетых изменёнными файлами (поезд выпусков, спека §7.2).

Зачем. Очередь слияния и ворота выпуска не должны гнать все проверки ради правки одного
скрипта, но и не должны пропускать проверку, которую правка могла сломать. Выбор — данные и
разбор, а не память о том, «что с чем связано»: имена проверок берутся РАЗБОРОМ gates.sh.

Проверка задета файлом F, если любое из правил:
  1. F — файл проверки (первый довод её команды) или его самотест `test_<имя>`;
  2. F входит во `входы:` механизма этого файла в mechanisms.yaml (так же читает кэш ворот)
     или во `входы_проб:` этого файла в release-train.yaml (их кэш не читает);
  3. (граф «кто зовёт» — фаза 4 поезда, здесь не построено);
  4. F — общий узел из `всегда_всё:` (release-train.yaml): задеты ВСЕ проверки;
  и для проверок без файла (`bash -c '…'`): путь F упомянут в тексте команды.

Берутся проверки обычного прогона gates.sh. Тяжёлые — раздел `--всё` — идут только по
флагу `--всё` или названные во `входы_проб:`; тогда запускать `gates.sh --только "<имена>"
--всё` (порядок важен).

`--поезд` — выбор для ворот выпуска: без правила 4 и без тяжёлых, кроме названных во
`входы_проб:`. Диапазон дня почти
всегда задевает gates.sh или gates_check.sh, и правило 4 делало бы ворота поезда полным
прогоном на часы; правку общего узла судит его собственная проверка (правила 1 и 2) в
поезде, а «всё» — очередь слияния в момент правки и ночной полный прогон.

    select-checks.py --диапазон A..B [--имена] [--поезд]
    select-checks.py --файлы f1 f2 … [--имена] [--поезд]
    select-checks.py --selftest

Код 0 — выбор готов (пустой выбор — пустая строка); 2 — разбора проверок нет, ключ
`входы_проб:` не называет проверку или git не ответил: это провал, а не «ничего не задето».
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATES = "scripts/gates.sh"
MECHANISMS = "harness/config/mechanisms.yaml"
TRAIN = "harness/config/release-train.yaml"

INTERPRETERS = {"python3", "python", "bash", "sh"}
# Имя в кавычках без подстановки: `проверка "$NAME"` — динамическое имя, его в разборе нет.
DECLARATION = re.compile(r'^\s*проверка\s+"([^"$]+)"\s*(.*)$')
# Раздел gates.sh по флагу — от `if` в нулевой колонке до первого `fi` в нулевой. «--всё» —
# тяжёлые проверки; «--selftest» — проверки-подделки самотеста ворот («падающая»), настоящими
# они не являются и в выбор попасть не должны; остальные разделы флагов (--хакатон) — не обычный прогон.
SECTION_OPEN = re.compile(r'^if \[ "\$\{1:-\}" = "(--[\w-]+)" \]; then\s*$')
FILE_LIKE = re.compile(r"[\w./-]+")


@dataclass(frozen=True)
class Check:
    name: str
    words: tuple
    text: str
    heavy: bool


def split_words(command: str) -> tuple:
    # Команда с незакрытой кавычкой не должна ронять разбор: слова тогда грубые,
    # но проверка остаётся в списке и ловится правилом упоминания в тексте.
    try:
        return tuple(shlex.split(command, comments=True))
    except ValueError:
        return tuple(command.split())


def read_command(first: str, lines: list, start: int) -> tuple:
    """Команда проверки с продолжениями через «\\». Возвращает (текст, следующая строка)."""
    command, i = first, start
    while command.endswith("\\") and i < len(lines):
        command = command[:-1] + " " + lines[i].strip()
        i += 1
    return command, i


def parse_checks(gates_text: str) -> list:
    """Проверки gates.sh в порядке объявления. Разбор, а не список: список разошёлся бы с файлом."""
    checks, section, i = [], None, 0
    lines = gates_text.splitlines()
    while i < len(lines):
        line = lines[i]
        i += 1
        opened = SECTION_OPEN.match(line)
        if opened:
            section = opened.group(1)
        elif section and re.match(r"fi\b", line):
            section = None
        found = DECLARATION.match(line)
        if found:
            command, i = read_command(found.group(2), lines, i)
            if section in (None, "--всё"):
                checks.append(Check(found.group(1), split_words(command), command, section == "--всё"))
    return checks


def check_file(words: tuple) -> str | None:
    """Файл проверки — первый довод после интерпретатора; у `bash -c '…'` файла нет."""
    rest = list(words)
    while rest and "=" in rest[0] and not rest[0].startswith("-"):
        rest.pop(0)                      # префикс среды: NAME=значение
    if rest and rest[0] in INTERPRETERS:
        rest.pop(0)
    if not rest or rest[0].startswith("-") or not FILE_LIKE.fullmatch(rest[0]):
        return None
    return rest[0]


def selftest_names(path: str) -> set:
    """Самотест проверки лежит рядом и зовётся test_<имя>: дефис в имени становится «_»."""
    stem = Path(path).stem.replace("-", "_")
    folder = str(Path(path).parent)
    return {f"{folder}/test_{stem}{ext}" for ext in (".py", ".sh")}


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def mechanism_inputs(root: Path) -> dict:
    """Файл механизма → множество путей его входов. Шаблоны раскрывает тот же код, что у кэша ворот."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gates_cache  # noqa: PLC0415 — каталог скриптов добавлен строкой выше
    result = {}
    for mech in load_yaml(root / MECHANISMS).get("механизмы") or []:
        if not mech.get("файл"):
            continue
        found = set()
        for pattern in mech.get("входы") or []:
            found.update(os.path.relpath(p, root) for p in gates_cache.expand(str(pattern), str(root)))
        result[mech["файл"]] = found
    return result


def rule_file(check: Check, f: str, inputs: dict) -> bool:
    return f == check_file(check.words)


def rule_selftest(check: Check, f: str, inputs: dict) -> bool:
    own = check_file(check.words)
    return own is not None and f in selftest_names(own)


def rule_inputs(check: Check, f: str, inputs: dict) -> bool:
    own = check_file(check.words)
    return own is not None and f in inputs["mechanism"].get(own, ())


def rule_probe_inputs(check: Check, f: str, inputs: dict) -> bool:
    own = check_file(check.words)
    return own is not None and f in inputs["probes"].get(own, ())


def rule_mention(check: Check, f: str, inputs: dict) -> bool:
    return check_file(check.words) is None and f in check.text


# Здесь же встанет правило 3 (граф «кто зовёт», фаза 4): ещё одна строка с функцией той же
# сигнатуры. Пока его нет — его нет в списке, а не стоит пустой заглушкой.
RULES = (
    ("файл проверки", rule_file),
    ("самотест", rule_selftest),
    ("входы механизма", rule_inputs),
    ("входы проб", rule_probe_inputs),
    ("упомянут в команде", rule_mention),
)


def first_reason(check: Check, files: list, inputs: dict):
    for f in files:
        for label, rule in RULES:
            if rule(check, f, inputs):
                return label, f
    return None


def select(checks: list, files: list, always_all: list, inputs: dict, heavy: bool) -> list:
    """Строки (проверка, правило, файл) в порядке gates.sh."""
    regular = [c for c in checks if heavy or not c.heavy]
    shared = next((f for f in files if f in always_all), None)
    if shared is not None:
        return [(c, "всегда_всё", shared) for c in regular]
    # Пробу, названную в данных судьёй узла, выбрал человек, а не догадка правил: тяжесть её не
    # снимает, иначе правку двери согласия в поезде не судил бы никто.
    pool = [c for c in checks if c in regular or check_file(c.words) in inputs["probes"]]
    rows = []
    for check in pool:
        reason = first_reason(check, files, inputs)
        if reason:
            rows.append((check, *reason))
    return rows


def changed_files(root: Path, span: str) -> list:
    out = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(root), "diff", "--name-only", "-z", span],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def run(root: Path, files: list, heavy: bool, train: bool = False) -> list:
    checks = parse_checks((root / GATES).read_text(encoding="utf-8"))
    if not checks:
        raise LookupError(f"в {GATES} не разобрано ни одной проверки")
    train_data = load_yaml(root / TRAIN)
    always_all = [] if train else train_data.get("всегда_всё") or []
    inputs = {"mechanism": mechanism_inputs(root), "probes": train_data.get("входы_проб") or {}}
    # Ключ без проверки с таким файлом (проба переименована или удалена) молча не выбирал бы ничего.
    stale = sorted(set(inputs["probes"]) - {check_file(c.words) for c in checks})
    if stale:
        raise LookupError(f"входы_проб в {TRAIN}: нет проверки с файлом {', '.join(stale)}")
    return select(checks, files, always_all, inputs, heavy and not train)


def render(rows: list, names_only: bool) -> str:
    if names_only:
        return "|".join(c.name for c, _, _ in rows)
    return "\n".join(f"{c.name} ← {label} ← {f}" for c, label, f in rows)


def cli(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--диапазон", dest="span")
    parser.add_argument("--файлы", dest="files", nargs="+")
    parser.add_argument("--имена", dest="names", action="store_true")
    parser.add_argument("--всё", dest="heavy", action="store_true")
    parser.add_argument("--поезд", dest="train", action="store_true")
    parser.add_argument("--корень", dest="root", default=str(ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        files = args.files if args.files is not None else changed_files(root, args.span)
        rows = run(root, files, args.heavy, args.train)
    except (LookupError, subprocess.CalledProcessError) as err:
        print(f"select-checks: {err}", file=sys.stderr)
        return 2
    print(render(rows, args.names))
    return 0


# ── самотест ─────────────────────────────────────────────────────────────────

STAND_GATES = """\
if [ "${1:-}" = "--selftest" ]; then
    проверка "падающая" bash -c 'exit 3'
    exit 0
fi
проверка "альфа" python3 scripts/alpha.py --flag
проверка "бета" bash scripts/beta.sh
# проверка "закомментированная" python3 scripts/ghost.py
проверка "гамма" bash -c 'echo scripts/gamma-thing.py; exit 0'
проверка "дельта" python3 scripts/check-delta.py
проверка "длинная" python3 scripts/long.py \\
    --продолжение scripts/tail-arg.py
проверка "с средой" HARNESS_X=1 python3 scripts/envy.py
проверка "самотест обёртки" bash scripts/lib/gates_check.sh --selftest
if [ "${1:-}" = "--всё" ]; then
    проверка "тяжёлая" python3 scripts/heavy.py
    проверка "тяжёлая проба двери" python3 scripts/test_door.py
fi
проверка "после тяжёлых" python3 scripts/after.py
"""
STAND_MECHANISMS = """\
механизмы:
- файл: scripts/alpha.py
  входы:
  - data/alpha-*.json
- файл: scripts/beta.sh
"""
STAND_TRAIN = """\
всегда_всё:
  - scripts/lib/gates_check.sh
  - scripts/gates.sh
входы_проб:
  scripts/beta.sh: [scripts/lib/door.py]
  scripts/check-delta.py: [scripts/lib/door.py]
  scripts/test_door.py: [scripts/lib/door.py]
"""
REGULAR = ["альфа", "бета", "гамма", "дельта", "длинная", "с средой", "самотест обёртки", "после тяжёлых"]


def make_stand(root: Path, gates: str = STAND_GATES, train: str = STAND_TRAIN) -> None:
    for rel, text in ((GATES, gates), (MECHANISMS, STAND_MECHANISMS), (TRAIN, train),
                      ("data/alpha-1.json", "{}"), ("docs/readme.md", "x")):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def names_for(root: Path, files: list, heavy: bool = False, train: bool = False) -> list:
    return [c.name for c, _, _ in run(root, files, heavy, train)]


def cli_in(root: Path, *args: str):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--корень", str(root), *args],
                          capture_output=True, text=True)


def git_in(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def selftest_cases(root: Path, empty: Path, repo: Path, stale: Path) -> list:
    """(описание, условие) — больной случай первым."""
    n = lambda files, heavy=False, train=False: names_for(root, files, heavy, train)  # noqa: E731
    cli_names = cli_in(root, "--файлы", "scripts/beta.sh", "scripts/alpha.py", "--имена")
    nothing = cli_in(root, "--файлы", "docs/readme.md", "--имена")
    broken = cli_in(empty, "--файлы", "scripts/beta.sh", "--имена")
    ranged = cli_in(repo, "--диапазон", "HEAD~1..HEAD", "--имена")
    table = cli_in(root, "--файлы", "data/alpha-1.json")
    train_cli = cli_in(root, "--файлы", "scripts/lib/gates_check.sh", "scripts/heavy.py", "--имена", "--поезд", "--всё")
    probe_table = cli_in(root, "--файлы", "scripts/lib/door.py", "--поезд")
    stale_key = cli_in(stale, "--файлы", "docs/readme.md", "--поезд")
    live = cli_in(ROOT, "--файлы", "docs/readme.md", "--поезд")
    return [
        ("БОЛЬНОЙ: правка gates_check.sh — выбор «всё» обычного прогона, без тяжёлых",
         n(["scripts/lib/gates_check.sh"]) == REGULAR),
        ("правка gates.sh — тоже «всё»", n(["scripts/gates.sh"]) == REGULAR),
        ("БОЛЬНОЙ: --поезд, правка gates_check.sh — не «всё», а её собственный самотест",
         n(["scripts/lib/gates_check.sh"], train=True) == ["самотест обёртки"]),
        ("--поезд: правка gates.sh без своей проверки — пусто, не «всё»",
         n(["scripts/gates.sh"], train=True) == []),
        ("БОЛЬНОЙ: --поезд, правка библиотеки двери — пробы, названные в входы_проб",
         n(["scripts/lib/door.py"], train=True) == ["бета", "дельта", "тяжёлая проба двери"]),
        ("входы_проб и без --поезд", n(["scripts/lib/door.py"]) == ["бета", "дельта", "тяжёлая проба двери"]),
        ("таблица: входы_проб названы своим правилом",
         probe_table.stdout.strip() == "бета ← входы проб ← scripts/lib/door.py\nдельта ← входы проб ← scripts/lib/door.py"
                                       "\nтяжёлая проба двери ← входы проб ← scripts/lib/door.py"),
        ("--поезд: тяжёлая не берётся и с флагом --всё",
         n(["scripts/heavy.py"], heavy=True, train=True) == []),
        ("--поезд в командной строке: самотест обёртки, без тяжёлой",
         train_cli.returncode == 0 and train_cli.stdout.strip() == "самотест обёртки"),
        ("«всё» с флагом --всё берёт и тяжёлую",
         "тяжёлая" in n(["scripts/gates.sh"], heavy=True)),
        ("файл проверки — её имя", n(["scripts/beta.sh"]) == ["бета"]),
        ("самотест test_<имя> — её имя", n(["scripts/test_beta.sh"]) == ["бета"]),
        ("самотест при дефисе в имени: test_check_delta.py", n(["scripts/test_check_delta.py"]) == ["дельта"]),
        ("входы механизма — имя", n(["data/alpha-1.json"]) == ["альфа"]),
        ("документ — пусто", n(["docs/readme.md"]) == []),
        ("bash -c: путь в тексте команды", n(["scripts/gamma-thing.py"]) == ["гамма"]),
        ("команда с продолжением строки: файл первого довода", n(["scripts/long.py"]) == ["длинная"]),
        ("префикс среды не мешает найти файл", n(["scripts/envy.py"]) == ["с средой"]),
        ("закомментированная проверка не разобрана", n(["scripts/ghost.py"]) == []),
        ("проверки-подделки раздела --selftest не берутся даже при «всё»",
         "падающая" not in n(["scripts/gates.sh"], heavy=True)),
        ("тяжёлая без флага не берётся", n(["scripts/heavy.py"]) == []),
        ("тяжёлая с флагом берётся", n(["scripts/heavy.py"], heavy=True) == ["тяжёлая"]),
        ("проверка после раздела тяжёлых — обычная", n(["scripts/after.py"]) == ["после тяжёлых"]),
        ("--имена: имена через «|» в порядке gates.sh, ровно для --только",
         cli_names.returncode == 0 and cli_names.stdout.strip() == "альфа|бета"),
        ("пустой выбор — пустая строка и код 0",
         nothing.returncode == 0 and nothing.stdout.strip() == ""),
        ("пустой разбор проверок — код 2", broken.returncode == 2),
        ("диапазон git: файлы из git diff", ranged.returncode == 0 and ranged.stdout.strip() == "бета"),
        ("таблица: проверка ← правило ← файл",
         table.stdout.strip() == "альфа ← входы механизма ← data/alpha-1.json"),
        ("БОЛЬНОЙ: ключ входы_проб без проверки с таким файлом — код 2 и имя ключа",
         stale_key.returncode == 2 and "scripts/gone.py" in stale_key.stderr),
        ("боевой release-train.yaml: каждый ключ входы_проб — файл проверки gates.sh",
         bool(load_yaml(ROOT / TRAIN).get("входы_проб")) and live.returncode == 0),
    ]


def selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="select-checks-") as tmp:
        root, empty, repo = Path(tmp, "стенд"), Path(tmp, "пустой"), Path(tmp, "репо")
        stale = Path(tmp, "устаревший-ключ")
        make_stand(root)
        make_stand(empty, gates="# проверок нет\n")
        make_stand(stale, train=STAND_TRAIN + "  scripts/gone.py: [scripts/lib/door.py]\n")
        make_stand(repo)
        git_in(repo, "init", "-q")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-q", "-m", "один")
        (repo / "scripts").mkdir(exist_ok=True)
        (repo / "scripts/beta.sh").write_text("echo\n", encoding="utf-8")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-q", "-m", "два")
        cases = selftest_cases(root, empty, repo, stale)
    failed = [name for name, ok in cases if not ok]
    for name in failed:
        print(f"САМОТЕСТ ПРОВАЛЕН: {name}")
    if failed:
        return 1
    print(f"САМОТЕСТ ПРОЙДЕН: {len(cases)} случаев (больной первым: правка gates_check.sh — выбор «всё»)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    sys.exit(cli(sys.argv[1:]))
