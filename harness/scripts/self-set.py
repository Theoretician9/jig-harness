#!/usr/bin/env -S python3 -I
# -*- coding: utf-8 -*-
"""Дверь слоя человека: harness/self/instance.yaml пишется только по кнопке «Да».

    python3 scripts/self-set.py <поле> <значение> [--источник вопрос|контекст] [--почему "зачем"]
    python3 scripts/self-set.py --старт

Поле — «копия», «роль», «цель», «человек» или «раздел.ключ» (проект.описание).
Модель только предлагает: без нажатия «Да» на предмет «<поле>=<значение>» файл остаётся
побайтно прежним, а владельцу уходит кнопка вида «слой-человека» (ask-consent.py).
Записывает код — следующим вызовом, когда «Да» уже в журнале кнопок.

Коды выхода:  0 — записано (или это же значение уже стоит);
              3 — не записано, ждём «Да» (кнопка попрошена или уже ушла);
              4 — владелец нажал «Нет» на это значение: не записано и не просится;
              2 — отказ двери (нет файла, журнал кнопок не прочитан, значение не встало).

Журнал $LOG_DIR/self-set.jsonl: поле, значение, источник (вопрос|контекст), состояние
(задан|да|нет), код_запроса; у «да» — sha256 файла после записи и хэши полей. Старт смены
(`--старт`, зовёт session_state.sh) сверяет файл с последним «да»: правка мимо двери —
строка «слой 2 изменён мимо согласия: <поля>»; пустые обязательные поля — один вопрос
в сутки.

Двери проб: SELF_INSTANCE (файл слоя 2), MEMORY_LAYERS (файл слоёв).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import events  # noqa: E402
import owner_word  # noqa: E402

KIND = "слой-человека"
WRITTEN, REFUSED_DOOR, WAITING, REFUSED = 0, 2, 3, 4
JOURNAL_FILE = owner_word.ЖУРНАЛ_СЛОЯ_ЧЕЛОВЕКА
SRC = "scripts/self-set.py"
EVENT = "self-set.state"
QUESTION_PERIOD = dt.timedelta(hours=24)
BYPASS_LINE = "слой 2 изменён мимо согласия:"
CODE_SHOWN = 8


class DoorError(Exception):
    """Дверь не может судить или писать; текст — причина для сессии."""


def instance_path() -> Path:
    return Path(os.environ.get("SELF_INSTANCE") or ROOT / "harness" / "self" / "instance.yaml")


def layers_path() -> Path:
    return Path(os.environ.get("MEMORY_LAYERS") or ROOT / "harness" / "config" / "memory-layers.yaml")


def load_instance(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise DoorError(f"{path} не прочитан: {error}") from None
    if not isinstance(data, dict):
        raise DoorError(f"{path}: верхний уровень — не словарь")
    return data


def value_at(data: dict, field: str):
    for part in field.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def is_blank(value) -> bool:
    return value is None or not str(value).strip()


# ── журнал ───────────────────────────────────────────────────────────────────

def journal_rows() -> list[dict]:
    """Записи self-set.jsonl: поля конверта events.emit раскрыты из data."""
    path = Path(events.log_dir()) / JOURNAL_FILE
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
        if isinstance(row, dict):
            data = row.get("data") if row.get("v") == 1 and isinstance(row.get("data"), dict) else row
            rows.append({**data, "ts": row.get("ts")})
    return rows


def record(field: str, value: str, source: str, state: str, **extra) -> bool:
    data = {"поле": field, "значение": value, "источник": source, "состояние": state, **extra}
    return events.emit(SRC, EVENT, "refused" if state == "нет" else "ok", journal="self-set", data=data)


def field_hashes(data: dict) -> dict:
    """Короткий отпечаток каждого верхнего поля: по нему видно, КАКОЕ поле правили мимо двери."""
    return {str(key): hashlib.sha256(json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)
                                     .encode("utf-8")).hexdigest()[:12] for key, val in data.items()}


# ── правка текста файла (комментарии живут, поэтому не yaml.dump) ────────────

def render_scalar(value: str) -> str:
    text = yaml.safe_dump(value, allow_unicode=True, width=10 ** 9, default_flow_style=True)
    return text.removesuffix("\n...\n").rstrip("\n")


def block_end(lines: list[str], start: int, indent: int) -> int:
    """Конец блока ключа: следом идут строки глубже его отступа и пустые; хвостовые пустые не его."""
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def find_key(lines: list[str], key: str, lo: int, hi: int, indent: int) -> int | None:
    pattern = re.compile(rf"^{' ' * indent}{re.escape(key)}\s*:(?:\s|$)")
    return next((i for i in range(lo, hi) if pattern.match(lines[i])), None)


def key_line(key: str, indent: int, rendered: str, old_line: str | None) -> str:
    """Строка «ключ: значение»; комментарий после прежнего однострочного значения сохраняется."""
    comment = ""
    if old_line is not None:
        rest = old_line.split(":", 1)[1]
        found = None if re.search(r"[\"']", rest) else re.match(r"^.*?(\s+#.*)$", rest)
        comment = found.group(1) if found else ""
    return f"{' ' * indent}{key}: {rendered}{comment}"


def insertion_point(lines: list[str]) -> int:
    """Место нового раздела: после последней строки, не пустой и не комментария в нулевой колонке."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() and not lines[i].startswith("#"):
            return i + 1
    return 0


def set_field_text(text: str, field: str, value: str) -> str:
    lines = text.split("\n")
    rendered = render_scalar(value)
    head, _, sub = field.partition(".")
    start = find_key(lines, head, 0, len(lines), 0)
    if not sub:
        if start is None:
            at = insertion_point(lines)
            lines[at:at] = [key_line(head, 0, rendered, None)]
        else:
            lines[start:block_end(lines, start, 0)] = [key_line(head, 0, rendered, lines[start])]
        return "\n".join(lines)
    if start is None:
        at = insertion_point(lines)
        lines[at:at] = [f"{head}:", key_line(sub, 2, rendered, None)]
        return "\n".join(lines)
    end = block_end(lines, start, 0)
    inline = lines[start].split(":", 1)[1].split("#")[0].strip()
    if inline:  # «проект: {}» и подобное — раздел пересоздаётся
        lines[start:end] = [f"{head}:", key_line(sub, 2, rendered, None)]
        return "\n".join(lines)
    child = find_key(lines, sub, start + 1, end, 2)
    if child is None:
        lines[end:end] = [key_line(sub, 2, rendered, None)]
    else:
        lines[child:block_end(lines, child, 2)] = [key_line(sub, 2, rendered, lines[child])]
    return "\n".join(lines)


def write_atomic(path: Path, text: str) -> None:
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        shutil.copymode(path, name)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


# ── суд кнопки и просьба ─────────────────────────────────────────────────────

def ask_button(subject: str, why: str) -> tuple[int, str]:
    """→ (код программы просьбы, код запроса или текст отказа)."""
    code, answer = owner_word.попросить_кнопку(KIND, subject, why, журнал_просьбы=Path(events.log_dir()))
    if code == 0:  # ответ — «… код <первые 8 знаков>»
        return 0, answer.rsplit(" ", 1)[-1]
    return code, answer


def set_field(field: str, value: str, source: str, why: str) -> int:
    path = instance_path()
    data = load_instance(path)
    head = field.split(".")[0]
    if field.count(".") > 1 or (isinstance(data.get(head), dict) and "." not in field):
        raise DoorError(f"поле «{field}»: пишется скалярное поле или «раздел.ключ»")
    if str(value_at(data, field) or "").strip() == value.strip():
        print(f"{field}: это значение уже стоит")
        return WRITTEN
    subject = f"{field}={value}"
    try:
        decision = owner_word.последнее_решение(KIND, subject)
        if decision and decision["событие"] == "нет":
            if not any(r.get("поле") == field and r.get("значение") == value and r.get("состояние") == "нет"
                       for r in journal_rows()):
                record(field, value, source, "нет")
            print(f"{field}: владелец нажал «Нет» на это значение — не записано и не прошу снова")
            return REFUSED
        opened = owner_word.согласие_кнопкой(KIND, subject)
    except owner_word.НетНосителя as error:
        raise DoorError(f"журнал кнопок не прочитан: {error}") from None
    if opened is None:
        return ask_and_wait(field, value, subject, source, why)
    return write_field(path, field, value, source)


def ask_and_wait(field: str, value: str, subject: str, source: str, why: str) -> int:
    code, answer = ask_button(subject, why)
    if code == 0:
        record(field, value, source, "задан", код_запроса=answer[:CODE_SHOWN])
        print(f"{field}: не записано — владельцу ушла кнопка «Да / Нет», код {answer[:CODE_SHOWN]}; "
              "после «Да» повторите эту же команду")
        return WAITING
    if code == owner_word.КОД_ПОВТОРА:
        print(f"{field}: не записано — {answer}")
        return WAITING
    raise DoorError(f"кнопка не попрошена: {answer}")


def write_field(path: Path, field: str, value: str, source: str) -> int:
    old = path.read_text(encoding="utf-8")
    new = set_field_text(old, field, value)
    try:
        stands = str(value_at(yaml.safe_load(new) or {}, field) or "").strip() == value.strip()
    except yaml.YAMLError:
        stands = False
    if not stands:
        raise DoorError(f"значение «{field}» не встало в файл читаемо; файл не тронут")
    write_atomic(path, new)
    digest = hashlib.sha256(new.encode("utf-8")).hexdigest()
    if not record(field, value, source, "да", sha256=digest, поля=field_hashes(yaml.safe_load(new))):
        raise DoorError("поле записано, но журнал self-set.jsonl его не принял")
    print(f"{field}: записано по кнопке «Да»")
    rebuild_cards()
    return WRITTEN


def rebuild_cards() -> None:
    """Карточки помощников несут instance: после правки они устарели (сторож запуска откажет).
    Дверь SELF_INSTANCE — проба с чужим файлом: карточки живого дерева она не трогает."""
    if os.environ.get("SELF_INSTANCE"):
        return
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "self-prompt.py"), "--карточки", "--в",
                           str(ROOT / ".claude" / "agents")], capture_output=True, text=True)
    if done.returncode not in (0, 3):
        print(f"ВНИМАНИЕ: карточки помощников не пересобраны: {done.stderr.strip()[-200:]}")


# ── старт смены ──────────────────────────────────────────────────────────────

def bypass_fields(rows: list[dict], path: Path) -> list[str] | None:
    """Поля, изменённые мимо двери, — или None (сверять не с чем или всё сходится)."""
    agreed = [r for r in rows if r.get("состояние") == "да" and r.get("sha256")]
    if not agreed or not path.is_file():
        return None
    last = agreed[-1]
    if hashlib.sha256(path.read_bytes()).hexdigest() == last["sha256"]:
        return None
    saved = last.get("поля") or {}
    now = field_hashes(load_instance(path))
    changed = sorted(key for key in set(saved) | set(now) if saved.get(key) != now.get(key))
    return changed or ["текст вне полей (комментарии)"]


def required_fields() -> list[str]:
    try:
        layers = yaml.safe_load(layers_path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [str(f) for f in layers.get("обязательные_поля_человека") or []]


def asked_recently(rows: list[dict]) -> bool:
    border = dt.datetime.now().astimezone() - QUESTION_PERIOD
    for row in rows:
        if row.get("источник") == "вопрос" and row.get("состояние") == "задан":
            try:
                if dt.datetime.fromisoformat(str(row.get("ts"))) >= border:
                    return True
            except ValueError:
                continue
    return False


def start_lines() -> list[str]:
    """Строки для смены: правка мимо двери и один вопрос о пробеле."""
    path = instance_path()
    rows = journal_rows()
    lines = []
    changed = bypass_fields(rows, path)
    if changed:
        lines.append(f"⚠ {BYPASS_LINE} {', '.join(changed)} — файл правили не через "
                     "scripts/self-set.py; скажи владельцу, он решает, что оставить")
    if path.is_file() and not asked_recently(rows):
        data = load_instance(path)
        gaps = [f for f in required_fields() if is_blank(value_at(data, f))]
        if gaps:
            record(gaps[0], "", "вопрос", "задан")
            lines.append(f"❓ слой 2 (instance.yaml): не заполнено «{gaps[0]}» (все пробелы: {', '.join(gaps)}). "
                         f"Спроси владельца в канале ОДИН вопрос про «{gaps[0]}», предложи значение и запиши: "
                         f"python3 scripts/self-set.py {gaps[0]} \"<значение>\" --источник вопрос "
                         "(владельцу придёт кнопка «Да / Нет»).")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="дверь слоя человека (instance.yaml)")
    parser.add_argument("field", nargs="?")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--источник", dest="source", choices=("вопрос", "контекст"), default="контекст")
    parser.add_argument("--почему", dest="why", default="")
    parser.add_argument("--старт", dest="start", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.start:
            lines = start_lines()
            if lines:
                print("\n".join(lines))
            return WRITTEN
        if not args.field or not args.value or not args.value.strip():
            parser.error("нужны <поле> и <значение>")
        log_dir = Path(events.log_dir())
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "self-set.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return set_field(args.field, args.value, args.source,
                             args.why or f"слой человека: {args.field}")
    except DoorError as error:
        print(f"self-set: отказ: {error}", file=sys.stderr)
        return REFUSED_DOOR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
