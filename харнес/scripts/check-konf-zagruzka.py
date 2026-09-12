#!/usr/bin/env python3
"""Гейт: конфиг харнеса читают общим загрузчиком, а не голым `source`.

Откуда взят. Улика 12.09.2026: проба переключения модели шла с
`TMUX_SESSION=проба-модели`, но `source /etc/harness/install.conf` перезаписал
переменную значением «agent» — и команда «/model fable» ушла в РАБОЧУЮ панель
агента. Сессия сменила модель посреди работы; заметил владелец, а не проверка.
Замер того же часа: голый source конфига стоял в 19 скриптах, то есть ЛЮБАЯ
проба, задающая ключи конфига в окружении, играла на боевом.

Что ловим. Строку `source X` / `. X`, где X — конфиг харнеса (путь с
install.conf или harness.conf либо переменная с таким именем). Лечится одной
заменой:

    source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
    konf_zagruzit

Чего НЕ ловим: сам загрузчик, чужие конфиги (МАНИФЕСТ.conf возможности,
временный конфиг внутри пробы) и закомментированные строки — гейт, краснящий
на здоровом коде, отключают.

Прогон: python3 scripts/check-konf-zagruzka.py [--selftest]
"""
import io
import os
import re
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
КАТАЛОГИ = ('scripts', 'харнес')  # scripts обходится рекурсивно, hooks внутри
ЗАГРУЗЧИК = os.path.join('scripts', 'lib', 'konf.sh')

# `source X`, `. X` — с необязательной охраной `[ -r X ] &&` перед ними.
ПОДКЛЮЧЕНИЕ = re.compile(r'(?:^|&&|;)\s*(?:source|\.)\s+("?[^"\s;]+"?)')
# Путь к конфигу харнеса, вписанный прямо.
ПУТЬ_КОНФИГА = re.compile(r'install\.conf|harness\.conf')
# Присваивание переменной пути конфига: `CONF="${HARNESS_INSTALL_CONF:-…}"`,
# `cfg=$INSTALL_CONF`. Имя роли не играет — играет то, ЧТО в неё положили:
# список имён был бы тем же частным случаем, от которого лечится загрузчик
# (ревью кода 12.09.2026, F1-gen-06: `CONF` уже занято в emergency-send.sh).
ПРИСВАИВАНИЕ = re.compile(r'^\s*(?:local\s+|export\s+)?([A-Za-z_][\w]*)=(.*)$')
# Имена, за которыми смысл закреплён во всём харнесе: путь мог быть вычислен в
# другом файле или прийти из окружения, и присваивания в этом файле нет.
ИЗВЕСТНЫЕ_ИМЕНА = {'INSTALL_CONF', 'HARNESS_CONF', 'HARNESS_INSTALL_CONF', 'conf_path'}



def _agent_user() -> str:
    """Имя пользователя-агента из паспорта: вшитое — след чужой установки."""
    import re as _re
    try:
        for строка in open("/etc/harness/install.conf", encoding="utf-8"):
            совпало = _re.match(r'\s*AGENT_USER\s*=\s*"?([^"#\s]*)"?', строка)
            if совпало and совпало.group(1):
                return совпало.group(1)
    except OSError:
        pass
    return "agent"

def находки_в(текст):
    """Строки, где конфиг харнеса читается голым source.

    Имена переменных, в которые положили путь конфига, набираются по ходу
    файла: `conf_path`, `CONF`, `INSTALL_CONF` — всё это одно и то же.
    """
    держат_конфиг = set(ИЗВЕСТНЫЕ_ИМЕНА)
    плохие = []
    for номер, строка in enumerate(текст.split('\n'), 1):
        if строка.lstrip().startswith('#'):
            continue
        присв = ПРИСВАИВАНИЕ.match(строка)
        if присв:
            имя, значение = присв.groups()
            if ПУТЬ_КОНФИГА.search(значение) or any(
                    ('$' + д) in значение or ('${' + д) in значение for д in держат_конфиг):
                держат_конфиг.add(имя)
        for цель in ПОДКЛЮЧЕНИЕ.findall(строка):
            голая = цель.strip('"').lstrip('$').strip('{}')
            if ПУТЬ_КОНФИГА.search(цель) or голая in держат_конфиг:
                плохие.append((номер, строка.strip()[:160]))
                break
    return плохие


def корни():
    """Репозиторий И каталог пакета: раскладка копирует пакет → проект.

    Правка только в репозитории умрёт при следующем разложить-харнес.sh, а
    сам раскладчик и УСТАНОВИТЬ.sh живут ТОЛЬКО в пакете — они читают конфиг в
    самый уязвимый момент, при установке, и до 12.09.2026 их не судил никто
    (ревью кода, F2-gen-04).
    """
    yield КОРЕНЬ, [os.path.join(КОРЕНЬ, к) for к in КАТАЛОГИ]
    пакет = найти_пакет()
    if пакет:
        yield пакет, [пакет]


def найти_пакет():
    """Каталог пакета из STARTER_DIR (данные), либо None — пакета нет."""
    путь = os.environ.get('HARNESS_CONF', '/etc/harness/harness.conf')
    try:
        for строка in io.open(путь, encoding='utf-8').read().splitlines():
            m = re.match(r'\s*STARTER_DIR\s*=\s*"?([^"#\s]*)"?', строка)
            if m and m.group(1):
                каталог = os.path.join(m.group(1), 'СТАРТОВЫЙ-ПАКЕТ', 'харнес')
                return каталог if os.path.isdir(каталог) else None
    except OSError:
        pass
    # STARTER_DIR пуст на этой установке — ищем по домам, как sverit-s-paketom.
    for дом in (os.path.join('/home', _agent_user()), os.path.expanduser('~')):
        каталог = os.path.join(дом, 'starter', 'СТАРТОВЫЙ-ПАКЕТ', 'харнес')
        if os.path.isdir(каталог):
            return каталог
    return None


def файлы_репозитория():
    for корень, каталоги_поиска in корни():
        for основа in каталоги_поиска:
            if not os.path.isdir(основа):
                continue
            for путь, каталоги, имена in os.walk(основа):
                каталоги[:] = [d for d in каталоги if d not in ('.git', '__pycache__')]
                for имя in sorted(имена):
                    if имя.endswith('.sh'):
                        yield корень, os.path.join(путь, имя)


def самотест():
    случаи = [
        ('source "$INSTALL_CONF"\n', 1,
         'БОЛЬНОЙ СЛУЧАЙ: та самая строка демонов, из-за которой проба играла на боевом'),
        ('[ -r "$INSTALL_CONF" ] && source "$INSTALL_CONF"\n', 1,
         'та же строка под охраной читаемости — тот же отказ'),
        ('. "$conf_path"\n', 1,
         'форма хуков: точка вместо source'),
        ('source /etc/harness/harness.conf\n', 1,
         'путь вписан прямо, без переменной'),
        ('source "$(dirname "$0")/lib/konf.sh"\nkonf_zagruzit\n', 0,
         'общий загрузчик — здоровый код'),
        ('source "$MANIFEST"\n', 0,
         'чужой конфиг (МАНИФЕСТ.conf возможности) — не наше дело'),
        ('CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"\nsource "$CONF"\n', 1,
         'БОЛЬНОЙ СЛУЧАЙ 2: переменная названа иначе (`CONF` уже занято в emergency-send.sh)'),
        ('cfg=$INSTALL_CONF\n. "$cfg"\n', 1,
         'путь переложен во вторую переменную — трасса ведёт к конфигу'),
        ('# source "$INSTALL_CONF"\n', 0,
         'закомментированная строка — не код'),
        ('echo "source $INSTALL_CONF"\n', 0,
         'упоминание в тексте сообщения — не подключение'),
        ('INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"\n', 0,
         'вычисление пути конфига — законно, ловим только чтение'),
    ]
    ok = True
    for текст, ждём, зачем in случаи:
        было = len(находки_в(текст))
        if было != ждём:
            ok = False
        print('  %s %s (находок %d, ждали %d)' % ('ок   ' if было == ждём else 'ПЛОХО', зачем, было, ждём))
    print('САМОТЕСТ %s: %d путей, первым — больной случай'
          % ('ПРОЙДЕН' if ok else 'ПРОВАЛЕН', len(случаи)))
    return 0 if ok else 1


def main():
    if '--selftest' in sys.argv:
        return самотест()
    всего = 0
    for корень, путь in файлы_репозитория():
        относительный = os.path.relpath(путь, корень)
        # Сам загрузчик и установщик, который пишет конфиг СВОИМИ руками, а
        # потом его же читает: чужого конфига там нет.
        if относительный in (ЗАГРУЗЧИК, 'scripts/lib/konf.sh', 'УСТАНОВИТЬ.sh'):
            continue
        текст = io.open(путь, encoding='utf-8', errors='replace').read()
        for номер, строка in находки_в(текст):
            всего += 1
            print('%s:%d  %s' % (путь if корень != КОРЕНЬ else относительный, номер, строка))
    if всего:
        print('[загрузка конфига] КРАСНЫЙ: %d голых source конфига — окружение обязано '
              'быть старше файла, иначе проба играет на боевом. Замена: source '
              '"$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh" + konf_zagruzit' % всего)
        return 1
    print('[загрузка конфига] чисто: конфиг читают общим загрузчиком')
    return 0


if __name__ == '__main__':
    sys.exit(main())
