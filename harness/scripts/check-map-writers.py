#!/usr/bin/env python3
"""Гейт: кто пишет карту разработки — берёт ОБЩИЙ лок карты.

Откуда взят. Замер 12.09.2026 (ревью спеки ежемесячной ревизии, I-10):
`dev-map.yaml` пишут пять сторон — живой агент, `devmap-selfheal`, приёмка,
`check-builder` и `task-closer`, — а общий `dev-map.lock` брали двое.
`task-closer` не брал лока вовсе и писал атомарной заменой: атомарность спасает
от полуфайла, но не от гонки — пока демон читал карту, агент дописал карточку,
и замена стёрла её целиком. `devmap-selfheal` брал СВОЙ лок, то есть защищался
только от второго себя, а карту коммитил такой, какой застанет.

Что ловим. Скрипт, который пишет в `dev-map.yaml` (перенаправление, mkstemp с
последующим rename, `Edit`-правка агентом), но не берёт `dev-map.lock`.

Чего НЕ ловим: чтение карты (гейты, приборы, дашборд) — читателям лок не нужен,
их десятки, и требовать лок от них значило бы красить здоровый код.

Прогон: python3 scripts/check-map-writers.py [--selftest]
"""
import io
import os
import re
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
КАТАЛОГИ = ('scripts', 'harness')
# Сам гейт, архиватор и прополка зовутся ЧЕЛОВЕКОМ или другим скриптом под
# локом; демоны и службы — те, кто пишет карту сам по себе.
ПИШУТ_ПО_КОМАНДЕ = ('scripts/archive-map.py', 'scripts/weed-the-map.py',
                    'scripts/check-map-writers.py', 'scripts/acceptance.py')

# Переменная, которой присвоен путь карты: `DEVMAP="$PROJECT_DIR/dev-map.yaml"`,
# `КАРТА = КОРЕНЬ / "dev-map.yaml"`. Судить по признаку «пишет любой файл и
# где-то упоминает карту» нельзя: первая редакция дала девять находок, и все
# девять были ложными — гейты, которые пишут СВОЙ временный файл.
ПУТЬ_КАРТЫ = re.compile(r'^\s*(?:local\s+|export\s+)?([A-Za-zА-Яа-я_][\w]*)\s*=\s*(.*dev-map\.yaml.*)$')
ЧИТАЕТ_КАРТУ = re.compile(r'dev-map\.yaml|DEVMAP')
ОБЩИЙ_ЛОК = re.compile(r'dev-map\.lock')


# Карта проекта, а не её копия: путь ведёт от PROJECT_DIR или от корня
# репозитория. Временные копии (`> "$STAGE_TMP/dev-map.yaml"` в pre-commit,
# образец карты в самотесте хука) — не карта, и лок им не нужен.
БОЕВОЙ_ПУТЬ = re.compile(r'PROJECT_DIR|КОРЕНЬ|^"?dev-map\.yaml')


def пишет_карту(текст):
    """Строки, где записывается БОЕВАЯ карта, а не её временная копия."""
    держат = set()
    for строка in текст.split('\n'):
        совпало = ПУТЬ_КАРТЫ.match(строка)
        if совпало and БОЕВОЙ_ПУТЬ.search(совпало.group(2).strip()):
            держат.add(совпало.group(1))
    if not держат:
        return []
    формы = []
    for имя in держат:
        формы += [
            re.compile(r'>\s*"?\$\{?' + имя + r'\}?'),          # > "$DEVMAP"
            re.compile(re.escape(имя) + r'\.write_text\('),      # КАРТА.write_text(
            re.compile(r'os\.replace\([^)]*' + re.escape(имя)),  # атомарная замена
            re.compile(r'open\(\s*' + re.escape(имя) + r'\s*,\s*["\']w'),
        ]
    найдено = []
    for номер, строка in enumerate(текст.split('\n'), 1):
        if строка.lstrip().startswith('#'):
            continue
        for форма in формы:
            if форма.search(строка):
                найдено.append((номер, строка.strip()[:80]))
                break
    return найдено


# Писатель, которому путь карты ПЕРЕДАЮТ (`map_path`, `paths.map`): присвоения
# пути в файле нет, и `пишет_карту` его не видит. Судим по имени приёмника
# записи — слово map/карт в последнем звене.
ПРИЁМНИК_КАРТЫ = r'(?<![\w.])(?![\w.]*(?:стенд|stand|tmp))[\w.]*(?:map|карт)\w*'
ФОРМЫ_ПО_АРГУМЕНТУ = (
    re.compile(r'\b' + ПРИЁМНИК_КАРТЫ + r'\.write_text\(', re.IGNORECASE),
    re.compile(r'os\.replace\([^)]*,\s*' + ПРИЁМНИК_КАРТЫ + r'\s*\)', re.IGNORECASE),
    re.compile(r'open\(\s*' + ПРИЁМНИК_КАРТЫ + r'\s*,\s*["\']w', re.IGNORECASE),
)


def пишет_карту_по_аргументу(текст, путь=''):
    # Пробы пишут карту СВОЕГО стенда, и лок им не нужен.
    if os.path.basename(путь).startswith('test_'):
        return []
    найдено = []
    for номер, строка in enumerate(текст.split('\n'), 1):
        if строка.lstrip().startswith('#'):
            continue
        if any(форма.search(строка) for форма in ФОРМЫ_ПО_АРГУМЕНТУ):
            найдено.append((номер, строка.strip()[:80]))
    return найдено


def находки_в(текст, путь=''):
    """['причина'] — пишет карту, но общего лока не берёт."""
    if ОБЩИЙ_ЛОК.search(текст):
        return []
    # Библиотека, которой путь карты передают, может не называть файл вовсе —
    # поэтому вторая форма не требует упоминания dev-map.yaml.
    пишет = (пишет_карту(текст) if ЧИТАЕТ_КАРТУ.search(текст) else []) \
        or пишет_карту_по_аргументу(текст, путь)
    if not пишет:
        return []
    номер, строка = пишет[0]
    return ['строка %d пишет карту (%s), а dev-map.lock не берёт' % (номер, строка)]


def файлы_репозитория():
    for каталог in КАТАЛОГИ:
        основа = os.path.join(КОРЕНЬ, каталог)
        if not os.path.isdir(основа):
            continue
        for путь, каталоги, имена in os.walk(основа):
            каталоги[:] = [d for d in каталоги if d not in ('.git', '__pycache__')]
            for имя in sorted(имена):
                if имя.endswith(('.sh', '.py')):
                    yield os.path.join(путь, имя)


def самотест():
    случаи = [
        ('DEVMAP="$PROJECT_DIR/dev-map.yaml"\ncat x > "$DEVMAP"\n', 1,
         'БОЛЬНОЙ СЛУЧАЙ: ровно task-closer до починки — пишет карту без лока'),
        ('DEVMAP="$PROJECT_DIR/dev-map.yaml"\nexec 9>"$LOG/dev-map.lock"\ncat x > "$DEVMAP"\n', 0,
         'тот же демон с общим локом — здоров'),
        ('LOCK="$LOG/devmap-selfheal.lock"\nDEVMAP="$PROJECT_DIR/dev-map.yaml"\ncat y > "$DEVMAP"\n', 1,
         'БОЛЬНОЙ СЛУЧАЙ 2: свой лок вместо общего защищает только от себя'),
        ('DEVMAP="$PROJECT_DIR/dev-map.yaml"\ngit show :dev-map.yaml > "$STAGE_TMP/dev-map.yaml"\n', 0,
         'БОЛЬНОЙ СЛУЧАЙ 3: копия карты во временном каталоге — не карта (так краснел pre-commit)'),
        ('yaml.safe_load(open("dev-map.yaml"))\nprint(итог)\n', 0,
         'читатель карты — лок ему не нужен, читателей десятки'),
        ('нет тут ничего про карту\n', 0,
         'файл вообще не про карту'),
        ('"""dev-map.yaml"""\ndef w(map_path, t):\n    map_path.write_text(t)\n', 1,
         'БОЛЬНОЙ СЛУЧАЙ 4: библиотека пишет карту по аргументу без лока (Р-14)'),
        ('"""dev-map.yaml"""\ndef w(report_path, t):\n    report_path.write_text(t)\n', 0,
         'библиотека пишет по аргументу не карту, а отчёт — здорова'),
    ]
    ok = True
    for текст, ждём, зачем in случаи:
        было = len(находки_в(текст))
        if было != ждём:
            ok = False
        print('  %s %s (находок %d, ждали %d)' % ('ок   ' if было == ждём else 'ПЛОХО', зачем, было, ждём))
    print('САМОТЕСТ %s: %d путей, четыре из них больные случаи'
          % ('ПРОЙДЕН' if ok else 'ПРОВАЛЕН', len(случаи)))
    return 0 if ok else 1


def main():
    if '--selftest' in sys.argv:
        return самотест()
    всего = 0
    for путь in файлы_репозитория():
        относительный = os.path.relpath(путь, КОРЕНЬ)
        if относительный in ПИШУТ_ПО_КОМАНДЕ:
            continue
        текст = io.open(путь, encoding='utf-8', errors='replace').read()
        for причина in находки_в(текст, относительный):
            всего += 1
            print('%s: %s' % (относительный, причина))
    if всего:
        print('[писатели карты] КРАСНЫЙ: %d писател(ей) карты без общего лока — '
              'пока один читает и заменяет файл, другой дописывает карточку, и она '
              'исчезает молча. Лок: $LOG_DIR/locks/services/dev-map.lock, '
              'дескриптор закрывать у потомков (9>&-)' % всего)
        return 1
    print('[писатели карты] чисто: каждый писатель карты берёт общий лок')
    return 0


if __name__ == '__main__':
    sys.exit(main())
