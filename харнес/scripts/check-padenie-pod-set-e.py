#!/usr/bin/env python3
"""Гейт: присваивание из конвейера с grep под `set -e` роняет весь скрипт.

Откуда взят. Улика 11.09.2026 (пункт 6 разбора кода харнеса): в session-warden
стояло `HOLDER=$(fuser … | grep -m1 '[0-9]')` под `set -euo pipefail`. Пустой
ответ fuser законен — держателя чужого пользователя он без root не покажет, —
но пустой grep возвращает 1, и весь сторож умирал кодом 1, не напечатав НИ
ОДНОЙ строки: ветка «кто держит лок» до своих сообщений не доходила. Молчащий
сторож не сторожит, а молчание в логе неотличимо от здоровой уступки дороги.

Что ловим. Строку вида `VAR=$(… | grep …)` в файле с `set -e`, если у неё нет
запасного исхода (`|| true`, `|| echo`, `|| :`). Опасны команды, чей ПУСТОЙ
ответ — законный случай: grep, pgrep, fuser. Продолжения строк («\\» на конце)
склеиваются: запасной исход часто стоит на следующей строке, и без склейки
гейт краснел бы на здоровом коде — а краснящий зря гейт отключают.

Прогон: python3 scripts/check-padenie-pod-set-e.py [--selftest]
"""
import io
import os
import re
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
КАТАЛОГИ = ('харнес/demons', 'scripts', 'scripts/hooks')

ЕСТЬ_SET_E = re.compile(r'^set -[a-z]*e', re.M)
ПРИСВАИВАНИЕ = re.compile(r'^\s*(?:local\s+|export\s+)?[A-Za-z_][A-Za-z_0-9]*=\$\(')
ОПАСНЫЕ = re.compile(r'\|\s*(grep|pgrep|fuser)\b')
ЗАПАСНОЙ_ИСХОД = re.compile(r'\|\|\s*(true|echo|:)\b')
# Вторая форма того же отказа: команда стоит САМА ПО СЕБЕ, её код никто не
# читает. `git grep -q` под set -e уронил check-secrets молча (11.09.2026).
# Внутри if/while/until/elif, после && и ||, с ведущим «!» код читается —
# это законные места, их не трогаем.
ГОЛАЯ_КОМАНДА = re.compile(r'^\s*(git\s+grep|grep|pgrep|fuser)\b')
ЧИТАЮТ_КОД = re.compile(r'^\s*(if|while|until|elif|!)\b|&&|\|\||\|\s*$')


def склеить_продолжения(текст):
    """[(номер первой строки, склеенная строка)] — перенос «\\» не разрывает команду."""
    строки = текст.split('\n')
    склеенные, буфер, начало = [], None, None
    for n, строка in enumerate(строки, 1):
        if буфер is None:
            буфер, начало = строка, n
        else:
            буфер += ' ' + строка.strip()
        if буфер.rstrip().endswith('\\'):
            буфер = буфер.rstrip()[:-1]
            continue
        склеенные.append((начало, буфер))
        буфер = None
    if буфер is not None:
        склеенные.append((начало, буфер))
    return склеенные


def находки_в(текст):
    if not ЕСТЬ_SET_E.search(текст):
        return []
    плохие = []
    for номер, строка in склеить_продолжения(текст):
        if строка.lstrip().startswith('#'):
            continue
        присваивание = ПРИСВАИВАНИЕ.match(строка) and ОПАСНЫЕ.search(строка)
        голая = ГОЛАЯ_КОМАНДА.match(строка) and not ЧИТАЮТ_КОД.search(строка)
        if not (присваивание or голая):
            continue
        if ЗАПАСНОЙ_ИСХОД.search(строка):
            continue
        плохие.append((номер, строка.strip()[:160]))
    return плохие


def самотест():
    случаи = [
        # (текст, ждём находок, чем случай важен)
        ("set -euo pipefail\nHOLDER=$(fuser x | grep -m1 '[0-9]')\n", 1,
         "БОЛЬНОЙ СЛУЧАЙ: ровно тот код, что убивал сторожа молча"),
        ("set -euo pipefail\nHOLDER=$(fuser x | grep -m1 '[0-9]' || true)\n", 0,
         "он же с запасным исходом — здоров"),
        ("set -euo pipefail\nscreen=$(printf x | grep -A 12 y \\\\\n    | cut -c1-400 || true)\n", 0,
         "запасной исход на СЛЕДУЮЩЕЙ строке — склейка обязана его увидеть"),
        ("set -euo pipefail\nlive=$(claude --version | grep -oE '[0-9]+' | head -1)\n", 1,
         "конвейер длиннее двух звеньев — pipefail роняет так же"),
        ("HOLDER=$(fuser x | grep -m1 '[0-9]')\n", 0,
         "без set -e падения нет — не наше дело"),
        ("set -euo pipefail\n# HOLDER=$(fuser x | grep y)\n", 0,
         "закомментированная строка — не код"),
        ("set -euo pipefail\nHOLDER=$(cat x | sort)\n", 0,
         "пустой ответ sort не бывает отказом — не ловим"),
        ("set -euo pipefail\ngit grep --cached -qP 'a' -- x >/dev/null 2>&1\n", 1,
         "БОЛЬНОЙ СЛУЧАЙ 2: голая `git grep -q` — так молча упал check-secrets"),
        ("set -euo pipefail\nif grep -q x f; then echo да; fi\n", 0,
         "grep в условии if — код читают, это законно"),
        ("set -euo pipefail\ngrep -q x f || RC=$?\n", 0,
         "код подхвачен через || — законно"),
        ("set -euo pipefail\nwhile grep -q x f; do :; done\n", 0,
         "grep в условии while — законно"),
        ("set -euo pipefail\ngrep -q x f && echo нашлось\n", 0,
         "код читает && — законно"),
    ]
    ok = True
    for текст, ждём, зачем in случаи:
        было = len(находки_в(текст))
        знак = 'ок   ' if было == ждём else 'ПЛОХО'
        if было != ждём:
            ok = False
        print('  %s %s (находок %d, ждали %d)' % (знак, зачем, было, ждём))
    print('САМОТЕСТ %s: %d путей, первым — больной случай'
          % ('ПРОЙДЕН' if ok else 'ПРОВАЛЕН', len(случаи)))
    return 0 if ok else 1


def main():
    if '--selftest' in sys.argv:
        return самотест()
    всего = 0
    for каталог in КАТАЛОГИ:
        d = os.path.join(КОРЕНЬ, каталог)
        if not os.path.isdir(d):
            continue
        for имя in sorted(os.listdir(d)):
            if not имя.endswith('.sh'):
                continue
            путь = os.path.join(d, имя)
            текст = io.open(путь, encoding='utf-8', errors='replace').read()
            for номер, строка in находки_в(текст):
                всего += 1
                print('%s:%d  %s' % (os.path.relpath(путь, КОРЕНЬ), номер, строка))
    if всего:
        print('[падение-под-set-e] КРАСНЫЙ: %d присваивани(й) без запасного исхода — '
              'пустой grep уронит скрипт молча; допишите «|| true» и разберите пустой случай явно' % всего)
        return 1
    print('[падение-под-set-e] чисто: присваиваний из grep без запасного исхода нет')
    return 0


if __name__ == '__main__':
    sys.exit(main())
