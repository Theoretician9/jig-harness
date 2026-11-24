#!/usr/bin/env python3
"""Проба: обновление не раскладывает пакет, где один файл пары новый, а другой прежний.

Находка копии сети 20.11.2026 (обновление до 0.14.3): после укладки
`harness/config/mechanisms.yaml` новый, а `scripts/gates.sh` прежний — «кто зовёт»
краснеет (91 механизм без вызывающего), в журнале укладки строки о gates.sh нет.
Так же прежним остался `scripts/acceptance.py` (строка 1441 — редакция 07.11 без
маркера «не-секрет»), и новая check-secrets.sh поймала её.

Механизм на master: `update.разбор()` относит файл пакета, правленный здесь и
изменённый в обновлении, к «расхождениям»; `_поставить_под_замком` после
`merge --ff-only` возвращает его из копии (`_вернуть_из_копии`) целиком прежним,
и раскладчик кладёт уже прежний файл — для двери он «тот же», в журнал не идёт.
Соседний файл того же обновления при этом новый: пара разъехалась до раскладки.

Судится боевой `поставить()` на стенде: подставной публичный репозиторий и его
клон — пакет (HARNESS_STARTER), свои паспорт, конфиг, журналы и каталог копий.
`разложить` подменён: он запоминает, какими лежат файлы пары в пакете в миг
раскладки. Годно одно из двух: раскладка не звана и пакет не сдвинут (отказ до
слияния с названным файлом), либо в миг раскладки оба файла пары новые.
Сосед: обновление без правок здесь доходит до раскладки с новой парой — иначе
пробу озеленил бы отказ любому обновлению.

Дверь мутаций: UPDATE_UNDER_TEST — путь к копии update.py.

    python3 scripts/test_update_kept_file_pair.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
SUBJECT = Path(os.environ.get("UPDATE_UNDER_TEST") or КОРЕНЬ / "scripts" / "update.py")
ВОРОТА = "harness/scripts/gates.sh"
РЕЕСТР = "harness/config/mechanisms.yaml"


def гит(*доводы, где):
    готово = subprocess.run(["git", *доводы], cwd=где, capture_output=True, text=True, timeout=60)
    if готово.returncode != 0 and доводы[0] not in ("merge-base",):
        raise RuntimeError(f"git {' '.join(доводы)}: {готово.stderr.strip()}")
    return готово.stdout.strip()


def записать(корень: Path, путь: str, текст: str) -> None:
    файл = корень / путь
    файл.parent.mkdir(parents=True, exist_ok=True)
    файл.write_text(текст, encoding="utf-8")


def стенд(корень: Path):
    """Публичный репозиторий с версией A (пара v1), клон-пакет на A, в публичном — B (пара v2)."""
    удалённый, исток, пакет = корень / "публичный.git", корень / "исток", корень / "пакет"
    гит("init", "-q", "--bare", "-b", "master", str(удалённый), где=корень)
    гит("clone", "-q", str(удалённый), str(исток), где=корень)
    for р in (исток,):
        гит("config", "user.email", "проба@харнес", где=р)
        гит("config", "user.name", "проба", где=р)
    записать(исток, ВОРОТА, 'проверка "а" python3 scripts/check-a.py\n')
    записать(исток, РЕЕСТР, "механизмы:\n- файл: scripts/check-a.py\n")
    гит("add", "-A", где=исток)
    гит("commit", "-q", "-m", "версия A", где=исток)
    гит("push", "-q", "origin", "master", где=исток)
    гит("clone", "-q", str(удалённый), str(пакет), где=корень)
    гит("config", "user.email", "проба@харнес", где=пакет)
    гит("config", "user.name", "проба", где=пакет)
    a = гит("rev-parse", "HEAD", где=пакет)
    # B: механизм check-b появился в реестре, и ворота его зовут — пара меняется вместе.
    записать(исток, ВОРОТА, 'проверка "а" python3 scripts/check-a.py\n'
                            'проверка "б" python3 scripts/check-b.py\n')
    записать(исток, РЕЕСТР, "механизмы:\n- файл: scripts/check-a.py\n- файл: scripts/check-b.py\n")
    гит("add", "-A", где=исток)
    гит("commit", "-q", "-m", "версия B", где=исток)
    гит("push", "-q", "origin", "master", где=исток)
    return пакет, a


def окружение(корень: Path, пакет: Path) -> None:
    журналы = корень / "журналы"
    (журналы / "locks").mkdir(parents=True, exist_ok=True)
    паспорт, конф = корень / "install.conf", корень / "harness.conf"
    паспорт.write_text(
        f'PROJECT_DIR="{корень / "проект"}"\nPROJECT_NAME="стенд"\n'
        f'AGENT_USER="{pwd.getpwuid(os.getuid()).pw_name}"\nLOG_DIR="{журналы}"\n'
        f'HEARTBEAT_DIR="{корень / "hb"}"\nSTATE_DIR="{корень / "state"}"\n', encoding="utf-8")
    конф.write_text(f'BACKUP_DIR="{корень / "копии"}"\nPUBLIC_BRANCH="master"\n'
                    f'PUBLIC_REMOTE="origin"\nSTARTER_DIR="{пакет}"\n', encoding="utf-8")
    двери = корень / "двери"
    двери.mkdir(exist_ok=True)
    for имя in ("alert-task", "finding", "tg"):
        (двери / имя).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (двери / имя).chmod(0o755)
    for ключ in ("PROJECT_DIR", "PROJECT_NAME", "LOG_DIR", "BACKUP_DIR", "STATE_DIR",
                 "HEARTBEAT_DIR", "STARTER_DIR", "PUBLIC_BRANCH", "PUBLIC_REMOTE"):
        os.environ[ключ] = ""
    os.environ.update({
        "HARNESS_SELFTEST": "1", "HARNESS_INSTALL_CONF": str(паспорт), "HARNESS_CONF": str(конф),
        "HARNESS_STARTER": str(пакет), "HARNESS_PUBLIC_BRANCH": "master",
        "HARNESS_PUBLIC_REMOTE": "origin", "HARNESS_VERSION_FILE": str(корень / "версия"),
        "HARNESS_BACKUP_DIR": str(корень / "копии-старой-двери"),
        "HARNESS_ALERT_TASK": str(двери / "alert-task"),
        "HARNESS_EVOLUTION_FINDING": str(двери / "finding"),
        "HARNESS_GATES_QUARANTINE": str(двери / "gates-quarantine.yaml"),
        "HARNESS_TG_SEND": str(двери / "tg")})


def загрузить():
    sys.path.insert(0, str(SUBJECT.resolve().parent / "lib"))
    спец = importlib.util.spec_from_file_location("update_kept_pair", SUBJECT)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def прогон(правка_здесь: bool):
    """→ (зовов раскладки, пара в миг раскладки | None, HEAD пакета до, после, ответ)."""
    корень = Path(tempfile.mkdtemp(prefix="o-update-pair-"))
    try:
        пакет, a = стенд(корень)
        окружение(корень, пакет)
        if правка_здесь:
            # Своя проверка копии вписана в ворота пакета и не закоммичена — «правлено здесь».
            with open(пакет / ВОРОТА, "a", encoding="utf-8") as fh:
                fh.write('проверка "своя" python3 scripts/check-own.py\n')
        модуль = загрузить()
        зовы = []

        def разложить(archive=None, rollback_of=None, **_):
            if rollback_of is None:
                зовы.append(((пакет / ВОРОТА).read_text(encoding="utf-8"),
                             (пакет / РЕЕСТР).read_text(encoding="utf-8")))
            return True, []

        ответ = модуль.поставить(
            разложить=разложить, перезапустить=lambda: True,
            проверить=lambda: Counter(), сказать=lambda *_a, **_k: None,
            спросить=lambda *_a, **_k: 1, сверить_корень=lambda: [])
        после = гит("rev-parse", "HEAD", где=пакет)
        return len(зовы), (зовы[0] if зовы else None), a, после, ответ
    finally:
        shutil.rmtree(корень, ignore_errors=True)


def main() -> int:
    if not SUBJECT.is_file():
        print(f"ПРОБА СЛОМАНА: нет {SUBJECT}")
        return 1
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    зовов, пара, до, после, ответ = прогон(правка_здесь=True)
    if зовов:
        ворота, реестр = пара
        новый_реестр = "check-b" in реестр
        новые_ворота = "check-b" in ворота
        case("БОЛЬНОЙ СЛУЧАЙ копии сети: ворота правлены здесь, обновление меняет и их, и реестр — "
             "в миг раскладки пара одной версии",
             новый_реестр == новые_ворота,
             f"реестр {'новый' if новый_реестр else 'прежний'}, gates.sh "
             f"{'новый' if новые_ворота else 'прежний'}; расхождения {ответ.get('расхождения')}")
    else:
        case("БОЛЬНОЙ СЛУЧАЙ копии сети: отказ до слияния — пакет не сдвинут, файл назван",
             после == до and not ответ.get("поставлено") and "gates.sh" in str(ответ.get("беда")),
             (после[:7], до[:7], ответ.get("беда")))

    зовов, пара, до, после, ответ = прогон(правка_здесь=False)
    case("сосед: без правок здесь обновление доходит до раскладки с новой парой",
         зовов == 1 and пара is not None and "check-b" in пара[0] and "check-b" in пара[1],
         (зовов, ответ.get("беда"), ответ.get("поставлено")))

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
