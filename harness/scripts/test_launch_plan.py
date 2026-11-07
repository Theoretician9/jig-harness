#!/usr/bin/env python3
"""Пробы плана запуска помощников (scripts/launch-plan.py) — до кода.

Спека: docs/specs/2026-10-29-правила-вложенности-кодом.md (§3–§7, таблица §9).
Круг 2 (решения оркестратора 30.10): мелочь не ждёт (Н-10), §5.4 — план
закрыт, глава удалена, план переименован; §5.2 (map-from-message.py) —
scripts/test_map_from_message_chapter.py.
Каждая строка §9 — случай этой пробы или пробы хука
(scripts/hooks/test_launch_plan_guard.py); карта соответствия —
.tasks/hr.nesting-creation-rules/пробы-карта.md.

══════════════════════════ КОНТРАКТ launch-plan.py ══════════════════════════

Вызов (флаги стоят до или после задачи, порядок не важен):
    python3 scripts/launch-plan.py [--карта F] [--планы D] [--json] <задача>
    python3 scripts/launch-plan.py --нужен-план <задача> [--json]
    python3 scripts/launch-plan.py --проверить-план <план>
    python3 scripts/launch-plan.py --прижить <задача> <план>/<глава>[/<подглава>…] [--новая-глава "<название>"]
    python3 scripts/launch-plan.py --закрепить <план>
    python3 scripts/launch-plan.py --отметить <ид> верно|ложно [--кто <имя>]
    python3 scripts/launch-plan.py --метрики [--сутки N] [--json | --строка]

  --карта F   dev-map.yaml (иначе env HARNESS_DEVMAP, иначе карта дерева).
  --планы D   каталог планов (иначе env HARNESS_PLANS_DIR, иначе
              $HARNESS_PROJECT_ROOT/docs/plans).
  --json      одна JSON-строка в stdout вместо текста.

Окружение:
  HARNESS_HELPER_ROLES   helper-roles.yaml; всё судится блоком `вывод_роли:`
                         (правила, порог_мелочи, порог_резки_сложности,
                         строгость, сложность, план_нужен, предохранитель
                         {жёсткий_верх, порог_нагрузки, окно_живых},
                         верх_вложенности, порог_приживления, обкатка,
                         таймаут_плана_с — предел вызова launch-plan.py из хука).
  HARNESS_PIPELINE_CONF  pipeline.yaml: веса, пороги П1/П2, шаги, характеры
                         (то же имя, что у agent_budget_guard.py).
  HARNESS_CONF           harness.conf: MIGRATIONS_GLOB, PROD_PATHS, GUARD_PATHS
                         (признаки цены, через pipeline.пути_установки).
  HARNESS_PROJECT_ROOT   корень проекта: от него считаются пути признаков цены
                         (PROD_PATHS — префиксы от корня), относительные пути
                         частей, .tasks/<id>/pretask.yaml. Иначе — корень дерева.
  HARNESS_LOG_DIR/LOG_DIR журналы (events.log_dir()).
  HARNESS_NOW            «сейчас», секунды эпохи (окно живых, срок двери).
  HARNESS_MAP_LOCK_WAIT  сколько секунд ждать лок карты при --прижить/--закрепить
                         (по умолчанию 30); истекло — код 1 «лок карты занят»,
                         карта и план не тронуты.

Данные плана. План — первый блок ```yaml файла <планы>/<план>.md с корнем
`план:` {id, владелец, цель, главы: [{id, название, охват, главы?, части?}],
части?}. Части задачи (список {файлы, вид, зависит_от: [номера с 1]}) ищутся
по порядку: `части:` главы карточки → `части:` плана карточки → `части:` плана,
чей владелец — задача → .tasks/<id>/pretask.yaml (`части:` того же вида либо
`файлы:` — одна часть) → заявки задачи в заявки-на-файлы.jsonl (одна часть).
Пути частей: относительный — от HARNESS_PROJECT_ROOT, затем realpath.
Глава карточки: поле `глава: <план>/<глава>[/<подглава>…]`.

Признаки и решение (§3.1, §3.2, §6.1):
  групп   — связные компоненты частей (общий файл или зависит_от); общий файл
            у НЕЗАВИСИМЫХ частей (нет пути по зависит_от) и цикл — код 1.
  шагов   — pipeline.yaml шаги.<глубина.уровень карточки>, суженные
            характеры.<характер>.шаги (список; «все» — без сужения).
  сложность — цена() по файлам частей (признаки без индекса git: realpath
            файла относительно realpath корня, добавлено 0, новый = файла нет): < П1 простая, < П2
            средняя, иначе высокая; файлов нет — volume.complexity по
            вывод_роли.сложность.complexity, след «сложность по оценке»;
            уровень «крупная» поднимает до средней.
  сложность_режет — цена ≥ порог_резки_сложности.цена (файлов нет —
            complexity ≥ порог_резки_сложности.complexity).
  ── живые (круг 1, по боевым журналам; разведка 30.10 13:16) ──
  Что пишется в бою. agent.launch (agent_budget_guard.py, вызовы-агентов.jsonl):
  task всегда «-», data {ревью, описание, уровень, управляющий? (agent_id
  зовущего управляющего), часть? (метка из prompt — пишет хук бюджета,
  проба scripts/hooks/test_agent_budget_part_label.py)}; ид РЕБЁНКА в строке
  нет (PreToolUse — ребёнка ещё нет). file.claim (заявки-на-файлы.jsonl):
  data.агент = agent_id помощника, task «-». role.decision
  (lead_role_guard.py, роли-помощников.jsonl): строка на КАЖДЫЙ Bash/Write/
  Edit/Agent помощника, data.агент = agent_id («-» — смена), task — трейлер
  истории (task_from history), задачей помощника НЕ считается. Процессов
  помощников в ps нет (они внутри процесса claude), события конца помощника
  нет (SubagentStop/PostToolUse Agent в settings нет) — источника ps нет.
  ид живых = data.агент (≠ «-») строк file.claim ∪ role.decision за
            окно_живых минут (активность помощника).
  вызовы  = строки agent.launch outcome ok|skipped (skipped — хук бюджета не
            судил, запуск состоялся) за окно_живых.
  новых   = ид из заявок/ролей, чья ПЕРВАЯ строка в прочитанном хвосте не
            раньше самого раннего вызова окна (ребёнок, начавший работать
            после запуска).
  без_активности = max(0, вызовы − новых) — запущены, но ещё не действовали.
  живых   = |ид живых| + без_активности (запуск и его ребёнок — один живой).
  задачи  — только из вызовов окна: задача метки data.часть («Часть:
            <путь>[#n]», #n не обязателен — так метит запуск управляющего;
            задача — последний сегмент пути); вызов без метки от управляющего L
            (data.управляющий) — задача любого меченого вызова L в окне, иначе
            ключ «управляющий:L» (анонимные дети одного управляющего — одна
            задача); вызов смены без метки — своя задача. task заявок и ролей
            задачей не считается (это wip смены или трейлер истории).
            Помощник, чей запуск старше окна, задачи не добавляет (занижение
            задач безопаснее: верх держит живых).
  задач_в_работе = число разных задач.
  свободно = жёсткий_верх − живых; ёмкость = max(свободно, 0).
  нужно   = групп первой волны без частей «ждёт» (файл в живой заявке другого
            помощника с другой задачей).
  правила — по фактам, где групп = нужно (группа, что ждёт, не делит работу);
            символ в условии («>=порог_нагрузки», «<=порог_мелочи.файлов»,
            «>=параллель_от_групп») — ключ вывод_роли или вывод_роли.
            предохранитель, вложенный ключ через точку.
  исполнителей: первая подошедшая строка `правила`; «ёмкость» и
            «min(групп, ёмкость)» = min(нужно, ёмкость), нужно 0 (файлов ещё
            нет) считается 1; ёмкость 0 — исполнителей 0, ждать: true, в
            тексте «ждать»; иначе ждать: false (ждать только при ёмкости 0).
            Правило мелочь не ждёт НИКОГДА (слово владельца 30.10, ревью
            Н-10: контроль мелочь не трогает): исполнителей 1, ждать: false и
            при ёмкости 0, строка ёмкости без «ждать».
  План закрыт (§5.4): владелец плана задачи (план её главы) со status done, а
            у плана есть открытые задачи (карточки с глава: <план>/…, status не
            done) — в «след» решения и --нужен-план строка «план закрыт с
            открытыми задачами: <план>: <задача>, <задача>…» (все открытые,
            закрытые не называются); код 0, текст без --json печатает её
            строкой «след: …».
  План задачи без блока план: — файл .tasks/<id>/план.md (как
            check-pipeline.py путь_бумаги(карточка, "план")): решение.план = его
            путь, состояние не «нужен_план_нет».
  Имена кода без транслита (ревью Н-18): chapter_ctx, ключ "chapter".

Вывод решения (--json), ключи:
  задача, состояние (норма|мелкая|нужен_план_нет|skipped), почему (у skipped),
  правило, роль, исполнителей, глубина, ждать,
  признаки {групп, файлов, шагов, уровень, сложность, источник_сложности
            (цена|оценка), цена (int|null), complexity, сложность_режет},
  исход_плана, глава, план (путь файла плана или null),
  части [{n, файлы [абсолютные realpath], зависит_от, группа, волна, ждёт, метка}],
  волн, ёмкость {жёсткий_верх, живых, свободно, задач_в_работе,
            задачи [имена, по алфавиту], по_источникам {заявки, роли, вызовы,
            без_активности}, нужно, ждут, урезал},
  строка_ёмкости, модели, след [строки], хеш_плана.
  хеш_плана — «sha256:<hex>» байтов файла-источника частей (план или
  pretask.yaml); хук сверяет его с файлом и предупреждает «устарел».
  Метка части: «Часть: <глава карточки>/<задача>#<n>», без главы
  «Часть: <задача>#<n>».
  Текст (без --json) содержит строку_ёмкости как отдельную строку:
  «ёмкость: свободно X из V, живых L, задач в работе Y; [сложность: цена N ≥
  порог резки P; ]решение: Z (правило…)»; часть «сложность: … порог резки»
  только под нагрузкой (задач_в_работе ≥ порог_нагрузки); при расхождении
  источников живых — «заявок A, ролей R, вызовов B».
Итог решения: $LOG_DIR/планы-запуска/<задача>.json (тот же JSON + хеш_плана).

--нужен-план (--json): {задача, исход (часть_плана|сирота|кандидат|нужен_план|
  мелкая), глава, подсказка [пути глав], след}. Подсказка — главы открытых
  планов (владелец не done) с наибольшей долей пересечения ≥
  порог_приживления, ничья — все.

Журнал $LOG_DIR/вложенность.jsonl (events.emit, journal="вложенность"):
  nesting.plan    src scripts/launch-plan.py, task <задача>, outcome ok
                  (skipped — правило мелочь или битые данные, почему в data),
                  data {ид, правило, роль, исполнителей, сложность,
                  источник_сложности, исход_плана, групп, файлов,
                  ёмкость {свободно, живых, задач_в_работе}, решение, урезал}.
  nesting.decision src scripts/hooks/launch_plan_guard.py (решение хука),
                  data {ид, часть, роль, правило, отказ (1–5|null),
                  предупреждение (bool), дверь, строгость, почему}.
  nesting.mark    task — task той строки журнала, чей data.ид отмечается (не
                  wip и не трейлер истории); data {ид, оценка: верно|ложно, кто}.
  nesting.chapter data {задача, глава, источник: сообщение|план|смена|подсказка}.

--метрики --json: {решений, вне_мелкой, по_ролям {роль: доля}, по_сложности,
  по_исходам, правила {имя: число}, сняли_дробление {нагрузка, простая},
  резка {порезали, не_порезали, почему {групп_1, свободно_меньше_2}},
  предупреждения {всего, верных, ложных, неотмеченных}, доля_дверей,
  доля_с_меткой, сбоев, к_красному {готово, пункты {решений, ложных,
  неотмеченных, метка, сбоев, дверей}}}. Доли — 0..1, 2 знака.
  Решения — строки nesting.plan (ok и skipped правила мелочь; skipped по битым
  данным решением не считается), по ОДНОЙ на задачу — последняя в окне
  (20 пересчётов одной задачи — одно решение); вне_мелкой — исход_плана не
  «мелкая». Резка: решение с сложность_режет при задач_в_работе ≥
  порог_нагрузки; правило резка_под_нагрузкой* — порезали, иначе не порезали:
  групп < 2 — групп_1, иначе свободно < 2 — свободно_меньше_2.
  сбоев — строки хука skipped с data.сбой (сбой|таймаут) в окне.
  к_красному — критерии §7 по блоку вывод_роли.обкатка (решений_мин,
  ложных_макс %, неотмеченных_макс %, метка_мин %, сбоев_за_сутки,
  дверей_макс %): пункт true — выполнен; готово — все true. Текст без --json
  содержит строку «к красному: готово|нет, …» с пунктами.
  Предупреждения — строки nesting.decision хука с data.предупреждение;
  неотмеченные — без nesting.mark на их data.ид; доля_с_меткой = строки хука
  с data.часть / все строки хука; доля_дверей = строки хука с data.дверь / все.
--метрики --строка: ОДНА строка для session_state.sh:
  «вложенность: решений N (вне мелкой M), lead L%, параллельных P%,
   тяжёлых H%, предупреждений W: ложных F, неотмеченных U, с меткой K%,
   дверей D%» (параллельных — доля решений с исполнителей ≥ 2,
   тяжёлых — доля сложности «высокая», проценты целые).

--проверить-план <план>: правила §4.3 по плану И ссылки карты на него (§5.4).
  Карточка с глава: <план>/<путь>, чьей главы в плане нет (глава удалена), —
  код 1, по строке на каждую такую карточку: «сирота: <задача> → <глава>».
  Плана с таким id нет, а карточки на него ссылаются (план переименован) —
  код 1, тот же перечень по всем этим карточкам. Плана нет и ссылок нет —
  код 1 «план не найден». Карточки чужих планов не называются.

Правка карты и плана — только вставкой текста под dev-map.lock (cards.map_lock),
сверка числа карточек и yaml.safe_load; комментарии сохраняются. Лок: карта
дерева — cards.lock_path() (общий с демонами); другая карта (--карта/
HARNESS_DEVMAP, realpath не карта дерева) — <каталог карты>/dev-map.lock.
Ожидание лока — не дольше HARNESS_MAP_LOCK_WAIT.
Глубина главы сверяется с верх_вложенности из данных и в --прижить (путь
глубже — код 1), и в --закрепить (подглава плана глубже — код 1).

Коды выхода: 0 — решение (в том числе ждать, предупреждения, skipped по
битым данным); 1 — нарушение данных (пересечение, цикл, «нечего делить»,
сирота (и перечень сирот --проверить-план), глубже верх_вложенности,
правило §4.3, нет карточки, неизвестный ид
отметки, задача уже в главе); 2 — неверный вызов.

═════════════════════════════════════════════════════════════════════════════

Стенд: свой temp-каталог (проект, планы, карта, журналы), копии
helper-roles.yaml/pipeline.yaml/harness.conf. В копии pipeline.yaml у
«обычная» 2 шага, у «крупная» 7 — числа стенда, а не боевые. Строки журналов
стенда — копия формата боевых строк (agent.launch, file.claim, role.decision)
с заменой значений; нагрузка — меченые запуски смены, как в бою.
окно_живых стенда — своё (ROLES); боевой helper-roles.yaml пробы не читают,
его значение (30 или 15) на них не влияет.
Механизм — рядом либо HARNESS_LAUNCH_PLAN (дверь для мутаций копии).
Прогон: python3 scripts/test_launch_plan.py
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
LAUNCH_PLAN = Path(os.environ.get("HARNESS_LAUNCH_PLAN") or HERE / "launch-plan.py")


def tree_file(*parts: str) -> Path:
    # Проект: scripts/ рядом с harness/; пакет: harness/scripts/ рядом с demons/.
    for candidate in (HERE.parent / "harness" / Path(*parts), HERE.parent / Path(*parts)):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / Path(*parts)


# Судья скорости: launch-plan.py грузит его из <корень проекта>/harness/demons, стенд кладёт
# туда ссылку на настоящий файл (или на копию под мутацией — дверь SPEED_MODE_BIN).
SPEED_JUDGE = Path(os.environ.get("SPEED_MODE_BIN") or tree_file("demons", "speed_mode.py"))
SRC = "scripts/launch-plan.py"
HOOK_SRC = "scripts/hooks/launch_plan_guard.py"
CLAIM_SRC = "scripts/hooks/file_claim_guard.py"
BUDGET_SRC = "scripts/hooks/agent_budget_guard.py"
ROLE_SRC = "scripts/hooks/lead_role_guard.py"
NEST_JOURNAL = "вложенность.jsonl"
ROLE_JOURNAL = "роли-помощников.jsonl"
LAUNCH_JOURNAL = "вызовы-агентов.jsonl"
LEAD, WORKER = "harness-lead", "harness-worker"
HISTORY_TASK = "задача-из-трейлера"

# Правила ссылаются на пороги символами: копия yaml, где меняется ТОЛЬКО ключ
# порога, обязана менять вывод (ревью Н-4).
RULES = [
    {"имя": "мелочь", "если": {"сложность": "простая", "групп": "<=1",
                               "файлов": "<=порог_мелочи.файлов"},
     "роль": WORKER, "исполнителей": 1, "глубина": 1},
    {"имя": "простая", "если": {"сложность": "простая"},
     "роль": WORKER, "исполнителей": 1, "глубина": 1},
    {"имя": "резка_под_нагрузкой_лид",
     "если": {"задач_в_работе": ">=порог_нагрузки", "сложность_режет": "да",
              "групп": ">=параллель_от_групп", "ёмкость": ">=2"},
     "роль": LEAD, "исполнителей": "min(групп, ёмкость)", "глубина": 2},
    {"имя": "резка_под_нагрузкой",
     "если": {"задач_в_работе": ">=порог_нагрузки", "сложность_режет": "да",
              "групп": ">=2", "ёмкость": ">=2"},
     "роль": WORKER, "исполнителей": "min(групп, ёмкость)", "глубина": 1},
    {"имя": "нагрузка", "если": {"задач_в_работе": ">=порог_нагрузки"},
     "роль": WORKER, "исполнителей": 1, "глубина": 1},
    {"имя": "параллель", "если": {"групп": ">=параллель_от_групп",
                                  "ёмкость": ">=параллель_от_групп"},
     "роль": LEAD, "исполнителей": "ёмкость", "глубина": 2},
    {"имя": "крупная", "если": {"сложность": "высокая", "шагов": ">=5", "ёмкость": ">=2"},
     "роль": LEAD, "исполнителей": "ёмкость", "глубина": 2},
    {"имя": "две", "если": {"групп": ">=2", "ёмкость": ">=2"},
     "роль": WORKER, "исполнителей": 2, "глубина": 1},
    {"имя": "прочее", "если": {}, "роль": WORKER, "исполнителей": 1, "глубина": 1},
]

ROLES = {"вывод_роли": {
    "порог_мелочи": {"файлов": 3},
    "параллель_от_групп": 3,
    "порог_резки_сложности": {"цена": 6, "complexity": 4},
    "строгость": "жёлтый",
    "сложность": {"complexity": {"простая": "<=2", "средняя": 3, "высокая": ">=4"}},
    "правила": RULES,
    "план_нужен": {"шагов": 5, "групп": 2},
    "предохранитель": {"жёсткий_верх": 5, "порог_нагрузки": 3, "окно_живых": 30},
    "верх_вложенности": 3,
    "порог_приживления": 0.5,
    "таймаут_плана_с": 60,
    "обкатка": {"суток": 3, "решений_мин": 20, "ложных_макс": 10,
                "неотмеченных_макс": 20, "метка_мин": 80, "сбоев_за_сутки": 0,
                "дверей_макс": 10},
}}

PIPELINE = {
    "веса": {"миграция": 5, "код_продукта": 3, "сторож_или_гейт": 3, "новый_файл": 1,
             "за_100_строк": 1, "за_100_строк_потолок": 3,
             "за_5_файлов": 1, "за_5_файлов_потолок": 2},
    "пороги": {"П1": 3, "П2": 6},
    "шаги": {"мелкая": ["тесты"],
             "обычная": ["тесты", "ревью_кода"],
             "крупная": ["спека", "ревью_спеки", "контракт", "тесты", "план",
                         "ревью_кода", "мутации"]},
    "характеры": {"разработка": {"шаги": "все"},
                  "починка": {"шаги": ["тесты", "ревью_кода"]}},
}

CONF_TEXT = 'MIGRATIONS_GLOB="migr/*"\nPROD_PATHS="prod/"\nGUARD_PATHS="guard/"\n'
MAP_COMMENT = "# Комментарий карты: проба сверяет, что он пережил правку кодом."
PLAN_COMMENT = "# Комментарий плана: переживёт вставку главы."
PLAN_TAIL = "Текст после блока: шаги исполнителя, их код не трогает."

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def set_path(tree: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for key in keys[:-1]:
        tree = tree.setdefault(key, {})
    tree[keys[-1]] = value


def iso_ago(minutes: float) -> str:
    moment = dt.datetime.now().astimezone() - dt.timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


def plan_block(text: str):
    """yaml первого блока ```yaml плана либо None."""
    found = re.search(r"```yaml\n(.*?)\n```", text, re.S)
    return yaml.safe_load(found.group(1)) if found else None


def comment_lines(text: str) -> list:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]


def map_cards(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {t["id"]: t for line in data.get("lines") or [] for t in line.get("tasks") or []}


class Stand:
    """Свой проект, планы, карта, журналы и копии данных."""

    def __init__(self, base: Path, roles: dict | None = None, pipeline: dict | None = None):
        self.base = base
        self.root = base / "proj"
        self.logs = base / "logs"
        self.plans_dir = self.root / "docs" / "plans"
        for folder in (self.root, self.logs, self.plans_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.devmap = self.root / "dev-map.yaml"
        self.roles_path = base / "helper-roles.yaml"
        self.pipeline_path = base / "pipeline.yaml"
        self.conf = base / "harness.conf"
        self.conf.write_text(CONF_TEXT, encoding="utf-8")
        self.cards: dict = {}
        self.plans: dict = {}
        self.set_roles(roles or {})
        self.set_pipeline(pipeline or {})

    # ── данные ──────────────────────────────────────────────────────────
    def set_roles(self, changes: dict, raw: str | None = None) -> None:
        if raw is not None:
            self.roles_path.write_text(raw, encoding="utf-8")
            return
        data = copy.deepcopy(ROLES)
        for dotted, value in changes.items():
            set_path(data["вывод_роли"], dotted, value)
        self.roles_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")

    def set_pipeline(self, changes: dict) -> None:
        data = copy.deepcopy(PIPELINE)
        for dotted, value in changes.items():
            set_path(data, dotted, value)
        self.pipeline_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                      encoding="utf-8")

    def file(self, rel: str, exists: bool = True) -> str:
        path = self.root / rel
        if exists:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
        return str(path)

    def card(self, task: str, status: str = "wip", level: str = "обычная",
             character: str = "разработка", complexity: int = 3,
             chapter: str | None = None) -> None:
        self.cards[task] = {"id": task, "status": status, "уровень": level,
                            "характер": character, "complexity": complexity,
                            "глава": chapter}

    def plan(self, pid: str, owner: str | None = None, chapters: list | None = None,
             parts: dict | None = None, owner_status: str = "plan") -> dict:
        owner = owner or f"владелец-{pid}"
        self.plans[pid] = {"id": pid, "владелец": owner, "цель": f"цель {pid}",
                           "главы": chapters if chapters is not None
                           else [{"id": "c", "название": "Глава c", "охват": []}],
                           "части": parts or {}}
        if owner not in self.cards:
            self.card(owner, status=owner_status)
        return self.plans[pid]

    def task_in_plan(self, task: str, parts: list, pid: str = "P", chapter: str = "c",
                     **card) -> None:
        if pid not in self.plans:
            self.plan(pid)
        self.plans[pid]["части"][task] = parts
        self.card(task, chapter=f"{pid}/{chapter}", **card)

    def pretask(self, task: str, files: list | None = None, parts: list | None = None) -> None:
        folder = self.root / ".tasks" / task
        folder.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "задача": task}
        if files is not None:
            data["файлы"] = files
        if parts is not None:
            data["части"] = parts
        (folder / "pretask.yaml").write_text(yaml.safe_dump(data, allow_unicode=True),
                                             encoding="utf-8")

    def append(self, journal: str, row: dict) -> None:
        with (self.logs / journal).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def claim(self, agent: str, task: str, path: str | None = None, age_min: float = 1) -> None:
        self.append("заявки-на-файлы.jsonl", {
            "v": 1, "ts": iso_ago(age_min), "src": CLAIM_SRC, "event": "file.claim",
            "outcome": "ok", "task": task, "task_from": "arg", "session": "проба",
            "data": {"путь": path or self.file(f"busy/{agent}.py"), "агент": agent,
                     "тип": WORKER}})

    def launch_row(self, mark: str | None = None, lead: str | None = None,
                   age_min: float = 1, outcome: str = "ok") -> None:
        """Строка agent_budget_guard.py в боевом формате: task «-», у запуска
        управляющим — data.управляющий, метка — data.часть (если была в prompt)."""
        data = {"ревью": False, "описание": "исполнитель",
                "уровень": "управляющий" if lead else "без-задачи"}
        if outcome == "skipped":
            data.update(уровень="-", почему="потолок не определён")
        if lead:
            data["управляющий"] = lead
        if mark:
            data["часть"] = mark
        self.append(LAUNCH_JOURNAL, {
            "v": 1, "ts": iso_ago(age_min), "src": BUDGET_SRC, "event": "agent.launch",
            "outcome": outcome, "task": "-", "task_from": "arg", "session": "проба",
            "data": data})

    def role_row(self, agent: str, role: str = WORKER, tool: str = "Bash",
                 age_min: float = 1) -> None:
        """Строка lead_role_guard.py в боевом формате: task — трейлер истории."""
        self.append(ROLE_JOURNAL, {
            "v": 1, "ts": iso_ago(age_min), "src": ROLE_SRC, "event": "role.decision",
            "outcome": "ok", "task": HISTORY_TASK, "task_from": "history",
            "session": "проба",
            "data": {"роль": role, "инструмент": tool, "решение": "пропуск",
                     "причина": "-", "агент": agent}})

    def hook_row(self, outcome: str, data: dict, task: str = "-", age_min: float = 1) -> None:
        self.append(NEST_JOURNAL, {
            "v": 1, "ts": iso_ago(age_min), "src": HOOK_SRC, "event": "nesting.decision",
            "outcome": outcome, "task": task, "task_from": "arg", "session": "проба",
            "data": data})

    # ── запись карты и планов ───────────────────────────────────────────
    def render_map(self) -> str:
        out = ["version: 1", MAP_COMMENT, "lines:", "  - id: стенд", "    layer: infra",
               '    name: "Стенд пробы"', "    tasks:"]
        for card in self.cards.values():
            out += [f"      # карточка {card['id']}: комментарий у карточки",
                    f"      - id: {card['id']}",
                    f"        status: {card['status']}",
                    f'        title: "проба {card["id"]}"',
                    '        summary: "карточка стенда"',
                    f"        глубина: {{ уровень: {card['уровень']}, характер: "
                    f"{card['характер']}, кем: проба, когда: \"2026-10-30\" }}",
                    f"        volume: {{ loc: 100, complexity: {card['complexity']}, "
                    "tokens_k: 50, estimated: true }"]
            if card["глава"]:
                out.append(f"        глава: {card['глава']}")
        out += ["# Хвост карты: тоже комментарий.", "layers: []", "queue: []", ""]
        return "\n".join(out)

    def render_plan(self, plan: dict) -> str:
        body = yaml.safe_dump({"план": plan}, allow_unicode=True, sort_keys=False)
        return (f"# План {plan['id']}\n\n```yaml\n{PLAN_COMMENT}\n{body.rstrip()}\n```\n\n"
                f"{PLAN_TAIL}\n")

    def write(self) -> None:
        self.devmap.write_text(self.render_map(), encoding="utf-8")
        for pid, plan in self.plans.items():
            (self.plans_dir / f"{pid}.md").write_text(self.render_plan(plan), encoding="utf-8")

    # ── запуск ──────────────────────────────────────────────────────────
    def env(self, now: float | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("HARNESS_TASK",)}
        env.update(HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                   HARNESS_DEVMAP=str(self.devmap), HARNESS_PLANS_DIR=str(self.plans_dir),
                   HARNESS_HELPER_ROLES=str(self.roles_path),
                   HARNESS_PIPELINE_CONF=str(self.pipeline_path),
                   HARNESS_CONF=str(self.conf), HARNESS_PROJECT_ROOT=str(self.root),
                   HARNESS_EVENTS_STATE=str(self.base / "state"),
                   PYTHONDONTWRITEBYTECODE="1")
        if now is not None:
            env["HARNESS_NOW"] = str(now)
        return env

    def run(self, *args: str, extra_env: dict | None = None) -> tuple:
        proc = subprocess.run(
            [sys.executable, str(LAUNCH_PLAN), "--карта", str(self.devmap),
             "--планы", str(self.plans_dir), *args],
            capture_output=True, text=True, env={**self.env(), **(extra_env or {})},
            timeout=120)
        return proc.returncode, proc.stdout, proc.stderr

    def decide(self, task: str, *extra: str) -> tuple:
        """(код, JSON решения или {}, весь вывод)."""
        code, out, err = self.run(task, "--json", *extra)
        return code, parse_json(out), out + err

    def need_plan(self, task: str) -> tuple:
        code, out, err = self.run("--нужен-план", task, "--json")
        return code, parse_json(out), out + err

    def rows(self, journal: str = NEST_JOURNAL) -> list:
        path = self.logs / journal
        if not path.exists():
            return []
        found = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
        return found


def parse_json(out: str) -> dict:
    for line in reversed(out.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def part(*files: str, deps: list | None = None) -> dict:
    return {"файлы": list(files), "вид": "код", "зависит_от": deps or []}


def get(tree, *keys):
    for key in keys:
        if isinstance(tree, dict):
            tree = tree.get(key)
        elif isinstance(tree, list) and isinstance(key, int) and 0 <= key < len(tree):
            tree = tree[key]
        else:
            return None
    return tree


def plain_parts(stand: Stand, prefix: str, count: int, price: str = "средняя") -> list:
    """count частей по одному файлу. Цена: простая 0, средняя 4 (код продукта,
    новый файл), высокая 7 (+ сторож); файлов меньше 5 — ширина не добавляет."""
    parts = []
    for i in range(1, count + 1):
        if i == 1 and price in ("средняя", "высокая"):
            parts.append(part(stand.file(f"prod/{prefix}{i}.py", exists=False)))
        elif i == 2 and price == "высокая":
            parts.append(part(stand.file(f"guard/{prefix}{i}.py")))
        else:
            parts.append(part(stand.file(f"src/{prefix}{i}.py")))
    return parts


def load(stand: Stand, tasks: int = 3, age_min: float = 1) -> None:
    """Нагрузка: tasks меченых запусков смены на tasks разных задач (боевой формат)."""
    for i in range(1, tasks + 1):
        stand.launch_row(f"Часть: чужая-{i}#1", age_min=age_min)


# ═══════════════════════════════ случаи ═══════════════════════════════════

def case_trifle(tmp: Path) -> None:
    print("мелочь: 1 файл, порог из данных")
    s = Stand(tmp / "мелочь")
    s.card("T")
    s.pretask("T", files=[s.file("src/a.py")])
    s.write()
    code, d, _ = s.decide("T")
    check("мелочь: код 0", 0, code)
    check("мелочь: правило", "мелочь", d.get("правило"))
    check("мелочь: роль", WORKER, d.get("роль"))
    check("мелочь: исполнителей", 1, d.get("исполнителей"))
    check("мелочь: исход §5", "мелкая", d.get("исход_плана"))
    plan_rows = [r for r in s.rows() if r.get("event") == "nesting.plan" and r.get("task") == "T"]
    check("мелочь: в журнале строка skipped (контроль молчит)",
          ["skipped"], [r.get("outcome") for r in plan_rows])

    # Меняется ТОЛЬКО ключ порога, правила те же (ревью Н-4).
    s2 = Stand(tmp / "мелочь-0", roles={"порог_мелочи.файлов": 0})
    s2.card("T")
    s2.pretask("T", files=[s2.file("src/a.py")])
    s2.write()
    code, d, _ = s2.decide("T")
    check("мелочь: копия yaml, где сменён только порог_мелочи.файлов 0, меняет правило",
          "простая", d.get("правило"))

    s3 = Stand(tmp / "параллель-от-4", roles={"параллель_от_групп": 4})
    s3.task_in_plan("T", plain_parts(s3, "p", 3))
    s3.write()
    code, d, _ = s3.decide("T")
    check("параллель_от_групп 4 (только ключ): 3 группы — не lead, правило две",
          ("две", WORKER), (d.get("правило"), d.get("роль")))


def case_parallel(tmp: Path) -> None:
    print("параллель: группы, а не части")
    s = Stand(tmp / "параллель")
    s.task_in_plan("T", plain_parts(s, "p", 3))
    s.write()
    code, d, _ = s.decide("T")
    check("параллель: 3 независимые части — групп 3", 3, get(d, "признаки", "групп"))
    check("параллель: роль lead", LEAD, d.get("роль"))
    check("параллель: правило", "параллель", d.get("правило"))
    check("параллель: исполнителей 3 = min(нужно, ёмкость)", 3, d.get("исполнителей"))

    s = Stand(tmp / "параллель-общий")
    shared = s.file("src/shared.py")
    s.task_in_plan("T", [part(s.file("prod/p1.py", exists=False), shared),
                         part(shared, deps=[1]),
                         part(s.file("src/p3.py"))])
    s.write()
    code, d, _ = s.decide("T")
    check("параллель: части 1 и 2 с общим файлом и зависимостью — групп 2",
          2, get(d, "признаки", "групп"))
    check("параллель: при 2 группах не lead", WORKER, d.get("роль"))
    check("параллель: 2 группы — правило две, 2 исполнителя",
          ("две", 2), (d.get("правило"), d.get("исполнителей")))


def case_chain(tmp: Path) -> None:
    print("зависит_от: цепочка 1→2→3")
    s = Stand(tmp / "цепочка")
    s.task_in_plan("T", [part(s.file("prod/c1.py", exists=False)),
                         part(s.file("src/c2.py"), deps=[1]),
                         part(s.file("src/c3.py"), deps=[2])])
    s.write()
    code, d, _ = s.decide("T")
    check("цепочка: групп 1", 1, get(d, "признаки", "групп"))
    check("цепочка: волн 3", 3, d.get("волн"))
    check("цепочка: волны частей 1, 2, 3", [1, 2, 3],
          [p.get("волна") for p in d.get("части") or []])
    check("цепочка: исполнителей 1", 1, d.get("исполнителей"))


def case_large(tmp: Path) -> None:
    print("крупная: уровень из глубина.уровень, шаги суженные характером")
    for name, level, character, steps, role in (
            ("крупная", "крупная", "разработка", 7, LEAD),
            ("обычная", "обычная", "разработка", 2, WORKER),
            ("починка", "крупная", "починка", 2, WORKER)):
        s = Stand(tmp / f"крупная-{name}")
        s.task_in_plan("T", [part(s.file("prod/k.py", exists=False), s.file("guard/g.py"))],
                       level=level, character=character)
        s.write()
        code, d, _ = s.decide("T")
        check(f"крупная/{name}: сложность высокая (цена 7)", ("высокая", 7),
              (get(d, "признаки", "сложность"), get(d, "признаки", "цена")))
        check(f"крупная/{name}: шагов {steps}", steps, get(d, "признаки", "шагов"))
        check(f"крупная/{name}: роль {role}", role, d.get("роль"))
        if name == "крупная":
            check("крупная: правило крупная", "крупная", d.get("правило"))


def case_no_overlap(tmp: Path) -> None:
    print("непересечение: realpath, цикл, нечего делить")
    s = Stand(tmp / "пересечение")
    absolute = s.file("src/a.py")
    s.task_in_plan("T", [part("./src/a.py"), part(absolute)])
    s.write()
    code, _, text = s.decide("T")
    check("пересечение: ./src/a.py и абсолютный путь в независимых частях — код 1", 1, code)
    check("пересечение: перечень называет файл", True, "a.py" in text)

    s = Stand(tmp / "пересечение-ссылка")
    target = s.file("src/a.py")
    link = s.root / "src" / "link.py"
    link.symlink_to(target)
    s.task_in_plan("T", [part(str(link)), part(target)])
    s.write()
    check("пересечение: симлинк на тот же файл — код 1", 1, s.decide("T")[0])

    s = Stand(tmp / "цикл")
    s.task_in_plan("T", [part(s.file("src/a.py"), deps=[2]), part(s.file("src/b.py"), deps=[1])])
    s.write()
    code, _, text = s.decide("T")
    check("цикл зависит_от — код 1", 1, code)
    check("цикл назван словом «цикл»", True, "цикл" in text)

    s = Stand(tmp / "без-файлов")
    s.task_in_plan("T", [{"файлы": [], "вид": "код", "зависит_от": []}])
    s.write()
    code, _, text = s.decide("T")
    check("план без файлов — код 1", 1, code)
    check("план без файлов — «нечего делить»", True, "нечего делить" in text)

    s = Stand(tmp / "пересечение-с-зависимостью")
    shared = s.file("src/a.py")
    s.task_in_plan("T", [part(shared), part(shared, deps=[1])])
    s.write()
    check("общий файл у ЗАВИСИМЫХ частей — не нарушение, код 0", 0, s.decide("T")[0])


def case_plan_part(tmp: Path) -> None:
    print("часть_плана и нужен_план")
    s = Stand(tmp / "часть-плана")
    x, y = s.file("src/x.py"), s.file("src/y.py")
    s.task_in_plan("T", [part(x), part(y)], level="крупная")
    s.write()
    code, d, _ = s.need_plan("T")
    check("часть_плана: код 0", 0, code)
    check("часть_плана: исход 1", "часть_плана", d.get("исход"))
    check("часть_плана: глава названа", "P/c", d.get("глава"))
    code, d, _ = s.decide("T")
    check("часть_плана: части берутся из главы", [[os.path.realpath(x)], [os.path.realpath(y)]],
          [p.get("файлы") for p in d.get("части") or []])
    check("часть_плана: метка части", "Часть: P/c/T#1", get(d, "части", 0, "метка"))

    s = Stand(tmp / "без-главы")
    s.plan("P", parts={"T": [part(s.file("src/x.py")), part(s.file("src/y.py"))]})
    s.card("T", level="крупная")
    s.write()
    code, d, _ = s.need_plan("T")
    check("без поля глава: исход 4 нужен_план", "нужен_план", d.get("исход"))

    s = Stand(tmp / "нужен-план-группы")
    s.card("T")
    s.pretask("T", parts=[part(s.file("src/x.py")), part(s.file("src/y.py"))])
    s.write()
    check("нужен_план по группам ≥ 2 (части pretask)", "нужен_план", s.need_plan("T")[1].get("исход"))

    s = Stand(tmp / "шагов-из-данных", roles={"план_нужен.шагов": 2})
    s.card("T")
    s.pretask("T", files=[s.file("src/x.py")])
    s.write()
    check("порог шагов плана из данных: шагов 2 → нужен_план", "нужен_план",
          s.need_plan("T")[1].get("исход"))

    s = Stand(tmp / "мелкая-без-файлов")
    s.card("T")
    s.write()
    code, d, _ = s.need_plan("T")
    check("файлов нет: исход мелкая", "мелкая", d.get("исход"))
    check("файлов нет: след о пропуске групп", True,
          any("групп" in str(line) for line in d.get("след") or []))
    check("метка без главы: «Часть: T#1»", "Часть: T#1",
          get(_pretask_decision(tmp / "метка-без-главы"), "части", 0, "метка"))


def _pretask_decision(base: Path) -> dict:
    s = Stand(base)
    s.card("T")
    s.pretask("T", files=[s.file("src/a.py")])
    s.write()
    return s.decide("T")[1]


def case_orphan(tmp: Path) -> None:
    print("сирота: глава или план не найдены")
    s = Stand(tmp / "сирота")
    s.plan("P")
    s.card("T", chapter="P/нет")
    s.card("U", chapter="Q/c")
    s.write()
    code, d, text = s.need_plan("T")
    check("сирота: несуществующая глава — код 1", 1, code)
    check("сирота: «не найдена» в выводе", True, "не найдена" in text)
    check("сирота: план не существует (переименован) — код 1", 1, s.need_plan("U")[0])


def case_candidate(tmp: Path) -> None:
    print("кандидат: пересечение с охватом по realpath")

    def stand(name: str, owner_status: str = "plan", roles: dict | None = None) -> Stand:
        s = Stand(tmp / f"кандидат-{name}", roles=roles)
        a, b = s.file("lib1/a.py"), s.file("lib1/b.py")
        s.plan("P", chapters=[{"id": "c", "название": "Глава c", "охват": [a, b]}],
               owner_status=owner_status)
        link = s.root / "links" / "a_link.py"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(a)
        s.card("T")
        s.pretask("T", files=[str(link), str(s.root / "lib1" / ".." / "lib1" / "b.py"),
                              s.file("lib2/z.py")])
        s.card("U")
        s.pretask("U", files=[s.file("lib2/a.py"), s.file("lib2/b.py"), s.file("lib2/z.py")])
        s.write()
        return s

    s = stand("открытый")
    code, d, _ = s.need_plan("T")
    check("кандидат: 2 из 3 файлов в охвате (симлинк, ..) — исход кандидат",
          "кандидат", d.get("исход"))
    check("кандидат: подсказка называет главу", ["P/c"], d.get("подсказка"))
    check("кандидат: тот же базовый файл в другом каталоге — не кандидат",
          "мелкая", s.need_plan("U")[1].get("исход"))
    check("кандидат: план с закрытым владельцем не подсказывается", "мелкая",
          stand("закрытый", owner_status="done").need_plan("T")[1].get("исход"))
    check("кандидат: порог_приживления из данных (0.9) — не кандидат", "мелкая",
          stand("порог", roles={"порог_приживления": 0.9}).need_plan("T")[1].get("исход"))

    s = Stand(tmp / "кандидат-ничья")
    a, b, e, f = (s.file(p) for p in ("lib1/a.py", "lib1/b.py", "lib3/e.py", "lib3/f.py"))
    s.plan("P", chapters=[{"id": "c", "название": "c", "охват": [a, b]},
                          {"id": "d", "название": "d", "охват": [e, f]}])
    s.card("T")
    s.pretask("T", files=[a, b, e, f])
    s.write()
    check("кандидат: ничья — показаны обе главы", ["P/c", "P/d"],
          sorted(s.need_plan("T")[1].get("подсказка") or []))


def case_nesting_depth(tmp: Path) -> None:
    print("вложенность глав: верх из данных, правила §4.3")
    deep = [{"id": "a", "название": "a", "охват": [], "главы": [
        {"id": "b", "название": "b", "охват": [], "главы": [
            {"id": "c", "название": "c", "охват": [], "главы": [
                {"id": "d", "название": "d", "охват": []}]}]}]}]
    for name, top, chapters, expected in (
            ("4-при-3", 3, deep, 1),
            ("4-при-4", 4, deep, 0),
            ("3-при-3", 3, [{"id": "a", "название": "a", "охват": [], "главы": [
                {"id": "b", "название": "b", "охват": [], "главы": [
                    {"id": "c", "название": "c", "охват": []}]}]}], 0)):
        s = Stand(tmp / f"глубина-{name}", roles={"верх_вложенности": top})
        s.plan("P", chapters=copy.deepcopy(chapters))
        s.write()
        code, out, err = s.run("--проверить-план", "P")
        check(f"вложенность {name}: код {expected}", expected, code)

    s = Stand(tmp / "дубль-главы")
    s.plan("P", chapters=[{"id": "a", "название": "a", "охват": []},
                          {"id": "a", "название": "a2", "охват": []}])
    s.write()
    check("§4.3.2: дубль id главы у одного родителя — код 1", 1,
          s.run("--проверить-план", "P")[0])

    s = Stand(tmp / "владелец-в-главе")
    s.plan("P", owner="O")
    s.card("O", status="plan", chapter="P/c")
    s.write()
    check("§4.3.4: владелец плана с главой в своём же плане — код 1", 1,
          s.run("--проверить-план", "P")[0])


def case_attach(tmp: Path) -> None:
    print("--прижить: вставка строки глава, карта цела")
    s = Stand(tmp / "прижить")
    s.plan("P")
    s.card("T")
    s.card("V")
    s.write()
    before = s.devmap.read_text(encoding="utf-8")
    cards_before = len(map_cards(s.devmap))
    code, out, err = s.run("--прижить", "T", "P/c")
    after = s.devmap.read_text(encoding="utf-8")
    check("прижить: код 0", 0, code)
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    check("прижить: добавлена ровно одна строка «глава: P/c»", ["глава: P/c"],
          [line.strip() for line in added])
    check("прижить: строк стало на одну больше", len(before.splitlines()) + 1,
          len(after.splitlines()))
    check("прижить: число карточек то же", cards_before, len(map_cards(s.devmap)))
    check("прижить: комментарии карты сохранились (не yaml.dump)",
          comment_lines(before), comment_lines(after))
    check("прижить: карточка T несёт глава P/c", "P/c", map_cards(s.devmap)["T"].get("глава"))
    check("прижить: соседняя карточка без главы", None, map_cards(s.devmap)["V"].get("глава"))
    chapter_rows = [r for r in s.rows() if r.get("event") == "nesting.chapter"]
    check("прижить: событие nesting.chapter с источником «смена»",
          [("T", "P/c", "смена")],
          [(get(r, "data", "задача"), get(r, "data", "глава"), get(r, "data", "источник"))
           for r in chapter_rows])
    check("прижить: повтор на задачу в главе — код 1", 1, s.run("--прижить", "T", "P/c")[0])
    check("прижить: несуществующая глава без --новая-глава — код 1", 1,
          s.run("--прижить", "V", "P/нет")[0])
    check("прижить: отказы не тронули карту", after, s.devmap.read_text(encoding="utf-8"))


def case_new_chapter(tmp: Path) -> None:
    print("--новая-глава: глава в блоке план:, комментарии плана целы")
    s = Stand(tmp / "новая-глава")
    s.plan("P")
    s.card("T")
    s.write()
    plan_file = s.plans_dir / "P.md"
    before = plan_file.read_text(encoding="utf-8")
    map_before = s.devmap.read_text(encoding="utf-8")
    code, out, err = s.run("--прижить", "T", "P/new", "--новая-глава", "Новая глава")
    after = plan_file.read_text(encoding="utf-8")
    block = plan_block(after) or {}
    check("новая глава: код 0", 0, code)
    check("новая глава: глава в блоке план:",
          [("c", "Глава c"), ("new", "Новая глава")],
          [(g.get("id"), g.get("название")) for g in get(block, "план", "главы") or []])
    check("новая глава: комментарии плана целы", comment_lines(before), comment_lines(after))
    check("новая глава: текст после блока цел", True, PLAN_TAIL in after)
    check("новая глава: карточка T несёт глава P/new", "P/new",
          map_cards(s.devmap)["T"].get("глава"))
    check("новая глава: число карточек то же", len(yaml.safe_load(map_before)["lines"][0]["tasks"]),
          len(map_cards(s.devmap)))


def case_pin(tmp: Path) -> None:
    print("--закрепить: глава всем задачам из части: глав")
    s = Stand(tmp / "закрепить")
    s.plan("P", chapters=[
        {"id": "data", "название": "Данные", "охват": [],
         "части": {"T1": [part(s.file("src/d.py"))]},
         "главы": [{"id": "groups", "название": "Группы", "охват": [],
                    "части": {"T3": [part(s.file("src/g.py"))]}}]},
        {"id": "hook", "название": "Хук", "охват": [],
         "части": {"T2": [part(s.file("src/h.py"))]}}])
    for task in ("T1", "T2", "T3"):
        s.card(task)
    s.write()
    before = s.devmap.read_text(encoding="utf-8")
    code, out, err = s.run("--закрепить", "P")
    cards = map_cards(s.devmap)
    check("закрепить: код 0", 0, code)
    check("закрепить: главы по месту части", {"T1": "P/data", "T2": "P/hook",
                                              "T3": "P/data/groups"},
          {t: (cards.get(t) or {}).get("глава") for t in ("T1", "T2", "T3")})
    check("закрепить: число карточек то же", len(yaml.safe_load(before)["lines"][0]["tasks"]),
          len(cards))
    check("закрепить: комментарии карты целы", comment_lines(before),
          comment_lines(s.devmap.read_text(encoding="utf-8")))

    s = Stand(tmp / "закрепить-две-главы")
    s.plan("P", chapters=[
        {"id": "a", "название": "a", "охват": [], "части": {"T": [part(s.file("src/a.py"))]}},
        {"id": "b", "название": "b", "охват": [], "части": {"T": [part(s.file("src/b.py"))]}}])
    s.card("T")
    s.write()
    before = s.devmap.read_text(encoding="utf-8")
    check("§4.3.3: задача в двух главах — код 1", 1, s.run("--закрепить", "P")[0])
    check("§4.3.3: карта не тронута", before, s.devmap.read_text(encoding="utf-8"))


def case_simple(tmp: Path) -> None:
    print("простая: сложность раньше групп; пороги цены из pipeline.yaml")
    s = Stand(tmp / "простая")
    s.task_in_plan("T", plain_parts(s, "s", 4, price="простая"))
    s.write()
    code, d, _ = s.decide("T")
    check("простая: цена 0 — сложность простая", ("простая", 0),
          (get(d, "признаки", "сложность"), get(d, "признаки", "цена")))
    check("простая: 4 группы, ёмкость 5 — 1 исполнитель, правило простая",
          ("простая", 1), (d.get("правило"), d.get("исполнителей")))

    s = Stand(tmp / "средняя-4")
    s.task_in_plan("T", plain_parts(s, "s", 4))
    s.write()
    code, d, _ = s.decide("T")
    check("средняя: цена 4 ≥ П1 — 4 исполнителя, параллель, lead",
          (4, "параллель", LEAD), (d.get("исполнителей"), d.get("правило"), d.get("роль")))

    s = Stand(tmp / "П1-из-данных", pipeline={"пороги.П1": 5})
    s.task_in_plan("T", plain_parts(s, "s", 4))
    s.write()
    check("П1 из pipeline.yaml (5): цена 4 — простая, 1 исполнитель", 1,
          s.decide("T")[1].get("исполнителей"))


def case_estimate(tmp: Path) -> None:
    print("сложность по оценке и по цене")
    s = Stand(tmp / "оценка")
    s.card("T", complexity=4)
    s.write()
    code, d, _ = s.decide("T")
    check("оценка: файлов нет, complexity 4 — высокая", "высокая", get(d, "признаки", "сложность"))
    check("оценка: источник «оценка»", "оценка", get(d, "признаки", "источник_сложности"))
    code, out, err = s.run("T")
    check("оценка: в тексте след «по оценке»", True, "по оценке" in out)
    s.card("U", complexity=2)
    s.write()
    check("оценка: complexity 2 — простая", "простая",
          get(s.decide("U")[1], "признаки", "сложность"))

    s = Stand(tmp / "цена-перекрывает")
    s.task_in_plan("T", [part(s.file("src/new.py", exists=False))], complexity=4)
    s.write()
    code, d, _ = s.decide("T")
    check("цена перекрывает оценку: цена 1, complexity 4 — простая", ("простая", "цена", 1),
          (get(d, "признаки", "сложность"), get(d, "признаки", "источник_сложности"),
           get(d, "признаки", "цена")))

    s = Stand(tmp / "крупная-поднимает")
    s.task_in_plan("T", [part(s.file("src/new.py", exists=False))], level="крупная")
    s.write()
    check("уровень крупная поднимает простую до средней", "средняя",
          get(s.decide("T")[1], "признаки", "сложность"))


def case_two_launches(tmp: Path) -> None:
    print("ёмкость: два запуска делят общий верх")
    s = Stand(tmp / "два-запуска")
    s.task_in_plan("A", plain_parts(s, "a", 4))
    s.task_in_plan("B", plain_parts(s, "b", 4))
    s.write()
    code, d, _ = s.decide("A")
    check("два запуска: первая задача — 4 исполнителя", 4, d.get("исполнителей"))
    # Боевой формат: task «-», задача только в data.часть (M15b).
    for n in range(1, 5):
        s.launch_row(f"Часть: P/c/A#{n}")
    code, d, _ = s.decide("B")
    check("два запуска: вторая задача — 1 исполнитель", 1, d.get("исполнителей"))
    check("два запуска: живых 4, свободно 1", (4, 1),
          (get(d, "ёмкость", "живых"), get(d, "ёмкость", "свободно")))
    check("два запуска: задач в работе 1 (A)", 1, get(d, "ёмкость", "задач_в_работе"))
    check("два запуска: task «-» и data.часть «Часть: P/c/A#n» — задача A (M15b)",
          ["A"], get(d, "ёмкость", "задачи"))


def case_load(tmp: Path) -> None:
    print("нагрузка: порог из данных")
    s = Stand(tmp / "нагрузка")
    s.task_in_plan("T", plain_parts(s, "n", 4))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("нагрузка: 3 задачи в работе — правило нагрузка, 1 исполнитель",
          ("нагрузка", 1), (d.get("правило"), d.get("исполнителей")))
    check("нагрузка: ёмкость живых 3, свободно 2, задач 3", (3, 2, 3),
          (get(d, "ёмкость", "живых"), get(d, "ёмкость", "свободно"),
           get(d, "ёмкость", "задач_в_работе")))
    code, out, err = s.run("T")
    line = next((x for x in out.splitlines() if x.startswith("ёмкость:")), "")
    check("строка ёмкости: «ёмкость: свободно 2 из 5»", True, "ёмкость: свободно 2 из 5" in line)
    check("строка ёмкости: «живых 3»", True, "живых 3" in line)
    check("строка ёмкости: «задач в работе 3»", True, "задач в работе 3" in line)
    check("строка ёмкости: «решение:»", True, "решение:" in line)
    check("строка ёмкости в JSON = строке текста", line, d.get("строка_ёмкости"))

    s = Stand(tmp / "нагрузка-4", roles={"предохранитель.порог_нагрузки": 4})
    s.task_in_plan("T", plain_parts(s, "n", 4))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("нагрузка: копия yaml с порогом 4 — дробит (две, 2)", ("две", 2),
          (d.get("правило"), d.get("исполнителей")))


def case_cut_under_load(tmp: Path) -> None:
    print("резка сложной под нагрузкой")
    s = Stand(tmp / "резка-4", roles={"предохранитель.жёсткий_верх": 6})
    s.task_in_plan("T", plain_parts(s, "r", 4, price="высокая"))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("резка: цена 7 ≥ 6, 4 группы, свободно 3 — сложность_режет", (7, True),
          (get(d, "признаки", "цена"), get(d, "признаки", "сложность_режет")))
    check("резка: правило резка_под_нагрузкой_лид, lead, 3 исполнителя",
          ("резка_под_нагрузкой_лид", LEAD, 3),
          (d.get("правило"), d.get("роль"), d.get("исполнителей")))
    check("резка: строка ёмкости называет порог резки", True,
          "порог резки 6" in (d.get("строка_ёмкости") or ""))

    s = Stand(tmp / "резка-2")
    s.task_in_plan("T", plain_parts(s, "r", 2, price="высокая"))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("резка: 2 группы, свободно 2 — резка_под_нагрузкой, 2 исполнителя",
          ("резка_под_нагрузкой", WORKER, 2),
          (d.get("правило"), d.get("роль"), d.get("исполнителей")))


def case_simple_under_load(tmp: Path) -> None:
    print("не сложная под нагрузкой — один")
    for name, price, rule in (("средняя", "средняя", "нагрузка"),
                              ("простая", "простая", "простая")):
        s = Stand(tmp / f"под-нагрузкой-{name}", roles={"предохранитель.жёсткий_верх": 6})
        s.task_in_plan("T", plain_parts(s, "u", 4, price=price))
        load(s)
        s.write()
        code, d, _ = s.decide("T")
        check(f"под нагрузкой {name}: 4 группы — 1 исполнитель, правило {rule}",
              (rule, 1), (d.get("правило"), d.get("исполнителей")))
        check(f"под нагрузкой {name}: сложность_режет нет", False,
              get(d, "признаки", "сложность_режет"))


def case_cut_nothing(tmp: Path) -> None:
    print("резка: нечем")
    s = Stand(tmp / "нечем-1", roles={"предохранитель.жёсткий_верх": 4})
    s.task_in_plan("T", plain_parts(s, "z", 4, price="высокая"))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("нечем: сложная, свободно 1 — 1 исполнитель", (1, False),
          (d.get("исполнителей"), d.get("ждать")))
    s = Stand(tmp / "нечем-0", roles={"предохранитель.жёсткий_верх": 3})
    s.task_in_plan("T", plain_parts(s, "z", 4, price="высокая"))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("нечем: свободно 0 — исполнителей 0, ждать", (0, True),
          (d.get("исполнителей"), d.get("ждать")))
    code, out, err = s.run("T")
    check("нечем: текст говорит «ждать»", True, "ждать" in out)


def case_cut_threshold_data(tmp: Path) -> None:
    print("порог резки данными")
    s = Stand(tmp / "порог-8", roles={"предохранитель.жёсткий_верх": 6,
                                      "порог_резки_сложности.цена": 8})
    s.task_in_plan("T", plain_parts(s, "q", 4, price="высокая"))
    load(s)
    s.write()
    check("порог цена 8: цена 7 под нагрузкой — 1", 1, s.decide("T")[1].get("исполнителей"))
    s = Stand(tmp / "порог-3", roles={"предохранитель.жёсткий_верх": 6,
                                      "порог_резки_сложности.цена": 3})
    s.task_in_plan("T", plain_parts(s, "q", 4))
    load(s)
    s.write()
    code, d, _ = s.decide("T")
    check("порог цена 3: цена 4 под нагрузкой — min(4, 3) = 3", (3, "резка_под_нагрузкой_лид"),
          (d.get("исполнителей"), d.get("правило")))
    s = Stand(tmp / "порог-оценка", roles={"предохранитель.жёсткий_верх": 6})
    s.card("T", complexity=4)
    s.pretask("T", parts=None)
    load(s)
    s.write()
    check("порог по оценке: файлов нет, complexity 4 — сложность_режет", True,
          get(s.decide("T")[1], "признаки", "сложность_режет"))


def case_ceiling(tmp: Path) -> None:
    print("потолок: жёсткий_верх из данных, окно живых")

    def stand(name: str, fresh: int, roles: dict | None = None) -> Stand:
        s = Stand(tmp / f"потолок-{name}", roles=roles)
        s.task_in_plan("T", plain_parts(s, "w", 7))
        for i in range(1, fresh + 1):
            s.claim(f"h{i}", "общая")
        s.claim("старый", "давняя", age_min=60)
        s.write()
        return s

    code, d, _ = stand("4", 4).decide("T")
    check("потолок: 7 групп, 4 живых (старая заявка не в счёт) — 1", (1, 4),
          (d.get("исполнителей"), get(d, "ёмкость", "живых")))
    code, d, _ = stand("5", 5).decide("T")
    check("потолок: 5 живых — 0, ждать", (0, True), (d.get("исполнителей"), d.get("ждать")))
    code, d, _ = stand("8", 4, roles={"предохранитель.жёсткий_верх": 8}).decide("T")
    check("потолок: жёсткий_верх 8, 4 живых — 4", 4, d.get("исполнителей"))
    code, d, _ = stand("окно", 4, roles={"предохранитель.окно_живых": 90}).decide("T")
    check("потолок: окно_живых 90 из данных — старая заявка живая, 5 живых", 5,
          get(d, "ёмкость", "живых"))


def case_live_sources(tmp: Path) -> None:
    print("живые по боевым журналам: заявки, роли, запуски без активности")

    def stand(name: str) -> Stand:
        s = Stand(tmp / f"источники-{name}")
        s.task_in_plan("T", plain_parts(s, "v", 1))
        return s

    s = stand("заявки-роли")
    for agent in ("a", "b", "c"):
        s.claim(agent, "-")
    for agent in ("a", "b", "c", "d"):
        s.role_row(agent)
    s.write()
    code, d, _ = s.decide("T")
    check("источники: заявки {a,b,c}, роли {a,b,c,d} — живых 4", 4, get(d, "ёмкость", "живых"))
    check("источники: разбор заявки 3, роли 4", (3, 4),
          (get(d, "ёмкость", "по_источникам", "заявки"),
           get(d, "ёмкость", "по_источникам", "роли")))
    code, out, err = s.run("T")
    check("источники: текст называет расхождение «заявок 3» и «ролей 4»", True,
          "заявок 3" in out and "ролей 4" in out)

    s = stand("объединение")
    s.claim("a", "-")
    s.claim("b", "-")
    s.role_row("c")
    s.role_row("d")
    s.write()
    check("источники: заявки {a,b} и роли {c,d} — объединение по ид 4", 4,
          get(s.decide("T")[1], "ёмкость", "живых"))

    s = stand("смена")
    s.role_row("-", role="-")
    s.role_row("a")
    s.write()
    check("источники: строка роли смены (агент «-») — не помощник, живых 1", 1,
          get(s.decide("T")[1], "ёмкость", "живых"))

    s = stand("вызовы")
    for n in range(1, 4):
        s.launch_row(f"Часть: t1#{n}")
    s.launch_row("Часть: t1#4", age_min=45)
    s.write()
    code, d, _ = s.decide("T")
    check("источники: 3 свежих запуска без активности (старый вне окна) — живых 3", 3,
          get(d, "ёмкость", "живых"))
    check("источники: вызовы 3 и без_активности 3 в разборе", (3, 3),
          (get(d, "ёмкость", "по_источникам", "вызовы"),
           get(d, "ёмкость", "по_источникам", "без_активности")))

    s = stand("пара")
    for n in range(1, 5):
        s.launch_row(f"Часть: t1#{n}", age_min=3)
    for agent in ("k1", "k2", "k3", "k4"):
        s.role_row(agent, age_min=1)
    s.write()
    code, d, _ = s.decide("T")
    check("источники: 4 запуска и 4 ребёнка, начавших после них — живых 4, не 8", 4,
          get(d, "ёмкость", "живых"))
    check("источники: без_активности 0 — запуск и его ребёнок один живой", 0,
          get(d, "ёмкость", "по_источникам", "без_активности"))

    s = stand("старые-и-новый")
    for agent in ("o1", "o2", "o3"):
        s.role_row(agent, age_min=50)
        s.role_row(agent, age_min=1)
    s.launch_row("Часть: t2#1", age_min=2)
    s.write()
    check("источники: 3 давно работающих и 1 свежий запуск — живых 4", 4,
          get(s.decide("T")[1], "ёмкость", "живых"))

    s = stand("кончился")
    s.role_row("x", age_min=45)
    s.claim("x", "-", age_min=45)
    s.write()
    check("источники: помощник без активности за окно — не живой", 0,
          get(s.decide("T")[1], "ёмкость", "живых"))

    s = stand("skipped")
    s.launch_row(outcome="skipped")
    s.write()
    check("источники: запуск skipped (хук бюджета не судил, запуск был) — живой", 1,
          get(s.decide("T")[1], "ёмкость", "живых"))


def case_battle_tasks(tmp: Path) -> None:
    print("задачи в работе на образце боевого формата")

    def stand(name: str) -> Stand:
        s = Stand(tmp / f"бой-{name}")
        s.task_in_plan("T", plain_parts(s, "t", 1))
        return s

    def lead_children(s: Stand, marks: list, own_mark: str | None = "Часть: P/c/A") -> None:
        """Смена запустила управляющего L (6 мин назад), L читал (5 мин) и
        запустил детей (1 мин): у каждого запуска — строка роли L c Agent."""
        if own_mark is not False:
            s.launch_row(own_mark, age_min=6)
        s.role_row("L", role=LEAD, age_min=5)
        for mark in marks:
            s.role_row("L", role=LEAD, tool="Agent", age_min=1)
            s.launch_row(mark, lead="L", age_min=1)

    s = stand("меченые")
    lead_children(s, [f"Часть: P/c/A#{n}" for n in range(1, 6)])
    s.write()
    code, d, _ = s.decide("T")
    check("бой: 5 запусков управляющего task «-», метка одной задачи — задач_в_работе 1",
          1, get(d, "ёмкость", "задач_в_работе"))
    check("бой: задача — A из метки", ["A"], get(d, "ёмкость", "задачи"))
    check("бой: живых 6 (управляющий и 5 детей, ещё не действовавших)", 6,
          get(d, "ёмкость", "живых"))

    s = stand("анонимные")
    lead_children(s, [None] * 5, own_mark=False)
    s.write()
    code, d, _ = s.decide("T")
    check("бой: 5 анонимных детей одного управляющего — одна задача, не 5",
          ["управляющий:L"], get(d, "ёмкость", "задачи"))

    s = stand("смешанные")
    lead_children(s, ["Часть: P/c/A#1", None, None, None, None])
    s.write()
    code, d, _ = s.decide("T")
    check("бой: анонимные дети L берут задачу меченого запуска L — задач 1 (A)",
          ["A"], get(d, "ёмкость", "задачи"))

    s = stand("смена-анонимно")
    s.launch_row()
    s.launch_row()
    s.write()
    check("бой: два анонимных запуска смены — две задачи", 2,
          get(s.decide("T")[1], "ёмкость", "задач_в_работе"))

    s = stand("трейлер")
    for agent in ("r1", "r2", "r3"):
        s.role_row(agent)
    s.write()
    code, d, _ = s.decide("T")
    check("бой: task строк ролей (трейлер истории) задачей не считается — задач 0, живых 3",
          (0, 3), (get(d, "ёмкость", "задач_в_работе"), get(d, "ёмкость", "живых")))


def case_waiting_part(tmp: Path) -> None:
    print("часть с занятым файлом ждёт")
    s = Stand(tmp / "ждёт")
    parts = plain_parts(s, "j", 4)
    s.task_in_plan("T", parts)
    s.claim("чужой", "чужая", path=parts[1]["файлы"][0])
    s.write()
    code, d, _ = s.decide("T")
    check("ждёт: часть 2 помечена «ждёт»", [False, True, False, False],
          [p.get("ждёт") for p in d.get("части") or []])
    check("ждёт: нужно 3, ждут 1", (3, 1), (get(d, "ёмкость", "нужно"), get(d, "ёмкость", "ждут")))
    check("ждёт: исполнителей 3 (свободно 4)", 3, d.get("исполнителей"))


def case_waiting_by_need(tmp: Path) -> None:
    print("правило по «нужно»: группа, что ждёт, не делит работу (Н-16)")
    s = Stand(tmp / "по-нужно")
    parts = plain_parts(s, "q", 2)
    s.task_in_plan("T", parts)
    s.claim("чужой", "чужая", path=parts[1]["файлы"][0])
    s.write()
    code, d, _ = s.decide("T")
    check("по нужно: групп 2 в признаках, нужно 1", (2, 1),
          (get(d, "признаки", "групп"), get(d, "ёмкость", "нужно")))
    check("по нужно: 2 группы, одна ждёт — не «две», правило прочее, 1 исполнитель",
          ("прочее", 1), (d.get("правило"), d.get("исполнителей")))


def case_high_without_files(tmp: Path) -> None:
    print("высокая без файлов при свободной ёмкости не ждёт (Н-3)")
    s = Stand(tmp / "высокая-без-файлов")
    s.card("T", level="крупная", complexity=4)
    s.write()
    code, d, _ = s.decide("T")
    check("высокая без файлов: сложность высокая по оценке, свободно 5",
          ("высокая", "оценка", 5),
          (get(d, "признаки", "сложность"), get(d, "признаки", "источник_сложности"),
           get(d, "ёмкость", "свободно")))
    check("высокая без файлов: не ждать, 1 исполнитель (нужно 0 считается 1)", (False, 1),
          (d.get("ждать"), d.get("исполнителей")))
    check("высокая без файлов: строка ёмкости без «ждать»", False,
          "ждать" in (d.get("строка_ёмкости") or ""))
    check("без нагрузки строка ёмкости не говорит «порог резки»", False,
          "порог резки" in (d.get("строка_ёмкости") or ""))


def fill_capacity(stand: Stand, count: int = 5) -> None:
    """count живых помощников по заявкам: при жёсткий_верх 5 ёмкость 0, задач 0."""
    for i in range(1, count + 1):
        stand.claim(f"полный{i}", "-")


def case_trifle_never_waits(tmp: Path) -> None:
    print("мелочь не ждёт и при ёмкости 0; простая и прочее ждут (Н-10, слово 30.10)")
    s = Stand(tmp / "мелочь-ёмкость-0")
    s.card("T")
    s.pretask("T", files=[s.file("src/a.py")])
    fill_capacity(s)
    s.write()
    code, d, _ = s.decide("T")
    check("мелочь при ёмкости 0: стенд — свободно 0, живых 5", (0, 5),
          (get(d, "ёмкость", "свободно"), get(d, "ёмкость", "живых")))
    check("мелочь при ёмкости 0: правило мелочь, исполнителей 1, ждать false",
          ("мелочь", 1, False), (d.get("правило"), d.get("исполнителей"), d.get("ждать")))
    check("мелочь при ёмкости 0: строка ёмкости без «ждать»", False,
          "ждать" in (d.get("строка_ёмкости") or ""))

    for name, parts_of, rule in (
            ("простая", lambda st: plain_parts(st, "s", 4, price="простая"), "простая"),
            ("прочее", lambda st: plain_parts(st, "o", 1), "прочее")):
        s = Stand(tmp / f"{name}-ёмкость-0")
        s.task_in_plan("T", parts_of(s))
        fill_capacity(s)
        s.write()
        code, d, _ = s.decide("T")
        check(f"{name} при ёмкости 0 (контроль): правило {rule}, исполнителей 0, ждать",
              (rule, 0, True), (d.get("правило"), d.get("исполнителей"), d.get("ждать")))


def trace_line(lines, marker: str) -> str:
    return next((str(line) for line in lines or [] if marker in str(line)), "")


def tokens(text: str) -> set:
    return set(re.findall(r"[\w.-]+", text))


CLOSED_MARK = "план закрыт с открытыми задачами"


def case_closed_plan(tmp: Path) -> None:
    print("план закрыт с открытыми задачами — предупреждение с перечнем (§5.4)")

    def stand(name: str, owner_status: str) -> Stand:
        s = Stand(tmp / f"закрыт-{name}")
        s.plan("P", owner="хозяин-плана", owner_status=owner_status,
               chapters=[{"id": "c", "название": "c", "охват": []},
                         {"id": "d", "название": "d", "охват": []}])
        s.task_in_plan("open-a", plain_parts(s, "a", 2, price="простая"))
        s.card("open-b", status="plan", chapter="P/d")
        s.card("shut-c", status="done", chapter="P/c")
        s.write()
        return s

    s = stand("done", "done")
    code, d, _ = s.decide("open-a")
    line = trace_line(d.get("след"), CLOSED_MARK)
    check("закрыт: решение — код 0, в «след» строка «план закрыт с открытыми задачами»",
          (0, True), (code, bool(line)))
    check("закрыт: перечень — open-a и open-b (обе главы), закрытая shut-c не названа",
          (True, True, False),
          ("open-a" in tokens(line), "open-b" in tokens(line), "shut-c" in tokens(line)))
    code, out, err = s.run("open-a")
    check("закрыт: текст решения печатает «след: план закрыт с открытыми задачами»", True,
          f"след: {CLOSED_MARK}" in out)
    code, d, _ = s.need_plan("open-a")
    line = trace_line(d.get("след"), CLOSED_MARK)
    check("закрыт: --нужен-план — код 0, часть_плана, в «след» перечень open-a, open-b",
          (0, "часть_плана", True, True),
          (code, d.get("исход"), "open-a" in tokens(line), "open-b" in tokens(line)))

    s = stand("открыт", "wip")
    check("открыт (контроль): владелец не done — строки нет ни в решении, ни в --нужен-план",
          ("", ""), (trace_line(s.decide("open-a")[1].get("след"), CLOSED_MARK),
                     trace_line(s.need_plan("open-a")[1].get("след"), CLOSED_MARK)))


def case_deleted_chapter(tmp: Path) -> None:
    print("глава удалена из плана — код 1 с перечнем карточек (§5.4)")
    s = Stand(tmp / "глава-удалена")
    s.plan("P")
    s.card("card-ok", chapter="P/c")
    s.card("card-gone1", chapter="P/gone")
    s.card("card-gone2", chapter="P/gone/sub")
    s.card("card-alien", chapter="Q/gone")
    s.write()
    code, out, err = s.run("--проверить-план", "P")
    found = tokens(out + err)
    check("глава удалена: --проверить-план — код 1, слово «сирота»", (1, True),
          (code, "сирота" in out + err))
    check("глава удалена: перечень card-gone1, card-gone2; card-ok и чужой card-alien не названы",
          (True, True, False, False),
          ("card-gone1" in found, "card-gone2" in found, "card-ok" in found,
           "card-alien" in found))

    s = Stand(tmp / "глава-цела")
    s.plan("P")
    s.card("card-ok", chapter="P/c")
    s.card("card-alien", chapter="Q/gone")
    s.write()
    check("глава цела (контроль): ссылки только на живую главу и чужой план — код 0", 0,
          s.run("--проверить-план", "P")[0])


def case_renamed_plan(tmp: Path) -> None:
    print("план переименован — сирота у всех его задач, перечень (§5.4)")
    s = Stand(tmp / "переименован")
    s.plan("P2")
    s.card("ren-a", chapter="P/c")
    s.card("ren-b", chapter="P/c/sub")
    s.card("ren-new", chapter="P2/c")
    s.write()
    code, out, err = s.run("--проверить-план", "P")
    found = tokens(out + err)
    check("переименован: --проверить-план старого id — код 1, слово «сирота»", (1, True),
          (code, "сирота" in out + err))
    check("переименован: перечень ren-a и ren-b; ren-new (в новом плане) не назван",
          (True, True, False), ("ren-a" in found, "ren-b" in found, "ren-new" in found))
    check("переименован: --нужен-план ren-a — код 1", 1, s.need_plan("ren-a")[0])
    check("переименован: новый план P2 — код 0 (его ссылки целы)", 0,
          s.run("--проверить-план", "P2")[0])


def case_tasks_plan(tmp: Path) -> None:
    print("план в .tasks/<id>/план.md — механизм видит план")
    s = Stand(tmp / "план-в-tasks")
    s.card("N", level="крупная")
    s.pretask("N", files=[s.file("src/n1.py")])
    plan_md = s.root / ".tasks" / "N" / "план.md"
    plan_md.write_text("# План: N\n\nФаза 1: правка src/n1.py.\n", encoding="utf-8")
    s.write()
    code, d, _ = s.decide("N")
    check("план в .tasks: код 0", 0, code)
    check("план в .tasks: состояние не «нужен_план_нет»", True,
          d.get("состояние") not in ("нужен_план_нет", None))
    check("план в .tasks: решение.план — путь .tasks/N/план.md", True,
          str(d.get("план") or "").endswith("/.tasks/N/план.md"))


def case_map_lock(tmp: Path) -> None:
    print("--карта: лок рядом с этой картой, ожидание с пределом (Н-11)")
    import fcntl
    import time as _time
    s = Stand(tmp / "лок")
    s.plan("P")
    s.card("T")
    s.write()
    before = s.devmap.read_text(encoding="utf-8")
    lock = s.devmap.parent / "dev-map.lock"
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        started = _time.monotonic()
        code, out, err = s.run("--прижить", "T", "P/c", extra_env={"HARNESS_MAP_LOCK_WAIT": "1"})
        spent = _time.monotonic() - started
    check("лок: занят лок рядом с картой стенда — код 1", 1, code)
    check("лок: отказ назван «лок»", True, "лок" in (out + err))
    check("лок: ожидание с пределом (меньше 30 с)", True, spent < 30)
    check("лок: карта не тронута", before, s.devmap.read_text(encoding="utf-8"))
    code, out, err = s.run("--прижить", "T", "P/c", extra_env={"HARNESS_MAP_LOCK_WAIT": "1"})
    check("лок: свободен — код 0", 0, code)


def case_depth_attach_pin(tmp: Path) -> None:
    print("верх_вложенности в --прижить и --закрепить (M10, M10d)")

    def deep(with_parts: bool, s: Stand) -> list:
        leaf = {"id": "d", "название": "d", "охват": []}
        if with_parts:
            leaf["части"] = {"T": [part(s.file("src/deep.py"))]}
        return [{"id": "a", "название": "a", "охват": [], "главы": [
            {"id": "b", "название": "b", "охват": [], "главы": [
                {"id": "c", "название": "c", "охват": [], "главы": [leaf]}]}]}]

    for top, expected in ((3, 1), (4, 0)):
        s = Stand(tmp / f"прижить-глубина-{top}", roles={"верх_вложенности": top})
        s.plan("P", chapters=deep(False, s))
        s.card("T")
        s.write()
        before = s.devmap.read_text(encoding="utf-8")
        code = s.run("--прижить", "T", "P/a/b/c/d")[0]
        check(f"прижить в P/a/b/c/d при верх_вложенности {top} — код {expected}", expected, code)
        if expected:
            check(f"прижить глубже верха {top}: карта не тронута", before,
                  s.devmap.read_text(encoding="utf-8"))

    for top, expected in ((3, 1), (4, 0)):
        s = Stand(tmp / f"закрепить-глубина-{top}", roles={"верх_вложенности": top})
        s.plan("P", chapters=deep(True, s))
        s.card("T")
        s.write()
        before = s.devmap.read_text(encoding="utf-8")
        code = s.run("--закрепить", "P")[0]
        check(f"закрепить с подглавой P/a/b/c/d при верх {top} — код {expected}", expected, code)
        if expected:
            check(f"закрепить глубже верха {top}: карта не тронута", before,
                  s.devmap.read_text(encoding="utf-8"))
        else:
            check("закрепить при верх 4: T несёт глава P/a/b/c/d", "P/a/b/c/d",
                  map_cards(s.devmap)["T"].get("глава"))


def case_metrics_round1(tmp: Path) -> None:
    print("метрики круга 1: решения по задачам, резка, сбои, к красному")

    def plan_row(task: str, **data) -> dict:
        base = {"ид": f"p-{task}", "правило": "нагрузка", "роль": WORKER, "исполнителей": 1,
                "сложность": "высокая", "источник_сложности": "цена", "исход_плана": "часть_плана",
                "групп": 1, "файлов": 1, "сложность_режет": True,
                "ёмкость": {"свободно": 3, "живых": 2, "задач_в_работе": 3},
                "решение": "1 исполнитель", "урезал": None}
        base.update(data)
        return {"v": 1, "ts": iso_ago(5), "src": SRC, "event": "nesting.plan", "outcome": "ok",
                "task": task, "task_from": "arg", "session": "проба", "data": base}

    def stand(name: str, roles: dict | None = None) -> Stand:
        s = Stand(tmp / f"метрики1-{name}", roles=roles)
        s.write()
        for n in range(20):
            s.append(NEST_JOURNAL, plan_row("Z", ид=f"p-Z-{n}", правило="параллель", роль=LEAD,
                                            исполнителей=3, сложность="средняя", групп=3,
                                            сложность_режет=False,
                                            ёмкость={"свободно": 5, "живых": 0,
                                                     "задач_в_работе": 0}))
        s.append(NEST_JOURNAL, plan_row("H1", групп=1))
        s.append(NEST_JOURNAL, plan_row("H2", групп=4, ёмкость={"свободно": 1, "живых": 4,
                                                               "задач_в_работе": 3}))
        s.append(NEST_JOURNAL, plan_row("H3", правило="резка_под_нагрузкой_лид", роль=LEAD,
                                        исполнителей=3, групп=4))
        s.hook_row("skipped", {"ид": "f-1", "часть": "Часть: Z#1", "почему": "boom", "сбой": "сбой"},
                   task="Z")
        s.hook_row("skipped", {"ид": "f-2", "часть": "Часть: Z#2", "почему": "долго",
                               "сбой": "таймаут"}, task="Z")
        s.hook_row("skipped", {"ид": "f-3", "часть": None, "почему": "без метки"})
        return s

    s = stand("основа")
    code, out, err = s.run("--метрики", "--сутки", "1", "--json")
    m = parse_json(out)
    check("метрики1: 20 пересчётов одной задачи — одно решение, всего 4", 4, m.get("решений"))
    check("метрики1: правила по задачам (параллель 1, не 20)",
          {"параллель": 1, "нагрузка": 2, "резка_под_нагрузкой_лид": 1}, m.get("правила"))
    check("метрики1: резка — порезали 1, не порезали 2 с причинами",
          {"порезали": 1, "не_порезали": 2, "почему": {"групп_1": 1, "свободно_меньше_2": 1}},
          m.get("резка"))
    check("метрики1: сбоев хука 2 (сбой и таймаут, «без метки» не сбой)", 2, m.get("сбоев"))
    ready = m.get("к_красному") or {}
    check("метрики1: к_красному — пункты §7", {"решений", "ложных", "неотмеченных", "метка",
                                                "сбоев", "дверей"},
          set((ready.get("пункты") or {}).keys()))
    check("метрики1: решений 4 < 20 и сбоев 2 > 0 — пункты не выполнены, не готово",
          (False, False, False),
          (get(ready, "пункты", "решений"), get(ready, "пункты", "сбоев"), ready.get("готово")))
    code, out, err = s.run("--метрики", "--сутки", "1")
    check("метрики1: текст — строка «к красному: нет»", True,
          any(line.startswith("к красному: нет") for line in out.splitlines()))

    s = stand("обкатка-из-данных", roles={"обкатка.решений_мин": 3, "обкатка.сбоев_за_сутки": 2})
    m = parse_json(s.run("--метрики", "--сутки", "1", "--json")[1])
    check("метрики1: копия yaml обкатка.решений_мин 3, сбоев_за_сутки 2 — пункты выполнены",
          (True, True), (get(m, "к_красному", "пункты", "решений"),
                         get(m, "к_красному", "пункты", "сбоев")))


def case_outputs(tmp: Path) -> None:
    print("итог в планы-запуска и событие nesting.plan")
    s = Stand(tmp / "итог")
    s.task_in_plan("T", plain_parts(s, "o", 3))
    s.write()
    code, d, _ = s.decide("T")
    saved_path = s.logs / "планы-запуска" / "T.json"
    try:
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    check("итог: файл планы-запуска/T.json с ролью и числом",
          (LEAD, 3), (saved.get("роль"), saved.get("исполнителей")))
    check("итог: хеш плана записан", True, bool(saved.get("хеш_плана")))
    check("итог: части записаны", 3, len(saved.get("части") or []))
    rows = [r for r in s.rows() if r.get("event") == "nesting.plan" and r.get("task") == "T"]
    data = get(rows, 0, "data") or {}
    check("событие: одна строка nesting.plan, outcome ok", ["ok"],
          [r.get("outcome") for r in rows])
    check("событие: src launch-plan.py", SRC, get(rows, 0, "src"))
    check("событие: правило, роль, исполнителей", ("параллель", LEAD, 3),
          (data.get("правило"), data.get("роль"), data.get("исполнителей")))
    check("событие: ёмкость {свободно, живых, задач_в_работе}", (5, 0, 0),
          (get(data, "ёмкость", "свободно"), get(data, "ёмкость", "живых"),
           get(data, "ёмкость", "задач_в_работе")))
    check("событие: сложность и источник, исход плана, ид",
          ("средняя", "цена", "часть_плана", True),
          (data.get("сложность"), data.get("источник_сложности"), data.get("исход_плана"),
           bool(data.get("ид"))))


def case_metrics(tmp: Path) -> None:
    print("--отметить и --метрики, строка для session_state.sh")
    s = Stand(tmp / "метрики")
    s.card("A")
    s.pretask("A", files=[s.file("src/a.py")])            # мелочь
    s.task_in_plan("B", plain_parts(s, "b", 4, price="простая"))   # простая, 4 группы
    s.task_in_plan("C", plain_parts(s, "c", 3))           # параллель, lead
    s.task_in_plan("D", plain_parts(s, "d", 4))           # нагрузка, после заявок
    s.write()
    for task in ("A", "B", "C"):
        s.decide(task)
    load(s)
    s.decide("D")
    s.hook_row("ok", {"ид": "h-1", "часть": "Часть: P/c/C#1", "отказ": None,
                      "предупреждение": False, "дверь": None, "строгость": "жёлтый"}, task="C")
    s.hook_row("ok", {"ид": "h-2", "часть": "Часть: P/c/B", "отказ": 2,
                      "предупреждение": True, "дверь": None, "строгость": "жёлтый"}, task="B")
    s.hook_row("skipped", {"ид": "h-3", "часть": None, "почему": "без метки"})
    s.hook_row("ok", {"ид": "старое", "часть": "Часть: P/c/C#2", "отказ": None,
                      "предупреждение": False}, task="C", age_min=3 * 24 * 60)
    s.append(NEST_JOURNAL, {"v": 1, "ts": iso_ago(3 * 24 * 60), "src": SRC,
                            "event": "nesting.plan", "outcome": "ok", "task": "старая",
                            "data": {"ид": "p-старое", "правило": "параллель", "роль": LEAD,
                                     "исполнителей": 3, "сложность": "высокая"}})
    code, out, err = s.run("--отметить", "h-2", "ложно", "--кто", "проба")
    check("отметить: код 0", 0, code)
    marks = [r for r in s.rows() if r.get("event") == "nesting.mark"]
    check("отметить: строка nesting.mark {ид, оценка, кто}", [("h-2", "ложно", "проба")],
          [(get(r, "data", "ид"), get(r, "data", "оценка"), get(r, "data", "кто")) for r in marks])
    check("отметить: task строки nesting.mark — задача отмеченного события (B)", ["B"],
          [r.get("task") for r in marks])
    check("отметить: неизвестный ид — код 1", 1, s.run("--отметить", "нет-такого", "верно")[0])

    code, out, err = s.run("--метрики", "--сутки", "1", "--json")
    m = parse_json(out)
    check("метрики: код 0", 0, code)
    check("метрики: решений 4 (старое вне суток)", 4, m.get("решений"))
    check("метрики: вне мелкой 3", 3, m.get("вне_мелкой"))
    check("метрики: доли по ролям", {WORKER: 0.75, LEAD: 0.25}, m.get("по_ролям"))
    check("метрики: сработавшие правила",
          {"мелочь": 1, "простая": 1, "параллель": 1, "нагрузка": 1}, m.get("правила"))
    check("метрики: дробление сняли нагрузка и простая", {"нагрузка": 1, "простая": 1},
          m.get("сняли_дробление"))
    check("метрики: предупреждения всего/верных/ложных/неотмеченных",
          {"всего": 1, "верных": 0, "ложных": 1, "неотмеченных": 0}, m.get("предупреждения"))
    check("метрики: доля с меткой 2 из 3", 0.67, m.get("доля_с_меткой"))
    check("метрики: доля дверей 0", 0.0, m.get("доля_дверей"))
    check("метрики: по исходам есть мелкая и часть_плана", True,
          {"мелкая", "часть_плана"} <= set((m.get("по_исходам") or {}).keys()))

    code, out, err = s.run("--метрики", "--сутки", "1", "--строка")
    lines = [x for x in out.splitlines() if x.strip()]
    line = lines[0] if lines else ""
    check("строка: ровно одна строка", 1, len(lines))
    check("строка: начинается «вложенность:»", True, line.startswith("вложенность:"))
    for piece in ("решений 4", "вне мелкой 3", "lead 25%", "параллельных 25%", "тяжёлых 0%",
                  "ложных 1", "неотмеченных 0", "с меткой 67%", "дверей 0%"):
        check(f"строка: «{piece}»", True, piece in line)


def case_broken(tmp: Path) -> None:
    print("битые данные и неверный вызов")
    s = Stand(tmp / "битые")
    s.card("T")
    s.pretask("T", files=[s.file("src/a.py")])
    s.write()
    s.set_roles({}, raw="вывод_роли: [не закрыт\n")
    code, d, _ = s.decide("T")
    check("битый yaml: код 0", 0, code)
    check("битый yaml: состояние skipped с причиной", ("skipped", True),
          (d.get("состояние"), bool(d.get("почему"))))
    rows = [r for r in s.rows() if r.get("event") == "nesting.plan"]
    check("битый yaml: в журнале skipped с причиной", [("skipped", True)],
          [(r.get("outcome"), bool(get(r, "data", "почему"))) for r in rows])
    s.set_roles({}, raw="роли: {}\n")
    check("нет блока вывод_роли: skipped", "skipped", s.decide("T")[1].get("состояние"))
    s.set_roles({})
    check("нет карточки — код 1", 1, s.decide("нет-такой")[0])
    proc = subprocess.run([sys.executable, str(LAUNCH_PLAN)], capture_output=True, text=True,
                          env=s.env(), timeout=60)
    check("без аргументов — код 2", 2, proc.returncode)


HACKATHON_STEPS = ["спека", "ревью_спеки", "контракт", "план", "тесты", "ревью_кода"]


def hackathon_lines(stand: Stand, task: str, until: str | None) -> list:
    """Строка режима и подсказка из текста решения; срок — в harness.conf стенда."""
    stand.conf.write_text(CONF_TEXT + (f'HACKATHON_UNTIL="{until}"\n' if until else ""),
                          encoding="utf-8")
    _, out, err = stand.run(task)
    return [line for line in (out + err).splitlines()
            if line.startswith("хакатон:") or "--включить-задачу" in line]


def case_hackathon_line(tmp: Path) -> None:
    print("строка режима хакатон: видно, включён ли режим и шаги уровня задачи")
    s = Stand(tmp / "хакатон", pipeline={"шаги.хакатон": HACKATHON_STEPS})
    s.card("T")
    s.card("H", level="хакатон")
    for task in ("T", "H"):
        s.pretask(task, files=[s.file(f"src/{task}.py")])
    s.write()
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    future = (now + dt.timedelta(hours=5)).isoformat()
    past = (now - dt.timedelta(hours=5)).isoformat()
    hint = "hackathon.py --включить-задачу T"
    check("хакатон: режим выключен — одна строка, шаги уровня",
          ["хакатон: режим выключен; уровень задачи обычная; шаги: тесты, ревью_кода"],
          hackathon_lines(s, "T", None))
    check("хакатон: срок истёк — выключен",
          ["хакатон: режим выключен; уровень задачи обычная; шаги: тесты, ревью_кода"],
          hackathon_lines(s, "T", past))
    lines = hackathon_lines(s, "T", future)
    check("хакатон: режим включён — строка со сроком", True,
          f"хакатон: режим включён до {future}; уровень задачи обычная; шаги: тесты, ревью_кода"
          in lines)
    check("хакатон: режим включён, задача не хакатон — подсказка включить задачу",
          True, any(hint in line for line in lines))
    lines = hackathon_lines(s, "H", future)
    check("хакатон: уровень хакатон — шаги без мутаций, подсказки нет",
          [f"хакатон: режим включён до {future}; уровень задачи хакатон; шаги: "
           + ", ".join(HACKATHON_STEPS)], lines)


# ═══════════════ потолок живых от режима скорости (Ч6, спека 10.3.12) ═══════════════

SPEED_DATA = {
    "режимы": {"slow": {"задач": 1, "имя": "медленный", "живых_потолок": 3},
               "medium": {"задач": 3, "имя": "средний", "живых_потолок": 5},
               "fast": {"задач": 5, "имя": "быстрый", "живых_потолок": 8},
               "ultra": {"задач": 10, "имя": "ультра", "живых_потолок": 12}},
    "живых_при_аварии": 2, "по_умолчанию": "medium", "ступени": [1, 3, 5, 10],
    "открытый_запуск_макс_мин": 240, "старт_ждать_с": 120, "спек_ждут_владельца_максимум": 3,
    "блокеры": {"пауза": {"потолок": 0, "говорить": "нет"}, "авария": {"потолок": 1, "говорить": "да"}},
}
SPEED_MODES = {1: "slow", 3: "medium", 5: "fast", 10: "ultra"}


def speed_stand(tmp: Path, name: str, eff: int | None, wip: int = 1, age_min: float = 0,
                raw: str | None = None, selected: str | None = None, accident: bool = False) -> tuple:
    """Стенд со своим скорость.json и судьёй; (стенд, доп. окружение). В работе у судьи — wip
    карточек wip (T и ещё wip−1); eff None — состояния нет вовсе."""
    s = Stand(tmp / f"скорость-{name}", roles={"предохранитель.порог_сверх_режима": 1})
    s.task_in_plan("T", plain_parts(s, "v", 4))
    for i in range(1, wip):
        s.card(f"раб-{i}")
    s.write()
    demons = s.root / "harness" / "demons"
    demons.mkdir(parents=True, exist_ok=True)
    (demons / "speed_mode.py").symlink_to(SPEED_JUDGE)
    panel = s.base / "panel"
    panel.mkdir()
    data = s.base / "speed-modes.yaml"
    data.write_text(yaml.safe_dump(SPEED_DATA, allow_unicode=True), encoding="utf-8")
    if raw is not None:
        (panel / "скорость.json").write_text(raw, encoding="utf-8")
    elif eff is not None:
        mode = selected or SPEED_MODES.get(eff, "ultra")
        state = {"ts": iso_ago(age_min), "выбранный": mode,
                 "выбранный_задач": SPEED_DATA["режимы"][mode]["задач"], "эффективный": eff,
                 "в_работе": [], "блокеры": {"авария": {"потолок": 1, "причина": "проба"}} if accident else {},
                 "кандидаты": {}, "к_взятию": [], "добор": 0, "добор_почему": "проба", "причина": "проба",
                 "готовых": 0, "ждут_владельца": 0, "фантомов": 0, "зависимостей": 0, "битые": []}
        (panel / "скорость.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    projects = s.base / "projects"
    projects.mkdir()
    env = {"HARNESS_PANEL_STATE": str(panel), "PANEL_STATE_DIR": str(panel),
           "SPEED_MODE_DATA": str(data), "CLAUDE_PROJECTS_DIR": str(projects),
           "HARNESS_ACTIVE_LINE": "", "SPEED_MODE": ""}
    return s, env


def speed_cap(stand: Stand, env: dict) -> tuple:
    code, out, err = stand.run("T", "--json", extra_env=env)
    d = parse_json(out)
    return d, (get(d, "ёмкость", "жёсткий_верх"), get(d, "ёмкость", "порог_нагрузки"),
               get(d, "ёмкость", "режим")), out + err


def case_speed_ceiling(tmp: Path) -> None:
    print("потолок живых и порог нагрузки от ЭФФЕКТИВНОГО режима судьи скорости")
    for eff, top in ((1, 3), (3, 5), (5, 8), (10, 12)):
        s, env = speed_stand(tmp, f"эфф-{eff}", eff)
        d, got, raw = speed_cap(s, env)
        check(f"эффективный {eff}: жёсткий_верх {top}, порог {eff + 1}, режим {eff}",
              (top, eff + 1, eff), got)
        check(f"эффективный {eff}: строка ёмкости называет режим и порог", True,
              f"эффективный режим {eff}" in (d.get("строка_ёмкости") or "")
              and f"при пороге {eff + 1}" in (d.get("строка_ёмкости") or ""))
    s, env = speed_stand(tmp, "авария", 1, accident=True)
    check("авария: жёсткий_верх 2 (живых_при_аварии)", 2, speed_cap(s, env)[1][0])
    s, env = speed_stand(tmp, "выбран-ультра", 1, selected="ultra")
    check("выбран ultra при эффективном 1: жёсткий_верх 3, порог 2 — судится эффективный",
          (3, 2, 1), speed_cap(s, env)[1])


def case_speed_judge_silent(tmp: Path) -> None:
    print("судья не ответил: прежние 5 и 3 из данных")
    variants = (("старое-11-мин", dict(eff=10, age_min=11)),
                ("битое", dict(eff=None, raw="{не json")),
                ("без-полей", dict(eff=None, raw=json.dumps({"ts": iso_ago(0), "эффективный": 10}))),
                ("нет-файла", dict(eff=None)))
    for name, kw in variants:
        s, env = speed_stand(tmp, f"молчит-{name}", **kw)
        d, got, raw = speed_cap(s, env)
        check(f"{name}: жёсткий_верх 5, порог 3, режим нет", (5, 3, None), got)
        check(f"{name}: строка ёмкости — «судья скорости не отвечает»", True,
              "судья скорости не отвечает" in (d.get("строка_ёмкости") or ""))
    s, env = speed_stand(tmp, "свежее-9-мин", 5, age_min=9)
    check("состояние 9 мин назад ещё верно: жёсткий_верх 8", 8, speed_cap(s, env)[1][0])


def case_speed_capacity(tmp: Path) -> None:
    print("ёмкость и правило нагрузки при потолке судьи")
    s, env = speed_stand(tmp, "шесть-живых", 1, accident=True)
    fill_capacity(s, 6)
    d, got, _ = speed_cap(s, env)
    check("6 живых при потолке 2: исполнителей 0, ждать", (2, 0, True),
          (got[0], d.get("исполнителей"), d.get("ждать")))
    for eff, wip, loaded in ((5, 3, False), (3, 3, False), (3, 4, True), (1, 2, True)):
        s, env = speed_stand(tmp, f"нагрузка-{eff}-{wip}", eff, wip=wip)
        d, got, _ = speed_cap(s, env)
        check(f"эффективный {eff}, в работе {wip} (порог {eff + 1}): «нагрузка» "
              f"{'выбрана' if loaded else 'не выбрана'}",
              (wip, loaded), (get(d, "ёмкость", "задач_в_работе"), d.get("правило") == "нагрузка"))
    s, env = speed_stand(tmp, "несвеж-нагрузка", 10, wip=1, age_min=30)
    load(s)
    d, got, _ = speed_cap(s, env)
    check("судья несвеж, 3 задачи по журналу запусков: порог 3 из данных — «нагрузка»",
          (3, 3, "нагрузка"), (got[1], get(d, "ёмкость", "задач_в_работе"), d.get("правило")))


CASES = (case_trifle, case_parallel, case_chain, case_large, case_no_overlap, case_plan_part,
         case_orphan, case_candidate, case_nesting_depth, case_attach, case_new_chapter,
         case_pin, case_simple, case_estimate, case_two_launches, case_load,
         case_cut_under_load, case_simple_under_load, case_cut_nothing,
         case_cut_threshold_data, case_ceiling, case_live_sources, case_battle_tasks,
         case_waiting_part, case_waiting_by_need, case_high_without_files,
         case_trifle_never_waits, case_closed_plan, case_deleted_chapter, case_renamed_plan,
         case_tasks_plan,
         case_map_lock, case_depth_attach_pin, case_outputs, case_metrics,
         case_metrics_round1, case_broken, case_hackathon_line,
         case_speed_ceiling, case_speed_judge_silent, case_speed_capacity)


def main() -> int:
    global failed, checked
    if not LAUNCH_PLAN.exists():
        print(f"нет launch-plan.py: {LAUNCH_PLAN}")
        print("SELFTEST: КРАСНЫЙ (механизма нет, случаи не запускались)")
        return 1
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in CASES:
            try:
                case(tmp)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
