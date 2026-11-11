#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт реестра стека: `harness/config/stack.yaml` согласован с установщиком в ОБЕ стороны.

Зачем. Реестр — единственное, что говорит механизму свежести, какие части стека
есть. Пакет, который ставит установщик, но которого нет в реестре, остаётся
невидимым навсегда; часть реестра, которой установщик не знает, — мёртвая запись,
которая выглядит работающей ([[a-two-way-invariant-checked-one-way]]).

Только ДАННЫЕ: команд реестра гейт не исполняет и в сеть не ходит — «мерится ли
часть на этой машине» судит демон (код 2), поэтому гейт не зависит от хоста.

Прогон:
    python3 scripts/check-stack-registry.py            # код 0 | 1; печатает расхождения и итог
    python3 scripts/check-stack-registry.py --selftest
Двери: HARNESS_STACK_REGISTRY (реестр), HARNESS_STACK_INSTALLER (каталог пакета; если задан,
поиск по умолчанию не ведётся).
"""
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402
from stack_freshness import PRESENCE, SOURCES  # noqa: E402 — одно место, где названы источники

REQUIRED = ("имя", "чьё", "критичность", "свежая", "безопасность", "задевает", "риск", "установка")
CRITICALITY = ("высокая", "обычная")
INSTALLER_FILES = ("USTANOVIT.sh", "harness/scripts/install-docker.sh")
PACKAGE_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+-]*")
# Строки-сообщения (совет «поставьте …») установкой не считаются.
MESSAGE_LINE = re.compile(r"^\s*(#|echo\b|printf\b|скажи\b|fail\b|warn\b)")
NPM_INSTALL = re.compile(r"npm install -g\s+['\"\\]*((?:@[\w.-]+/)?[\w.-]+)")
PIP_INSTALL = re.compile(r"pip['\"]?\s+install\s+(?:-\S+\s+)*([\w.-]+)")
NPX_INSTALL = re.compile(r"npx -y playwright install-deps \w+")
APT_INSTALL = re.compile(r"apt-get install\s+([^;&|\"})]*)")
# Установщик ставит через обёртку apt_поставить (замок apt), прямой apt-get — тоже встречается.
APT_WRAPPER = re.compile(r"apt_поставить\s+([^;&|\"})]*)")


def registry_path():
    if os.environ.get("HARNESS_STACK_REGISTRY"):
        return Path(os.environ["HARNESS_STACK_REGISTRY"])
    for candidate in (HERE.parent / "harness" / "config" / "stack.yaml", HERE.parent / "config" / "stack.yaml"):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "config" / "stack.yaml"


def installer_candidates():
    if os.environ.get("HARNESS_STACK_INSTALLER"):
        return [Path(os.environ["HARNESS_STACK_INSTALLER"])]
    starter = config.из_файла(config.путь_harness_conf()).get("STARTER_DIR", "").strip()
    user = config.из_файла(config.путь_install_conf()).get("AGENT_USER", "").strip()
    found = [Path(starter)] if starter else []
    if user:
        found += [Path(f"/home/{user}/starter/STARTER-PACKAGE"), Path(f"/home/{user}/STARTER-PACKAGE")]
    return found + [HERE.parent, HERE.parent.parent]    # корень раскладки пакета: рядом с harness/


def installer_texts():
    """(тексты файлов установщика, где искал). Нет установщика — пустой список."""
    tried = installer_candidates()
    for directory in tried:
        if (directory / "USTANOVIT.sh").is_file():
            return [(directory / name).read_text(encoding="utf-8")
                    for name in INSTALLER_FILES if (directory / name).is_file()], tried
    return [], tried


def installer_items(texts):
    """(пакеты, строки npx): что именно ставит установщик."""
    packages, npx = set(), set()
    for text in texts:
        for line in text.split("\n"):
            if MESSAGE_LINE.match(line):
                continue
            for match in (*APT_INSTALL.finditer(line), *APT_WRAPPER.finditer(line)):
                for token in match.group(1).split():
                    if token.startswith("-"):
                        continue
                    if not PACKAGE_TOKEN.fullmatch(token):
                        break
                    packages.add(token)
            packages.update(NPM_INSTALL.findall(line))
            packages.update(PIP_INSTALL.findall(line))
            npx.update(NPX_INSTALL.findall(line))
    return packages, npx


def part_names_for_installer(part):
    names = {part["имя"], part.get("пакет_установщика")}
    if isinstance(part.get("свежая"), dict):
        names.add(part["свежая"].get("пакет"))
    return {n for n in names if n}


def check_part_shape(part):
    problems = []
    name = part.get("имя", "?")
    for key in REQUIRED:
        if key not in part:
            problems.append(f"часть {name}: нет обязательного поля «{key}»")
    fresh = part.get("свежая")
    if isinstance(fresh, dict) and fresh.get("источник") not in SOURCES:
        problems.append(f"часть {name}: неизвестный источник «{fresh.get('источник')}»")
    if part.get("чьё") not in ("харнес", "машина", "чужое"):
        problems.append(f"часть {name}: «чьё» не из списка харнес | машина | чужое")
    if part.get("критичность") not in CRITICALITY:
        problems.append(f"часть {name}: «критичность» не из списка {' | '.join(CRITICALITY)}")
    presence = part.get("есть_если")
    if presence is not None:
        if not isinstance(presence, dict):
            problems.append(f"часть {name}: «есть_если» должен быть словарём")
        else:
            problems += [f"часть {name}: «есть_если» знает только {' | '.join(PRESENCE)}, а не «{kind}»"
                         for kind in presence if kind not in PRESENCE]
    current = part.get("текущая")
    if current is None and isinstance(fresh, dict) and fresh.get("источник") not in ("apt-ждущие",):
        problems.append(f"часть {name}: нет «текущая»")
    if current is not None:
        problems += argv_problems(name, "текущая.команда", current.get("команда"))
        if current.get("образец"):
            try:
                re.compile(current["образец"])
            except re.error as error:
                problems.append(f"часть {name}: образец не компилируется ({error})")
    door = part.get("установка") if isinstance(part.get("установка"), dict) else {}
    kind = door.get("дверь", "нет")
    if kind == "окно-ротации":
        # Ставит не установщик, а сторож окна ротации: argv нет, но ключ и причина обязательны.
        problems += [f"часть {name}: установка.{key} обязателен для двери окно-ротации"
                     for key in ("ключ", "причина") if not door.get(key)]
    elif kind != "нет":
        problems += argv_problems(name, "установка.argv", door.get("argv"))
        problems += argv_problems(name, "установка.после", door.get("после"))
    return problems


def argv_problems(name, where, argv):
    if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
        return []
    return [f"часть {name}: {where} — argv должен быть списком строк (исполняется без shell), а не {type(argv).__name__}"]


def find_discrepancies(registry, packages, npx):
    problems = []
    parts = [p for p in registry["части"] if isinstance(p, dict)]
    general = set(registry.get("в_общей_строке_ОС") or [])
    not_judged = registry.get("не_судим") or {}
    covered = set()
    for part in parts:
        covered |= part_names_for_installer(part)
    for package in sorted(packages):
        if package not in covered and package not in general:
            problems.append(f"пакет установщика {package} без решения в реестре "
                            f"(часть, строка общей ОС или «не_судим»)")
    for line in sorted(npx):
        if line not in not_judged:
            problems.append(f"строка установщика «{line}» без причины в «не_судим»")
    for part in parts:
        if part.get("чьё") == "харнес" and not (part_names_for_installer(part) & packages) \
                and part.get("откуда") != "вручную":
            problems.append(f"часть {part['имя']} (чьё: харнес) не названа установщиком "
                            f"и не помечена «откуда: вручную»")
    for name in sorted(general - packages):
        problems.append(f"«{name}» в общей строке ОС, но установщик его не ставит")
    return problems


def selftest():
    registry = {"части": [{"имя": "tmux", "чьё": "харнес"}], "в_общей_строке_ОС": ["unzip"], "не_судим": {}}
    cases = [("всё названо", registry, {"tmux", "unzip"}, set(), 0),
             ("пакет без решения", registry, {"tmux", "unzip", "htop"}, set(), 1),
             ("мёртвая запись общей строки", registry, {"tmux"}, set(), 1),
             ("часть без установщика", registry, {"unzip"}, set(), 1),
             ("npx без причины", registry, {"tmux", "unzip"}, {"npx -y playwright install-deps chromium"}, 1)]
    failed = 0
    for title, reg, packages, npx, expected in cases:
        got = len(find_discrepancies(reg, packages, npx))
        if got != expected:
            failed += 1
            print(f"  ПРОВАЛ {title}: расхождений {got}, ждали {expected}")
    for title, line in (("apt-get install", 'делай "apt-get install -y git tmux"'),
                        ("обёртка apt_поставить", 'делай "apt_поставить git tmux"'),
                        ("определение обёртки не пакеты", "apt_поставить() { # git tmux")):
        got = installer_items([line])[0]
        want = set() if "()" in line else {"git", "tmux"}
        if got != want:
            failed += 1
            print(f"  ПРОВАЛ {title}: пакеты {sorted(got)}, ждали {sorted(want)}")
    shape = {"имя": "перезагрузка", "чьё": "машина", "критичность": "высокая", "свежая": {"источник": "перезагрузка"},
             "текущая": {"команда": ["uname", "-r"]}, "безопасность": "перезагрузка", "задевает": [], "риск": "r"}
    door = {"дверь": "окно-ротации", "ключ": "AUTO_REBOOT_IN_ROTATION", "причина": "сама при смене сессии"}
    shape_cases = [("окно-ротации с ключом", dict(shape, установка=door), 0),
                   ("окно-ротации без ключа", dict(shape, установка={k: v for k, v in door.items() if k != "ключ"}), 1),
                   ("окно-ротации без причины", dict(shape, установка={k: v for k, v in door.items() if k != "причина"}), 1),
                   ("дверь npm без argv", dict(shape, установка={"дверь": "npm"}), 2)]
    for title, part, expected in shape_cases:
        got = len(check_part_shape(part))
        if got != expected:
            failed += 1
            print(f"  ПРОВАЛ {title}: замечаний {got}, ждали {expected}")
    print(f"check-stack-registry --selftest: случаев {len(cases) + 3 + len(shape_cases)}, неудач {failed}")
    return 1 if failed else 0


def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    path = registry_path()
    try:
        registry = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(registry, dict) and isinstance(registry.get("части"), list)
    except (OSError, yaml.YAMLError, AssertionError) as error:
        print(f"реестр не прочитан ({path}): {error}")
        return 1
    problems = []
    judged = [p for p in registry["части"] if isinstance(p, dict) and p.get("чьё") != "чужое"]
    for part in registry["части"]:
        problems += check_part_shape(part) if isinstance(part, dict) else ["часть не словарь"]
    texts, tried = installer_texts()
    packages = set()
    if texts:
        packages, npx = installer_items(texts)
        problems += find_discrepancies(registry, packages, npx)
    else:
        print("сверка с установщиком невозможна: искал " + ", ".join(str(t) for t in tried))
    if not judged:
        problems.append("судимых частей 0: реестр пуст или все части чужие")
    for problem in problems:
        print(f"расхождение: {problem}")
    print(f"судимых частей {len(judged)}, пакетов установщика {len(packages)}, расхождений {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
