#!/usr/bin/env python3
"""Писатель памяти с панели: правка записи проекта из браузера.

Владелец 11.09.2026: «Нужна возможность работать с памятью - вытащить показ и
редактирование файлов памяти и сделать те что будут по умолчанию после
установки».

Память — часть репозитория, поэтому правка выглядит как обычная правка файла:
текст приходит на вход, файл переписывается атомарно, прежний уезжает в копию
(И-1). Коммит делает демон `харнес/demons/pamyat-kommit.sh`: панель не имеет
права работать с git, а решение, требующее «чтобы кто-то потом закоммитил», —
не решение.

Опасность здесь ровно одна и она понятна: имя файла приходит из браузера.
Поэтому имя проверяется трижды — расширение `.md`, отсутствие путей и подъёмов,
и итоговый путь обязан лежать ровно в каталоге памяти.

Прогон:
    zapisat-pamyat.py <имя.md>        # текст записи — на stdin
Код возврата: 0 — записано; 2 — имя не годится; 3 — пустой текст;
4 — копия не удалась; 5 — записать не вышло.
"""
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПАМЯТЬ = Path(os.environ.get("HARNESS_PAMYAT") or КОРЕНЬ / "память")
LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/harness"))
# Имя записи: русские и латинские буквы, цифры, дефис. Ни точек, ни путей.
ИМЯ = re.compile(r"^[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9-]{0,80}\.md$")


def отказ(код: int, причина: str) -> int:
    print(f"[память] отказ: {причина}", file=sys.stderr)
    return код


def годное_имя(имя: str) -> str | None:
    """Причина отказа или None."""
    if not ИМЯ.match(имя):
        return (f"имя «{имя}» не годится: нужны буквы, цифры и дефис, "
                f"расширение .md, без путей")
    цель = (ПАМЯТЬ / имя).resolve()
    if цель.parent != ПАМЯТЬ.resolve():
        return f"путь «{цель}» выводит за каталог памяти"
    return None


def снять_копию(файл: Path) -> Path | None:
    if not файл.exists():
        return None
    каталог = LOG_DIR / "панель-копии"
    каталог.mkdir(parents=True, exist_ok=True)
    метка = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    копия = каталог / f"{файл.name}.{метка}"
    копия.write_bytes(файл.read_bytes())
    return копия


def записать_журнал(имя: str, было_знаков: int, стало_знаков: int) -> None:
    запись = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
              "память": имя, "было знаков": было_знаков,
              "стало знаков": стало_знаков,
              "кто": os.environ.get("SUDO_USER") or os.environ.get("USER") or "?"}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "панель.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(запись, ensure_ascii=False) + "\n")
    except OSError as беда:
        print(f"[память] журнал не записан: {беда}", file=sys.stderr)


def _вернуть_хозяина(новый_файл: Path, прежний) -> None:
    """Оставить прежних владельца и группу: запись идёт через sudo."""
    try:
        os.chown(новый_файл, прежний.st_uid, прежний.st_gid)
    except (PermissionError, OSError):
        pass


def main(argv: list[str]) -> int:
    argv = [а for а in argv if а != "--под-sudo"]
    if len(argv) != 1:
        return отказ(2, "нужно одно имя записи, текст — на stdin")
    имя = argv[0]
    беда = годное_имя(имя)
    if беда:
        return отказ(2, беда)
    текст = sys.stdin.read()
    if not текст.strip():
        return отказ(3, "пустой текст — это потеря записи, а не правка")

    файл = ПАМЯТЬ / имя
    каталог_пишется = os.access(ПАМЯТЬ, os.W_OK)
    файл_пишется = (not файл.exists()) or os.access(файл, os.W_OK)
    if (not (каталог_пишется and файл_пишется)) and "--под-sudo" not in sys.argv:
        готово = subprocess.run(
            ["sudo", "-n", "/usr/bin/python3", str(Path(__file__).resolve()),
             имя, "--под-sudo"], input=текст, capture_output=True, text=True,
            timeout=60)
        sys.stdout.write(готово.stdout)
        sys.stderr.write(готово.stderr)
        return готово.returncode

    try:
        копия = снять_копию(файл)
    except OSError as ошибка:
        return отказ(4, f"копия прежней записи не снята: {ошибка}")

    было = len(файл.read_text(encoding="utf-8")) if файл.exists() else 0
    врем = файл.with_suffix(".md.tmp")
    try:
        врем.write_text(текст, encoding="utf-8")
        if файл.exists():
            прежний = os.stat(файл)
            os.chmod(врем, прежний.st_mode & 0o7777)
            _вернуть_хозяина(врем, прежний)
        else:
            прежний = os.stat(ПАМЯТЬ)
            os.chmod(врем, 0o664)
            _вернуть_хозяина(врем, прежний)
        os.replace(врем, файл)
    except OSError as ошибка:
        врем.unlink(missing_ok=True)
        return отказ(5, f"записать не вышло: {ошибка}")

    записать_журнал(имя, было, len(текст))
    print(f"[память] {имя}: {было} → {len(текст)} знаков"
          + (f"; копия: {копия}" if копия else "; новая запись"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
