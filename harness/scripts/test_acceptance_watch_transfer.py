#!/usr/bin/env python3
"""Приёмка: карта не проходит валидатор после переноса в архив — причина видна.

Копия сети 20.11.2026, 04:47: acceptance-watch убрал закрытые задачи
в архив, валидатор карты вернул не 0, демон прислал тревогу «нужен разбор» и
вышел с кодом 1, а вывод валидатора ушёл в /dev/null — причину не восстановить.
Карта и архив при этом остались записанными: битая карта не даёт закоммитить
ни одной правки карты (pre-commit судит dev-map.yaml в каждом коммите с ней).

Стенд — свой PROJECT_DIR: подставные acceptance.py (свод без провалов),
archive-map.py (дописывает в карту и архив свою отметку, как перенос),
pre-commit-gate.sh (краснеет с узнаваемой строкой, если в карте отметка
переноса) и tg_send.sh (пишет отправленное в файл). Демон берётся из двери
PROBE_ACCEPTANCE_WATCH (по умолчанию — из дерева рядом с пробой); копия-мутант
кладётся рядом с подлинником: демон находит scripts/lib/config.sh по своему пути.

Случаи:
  А1 строки вывода валидатора есть в журнале демона (его stdout/stderr — это
     журнал cron) и в тексте тревоги владельцу;
  А2 перенос, после которого здоровая карта стала больной, откачен: карта и
     архив байт в байт как до переноса;
  А3 карта красная ещё до переноса — перенос не откатывается, тревога говорит
     «карта красная до переноса» и несёт причину.

Прогон: python3 scripts/test_acceptance_watch_transfer.py   (0 — все случаи прошли)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ДЕМОН = Path(os.environ.get("PROBE_ACCEPTANCE_WATCH") or next(
    п for п in (HERE.parent / "harness" / "demons" / "acceptance-watch.sh",
                HERE.parent / "demons" / "acceptance-watch.sh") if п.is_file()))

ПРИЧИНА = "[хакатон] у t.хак нет карточки долга: проба-причина-7f3"
ОТМЕТКА = "# перенесено-пробой\n"
КАРТА = ("version: 1\nlines:\n  - id: harness\n    layer: infra\n    name: \"Ядро\"\n"
         "    tasks:\n      - id: t.хак\n        status: test\n        title: \"x\"\n"
         "        summary: \"x\"\n")
АРХИВ = "# Архив карты разработки: закрытые задачи.\nversion: 1\nlines:\n"

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


def записать(путь: Path, текст: str, исполняемый: bool = False) -> None:
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(текст, encoding="utf-8")
    if исполняемый:
        путь.chmod(0o755)


def стенд(двор: Path) -> Path:
    проект = двор / "проект"
    s = проект / "scripts"
    записать(s / "acceptance.py",
             '#!/usr/bin/env python3\nprint(\'{"итог": {"доказано": 1}}\')\n', True)
    записать(s / "archive-map.py",
             "#!/usr/bin/env python3\n"
             "from pathlib import Path\n"
             f"for имя in ('dev-map.yaml', 'docs/dev-map-archive.yaml'):\n"
             f"    п = Path(имя); п.write_text(п.read_text(encoding='utf-8') + {ОТМЕТКА!r}, encoding='utf-8')\n"
             "print('[архив карты] закрытых задач: 1, это 10 знаков; в карте сейчас 100')\n", True)
    записать(s / "pre-commit-gate.sh",
             "#!/usr/bin/env bash\n"
             f"if grep -q 'перенесено-пробой' \"$2\"; then echo '[dev-map] схема в порядке'; "
             f"echo '{ПРИЧИНА}'; exit 1; fi\n"
             "echo '[dev-map] схема в порядке'; exit 0\n", True)
    записать(s / "tg_send.sh",
             '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "${OTPRAVLENO:?}"\nexit 0\n', True)
    записать(проект / "dev-map.yaml", КАРТА)
    записать(проект / "docs" / "dev-map-archive.yaml", АРХИВ)
    (проект / "docs" / "приёмка").mkdir(parents=True, exist_ok=True)
    записать(двор / "конф" / "install.conf", f'PROJECT_DIR="{проект}"\n')
    записать(двор / "конф" / "harness.conf", "")
    return проект


def прогнать(двор: Path, карта_до: str) -> tuple:
    """Демон на своём стенде → (журнал, тревога, карта после, архив после)."""
    проект = стенд(двор)
    записать(проект / "dev-map.yaml", карта_до)
    отправлено = двор / "отправлено.log"
    отправлено.write_text("", encoding="utf-8")
    окружение = {**os.environ, "PROJECT_DIR": str(проект), "LOG_DIR": str(двор / "лог"),
                 "HEARTBEAT_DIR": str(двор / "метки"), "HARNESS_HOME": str(двор / "конф"),
                 "HARNESS_INSTALL_CONF": str(двор / "конф" / "install.conf"),
                 "HARNESS_CONF": str(двор / "конф" / "harness.conf"),
                 "HARNESS_ACTIVE_LINE": "harness", "OTPRAVLENO": str(отправлено)}
    готово = subprocess.run(["bash", str(ДЕМОН)], env=окружение, capture_output=True,
                            text=True, timeout=120)
    журнал = готово.stdout + готово.stderr
    if "карта не проходит валидатор" not in журнал:
        журнал += (f"\n[проба] стенд не дошёл до тревоги переноса (код {готово.returncode}); "
                   f"хвост: {журнал.strip().splitlines()[-3:]!r}")
    return (журнал, отправлено.read_text(encoding="utf-8"),
            (проект / "dev-map.yaml").read_text(encoding="utf-8"),
            (проект / "docs" / "dev-map-archive.yaml").read_text(encoding="utf-8"))


def main() -> int:
    дворы = [Path(tempfile.mkdtemp(prefix="proba-acceptance-transfer-")) for _ in range(2)]
    try:
        журнал, тревога, карта, архив = прогнать(дворы[0], КАРТА)
        беды = [с for с in журнал.splitlines() if с.startswith("[проба]")]
        if ПРИЧИНА not in журнал:
            беды.append("в журнале демона нет строки вывода валидатора")
        if ПРИЧИНА not in тревога:
            беды.append(f"в тексте тревоги нет причины: {тревога.strip()!r}")
        судить("А1 причина (вывод валидатора) в журнале и в тревоге", беды)

        беды = []
        if карта != КАРТА:
            беды.append("карта осталась после переноса, который её сломал")
        if архив != АРХИВ:
            беды.append("архив остался после откаченного переноса")
        судить("А2 перенос, сломавший здоровую карту, откачен: карта и архив как до него", беды)

        # Карта красная ещё до приёмки: откат вернул бы ту же больную карту и спрятал бы
        # перенос — перенос остаётся, тревога говорит, что карта была красной до него.
        журнал, тревога, карта, архив = прогнать(дворы[1], КАРТА + ОТМЕТКА)
        беды = [с for с in журнал.splitlines() if с.startswith("[проба]")]
        if карта != КАРТА + ОТМЕТКА + ОТМЕТКА or архив != АРХИВ + ОТМЕТКА:
            беды.append("перенос откачен, хотя карта была красной до него")
        if "красная до переноса" not in тревога or ПРИЧИНА not in тревога:
            беды.append(f"тревога не говорит «красная до переноса» с причиной: {тревога.strip()!r}")
        судить("А3 карта красная до переноса — перенос не откачен, тревога это называет", беды)
    finally:
        for двор in дворы:
            shutil.rmtree(двор, ignore_errors=True)
    print(f"путей: {случаев}, неудач: {неудач}")
    return 1 if неудач else 0


if __name__ == "__main__":
    sys.exit(main())
