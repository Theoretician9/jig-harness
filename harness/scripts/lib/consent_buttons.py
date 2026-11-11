#!/usr/bin/env python3
"""Кнопки согласия на стороне диспетчера: что слать владельцу и как судить нажатие.

Диспетчер на bash только ходит в Bot API; решения («слать ли», «принять ли
нажатие») принимает этот модуль, потому что jq-цепочка по сроку, nonce и
последнему message_id не читается и ломается молча.

consent-buttons.jsonl пишет только этот модуль и только по вызову диспетчера:
из этого файла дверь берёт слово владельца. Сторож отменяет запуск sent/judge
из сессии (guard_bash.запуск_писателя_кнопок).

Вызовы (каталог журналов — первый аргумент):
  unsent  <log> <secrets_dir>   → строки JSON {nonce, target, text, reply_markup} для отправки;
                                  target = "<вид>:<цель>" (без вида, с «/» в начале — удаление)
  sent    <log> <nonce> <target> → ответ sendMessage на stdin; пишет «отправлено» только при ok:true
  judge   <log> <owner_id>      → callback_query на stdin; пишет «да»/«нет», если нажатие прошло
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import door_subjects  # noqa: E402
import events  # noqa: E402
import owner_word  # noqa: E402
import patterns  # noqa: E402
import push_subject  # noqa: E402

BUTTONS = "consent-buttons.jsonl"
REQUESTS = "consent-requests.jsonl"
EVENT_OF = {name: event for event, name in owner_word.СОБЫТИЯ_СОГЛАСИЯ.items()}
CALLBACK_RE = re.compile(r"^c:([A-Za-z0-9_-]{22}):(y|n)$")
WHY_LIMIT = 200
DECISIONS = {"y": "да", "n": "нет"}
GIT_TIMEOUT_SEC = 5
LIST_LIMIT = 5
SUBJECT_LIMIT = 60
VALUE_LIMIT = 240
TEXT_LIMIT = 700


class ReadError(Exception):
    """Текст запроса не собран (git не прочитался, предмет не разобран): запрос не шлётся."""


def read_rows(path: Path) -> list[dict]:
    """Плоские записи согласия из конвертов. Битая и не-конверт пропускаются: чужая
    порча не должна останавливать отправку."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        flat = owner_word.плоская_запись(row) if isinstance(row, dict) else None
        if flat is not None:
            rows.append(flat)
    return rows


def minutes_since(ts: object) -> float | None:
    try:
        then = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if then.tzinfo is None:
        return None
    return (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 60


def append_row(log: Path, name: str, data: dict) -> None:
    """Запись через events.emit в журнал каталога log. Не записано — OSError:
    решение, которого нет в журнале, нельзя подтверждать владельцу."""
    previous = os.environ.get("HARNESS_LOG_DIR")
    os.environ["HARNESS_LOG_DIR"] = str(log)
    try:
        written = events.emit("scripts/lib/consent_buttons.py", EVENT_OF[name], "ok",
                              journal="consent-buttons", data=data)
    finally:
        if previous is None:
            del os.environ["HARNESS_LOG_DIR"]
        else:
            os.environ["HARNESS_LOG_DIR"] = previous
    if not written:
        raise OSError(f"{BUTTONS}: запись «{name}» не попала в журнал")


def last_sent(rows: list[dict], nonce: str) -> dict | None:
    sent = [r for r in rows if r.get("nonce") == nonce and r.get("событие") == "отправлено"]
    return sent[-1] if sent else None


def decided(rows: list[dict], nonce: str) -> bool:
    return any(r.get("nonce") == nonce and r.get("событие") in DECISIONS.values() for r in rows)


def ttl_phrase(ttl: int) -> str:
    """Срок кнопки словами: сутки читаются в часах."""
    return f"{ttl // 60} ч" if ttl >= 120 and ttl % 60 == 0 else f"{ttl} мин"


def delete_text(request: dict, secrets_dir: str, ttl: int) -> str:
    """Для секрета — каталог, размер и дата, без имени файла: имя может быть ключом доступа."""
    target = Path(str(request["цель"]))
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    # Как у двери: секрет — всё под корнем, а не только файл прямо в нём.
    in_secrets = bool(secrets_dir) and target.is_relative_to(os.path.realpath(secrets_dir))
    if in_secrets:
        try:
            info = target.stat()
            what = (f"Секрет в каталоге {target.parent}, {info.st_size} байт, изменён "
                    f"{dt.datetime.fromtimestamp(info.st_mtime):%Y-%m-%d %H:%M}.")
        except OSError:
            what = f"Секрет в каталоге {target.parent} (файл не прочитан)."
    else:
        what = f"Копия {target}."
    return f"Сессия просит удалить или перезаписать файл. {what}\nПочему: {why}\nКнопка действует {ttl_phrase(ttl)}."


def git_lines(repo: str, *args: str) -> list[str]:
    """Вывод git по репо. Без подмены объектов: git replace иначе подменит заголовки
    при тех же хэшах. Зависший git не держит круг диспетчера."""
    try:
        done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SEC, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))
    except (subprocess.TimeoutExpired, OSError) as err:
        raise ReadError(f"git {args[0]} не прочитался: {type(err).__name__}") from None
    if done.returncode != 0:
        raise ReadError(f"git {args[0]} вернул {done.returncode}")
    return done.stdout.splitlines()


def commits_word(count: int) -> str:
    if count % 100 in range(11, 15) or count % 10 not in (1, 2, 3, 4):
        return "коммитов"
    return "коммит" if count % 10 == 1 else "коммита"


def short_address(address: str) -> str:
    """Хост/путь: схема не нужна владельцу, а scp-форма «хост:путь» читается как хост/путь."""
    plain = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", address)
    return re.sub(r"^([^/:]+):(?!//)", r"\1/", plain)


def fit_text(head: str, listed: list[str], count: int, tail: str) -> str:
    """Шапка, до LIST_LIMIT заголовков, «и ещё N», хвост — в пределах TEXT_LIMIT.

    При нехватке места уходят строки списка, а не хвост с «Почему»."""
    lines = [line if len(line) <= 8 + SUBJECT_LIMIT else line[:7 + SUBJECT_LIMIT] + "…" for line in listed]
    for shown in range(len(lines), -1, -1):
        rest = count - shown
        more = [f"и ещё {rest}"] if rest > 0 else []
        text = "\n".join([head, *lines[:shown], *more, tail])
        if len(text) <= TEXT_LIMIT:
            return text
    return text[:TEXT_LIMIT]


def push_text(request: dict, secrets_dir: str, ttl: int) -> str:
    try:
        subject = push_subject.parse_subject(str(request["цель"]))
    except push_subject.PushSubjectError as err:
        raise ReadError(str(err)) from None
    span = f"{subject['base']}..{subject['head']}"
    count = int(git_lines(subject["repo"], "rev-list", "--count", span)[0])
    listed = git_lines(subject["repo"], "log", f"--max-count={LIST_LIMIT}", "--format=%h %s", span)
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    branch = subject["ref"].removeprefix("refs/heads/")
    head = (f"Пуш: {count} {commits_word(count)} в {short_address(subject['address'])}, "
            f"ветка {branch}.")
    return fit_text(head, listed, count, f"Почему: {why}\nКнопка действует {ttl_phrase(ttl)}.")


def parse_or_fail(parser, request: dict) -> dict:
    try:
        return parser(str(request["цель"]))
    except push_subject.PushSubjectError as err:
        raise ReadError(str(err)) from None


def deploy_text(request: dict, secrets_dir: str, ttl: int) -> str:
    subject = parse_or_fail(door_subjects.parse_deploy_subject, request)
    tree, sha = subject["tree"], subject["sha"]
    short, _, title = git_lines(tree, "log", "-1", "--format=%h %s", sha)[0].partition(" ")
    tags = git_lines(tree, "tag", "-l", "deploy-*", "--sort=-refname")
    span = f"{tags[0]}..{sha}" if tags else sha
    count = int(git_lines(tree, "rev-list", "--count", span)[0])
    listed = git_lines(tree, "log", f"--max-count={LIST_LIMIT}", "--format=%h %s", span)
    since = f"С выката {tags[0]}" if tags else "Выкатов раньше не было, в истории"
    head = (f"Выкат: {Path(tree).name}, коммит {short} «{title[:SUBJECT_LIMIT]}».\n"
            f"{since} — {count} {commits_word(count)}:")
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    return fit_text(head, listed, count, f"Почему: {why}\nКнопка действует {ttl_phrase(ttl)}.")


def drop_text(request: dict, secrets_dir: str, ttl: int) -> str:
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    return (f"Снести пробную базу {request['цель']}? Дамп будет снят перед удалением.\n"
            f"Почему: {why}\nКнопка действует {ttl_phrase(ttl)}.")


def rewrite_text(request: dict, secrets_dir: str, ttl: int) -> str:
    subject = parse_or_fail(door_subjects.parse_rewrite_subject, request)
    branch = subject["ref"].removeprefix("refs/heads/")
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    return (f"Переписать публичную историю: {short_address(subject['address'])}, ветка {branch}, "
            f"вершина {subject['tip'][:7]} → {subject['new'][:7]}. Все ссылки удалённого отправляются "
            f"принудительно; бэкап прежней истории снят дверью.\n"
            f"Почему: {why}\nКнопка действует {ttl_phrase(ttl)}.")


def published_since(subject: dict) -> str | None:
    """Хэш пакета, из которого собрана вершина публичной ветки (его несёт заголовок
    коммита сборки). Нет вершины в сборке или заголовок чужой — None."""
    try:
        title = git_lines(subject["build"], "log", "-1", "--format=%s", subject["remote_tip"])[0]
    except (ReadError, IndexError):
        return None
    found = patterns.ЗАГОЛОВОК_РЕЛИЗА.fullmatch(title)
    return found.group(1) if found else None


def unsent_in_build(subject: dict) -> int:
    """Сколько коммитов сборки ещё не на удалённом (повтор упавшей отправки). Не посчиталось — 0."""
    if subject["build_tip"] == door_subjects.NO_TIP:
        return 0
    try:
        return int(git_lines(subject["build"], "rev-list", "--count",
                             f"{subject['remote_tip']}..{subject['build_tip']}")[0])
    except (ReadError, IndexError, ValueError):
        return 0


def publish_text(request: dict, secrets_dir: str, ttl: int) -> str:
    subject = parse_or_fail(door_subjects.parse_publish_subject, request)
    pack, sha = subject["package"], subject["sha"]
    short, _, title = git_lines(pack, "log", "-1", "--format=%h %s", sha)[0].partition(" ")
    branch = subject["ref"].removeprefix("refs/heads/")
    head = (f"Публикация харнеса: пакет {short} «{title[:SUBJECT_LIMIT]}» → "
            f"{short_address(subject['address'])}, ветка {branch}; сейчас там {subject['remote_tip'][:7]}.")
    listed, count = [], 0
    since = published_since(subject)
    if since:
        try:
            span = f"{since}..{sha}"
            count = int(git_lines(pack, "rev-list", "--count", span)[0])
            listed = git_lines(pack, "log", f"--max-count={LIST_LIMIT}", "--format=%h %s", span)
            head += f"\nС прошлой публикации {count} {commits_word(count)} пакета:"
        except (ReadError, IndexError, ValueError):
            listed, count = [], 0
    unsent = unsent_in_build(subject)
    if unsent:
        head += f"\nВ сборке {unsent} неотправленных {commits_word(unsent)} — уедут вместе."
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    return fit_text(head, listed, count, f"Почему: {why}\nКнопка действует {ttl_phrase(ttl)}.")


def spec_text(request: dict, secrets_dir: str, ttl: int) -> str:
    task_id, _, digest = str(request["цель"]).partition(" ")
    try:
        # Владельцу показывается ТА редакция, к которой привязана кнопка.
        if door_subjects.spec_subject(task_id) != str(request["цель"]):
            raise ReadError(f"спека {task_id} правлена после запроса")
        edited = dt.datetime.fromtimestamp(door_subjects.spec_file(task_id).stat().st_mtime)
    except (push_subject.PushSubjectError, OSError) as err:
        raise ReadError(str(err)) from None
    title = str(request.get("почему") or "")[:WHY_LIMIT]
    return (f"Одобрить спеку {task_id} — {title}?\nРедакция {digest[:8]}, правлена {edited:%Y-%m-%d %H:%M}.\n"
            f"Кнопка действует {ttl_phrase(ttl)}.")


def layer_text(request: dict, secrets_dir: str, ttl: int) -> str:
    field, _, value = str(request["цель"]).partition("=")
    why = str(request.get("почему") or "")[:WHY_LIMIT]
    return (f"Записать в личность этой копии: {field} = «{value[:VALUE_LIMIT]}»?\n"
            f"Почему: {why}\nКнопка действует {ttl_phrase(ttl)}.")


# Текст по виду: ключи — ВИДЫ_СОГЛАСИЯ.
TEXT = {"удаление": delete_text, "пуш": push_text, "выкат": deploy_text,
        "публикация": publish_text, "спека": spec_text, "снос-базы": drop_text,
        "перепись": rewrite_text, "слой-человека": layer_text}


def message_text(request: dict, secrets_dir: str, ttl: int) -> str:
    return TEXT[request.get("вид", owner_word.ВИД_ПО_УМОЛЧАНИЮ)](request, secrets_dir, ttl)


def keyboard(nonce: str) -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": "Да", "callback_data": f"c:{nonce}:y"},
        {"text": "Нет", "callback_data": f"c:{nonce}:n"}]]}, ensure_ascii=False)


def unsent(log: Path, secrets_dir: str) -> list[dict]:
    buttons = read_rows(log / BUTTONS)
    out, seen = [], set()
    for request in read_rows(log / REQUESTS):
        nonce = request.get("nonce")
        kind = request.get("вид", owner_word.ВИД_ПО_УМОЛЧАНИЮ)
        ttl = owner_word.срок_кнопки_мин(kind)
        if not nonce or not request.get("цель") or nonce in seen:
            continue
        seen.add(nonce)
        age = minutes_since(request.get("ts"))
        if last_sent(buttons, nonce) or decided(buttons, nonce) or age is None or age > ttl:
            continue
        if kind not in owner_word.ВИДЫ_СОГЛАСИЯ:
            print(f"запрос {nonce[:8]}: незнакомый вид «{kind}» — не отправлен", file=sys.stderr)
            continue
        try:
            text = message_text(request, secrets_dir, ttl)
        except ReadError as err:
            print(f"запрос {nonce[:8]} (вид {kind}) не отправлен: {err}", file=sys.stderr)
            continue
        out.append({"nonce": nonce, "target": f"{kind}:{request['цель']}",
                    "text": text, "reply_markup": keyboard(nonce)})
    return out


def split_target(target: str) -> tuple[str, str]:
    """"<вид>:<цель>" → (вид, цель). Без префикса (цель-путь начинается с «/») — удаление:
    так отдаёт target диспетчер прежней версии в окне смешанных версий."""
    if target.startswith("/"):
        return owner_word.ВИД_ПО_УМОЛЧАНИЮ, target
    kind, _, rest = target.partition(":")
    return kind, rest


def record_sent(log: Path, nonce: str, target: str, response: str) -> str | None:
    """None — записано; иначе причина, по которой «отправлено» не пишется.

    Цель приходит из того же чтения, по которому построен показанный текст:
    запись «отправлено» хранит именно то, что увидел владелец.
    """
    kind, goal = split_target(target)
    if kind not in owner_word.ВИДЫ_СОГЛАСИЯ or not goal:
        return f"target не разобран: вид «{kind}» не из ВИДЫ_СОГЛАСИЯ"
    try:
        body = json.loads(response)
        message_id = body["result"]["message_id"]
    except (ValueError, KeyError, TypeError):
        return "в ответе Bot API нет message_id"
    if body.get("ok") is not True or not isinstance(message_id, int):
        return "Bot API ответил не ok"
    append_row(log, "отправлено", {"nonce": nonce, "вид": kind, "цель": goal,
                                   "message_id": message_id, "from_id": None})
    return None


def reject(answer: str, why: str, edit: str | None = None) -> dict:
    return {"answer": answer, "log": f"нажатие отвергнуто: {why}", "edit": edit}


def judge(log: Path, owner_id: str, callback: dict) -> dict:
    """Вердикт для диспетчера: {answer, log, edit, message_id, chat_id}. Записывает решение."""
    sender = (callback.get("from") or {}).get("id")
    message = callback.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    if str(sender) != owner_id:
        return reject("Это не твой запрос", f"чужой from.id={sender}")
    if str(chat) != owner_id:
        return reject("Это не твой запрос", f"чужой chat.id={chat}")
    found = CALLBACK_RE.match(str(callback.get("data") or ""))
    if not found:
        return reject("Не понял нажатие", "callback_data не по шаблону")
    nonce, answer = found.groups()
    rows = read_rows(log / BUTTONS)
    sent = last_sent(rows, nonce)
    if sent is None:
        return reject("Запрос не найден", f"по nonce {nonce[:8]} нет «отправлено»")
    message_id = message.get("message_id")
    if str(message_id) != str(sent.get("message_id")):
        return reject("Нажми в последнем сообщении", f"nonce {nonce[:8]}: сообщение {message_id} не последнее")
    if decided(rows, nonce):
        return reject("Уже решено", f"nonce {nonce[:8]}: решение уже записано")
    age = minutes_since(sent.get("ts"))
    if age is None or age > owner_word.срок_кнопки_мин(sent.get("вид", owner_word.ВИД_ПО_УМОЛЧАНИЮ)):
        return reject("Запрос устарел — попроси заново", f"nonce {nonce[:8]}: срок кнопки вышел",
                      edit="Запрос устарел, решение не записано.")
    event = DECISIONS[answer]
    # Вид и цель из «отправлено» (охраняемый носитель), а не из журнала запросов: его сессия может переписать.
    append_row(log, event, {"nonce": nonce, "вид": sent.get("вид", owner_word.ВИД_ПО_УМОЛЧАНИЮ),
                            "цель": sent["цель"],
                            "message_id": message_id, "from_id": sender})
    return {"answer": "Принято" if event == "да" else "Отказ записан",
            "log": f"решение «{event}» записано, nonce {nonce[:8]}",
            "edit": f"Ответ записан: {event}."}


def main(argv: list[str]) -> int:
    command, log = argv[1], Path(argv[2])
    if command == "unsent":
        for row in unsent(log, argv[3]):
            print(json.dumps(row, ensure_ascii=False))
    elif command == "sent":
        why = record_sent(log, argv[3], argv[4], sys.stdin.read())
        if why:
            print(why)
            return 1
    elif command == "judge":
        try:
            callback = json.loads(sys.stdin.read())
        except ValueError:
            callback = {}
        print(json.dumps(judge(log, argv[3], callback if isinstance(callback, dict) else {}),
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
