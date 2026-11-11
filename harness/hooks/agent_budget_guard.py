#!/usr/bin/env python3
"""PreToolUse на запуск помощника: потолок числа помощников на одну задачу.

Потолок — данные: ключ `помощники` в harness/config/pipeline.yaml, по уровню
глубины задачи (`всего` — запусков на задачу, `ревью` — из них ревьюеров,
0 — без потолка). Без него число помощников держалось привычкой скила, и
мелкая задача получала 3–5 ревьюеров.

Задача запуска — метка «Часть: <id>» в prompt, если такой id есть в карте
(карточка без уровня глубины берёт потолок «без-задачи»); иначе единственная
`wip` карты (или HARNESS_TASK), тем же способом, что у журнала событий
(scripts/lib/events.py, current_task). Последний трейлер
Dev-Map задачей не считается: он называет ПРЕДЫДУЩУЮ задачу, и запуски новой
упёрлись бы в чужой счёт.

Счёт — строки самого хука в $LOG_DIR/вызовы-агентов.jsonl (формат v1 через
events.emit, событие agent.launch, исход ok); решения agent-level.py и строки
других писателей в том же журнале не считаются.
Ревьюер узнаётся по слову «ревью»/«review» в описании запуска — признак
слабый: ревьюер без этого слова идёт как помощник, но в «всего» он попадает.

Сверх потолка — отказ (код 2, причина в stderr) с законной дверью:
    python3 scripts/hooks/agent_budget_guard.py --door 'почему'
Дверь — файл со сроком 15 минут и задачей; переменная окружения дверью быть не
может: хук запускает оболочка, и окружение команды агента до него не доходит.

Хуки на Agent оболочка зовёт ПАРАЛЛЕЛЬНО: запуск, который отменит сосед
agent_skill_guard.py, не состоится — его решение спрашивается здесь же, и
такой запуск не засчитывается. Пачка запусков в одном сообщении идёт
одновременно — счёт и запись стоят под замком, иначе пачка проскочит потолок.

Нет задачи (ни метки с id из карты, ни `wip`: демон, помощник-управляющий) —
запуски считаются по СЕССИИ под потолком уровня «без-задачи»: иначе при пустой карте потолка нет вовсе
(замер 29.10: все 20 запусков легли skipped). Потолок или уровень не
определены — пропуск со следом в журнале (fail-open, как у model_guard.py):
запрет вслепую остановил бы смену.

Вложенные запуски управляющего (agent_type harness-lead): считаются отдельно на
управляющего, ключ — его agent_id (строка запуска несёт data.управляющий), и
НЕ съедают ни потолок смены «без-задачи», ни потолок задачи: иначе управляющий
со своими исполнителями упирался в «8 из 8» на 9-м запуске. Потолок «всего на
управляющего» — pipeline.yaml → помощники.управляющий; «параллельно» —
helper-roles.yaml → роли.harness-lead.параллельно_максимум. Конца запуска
хук не видит, поэтому живым считается исполнитель управляющего (parentAgentId в
meta.json), чей транскрипт писался не позже LIVE_WINDOW_S секунд назад.

Глубина вложенности: в входе хука её нет, есть только `agent_id` зовущего
помощника. Глубину хранит `<каталог транскрипта>/<session_id>/subagents/
agent-<agent_id>.meta.json`, поле spawnDepth (1 — помощник смены). Новый
помощник на единицу глубже зовущего (смена — 0): глубже 2 — отказ. Файла нет — глубина
неизвестна, правило молчит. Двери у глубины нет: это не число, а форма.

Тип без собранной карточки (без типа, general-purpose, claude, Explore, Plan,
claude-code-guide, типы плагинов) — ОТКАЗ: системный промпт встроенного типа не
правится, личности в нём нет, а CLAUDE.md проекта он грузит целиком (71 тыс.
токенов против 20 тыс. у карточки с omitClaudeMd). Собранные карточки — раздел
«карточки» launch-policy.yaml (тип → класс), их тело собирает self-prompt.py
--карточки. Последняя строка карточки «отпечаток: <метка>» сверяется с ТЕКУЩЕЙ
сборкой класса: правка роли без пересборки — отказ «пересобери карточки». Не
удалось собрать класс сверки — пропуск (fail-open, как у потолка). Отказ — строка
agent.unbuilt-type исходом refused; потолок её не считает.
"""
from __future__ import annotations

import fcntl
import functools
import importlib.util
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import config                                            # noqa: E402
import events                                            # noqa: E402
import launch_plan_lib                                   # noqa: E402
from launch_plan_lib import meta_path, tool_use_id_of    # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_skill_guard                                 # noqa: E402

TOOLS = ("Agent", "Task")
# Имя журнала стоит литералом и в emit(): ворота G2 узнают писателя по нему.
JOURNAL = "вызовы-агентов"
SRC = "scripts/hooks/agent_budget_guard.py"
LAUNCH_EVENT = "agent.launch"
RESUME_EVENT = "agent.resume"
DOOR_NAME = "agent-budget.door"
LOCK_NAME = "agent-budget.lock"
DOOR_TTL_S = 15 * 60
DOOR_REASON_MIN = 10
REVIEW_MARK = re.compile(r"ревью|review", re.IGNORECASE)
# Та же метка, что у launch_plan_guard.py: по ней план запуска знает задачу запуска.
PART_LABEL = re.compile(r"Часть:\s*(?P<path>[^\s#]+)(?:#(?P<n>\d+))?")
UNBUILT_EVENT = "agent.unbuilt-type"
UNKNOWN_CARD_EVENT = "agent.unknown-card"
UNLABELED_EVENT = "agent.unlabeled"
LIGHT_TYPE = "harness-worker"
SCOUT_TYPE = "harness-explorer"
FINGERPRINT_PREFIX = "отпечаток: "
NO_TASK = "-"
NO_TASK_LEVEL = "без-задачи"
MAX_DEPTH = 2
LEAD_TYPE = "harness-lead"
LEAD_LEVEL = "управляющий"
LIVE_WINDOW_S = 180


def pipeline_path() -> Path:
    return Path(os.environ.get("HARNESS_PIPELINE_CONF")
                or ROOT / "harness" / "config" / "pipeline.yaml")


def roles_path() -> Path:
    return Path(os.environ.get("HARNESS_HELPER_ROLES")
                or ROOT / "harness" / "config" / "helper-roles.yaml")


def lead_parallel_max() -> int:
    """Потолок одновременных исполнителей управляющего; 0 — не определён, без потолка."""
    try:
        data = yaml.safe_load(roles_path().read_text(encoding="utf-8")) or {}
        return int(data["роли"][LEAD_TYPE]["параллельно_максимум"])
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError):
        return 0


def journal_path() -> Path:
    return Path(events.log_dir()) / f"{JOURNAL}.jsonl"


def door_path() -> Path:
    return Path(events.log_dir()) / DOOR_NAME


def ceilings() -> dict:
    try:
        data = yaml.safe_load(pipeline_path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data.get("помощники") or {}


def current_task() -> str | None:
    task, source = events.current_task()
    return task if source in ("env", "map") else None


@functools.lru_cache(maxsize=1)
def devmap_tree():
    """Разобранная карта или None, если не читается; один разбор на запуск хука."""
    # Карта ~370 КБ: чистый питоновский разбор — 1,2 с на каждый запуск, C — 0,14 с.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        return yaml.load(Path(events.devmap_path()).read_text(encoding="utf-8"), Loader=loader)
    except (OSError, ValueError, yaml.YAMLError):    # ValueError — и битая кодировка
        return None


def find_card(task: str) -> dict | None:
    """Карточка с id `task` или None. Наличие и уровень берутся из одного
    результата: у карточек без `глубина` уровня нет, но задачей метки они быть обязаны."""
    seen: set[int] = set()
    stack = [devmap_tree()]
    while stack:
        node = stack.pop()
        if id(node) in seen:                         # якоря YAML бывают рекурсивными
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            if isinstance(node.get("id"), str) and node["id"] == task:
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def card_level(card: dict | None) -> str | None:
    depth = (card or {}).get("глубина")
    return depth.get("уровень") if isinstance(depth, dict) else None


def launches(task: str, session: str, lead: str = "") -> list:
    """Засчитанные запуски по журналу: управляющего `lead` либо задачи; у запусков
    без задачи ключ — сессия. Запуски управляющих в счёт смены и задачи не идут."""
    try:
        lines = journal_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not (row.get("src") == SRC and row.get("event") == LAUNCH_EVENT
                and row.get("outcome") == "ok"):
            continue
        by_lead = (row.get("data") or {}).get("управляющий") or ""
        if lead:
            counted = by_lead == lead
        else:
            counted = (not by_lead and row.get("task") == task
                       and (task != NO_TASK or row.get("session") == (session or None)))
        if counted:
            found.append(row)
    return found


def door_reason(task: str) -> str:
    try:
        door = json.loads(door_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if door.get("задача") != task or int(door.get("до") or 0) < int(time.time()):
        return ""
    return str(door.get("почему") or "").strip()


def record(outcome: str, task: str, session: str, data: dict) -> None:
    """След запуска: по этим строкам ведётся счёт, без них потолка нет."""
    events.emit(SRC, LAUNCH_EVENT, outcome, journal="вызовы-агентов", task=task,
                session=session or None, data=data)


def note(event: str, task: str, session: str, data: dict) -> None:
    """Сигнальное событие: сбой записи не должен останавливать запуск."""
    try:
        events.emit(SRC, event, "ok", journal="вызовы-агентов", task=task,
                    session=session or None, data=data)
    except Exception:  # noqa: BLE001
        pass


def speed_mode_on() -> bool:
    return bool(config.из_файла(config.путь_harness_conf()).get("SPEED_MODE"))


def live_children(payload: dict, lead: str) -> int:
    """Сколько исполнителей управляющего `lead` писали в транскрипт недавно."""
    folder = (Path(str(payload.get("transcript_path") or "")).parent
              / str(payload.get("session_id") or "") / "subagents")
    now = time.time()
    live = 0
    for meta in folder.glob("agent-*.meta.json"):
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("parentAgentId") != lead:
                continue
            log = meta.with_name(meta.name.removesuffix(".meta.json") + ".jsonl")
            live += now - log.stat().st_mtime <= LIVE_WINDOW_S
        except (OSError, ValueError, AttributeError):
            continue
    return live


def over_lead_ceiling(payload: dict, lead: str, ceiling: dict) -> str:
    """Причина отказа управляющему словами или пустая строка."""
    parallel = lead_parallel_max()
    live = live_children(payload, lead) if parallel else 0
    if parallel and live >= parallel:
        return (f"у управляющего {lead} уже {live} живых исполнителей: параллельно не "
                f"более {parallel} (helper-roles.yaml)")
    total = int(ceiling.get("всего") or 0)
    done = len(launches(NO_TASK, "", lead))
    if total and done >= total:
        return (f"помощник №{done + 1} управляющего {lead} сверх потолка: "
                f"запусков уже {done} из {total}")
    return ""


def over_ceiling(task: str, session: str, level: str, ceiling: dict,
                 is_review: bool) -> str:
    """Причина отказа словами или пустая строка, если запуск в пределах."""
    done = launches(task, session)
    total = int(ceiling.get("всего") or 0)
    review = int(ceiling.get("ревью") or 0)
    reviews_done = sum(1 for r in done if (r.get("data") or {}).get("ревью"))
    if is_review and review and reviews_done >= review:
        return (f"ревьюер №{reviews_done + 1} задаче {task} (уровень «{level}») сверх "
                f"потолка: ревьюеров уже {reviews_done} из {review}")
    if total and len(done) >= total:
        return (f"помощник №{len(done) + 1} задаче {task} (уровень «{level}») сверх "
                f"потолка: запусков уже {len(done)} из {total}")
    return ""


def note_resume(payload: dict) -> None:
    """SendMessage открывает помощника снова после его конца: статус без этой записи считал бы его законченным."""
    target = str((payload.get("tool_input") or {}).get("to") or "")
    call = tool_use_id_of(payload, target) if target else ""
    if call:
        note(RESUME_EVENT, current_task() or NO_TASK, str(payload.get("session_id") or ""), {"вызов": call})


def card_advice(refusal: str) -> str:
    return (f"Запуск помощника остановлен: {refusal}.\n"
            f"Разведка и чтение — subagent_type «{SCOUT_TYPE}», работа — «{LIGHT_TYPE}»; "
            "оба идут со своей собранной карточкой (старт 20 тыс. токенов).\n"
            "Встроенные Explore, Plan, claude-code-guide, general-purpose и типы плагинов закрыты.")


def decide(payload: dict) -> tuple[int, str]:
    """(код возврата, отказ). 0 — пропуск, 2 — отказ с причиной."""
    if payload.get("tool_name") == "SendMessage":
        note_resume(payload)
        return 0, ""
    if (payload.get("tool_name") or "") not in TOOLS:
        return 0, ""
    if agent_skill_guard.решение(payload)[0]:
        return 0, ""            # отказ скажет сосед; несостоявшийся запуск не считаем
    refusal = card_refusal(payload)
    if refusal:
        return 2, card_advice(refusal)
    lock = Path(events.log_dir()) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return judge(payload)


def self_prompt():
    """Сборщик личности как модуль: метка класса считается тем же кодом, что и карточка."""
    spec = importlib.util.spec_from_file_location("self_prompt", ROOT / "scripts" / "self-prompt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def card_state(agent_type: str) -> tuple[bool, str]:
    """(тип имеет собранную карточку, причина отказа по карточке или пусто).
    Политику или сборку не прочесть — тип считается собранным и сверка молчит."""
    try:
        builder = self_prompt()
        tree = Path(os.environ.get("SELF_TREE_DIR") or ROOT)
        policy = builder.load_policy(Path(os.environ.get("LAUNCH_POLICY")
                                          or tree / "harness" / "config" / "launch-policy.yaml"))
        spec = (policy.get("карточки") or {}).get(agent_type)
        if spec is None:
            return False, ""
        etalon = Path(os.environ.get("SELF_ETALON_DIR") or builder.DEFAULT_ETALON)
        label = builder.build(spec["класс"], policy, etalon, tree)[0].partition("\n")[0]
    except Exception:  # noqa: BLE001 - сверка не должна ронять запуск: без неё тип считается собранным
        return True, ""
    cards = Path(os.environ.get("SELF_CARDS_DIR") or tree / ".claude" / "agents")
    try:
        last = (cards / f"{agent_type}.md").read_text(encoding="utf-8").rstrip("\n").rpartition("\n")[2]
    except FileNotFoundError:
        return True, ""         # нет файла — оболочка типа не знает сама; пропажу карточки судит гейт
    except (OSError, UnicodeDecodeError):
        last = ""
    if last == FINGERPRINT_PREFIX + label.removeprefix("Метка сборки: "):
        return True, ""
    return True, (f"карточка {agent_type} не равна текущей сборке класса «{spec['класс']}»: "
                  f"пересобери карточки (python3 scripts/self-prompt.py --карточки --в {cards})")


def card_refusal(payload: dict) -> str:
    """Причина отказа по типу и карточке словами или пустая строка; отказ пишется в журнал."""
    agent_type = str((payload.get("tool_input") or {}).get("subagent_type") or "")
    built, reason = card_state(agent_type)
    if not built:
        reason = (f"тип «{agent_type or 'без типа'}» без собранной карточки: его системный промпт "
                  "не правится, личности харнеса в нём нет, а CLAUDE.md проекта он грузит целиком")
    if reason:
        events.emit(SRC, UNBUILT_EVENT, "refused", journal="вызовы-агентов",
                    session=payload.get("session_id") or None,
                    data={"тип": agent_type or "-", "почему": reason})
    return reason


def agent_model(tool_input: dict) -> str:
    """Модель запуска: явная, иначе из frontmatter агента; пусто, если не названа."""
    if tool_input.get("model"):
        return str(tool_input["model"])
    card = ROOT / ".claude" / "agents" / f"{tool_input.get('subagent_type') or ''}.md"
    try:
        head = card.read_text(encoding="utf-8").split("---")[1]
    except (OSError, IndexError, ValueError):
        return ""
    found = re.search(r"^model:\s*(\S+)", head, re.MULTILINE)
    return found[1] if found else ""


def caller_depth(payload: dict) -> int | None:
    """spawnDepth зовущего помощника; 0 — зовёт смена; None — глубина неизвестна."""
    agent = payload.get("agent_id")
    if not agent:
        return 0
    try:
        return int(json.loads(meta_path(payload, agent).read_text(encoding="utf-8"))["spawnDepth"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def too_deep(payload: dict) -> str:
    """Причина отказа по глубине словами или пустая строка."""
    caller = caller_depth(payload)
    if caller is None:
        return ""
    depth = caller + 1
    if depth > MAX_DEPTH:
        return f"глубина вложенности {depth} сверх потолка {MAX_DEPTH}"
    return ""


def judge(payload: dict) -> tuple[int, str]:
    tool_input = payload.get("tool_input") or {}
    description = str(tool_input.get("description") or "")
    is_review = bool(REVIEW_MARK.search(
        f"{description} {tool_input.get('subagent_type') or ''}"))
    session = payload.get("session_id") or ""
    data = {"ревью": is_review, "описание": description[:120],
            "тип": str(tool_input.get("subagent_type") or ""), "модель": agent_model(tool_input),
            "вызов": str(payload.get("tool_use_id") or "")}
    caller = str(payload.get("agent_id") or "")
    parent_call = tool_use_id_of(payload, caller) if caller else ""
    if parent_call:
        data["родитель"] = parent_call
    label = PART_LABEL.search(str(tool_input.get("prompt") or ""))
    if label:
        data["часть"] = f"Часть: {label['path']}" + (f"#{label['n']}" if label["n"] else "")

    marked = launch_plan_lib.mark_task({"data": data})
    card = find_card(marked) if marked else None
    if card is not None:
        task = marked
    else:
        task = current_task() or NO_TASK
        if marked:
            unreadable = {"почему": "карта не читается"} if devmap_tree() is None else {}
            note(UNKNOWN_CARD_EVENT, task, session, {"метка": marked, **data, **unreadable})
        card = find_card(task) if task != NO_TASK else None
    if not marked and not caller and speed_mode_on():
        note(UNLABELED_EVENT, task, session, data)

    depth_reason = too_deep(payload)
    if depth_reason:
        record("refused", task, session, {**data, "почему": depth_reason})
        advice = (f"Запуск помощника остановлен: {depth_reason}.\n"
                  f"Исполнитель глубины 2 — {LIGHT_TYPE} (без CLAUDE.md проекта); "
                  "глубже 2 цепочку не строят: разбей работу на плоские запуски "
                  "из одного звена.")
        return 2, advice

    lead = str(payload.get("agent_id") or "") if payload.get("agent_type") == LEAD_TYPE else ""
    if lead:
        level = LEAD_LEVEL
    elif task == NO_TASK:
        level = NO_TASK_LEVEL
    else:
        level = card_level(card)
        if level is None and marked == task:
            level = NO_TASK_LEVEL    # карточка без уровня: метка не снимает потолок
    ceiling = ceilings().get(level) if level else None
    if not isinstance(ceiling, dict):
        record("skipped", task, session,
               {**data, "уровень": level or "-", "почему": "потолок не определён"})
        return 0, ""
    data["уровень"] = level
    if lead:
        data["управляющий"] = lead

    reason = (over_lead_ceiling(payload, lead, ceiling) if lead
              else over_ceiling(task, session, level, ceiling, is_review))
    if not reason:
        record("ok", task, session, data)
        return 0, ""
    why = door_reason(task)
    if why:
        record("ok", task, session, {**data, "дверь": why})
        return 0, ""
    record("refused", task, session, {**data, "почему": reason})
    door_env = f"HARNESS_TASK={shlex.quote(task)} " if task != NO_TASK else ""
    return 2, (
        f"Запуск помощника остановлен: {reason}.\n"
        f"Потолок — данные: harness/config/pipeline.yaml → помощники.{level}.\n"
        "Законная дверь, если помощник нужен: открыть её с причиной на 15 минут —\n"
        f"  {door_env}python3 scripts/hooks/agent_budget_guard.py --door 'почему нужен ещё один'\n"
        "Причина уходит в журнал вызовы-агентов.jsonl с каждым запуском через дверь.")


def open_door(reason: str) -> int:
    reason = reason.strip()
    if len(reason) < DOOR_REASON_MIN:
        print(f"дверь не открыта: причина короче {DOOR_REASON_MIN} знаков", file=sys.stderr)
        return 1
    task = current_task() or NO_TASK
    until = int(time.time()) + DOOR_TTL_S
    door_path().parent.mkdir(parents=True, exist_ok=True)
    door_path().write_text(json.dumps({"задача": task, "почему": reason, "до": until},
                                      ensure_ascii=False), encoding="utf-8")
    print(f"дверь открыта для {task} на {DOOR_TTL_S // 60} минут: {reason}")
    return 0


def main() -> int:
    if "--door" in sys.argv:
        position = sys.argv.index("--door") + 1
        return open_door(sys.argv[position] if position < len(sys.argv) else "")
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    code, text = decide(payload)
    if code:
        print(text, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
