#!/usr/bin/env python3
"""Пробы `scripts/precheck.py`: пять «бумажных» гейтов коммита до `git commit`.

Откуда. План docs/plans/2026-10-28-ускорение-кодом.md, пункт О5: из ~420 отказов
pre-commit за октябрь ~330 дали пять гейтов (сверка с пакетом 109, прополка
карты 85, пайплайн 57, гигиена 56, схема карты 41; замер gates.jsonl 28.10).
Каждый отказ — повторный круг помощника. precheck зовёт ТЕ ЖЕ скрипты ТЕМИ ЖЕ
аргументами, беря команды из строк самого хука: своей копии правил у него нет.

Стенд: временный git-репозиторий, в его scripts/ — копия precheck под пробой и
заглушки пяти гейтов, которые пишут в журнал, с какими аргументами, входом и
каталогом их позвали, и выходят с кодом, заданным пробой. Хук стенда — текст
НАСТОЯЩЕГО pre-commit-gate.sh с перехватом `--devmap-only` сверху: строки гейтов
precheck читает из живого текста, так что отставание от хука проба увидит.

Дверь пробы: PRECHECK_UNDER_TEST — путь копии precheck (мутации).
Прогон: python3 scripts/test_precheck.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

ROOT = config.корень_дерева(__file__)
HERE = Path(__file__).resolve().parent
UNDER_TEST = Path(os.environ.get("PRECHECK_UNDER_TEST") or HERE / "precheck.py")
REAL_HOOK = HERE / "pre-commit-gate.sh"

# Гейты по имени скрипта → имя гейта в хуке (оно же в gates.jsonl).
GATES = {
    "check-context-hygiene.py": "context_hygiene",
    "weed-the-map.py": "прополка_карты",
    "check-pipeline.py": "пайплайн",
    "pre-commit-gate.sh": "devmap_schema",
    "compare-with-package.py": "package_sync",
}

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
name = Path(__file__).name
if args[:1] == ["--as"]:
    name, args = args[1], args[2:]
key = name.split(".")[0].upper().replace("-", "_")
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
files = {a: Path(a).read_text(encoding="utf-8") for a in args if os.path.isfile(a)}
with open(os.environ["PRECHECK_STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"name": name, "args": args, "stdin": stdin,
                          "cwd": os.getcwd(), "files": files,
                          "force": os.environ.get("HARNESS_PIPELINE_FORCE"),
                          "why": os.environ.get("HARNESS_PIPELINE_WHY")},
                         ensure_ascii=False) + "\n")
time.sleep(float(os.environ.get("STUB_SLEEP_" + key, "0")))
rc = int(os.environ.get("STUB_RC_" + key, "0"))
print(f"заглушка {name}: начало вывода")
if rc:
    print(f"  ✗ ПРИЧИНА-{name}")
    print("  хвост вывода")
sys.exit(rc)
'''

HOOK_PREFIX = '''#!/usr/bin/env bash
if [ "${1:-}" = "--devmap-only" ]; then
    exec python3 "$(dirname "$0")/stub.py" --as pre-commit-gate.sh "$@"
fi
exit 99
'''

paths = 0
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name}" + (f": {detail}" if detail else ""))


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def build_stand(base: Path, hook_text: str) -> Path:
    repo = base / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(UNDER_TEST, scripts / "precheck.py")
    (scripts / "stub.py").write_text(STUB, encoding="utf-8")
    for script in GATES:
        if script.endswith(".py"):
            (scripts / script).write_text(STUB, encoding="utf-8")
    (scripts / "pre-commit-gate.sh").write_text(HOOK_PREFIX + hook_text, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "s@s")
    git(repo, "config", "user.name", "s")
    (repo / "dev-map.yaml").write_text("версия: индекс\n", encoding="utf-8")
    (repo / "доки").mkdir()
    (repo / "доки" / "файл с пробелом.md").write_text("x\n", encoding="utf-8")
    git(repo, "add", "dev-map.yaml", "доки/файл с пробелом.md")
    # Рабочее дерево расходится с индексом: судить положено индекс.
    (repo / "dev-map.yaml").write_text("версия: дерево\n", encoding="utf-8")
    return repo


def run(repo: Path, env_extra: dict | None = None, *args: str, cwd: Path | None = None):
    log = repo.parent / "stub.log"
    log.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("STUB_", "HARNESS_PIPELINE_"))}
    env.update({"PRECHECK_STUB_LOG": str(log), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, str(repo / "scripts" / "precheck.py"), *args],
                          cwd=cwd or repo, env=env, capture_output=True, text=True, timeout=60)
    calls = [json.loads(s) for s in log.read_text(encoding="utf-8").splitlines() if s]
    return proc.returncode, proc.stdout + proc.stderr, calls


def line_of(out: str, gate: str) -> str:
    for line in out.splitlines():
        if f" {gate} " in f" {line} " or line.rstrip().endswith(f" {gate}"):
            return line
    return ""


def main() -> int:
    if not UNDER_TEST.is_file():
        print(f"precheck не найден: {UNDER_TEST}")
        return 1
    hook_text = REAL_HOOK.read_text(encoding="utf-8")
    print("== всё чисто: каждый из пяти гейтов позван ровно раз, итог 0")
    with tempfile.TemporaryDirectory() as tmp:
        repo = build_stand(Path(tmp), hook_text)
        rc, out, calls = run(repo)
        check("код возврата 0 при пяти зелёных", rc == 0, f"rc={rc}\n{out}")
        names = [c["name"] for c in calls]
        for script, gate in GATES.items():
            check(f"гейт {gate} ({script}) позван ровно раз", names.count(script) == 1,
                  f"вызовы: {names}")
            check(f"гейт {gate} назван в выводе «прошёл»", "прошёл" in line_of(out, gate), out)
        by = {c["name"]: c for c in calls}
        check("все гейты зовутся из корня репозитория",
              all(Path(c["cwd"]).resolve() == repo.resolve() for c in calls),
              str([c["cwd"] for c in calls]))

        print("== аргументы — те же, что в строках хука")
        check("прополка карты: --проверить",
              by.get("weed-the-map.py", {}).get("args") == ["--проверить"],
              str(by.get("weed-the-map.py")))
        hyg = by.get("check-context-hygiene.py", {}).get("args") or []
        check("гигиена: --repo <корень>",
              len(hyg) == 2 and hyg[0] == "--repo" and Path(hyg[1]).resolve() == repo.resolve(),
              str(hyg))
        check("пайплайн: без аргументов", by.get("check-pipeline.py", {}).get("args") == [],
              str(by.get("check-pipeline.py")))
        staged = by.get("compare-with-package.py", {}).get("stdin", "")
        check("сверка с пакетом получает список индекса на вход, имя с пробелом целиком",
              sorted(s for s in staged.splitlines() if s)
              == ["dev-map.yaml", "доки/файл с пробелом.md"], repr(staged))
        dm = by.get("pre-commit-gate.sh", {})
        check("схема карты: хук с --devmap-only", (dm.get("args") or [None])[0] == "--devmap-only",
              str(dm))
        check("схема карты судит копию ИНДЕКСА, не рабочего дерева",
              list((dm.get("files") or {}).values()) == ["версия: индекс\n"], str(dm.get("files")))

        print("== отказ одного гейта виден с причиной, код возврата не проглочен")
        for script, gate in GATES.items():
            key = script.split(".")[0].upper().replace("-", "_")
            rc, out, calls = run(repo, {f"STUB_RC_{key}": "1"})
            line = line_of(out, gate)
            check(f"отказ {gate}: код возврата 1", rc == 1, f"rc={rc}")
            check(f"отказ {gate}: строка «отказ» с причиной одной строкой",
                  "отказ" in line and f"ПРИЧИНА-{script}" in line, out)
            check(f"отказ {gate}: остальные гейты всё равно позваны",
                  len(calls) == len(GATES), str([c["name"] for c in calls]))

        print("== жёлтый (77) показывается и коммит не останавливает — как в хуке")
        rc, out, _ = run(repo, {"STUB_RC_CHECK_PIPELINE": "77"})
        check("жёлтый пайплайн: код 0", rc == 0, f"rc={rc}\n{out}")
        check("жёлтый пайплайн назван жёлтым", "жёлтый" in line_of(out, "пайплайн"), out)

        print("== код 77 у гейта со строкой «|| fail» — отказ: жёлтым его щадит только «гейт»")
        rc, out, _ = run(repo, {"STUB_RC_CHECK_CONTEXT_HYGIENE": "77"})
        check("77 у гигиены: код 1", rc == 1, f"rc={rc}\n{out}")
        check("77 у гигиены назван отказом", "отказ" in line_of(out, "context_hygiene"), out)

        print("== запуск из подкаталога: гейты всё равно зовутся из корня, как в хуке")
        rc, out, calls = run(repo, None, cwd=repo / "доки")
        check("из подкаталога: код 0 и пять вызовов", rc == 0 and len(calls) == len(GATES),
              f"rc={rc} {[c['name'] for c in calls]}\n{out}")
        check("из подкаталога: каталог каждого вызова — корень",
              calls and all(Path(c["cwd"]).resolve() == repo.resolve() for c in calls),
              str([c["cwd"] for c in calls]))

        print("== дверь обхода пайплайна в precheck не открывается")
        rc, out, calls = run(repo, {"HARNESS_PIPELINE_FORCE": "1",
                                    "HARNESS_PIPELINE_WHY": "проба"})
        check("гейты не видят HARNESS_PIPELINE_FORCE (иначе запись обхода без коммита)",
              calls and all(c["force"] is None and c["why"] is None for c in calls),
              str([(c["name"], c["force"]) for c in calls]))
        check("вывод говорит, что обход оставлен коммиту", "обход" in out, out)

        print("== предел времени: зависший гейт — отказ, а не ожидание")
        rc, out, _ = run(repo, {"STUB_SLEEP_CHECK_PIPELINE": "5"}, "--предел", "1")
        check("зависший гейт: код 1", rc == 1, f"rc={rc}")
        check("зависший гейт: «не уложился»", "не уложился" in line_of(out, "пайплайн"), out)

        print("== карта не в индексе: хук схему не судит — и precheck тоже")
        git(repo, "rm", "-q", "-f", "--cached", "dev-map.yaml")
        rc, out, calls = run(repo)
        check("схема карты не позвана", "pre-commit-gate.sh" not in [c["name"] for c in calls],
              str([c["name"] for c in calls]))
        check("схема карты названа пропущенной", "пропущен" in line_of(out, "devmap_schema"), out)
        check("пропуск — не отказ: код 0", rc == 0, f"rc={rc}")

    print("== хук без строки гейта: precheck отказывается работать, а не молчит")
    with tempfile.TemporaryDirectory() as tmp:
        broken = hook_text.replace('гейт "пайплайн"', 'гейт "пайплайн_новый"')
        check("стенд действительно сломан", broken != hook_text)
        repo = build_stand(Path(tmp), broken)
        rc, out, _ = run(repo)
        check("строки нет: код 2", rc == 2, f"rc={rc}\n{out}")
        check("строки нет: названо имя гейта", "пайплайн" in out, out)

    print(f"SELFTEST: {'зелёный' if not failures else 'КРАСНЫЙ'} ({paths} путей, неудач {failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
