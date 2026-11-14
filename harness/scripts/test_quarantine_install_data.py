#!/usr/bin/env python3
"""Карантин ворот — данные установки: сборка везёт пустую заготовку, раскладка
карантин копии не перезаписывает.

Откуда взято. Копия bkb 13.11.2026 не встала на сборку b390c75: пакетный
harness/config/gates-quarantine.yaml вёз 30 записей НАШЕЙ установки (наши
гейты, наши карточки hr.qr-fix-*), суд карантина на копии назвал каждую
«карточки нет в карте» — 31 «карантин: …», автооткат. Слово владельца: «нельзя
чтобы улетало в обновления наши карточки». Спека —
спека задачи, раздел
«Карточки проекта не едут в сборку».

Случаи:
  К1 данные сборки подменяют gates-quarantine.yaml заготовкой, и заготовка —
     «карантин: []» (ни одной записи);
  К2 сборщик на стенд-пакете с записями карантина кладёт в дерево сборки файл
     без записей (судится РЕЗУЛЬТАТ сборки, а не только данные);
  К3 раскладчик держит gates-quarantine.yaml в LIVE_SETTINGS (кладёт, только
     если файла в проекте нет);
  К4 модуль живых настроек: свой карантин копии после сверки с пакетом цел
     побайтно;
  К5 модуль: карантин копии, равный прошлой ПАКЕТНОЙ версии (наши записи,
     приехавшие сборкой b390c75), заменяется нынешней пакетной — заготовкой.

Стенды — свой mkdtemp, пакет стенда — свой git. Живые проект, пакет, /etc не
пишутся; данные сборки и заготовка читаются из дерева, где лежит проба.
Итог: «КАРАНТИН УСТАНОВКИ: путей N, неудач M», код 0/1.
"""
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from config import katalog_paketa, корень_дерева  # noqa: E402

TREE = корень_дерева(__file__)
DATA = TREE / "harness" / "config" / "public-build.yaml"
BUILDER = HERE / "build-public.py"
MODULE = HERE / "lib" / "live_settings.py"
TARGET = "harness/config/gates-quarantine.yaml"

OUR_RECORDS = """\
карантин:
  - гейт: "самотест раскладчика"
    карточка: hr.stend-nasha-kartochka
    заведено: "2026-11-13"
    срок: "2026-11-20"
    причина: "запись установки-источника"
"""
COPY_RECORDS = """\
# карантин копии: её гейт, её карточка
карантин:
  - гейт: "свой гейт копии"
    карточка: hr.kartochka-kopii
    заведено: "2026-11-14"
    срок: "2026-11-18"
    причина: "своя запись копии"
"""

paths = failures = 0


def check(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + text + ("" if ok else f": {detail}"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=proba",
                           "-c", "user.email=proba@stand", "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def records_of(text: str):
    """Список записей карантина; None — файл не разобран или не словарь."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("карантин")


def template_path() -> str | None:
    data = yaml.safe_load(DATA.read_text(encoding="utf-8")) or {}
    return (data.get("заменить_шаблоном") or {}).get(TARGET)


def case_k1() -> None:
    template = template_path()
    check(template is not None, f"К1 «заменить_шаблоном» в {DATA.name} подменяет {TARGET}",
          f"записи нет: {sorted((yaml.safe_load(DATA.read_text(encoding='utf-8')) or {}).get('заменить_шаблоном') or {})}")
    if template is None:
        return
    source = TREE / template
    check(source.is_file(), f"К1 заготовка {template} лежит в дереве", str(source))
    if source.is_file():
        records = records_of(source.read_text(encoding="utf-8"))
        check(records == [], "К1 заготовка — «карантин: []»", f"карантин = {records!r}")


def case_k2(root: Path) -> None:
    template = template_path()
    if template is None or not (TREE / template).is_file():
        check(False, "К2 сборка кладёт пустой карантин", "в данных сборки нет заготовки карантина")
        return
    package = root / "пакет"
    (package / "harness" / "config").mkdir(parents=True)
    (package / TARGET).write_text(OUR_RECORDS, encoding="utf-8")
    (package / template).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TREE / template, package / template)
    git(package, "init", "-q")
    git(package, "add", "-A")
    git(package, "commit", "-qm", "стенд")
    project = root / "проект"
    project.mkdir()
    build = root / "сборка"
    build.mkdir()
    spec = importlib.util.spec_from_file_location("build_public_stand", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    passport = {"PROJECT_DIR": str(project), "PROJECT_NAME": "Подставной", "AGENT_USER": "podstavnoj"}
    builder.собрать(package, str(build), {"заменить_шаблоном": {TARGET: template}}, passport)
    built = (build / TARGET).read_text(encoding="utf-8")
    check(records_of(built) == [], "К2 в дереве сборки gates-quarantine.yaml без записей",
          f"карантин = {records_of(built)!r}")
    check("hr.stend-nasha-kartochka" not in built, "К2 карточка источника в сборку не уехала")


def layout_script() -> Path:
    beside = HERE.parent / "razlozhit-harnes.sh"       # пакет: harness/scripts/..
    return beside if beside.is_file() else katalog_paketa() / "harness" / "razlozhit-harnes.sh"


def case_k3() -> None:
    script = layout_script()
    match = re.search(r'^LIVE_SETTINGS="([^"]*)"', script.read_text(encoding="utf-8"), re.M)
    names = match.group(1).split() if match else []
    check(bool(names), f"К3 LIVE_SETTINGS найден в {script}", "строки нет")
    check("gates-quarantine.yaml" in names,
          "К3 gates-quarantine.yaml в LIVE_SETTINGS (кладётся, только если его нет)",
          f"LIVE_SETTINGS = {names}")


def load_module():
    if not MODULE.is_file():
        return None
    spec = importlib.util.spec_from_file_location("live_settings", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_with_versions(root: Path, versions: list) -> Path:
    repo = root / "пакет-версий"
    (repo / "harness" / "config").mkdir(parents=True)
    git(repo, "init", "-q")
    path = repo / TARGET
    for number, text in enumerate(versions):
        path.write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"v{number}")
    return path


def case_k4_k5(root: Path) -> None:
    module = load_module()
    if module is None:
        check(False, "К4 свой карантин копии цел после сверки", f"нет модуля {MODULE}")
        check(False, "К5 наш карантин на копии заменён заготовкой", f"нет модуля {MODULE}")
        return
    empty = (TREE / (template_path() or "")).read_text(encoding="utf-8") \
        if template_path() and (TREE / template_path()).is_file() else "карантин: []\n"
    package = package_with_versions(root, [OUR_RECORDS, empty])
    known = module.package_blobs(package)

    live = root / "копия-4" / TARGET
    live.parent.mkdir(parents=True)
    live.write_text(COPY_RECORDS, encoding="utf-8")
    module.settle(live, package, known)
    check(live.read_text(encoding="utf-8") == COPY_RECORDS, "К4 свой карантин копии цел побайтно",
          f"стал: {live.read_text(encoding='utf-8')[:200]!r}")

    live = root / "копия-5" / TARGET
    live.parent.mkdir(parents=True)
    live.write_text(OUR_RECORDS, encoding="utf-8")
    said = "\n".join(module.settle(live, package, known))
    check(live.read_bytes() == package.read_bytes(),
          "К5 наш карантин (прошлая пакетная версия) заменён нынешней пакетной",
          f"записей осталось {records_of(live.read_text(encoding='utf-8'))!r}; итог {said!r}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="proba-quarantine-install-"))
    try:
        for label, case in (("К1", case_k1), ("К2", lambda: case_k2(root / "к2")),
                            ("К3", case_k3), ("К4-К5", lambda: case_k4_k5(root / "к45"))):
            try:
                case()
            except Exception as error:      # поломка пробы — красный случай, не трасса
                check(False, f"{label} проба упала", f"{type(error).__name__}: {error}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"КАРАНТИН УСТАНОВКИ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
