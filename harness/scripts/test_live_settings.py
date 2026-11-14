#!/usr/bin/env python3
"""Проба досыльщика ключей в живые YAML-настройки (Г3б).

Откуда взято. Копия харнеса (отчёт 10.11.2026): раскладка пропускает живые
pipeline.yaml / queue.yaml / … целиком («настройки владельца, не трогаю»), а
новый код ждёт новых ключей (queue.yaml: предел_порядка_сек, pipeline.yaml:
помощники). Спека — в карточке задачи.

Проба гоняет модуль scripts/lib/live_settings.py напрямую, без раскладки: стенд —
свой временный каталог, пакет — свой git-репозиторий с двумя версиями файла.

Прогон:
    python3 scripts/test_live_settings.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "scripts" / "lib" / "live_settings.py"

# Пакет «прошлый» и «новый»: в новом есть верхний ключ помощники (с комментарием
# над ним) и вложенный шаги.хакатон.
PACKAGE_OLD = """\
# Веса очереди.
веса:
  срочно: 3
шаги:
  мелкая: [тесты]
"""
PACKAGE_NEW = """\
# Веса очереди.
веса:
  срочно: 3
шаги:
  мелкая: [тесты]
  хакатон: [спека, тесты]

# Помощники: потолок запусков на задачу.
помощники:
  мелкая: {всего: 2}
"""
RECORDS_OLD = "версия: 1\nпроверено:\n  scripts/x.py: {увидели: да}\n"
RECORDS_NEW = "версия: 1\nпроверено: {}\nотложено: {}\n"

failures = 0
cases = 0


def report(name: str, troubles: list) -> None:
    global failures, cases
    cases += 1
    if troubles:
        failures += 1
        print(f"  ПЛОХО {name}: {'; '.join(troubles)}")
    else:
        print(f"  ок    {name}")


def load_module():
    if not MODULE.is_file():
        return None
    spec = importlib.util.spec_from_file_location("live_settings", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def package_stand(stand: Path, rel: str, versions: list) -> Path:
    """Git-пакет, где файл rel прошёл через versions по коммиту на версию; путь к файлу."""
    repo = stand / "pkg"
    (repo / "harness" / "config").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "proba@stand")
    git(repo, "config", "user.name", "proba")
    path = repo / "harness" / "config" / rel
    for number, text in enumerate(versions):
        path.write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"v{number}")
    return path


def live_stand(stand: Path, rel: str, text: str) -> Path:
    path = stand / "proj" / "harness" / "config" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def settle(module, live: Path, package: Path, records: bool = False) -> str:
    return "\n".join(module.settle(live, package, module.package_blobs(package), records=records))


def case_unedited_replaced(module, stand: Path) -> list:
    """(1) живой = прошлый пакетный → после раскладки побайтно новый пакетный."""
    package = package_stand(stand, "pipeline.yaml", [PACKAGE_OLD, PACKAGE_NEW])
    live = live_stand(stand, "pipeline.yaml", PACKAGE_OLD)
    said = settle(module, live, package)
    troubles = []
    if live.read_bytes() != package.read_bytes():
        troubles.append("живой не равен новому пакетному")
    if "заменён (не правился)" not in said:
        troubles.append(f"итог не назвал замену: {said!r}")
    return troubles


def case_owner_edit_kept(module, stand: Path) -> list:
    """(2) правка владельца + нет верхнего ключа → ключ дописан с комментарием, правка цела."""
    package = package_stand(stand, "pipeline.yaml", [PACKAGE_OLD, PACKAGE_NEW])
    owner = PACKAGE_OLD.replace("срочно: 3", "срочно: 7  # владелец 12.11")
    live = live_stand(stand, "pipeline.yaml", owner)
    before = yaml.safe_load(owner)
    said = settle(module, live, package)
    text = live.read_text(encoding="utf-8")
    troubles = []
    try:
        after = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [f"после досылки файл не читается: {error}"]
    if not isinstance(after, dict):
        return [f"после досылки не словарь: {after!r}"]
    if len(after) != len(before) + 1:
        troubles.append(f"верхних ключей до {len(before)}, после {len(after)} (ждали +1)")
    if after.get("помощники") != {"мелкая": {"всего": 2}}:
        troubles.append(f"помощники не дописаны значением пакета: {after.get('помощники')!r}")
    if after.get("веса") != {"срочно": 7}:
        troubles.append(f"правка владельца потеряна: веса = {after.get('веса')!r}")
    if not text.startswith(owner):
        troubles.append("текст владельца изменён (досылка — только в конец)")
    if "# владелец 12.11" not in text or "# Помощники: потолок запусков на задачу." not in text:
        troubles.append("комментарий владельца или блока пакета потерян")
    if "дописано" not in said or "помощники" not in said:
        troubles.append(f"итог не назвал дописанный ключ: {said!r}")
    again = settle(module, live, package)
    if live.read_text(encoding="utf-8") != text or "дописано" in again:
        troubles.append(f"повтор не пуст: {again!r}")
    return troubles


def case_nested_named(module, stand: Path) -> list:
    """(3) нет вложенного шаги.хакатон → назван поимённо, сам вложенный ключ не дописан."""
    package = package_stand(stand, "pipeline.yaml", [PACKAGE_OLD, PACKAGE_NEW])
    owner = PACKAGE_OLD.replace("[тесты]", "[тесты, мутации]")
    live = live_stand(stand, "pipeline.yaml", owner)
    said = settle(module, live, package)
    troubles = []
    if "вложенные без ключа" not in said or "шаги.хакатон" not in said:
        troubles.append(f"вложенный ключ не назван: {said!r}")
    try:
        after = yaml.safe_load(live.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return troubles + [f"файл не читается: {error}"]
    if "хакатон" in (after.get("шаги") or {}):
        troubles.append("вложенный ключ дописан, а спека велит только назвать")
    if after.get("шаги", {}).get("мелкая") != ["тесты", "мутации"]:
        troubles.append(f"правка владельца в шаги потеряна: {after.get('шаги')!r}")
    return troubles


def case_broken_untouched(module, stand: Path) -> list:
    """(4) вставка ломает YAML (живой — поточная запись) → файл не тронут, причина названа."""
    package = package_stand(stand, "pipeline.yaml", [PACKAGE_OLD, PACKAGE_NEW])
    owner = "{веса: {срочно: 7}, шаги: {мелкая: [тесты]}}\n"
    live = live_stand(stand, "pipeline.yaml", owner)
    said = settle(module, live, package)
    troubles = []
    if live.read_text(encoding="utf-8") != owner:
        troubles.append("файл изменён, хотя результат вставки не читается")
    if "не тронут" not in said:
        troubles.append(f"итог не сказал «не тронут»: {said!r}")
    leftovers = [p.name for p in live.parent.iterdir() if p.name != live.name]
    if leftovers:
        troubles.append(f"рядом остались временные файлы: {leftovers}")
    return troubles


def case_records_never_replaced(module, stand: Path) -> list:
    """(г) live-check-state.yaml = прошлый пакетный С ЗАПИСЯМИ → не заменён, ключ назван, не дописан."""
    package = package_stand(stand, "live-check-state.yaml", [RECORDS_OLD, RECORDS_NEW])
    live = live_stand(stand, "live-check-state.yaml", RECORDS_OLD)
    said = settle(module, live, package, records=True)
    troubles = []
    if live.read_text(encoding="utf-8") != RECORDS_OLD:
        troubles.append("записи установки переписаны (И-1)")
    if "отложено" not in said:
        troubles.append(f"недостающий верхний ключ записей не назван: {said!r}")
    return troubles


def main() -> int:
    module = load_module()
    print("── Г3б: досылка ключей в живые YAML-настройки ──")
    for name, case in (
            ("(1) живой = прошлый пакетный → заменён новым побайтно", case_unedited_replaced),
            ("(2) правка владельца цела, верхний ключ дописан с комментарием, повтор пуст", case_owner_edit_kept),
            ("(3) вложенный шаги.хакатон назван, не дописан", case_nested_named),
            ("(4) вставка ломает YAML → файл не тронут", case_broken_untouched),
            ("(г) записи установки не заменяются даже равные прошлому пакету", case_records_never_replaced)):
        if module is None:
            report(name, [f"модуля нет: {MODULE}"])
            continue
        stand = Path(tempfile.mkdtemp(prefix="proba-live-settings-"))
        try:
            troubles = case(module, stand)
        except Exception as error:  # noqa: BLE001 — сбой случая и есть находка
            troubles = [f"исключение {type(error).__name__}: {error}"]
        finally:
            shutil.rmtree(stand, ignore_errors=True)
        report(name, troubles)
    print(f"ЖИВЫЕ НАСТРОЙКИ: случаев {cases}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
