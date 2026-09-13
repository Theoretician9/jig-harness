#!/usr/bin/env python3
"""Гейт: каждый внутренний импорт ведёт в существующий модуль.

Откуда взят. Перевод имён файлов на латиницу (11.09.2026) переименовал 400
модулей и поправил ссылки — но формы «from .настройки import настройки» и
«from collectors.перенос_ремонтов import перенести» правило не покрывало, и
175 импортов остались указывать на исчезнувшие имена. Ни один сторож этого не
увидел: ruff проверяет ИМЕНА (F821), а не существование модуля, тесты по
правилу гоняются только перед выкатом, сборка фронта питона не касается.
Отказ был бы найден в проде — при первом обращении к двери API.

Гейт смотрит ровно одно: путь импорта разрешается в файл или каталог-пакет.
Чужие библиотеки не трогает — судит только то, что лежит в репозитории.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from konf import product_dir          # noqa: E402  (после правки sys.path)

# Корни импорта: откуда python видит модули. Каталог продукта — ДАННЫЕ
# (PRODUCT_DIR в harness.conf), а не имя, вшитое в харнес: у другого проекта
# он называется иначе, и гейт молча не нашёл бы ни одного корня.
# «<продукт>/tests» — свой корень: conftest кладёт его в sys.path, и
# «from _obshchee import …» ищется там.
ПРОДУКТ = product_dir()
КОРНИ_ИМПОРТА = tuple(filter(None, (ПРОДУКТ, ".",
                                    f"{ПРОДУКТ}/tests" if ПРОДУКТ else "")))
ПРОПУСК = tuple(filter(None, (".tasks/", f"{ПРОДУКТ}/.tasks/" if ПРОДУКТ else "",
                              "UNIFIED/", "node_modules/")))


def файлы():
    готово = subprocess.run(
        ["git", "-C", str(КОРЕНЬ), "-c", "core.quotepath=false", "ls-files", "*.py"],
        capture_output=True, text=True)
    if готово.returncode != 0:
        # На СВЕЖЕЙ установке git init ещё не сделан (это задание первой смены),
        # и гейт валился трассировкой CalledProcessError — ворота новой машины
        # краснели там, где проверять просто нечем (живой прогон 13.09.2026).
        print("[импорты] нечем проверять: дерево ещё не git-репозиторий "
              f"({(готово.stderr or '').strip()[:120]})")
        sys.exit(77)
    вывод = готово.stdout
    for путь in вывод.splitlines():
        if путь and not путь.startswith(ПРОПУСК):
            да = КОРЕНЬ / путь
            if да.exists():
                yield путь, да


def существует(модуль: str, база: Path) -> bool:
    """Модуль есть, если рядом лежит «имя.py» или каталог-пакет «имя/»."""
    кусок = Path(*модуль.split("."))
    return (база / кусок).with_suffix(".py").exists() or (база / кусок).is_dir()


КИРИЛЛИЦА = re.compile(r"[А-Яа-яЁё]")


def мёртвое_имя(модуль: str) -> bool:
    """Кириллица в пути импорта — всегда мёртвая ссылка.

    `свой()` ищет модуль среди корней импорта и, не найдя, считает его чужой
    библиотекой — то есть ИСЧЕЗНУВШЕЕ имя молча проходит гейт. Для кириллицы
    сомнений нет: чужих библиотек с такими именами не бывает, а свои с
    11.09.2026 переведены на латиницу. Улика того же дня:
    importlib.import_module("виртуальные_датчики") пережил перевод и упал в
    прогоне ModuleNotFoundError — гейт был зелёным.
    """
    return bool(КИРИЛЛИЦА.search(модуль))


def свой(модуль: str) -> Path | None:
    """Корень импорта, которому принадлежит модуль. Чужая библиотека — None."""
    вершина = модуль.split(".")[0]
    for корень in КОРНИ_ИМПОРТА:
        база = КОРЕНЬ / корень
        if (база / вершина).is_dir() or (база / вершина).with_suffix(".py").exists():
            return база
    return None



def _имена_модуля(путь: Path) -> set[str]:
    """Имена верхнего уровня модуля: функции, классы, константы, импорты."""
    try:
        дерево = ast.parse(путь.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    имена: set[str] = set()
    for узел in дерево.body:
        if isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            имена.add(узел.name)
        elif isinstance(узел, ast.Assign):
            for цель in узел.targets:
                if isinstance(цель, ast.Name):
                    имена.add(цель.id)
                elif isinstance(цель, (ast.Tuple, ast.List)):
                    имена |= {э.id for э in цель.elts if isinstance(э, ast.Name)}
        elif isinstance(узел, ast.AnnAssign) and isinstance(узел.target, ast.Name):
            имена.add(узел.target.id)
        elif isinstance(узел, (ast.Import, ast.ImportFrom)):
            имена |= {(ч.asname or ч.name).split(".")[0] for ч in узел.names}
    return имена


def _модуль_по_имени(имя: str) -> Path | None:
    кусок = Path(*имя.split("."))
    for корень in КОРНИ_ИМПОРТА:
        путь = (КОРЕНЬ / корень / кусок).with_suffix(".py")
        if путь.exists():
            return путь
    return None


def атрибуты_модулей(путь: str, файл: Path, дерево: ast.AST):
    """Обращения «модуль.атрибут», которых в том модуле НЕТ.

    Живой случай 11.09.2026: перевод имён поправил имя модуля в месте вызова,
    а функция внутри осталась своей — «shema.shema(node_id)» при «def схема».
    Ни линтер, ни гейт импортов этого не видят: атрибут ищется в рантайме, и
    отказ пришёл ответом 500 от живого API.
    """
    свои: dict[str, Path] = {}
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Import):
            for ч in узел.names:
                м = _модуль_по_имени(ч.name)
                if м:
                    свои[(ч.asname or ч.name).split(".")[0]] = м
        elif isinstance(узел, ast.ImportFrom) and узел.level:
            база = файл.parent
            for _ in range(узел.level - 1):
                база = база.parent
            подкаталог = (узел.module or "").replace(".", "/")
            for ч in узел.names:
                м = (база / подкаталог / ч.name).with_suffix(".py")
                if м.exists():
                    свои[ч.asname or ч.name] = м
        elif isinstance(узел, ast.ImportFrom) and узел.module:
            for ч in узел.names:
                м = _модуль_по_имени(f"{узел.module}.{ч.name}")
                if м:
                    свои[ч.asname or ч.name] = м
    if not свои:
        return []
    # Имя, которому в этом файле присвоили значение или которое стало
    # параметром, модулем быть перестало: «dvizhok = sa.create_engine(...)».
    перекрыты: set[str] = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Assign):
            for цель in узел.targets:
                if isinstance(цель, ast.Name):
                    перекрыты.add(цель.id)
                elif isinstance(цель, (ast.Tuple, ast.List)):
                    перекрыты |= {э.id for э in цель.elts if isinstance(э, ast.Name)}
        elif isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef)):
            перекрыты |= {а.arg for а in узел.args.args + узел.args.kwonlyargs}
    свои = {и: п for и, п in свои.items() if и not in перекрыты}
    известные = {и: _имена_модуля(п) for и, п in свои.items()}
    беды = []
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Attribute) and isinstance(узел.value, ast.Name):
            имя = узел.value.id
            если_знаем = известные.get(имя)
            if если_знаем and узел.attr not in если_знаем:
                # Путь модуля печатаем относительно корня, когда он внутри
                # него: во временном стенде самотеста он лежит снаружи.
                где = свои[имя]
                try:
                    где = где.relative_to(КОРЕНЬ)
                except ValueError:
                    pass
                беды.append(f"{путь}:{узел.lineno}: {имя}.{узел.attr} — "
                            f"в {где} такого имени нет")
    return беды


def разбор(путь: str, файл: Path):
    """Импорты этого файла, которые никуда не ведут."""
    try:
        дерево = ast.parse(файл.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as беда:
        return [f"{путь}:{беда.lineno}: не разбирается: {беда.msg}"]
    беды = атрибуты_модулей(путь, файл, дерево)
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.ImportFrom):
            if узел.level:                      # from .x import y
                база = файл.parent
                for _ in range(узел.level - 1):
                    база = база.parent
                if узел.module and (мёртвое_имя(узел.module)
                                    or not существует(узел.module, база)):
                    беды.append(f"{путь}:{узел.lineno}: "
                                f"from {'.' * узел.level}{узел.module} import — модуля нет")
                continue
            if узел.module:
                база = свой(узел.module)
                if мёртвое_имя(узел.module) or (
                        база is not None and not существует(узел.module, база)):
                    беды.append(f"{путь}:{узел.lineno}: "
                                f"from {узел.module} import — модуля нет")
        elif isinstance(узел, ast.Call):
            # Импорт СТРОКОЙ: importlib.import_module("api.app.настройки").
            # ast-разбор его не считает импортом, а модуля может не быть —
            # 11.09 два таких вызова пережили перевод имён и упали в прогоне.
            зовут = узел.func
            имя_вызова = зовут.attr if isinstance(зовут, ast.Attribute) else (
                зовут.id if isinstance(зовут, ast.Name) else "")
            if имя_вызова == "import_module" and узел.args:
                что = узел.args[0]
                if isinstance(что, ast.Constant) and isinstance(что.value, str):
                    база = свой(что.value)
                    if мёртвое_имя(что.value) or (
                            база is not None and not существует(что.value, база)):
                        беды.append(f"{путь}:{узел.lineno}: "
                                    f"import_module(\"{что.value}\") — модуля нет")
        elif isinstance(узел, ast.Import):
            for имя in узел.names:
                база = свой(имя.name)
                if мёртвое_имя(имя.name) or (
                        база is not None and not существует(имя.name, база)):
                    беды.append(f"{путь}:{узел.lineno}: import {имя.name} — модуля нет")
    return беды


def самотест():
    """Гейт обязан СОЗДАТЬ своё условие: сломанный импорт и целый рядом."""
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as времянка:
        корень = Path(времянка)
        (корень / "pkg").mkdir()
        (корень / "pkg" / "__init__.py").write_text("")
        (корень / "pkg" / "est.py").write_text("ЗНАЧЕНИЕ = 1\n")
        целый = корень / "pkg" / "zovet.py"
        целый.write_text("from .est import ЗНАЧЕНИЕ\n")
        сломанный = корень / "pkg" / "lomanyj.py"
        сломанный.write_text("from .ischez import ЧТО\nfrom pkg.tozhe_net import X\n")

        беды = разбор("pkg/zovet.py", целый)
        if беды:
            ok = False
            print(f"  ПЛОХО целый импорт назван сломанным: {беды}")
        else:
            print("  ок    целый относительный импорт гейт не трогает")

        # Атрибут, которого в модуле нет: имя модуля поправили, а функцию
        # внутри — нет («shema.shema» при «def схема»). Ответ 500 от API.
        (корень / "pkg" / "zovet_atribut.py").write_text(
            "from . import est\nznachenie = est.НЕТ_ТАКОГО\n")
        свои = разбор("pkg/zovet_atribut.py", корень / "pkg" / "zovet_atribut.py")
        if len(свои) == 1 and "НЕТ_ТАКОГО" in свои[0]:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: обращение к несуществующему атрибуту модуля")
        else:
            ok = False
            print(f"  ПЛОХО атрибут модуля не проверен: {свои}")

        беды = разбор("pkg/lomanyj.py", сломанный)
        # Абсолютный «pkg.тоже_нет» тут чужой: корня pkg в репозитории нет,
        # и гейт судит только своё — значит ждём ровно одну находку.
        if len(беды) == 1 and "ischez" in беды[0]:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: исчезнувший модуль найден по строке")
        else:
            ok = False
            print(f"  ПЛОХО сломанный импорт не найден: {беды}")

        строкой = корень / "pkg" / "strokoj.py"
        # Имя корня берём настоящее: «свой» ищет вершину пути в репозитории,
        # и выдуманный пакет он законно сочтёт чужой библиотекой. Стояло «app»
        # — каталог продукта; 12.09.2026 продукт уехал в свой проект, вершина
        # исчезла, гейт стал считать образец чужой библиотекой, и путь молча
        # покраснел. Ворота зовут гейт БЕЗ --selftest, поэтому красное никто не
        # видел (ревью кода 12.09.2026, F2-gen-05). Берём «scripts» — каталог
        # самого харнеса, он есть на любой установке.
        строкой.write_text('import importlib\n'
                           'м = importlib.import_module("scripts.net_takogo")\n')
        беды = разбор("pkg/strokoj.py", строкой)
        if len(беды) == 1 and "import_module" in беды[0]:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: импорт СТРОКОЙ тоже судится")
        else:
            ok = False
            print(f"  ПЛОХО импорт строкой пропущен: {беды}")

        # Живой отказ 11.09.2026: модуль переведён на латиницу, а
        # import_module("виртуальные_датчики") остался. Имени нет ни в
        # репозитории, ни среди библиотек — «свой» счёл его чужим и пропустил.
        кириллицей = корень / "pkg" / "kirillicej.py"
        кириллицей.write_text('import importlib\n'
                              'м = importlib.import_module("виртуальные_датчики")\n'
                              'import настройки\n')
        беды = разбор("pkg/kirillicej.py", кириллицей)
        if len(беды) == 2 and all("модуля нет" in б for б in беды):
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: кириллическое имя модуля мертво всегда")
        else:
            ok = False
            print(f"  ПЛОХО кириллический импорт пропущен: {беды}")

    print("САМОТЕСТ %s: 5 путей, четыре — больные случаи"
          % ("ПРОЙДЕН" if ok else "ПРОВАЛЕН"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return самотест()
    беды = []
    сколько = 0
    for путь, файл in файлы():
        сколько += 1
        беды += разбор(путь, файл)
    if беды:
        print(f"[импорты] СЛОМАНЫ: {len(беды)} из {сколько} файлов python")
        for строка in беды[:40]:
            print("   ", строка)
        if len(беды) > 40:
            print(f"    … и ещё {len(беды) - 40}")
        return 1
    print(f"[импорты] все внутренние импорты разрешаются (файлов: {сколько})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
