#!/usr/bin/env python3
"""Откат обновления возвращает живой harness.conf к состоянию до укладки.

Доустановка (install-new.py) прямого прогона дописывает в живой конфиг метки
HEARTBEAT_PERIODS и ключи новой версии. Откат снимает файлы по журналу укладки,
строки crontab и юниты новой версии — а конфиг оставлял: у копии сети
20.11.2026 после отката остались метки batch_queue:30 и claude-mem-fix:1440 без
кода и без строки cron, и сторож пульса объявил оба демона МЁРТВЫМИ.

Договор тот же, что у crontab: откат снимает ровно то, что дописала доустановка
прямого прогона, и не трогает дописанное владельцем после неё.

Стенд — свой каталог: harness.conf (дверь HARNESS_CONF), crontab-файл (свой
crontab первым в PATH), пустой каталог юнитов (HARNESS_SYSTEMD_DIR). Доустановка
подменена в subprocess модуля: прямой прогон пишет конфиг, как настоящая.
Обновлятор берётся из двери PROBE_UPDATER (по умолчанию рядом с пробой) — так
проба судит мутанта в копии.

Случаи:
  К1 откат возвращает конфиг байт в байт к состоянию до укладки;
  К2 строка, дописанная владельцем после укладки, откатом не снимается,
     а дописанное доустановкой — снимается;
  К3 доустановка конфиг не меняла — откат его не переписывает;
  К4 слова канала называют снятые ключи, но не значения: значение бывает паролем.

Прогон: python3 scripts/test_update_rollback_conf.py   (0 — все случаи прошли)
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPDATER = Path(os.environ.get("PROBE_UPDATER") or HERE / "update.py")

ДО = 'PROJECT_NAME="стенд"\nHEARTBEAT_PERIODS="backup:1440 own-job:5"\n'
ПОСЛЕ = ('PROJECT_NAME="стенд"\n'
         'HEARTBEAT_PERIODS="backup:1440 own-job:5 batch_queue:30 claude-mem-fix:1440"\n'
         'BATCH_QUEUE_MAX="4"\n')
СВОЯ_СТРОКА = 'OWNER_KEY="1"\n'

случаев = неудач = 0


def судить(имя: str, беды: list) -> None:
    global случаев, неудач
    случаев += 1
    if беды:
        неудач += 1
        print(f"ПЛОХО · {имя}")
        for беда in беды:
            print(f"    {беда}")
    else:
        print(f"ок  · {имя}")


def загрузить_обновлятор():
    спец = importlib.util.spec_from_file_location("обновление", UPDATER)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


class Доустановка:
    """subprocess модуля обновления: install-new.py пишет в конфиг то, что
    задано в `запишет` (None — конфиг не трогает); прочее — к настоящему."""

    def __init__(self, конфиг: Path):
        self.конфиг = конфиг
        self.запишет = None

    def __getattr__(self, имя):
        return getattr(subprocess, имя)

    def run(self, команда, *доводы, **ключи):
        if any(str(ч).endswith("install-new.py") for ч in команда):
            if self.запишет is not None:
                self.конфиг.write_text(self.запишет, encoding="utf-8")
            return subprocess.CompletedProcess(команда, 0, "", "")
        return subprocess.run(команда, *доводы, **ключи)


def стенд() -> Path:
    двор = Path(tempfile.mkdtemp(prefix="proba-rollback-conf-"))
    (двор / "bin").mkdir()
    (двор / "юниты").mkdir()
    крон = двор / "crontab.txt"
    крон.write_text("*/5 * * * * /opt/x/own.sh\n", encoding="utf-8")
    (двор / "bin" / "crontab").write_text(
        "#!/bin/bash\n"
        f"case \"$1\" in\n  -l) exec cat '{крон}' ;;\n"
        f"  -) cat > '{крон}'; exit 0 ;;\nesac\nexit 2\n", encoding="utf-8")
    (двор / "bin" / "crontab").chmod(0o755)
    return двор


def прогнать(обновление, двор: Path, имя: str, вперёд_пишет, после_укладки=None) -> Path:
    """Прямой прогон, затем (по желанию) правка владельца, затем откат → путь конфига."""
    конфиг = двор / f"{имя}.conf"
    конфиг.write_text(ДО, encoding="utf-8")
    os.environ["HARNESS_CONF"] = str(конфиг)
    доустановка = Доустановка(конфиг)
    обновление.subprocess = доустановка
    вперёд, назад = двор / f"{имя}-архив", двор / f"{имя}-архив-откат"
    вперёд.mkdir()
    назад.mkdir()
    доустановка.запишет = вперёд_пишет
    обновление._разложить_пакет(archive=вперёд, rollback_of=None)
    if после_укладки is not None:
        после_укладки(конфиг)
    доустановка.запишет = None   # прежняя версия меток новой не знает и ничего не пишет
    обновление._разложить_пакет(archive=назад, rollback_of=вперёд)
    return конфиг


def main() -> int:
    обновление = загрузить_обновлятор()
    двор = стенд()
    подмены = {"_launch_layout": lambda *_a, **_k: True,
               "_напечатать_эталон": lambda: None, "_пересобрать_карточки": lambda: False,
               "пересобрать_производные": lambda *_a, **_k: [],
               "_записать_шаги_после_двери": lambda *_a: None}
    прежние = {имя: getattr(обновление, имя) for имя in (*подмены, "subprocess")}
    окружение = {к: os.environ.get(к) for к in ("PATH", "HARNESS_CONF", "HARNESS_SYSTEMD_DIR",
                                                "HARNESS_SYSTEMCTL")}
    try:
        for имя, значение in подмены.items():
            setattr(обновление, имя, значение)
        os.environ["PATH"] = f"{двор / 'bin'}:{окружение['PATH']}"
        os.environ["HARNESS_SYSTEMD_DIR"] = str(двор / "юниты")
        os.environ["HARNESS_SYSTEMCTL"] = "true"

        конфиг = прогнать(обновление, двор, "к1", ПОСЛЕ)
        стало = конфиг.read_bytes()
        судить("К1 откат вернул harness.conf байт в байт к состоянию до укладки",
               [] if стало == ДО.encode() else
               [f"после отката {стало.decode()!r}", f"ждали {ДО!r}"])

        def владелец_дописал(путь: Path) -> None:
            путь.write_text(путь.read_text(encoding="utf-8") + СВОЯ_СТРОКА, encoding="utf-8")

        конфиг = прогнать(обновление, двор, "к2", ПОСЛЕ, владелец_дописал)
        стало = конфиг.read_text(encoding="utf-8")
        судить("К2 откат снял дописанное доустановкой, строку владельца оставил",
               [] if стало == ДО + СВОЯ_СТРОКА else
               [f"после отката {стало!r}", f"ждали {ДО + СВОЯ_СТРОКА!r}"])

        def запомнить(путь: Path) -> None:
            запомнить.след = (путь.stat().st_ino, путь.stat().st_mtime_ns)
        конфиг = прогнать(обновление, двор, "к3", None, запомнить)
        след = (конфиг.stat().st_ino, конфиг.stat().st_mtime_ns)
        беды = []
        if конфиг.read_text(encoding="utf-8") != ДО:
            беды.append(f"конфиг изменён: {конфиг.read_text(encoding='utf-8')!r}")
        if след != запомнить.след:
            беды.append("конфиг переписан без нужды (сменились inode или mtime)")
        судить("К3 доустановка конфиг не меняла — откат его не переписывает", беды)

        архив = двор / "к4-архив-откат"
        архив.mkdir()
        приманка = 'STAND_PG="user=a password=p4ss-7f3"\n'  # не-секрет: приманка пробы И-3
        (архив / "harness.conf.снято").write_text(приманка + 'BATCH_QUEUE_MAX="4"\n', encoding="utf-8")
        слова = обновление._слова_о_снятом_конфиге(архив)
        беды = [f"в словах канала значение строки конфига: {слова!r}"] if "p4ss-7f3" in слова else []
        if "STAND_PG" not in слова or "BATCH_QUEUE_MAX" not in слова:
            беды.append(f"слова не называют снятые ключи: {слова!r}")
        судить("К4 слова канала об откате называют ключи, не значения (И-3)", беды)
    except Exception as беда:  # noqa: BLE001 — сбой пробы и есть находка
        судить("стенд отката конфига", [f"исключение {type(беда).__name__}: {беда}"])
    finally:
        for имя, значение in прежние.items():
            setattr(обновление, имя, значение)
        for ключ, значение in окружение.items():
            if значение is None:
                os.environ.pop(ключ, None)
            else:
                os.environ[ключ] = значение
        shutil.rmtree(двор, ignore_errors=True)
    print(f"путей: {случаев}, неудач: {неудач}")
    return 1 if неудач else 0


if __name__ == "__main__":
    sys.exit(main())
