#!/usr/bin/env python3
"""Гейт: слой 1 личности (harness/self/**) не держит значений паспорта установки.

Откуда взят (чистая установка несёт личность, Р2, 13.11.2026). Публичная
сборка обезличивает слой 1: путь проекта становится ${PROJECT_DIR}, домашний
каталог — /home/${AGENT_USER}. Эталон личности печатается из сборки, а пакет
держал литералы (roles/lead.md, roles/worker.md). Эталон и пакет расходились
на каждой машине («разошёлся» без правки), а чужая установка получила бы в
системный промпт путь ЭТОЙ машины.

Что судится: файлы слоя 1 под <корень>/harness/self/. Слой 2 (файлы, которые
политика запуска помечает «слой: 2», — instance.yaml) законно называет свою
установку и не судится. Список файлов берётся разбором дерева; пустой список —
«судить нечем», а не «чисто».

Образцы — значения паспорта (install.conf): PROJECT_DIR, /home/<AGENT_USER>,
PROJECT_NAME (целым словом). Пустой паспорт — «судить нечем».

    check-self-layer-neutral.py [--repo <корень>] [--паспорт <install.conf>]
    check-self-layer-neutral.py --selftest
Код возврата: 0 — чисто, 1 — найдено значение паспорта, 2 — судить нечем.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402

SELF_DIR = Path("harness") / "self"
POLICY = Path("harness") / "config" / "launch-policy.yaml"


def argument(name: str, default: str | None = None) -> str | None:
    if name in sys.argv:
        at = sys.argv.index(name) + 1
        if at < len(sys.argv):
            return sys.argv[at]
    return default


def patterns(passport: dict) -> list:
    """[(регулярка, имя ключа)]. Пустые значения пропускаются: пустой образец совпал бы со всем."""
    found = []
    project_dir = (passport.get("PROJECT_DIR") or "").strip().rstrip("/")
    if project_dir:
        found.append((re.compile(re.escape(project_dir)), "PROJECT_DIR"))
    user = (passport.get("AGENT_USER") or "").strip()
    if user:
        found.append((re.compile(re.escape(f"/home/{user}") + r"(?![\w.-])"), "AGENT_USER"))
    name = (passport.get("PROJECT_NAME") or "").strip()
    if name:
        # Целым словом: имя проекта внутри чужого слова — не след установки.
        found.append((re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])"), "PROJECT_NAME"))
    return found


def layer2_files(repo: Path) -> set:
    """Относительные к harness/self пути файлов слоя 2 — по политике запуска."""
    try:
        policy = yaml.safe_load((repo / POLICY).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    prefix = f"{SELF_DIR.as_posix()}/"
    return {spec["из"].removeprefix(prefix) for spec in (policy.get("наборы") or {}).values()
            if isinstance(spec, dict) and spec.get("слой") == 2 and str(spec.get("из", "")).startswith(prefix)}


def judged_files(repo: Path) -> list:
    base = repo / SELF_DIR
    skip = layer2_files(repo)
    return sorted(p for p in base.rglob("*")
                  if p.is_file() and p.relative_to(base).as_posix() not in skip)


def findings(files: list, repo: Path, found_patterns: list) -> list:
    out = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), 1):
            for regex, key in found_patterns:
                if regex.search(line):
                    out.append(f"{path.relative_to(repo)}:{number}: значение {key} — нужно ${{{key}}}")
                    break
    return out


def judge(repo: Path, passport: dict) -> tuple:
    """(код, строки вывода)."""
    found_patterns = patterns(passport)
    if not found_patterns:
        return 2, ["[слой-1] паспорт установки не прочитан — судить нечем, а не «чисто»"]
    files = judged_files(repo)
    if not files:
        return 2, [f"[слой-1] в {repo / SELF_DIR} нет файлов слоя 1 — судить нечем"]
    hits = findings(files, repo, found_patterns)
    lines = [f"[слой-1] судимых файлов {len(files)}, образцов {len(found_patterns)}"]
    if hits:
        return 1, lines + [f"  {h}" for h in hits] + [
            f"[слой-1] значения паспорта в слое 1: {len(hits)} — замени на ${{PROJECT_DIR}} / ${{AGENT_USER}}"]
    return 0, lines + ["[слой-1] значений паспорта в слое 1 нет"]


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    repo = Path(argument("--repo") or config.корень_дерева(__file__))
    passport = config.из_файла(argument("--паспорт") or config.путь_install_conf())
    code, lines = judge(repo, passport)
    for line in lines:
        print(line, file=sys.stderr if code == 2 else sys.stdout)
    return code


def selftest() -> int:
    sys.path.insert(0, str(HERE / "lib"))
    from selftest_cases import Cases
    cases = Cases()
    passport = {"PROJECT_DIR": "/srv/стенд-проект", "AGENT_USER": "жилец", "PROJECT_NAME": "Стенд"}
    with tempfile.TemporaryDirectory(prefix="harness-run-self-neutral-") as tmp:
        repo = Path(tmp)
        roles = repo / SELF_DIR / "roles"
        roles.mkdir(parents=True)
        (repo / POLICY).parent.mkdir(parents=True)
        (repo / POLICY).write_text(
            "наборы:\n  копия: {из: harness/self/instance.yaml, слой: 2}\n"
            "  роль: {из: harness/self/roles/r.md, слой: 1}\n", encoding="utf-8")
        (repo / SELF_DIR / "instance.yaml").write_text("копия: Стенд\n", encoding="utf-8")
        role = roles / "r.md"

        role.write_text("Проект `${PROJECT_DIR}`; пакет `/home/${AGENT_USER}/starter`.\n", encoding="utf-8")
        code, _ = judge(repo, passport)
        cases.check("подстановки вместо значений — чисто (слой 2 с именем не судится)", 0, code)

        for text, key in (("Проект `/srv/стенд-проект/scripts`\n", "PROJECT_DIR"),
                          ("пакет `/home/жилец/starter`\n", "AGENT_USER"),
                          ("копия Стенд ведёт\n", "PROJECT_NAME")):
            role.write_text("строка\n" + text, encoding="utf-8")
            code, lines = judge(repo, passport)
            cases.check(f"литерал {key} — код 1 и файл:строка названы", (1, True),
                        (code, any(f"roles/r.md:2: значение {key}" in l for l in lines)))

        role.write_text("Стендовый, ПодСтенд и /home/жилец2 — не следы\n", encoding="utf-8")
        cases.check("имя внутри слова и чужой дом — не след", 0, judge(repo, passport)[0])

        cases.check("пустой паспорт — судить нечем", 2, judge(repo, {})[0])
        empty = repo / "пусто"
        (empty / SELF_DIR).mkdir(parents=True)
        cases.check("пустой harness/self — судить нечем", 2, judge(empty, passport)[0])

        conf = repo / "install.conf"
        conf.write_text('PROJECT_DIR="/srv/стенд-проект"\nAGENT_USER="жилец"\n', encoding="utf-8")
        role.write_text("Проект /srv/стенд-проект\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("PROJECT_DIR", "AGENT_USER", "PROJECT_NAME")}
        done = subprocess.run([sys.executable, __file__, "--repo", str(repo), "--паспорт", str(conf)],
                              env=env, capture_output=True, text=True, timeout=60)
        cases.check("запуск: паспорт разобран из файла, литерал — код 1", 1, done.returncode)
    print(f"итог check-self-layer-neutral --selftest: случаев {cases.paths}, неудач {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
