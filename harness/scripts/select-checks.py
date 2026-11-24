#!/usr/bin/env python3
"""Выбор проверок gates.sh, задетых изменёнными файлами (поезд выпусков, спека §7.2).

Зачем. Очередь слияния и ворота выпуска не должны гнать все проверки ради правки одного
скрипта, но и не должны пропускать проверку, которую правка могла сломать. Выбор — данные и
разбор, а не память о том, «что с чем связано»: имена проверок берутся РАЗБОРОМ gates.sh.

Проверка задета файлом F, если любое из правил:
  1. F — файл проверки (первый довод её команды) или его самотест `test_<имя>`;
  2. F входит во `входы:` механизма этого файла в mechanisms.yaml (так же читает кэш ворот)
     или во `входы_проб:` этого файла в release-train.yaml (их кэш не читает);
  3. граф «кто зовёт» (check-who-calls.py): F — файл, который файл проверки зовёт сам или
     через звено (глубина ≤ 2: импорт, исполнитель, строка-путь; комментарий не вызов);
  Общий узел из `всегда_всё:` (release-train.yaml) судят только правила 1–3, как любой файл;
  граф через такой узел не раскрывается (он звено-посредник, а не вызов проверкой);
  и для проверок без файла (`bash -c '…'`): путь F упомянут в тексте команды.

Берутся проверки обычного прогона gates.sh. Тяжёлые — раздел `--всё` — идут только по
флагу `--всё` или названные во `входы_проб:`; тогда запускать `gates.sh --только "<имена>"
--всё` (порядок важен).

`--поезд` — выбор для ворот выпуска: без тяжёлых, кроме названных во `входы_проб:`.

`--очередь` — выбор очереди слияния: проверки из `очередь_без_входов:` (release-train.yaml)
правило 2 «входы механизма» не выбирает. Их входы — весь scripts/** (кэш прав: им нужна вся
раскладка), и выбор брал их на каждую подачу; без своих файлов в диапазоне их судит
nightly-gates (`gates.sh --всё`).

    select-checks.py --диапазон A..B [--имена] [--поезд]
    select-checks.py --файлы f1 f2 … [--имена] [--поезд] [--очередь]
    select-checks.py --selftest

Код 0 — выбор готов (пустой выбор — пустая строка); 2 — разбора проверок нет, ключ
`входы_проб:` не называет проверку или git не ответил: это провал, а не «ничего не задето».
"""
from __future__ import annotations

import argparse
import importlib.util
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
    # Перепись всего дерева (вход `scripts/**`) не различает ветки: её судит ночь, а не выбор.
    return (own is not None and own not in inputs["outside"] and check.name not in inputs["own_only"]
            and f in inputs["mechanism"].get(own, ()))


def rule_probe_inputs(check: Check, f: str, inputs: dict) -> bool:
    own = check_file(check.words)
    return own is not None and f in inputs["probes"].get(own, ())


def rule_called(check: Check, f: str, inputs: dict) -> bool:
    own = check_file(check.words)
    return own is not None and f in inputs["calls"].get(own, ())


def rule_mention(check: Check, f: str, inputs: dict) -> bool:
    return check_file(check.words) is None and f in check.text


RULES = (
    ("файл проверки", rule_file),
    ("самотест", rule_selftest),
    ("входы механизма", rule_inputs),
    ("входы проб", rule_probe_inputs),
    ("упомянут в команде", rule_mention),
    ("кто зовёт", rule_called),
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


# Без ключа в данных — глубина 2: звено «файл → вызванный» и ещё одно за ним.
DEFAULT_CALL_DEPTH = 2


def index_lines(text: str) -> dict:
    """Слово → строки, где оно стоит: форма вызова без имени файла не бывает, поэтому регулярки
    считаются только по строкам с именем, а не по всем парам файлов (минуты на живом дереве)."""
    index = {}
    for line in text.splitlines():
        for word in set(re.findall(r"[\w-]+", line)):
            index.setdefault(word, []).append(line)
    return index


def call_graph(root: Path, starts: set, depth: int, hubs: set = frozenset()) -> dict:
    """Файл проверки → файлы, которые он зовёт, глубина ≤ depth, обход в ширину.

    Звено «A зовёт B» — то же, что считает check-who-calls.py: в тексте A без комментариев
    есть форма вызова имени B. Множество посещённых режет цикл (B зовёт A обратно).
    """
    spec = importlib.util.spec_from_file_location("check_who_calls", Path(__file__).resolve().parent / "check-who-calls.py")
    who = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(who)
    paths = who.файлы_населения(root)
    texts = {str(p.relative_to(root)): who.без_слов(p.read_text(encoding="utf-8", errors="replace"), p.suffix)
             for p in paths}
    lines_by_word = {rel: index_lines(text) for rel, text in texts.items()}
    by_word = {}
    for rel in texts:
        by_word.setdefault(Path(rel).name.split(".")[0], []).append(rel)
    edges = {}

    def calls(rel: str, callee: str) -> bool:
        lines = lines_by_word[rel].get(Path(callee).name.split(".")[0])
        if not lines:
            return False
        name = Path(callee).name
        forms = [(label, rf"""["'][^"'\n]*/{re.escape(name)}["']""" if label == "строкой-путём" else pattern)
                 for label, pattern in who.формы_вызова(name)]
        return any(re.search(pattern, "\n".join(lines), re.M) for _, pattern in forms)

    def callees(rel: str) -> list:
        if rel not in edges:
            named = (g for word in lines_by_word[rel] for g in by_word.get(word, ()))
            edges[rel] = sorted(g for g in set(named) if g != rel and calls(rel, g))
        return edges[rel]

    graph = {}
    for start in starts & set(texts):
        seen, level = {start}, [start]
        for _ in range(depth):
            level = [g for rel in level if rel == start or rel not in hubs
                     for g in callees(rel) if g not in seen]
            seen.update(level)
        graph[start] = seen - {start}
    return graph


def run(root: Path, files: list, heavy: bool, train: bool = False, queue: bool = False) -> list:
    checks = parse_checks((root / GATES).read_text(encoding="utf-8"))
    if not checks:
        raise LookupError(f"в {GATES} не разобрано ни одной проверки")
    train_data = load_yaml(root / TRAIN)
    always_all = train_data.get("всегда_всё") or []
    inputs = {"mechanism": mechanism_inputs(root), "probes": train_data.get("входы_проб") or {},
              "outside": set(train_data.get("вне_выбора") or ()),
              "own_only": set(train_data.get("очередь_без_входов") or []) if queue else set()}
    # Ключ без проверки с таким файлом (проба переименована или удалена) молча не выбирал бы ничего.
    stale = sorted(set(inputs["probes"]) - {check_file(c.words) for c in checks})
    if stale:
        raise LookupError(f"входы_проб в {TRAIN}: нет проверки с файлом {', '.join(stale)}")
    files_of_checks = {check_file(c.words) for c in checks}
    unknown = sorted(inputs["outside"] - files_of_checks)
    if unknown:
        raise LookupError(f"вне_выбора в {TRAIN}: нет проверки с файлом {', '.join(unknown)}")
    depth = train_data.get("выбор_глубина_вызовов", DEFAULT_CALL_DEPTH)
    inputs["calls"] = call_graph(root, files_of_checks - {None}, depth, set(always_all))
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
    parser.add_argument("--очередь", dest="queue", action="store_true")
    parser.add_argument("--корень", dest="root", default=str(ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        files = args.files if args.files is not None else changed_files(root, args.span)
        rows = run(root, files, args.heavy, args.train, args.queue)
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
проверка "зовёт обёртку" bash scripts/uses-wrapper.sh
проверка "эпсилон" python3 scripts/epsilon.py
проверка "имена-данные" python3 scripts/check-names.py
проверка "эта" bash scripts/check-eta.sh
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
# Граф «кто зовёт» (правило 3): цепочка от файла альфы. Глубина 1 — импорт, 2 — исполнитель
# в строке-пути, 3 — дальше предела; последнее звено зовёт файл альфы обратно (цикл не
# должен зацикливать обход). У дельты форма вызова стоит в комментарии: без снятия
# комментариев она звеном выбрала бы дельту.
STAND_CALLS = {
    "scripts/alpha.py": "import alpha_lib\n\nalpha_lib.run()\n",
    "scripts/alpha_lib.py": ('import subprocess\n\n\ndef run():\n'
                             '    subprocess.run(["bash", "scripts/alpha-deep.sh"], check=True)\n'),
    "scripts/alpha-deep.sh": "#!/usr/bin/env bash\npython3 scripts/alpha-deepest.py\n",
    "scripts/alpha-deepest.py": 'import subprocess\n\nsubprocess.run(["python3", "scripts/alpha.py"])\n',
    "scripts/check-delta.py": "# python3 scripts/alpha_lib.py\nprint('дельта')\n",
    # Общий узел (К-1 ревью фазы 4): обёртку зовёт файл проверки — правка узла выбирает его.
    "scripts/lib/gates_check.sh": "#!/usr/bin/env bash\nproverka() { :; }\n",
    "scripts/uses-wrapper.sh": "#!/usr/bin/env bash\nsource scripts/lib/gates_check.sh\n",
    # Строка-данные (И-1): множество голых имён — не вызов, хотя имя в кавычках (как `долг`
    # в lib/config.py). Строка-путь с каталогом — вызов: её форма должна остаться звеном.
    "scripts/check-names.py": ('import subprocess\n\nimport names_data\n\n'
                               'RUNNER = "scripts/kappa.sh"\n'
                               'subprocess.run(["bash", RUNNER] if names_data.DEBT else [], check=False)\n'),
    "scripts/names_data.py": 'DEBT = {"epsilon.py", "other-name.sh"}\n',
    "scripts/epsilon.py": "print('эпсилон')\n",
    "scripts/kappa.sh": "#!/usr/bin/env bash\necho kappa\n",
    # Перевод строки (Н-5): кавычка одной строки и `имя"` другой — не строка-путь. Обе строки
    # несут слово zeta, поэтому индекс строк склеивает их через «\n». Имя в первой строке —
    # вне кавычек: `'zeta'` было бы формой importlib, то есть звеном по делу.
    "scripts/check-eta.sh": "#!/usr/bin/env bash\necho zeta '%s'\ncat <<EOF\nzeta: scripts/zeta.py\"\nEOF\n",
    "scripts/zeta.py": "print('z')\n",
}
# Узел gates.sh, который зовёт файл проверки (И-1, второй узел): через узел граф не ходит,
# иначе правка любой проверки тянет всех, кто зовёт gates.sh. Свой стенд — в общем такой
# файл менял бы выбор старых случаев.
HUB_GATES_LINE = 'проверка "зовёт ворота" bash scripts/runs-gates.sh\n'
HUB_FILES = {"scripts/runs-gates.sh": "#!/usr/bin/env bash\nbash scripts/gates.sh --только альфа\n"}

# Глубина графа — данные: через узел-хаб (tg_send.sh
# зовут 76 файлов) глубина 2 брала ветке из 12 файлов 67 проверок, глубина 1 — 34. Цепочка
# A → H → F: при глубине 1 из release-train.yaml правка F проверку A не задевает, правка H — да.
DEPTH_FILES = {
    GATES: 'проверка "А" python3 scripts/check-a.py\n',
    MECHANISMS: "механизмы: []\n",
    "scripts/check-a.py": "import h_mid\n\nh_mid.run()\n",
    "scripts/h_mid.py": ('import subprocess\n\n\ndef run():\n'
                         '    subprocess.run(["bash", "scripts/f_leaf.sh"], check=True)\n'),
    "scripts/f_leaf.sh": "#!/usr/bin/env bash\necho f\n",
}
DEPTH_TRAIN = "выбор: {{ потолок_мин: 30 }}\nвыбор_глубина_вызовов: {depth}\nвсегда_всё: []\n"
# Переписи всего дерева (вход `scripts/**`) вне выбора: такой вход не различает ветки — любая
# правка scripts/ брала «кто зовёт» и «обновление» (~30 мин на двоих при потолке 30). Список —
# данными; «обычная» с тем же входом, но не в списке, остаётся выбираемой: вычёркивает список,
# а не шаблон `**`.
CENSUS_FILES = {
    GATES: 'проверка "перепись" python3 scripts/check-census.py\nпроверка "обычная" python3 scripts/plain.py\n',
    MECHANISMS: ('механизмы:\n- файл: scripts/check-census.py\n  входы: ["scripts/**"]\n'
                 '- файл: scripts/plain.py\n  входы: ["scripts/**"]\n'),
    "scripts/check-census.py": "print('перепись')\n",
    "scripts/plain.py": "print('обычная')\n",
    "scripts/x.py": "print('x')\n",
}
CENSUS_TRAIN = "выбор: { потолок_мин: 30 }\nвсегда_всё: []\nвне_выбора:\n  - scripts/check-census.py\n"
CENSUS_STALE_TRAIN = "выбор: { потолок_мин: 30 }\nвсегда_всё: []\nвне_выбора:\n  - scripts/nobody.py\n"


def make_files(root: Path, files: dict) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_stand(root: Path, gates: str = STAND_GATES, train: str = STAND_TRAIN) -> None:
    for rel, text in ((GATES, gates), (MECHANISMS, STAND_MECHANISMS), (TRAIN, train),
                      ("data/alpha-1.json", "{}"), ("docs/readme.md", "x"), *STAND_CALLS.items()):
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


def narrow_cases(depth1: Path, depth2: Path, census: Path, census_stale: Path) -> list:
    """Выбор не раздут хабами и переписями всего дерева."""
    stale_out = cli_in(census_stale, "--файлы", "scripts/x.py")
    return [
        ("БОЛЬНОЙ: глубина 1 из release-train.yaml — правка F (A зовёт H, H зовёт F) не выбирает A",
         names_for(depth1, ["scripts/f_leaf.sh"]) == []),
        ("глубина 1 из данных: правка H (A зовёт H) выбирает A",
         names_for(depth1, ["scripts/h_mid.py"]) == ["А"]),
        ("глубина 2 из данных: правка F выбирает A — глубина читается из данных, не константа",
         names_for(depth2, ["scripts/f_leaf.sh"]) == ["А"]),
        ("БОЛЬНОЙ: перепись `scripts/**` из вне_выбора — правка постороннего scripts/x.py её не "
         "выбирает (обычная с тем же входом — выбирает)",
         names_for(census, ["scripts/x.py"]) == ["обычная"]),
        ("вне_выбора: правка САМОГО файла переписи выбирает её (правило «файл проверки»)",
         "перепись" in names_for(census, ["scripts/check-census.py"])),
        ("БОЛЬНОЙ: ключ вне_выбора без проверки с таким файлом — код 2 и имя файла",
         stale_out.returncode == 2 and "scripts/nobody.py" in stale_out.stderr),
    ]


def selftest_cases(root: Path, empty: Path, repo: Path, stale: Path, hub: Path, own: Path) -> list:
    """(описание, условие) — больной случай первым."""
    n = lambda files, heavy=False, train=False: names_for(root, files, heavy, train)  # noqa: E731
    n_hub = lambda files, train=False: names_for(hub, files, False, train)  # noqa: E731
    shared_table = cli_in(root, "--файлы", "scripts/lib/gates_check.sh")
    cli_names = cli_in(root, "--файлы", "scripts/beta.sh", "scripts/alpha.py", "--имена")
    nothing = cli_in(root, "--файлы", "docs/readme.md", "--имена")
    broken = cli_in(empty, "--файлы", "scripts/beta.sh", "--имена")
    ranged = cli_in(repo, "--диапазон", "HEAD~1..HEAD", "--имена")
    table = cli_in(root, "--файлы", "data/alpha-1.json")
    train_cli = cli_in(root, "--файлы", "scripts/lib/gates_check.sh", "scripts/heavy.py", "--имена", "--поезд", "--всё")
    probe_table = cli_in(root, "--файлы", "scripts/lib/door.py", "--поезд")
    stale_key = cli_in(stale, "--файлы", "docs/readme.md", "--поезд")
    live = cli_in(ROOT, "--файлы", "docs/readme.md", "--поезд")
    graph_table = cli_in(root, "--файлы", "scripts/alpha-deep.sh")
    own_inputs = cli_in(own, "--файлы", "data/alpha-1.json", "--имена", "--очередь")
    own_file = cli_in(own, "--файлы", "scripts/alpha.py", "--имена", "--очередь")
    own_train = cli_in(own, "--файлы", "data/alpha-1.json", "--имена")
    return [
        # 21.11.2026: «обновление» и «население кода» выбирались входами scripts/** на каждую
        # подачу очереди — 950–2100 с дважды; без своих файлов их судит ночь.
        ("БОЛЬНОЙ: --очередь — проверку из очередь_без_входов входы механизма не выбирают",
         own_inputs.returncode == 0 and own_inputs.stdout.strip() == ""),
        ("--очередь: свой файл проверки из очередь_без_входов выбирает её",
         own_file.returncode == 0 and own_file.stdout.strip() == "альфа"),
        ("без --очередь ключ очередь_без_входов не действует: входы выбирают",
         own_train.returncode == 0 and own_train.stdout.strip() == "альфа"),
        # К-1 (решение оркестратора 20.11, вариант 2): «всё» шло 32–87 мин при потолке 30 —
        # ветку на общий узел очередь не сливала никогда. Узел судят правила 1–3, промах — ночь.
        ("БОЛЬНОЙ: правка gates_check.sh без --поезд — не «всё», а её самотест и звавшие её",
         n(["scripts/lib/gates_check.sh"]) == ["самотест обёртки", "зовёт обёртку"]),
        ("таблица: общий узел назван правилами 1–3, не «всегда_всё»",
         shared_table.returncode == 0
         and shared_table.stdout.strip() == "самотест обёртки ← файл проверки ← scripts/lib/gates_check.sh"
                                            "\nзовёт обёртку ← кто зовёт ← scripts/lib/gates_check.sh"),
        ("правка gates.sh без звавших — пусто, не «всё»", n(["scripts/gates.sh"]) == []),
        ("правка gates.sh с --всё — не «всё»: тяжёлая не берётся", n(["scripts/gates.sh"], heavy=True) == []),
        ("правка gates.sh, который зовёт файл проверки, — только звавшая",
         n_hub(["scripts/gates.sh"]) == ["зовёт ворота"]),
        # И-1: живьём 2 файла давали 173 проверки (013fd791..b4d9304c) ложными звеньями.
        ("БОЛЬНОЙ: множество голых имён в файле данных — не вызов: правка эпсилона не тянет "
         "проверку, импортирующую файл данных", n(["scripts/epsilon.py"]) == ["эпсилон"]),
        ("строка-путь с каталогом — звено: правка kappa.sh выбирает проверку, где путь в строке",
         n(["scripts/kappa.sh"]) == ["имена-данные"]),
        ("БОЛЬНОЙ: через узел gates.sh граф не ходит — правка файла проверки не тянет звавшую "
         "gates.sh", n_hub(["scripts/alpha.py"]) == ["альфа"]),
        ("строка-путь не идёт через перевод строки: кавычка одной строки и `имя\"` другой — "
         "не звено (Н-5)", n(["scripts/zeta.py"]) == []),
        ("БОЛЬНОЙ: граф «кто зовёт» — файл, который файл проверки зовёт через звено (глубина 2), "
         "выбирает её", n(["scripts/alpha-deep.sh"]) == ["альфа"]),
        ("граф: глубина 1 (импорт) выбирает; упоминание в комментарии дельты — не вызов",
         n(["scripts/alpha_lib.py"]) == ["альфа"]),
        ("граф: глубина 3 — за пределом, не выбирает (и цикл не зацикливает обход)",
         n(["scripts/alpha-deepest.py"]) == []),
        ("граф и в --поезд: правила 1–3 там действуют (§4.1)",
         n(["scripts/alpha-deep.sh"], train=True) == ["альфа"]),
        ("таблица: граф назван своим правилом «кто зовёт»",
         graph_table.returncode == 0
         and graph_table.stdout.strip() == "альфа ← кто зовёт ← scripts/alpha-deep.sh"),
        ("--поезд, правка gates_check.sh — тот же выбор, что в очереди: самотест и звавшие",
         n(["scripts/lib/gates_check.sh"], train=True) == ["самотест обёртки", "зовёт обёртку"]),
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
         train_cli.returncode == 0 and train_cli.stdout.strip() == "самотест обёртки|зовёт обёртку"),
        ("файл проверки — её имя", n(["scripts/beta.sh"]) == ["бета"]),
        ("самотест test_<имя> — её имя", n(["scripts/test_beta.sh"]) == ["бета"]),
        ("самотест при дефисе в имени: test_check_delta.py", n(["scripts/test_check_delta.py"]) == ["дельта"]),
        ("входы механизма — имя", n(["data/alpha-1.json"]) == ["альфа"]),
        ("документ — пусто", n(["docs/readme.md"]) == []),
        ("bash -c: путь в тексте команды", n(["scripts/gamma-thing.py"]) == ["гамма"]),
        ("команда с продолжением строки: файл первого довода", n(["scripts/long.py"]) == ["длинная"]),
        ("префикс среды не мешает найти файл", n(["scripts/envy.py"]) == ["с средой"]),
        ("закомментированная проверка не разобрана", n(["scripts/ghost.py"]) == []),
        ("проверки-подделки раздела --selftest не разобраны",
         "падающая" not in [c.name for c in parse_checks(STAND_GATES)]),
        ("тяжёлая без флага не берётся", n(["scripts/heavy.py"]) == []),
        ("тяжёлая с флагом берётся", n(["scripts/heavy.py"], heavy=True) == ["тяжёлая"]),
        ("проверка после раздела тяжёлых — обычная", n(["scripts/after.py"]) == ["после тяжёлых"]),
        ("--имена: имена через «|» в порядке gates.sh, ровно для --только",
         cli_names.returncode == 0 and cli_names.stdout.strip() == "альфа|бета"),
        ("пустой выбор — пустая строка и код 0",
         nothing.returncode == 0 and nothing.stdout.strip() == ""),
        # Причина — своим словом: у пустого стенда код 2 дал бы и устаревший ключ входы_проб.
        ("пустой разбор проверок — код 2",
         broken.returncode == 2 and "ни одной проверки" in broken.stderr),
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
        stale, hub = Path(tmp, "устаревший-ключ"), Path(tmp, "узел-ворот")
        own = Path(tmp, "очередь-без-входов")
        make_stand(root)
        make_stand(own, train=STAND_TRAIN + "очередь_без_входов: [альфа]\n")
        make_stand(hub, gates=STAND_GATES + HUB_GATES_LINE)
        for rel, text in HUB_FILES.items():
            (hub / rel).write_text(text, encoding="utf-8")
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
        cases = selftest_cases(root, empty, repo, stale, hub, own)
        depth1, depth2 = Path(tmp, "глубина-1"), Path(tmp, "глубина-2")
        census, census_stale = Path(tmp, "перепись"), Path(tmp, "перепись-устаревшая")
        for where, files in ((depth1, {**DEPTH_FILES, TRAIN: DEPTH_TRAIN.format(depth=1)}),
                             (depth2, {**DEPTH_FILES, TRAIN: DEPTH_TRAIN.format(depth=2)}),
                             (census, {**CENSUS_FILES, TRAIN: CENSUS_TRAIN}),
                             (census_stale, {**CENSUS_FILES, TRAIN: CENSUS_STALE_TRAIN})):
            make_files(where, files)
        cases += narrow_cases(depth1, depth2, census, census_stale)
    failed = [name for name, ok in cases if not ok]
    for name in failed:
        print(f"САМОТЕСТ ПРОВАЛЕН: {name}")
    if failed:
        return 1
    print(f"САМОТЕСТ ПРОЙДЕН: {len(cases)} случаев (больной первым: правка gates_check.sh — самотест и звавшие, не «всё»)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    sys.exit(cli(sys.argv[1:]))
