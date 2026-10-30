#!/usr/bin/env python3
"""Пробы гейта «остатки в пакете»: заглушки памяти, мёртвые файлы, ссылки в пустоту.

Откуда. Разбор правок соседней установки (18.10.2026, пункты 3.11 и 2.4): в пакете
лежали 14 записей памяти-заглушек («полная запись с уликами в этот харнес не
приехала») вместо полных записей проекта и мёртвый `harness/config/vorota.sh`
(26 КБ, после переименования 21.09 его не зовёт никто — упоминание осталось
только в комментарии). Обе беды — остаток догоняния, и ни один гейт их не видел.

Почему запись пакета обязана иметь тёзку в памяти проекта. Прибор набора
`scripts/memory-to-package.py` переносит запись ПОД ТЕМ ЖЕ ИМЕНЕМ. Запись
пакета под коротким именем (`attached-not-forever` при проектной
`an-attached-client-is-not-forever`) прибор обновить не может никогда, панель
не видит её «в наборе», а ссылки полных записей ведут на проектные имена.

Гейт (его пишет шаг «код»): `scripts/check-package-leftovers.py`
    --package <корень пакета> --project <корень проекта>
    rc 0 — чисто; rc 1 — находки, по строке на находку: путь в пакете и слово
    причины («заглушка», «мёртвый», «ссылка в пустоту», «нет в памяти проекта»).
Дверь пробы: PACKAGE_LEFTOVERS_GATE — путь копии гейта (мутации).

Прогон: python3 scripts/test_package_leftovers.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

# Проба живёт в двух раскладках (пакет: harness/scripts/, проект: scripts/):
# гейт — сосед этого файла, проект — из паспорта установки.
GATE = Path(os.environ.get("PACKAGE_LEFTOVERS_GATE") or Path(__file__).resolve().parent / "check-package-leftovers.py")
PACKAGE = config.katalog_paketa()
PROJECT = Path(config.из_файла(config.путь_install_conf()).get("PROJECT_DIR") or config.корень_дерева(__file__))
STUB_MARK = "Полная запись с уликами в этот харнес не приехала"

paths = 0
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name}" + (f": {detail}" if detail else ""))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def record(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: проба\nmetadata:\n  type: feedback\n---\n\n{body}\n"


def healthy_stand(base: Path) -> tuple[Path, Path]:
    """Пакет и проект, где всё сходится: от него каждый больной случай
    отличается ОДНИМ изменением."""
    pkg, prj = base / "pkg", base / "prj"
    full = record("full-record", "Случай с уликами. Связано: [[other-record]].")
    other = record("other-record", "Вторая запись.")
    write(pkg / "harness/память/full-record.md", full)
    write(pkg / "harness/память/other-record.md", other)
    write(pkg / "harness/память/MEMORY.md", "| когда | что — [[full-record]] |\n")
    write(prj / "память/full-record.md", full)
    write(prj / "память/other-record.md", other)
    # Файл есть только в пакете, но код зовёт его по имени — живой (как
    # logrotate-harness, который кладёт в /etc установщик).
    write(pkg / "harness/config/rotate-conf", "weekly\n")
    write(pkg / "harness/scripts/layout.sh",
          "#!/usr/bin/env bash\ncp \"$HERE/config/rotate-conf\" /etc/x\n")
    write(prj / "scripts/layout.sh",
          "#!/usr/bin/env bash\ncp \"$HERE/config/rotate-conf\" /etc/x\n")
    return pkg, prj


def run_gate(pkg: Path, prj: Path) -> tuple[int, str]:
    done = subprocess.run([sys.executable, str(GATE), "--package", str(pkg), "--project", str(prj)],
                          capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


def finding(out: str, path_part: str, word: str) -> bool:
    """Находка — ОДНА строка, где названы и файл, и причина: слово причины
    где-то в выводе и путь где-то ещё не доказывают, что судили этот файл."""
    return any(path_part in line and word in line for line in out.splitlines())


def stand_cases() -> None:
    if not GATE.is_file():
        check("гейт остатков существует", False, f"механизма нет: {GATE} (его пишет шаг «код»)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        rc, out = run_gate(pkg, prj)
        check("здоровый стенд — rc 0", rc == 0, f"rc {rc}: {out.strip()[:300]}")
        check("здоровый: файл пакета, который код зовёт по имени, не мёртвый",
              "rotate-conf" not in out, out.strip()[:300])
        # Пустая строка на зелёном неотличима от «судить было нечем»: в прогоне
        # ворот она стояла без приговора. Числа — из стенда: три записи памяти,
        # один файл конфига.
        check("здоровый: приговор называет число судимого",
              out.strip() == "чисто: записей памяти 3, файлов конфига 1", repr(out.strip()[:300]))

    # БОЛЬНОЙ 3.11: заглушка вместо полной записи (живой текст заглушек 14.10).
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/память/full-record.md",
              record("full-record", f"**Why:** запись восстановлена из указателя. {STUB_MARK}: "
                     "пакет доставил ссылку, но не файл."))
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ: запись-заглушка — rc 1", rc == 1, f"rc {rc}")
        check("БОЛЬНОЙ: заглушка названа файлом и словом «заглушка»",
              finding(out, "full-record.md", "заглушка"), out.strip()[:300])

    # БОЛЬНОЙ 2.4: файл только в пакете, упомянут лишь в комментарии кода.
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/config/old-gates.sh", "#!/usr/bin/env bash\necho old\n")
        write(pkg / "harness/scripts/names.py",
              "# миграция переименовала «scripts/old-gates.sh» прямо внутри пробы\nprint(1)\n")
        write(prj / "scripts/names.py",
              "# миграция переименовала «scripts/old-gates.sh» прямо внутри пробы\nprint(1)\n")
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ: мёртвый файл пакета (имя только в комментарии) — rc 1", rc == 1, f"rc {rc}")
        check("БОЛЬНОЙ: мёртвый файл назван путём и словом «мёртвый»",
              finding(out, "harness/config/old-gates.sh", "мёртвый"), out.strip()[:300])

    # Тот же файл, но упомянут только в документе: документ ничего не зовёт.
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/config/old-gates.sh", "#!/usr/bin/env bash\necho old\n")
        write(pkg / "UNIFIED/README.md", "| `config/old-gates.sh` | ворота |\n")
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ: имя мёртвого файла только в документе — всё равно мёртвый",
              rc == 1 and finding(out, "harness/config/old-gates.sh", "мёртвый"), f"rc {rc}: {out.strip()[:300]}")

    # Файл конфига называет сам себя (шапка-пример, include самого себя):
    # своя строка не зов, иначе любой самоописанный груз живёт вечно.
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/config/self-named.conf", "include self-named.conf\nweekly\n")
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ: файл, который называет только сам себя, — мёртвый",
              rc == 1 and finding(out, "harness/config/self-named.conf", "мёртвый"), f"rc {rc}: {out.strip()[:300]}")

    # БОЛЬНОЙ: указатель пакета ведёт на запись, которой в пакете нет.
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/память/MEMORY.md",
              "| когда | что — [[full-record]] |\n| ещё | — [[attached-not-forever]] |\n")
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ: ссылка указателя в пустоту — rc 1", rc == 1, f"rc {rc}")
        check("БОЛЬНОЙ: названо имя пропавшей записи и «ссылка в пустоту»",
              finding(out, "attached-not-forever", "ссылка в пустоту"), out.strip()[:300])

    # БОЛЬНОЙ: запись пакета под именем, которого нет в памяти проекта, —
    # прибор набора её не обновит, панель не покажет «в наборе».
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/память/short-name.md", record("short-name", "Полный текст под другим именем."))
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ: запись пакета без тёзки в проекте — rc 1", rc == 1, f"rc {rc}")
        check("БОЛЬНОЙ: названа запись и «нет в памяти проекта»",
              finding(out, "short-name.md", "нет в памяти проекта"), out.strip()[:300])

    # Заготовки пакета (MEMORY/ORDERS/TONE) — не записи: тёзка в проекте у них
    # есть, но содержимое ОБЯЗАНО отличаться, и это не находка.
    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/память/ORDERS.md", "# ORDERS — заготовка\n")
        write(pkg / "harness/память/TONE.md", "# ТОН — заготовка\n")
        rc, out = run_gate(pkg, prj)
        check("здоровый: заготовки ORDERS/TONE без тёзки в проекте не находка", rc == 0, f"rc {rc}: {out.strip()[:300]}")


def index_and_missing_package_cases() -> None:
    """VG-07: указатель MEMORY.md пакета ссылается и формой markdown
    `[…](имя.md)`, и формой `имя.md` в обратных кавычках — гейт судил только
    `[[…]]`, и 16 ссылок указателя в пустоту прошли зелёными.
    VG-09: пакета на ожидаемом пути нет — отказ словами, а не трассировка."""
    if not GATE.is_file():
        return

    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/память/MEMORY.md",
              "| когда | что — [[full-record]] |\n\n## Указатель\n\n"
              "- [Полная запись](full-record.md) — живая\n"
              "- [Короткое имя](attached-not-forever.md) — файла нет\n")
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ VG-07: markdown-ссылка указателя (имя.md) в пустоту — rc 1", rc == 1, f"rc {rc}: {out.strip()[:300]}")
        check("БОЛЬНОЙ VG-07: названо имя attached-not-forever и «ссылка в пустоту»",
              finding(out, "attached-not-forever", "ссылка в пустоту"), out.strip()[:300])
        check("VG-07, сосед: живая markdown-ссылка (full-record.md) не находка",
              not finding(out, "full-record", "ссылка в пустоту"), out.strip()[:300])

    with tempfile.TemporaryDirectory() as tmp:
        pkg, prj = healthy_stand(Path(tmp))
        write(pkg / "harness/память/TONE.md", "# ТОН — заготовка\n")
        write(pkg / "harness/память/MEMORY.md",
              "| когда | что — [[full-record]] |\n\n## Указатель\n\n"
              "- `exit-closes-pane.md` — файла нет\n"
              "Как говорить с владельцем, живёт в `память/TONE.md`.\n")
        rc, out = run_gate(pkg, prj)
        check("БОЛЬНОЙ VG-07: имя в кавычках (`exit-closes-pane.md`) в пустоту — rc 1", rc == 1, f"rc {rc}: {out.strip()[:300]}")
        check("БОЛЬНОЙ VG-07: названо имя exit-closes-pane и «ссылка в пустоту»",
              finding(out, "exit-closes-pane", "ссылка в пустоту"), out.strip()[:300])
        check("VG-07, сосед: `память/TONE.md` (заготовка есть в пакете) не находка",
              "TONE" not in out, out.strip()[:300])

    with tempfile.TemporaryDirectory() as tmp:
        absent = Path(tmp) / "нет-пакета"
        prj = Path(tmp) / "prj"
        write(prj / "память/full-record.md", record("full-record", "x"))
        rc, out = run_gate(absent, prj)
        check("БОЛЬНОЙ VG-09: пакета нет — отказ без трассировки, путь назван, rc не 0",
              rc != 0 and "Traceback" not in out and str(absent) in out,
              f"rc {rc}: {out.strip()[-300:]}")

    for missing in ("harness/config", "harness/память"):
        with tempfile.TemporaryDirectory() as tmp:
            pkg, prj = healthy_stand(Path(tmp))
            shutil.rmtree(pkg / missing)
            rc, out = run_gate(pkg, prj)
            check(f"БОЛЬНОЙ VG-10: в пакете нет {missing} — rc 2, «пакет не прочитан», "
                  "каталог назван, без трассировки",
                  rc == 2 and "пакет не прочитан" in out and missing in out
                  and "Traceback" not in out, f"rc {rc}: {out.strip()[-300:]}")


def live_cases() -> None:
    memory = PACKAGE / "harness" / "память"
    if not memory.is_dir():
        check("каталог памяти пакета на месте", False, f"нет {memory}: судить нечем")
        return
    stubs = sorted(p.name for p in memory.glob("*.md") if STUB_MARK in p.read_text(encoding="utf-8"))
    check("ЖИВОЙ 3.11: записей-заглушек в пакете 0", not stubs, f"{len(stubs)}: {' '.join(stubs)}")
    dead = PACKAGE / "harness" / "config" / "vorota.sh"
    check("ЖИВОЙ 2.4: мёртвого harness/config/vorota.sh в пакете нет", not dead.exists(),
          f"лежит, {dead.stat().st_size if dead.exists() else 0} байт")
    names = {p.stem for p in memory.glob("*.md")}
    index = (memory / "MEMORY.md").read_text(encoding="utf-8") if (memory / "MEMORY.md").is_file() else ""
    links = set(re.findall(r"\[\[([^\]|#]+)", index))
    check("ЖИВОЙ: в указателе пакета есть ссылки (иначе следующий случай пуст)", bool(links))
    missing = sorted(links - names)
    check("ЖИВОЙ: каждая ссылка указателя пакета ведёт на запись пакета", not missing, " ".join(missing))
    md_links = set(re.findall(r"\]\(([^)/]+)\.md\)", index))
    check("ЖИВОЙ VG-07: в указателе пакета есть markdown-ссылки (иначе следующий случай пуст)", bool(md_links))
    md_missing = sorted(md_links - names)
    check("ЖИВОЙ VG-07: каждая markdown-ссылка указателя пакета ведёт на запись пакета",
          not md_missing, f"{len(md_missing)}: {' '.join(md_missing)}")
    if GATE.is_file():
        rc, out = run_gate(PACKAGE, PROJECT)
        check("ЖИВОЙ: гейт на настоящих пакете и проекте — rc 0", rc == 0, f"rc {rc}:\n{out.strip()[:1500]}")


def main() -> int:
    print("[стенды]")
    stand_cases()
    index_and_missing_package_cases()
    print("[живой пакет]")
    live_cases()
    if failures:
        print(f"SELFTEST: КРАСНЫЙ ({failures} из {paths} путей)")
        return 1
    print(f"SELFTEST: зелёный ({paths} путей; первый больной — 14 заглушек памяти пакета, 18.10)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
