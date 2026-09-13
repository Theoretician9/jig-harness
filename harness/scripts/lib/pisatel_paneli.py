#!/usr/bin/env python3
"""Запись файла с панели: одна механика на трёх писателей.

СЛУЧАЙ (ревизия лаконичности 12.09.2026). `zapisat-klyuch.py`,
`zapisat-shag.py` и `zapisat-pamyat.py` отличаются только тем, ЧТО проверяют на
входе. Всё остальное было скопировано трижды: отказ, копия перед записью,
журнал панели, возврат хозяина, подъём прав через sudo, атомарная запись
tmp+chmod+chown+replace. Правка правил записи — прав, копии, журнала —
требовала трёх одинаковых правок, и забытая третья молчала бы.

Здесь механика. У писателя остаётся его смысл: что ему дали на вход, годится
ли это и какой файл менять.

    python3 scripts/lib/pisatel_paneli.py --selftest
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import konf as конф                                    # noqa: E402

LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/harness"))


def отказ(метка: str, код: int, причина: str) -> int:
    """Отказ словами и своим кодом: панель показывает причину владельцу."""
    print(f"[{метка}] отказ: {причина}", file=sys.stderr)
    return код


def снять_копию(файл: Path, log_dir: Path | None = None) -> Path | None:
    """Копия прежнего файла ДО записи (И-1). None — файла ещё не было.

    Без копии запись не начинается: панель правит конфиги и память, а вернуть
    прежнее содержимое неоткуда — git тут не помощник, коммит делает демон
    позже.
    """
    if not файл.exists():
        return None
    каталог = (log_dir or LOG_DIR) / "панель-копии"
    каталог.mkdir(parents=True, exist_ok=True)
    метка = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    копия = каталог / f"{файл.name}.{метка}"
    копия.write_bytes(файл.read_bytes())
    return копия


def записать_журнал(метка: str, поля: dict, log_dir: Path | None = None) -> None:
    """Кто, когда и что поменял — единственный ответ на «почему харнес ведёт
    себя иначе». Отказ журнала не отменяет саму запись, но и не молчит."""
    запись = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **поля,
              "кто": os.environ.get("SUDO_USER") or os.environ.get("USER") or "?"}
    каталог = log_dir or LOG_DIR
    try:
        каталог.mkdir(parents=True, exist_ok=True)
        with open(каталог / "панель.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(запись, ensure_ascii=False) + "\n")
    except OSError as беда:
        print(f"[{метка}] журнал не записан: {беда}", file=sys.stderr)


def _вернуть_хозяина(новый: Path, прежний: os.stat_result) -> None:
    """Оставить прежних владельца и группу: запись идёт через sudo, и без
    этого файл молча переходит к root — следующая правка ещё пройдёт, а
    раскладка пакета и обычные инструменты уже нет (улика 11.09.2026)."""
    try:
        os.chown(новый, прежний.st_uid, прежний.st_gid)
    except (PermissionError, OSError):
        pass


def надо_поднять_права(цели: list[Path]) -> bool:
    """Хоть одна цель недоступна на запись — писать сами не можем.

    Флаг PANEL_NO_SUDO нужен проверкам: у них файлы свои, и лишний sudo сбросил
    бы подменённые пути прямо посреди пробы.
    """
    if os.environ.get("PANEL_NO_SUDO"):
        return False
    return any(not os.access(цель, os.W_OK) for цель in цели)


def поднять_права(скрипт: Path, доводы: list[str], вход: str | None = None) -> int:
    """Переисполниться под sudo по своей строке sudoers; отдаёт код.

    Признак «уже поднимались» едет ДОВОДОМ `--под-sudo`, потому что sudo
    сбрасывает окружение: без него служба панели под ProtectSystem=strict
    уходила в лавину попыток, пока запрос не отваливался по таймауту
    (улика 11.09.2026).
    """
    готово = subprocess.run(
        ["sudo", "-n", "/usr/bin/python3", str(скрипт), *доводы, "--под-sudo"],
        input=вход, capture_output=True, text=True, timeout=60)
    sys.stdout.write(готово.stdout)
    sys.stderr.write(готово.stderr)
    return готово.returncode


def записать_атомарно(файл: Path, текст: str, права_нового: int = 0o664) -> None:
    """tmp → chmod/chown по прежнему файлу → replace.

    Атомарность здесь не украшение: панель пишет файлы, которые в этот же
    момент читают демоны и сама сессия. Половина файла = сломанный конфиг.
    Права и хозяин берутся у ПРЕЖНЕГО файла, а для нового — у его каталога.
    """
    врем = файл.with_suffix(файл.suffix + ".tmp")
    try:
        врем.write_text(текст, encoding="utf-8")
        if файл.exists():
            прежний = os.stat(файл)
            os.chmod(врем, прежний.st_mode & 0o7777)
        else:
            прежний = os.stat(файл.parent)
            os.chmod(врем, права_нового)
        _вернуть_хозяина(врем, прежний)
        os.replace(врем, файл)
    except OSError:
        врем.unlink(missing_ok=True)
        raise


def _selftest() -> int:
    import tempfile

    путей = неудач = 0

    def проба(имя: str, ждём, факт):
        nonlocal путей, неудач
        путей += 1
        if факт == ждём:
            print(f"  ок    {имя}")
        else:
            неудач += 1
            print(f"  ПЛОХО {имя}: ждали «{ждём}», вышло «{факт}»")

    with tempfile.TemporaryDirectory() as врем:
        дом = Path(врем)
        лог = дом / "log"
        файл = дом / "конфиг.conf"
        файл.write_text("KEY=старое\n", encoding="utf-8")
        os.chmod(файл, 0o640)

        копия = снять_копию(файл, лог)
        проба("копия снята до записи", "KEY=старое\n",
              копия.read_text(encoding="utf-8"))
        проба("копии нет у файла, которого не было", None,
              снять_копию(дом / "нет-такого", лог))

        ино_до = os.stat(файл).st_ino
        записать_атомарно(файл, "KEY=новое\n")
        проба("записано", "KEY=новое\n", файл.read_text(encoding="utf-8"))
        # Подмена, а не дозапись поверх: у os.replace новый inode, а у «скопировать
        # в существующий файл» — прежний. Разница и есть атомарность: читатель
        # (демон, сессия) видит либо старый файл целиком, либо новый целиком,
        # никогда половину.
        проба("файл подменён целиком (новый inode)", True,
              os.stat(файл).st_ino != ино_до)
        проба("права прежнего файла сохранены", 0o640,
              os.stat(файл).st_mode & 0o777)
        проба("временный файл не остался", False,
              (дом / "конфиг.conf.tmp").exists())
        проба("прежнее содержимое лежит в копии", "KEY=старое\n",
              копия.read_text(encoding="utf-8"))

        новый = дом / "новый.md"
        записать_атомарно(новый, "текст\n", права_нового=0o664)
        проба("новый файл получает свои права", 0o664,
              os.stat(новый).st_mode & 0o777)

        # БОЛЬНОЙ СЛУЧАЙ: запись не удалась — прежний файл обязан уцелеть, а
        # огрызок не остаться. Каталог только для чтения даёт настоящий отказ.
        закрытый = дом / "закрытый"
        закрытый.mkdir()
        цель = закрытый / "файл.conf"
        цель.write_text("живое\n", encoding="utf-8")
        os.chmod(закрытый, 0o500)
        try:
            записать_атомарно(цель, "новое\n")
            упало = False
        except OSError:
            упало = True
        finally:
            os.chmod(закрытый, 0o700)
        проба("отказ записи назван, а не проглочен", True, упало)
        проба("прежнее содержимое цело", "живое\n", цель.read_text(encoding="utf-8"))
        проба("огрызка не осталось", False, (закрытый / "файл.conf.tmp").exists())

        записать_журнал("проба", {"ключ": "KEY", "было": "старое"}, лог)
        строки = (лог / "панель.jsonl").read_text(encoding="utf-8").splitlines()
        проба("журнал записан одной строкой", 1, len(строки))
        запись = json.loads(строки[0])
        проба("в журнале есть кто и когда", True,
              bool(запись.get("кто")) and bool(запись.get("ts")))

        проба("права поднимать не надо: файл свой", False,
              надо_поднять_права([файл]))
        os.chmod(закрытый, 0o500)
        проба("права поднять надо: каталог закрыт", True,
              надо_поднять_права([закрытый / "нет-файла"]))
        os.environ["PANEL_NO_SUDO"] = "1"
        проба("PANEL_NO_SUDO держит пробу на своих файлах", False,
              надо_поднять_права([закрытый / "нет-файла"]))
        os.environ.pop("PANEL_NO_SUDO", None)
        os.chmod(закрытый, 0o700)

    print(f"SELFTEST: {'зелёный' if not неудач else 'КРАСНЫЙ'} ({путей} путей; "
          "среди них больной случай «запись не удалась — прежнее цело»)")
    return 1 if неудач else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else print(__doc__) or 0)
