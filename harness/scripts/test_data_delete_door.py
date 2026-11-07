#!/usr/bin/env python3
"""Дверь удаления данных: формы обхода, найденные ревью 15.10.2026, и правило кнопки.

Четыре случая этой двери жили в test_guard_bash.py и НЕ судили ни одной из этих
форм: ревью нашло, что причину можно было подать в кавычках `echo` соседнего
звена. С 06.11.2026 дверь открывает только «Да» КНОПКОЙ на realpath цели (слово
владельца 05.11: «все вопросы по поводу моего Да — кнопками»): текст владельца
её не открывает никогда, он может только закрыть (отзыв). Формы обхода судятся
при ЖИВОЙ кнопке — иначе красное давала бы её нехватка, а не форма.

Стенд свой на каждый случай: своё LOG_DIR с журналом входящих и журналом кнопок,
боевые журналы не читаются. Ключ стенда HARNESS_OWNER_WORD_STEND=1 — без него
подмена каталога журнала сама есть отказ двери.
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile

КОРЕНЬ = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
ХУК = str(КОРЕНЬ / 'scripts' / 'hooks' / 'guard_bash.py')
ЦЕЛЬ = '/var/lib/harness/secrets'
# Владелец стенда: судья засчитывает «Да» только с этим from_id (TG_CHAT_ID конфига стенда).
ВЛАДЕЛЕЦ = 4242
СЛОВО_ПО_ДЕЛУ = f'Удали секреты и копии базы из {ЦЕЛЬ}'

плохо = 0


def позвать(команда: str, конф: pathlib.Path) -> int:
    среда = dict(os.environ, HARNESS_INSTALL_CONF=str(конф),
                 HARNESS_OWNER_WORD_STEND='1')
    готово = subprocess.run([sys.executable, ХУК], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": команда}}),
        capture_output=True, text=True, env=среда, check=False)
    return готово.returncode


def судить(зачем: str, вышло: int, ждём: int) -> None:
    global плохо
    if вышло == ждём:
        print(f'  ок    {зачем}')
    else:
        плохо += 1
        print(f'  ПЛОХО {зачем}: код {вышло}, ждали {ждём}')


def метка(назад_с: float) -> str:
    return (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=назад_с)).isoformat(timespec='seconds')


def стенд(врем: str, имя: str, нажатия=(), слова=()) -> tuple[pathlib.Path, pathlib.Path]:
    """Свежий LOG_DIR: нажатия — [(событие, цель, секунд назад)], слова — [(текст, секунд назад)]."""
    двор = pathlib.Path(врем, имя, 'log')
    двор.mkdir(parents=True)
    # Ключ «text» ЛАТИНИЦЕЙ — так пишет живой диспетчер (tg-dispatcher.sh).
    строки = [{"ts": метка(назад), "chat_id": "1000000001", "text": текст, "message_id": str(900 + i)}
              for i, (текст, назад) in enumerate(слова)]
    (двор / 'inbox.jsonl').write_text(''.join(json.dumps(з, ensure_ascii=False) + '\n' for з in строки),
                                      encoding='utf-8')
    # Живой порядок: просьба ask-consent на минуту раньше нажатия; нажатие несёт from_id владельца.
    конверты, просьбы = [], []
    for i, (событие, цель, назад) in enumerate(нажатия):
        данные = {"nonce": f"n{i}", "вид": "удаление", "цель": цель, "message_id": 700 + i,
                  "from_id": ВЛАДЕЛЕЦ}
        конверты.append({"v": 1, "ts": метка(назад), "src": "scripts/lib/consent_buttons.py",
                         "event": событие, "outcome": "ok", "data": данные})
        просьбы.append({"v": 1, "ts": метка(назад + 60), "src": "scripts/ask-consent.py",
                        "event": "consent.request", "outcome": "ok",
                        "data": {"nonce": f"n{i}", "вид": "удаление", "цель": цель, "почему": "проба"}})
    (двор / 'consent-buttons.jsonl').write_text(
        ''.join(json.dumps(к, ensure_ascii=False) + '\n' for к in конверты), encoding='utf-8')
    (двор / 'consent-requests.jsonl').write_text(
        ''.join(json.dumps(к, ensure_ascii=False) + '\n' for к in просьбы), encoding='utf-8')
    конф = двор.parent / 'install.conf'
    конф.write_text(f'LOG_DIR="{двор}"\nTG_CHAT_ID="{ВЛАДЕЛЕЦ}"\n', encoding='utf-8')
    return двор, конф


ДА = ('consent.yes', os.path.realpath(ЦЕЛЬ), 60)
КОМАНДА = f"HARNESS_DATA_DELETE_WHY='его слово' rm -rf {ЦЕЛЬ}"

with tempfile.TemporaryDirectory() as врем:
    # БОЛЬНОЙ СЛУЧАЙ ПЕРВЫМ: законная форма проходит — иначе зелень отказов
    # ничего не стоит (дверь могла быть закрыта вообще для всего).
    двор, конф = стенд(врем, 'законная', [ДА])
    судить('БОЛЬНОЙ СЛУЧАЙ: «Да» кнопкой на цель и причина в команде — проходит',
           позвать(КОМАНДА, конф), 0)
    след = двор / 'удаления-секретов.jsonl'
    записи = [json.loads(л) for л in след.read_text(encoding='utf-8').splitlines()] if след.exists() else []
    судить('проход оставил след: файл = realpath цели, источник guard_bash',
           int(any(з.get('файл') == os.path.realpath(ЦЕЛЬ) and з.get('источник') == 'guard_bash' for з in записи)), 1)
    # Причина из команды — в след прохода: дверь без следа неотличима от дыры.
    исключения = двор / 'guard_exceptions.log'
    судить('проход оставил след с причиной команды в guard_exceptions.log',
           int(исключения.exists() and 'его слово' in исключения.read_text(encoding='utf-8')), 1)
    судить('второе rm той же цели по тому же «Да» — отказ: «Да» одно, попытка одна',
           позвать(КОМАНДА, конф), 2)

    # Формы обхода, найденные ревью 15.10.2026, — при живой кнопке.
    for номер, (зачем, команда) in enumerate((
            ('причина в кавычках echo соседнего звена', f"echo \"HARNESS_DATA_DELETE_WHY='x'\" ; rm -rf {ЦЕЛЬ}"),
            ('причина префиксом ДРУГОЙ команды звена', f"HARNESS_DATA_DELETE_WHY='x' echo foo ; rm -rf {ЦЕЛЬ}"),
            ('причина после команды, а не префиксом', f"rm -rf {ЦЕЛЬ} HARNESS_DATA_DELETE_WHY='x'"),
            ('причина в звене через &&', f"HARNESS_DATA_DELETE_WHY='x' true && rm -rf {ЦЕЛЬ}"),
            ('кнопка есть, причины в команде нет', f"rm -rf {ЦЕЛЬ}"))):
        _, конф = стенд(врем, f'форма-{номер}', [ДА])
        судить(f'{зачем} — отказ', позвать(команда, конф), 2)

    # ТЕКСТ НЕ ОТКРЫВАЕТ: ни по делу с названным путём, ни бытовые фразы из живого журнала.
    for номер, фраза in enumerate((СЛОВО_ПО_ДЕЛУ, 'доступ к серверу у меня удаленный',
                                   'Что за мусор? Откуда он взялся?', 'Диск сервера забит, почистить надо')):
        _, конф = стенд(врем, f'текст-{номер}', слова=[(фраза, 30)])
        судить(f'слово «{фраза[:34]}» без кнопки двери не открывает', позвать(КОМАНДА, конф), 2)

    # Кнопка на ДРУГОЙ путь — не разрешение на эту цель; на названный — открывает.
    другая = os.path.realpath('/var/www/proba-produkta/secrets')
    _, конф = стенд(врем, 'другой', [('consent.yes', другая, 60)])
    судить('кнопка на ДРУГОЙ путь не открывает эту цель', позвать(КОМАНДА, конф), 2)
    судить('и для НАЗВАННОГО пути та же кнопка открывает',
           позвать("HARNESS_DATA_DELETE_WHY='x' rm -rf /var/www/proba-produkta/secrets", конф), 0)

    # СРОК: «Да» трёхдневной давности относилось к другому разговору.
    _, конф = стенд(врем, 'старое', [('consent.yes', os.path.realpath(ЦЕЛЬ), 3 * 86400)])
    судить('«Да» трёхдневной давности двери не открывает', позвать(КОМАНДА, конф), 2)

    # «Нет» после «Да» и отзыв текстом закрывают.
    _, конф = стенд(врем, 'нет', [('consent.yes', os.path.realpath(ЦЕЛЬ), 120), ('consent.no', os.path.realpath(ЦЕЛЬ), 60)])
    судить('«Нет» кнопкой после «Да» закрывает', позвать(КОМАНДА, конф), 2)
    _, конф = стенд(врем, 'отзыв', [('consent.yes', os.path.realpath(ЦЕЛЬ), 120)], слова=[('стоп, не удаляй секреты', 60)])
    судить('отзыв текстом после «Да» закрывает', позвать(КОМАНДА, конф), 2)

print(f'ИТОГ: плохих {плохо}')
sys.exit(1 if плохо else 0)
