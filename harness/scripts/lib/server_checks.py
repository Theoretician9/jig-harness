#!/usr/bin/env python3
"""Разбор файла проверок продукта и записка тестов для двери выката.

CLI (подкоманды — данные, не идентификаторы):
  наборы <файл> [--только "<имена>"]      -> JSON-массив наборов в порядке файла
  хэш-образа <файл> <набор> <корень>      -> sha256(install + манифесты)[:12]
  хэш-файла <файл>                        -> sha256 файла
  записка <журнал> <sha> <хэш файла> <только> -> "путь\\tts" или пусто
  записка-собрать                         -> stdin JSON, stdout data записки
  службы <файл.sh>...                     -> общий отсортированный список SERVICES
Любой отказ: код 1, слова в stderr, пустой stdout.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

SERVICES_RE = re.compile(r'^\s*SERVICES="([^"]*)"', re.M)
STANDS = ("none", "pg-redis")
SUITE_KEYS = ("name", "base", "install", "manifests", "steps")


class Refusal(Exception):
    pass


def parse_step(step, suite):
    # Шаг без cmd стал бы пустым docker run, который «проходит».
    if not isinstance(step, dict) or not step.get("cmd"):
        raise Refusal(f"набор {suite}: шаг без cmd")
    return {"dir": step.get("dir", "."), "cmd": step["cmd"], "env": step.get("env") or {}}


def parse_suite(raw):
    name = raw.get("name") if isinstance(raw, dict) else None
    if not name:
        raise Refusal("набор без name")
    for key in SUITE_KEYS:
        if key not in raw:
            raise Refusal(f"набор {name}: нет ключа {key}")
    if not raw["steps"]:
        raise Refusal(f"набор {name}: нет шагов")
    stand = raw.get("stand", "none")
    timeout_s = raw.get("timeout_s", 900)
    # Опечатка в stand тихо запустила бы набор без стенда; timeout_s не число
    # сломал бы предел прогона уже в двери.
    if stand not in STANDS:
        raise Refusal(f"набор {name}: stand {stand!r} не из {' '.join(STANDS)}")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        raise Refusal(f"набор {name}: timeout_s {timeout_s!r} — нужно положительное целое")
    suite = {"name": name, "base": raw["base"], "install": raw["install"],
             "manifests": raw["manifests"], "stand": stand, "timeout_s": timeout_s,
             "steps": [parse_step(s, name) for s in raw["steps"]]}
    # По image дверь решает, на каком образе идёт набор (артефакт этапа B или свой).
    if raw.get("image"):
        suite["image"] = raw["image"]
    return suite


def load_suites(path):
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise Refusal(f"файл проверок не прочитан: {path}: {exc}")
    # Пустой список — провал: ноль наборов не должен сойти за «всё зелено».
    raw = data.get("suites") if isinstance(data, dict) else None
    if not raw:
        raise Refusal(f"в {path} нет наборов suites")
    suites = [parse_suite(r) for r in raw]
    names = [s["name"] for s in suites]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise Refusal(f"повтор имени набора: {' '.join(dups)}")
    return suites


def cmd_suites(args):
    only = []
    if "--только" in args:
        at = args.index("--только")
        only = args[at + 1].split() if at + 1 < len(args) else []
        args = args[:at]
    suites = load_suites(args[0])
    unknown = sorted(set(only) - {s["name"] for s in suites})
    if unknown:
        raise Refusal(f"неизвестные наборы в --только: {' '.join(unknown)}")
    chosen = [s for s in suites if not only or s["name"] in only]
    print(json.dumps(chosen, ensure_ascii=False))


def cmd_image_hash(args):
    path, name, root = args
    suite = next((s for s in load_suites(path) if s["name"] == name), None)
    if suite is None:
        raise Refusal(f"нет набора {name} в {path}")
    digest = hashlib.sha256(suite["install"].encode("utf-8"))
    for manifest in suite["manifests"]:
        try:
            digest.update((Path(root) / manifest).read_bytes())
        except OSError:
            raise Refusal(f"нет манифеста {manifest} в {root}")
    print(digest.hexdigest()[:12])


def cmd_file_hash(args):
    try:
        print(hashlib.sha256(Path(args[0]).read_bytes()).hexdigest())
    except OSError as exc:
        raise Refusal(f"файл не прочитан: {exc}")


def journal_records(journal):
    # Журнала ещё нет (первый прогон) — не ошибка; оборванную строку пропускаем.
    path = Path(journal)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def is_match(rec, sha, file_sha, only):
    data = rec.get("data") or {}
    return (rec.get("event") == "deploy.checks" and data.get("tests") == "success"
            and data.get("sha") == sha and data.get("checks_file_sha") == file_sha
            and set(data.get("only") or []) == only)


def cmd_note(args):
    journal, sha, file_sha, only = args
    found = [r for r in journal_records(journal) if is_match(r, sha, file_sha, set(only.split()))]
    if found:
        print(f"{journal}\t{found[-1]['ts']}")


def verdict(payload):
    # success открывает выкат, поэтому любое слово кроме passed и failed (skipped,
    # timeout, опечатка) — «не отработало»: error важнее failure, о коде судить нельзя.
    results = [s["result"] for s in payload["suites"]]
    names = sorted(s["name"] for s in payload["suites"])
    if any(r not in ("passed", "failed") for r in results):
        return "error"
    if "failed" in results:
        return "failure"
    if names != sorted(payload["selected"]):
        return "error"
    return "success"


def cmd_note_collect(args):
    payload = json.load(sys.stdin)
    if not payload["selected"]:
        raise Refusal("не выбрано ни одного набора")
    keys = ("sha", "branch", "checks_file_sha", "only", "suites", "mem_min_mb", "load_max", "seconds")
    data = {k: payload[k] for k in keys}
    data["tests"] = verdict(payload)
    print(json.dumps(data, ensure_ascii=False))


def services_of(path):
    try:
        match = SERVICES_RE.search(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise Refusal(f"файл не прочитан: {exc}")
    words = set(match.group(1).split()) if match else set()
    if not words:
        raise Refusal(f"в {path} нет непустой строки SERVICES=\"...\"")
    return words


def cmd_services(args):
    sets = {p: services_of(p) for p in args}
    first_path, first = next(iter(sets.items()))
    for path, words in sets.items():
        if words != first:
            diff = sorted(words ^ first)
            raise Refusal(f"списки служб разошлись: {path} против {first_path}: {' '.join(diff)}")
    print(" ".join(sorted(first)))


COMMANDS = {"наборы": cmd_suites, "хэш-образа": cmd_image_hash, "хэш-файла": cmd_file_hash,
            "записка": cmd_note, "записка-собрать": cmd_note_collect, "службы": cmd_services}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(f"подкоманда одна из: {' '.join(COMMANDS)}", file=sys.stderr)
        return 1
    try:
        COMMANDS[argv[0]](argv[1:])
    except Refusal as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
