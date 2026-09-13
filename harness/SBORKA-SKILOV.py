#!/usr/bin/env python3
"""СБОРКА-СКИЛОВ: генерация skills/<имя>/SKILL.md из глав UNIFIED.

Два источника истины запрещены: тело скила — секции главы ДОСЛОВНО
(единственное исключение — таблица ЗАМЕНЫ: пути донорских глав приводятся
к путям этого пакета). Править скилы руками нельзя — правка вносится в главу,
затем перегенерация:

    python3 harness/SBORKA-SKILOV.py [путь-к-каталогу-глав]

Путь к главам: аргумент → $HARNESS_CHAPTERS_DIR → рядом с пакетом
(../../UNIFIED или ../UNIFIED). Генератор падает (код 1) ДО любой записи,
если глава или секция не нашлась, скил вышел пустым или description не
содержит условия срабатывания («когда…»): сначала все скилы собираются и
проверяются в памяти, затем пишутся в skills.tmp и атомарно подменяют skills/.
"""
import os
import re
import shutil
import sys
from pathlib import Path

# ── Декларативная таблица скилов ─────────────────────────────────────────────
# имя → (description с наблюдаемым условием срабатывания,
#        [(файл главы, [дословные заголовки '## ...' без решётки]), ...])
SKILLS = {
    "приём-задачи": (
        "Когда в канал пришла новая задача или сообщение владельца с работой — "
        "как принять её, понять и не начать с кода",
        [("01-ПРИНЦИПЫ-И-ПРАВИЛА.md", ["Как думать"]),
         ("05-ПАЙПЛАЙН.md", ["Глубина по размеру задачи"])],
    ),
    "пишу-спеку": (
        "Когда задача принята и по глубине (T2/T3) требует спеку — как писать "
        "спеку до кода: состояния, форматы, проверенные факты",
        [("05-ПАЙПЛАЙН.md", ["1. Спека"])],
    ),
    "ревью-спеки": (
        "Когда спека написана и до согласования нужно найти её дыры — "
        "adversarial-ревью чужими глазами",
        [("05-ПАЙПЛАЙН.md", ["2. Adversarial-ревью спеки"])],
    ),
    "пишу-план": (
        "Когда спека согласована и пора превращать её в план реализации — "
        "включая прогон каждой команды плана ДО записи в план",
        [("05-ПАЙПЛАЙН.md", ["3. План реализации"]),
         ("11-ЛОВУШКИ.md", ["Процессные: порядок действий создаёт беду"])],
    ),
    "pretask-контракт": (
        "Когда план готов и перед реализацией нужен контракт задачи "
        "(pretask.yaml) отдельным аналитиком — критерии командами, риски, край",
        [("05-ПАЙПЛАЙН.md", ["4. Контракт на задачу"]),
         ("06-ГЕЙТЫ-КАЧЕСТВА.md", ["`pretask.yaml` — контракт задачи"])],
    ),
    "тдд-тесты-до-кода": (
        "Когда контракт есть и пора писать тесты — до кода и другими глазами, "
        "с обязательным красным прогоном",
        [("05-ПАЙПЛАЙН.md", ["6. Тесты — до кода и другими глазами"])],
    ),
    "ревью-кода": (
        "Когда код написан и тесты зелёные — ревью по ролям перед коммитом: "
        "планка кода, хирургичность, честность тестов",
        [("05-ПАЙПЛАЙН.md", ["9. Ревью кода"]),
         ("01-ПРИНЦИПЫ-И-ПРАВИЛА.md", ["Планка кода: Торвальдс + SOLID"]),
         ("06-ГЕЙТЫ-КАЧЕСТВА.md", ["Ревью по ролям"])],
    ),
    "выкат-и-смоук": (
        "Когда работа закоммичена и пора выкатывать — один скрипт с воротами, "
        "откат наготове и живой смоук на проде после",
        [("09-ВЫКАТ.md", ["Правило 1. Выкат — одним скриптом с жёстким порядком",
                          "Правило 2. Ворота перед выкатом",
                          "Правило 6. Откат должен существовать всегда"]),
         ("08-ПРИБОРЫ-И-ЗАМЕРЫ.md", ["Тип 7. Живой смоук — глаз на проде после выката"])],
    ),
    "закрытие-задачи": (
        "Когда смоук пройден и задача завершается — закрытие: честный отчёт "
        "«чем подтверждено», статус test, память, передача, следующий шаг",
        [("05-ПАЙПЛАЙН.md", ["13. Закрытие"]),
         ("02-РАБОТА-С-ВЛАДЕЛЬЦЕМ.md", ["Честность отчёта"]),
         ("04-ПЕРЕДАЧА-КОНТЕКСТА.md", ["Когда обновлять"])],
    ),
    "разбор-аварии": (
        "Когда прод сломался или владелец видит дефект — разбор: замер до "
        "починки, настоящий источник, системное закрытие в таблицу проблем",
        [("01-ПРИНЦИПЫ-И-ПРАВИЛА.md", ["Как мерить"]),
         ("10-СЕРВЕР-И-НАДЁЖНОСТЬ.md", ["Известные проблемы — как таблица, а не как память",
                                        "N. <Симптом одной строкой>"])],
    ),
    "агенты-параллельно": (
        "Когда задач две и больше и они независимы (разные подсистемы, разные "
        "файлы тестов, разные причины отказа) — как запускать агентов "
        "одновременно, что каждому дать и как принимать их работу",
        [("PACKAGE:skills-src/агенты-параллельно.md", None)],
    ),
    "работа-субагентами": (
        "Когда план написан и его задачи делаются не своей сменой, а агентами "
        "— роли, выбор модели под роль, круги правок с предохранителем, журнал "
        "решений; наши запреты: одна ветка, выкат только deploy.sh, прогон фоном",
        [("PACKAGE:skills-src/работа-субагентами.md", None)],
    ),
    "установка-расширений": (
        "Когда наступило условие из данных harness/config/расширения.yaml "
        "(первый фронт, первый запрос документа, первая внешняя библиотека) или "
        "в задаче появилось мобильное/клиентское приложение — ставит код "
        "(scripts/rasshirenija.py, демон rasshirenija-watch), не руки",
        [("PACKAGE:skills-src/установка-расширений.md", None)],
    ),
    "падение-тестов": (
        "Когда упал тест, гейт или прогон — разбор падения: соло-прогон флейка "
        "без права обхода хука, красный тест при верной реализации = дефект "
        "теста, своя оболочка прячет дефект",
        [("06-ГЕЙТЫ-КАЧЕСТВА.md", ["Pre-commit хук"]),
         ("11-ЛОВУШКИ.md", ["Инструментальные: прибор или проверка врут"])],
    ),
    "постройка-прибора": (
        "Когда нужен новый замер/проверка/дашборд — правила постройки прибора: "
        "доказать на больном случае, не верить прибору на слово",
        [("08-ПРИБОРЫ-И-ЗАМЕРЫ.md", ["Правила постройки любого прибора",
                                     "Прибор доволен — а глаз нет"])],
    ),
    "добыча-улик": (
        "Когда правило или потолок предлагается без пережитого случая — сначала "
        "добыть улики из журналов и истории и превратить их в правила",
        [("14-СБОРКА-НА-НОВОМ-ПРОЕКТЕ.md", ["Этап 1. Добыть улики",
                                            "Этап 2. Превратить улики в правила"])],
    ),
}

# ── Замены путей: пути донорских глав → пути этого пакета ────────────────────
# Главы писались в донорских проектах и ссылаются на их раскладку
# (templates/, .tasks/_config). Применяется к каждому извлечённому телу
# ПОСЛЕ извлечения. Порядок важен: точные ключи раньше общих; общий
# 'templates/' — последним, чтобы не перебить точные.
ЗАМЕНЫ = {
    # опасная команда (симлинк из .git/hooks: ../../ = корень репозитория)
    "ln -sf ../../.tasks/_config/pre-commit-hook.sh":
        "ln -sf ../../scripts/pre-commit-hook.sh",
    "templates/spec-template.md": "harness/шаблоны-задач/spec-template.md",
    "templates/plan-template.md": "harness/шаблоны-задач/plan-template.md",
    "templates/pretask-template.yaml": "harness/шаблоны-задач/pretask-template.yaml",
    "templates/pre-commit-hook.sh": "scripts/pre-commit-hook.sh",
    "templates/deploy.sh": "scripts/deploy.sh",
    "templates/deploy_guard.py": "scripts/deploy_guard.py",
    "templates/check-secrets.sh": "scripts/check-secrets.sh",
    "templates/hooks/": "scripts/hooks/",
    "templates/": "harness/шаблоны-задач/",
}



def find_chapters_dir() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [Path(sys.argv[1])] if len(sys.argv) > 1 else []
    env = os.environ.get("HARNESS_CHAPTERS_DIR")
    if env:
        candidates.append(Path(env))
    # Вторая раскладка — публичная сборка: главы уехали в служебный подкаталог,
    # чтобы человек не видел исходники правил первым экраном (12.09.2026).
    candidates += [here.parent.parent / "UNIFIED", here.parent / "UNIFIED",
                   here.parent.parent / "sluzhebnoe" / "UNIFIED",
                   here.parent / "sluzhebnoe" / "UNIFIED"]
    for c in candidates:
        if c.is_dir() and list(c.glob("0*-*.md")):
            return c
    sys.exit("СБОРКА-СКИЛОВ: каталог глав не найден — передай путь аргументом")


def extract_section(text: str, title: str, chapter: str) -> str:
    """Секция от '## <title>' до следующего '## ' — дословно, с заголовком."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == f"## {title}"), None)
    if start is None:
        sys.exit(f"СБОРКА-СКИЛОВ: в {chapter} нет секции '## {title}'")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return "".join(lines[start:end]).rstrip("\n") + "\n"


def apply_замены(body: str) -> str:
    for донорский, местный in ЗАМЕНЫ.items():
        body = body.replace(донорский, местный)
    return body


def шапка(name: str, description: str, chapter_ids: list[str]) -> str:
    # Записи в журнал скилов руками здесь НЕТ и быть не должно: вызов пишет хук
    # PostToolUse:Skill (scripts/hooks/skill_log.sh) с признаком «источник»:
    # «хук». Пока команда стояла в шапке, журнал вёлся дважды — живой дубль
    # 11.09 в 01:50:43 (хук) и 01:50:49 (рука) про один вызов, — а гейты всё
    # равно не вправе считать самоотчёт.
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"> Сгенерировано из глав(ы) {', '.join(chapter_ids)} скриптом SBORKA-SKILOV.py —\n"
        "> НЕ ПРАВИТЬ РУКАМИ: правка вносится в главу, затем перегенерация.\n"
        "> Рабочие каталоги задач: .tasks/<id>/ в корне проекта (создаются на задачу).\n\n"
    )


def скилы_возможностей() -> dict:
    """Скилы УСТАНОВЛЕННЫХ возможностей (harness/vozmozhnosti/<id>/skills-src/*.md).

    Ключевое слово — установленных: пока в каталоге возможности нет метки
    `.установлено`, её скилы не собираются вовсе. Так «поставить, когда
    пригодится» становится механизмом: до наступления условия ни один скил
    возможности не занимает ни строки контекста, а после установки они
    появляются сами, той же командой, что поставила возможность.

    Имя и условие срабатывания лежат в самом источнике метками-комментариями:
        <!-- имя: android-env -->
        <!-- описание: Когда … -->
    Источник без любой из двух меток валит сборку — скил без условия
    срабатывания молчит ровно тогда, когда нужен.
    """
    root = Path(__file__).resolve().parent / "vozmozhnosti"
    собранные = {}
    if not root.is_dir():
        return собранные
    for pack in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (pack / ".установлено").is_file():
            continue
        for src in sorted((pack / "skills-src").glob("*.md")):
            body = src.read_text(encoding="utf-8")
            m_name = re.search(r"<!--\s*имя:\s*(.+?)\s*-->", body)
            m_desc = re.search(r"<!--\s*описание:\s*(.+?)\s*-->", body, re.S)
            if not m_name or not m_desc:
                sys.exit(f"СБОРКА-СКИЛОВ: в {src} нет метки «имя:» или «описание:» — "
                         "скил без условия срабатывания не собирается")
            name = m_name.group(1)
            if name in SKILLS or name in собранные:
                sys.exit(f"СБОРКА-СКИЛОВ: имя скила «{name}» из {src} уже занято")
            description = " ".join(m_desc.group(1).split())
            собранные[name] = (
                шапка(name, description, [f"возможность {pack.name}"])
                + f"<!-- дословно из возможности {pack.name}, файл {src.name} -->\n"
                + apply_замены(body.rstrip("\n")) + "\n"
            )
    return собранные


def render(chapters: Path) -> dict:
    """Собрать ВСЕ скилы в память (dry-извлечение) — ни одной записи на диск.

    Любая отсутствующая глава/секция валит сборку здесь, до записи."""
    texts = {}
    rendered = {}
    for name, (description, sources) in SKILLS.items():
        chapter_ids = sorted({("пакет" if src[0].startswith("PACKAGE:") else src[0].split("-", 1)[0])
                              for src in sources})
        body_parts = []
        for chapter_file, titles in sources:
            if chapter_file.startswith("PACKAGE:"):
                rel = chapter_file.split(":", 1)[1]
                local = Path(__file__).resolve().parent / rel
                if not local.is_file():
                    sys.exit(f"СБОРКА-СКИЛОВ: нет файла пакета {rel}")
                body_parts.append(apply_замены(
                    f"<!-- дословно из {rel} (файл пакета) -->\n"
                    + local.read_text(encoding="utf-8").rstrip("\n") + "\n"))
                continue
            if chapter_file not in texts:
                chapter_path = chapters / chapter_file
                if not chapter_path.is_file():
                    sys.exit(f"СБОРКА-СКИЛОВ: нет главы {chapter_path}")
                texts[chapter_file] = chapter_path.read_text(encoding="utf-8")
            for title in titles:
                body_parts.append(apply_замены(
                    f"<!-- дословно из {chapter_file} -->\n"
                    + extract_section(texts[chapter_file], title, chapter_file)))
        rendered[name] = шапка(name, description, chapter_ids) + "\n".join(body_parts)
    rendered.update(скилы_возможностей())
    return rendered


def verify(rendered: dict) -> None:
    """Каждый скил непуст (тело длиннее шапки) и срабатывает по «когда…».

    Проверка идёт по памяти ДО записи на диск."""
    bad = []
    for name, text in rendered.items():
        m = re.search(r"^description: (.+)$", text, re.MULTILINE)
        if len(text) < 1200:
            bad.append(f"{name}: тело подозрительно короткое ({len(text)} зн.)")
        if not m or not re.search(r"(^|[ (])(К|к)огда\b", m.group(1)):
            bad.append(f"{name}: description без условия срабатывания («когда…»)")
    if bad:
        sys.exit("СБОРКА-СКИЛОВ: проверка не пройдена:\n  " + "\n  ".join(bad))


def write_atomic(rendered: dict, out_root: Path) -> None:
    """Записать всё в skills.tmp и атомарно подменить skills/ (os.replace)."""
    tmp_root = out_root.parent / (out_root.name + ".tmp")
    old_root = out_root.parent / (out_root.name + ".old")
    for stale in (tmp_root, old_root):
        if stale.exists():
            shutil.rmtree(stale)
    for name, text in rendered.items():
        skill_dir = tmp_root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    if out_root.exists():
        os.replace(out_root, old_root)
    os.replace(tmp_root, out_root)
    if old_root.exists():
        shutil.rmtree(old_root)


def svyazat_s_obolochkoj(out_root: Path) -> tuple:
    """Скилы собраны в harness/skills — но ОБОЛОЧКА смотрит только в
    <проект>/.claude/skills. Связь держали симлинки, которых не создавал никто:
    их поставили руками в первый день, и любой НОВЫЙ скил до оболочки не
    доезжал, а на чистой установке скилов не было вовсе. Улика 12.09.2026: два
    новых скила собрались, а `ls .claude/skills` их не показал — это и есть
    «установка скилов ни разу не сработала» словами владельца.

    Свои каталоги продукта (не симлинки) не трогаются: они живут рядом по
    решению из CLAUDE.md. Битые ссылки на исчезнувшие скилы убираются.
    """
    проект = out_root.parent.parent
    каталог = проект / ".claude" / "skills"
    if not (проект / ".claude").is_dir():
        return (0, 0)   # пакет: оболочки рядом нет, связывать нечего
    каталог.mkdir(parents=True, exist_ok=True)
    создано = убрано = 0
    for скил in sorted(out_root.iterdir()):
        if not скил.is_dir():
            continue
        ссылка = каталог / скил.name
        цель = Path("../..") / out_root.parent.name / out_root.name / скил.name
        if ссылка.is_symlink() and os.readlink(ссылка) == str(цель):
            continue
        if ссылка.is_symlink() or not ссылка.exists():
            ссылка.unlink(missing_ok=True)
            ссылка.symlink_to(цель)
            создано += 1
    for ссылка in sorted(каталог.iterdir()):
        if ссылка.is_symlink() and not ссылка.resolve().exists():
            ссылка.unlink()
            убрано += 1
    return (создано, убрано)


def sverit(rendered: dict, out_root: Path) -> list:
    """Чем собранное в памяти расходится с тем, что лежит на диске.

    Скил — копия куска главы, и генератор зовут КОМАНДОЙ: поправил главу,
    забыл перегенерировать — агент работает по устаревшему правилу и уверен,
    что прав. Механизм, который держится на чьей-то памяти, — не механизм
    (владелец 11.09.2026: «Внимание модели — последний носитель правила, а не
    первый»). Отсюда сверка: собрать в памяти и сравнить с диском.
    """
    расхождения = []
    for имя, текст in sorted(rendered.items()):
        файл = out_root / имя / "SKILL.md"
        if not файл.exists():
            расхождения.append(f"{имя}: собран из главы, но на диске его нет")
        elif файл.read_text(encoding="utf-8") != текст:
            расхождения.append(f"{имя}: на диске не то, что собирается из главы")
    лишние = sorted(п.name for п in out_root.iterdir()
                    if п.is_dir() and п.name not in rendered) if out_root.is_dir() else []
    расхождения += [f"{имя}: лежит в скилах, но ни из какой главы не собирается"
                    for имя in лишние]
    return расхождения


def main() -> None:
    # «--где-главы» печатает найденный каталог и выходит. Нужен не для удобства:
    # после переноса глав в служебный подкаталог публичной сборки (12.09.2026)
    # проба обязана звать ТУ ЖЕ функцию поиска, что и боевой путь, иначе она
    # проверяет свою копию логики.
    if "--где-главы" in sys.argv[1:]:
        print(find_chapters_dir())
        return
    chapters = find_chapters_dir()
    out_root = Path(__file__).resolve().parent / "skills"
    rendered = render(chapters)     # dry: всё в памяти
    verify(rendered)                # отказ — до любой записи
    # «--сверить» ничего не пишет: это гейт для ворот и pre-commit.
    if "--сверить" in sys.argv[1:]:
        расхождения = sverit(rendered, out_root)
        if расхождения:
            print(f"СКИЛЫ РАЗОШЛИСЬ С ГЛАВАМИ: {len(расхождения)}")
            for строка in расхождения:
                print("   ", строка)
            print("Починка: python3 harness/SBORKA-SKILOV.py")
            sys.exit(1)
        print(f"скилы: все {len(rendered)} совпадают с главами")
        return
    write_atomic(rendered, out_root)
    создано, убрано = svyazat_s_obolochkoj(out_root)
    print(f"СБОРКА-СКИЛОВ: собрано {len(rendered)} скилов из {chapters} → {out_root}; "
          "проверка пройдена")
    if создано or убрано:
        print(f"СБОРКА-СКИЛОВ: оболочка (.claude/skills): связано {создано}, "
              f"убрано битых {убрано}")


if __name__ == "__main__":
    main()
