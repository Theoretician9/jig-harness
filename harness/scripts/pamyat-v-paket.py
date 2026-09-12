#!/usr/bin/env python3
"""Набор памяти по умолчанию: какие записи достаются новой установке.

Владелец 11.09.2026: «сделать те что будут по умолчанию после установки».

Набор — это НЕ признак внутри файла, а факт: запись лежит в стартовом пакете,
из которого разворачивается новый харнес. Признак внутри файла пришлось бы
сверять с пакетом отдельным гейтом, а факт сверять не с чем — он и есть
состояние.

Прогон:
    pamyat-v-paket.py <имя.md> включить|исключить
    pamyat-v-paket.py --список        # что сейчас в наборе
Код возврата: 0 — сделано; 2 — имя не годится; 5 — файла нет или не записалось.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

КОРЕНЬ_ЛИБ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ_ЛИБ / "scripts" / "lib"))
from konf import из_файла  # noqa: E402 — один парсер паспорта на весь харнес


def _пакет_по_паспорту() -> str:
    """Каталог пакета по конвенции установки: /home/<AGENT_USER>/starter/…

    Имя пользователя, вшитое в код, — след НАШЕЙ установки: у другого
    владельца агент зовётся иначе, и путь молча ведёт в никуда. Паспорт читает
    ОБЩАЯ функция: за одну смену 12.09.2026 эта же выборка была скопирована
    руками четырежды, и копии разошлись — две брали первое совпадение, две
    последнее (ревью кода, №7).
    """
    агент = из_файла("/etc/harness/install.conf").get("AGENT_USER", "")
    return f"/home/{агент or 'agent'}/starter/STARTER-PACKAGE"


КОРЕНЬ = Path(__file__).resolve().parent.parent
ПАМЯТЬ = Path(os.environ.get("HARNESS_PAMYAT") or КОРЕНЬ / "память")
ПАКЕТ = Path(os.environ.get("HARNESS_STARTER")
             or _пакет_по_паспорту()) / "harness" / "память"
LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/harness"))
ИМЯ = re.compile(r"^[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9-]{0,80}\.md$")


def отказ(код: int, причина: str) -> int:
    print(f"[набор-памяти] отказ: {причина}", file=sys.stderr)
    return код


def в_наборе() -> list[str]:
    try:
        return sorted(п.name for п in ПАКЕТ.glob("*.md"))
    except OSError:
        return []


def записать_журнал(имя: str, действие: str) -> None:
    запись = {"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
              "набор памяти": имя, "действие": действие,
              "кто": os.environ.get("SUDO_USER") or os.environ.get("USER") or "?"}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "панель.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(запись, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    argv = [а for а in argv if а != "--под-sudo"]
    if "--список" in argv:
        print("\n".join(в_наборе()))
        return 0
    if len(argv) != 2:
        return отказ(2, "нужно: <имя.md> включить|исключить")
    имя, действие = argv
    if not ИМЯ.match(имя):
        return отказ(2, f"имя «{имя}» не годится")
    if действие not in ("включить", "исключить"):
        return отказ(2, "второй довод — «включить» или «исключить»")

    источник, цель = ПАМЯТЬ / имя, ПАКЕТ / имя
    if цель.resolve().parent != ПАКЕТ.resolve():
        return отказ(2, "путь выводит за каталог набора")

    нужен_подъём = not os.access(ПАКЕТ, os.W_OK)
    if нужен_подъём and "--под-sudo" not in sys.argv:
        готово = subprocess.run(
            ["sudo", "-n", "/usr/bin/python3", str(Path(__file__).resolve()),
             имя, действие, "--под-sudo"], capture_output=True, text=True, timeout=60)
        sys.stdout.write(готово.stdout)
        sys.stderr.write(готово.stderr)
        return готово.returncode

    try:
        if действие == "включить":
            if not источник.exists():
                return отказ(5, f"записи «{имя}» нет в памяти проекта")
            ПАКЕТ.mkdir(parents=True, exist_ok=True)
            shutil.copy2(источник, цель)
            прежний = os.stat(ПАКЕТ)
            os.chown(цель, прежний.st_uid, прежний.st_gid)
        else:
            if not цель.exists():
                return отказ(5, f"записи «{имя}» в наборе и не было")
            цель.unlink()
    except OSError as беда:
        return отказ(5, f"не вышло: {беда}")

    записать_журнал(имя, действие)
    print(f"[набор-памяти] {имя}: {действие}; в наборе записей: {len(в_наборе())}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
