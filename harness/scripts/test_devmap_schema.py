#!/usr/bin/env python3
"""Проба: валидатор карты судит карточки по схеме-данным (шаг 1 задачи «схема карты»).

Слово владельца 07.11: «карту задач надо классифицировать, описать свойства,
результаты, состояния, процессы». Схема — harness/config/devmap-schema.yaml;
судит её тот же валидатор карты, что и раньше (pre-commit-gate.sh, режим
--devmap-only), второго судьи нет.

Стенд свой: маленькая схема и маленькие карты во временном каталоге. Схему
валидатор находит так же, как на проекте, — harness/config/devmap-schema.yaml
от каталога карты вверх; двери среды для схемы нет, чтобы коммит не мог
подсунуть гейту свою мягкую схему. Живая карта и живая схема не читаются.
Каждый случай — своя карта с ОДНОЙ поломкой, иначе красный от соседней
поломки выдал бы себя за пойманную.

Двери:
    DEVMAP_GATE    путь к pre-commit-gate.sh (умолчание — рядом с пробой)
Ключ DEVMAP_SCHEMA_STRICT проба ставит сама в каждом случае (1, а в случаях
«STRICT=0» — 0): машинный harness.conf на исход не влияет.
Прогон (только фоном):
    python3 scripts/test_devmap_schema.py
Последняя строка: `СХЕМА КАРТЫ: случаев N, неудач F`.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(os.environ.get("DEVMAP_GATE") or Path(__file__).resolve().parent / "pre-commit-gate.sh")

SCHEMA = """\
версия: 1
статусы:
  plan: { где: [карта, архив] }
  test: { где: [карта, архив] }
  снято: { где: [архив] }
поля:
  id: { тип: строка }
  status: { тип: строка }
  title: { тип: строка }
  summary: { тип: строка }
  owner_testable: { тип: логическое }
  как_проверить: { тип: строка }
  blocked: { тип: [строка, пусто] }
  проверка:
    тип: словарь
    тип_в_архиве: [строка]
    поля:
      команда: { тип: строка }
      ждём: { тип: [строка, список] }
  checklist:
    тип: список
    элемент:
      тип: словарь
      поля:
        label: { тип: строка }
        done: { тип: логическое }
устаревшие:
  ревизия_старая: { карточек: { карта: 1, архив: 3 } }
"""

HEAD = """\
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
"""

ARCHIVE_HEAD = """\
version: 1
lines:
  - id: core
    name: "Ядро"
    tasks:
"""


def card(tid, status="plan", extra=""):
    text = (f"      - id: {tid}\n        status: {status}\n        owner_testable: false\n"
            f"        title: \"Т\"\n        summary: \"С\"\n")
    return text + "".join(f"        {line}\n" for line in extra.splitlines() if line)


FAILS = 0
CASES = 0


def run(stand, name, body, archive=False, strict="1"):
    path = stand / f"{name}.yaml"
    path.write_text((ARCHIVE_HEAD if archive else HEAD) + body, encoding="utf-8")
    cmd = ["bash", str(GATE), "--devmap-only", str(path)] + (["--архив"] if archive else [])
    env = dict(os.environ, DEVMAP_SCHEMA_STRICT=strict)
    done = subprocess.run(cmd, capture_output=True, text=True, cwd=stand, env=env, timeout=120)
    return done.returncode, done.stdout + done.stderr


def expect(stand, label, body, red, words=(), archive=False, strict="1"):
    global FAILS, CASES
    CASES += 1
    code, out = run(stand, f"c{CASES}", body, archive, strict)
    ok = (code != 0) == red and all(w in out for w in words)
    if not ok:
        FAILS += 1
    verdict = "красный" if code else "зелёный"
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {label}: {verdict}" + ("" if ok else f"\n{out}"))


def main():
    with tempfile.TemporaryDirectory(prefix="o-devmap-schema-") as tmp:
        stand = Path(tmp)
        (stand / "harness" / "config").mkdir(parents=True)
        (stand / "harness" / "config" / "devmap-schema.yaml").write_text(SCHEMA, encoding="utf-8")

        expect(stand, "здоровая карта по схеме", card("t.ok", extra='как_проверить: "x"\nblocked: null')
               + card("t.test", "test", 'проверка: { команда: "true", ждём: [a] }'), red=False)
        expect(stand, "поле вне схемы — красный с карточкой и полем",
               card("t.лишнее", extra="выдумка: 1"), red=True, words=("t.лишнее", "выдумка"))
        expect(stand, "неверный тип — красный с карточкой и полем",
               card("t.тип", extra='blocked: 5'), red=True, words=("t.тип", "blocked", "число"))
        expect(stand, "логическое строкой — неверный тип",
               card("t.лог").replace("owner_testable: false", 'owner_testable: "нет"'),
               red=True, words=("t.лог", "owner_testable"))
        expect(stand, "статус вне списка схемы — красный",
               card("t.статус", "выдумано"), red=True, words=("t.статус", "выдумано"))
        expect(stand, "статус архива в живой карте — недопустимый переход",
               card("t.снято", "снято"), red=True, words=("t.снято", "снято"))
        # test без проверки — законный вход строителя проверок: красная карта
        # остановила бы его самого (check-builder.sh судит карту этим валидатором).
        expect(stand, "test без результата — зелёный (вход строителя проверок)",
               card("t.безрез", "test"), red=False)
        expect(stand, "подполе словаря вне схемы — красный с путём",
               card("t.под", extra='проверка: { команда: "true", лишнее: 1 }'),
               red=True, words=("t.под", "проверка.лишнее"))
        expect(stand, "поле элемента списка вне схемы — красный с путём",
               card("t.пункт", extra='checklist:\n  - { label: "a", done: false, хвост: 1 }'),
               red=True, words=("t.пункт", "checklist[0].хвост"))
        expect(stand, "тип подполя проверяется",
               card("t.подтип", extra='проверка: { команда: 7 }'),
               red=True, words=("t.подтип", "проверка.команда"))
        expect(stand, "устаревшее поле в пределе — зелёный",
               card("t.стар1", extra='ревизия_старая: "x"'), red=False)
        expect(stand, "устаревшее поле сверх предела — красный (новых не заводить)",
               card("t.стар1", extra='ревизия_старая: "x"') + card("t.стар2", extra='ревизия_старая: "y"'),
               red=True, words=("ревизия_старая", "t.стар2"))
        expect(stand, "строка вместо словаря в карте — красный",
               card("t.строкой", extra='проверка: "команда текстом"'), red=True, words=("t.строкой", "проверка"))
        expect(stand, "архив: снято, тип_в_архиве и устаревшее без предела — зелёный",
               card("a.1", "снято", 'проверка: "текстом"\nревизия_старая: "1"')
               + card("a.2", "test", 'ревизия_старая: "2"') + card("a.3", extra='ревизия_старая: "3"')
               + card("a.4", extra='ревизия_старая: "4"'), red=False, archive=True)
        expect(stand, "архив: поле вне схемы — красный",
               card("a.лишнее", extra="выдумка: 1"), red=True, words=("a.лишнее", "выдумка"), archive=True)
        expect(stand, "архив: статус вне схемы — красный",
               card("a.ст", "выдумано"), red=True, words=("a.ст", "выдумано"), archive=True)
        # DEVMAP_SCHEMA_STRICT=0 (умолчание пакета у копий): поле вне схемы и
        # устаревшее сверх предела — предупреждение с карточкой и полем, код 0.
        expect(stand, "STRICT=0: поле вне схемы — предупреждение, код 0",
               card("t.лишнее", extra="выдумка: 1"), red=False, words=("⚠", "t.лишнее", "выдумка"), strict="0")
        expect(stand, "STRICT=0: устаревшее сверх предела — предупреждение, код 0",
               card("t.стар1", extra='ревизия_старая: "x"') + card("t.стар2", extra='ревизия_старая: "y"'),
               red=False, words=("⚠", "ревизия_старая", "t.стар2"), strict="0")
        expect(stand, "STRICT=0: неверный тип — всё равно красный",
               card("t.тип", extra='blocked: 5'), red=True, words=("t.тип", "blocked"), strict="0")
        expect(stand, "STRICT=0: статус вне списка — всё равно красный",
               card("t.статус", "выдумано"), red=True, words=("t.статус", "выдумано"), strict="0")

    print(f"СХЕМА КАРТЫ: случаев {CASES}, неудач {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
