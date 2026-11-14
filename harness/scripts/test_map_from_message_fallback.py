#!/usr/bin/env python3
"""Проба: модель отказала — карточка заводится только на РАБОТУ.

Больной случай 14.11.2026: потолок повода «о-чём-сообщение-владельца» (30 в
сутки) кончился к 20:32, и запасной путь map-from-message.py завёл карточку на
КАЖДОЕ следующее сообщение владельца, взяв заголовком первую строку: «Что
происходит», «Ответ им отправил?», «1. По моему уже лишнее» (ответ на вопрос
агента), «Про ночь не тебе». Из 17 карточек дня 13 были не работой.

══════════════════════════════ КОНТРАКТ ══════════════════════════════════════
`map-from-message.py --сообщение <id> --карта К --инбокс И --линия L`, модель
отказывает (заглушка, код 3 — как при исчерпанном потолке) ИЛИ отвечает
«задач 1» (так она ответила на «че мы сегодня обновим релизы … или что?»):
 • сообщение — только вопросы / ответ на нумерованные вопросы агента / голые
   числа / оговорка «не тебе», и нет слова работы — карточки нет, в спутнике
   «<id> не-задача: …» (гейт задач из канала видит исход);
 • есть поручение (слово работы гейта, «можешь/можем/можно … <глагол>», или
   хоть одна часть сообщения, которую по форме не назвать вопросом/ответом) —
   карточка, строки спутника нет. При сомнении — карточка.
═════════════════════════════════════════════════════════════════════════════

Тексты — настоящие сообщения 14.11.2026, взятые из
/var/log/harness/inbox/обработано/<id>.txt (id — как в источнике карточки).
Ожидание — по разбору оркестратора, кроме двух: «Мне нужно уже быстрее
раскатать…» и «По карте задач, надо все сделать…» несут поручение по форме
(«нужно», «надо»), и правило «вопрос+поручение — карточка, при сомнении —
карточка» даёт им карточку. Что это повтор уже заведённой работы, по форме
сообщения не видно.

Стенд: свой temp-каталог на случай; модель — заглушка HARNESS_SPROSIT_MODEL,
каждый вызов оставляет метку. Живой dev-map.yaml не читается и не пишется.
Дверь: MAP_FROM_MESSAGE_PY — копия механизма для мутаций.
Прогон: python3 scripts/test_map_from_message_fallback.py; код 0 — зелёный.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")

MAP_TEXT = """version: 1
lines:
  - id: core
    name: Ядро
    tasks:
      - id: hr.existing
        status: plan
        title: "Прежняя задача"
        summary: "лежит"
"""

# Настоящая реплика агента перед «1) а как ты проверишь / 2. Пока хватит».
AGENT_NUMBERED = (
    "Два вопроса по карте (остальные сомнения решил сам):\n"
    "1) «На новом сервере где я» — то, что ты тогда видел, не записано. Закрыть "
    "или проверить на новой копии с 0.12.1? Рекомендую: проверить заодно с раскаткой.\n"
    "2) Учёт процессов: ЦП и токены по каждому уже есть, долей в процентах нет. "
    "Хватит так? Рекомендую: хватит.\n"
    "Ответь, например, «1 проверить, 2 хватит».")
AGENT_PLAIN = "Ворота зелёные, выкат 0.12.1 идёт на трёх серверах."

CARD, NO = True, False
# (id, текст, карточка?, реплика агента перед сообщением)
REAL = (
    ("1794653374.428027396", "А ты можешь сделать так чтобы обновление встало без ошибок\n", CARD, AGENT_PLAIN),
    ("1794670347.166011946", "Нам эти флейки проблемы доставляют, если это падает не потому что ошибка в коде, то зачем нам эти флейки нужны, сделай так чтобы они не появлялись\n", CARD, AGENT_PLAIN),
    ("1794671297.006186315", "Сколько задач в карте задач\n", NO, AGENT_PLAIN),
    ("1794671373.872321894", "Почисти карту задач\n", CARD, AGENT_PLAIN),
    ("1794672994.838123056", "Тебе от CF пришла обратная связь? Там не поставился харнес\n", NO, AGENT_PLAIN),
    ("1794673009.254015664", "А чем ночь будет отличаться\n", NO, AGENT_PLAIN),
    ("1794673023.227813095", "Про ночь не тебе\n", NO, AGENT_PLAIN),
    ("1794673304.248510657", "Ответ им отправил?\n", NO, AGENT_PLAIN),
    ("1794673373.064050338", "Что с картой задач?\n", NO, AGENT_PLAIN),
    # Отступ от списка оркестратора: поручение по форме («нужно»/«надо»).
    ("1794673600.970568522", "Мне нужно уже быстрее раскатать обновления на серверах\n", CARD, AGENT_PLAIN),
    ("1794673626.930259283", "По карте задач, надо все сделать. Как можно весь список сделать наиболее эффективно?\n", CARD, AGENT_PLAIN),
    ("1794674166.398306824", "1) а как ты проверишь \n2. Пока хватит\n", NO, AGENT_NUMBERED),
    ("1794674211.349107618", "1. По моему уже лишнее\n", NO, AGENT_NUMBERED),
    ("1794674443.033307173", "Что происходит\n", NO, AGENT_PLAIN),
    ("1794674488.836079097", "Как думаешь за какой срок управимся\n", NO, AGENT_PLAIN),
    ("1794675347.430384193", "Сейчас вот на БКБ ставится обновление. Сначала там пару часов была проверка, потом раскатка, а потом самопроверка. Нахрена так много. Мы можем что-то как-то оптимизировать тоже этот процесс. Проверка на проверке, проверка погоняет. Конечно нам нужна безопасность, стабильность, качество. Это точно все нужно. Но может быть эти проверки как-то оптимизировать нужно? Можно.\n", CARD, AGENT_PLAIN),
    ("1794675460.768614997", "CF когда получит команду\n", NO, AGENT_PLAIN),
)

EXTRA = (
    # вопрос + поручение — карточка
    ("1794680001.000000001", "Что с картой задач? Почисти её", CARD, AGENT_PLAIN),
    ("1794680002.000000002", "Зачем там дубли, убери их", CARD, AGENT_PLAIN),
    ("1794680007.000000007", "Что с картой? Убери дубли", CARD, AGENT_PLAIN),
    # вежливая просьба вопросом — карточка
    ("1794680003.000000003", "Почистишь карту?", CARD, AGENT_PLAIN),
    # нумерованная строка без нумерованных вопросов агента — не ответ, карточка
    ("1794680004.000000004", "1. Убери дубли из карты", CARD, AGENT_PLAIN),
    # вопрос со словом работы гейта или с «можешь <глагол>» — карточка
    ("1794680009.000000009", "Почему бот не работает?", CARD, AGENT_PLAIN),
    ("1794680010.000000010", "Когда можешь почистить карту?", CARD, AGENT_PLAIN),
    # справка о прошлом без вопроса — сообщение о беде, карточка
    ("1794680011.000000011", "Там не поставился харнес", CARD, AGENT_PLAIN),
    # ответ цифрой — без карточки
    ("1794680005.000000005", "2", NO, AGENT_NUMBERED),
    ("1794680006.000000006", "1, 2", NO, AGENT_NUMBERED),
)

# Модель ОТВЕЧАЕТ «задач 1» (как в 10 архивных «задач 1, вопросов 1»): на
# чистый вопрос карточки нет, на поручение — есть. (id, текст, карточка?, реплика)
MODEL_SAYS_ONE = (
    ("1794658800.593611394", "че мы сегодня обновим релизы на других серверах или что?\n", NO, AGENT_PLAIN),
    ("1794481074.103703721", "А чем сервер нагружен?\n", NO, AGENT_PLAIN),
    ("1794361989.411910684", "Ты когда аккаунт сменишь\n", NO, AGENT_PLAIN),
    ("1794599569.426732640", "Как ускорить, как оптимизировать процесс, как сделать поямо скйчас более эффективнуб работу\n", NO, AGENT_PLAIN),
    ("1794653374.428027396", "А ты можешь сделать так чтобы обновление встало без ошибок\n", CARD, AGENT_PLAIN),
    ("1794671373.872321894", "Почисти карту задач\n", CARD, AGENT_PLAIN),
    ("1794680008.000000008", "Что с картой задач? Почисти её", CARD, AGENT_PLAIN),
)
ONE_TASK = {"задачи": ["задача из ответа модели"], "имена": ["model-named-task"],
            "слои": ["harness"], "указания": [], "вопросы": ["вопрос"], "ответы": []}

checked = failed = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok or not detail else f": {detail}"))


def card_ids(text: str) -> set:
    data = yaml.safe_load(text) or {}
    return {t["id"] for line in data.get("lines") or [] for t in line.get("tasks") or []}


def triaged_line(path: Path, message_id: str):
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(rf"{re.escape(message_id)}(?!\d)", line):
            return line
    return None


def judge(message_id: str, text: str, want_card: bool, agent: str, stand: Path,
          model_answer: dict | None) -> None:
    done = stand / "inbox" / "обработано"
    done.mkdir(parents=True)
    (done / f"{message_id}.txt").write_text(text, encoding="utf-8")
    (stand / "docs").mkdir()
    (stand / "docs" / "dev-map-archive.yaml").write_text("tasks: []\n", encoding="utf-8")
    (stand / "память").mkdir()
    log = stand / "log"
    log.mkdir()
    sent = datetime.datetime.fromtimestamp(float(message_id) - 600).isoformat()
    (stand / "tg_out.jsonl").write_text(
        json.dumps({"ts": sent, "text": agent}, ensure_ascii=False) + "\n", encoding="utf-8")
    map_file = stand / "dev-map.yaml"
    map_file.write_text(MAP_TEXT, encoding="utf-8")
    mark = stand / "model-calls"
    stub = stand / "stub-model.py"
    reply = ("print('отказ: «о-чём-сообщение-владельца» уже спрашивали 30 раз за "
             "сутки при потолке 30', file=sys.stderr)\nsys.exit(3)\n"
             if model_answer is None else
             f"print({json.dumps(model_answer, ensure_ascii=False)!r})\n")
    stub.write_text(f"import sys\nwith open({str(mark)!r}, 'a') as f:\n    f.write('1\\n')\n"
                    + reply, encoding="utf-8")
    env = dict(os.environ, LOG_DIR=str(log), HARNESS_LOG_DIR=str(log),
               HARNESS_EVENTS_STATE=str(log / ".events"),
               HEARTBEAT_DIR=str(log / "heartbeat"),
               HARNESS_PANEL_STATE=str(log / "panel-state"),
               HARNESS_SPROSIT_MODEL=str(stub), HARNESS_PLANS_DIR=str(stand / "планы"),
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("HARNESS_DEVMAP", None)
    run = subprocess.run(
        [sys.executable, str(UNDER_TEST), "--сообщение", message_id, "--карта", str(map_file),
         "--инбокс", str(stand / "inbox"), "--линия", "core"],
        capture_output=True, text=True, env=env, cwd=stand, timeout=180)
    tail = (run.stdout + run.stderr).strip().splitlines()[-2:]
    name = (f"{'модель «задач 1»' if model_answer else 'модель отказала'}: "
            f"{text.strip()[:60]!r} → {'карточка' if want_card else 'нет'}")
    try:
        added = card_ids(map_file.read_text(encoding="utf-8")) - {"hr.existing"}
    except yaml.YAMLError as exc:
        case(f"{name}: карта читается", False, str(exc)[:200])
        return
    line = triaged_line(stand / "память" / ".tasks-triaged", message_id)
    calls = len(mark.read_text().splitlines()) if mark.exists() else 0
    case(f"{name}: код 0", run.returncode == 0, f"код {run.returncode}, {tail}")
    case(f"{name}: модель спрошена", calls == 1, f"вызовов {calls}")
    if want_card:
        case(f"{name}: карточка заведена", len(added) == 1, f"новые {sorted(added)}, {tail}")
        case(f"{name}: строки спутника нет", line is None, f"{line!r}")
    else:
        case(f"{name}: карточки нет", not added, f"новые {sorted(added)}, {tail}")
        case(f"{name}: исход «не-задача» в спутнике",
             line is not None and re.search(r"не[- ]задача", line) is not None,
             f"{line!r}, {tail}")


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False, "файла нет")
        print(f"SELFTEST: КРАСНЫЙ ({checked} проверок, неудач {failed})")
        return 1
    cases = ([(*c, None) for c in REAL + EXTRA]
             + [(*c, ONE_TASK) for c in MODEL_SAYS_ONE])
    red = 0
    with tempfile.TemporaryDirectory(prefix="map-fallback-") as raw:
        for number, (message_id, text, want, agent, model_answer) in enumerate(cases, 1):
            was = failed
            stand = Path(raw) / f"случай-{number}"
            stand.mkdir()
            try:
                judge(message_id, text, want, agent, stand, model_answer)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал
                case(f"{text[:40]!r}: исключение", False, f"{type(exc).__name__}: {exc}")
            red += failed > was
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(cases)} случаев, красных случаев {red}, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
