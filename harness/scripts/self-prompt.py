#!/usr/bin/env python3
"""Сборщик личности: системный промпт класса запуска (спека личности и политики §4.3).

    self-prompt.py --класс <класс> --в <файл>
    self-prompt.py --карточки --в <каталог>     собрать карточки помощников
    self-prompt.py --карточки --проверить       сверить каталог карточек со сборкой
    self-prompt.py --selftest

Наборы текста и классы — в harness/config/launch-policy.yaml. Слой 1 читается
из эталона (SELF_ETALON_DIR, по умолчанию /etc/harness/self): его держит дверь
согласия, а не копия харнеса. Слой 2 (instance.yaml) — всегда из дерева.

Коды: 0 — собрано из эталона; 3 — эталона нет, слой 1 взят из дерева (файл
собран); 1 — отказ (файл НЕ создаётся, причина в stderr).

Карточка помощника = шапка из политики (раздел «карточки») + собранный текст класса
+ последняя строка «отпечаток: <метка сборки>»: системный промпт помощника — её тело.
Сверка карточек читает каталог SELF_CARDS_DIR (по умолчанию .claude/agents дерева) и
ничего в нём не чинит.

Двери (для проб): SELF_ETALON_DIR, SELF_TREE_DIR (корень дерева), LAUNCH_POLICY,
SELF_CARDS_DIR.
"""
import argparse
import hashlib
import json
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
from token_estimate import tokens  # noqa: E402

OK, REFUSED, DEGRADED = 0, 1, 3
SELF_PREFIX = "harness/self/"
DEFAULT_ETALON = config.корень_машины() + "/self"


class Refusal(Exception):
    """Отказ сборки: сообщение уходит в stderr, файл не создаётся."""


def load_policy(path: Path) -> dict:
    try:
        policy = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise Refusal(f"политика запуска {path} не прочитана: {err}")
    if not isinstance(policy, dict) or not policy.get("классы") or not policy.get("наборы"):
        raise Refusal(f"в политике {path} нет разделов «классы» и «наборы»")
    return policy


def source_path(spec: dict, etalon: Path, tree: Path, etalon_present: bool) -> Path:
    rel = spec["из"]
    if spec.get("слой") == 1 and etalon_present:
        # Эталон хранит harness/self/<x> как <эталон>/<x>.
        return etalon / rel.removeprefix(SELF_PREFIX)
    return tree / rel


def cut(text: str, spec: dict, name: str) -> str:
    if "поля" in spec:
        try:
            data = json.loads(text)
            return json.dumps({k: data[k] for k in spec["поля"]}, ensure_ascii=False, indent=2) + "\n"
        except (ValueError, KeyError, TypeError) as err:
            raise Refusal(f"набор {name}: поля {spec['поля']} не выбраны: {err!r}")
    if "раздел" in spec:
        m = re.search(rf"^## {re.escape(spec['раздел'])}\n.*?(?=^## |\Z)", text, re.S | re.M)
        if not m:
            raise Refusal(f"набор {name}: нет раздела «{spec['раздел']}»")
        return m.group(0)
    return text


def piece_of(name: str, spec: dict, etalon: Path, tree: Path, etalon_present: bool) -> str:
    path = source_path(spec, etalon, tree, etalon_present)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise Refusal(f"набор {name}: источник {path} не прочитан: {err}")
    piece = cut(text, spec, name)
    cost = tokens(piece)
    if "потолок" in spec and cost > spec["потолок"]:
        raise Refusal(f"набор {name}: {cost} токенов выше потолка {spec['потолок']}")
    return piece if piece.endswith("\n") else piece + "\n"


PASSPORT_KEYS = ("PROJECT_DIR", "AGENT_USER", "PROJECT_NAME")


def expand_passport(body: str) -> str:
    """Слой 1 обезличен (${PROJECT_DIR}): модель получает значения этой установки."""
    values = config.конфиг()
    for key in PASSPORT_KEYS:
        if values.get(key):
            body = body.replace("${" + key + "}", values[key].strip())
    return body


def build(cls: str, policy: dict, etalon: Path, tree: Path) -> tuple:
    classes = policy["классы"]
    if cls not in classes:
        raise Refusal(f"неизвестный класс «{cls}»; известны: {', '.join(classes)}")
    etalon_present = etalon.is_dir()
    pieces = []
    for name in classes[cls]:
        if name not in policy["наборы"]:
            raise Refusal(f"класс {cls}: набора «{name}» нет в политике")
        pieces.append(piece_of(name, policy["наборы"][name], etalon, tree, etalon_present))
    body = expand_passport("\n".join(pieces))
    tail = hashlib.sha256(body.encode("utf-8")).hexdigest()[:6]
    return f"Метка сборки: {cls}-{tail}\n{body}", etalon_present


def card_texts(policy: dict, etalon: Path, tree: Path) -> tuple:
    """({тип: текст карточки}, эталон был). Все карточки собираются до первой записи:
    отказ любого класса не оставляет половину карточек новой, половину старой."""
    cards, present = {}, True
    for name, spec in (policy.get("карточки") or {}).items():
        text, present = build(spec["класс"], policy, etalon, tree)
        label, _, body = text.partition("\n")
        stamp = label.removeprefix("Метка сборки: ")
        cards[name] = f"---\n{spec['шапка']}\n---\n\n{body.strip(chr(10))}\n\nотпечаток: {stamp}\n"
    if not cards:
        raise Refusal("в политике нет раздела «карточки»")
    return cards, present


def stale_cards(cards: dict, folder: Path) -> list:
    """Имена карточек каталога, не равных сборке (нет файла, правка руками, чужой отпечаток)."""
    stale = []
    for name, text in cards.items():
        try:
            same = (folder / f"{name}.md").read_text(encoding="utf-8") == text
        except (OSError, UnicodeDecodeError):
            same = False
        if not same:
            stale.append(name)
    return stale


def write_atomic(target: Path, text: str) -> None:
    # Временный файл в каталоге цели: rename в пределах одной файловой системы
    # атомарен, параллельный читатель видит старый файл целиком или новый целиком.
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    except OSError as err:
        raise Refusal(f"каталог цели {target.parent} недоступен: {err}")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except OSError as err:
        Path(tmp).unlink(missing_ok=True)
        raise Refusal(f"файл {target} не записан: {err}")


def run_cards(args, policy: dict, etalon: Path, tree: Path) -> int:
    cards, etalon_present = card_texts(policy, etalon, tree)
    if args.verify:
        folder = Path(os.environ.get("SELF_CARDS_DIR") or tree / ".claude" / "agents")
        stale = stale_cards(cards, folder)
        for name in stale:
            print(f"self-prompt: карточка {name} не равна сборке: пересобери "
                  f"(self-prompt.py --карточки --в {folder})", file=sys.stderr)
        return REFUSED if stale else OK
    Path(args.out).mkdir(parents=True, exist_ok=True)
    for name in stale_cards(cards, Path(args.out)):
        write_atomic(Path(args.out) / f"{name}.md", cards[name])
    return OK if etalon_present else DEGRADED


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--класс", dest="cls")
    parser.add_argument("--в", dest="out")
    parser.add_argument("--карточки", dest="cards", action="store_true")
    parser.add_argument("--проверить", dest="verify", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return subprocess.run([sys.executable, str(HERE / "test_self_prompt.py")]).returncode
    if args.cards:
        if not args.verify and not args.out:
            parser.error("нужны --в <каталог> или --проверить")
    elif not args.cls or not args.out:
        parser.error("нужны --класс и --в")
    etalon = Path(os.environ.get("SELF_ETALON_DIR") or DEFAULT_ETALON)
    tree = Path(os.environ.get("SELF_TREE_DIR") or HERE.parent)
    policy_path = Path(os.environ.get("LAUNCH_POLICY") or tree / "harness" / "config" / "launch-policy.yaml")
    try:
        if args.cards:
            return run_cards(args, load_policy(policy_path), etalon, tree)
        text, etalon_present = build(args.cls, load_policy(policy_path), etalon, tree)
        write_atomic(Path(args.out), text)
    except Refusal as err:
        print(f"self-prompt: отказ: {err}", file=sys.stderr)
        return REFUSED
    return OK if etalon_present else DEGRADED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
