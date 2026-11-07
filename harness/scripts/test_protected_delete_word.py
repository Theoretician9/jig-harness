#!/usr/bin/env python3
"""test_protected_delete_word.py — дверь protected-delete.py: текст владельца её не открывает, кнопка «Да» — да.

Находка ревью спеки hr.consent-buttons: дверь брала слово «глагол + предмет» («удали
ключ») без названной цели, и 60 минут им удалялся ЛЮБОЙ одиночный файл в секретах.
Слово владельца 05.11.2026 («все вопросы по поводу моего Да — кнопками») снимает
вопрос целиком: текст дверь не открывает никогда, открывает только «Да» кнопкой
на realpath файла (owner_word.судить_кнопкой).

Случаи:
  1 — любой текст (без пути, с путём, голое «сноси» после нашего вопроса) — закрыто,
      отказ называет кнопку;
  2 — «Да» кнопкой на файл открывает ровно его, не соседа; копия снята перед удалением;
  3 — «Нет» кнопкой и текст-отзыв после «Да» закрывают;
  4 — «Да» старше срока кнопки (15 мин) и повторное «Да» на воссозданный файл — закрыто.

Каждый случай — свой стенд во временном каталоге: секреты, копии, журналы,
паспорт установки; боевые каталоги не читаются (ключ стенда owner_word).
Дверь берётся рядом с пробой; PROTECTED_DELETE_SCRIPT подставляет копию двери
с мутацией (копию дерева scripts/ целиком: дверь тянет библиотеки рядом).

    python3 scripts/test_protected_delete_word.py
Код возврата: 0 — «падений 0», 1 — красная.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DELETE = Path(os.environ.get("PROTECTED_DELETE_SCRIPT") or ROOT / "scripts" / "protected-delete.py")
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from selftest_cases import Cases  # noqa: E402

WINDOW_MIN = 60
TTL_MIN = 15

cases = Cases()
check = cases.check


def now_iso(ago_s: float = 0) -> str:
    """Время в форме `date -Is`, как его пишет диспетчер в inbox.jsonl."""
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)).isoformat(timespec="seconds")


# Владелец стенда: судья засчитывает «Да» только с этим from_id (TG_CHAT_ID конфига стенда).
ВЛАДЕЛЕЦ = 4242


class Door:
    """Свой стенд двери: каталоги и паспорт создаёт сам, слова и нажатия кладёт проба."""

    serial = 0

    def __init__(self, yard: Path, name: str) -> None:
        Door.serial += 1
        d = self.dir = yard / f"{Door.serial:02d}-{name}"
        self.log, self.secrets, self.backups = d / "log", d / "secrets", d / "backups"
        for p in (self.log, self.secrets, self.backups):
            p.mkdir(parents=True)
        # Пустой журнал входящих: его отсутствие — «прочитать не смогли», это
        # другой отказ, и проба прошла бы по чужой причине.
        (self.log / "inbox.jsonl").write_text("", encoding="utf-8")
        (d / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nSECRETS_DIR="{self.secrets}"\nBACKUP_DIR="{self.backups}"\n'
            f'TG_CHAT_ID="{ВЛАДЕЛЕЦ}"\n', encoding="utf-8")
        (d / "harness.conf").write_text(
            f"CONFIRM_MAX_AGE_MIN={WINDOW_MIN}\nCONSENT_BUTTON_TTL_MIN={TTL_MIN}\n", encoding="utf-8")
        self.env = dict(os.environ, HARNESS_HOME=str(d), HARNESS_INSTALL_CONF=str(d / "install.conf"),
                        HARNESS_CONF=str(d / "harness.conf"), LOG_DIR=str(self.log),
                        SECRETS_DIR=str(self.secrets), HARNESS_OWNER_WORD_STEND="1",
                        CONFIRM_MAX_AGE_MIN=str(WINDOW_MIN), CONSENT_BUTTON_TTL_MIN=str(TTL_MIN))
        self.message_id = 7000

    def file(self, name: str) -> Path:
        path = self.secrets / name
        path.write_text(f"содержимое {name}", encoding="utf-8")
        return path

    def say(self, text: str, ago_s: float) -> None:
        """Слово владельца в журнале диспетчера (с message_id — признаком авторства)."""
        self.message_id += 1
        with open(self.log / "inbox.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(ago_s), "message_id": str(self.message_id),
                                 "chat_id": "1000000001", "text": text}, ensure_ascii=False) + "\n")

    def press(self, event: str, target: Path, ago_s: float, kind: str = "удаление") -> None:
        """Нажатие кнопки на realpath файла: конверт, как его пишет consent_buttons.judge, с from_id
        владельца; просьба ask-consent — на минуту раньше (живой порядок)."""
        self.message_id += 1
        data = {"nonce": f"n{self.message_id}", "вид": kind, "цель": os.path.realpath(target),
                "message_id": self.message_id, "from_id": ВЛАДЕЛЕЦ}
        asked = {"nonce": data["nonce"], "вид": kind, "цель": data["цель"], "почему": "проба"}
        with open(self.log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": now_iso(ago_s + 60), "src": "scripts/ask-consent.py",
                                 "event": "consent.request", "outcome": "ok", "data": asked},
                                ensure_ascii=False) + "\n")
        with open(self.log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": now_iso(ago_s), "src": "scripts/lib/consent_buttons.py",
                                 "event": event, "outcome": "ok", "data": data}, ensure_ascii=False) + "\n")

    def yes(self, target: Path, ago_s: float = 60) -> None:
        self.press("consent.yes", target, ago_s)

    def ask(self, target: Path, ago_s: float = 120) -> None:
        """Просьба ask-consent об удалении файла без нажатия: владельца спросили, а «Да» он дал другому."""
        asked = {"nonce": f"ask{ago_s}{target.name}", "вид": "удаление", "цель": os.path.realpath(target),
                 "почему": "проба"}
        with open(self.log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": now_iso(ago_s), "src": "scripts/ask-consent.py",
                                 "event": "consent.request", "outcome": "ok", "data": asked},
                                ensure_ascii=False) + "\n")

    def delete(self, target: Path) -> tuple[int, bool]:
        """(код двери, цел ли файл после)."""
        return self.run(target)[0:2]

    def run(self, target: Path) -> tuple[int, bool, str]:
        done = subprocess.run(["python3", str(DELETE), str(target)],
                              capture_output=True, text=True, env=self.env, timeout=60)
        out = (done.stdout + done.stderr).strip()
        if done.returncode not in (0, 1):
            print(f"    дверь ответила {done.returncode}: {out[:200]}")
        return done.returncode, target.exists(), out


CLOSED = (1, True)
OPENED = (0, False)


def probe_text_never_opens(yard: Path) -> None:
    print("— 1. текст владельца дверь не открывает — закрыта, отказ называет кнопку")
    for text in ("удали ключ", "Удали ключ", "удали файл", "снеси секрет",
                 "Удаляй этот снимок из секретов", "сноси", "удаляй", "удаляй к вечеру"):
        d = Door(yard, "text")
        target = d.file("chuzhoj.key")
        d.say(text, 120)
        code, alive, out = d.run(target)
        check(f"1) «{text}» без кнопки — отказ, файл цел", (CLOSED, True), ((code, alive), "кнопк" in out.lower()))
    # Текст с названным путём цели — тоже не разрешение.
    d = Door(yard, "text-path")
    target = d.file("a.key")
    d.say(f"удали ключ {target}", 120)
    check("1) «удали ключ <путь>» без кнопки — отказ, файл цел", CLOSED, d.delete(target))


def probe_button(yard: Path) -> None:
    print("— 2. «Да» кнопкой на файл — открыто ровно на него")
    d = Door(yard, "button")
    target, neighbour = d.file("staryj.key"), d.file("sosed.key")
    prefix_neighbour = d.file("staryj.key2")
    # Про соседей владельца спрашивали: отказ обязан дать сверка цели «Да», а не суд парной просьбы.
    d.ask(neighbour)
    d.ask(prefix_neighbour)
    d.yes(target)
    check("2) «Да» на staryj.key — сосед sosed.key не открыт", CLOSED, d.delete(neighbour))
    check("2) «Да» на staryj.key — сосед по префиксу имени не открыт", CLOSED, d.delete(prefix_neighbour))
    check("2) «Да» на staryj.key — удалён (контроль законной двери)", OPENED, d.delete(target))
    copies = list((d.backups / "секреты-удалённые").glob("staryj.key.*"))
    check("2) копия снята перед удалением", ["содержимое staryj.key"],
          [c.read_text(encoding="utf-8") for c in copies])
    d = Door(yard, "other-kind")
    target = d.file("a.key")
    d.ask(target)
    d.press("consent.yes", target, 60, kind="выкат")
    check("2) «Да» другого вида (выкат) на тот же путь — отказ, файл цел", CLOSED, d.delete(target))


def probe_closers(yard: Path) -> None:
    print("— 3. «Нет» кнопкой и текст-отзыв после «Да» закрывают")
    d = Door(yard, "no")
    target = d.file("a.key")
    d.yes(target, 180)
    d.press("consent.no", target, 60)
    code, alive, out = d.run(target)
    check("3) «Нет» после «Да» — отказ, файл цел, «нажал „Нет“ в»", (CLOSED, True),
          ((code, alive), "нажал „нет“ в" in out.lower()))
    d = Door(yard, "revoke")
    target = d.file("a.key")
    d.yes(target, 180)
    d.say("стоп, не удаляй этот файл", 60)
    check("3) отзыв текстом после «Да» — отказ, файл цел", CLOSED, d.delete(target))
    d = Door(yard, "revoke-subject")
    target = d.file("a.key")
    d.yes(target, 180)
    d.say("этот файл пока не удаляй", 60)
    check("3) отзыв предметом вида («файл» + «не») после «Да» — отказ, файл цел", CLOSED, d.delete(target))
    d = Door(yard, "word-before-yes")
    target = d.file("a.key")
    d.say("стоп, не удаляй этот файл", 300)
    d.yes(target, 60)
    check("3) отзыв ДО «Да» не мешает: новое «Да» открывает", OPENED, d.delete(target))


def probe_stale(yard: Path) -> None:
    print(f"— 4. «Да» старше срока кнопки ({TTL_MIN} мин) и повторное — закрыто")
    d = Door(yard, "stale")
    target = d.file("a.key")
    d.yes(target, (TTL_MIN + 5) * 60)
    check("4) «Да» старше срока — отказ, файл цел", CLOSED, d.delete(target))
    d = Door(yard, "repeat")
    target = d.file("a.key")
    d.yes(target, 120)
    check("4) первое удаление по «Да» — открыто", OPENED, d.delete(target))
    again = d.file("a.key")
    check("4) тот же файл воссоздан, то же «Да» — отказ (одно «Да» — одно удаление)", CLOSED, d.delete(again))


def main() -> int:
    yard = Path(tempfile.mkdtemp(prefix="proba-protected-delete-word-")).resolve()
    atexit.register(shutil.rmtree, yard, True)
    print(f"дверь: {DELETE}")
    for probe in (probe_text_never_opens, probe_button, probe_closers, probe_stale):
        probe(yard)
    if cases.paths == 0:
        print("ПРОБА КРАСНАЯ: ни одного пути не прогнано")
        return 1
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
