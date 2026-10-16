#!/usr/bin/env python3
"""Дверь удаления данных: формы обхода, найденные ревью 15.10.2026.

Четыре случая этой двери жили в test_guard_bash.py и НЕ судили ни одной из этих
форм: ревью нашло, что причину можно было подать в кавычках `echo` соседнего
звена, а разрешением владельца служила любая его бытовая фраза с глаголом и
предметом — на живых строках его журнала открывалось удаление ключа канала.

Стенд свой: своё LOG_DIR с машинным журналом входящих, боевые входящие не
читаются. Ключ стенда HARNESS_OWNER_WORD_STEND=1 — без него подмена каталога
журнала сама есть отказ двери.
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
СЛОВО_ПО_ДЕЛУ = f'Удали секреты и копии базы из {ЦЕЛЬ}'

плохо = 0


def позвать(команда: str, двор: pathlib.Path, конф: pathlib.Path) -> int:
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


with tempfile.TemporaryDirectory() as врем:
    двор = pathlib.Path(врем, 'log')
    двор.mkdir(parents=True)
    # Ключ «text» ЛАТИНИЦЕЙ — так пишет живой диспетчер (tg-dispatcher.sh);
    # прежние стенды подавали «текст», то есть форму, которой в жизни нет, и
    # снятие ветки чтения боевого ключа не покраснило бы ни одной пробы
    # (находка ревью 15.10.2026).
    запись = {"ts": datetime.datetime.now().astimezone().isoformat(),
              "chat_id": "1000000001", "text": СЛОВО_ПО_ДЕЛУ, "message_id": "900"}
    (двор / 'inbox.jsonl').write_text(json.dumps(запись, ensure_ascii=False) + '\n',
                                      encoding='utf-8')
    конф = pathlib.Path(врем, 'install.conf')
    конф.write_text(f'LOG_DIR="{двор}"\n', encoding='utf-8')

    # БОЛЬНОЙ СЛУЧАЙ ПЕРВЫМ: законная форма проходит — иначе зелень отказов
    # ничего не стоит (дверь могла быть закрыта вообще для всего).
    судить('БОЛЬНОЙ СЛУЧАЙ: законная форма со словом владельца проходит',
           позвать(f"HARNESS_DATA_DELETE_WHY='его слово' rm -rf {ЦЕЛЬ}", двор, конф), 0)

    # Формы обхода, найденные ревью 15.10.2026.
    судить('причина в кавычках echo соседнего звена — отказ',
           позвать(f"echo \"HARNESS_DATA_DELETE_WHY='x'\" ; rm -rf {ЦЕЛЬ}", двор, конф), 2)
    судить('причина префиксом ДРУГОЙ команды звена — отказ',
           позвать(f"HARNESS_DATA_DELETE_WHY='x' echo foo ; rm -rf {ЦЕЛЬ}", двор, конф), 2)
    судить('причина после команды, а не префиксом — отказ',
           позвать(f"rm -rf {ЦЕЛЬ} HARNESS_DATA_DELETE_WHY='x'", двор, конф), 2)
    судить('причина в звене через && — отказ',
           позвать(f"HARNESS_DATA_DELETE_WHY='x' true && rm -rf {ЦЕЛЬ}", двор, конф), 2)

    # ЦЕЛЬ НЕ НАЗВАНА в слове: его бытовые фразы из живого журнала.
    for фраза in ('доступ к серверу у меня удаленный',
                  'Что за мусор? Откуда он взялся?',
                  'Диск сервера забит, почистить надо'):
        запись['text'] = фраза
        (двор / 'inbox.jsonl').write_text(json.dumps(запись, ensure_ascii=False) + '\n',
                                          encoding='utf-8')
        судить(f'бытовая фраза «{фраза[:34]}» двери не открывает',
               позвать(f"HARNESS_DATA_DELETE_WHY='x' rm -rf {ЦЕЛЬ}", двор, конф), 2)

    # Слово о ДРУГОМ предмете — не разрешение на эту цель.
    запись['text'] = 'Удали секреты и копии базы из /var/www/proba-produkta'
    (двор / 'inbox.jsonl').write_text(json.dumps(запись, ensure_ascii=False) + '\n',
                                      encoding='utf-8')
    судить('слово о ДРУГОМ пути не открывает эту цель',
           позвать(f"HARNESS_DATA_DELETE_WHY='x' rm -rf {ЦЕЛЬ}", двор, конф), 2)
    судить('и для НАЗВАННОГО пути та же запись открывает',
           позвать("HARNESS_DATA_DELETE_WHY='x' rm -rf /var/www/proba-produkta/secrets", двор, конф), 0)

    # ОКНО СВЕЖЕСТИ: разрешение, данное вчера, относилось к другому разговору.
    # Ревью 15.10.2026: этого условия не судил ни один случай двери, и снятие
    # окна оставляло пробы зелёными.
    старое = dict(запись, text=СЛОВО_ПО_ДЕЛУ,
                  ts=(datetime.datetime.now().astimezone()
                      - datetime.timedelta(days=3)).isoformat())
    (двор / 'inbox.jsonl').write_text(json.dumps(старое, ensure_ascii=False) + '\n',
                                      encoding='utf-8')
    судить('слово трёхдневной давности двери не открывает',
           позвать(f"HARNESS_DATA_DELETE_WHY='x' rm -rf {ЦЕЛЬ}", двор, конф), 2)

    # СЛЕД: проход через дверь И-1 обязан оставлять запись — дверь без следа
    # неотличима от дыры (так сказано в самом стороже).
    запись['text'] = СЛОВО_ПО_ДЕЛУ
    (двор / 'inbox.jsonl').write_text(json.dumps(запись, ensure_ascii=False) + '\n',
                                      encoding='utf-8')
    след = двор / 'guard_exceptions.log'
    было = след.read_text() if след.exists() else ''
    позвать(f"HARNESS_DATA_DELETE_WHY='след проверяется' rm -rf {ЦЕЛЬ}", двор, конф)
    стало = след.read_text() if след.exists() else ''
    судить('проход через дверь оставил след с причиной',
           int('след проверяется' in стало[len(было):]), 1)

print(f'ИТОГ: плохих {плохо}')
sys.exit(1 if плохо else 0)
