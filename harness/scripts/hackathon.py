#!/usr/bin/env python3
"""Режим «хакатон»: срок, уровень карточки и долг «довести до продакшна».

Откуда взят. Решение владельца 28-29.10.2026: на хакатоне задача идёт по
облегчённому пайплайну (спека и ревью спеки остаются, ревью одно), но не
бесплатно — каждая такая задача заводит карточку долга, и закрыть её (done)
нельзя, пока долг открыт. Срок режима — 72 часа.
Спека: docs/specs/2026-10-28-режим-хакатон.md.

Библиотека (зовут check-pipeline.py, pre-commit-gate.sh, deploy.sh) и CLI:

    python3 scripts/hackathon.py --активен [--карта <путь>]
    python3 scripts/hackathon.py --включить [часы]
    python3 scripts/hackathon.py --выключить
    python3 scripts/hackathon.py --включить-задачу <id> --карта <путь>
    python3 scripts/hackathon.py --проверить-карту <путь>
    python3 scripts/hackathon.py --строка [--карта <путь>]   (одна строка для оглядки старта)
"""
import ast
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import cards                                              # noqa: E402
import config                                             # noqa: E402

LEVEL = "хакатон"
KEY = "HACKATHON_UNTIL"
DEFAULT_HOURS = 72
DEBT_SUFFIX = "-prod-debt"


def _conf(conf_path):
    return str(conf_path) if conf_path else config.путь_harness_conf()


def until(conf_path=None):
    """Срок режима или None: пусто, нет ключа, не разобрано и «без пояса» — всё None."""
    raw = config.из_файла(_conf(conf_path)).get(KEY, "")
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Срок без пояса нельзя сравнить с «сейчас» честно: режим не включается.
    return moment if moment.tzinfo else None


def until_text(conf_path=None):
    """Срок как записан в конфиге (для показа), если он действителен."""
    return config.из_файла(_conf(conf_path)).get(KEY, "") if until(conf_path) else None


def active(now=None, conf_path=None):
    deadline = until(conf_path)
    return deadline is not None and (now or dt.datetime.now(dt.timezone.utc)) < deadline


def is_hackathon(card):
    depth = card.get("глубина") if isinstance(card, dict) else None
    return isinstance(depth, dict) and depth.get("уровень") == LEVEL


def _pipeline():
    spec = importlib.util.spec_from_file_location("pipeline_hackathon", HERE / "pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FORBIDDEN_IN_INDEX = "harness/config/hackathon-forbidden.yaml"
# Корни суда: всё, что они исполняют, хакатону запрещено — иначе коммит подменяет судью.
JUDGE_ROOTS = ("scripts/check-pipeline.py", "scripts/hackathon.py",
               "scripts/pre-commit-gate.sh", "scripts/commit-msg-gate.sh")
BARE_NAME = re.compile(r"[\w.-]+\.(?:py|sh)$")
SHELL_CALL = re.compile(r"/((?:lib/)?[\w.-]+\.(?:py|sh))\b")


def _forbidden_text(path):
    """Файл правил: явный путь — как есть; в git — из ИНДЕКСА (в коммит уходит индекс, не дерево);
    вне git (копия дерева) индекса нет — с диска."""
    if path:
        return Path(path).read_text(encoding="utf-8")
    root = str(HERE.parent)
    inside = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, timeout=30)
    if inside.returncode != 0:
        return (HERE.parent / FORBIDDEN_IN_INDEX).read_text(encoding="utf-8")
    done = subprocess.run(["git", "-C", root, "show", f":{FORBIDDEN_IN_INDEX}"],
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    if done.returncode != 0:
        raise OSError(done.stderr.strip() or "git не отдал файл из индекса")
    return done.stdout


def forbidden_masks(path=None):
    """Маски путей из данных. Пустой или нечитаемый файл — исключение: отказ, не «можно»."""
    try:
        masks = (yaml.safe_load(_forbidden_text(path)) or {}).get("paths")
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError, yaml.YAMLError) as error:
        raise RuntimeError(f"список запретов хакатона не прочитан ({path or 'индекс'}): {error}")
    if not isinstance(masks, list) or not masks or not all(isinstance(m, str) and m for m in masks):
        raise RuntimeError(f"список запретов хакатона пуст или негоден ({path or 'индекс'})")
    return masks


def _in_scripts(root, name):
    for folder in ("scripts", "scripts/lib"):
        if (root / folder / name).is_file():
            return f"{folder}/{name}"
    return None


def _python_edges(root, path):
    found = set()
    for node in ast.walk(ast.parse((root / path).read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] + ".py" for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module.split(".")[0] + ".py"]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            names = [node.right.value] if isinstance(node.right, ast.Constant) else []
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "join" and node.args and isinstance(node.args[-1], ast.Constant)):
            names = [node.args[-1].value]
        found |= {hit for name in names if isinstance(name, str) and BARE_NAME.match(name)
                  for hit in [_in_scripts(root, name)] if hit}
    return found


def _shell_edges(root, path):
    lines = (root / path).read_text(encoding="utf-8").splitlines()
    return {f"scripts/{rel}" for line in lines if not line.lstrip().startswith("#")
            for rel in SHELL_CALL.findall(line) if (root / "scripts" / rel).is_file()}


def judge_dependencies(root=None):
    """Корни суда и всё, что они исполняют, транзитивно — выводом из текста, не перечнем.

    Пустой итог и нечитаемый файл — исключение: суд без списка не судит.
    """
    root = Path(root) if root else HERE.parent
    seen, todo = set(), [r for r in JUDGE_ROOTS if (root / r).is_file()]
    try:
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            edges = _python_edges if path.endswith(".py") else _shell_edges
            todo.extend(edges(root, path) - seen)
    except (OSError, ValueError, SyntaxError) as error:
        raise RuntimeError(f"зависимости суда хакатона не выведены ({path}): {error}")
    if not seen:
        raise RuntimeError(f"зависимости суда хакатона не выведены: корней суда нет в {root}")
    return sorted(seen)


def touches_harness_guards(paths, conf_path=None):
    """Сторожа из GUARD_PATHS, файлы правил из hackathon-forbidden.yaml и зависимости суда.

    Зависимости сравниваются точным равенством, а не через pipeline._под_путями:
    иначе коммит, ломающий саму сверку путей, отключал бы свой суд.
    """
    pipeline = _pipeline()
    guard = pipeline.пути_установки(_conf(conf_path))["GUARD_PATHS"]
    forbidden = " ".join(forbidden_masks())
    judge = set(judge_dependencies())
    return [path for path in paths
            if path in judge or pipeline._под_путями(path, guard) or pipeline._под_путями(path, forbidden)]


def debt_id(task_id):
    return task_id + DEBT_SUFFIX


def make_debt_card(task_id, today):
    return {"id": debt_id(task_id), "status": "plan",
            "title": f"Довести до продакшна: {task_id}",
            "summary": f"Хакатон-задача {task_id} сделана в режиме хакатон; "
                       f"довести до продакшна: мутации, полные ворота, полное ревью. Заведена {today}."}


def debt_open(card_list, task_id):
    for card in card_list:
        if card.get("id") == debt_id(task_id):
            return card.get("status") not in cards.CLOSED_STATUSES
    return False


def _tasks(data):
    return [(line.get("id"), task) for line in (data or {}).get("lines") or []
            for task in line.get("tasks") or []]


def _load(map_path):
    return yaml.safe_load(Path(map_path).read_text(encoding="utf-8")) or {}


def cmd_active(map_path):
    if not active():
        return 1
    if map_path is None:
        return 0
    wip = [task for _, task in _tasks(_load(map_path)) if task.get("status") == "wip"]
    return 0 if len(wip) == 1 and is_hackathon(wip[0]) else 1


def cmd_line(map_path):
    mode = f"до {until_text()}" if active() else "выкл"
    path = map_path or str(HERE.parent / "dev-map.yaml")
    count = sum(1 for _, task in _tasks(_load(path)) if is_hackathon(task))
    print(f"хакатон: {mode}; уровней хакатон {count}")
    return 0


def cmd_enable(hours):
    # write-key.py как CLI не годится (регулярка значений запрещает «:» и «+»),
    # поэтому берём его замену строки и общий атомарный писатель панели.
    spec = importlib.util.spec_from_file_location("write_key", HERE / "write-key.py")
    write_key = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(write_key)
    writer = write_key.писатель
    target = Path(config.путь_harness_conf())
    if writer.надо_поднять_права([target]):
        return writer.отказ("хакатон", 5, f"нет права записи в {target}")
    deadline = (dt.datetime.now(dt.timezone.utc).astimezone()
                + dt.timedelta(hours=hours)).replace(microsecond=0).isoformat()
    text = target.read_text(encoding="utf-8")
    copy = writer.снять_копию(target, writer.LOG_DIR)
    writer.записать_атомарно(target, write_key.заменить(text, KEY, f'"{deadline}"'))
    writer.записать_журнал("хакатон", {"ключ": KEY, "было": config.из_текста(text).get(KEY),
                                       "стало": deadline, "файл": target.name}, writer.LOG_DIR)
    print(f"[хакатон] {KEY} = {deadline}; копия: {copy}")
    return 0


def _set_level(block, today):
    """Текст карточки с уровнем «хакатон»: ключи глубины целы, нет глубины — заводим."""
    depth = re.search(r"глубина:", block)
    if depth:
        level = re.compile(r"(уровень:\s*)[^,}\s]+").search(block, depth.end())
        if level is None:
            raise cards.MapRefused("у глубины карточки нет уровня")
        return block[:level.start()] + level.group(1) + LEVEL + block[level.end():]
    lines = block.splitlines(keepends=True)
    last = max(i for i, line in enumerate(lines)
               if line.strip() and not line.lstrip().startswith("#"))
    indent = re.match(r"\s*", lines[last]).group(0)
    field = indent if not lines[last].lstrip().startswith("- ") else indent + "  "
    status = re.search(r"^([ \t]+)status:", block, re.M)
    field = status.group(1) if status else field
    row = (f"{field}глубина: {{ уровень: {LEVEL}, характер: разработка, "
           f'кем: hackathon.py, когда: "{today}" }}\n')
    if not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, row)
    return "".join(lines)


def _card_text(card):
    quote = lambda value: json.dumps(value, ensure_ascii=False)       # noqa: E731
    return [f"      - id: {card['id']}\n", f"        status: {card['status']}\n",
            f"        title: {quote(card['title'])}\n", f"        summary: {quote(card['summary'])}\n"]


def _write_atomic(path, text):
    """Карта — главные данные: пишем рядом и подменяем, оборванная запись её не режет."""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


def cmd_enable_task(task_id, map_path):
    today = dt.date.today().isoformat()
    with cards.map_lock(cards.lock_path()):
        path = Path(map_path)
        text = path.read_text(encoding="utf-8")
        owners = [line for line, task in _tasks(yaml.safe_load(text) or {}) if task.get("id") == task_id]
        found = cards.блок(text, task_id)
        if not owners or found is None:
            print(f"[хакатон] отказ: в карте {map_path} нет задачи {task_id}", file=sys.stderr)
            return 2
        block, start, end = found
        new_text = text[:start] + _set_level(block, today) + text[end:]
        if debt_id(task_id) not in cards.task_ids(yaml.safe_load(new_text) or {}):
            new_text = cards.insert_cards(new_text, owners[0],
                                          [_card_text(make_debt_card(task_id, today))])
        _write_atomic(path, new_text)
    print(f"[хакатон] {task_id}: уровень «{LEVEL}», долг {debt_id(task_id)}")
    return 0


def cmd_check_map(map_path):
    card_list = [task for _, task in _tasks(_load(map_path))]
    bad = False
    for card in card_list:
        if not is_hackathon(card):
            continue
        task_id = card.get("id")
        if not any(c.get("id") == debt_id(task_id) for c in card_list):
            print(f"[хакатон] у {task_id} нет карточки долга: "
                  f"hackathon.py --включить-задачу {task_id} --карта {map_path}")
            bad = True
        elif card.get("status") in cards.CLOSED_STATUSES and debt_open(card_list, task_id):
            print(f"[хакатон] {task_id} закрыта при открытом долге {debt_id(task_id)}: "
                  f"сперва долг до «принято»")
            bad = True
    return 1 if bad else 0


def cmd_disable():
    # Конфиг пишет только write-key.py (единственный писатель), поэтому
    # выключение делегируем ему, а не правим файл здесь; долги не трогаем.
    writer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "write-key.py")
    return subprocess.run([sys.executable, writer, "HACKATHON_UNTIL", "0"]).returncode


def main(argv):
    def value_of(flag):
        return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None

    if "--активен" in argv:
        return cmd_active(value_of("--карта"))
    if "--строка" in argv:
        return cmd_line(value_of("--карта"))
    if "--включить-задачу" in argv:
        task_id, map_path = value_of("--включить-задачу"), value_of("--карта")
        if not task_id or not map_path:
            print("нужно: --включить-задачу <id> --карта <путь>", file=sys.stderr)
            return 2
        return cmd_enable_task(task_id, map_path)
    if "--включить" in argv:
        return cmd_enable(float(value_of("--включить") or DEFAULT_HOURS))
    if "--выключить" in argv:
        return cmd_disable()
    if "--проверить-карту" in argv:
        return cmd_check_map(value_of("--проверить-карту"))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
