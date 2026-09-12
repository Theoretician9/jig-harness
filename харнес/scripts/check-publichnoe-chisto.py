#!/usr/bin/env python3
"""Гейт: в публичной сборке нет следов ЭТОЙ установки.

Судит собранное дерево и его историю — то есть РЕЗУЛЬТАТ, а не источник.
Образцы берутся независимо от списка сборщика: сборщик, проверяющий сам
себя, зелен по построению («что заменил — того и нет»), и след, которого в
его данных нет, уехал бы наружу молча.

Источники образцов:
  * значения ключей паспорта установки (имя и путь проекта, номер чата
    владельца, имя продукта и его прозвища, домашний каталог агента);
  * адреса хранилищ, на которые смотрит пакет (git remote);
  * почты авторов коммитов проекта;
  * имя сервера.

Чего здесь нарочно НЕТ: имени пользователя самого по себе, режима работы,
каталога журналов. Замер дна 12.09.2026: «agent» встречается в дереве 199
раз, «semi» 68, /var/log/harness 93 — гейт с такими образцами был бы красным
всегда, а краснеющий без причины гейт отключают. Домашний путь /home/<имя>
ловится целиком — он и есть след.

В выводе — ИМЯ ключа, а не значение: вывод уезжает в журнал публикации и в
канал, и печатать там номер чата владельца незачем.

Запуск:
  python3 scripts/check-publichnoe-chisto.py <каталог сборки>
  python3 scripts/check-publichnoe-chisto.py --selftest
"""
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
from konf import из_файла  # noqa: E402

ПАСПОРТ = "/etc/harness/install.conf"
КОНФИГ = "/etc/harness/harness.conf"
ОБРАЗЦЫ_ПАСПОРТА = ("PROJECT_NAME", "PROJECT_DIR", "TG_CHAT_ID", "PRODUCT_DIR")
ДВОИЧНЫЕ = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".woff", ".woff2"}


def довод(имя: str, умолчание: str | None = None) -> str | None:
    if имя in sys.argv:
        место = sys.argv.index(имя) + 1
        if место < len(sys.argv):
            return sys.argv[место]
    return умолчание


def образцы(паспорт: dict, пакет: Path, проект: Path) -> list:
    """Пары «значение → имя образца». Пустые значения пропускаются.

    Пустое значение — не «совпало со всем», а «ключ не заполнен»: PRODUCT_DIR
    на свежей установке пуст, и поиск пустой строки нашёл бы каждый файл.
    """
    пары = []
    for ключ in ОБРАЗЦЫ_ПАСПОРТА:
        значение = (паспорт.get(ключ) or "").strip()
        if значение:
            пары.append((значение, ключ))
    for прозвище in (паспорт.get("PRODUCT_ALIASES") or "").split():
        if прозвище.strip():
            пары.append((прозвище.strip(), "PRODUCT_ALIASES"))
    пользователь = (паспорт.get("AGENT_USER") or "").strip()
    if пользователь:
        пары.append((f"/home/{пользователь}", "домашний каталог агента"))

    for каталог, имя in ((пакет, "адрес хранилища пакета"), (проект, "адрес хранилища проекта")):
        готово = subprocess.run(["git", "-C", str(каталог), "remote", "-v"],
                                capture_output=True, text=True, timeout=30)
        for строка in готово.stdout.splitlines():
            части = строка.split()
            if len(части) >= 2 and части[1]:
                пары.append((части[1], имя))

    почты = subprocess.run(["git", "-C", str(проект), "log", "--format=%ae"],
                           capture_output=True, text=True, timeout=60)
    for почта in sorted(set(почты.stdout.split())):
        # noreply-адреса не след установки: они одинаковы у всех и стоят в
        # публичной истории любого проекта.
        if почта and "noreply" not in почта:
            пары.append((почта, "почта автора коммитов"))

    хост = socket.gethostname().strip()
    if хост and хост not in ("localhost",):
        пары.append((хост, "имя сервера"))

    # Длинные образцы первыми: находка по пути важнее находки по имени внутри
    # него, и в отчёте должен стоять точный образец.
    пары.sort(key=lambda п: len(п[0]), reverse=True)
    return пары


def найти_в_дереве(сборка: Path, пары: list) -> list:
    находки = []
    for файл in sorted(сборка.rglob("*")):
        if not файл.is_file() or ".git" in файл.parts:
            continue
        if файл.suffix.lower() in ДВОИЧНЫЕ:
            continue
        try:
            текст = файл.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for номер, строка in enumerate(текст.splitlines(), 1):
            for значение, имя in пары:
                if значение in строка:
                    находки.append(f"{файл.relative_to(сборка)}:{номер}: образец={имя}")
                    break
    return находки


def найти_в_истории(сборка: Path, пары: list) -> list:
    """История судится отдельно: в .git обычный поиск слеп — объекты сжаты."""
    if not (сборка / ".git").exists():
        return []
    готово = subprocess.run(["git", "-C", str(сборка), "log", "--all", "--format=%H%n%B"],
                            capture_output=True, text=True, timeout=120)
    находки = []
    коммит = "?"
    for строка in готово.stdout.splitlines():
        if len(строка) == 40 and all(з in "0123456789abcdef" for з in строка):
            коммит = строка[:8]
            continue
        for значение, имя in пары:
            if значение in строка:
                находки.append(f"история {коммит}: образец={имя}")
                break
    return находки


def проверить(сборка: Path, пары: list) -> list:
    return найти_в_дереве(сборка, пары) + найти_в_истории(сборка, пары)


def main() -> int:
    свои = [д for д in sys.argv[1:] if not д.startswith("--")]
    паспорт = из_файла(довод("--паспорт", ПАСПОРТ))
    паспорт.update(из_файла(довод("--конфиг", КОНФИГ)))
    сборка = Path(свои[0] if свои else паспорт.get("PUBLIC_BUILD_DIR", ""))
    if not str(сборка).strip() or not сборка.is_dir():
        print(f"[чистота] сборки нет: {сборка or '(путь не задан)'} — проверять нечего",
              file=sys.stderr)
        return 2

    пакет = Path(довод("--пакет") or f"/home/{паспорт.get('AGENT_USER', 'agent')}/starter/СТАРТОВЫЙ-ПАКЕТ")
    проект = Path(довод("--проект") or паспорт.get("PROJECT_DIR") or str(КОРЕНЬ))
    находки = проверить(сборка, образцы(паспорт, пакет, проект))
    if находки:
        for строка in находки[:50]:
            print(f"  {строка}")
        if len(находки) > 50:
            print(f"  … и ещё {len(находки) - 50}")
        print(f"[чистота] в публичной сборке следы установки: {len(находки)}")
        return 1
    print("[чистота] в публичной сборке следов установки нет")
    return 0


# ── самотест: подставная сборка, девять путей ───────────────────────────────
def _подставная_сборка(корень: Path, паспорт: dict) -> Path:
    сборка = корень / "сборка"
    (сборка / "харнес").mkdir(parents=True)
    (сборка / "README.md").write_text("Ставится куда угодно.\n", encoding="utf-8")
    (сборка / "харнес" / "код.py").write_text("ПОРОГ = 15\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(сборка), "init", "-q", "-b", "master"], check=True)
    subprocess.run(["git", "-C", str(сборка), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(сборка), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "версия от 2026-09-12"], check=True)
    return сборка


def самотест() -> int:
    """Гейт обязан ловить то, чего сборщик не знает."""
    путей = неудач = 0

    def проба(ок: bool, текст: str) -> None:
        nonlocal путей, неудач
        путей += 1
        неудач += 0 if ок else 1
        print(("  ок    " if ок else "  ПЛОХО ") + текст)

    with tempfile.TemporaryDirectory(prefix="самотест-чистоты-") as врем:
        корень = Path(врем)
        пакет = корень / "пакет"
        пакет.mkdir()
        subprocess.run(["git", "-C", str(пакет), "init", "-q", "-b", "master"], check=True)
        subprocess.run(["git", "-C", str(пакет), "remote", "add", "jig",
                        "git@подставной-хост:кто-то/репо.git"], check=True)
        проект = корень / "проект"
        проект.mkdir()
        subprocess.run(["git", "-C", str(проект), "init", "-q", "-b", "master"], check=True)
        (проект / "ф").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(проект), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(проект), "-c", "user.name=t",
                        "-c", "user.email=хозяин@подставной.рф", "commit", "-qm", "п"], check=True)

        паспорт = {"PROJECT_NAME": "Подставной", "PROJECT_DIR": "/srv/подставной",
                   "TG_CHAT_ID": "987654321", "PRODUCT_DIR": "", "AGENT_USER": "подставной",
                   "PRODUCT_ALIASES": "ПРОДУКТ продукт"}
        пары = образцы(паспорт, пакет, проект)
        сборка = _подставная_сборка(корень, паспорт)

        # БОЛЬНОЙ первым: след, которого в данных сборщика нет вовсе.
        (сборка / "харнес" / "код.py").write_text(
            'CHAT = "987654321"\n', encoding="utf-8")
        находки = проверить(сборка, пары)
        проба(any("TG_CHAT_ID" in н for н in находки),
              "БОЛЬНОЙ: номер чата владельца в коде сборки — находка")
        проба(all("987654321" not in н for н in находки),
              "БОЛЬНОЙ: в выводе стоит ИМЯ образца, а не его значение")

        # Коммит с нашим путём в СООБЩЕНИИ: дерево при этом чистое. Файл меняем
        # на другое содержимое, иначе коммитить нечего и git отвечает отказом.
        (сборка / "харнес" / "код.py").write_text("ПОРОГ = 16\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(сборка), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(сборка), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "правка в /srv/подставной"], check=True)
        находки = проверить(сборка, пары)
        проба(any(н.startswith("история") for н in находки),
              "БОЛЬНОЙ: след только в сообщении коммита — дерево чистое, а гейт красный")

        subprocess.run(["git", "-C", str(сборка), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--amend", "-m", "версия от 2026-09-12"], check=True)

        (сборка / "README.md").write_text(
            "git clone git@подставной-хост:кто-то/репо.git\n", encoding="utf-8")
        проба(any("адрес хранилища" in н for н in проверить(сборка, пары)),
              "БОЛЬНОЙ: адрес нашего хранилища в документе — находка")

        (сборка / "README.md").write_text("Ставится куда угодно.\n", encoding="utf-8")
        (сборка / "харнес" / "код.py").write_text(
            'ПУТЬ = "/home/подставной/starter"\nИМЯ = "подставной пользователь"\n',
            encoding="utf-8")
        находки = проверить(сборка, пары)
        проба(any("домашний каталог" in н for н in находки),
              "БОЛЬНОЙ: домашний каталог агента — находка")
        проба(len([н for н in находки if "код.py" in н]) == 1,
              "имя пользователя само по себе находкой не считается")

        (сборка / "харнес" / "код.py").write_text(
            f'СЕРВЕР = "{socket.gethostname()}"\n', encoding="utf-8")
        проба(any("имя сервера" in н for н in проверить(сборка, пары)),
              "БОЛЬНОЙ: имя сервера в файле — находка")

        (сборка / "харнес" / "код.py").write_text("ПОРОГ = 15\n", encoding="utf-8")
        проба(проверить(сборка, пары) == [],
              "здоровый: чистое дерево и чистая история — находок нет")

        # Пустой ключ не должен совпадать со всем подряд.
        проба(all(имя != "PRODUCT_DIR" for _, имя in пары),
              "пустой PRODUCT_DIR в образцы не попал")

        # Образцы из ФАЙЛА: подменённое окружение вердикта не меняет.
        образец_файла = корень / "паспорт.conf"
        образец_файла.write_text('PROJECT_DIR="/из-файла"\n', encoding="utf-8")
        os.environ["PROJECT_DIR"] = "/из-окружения"
        проба(из_файла(str(образец_файла))["PROJECT_DIR"] == "/из-файла",
              "образцы берутся из файла паспорта, а не из окружения")
        os.environ.pop("PROJECT_DIR", None)

    if неудач:
        print("САМОТЕСТ ПРОВАЛЕН")
        return 1
    print(f"САМОТЕСТ ПРОЙДЕН: {путей} путей, первым — больной случай")
    return 0


if __name__ == "__main__":
    sys.exit(самотест() if "--selftest" in sys.argv else main())
