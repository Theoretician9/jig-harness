#!/usr/bin/env python3
"""test_button_press_wakes_shift.py — пробы задачи hr.button-press-wakes-shift.

Живой случай 07.11 04:49: владелец нажал «Да» на две спеки, смена узнала об этом
только из его вопроса. Входящее сообщение будит смену (agent_kick), нажатие кнопки —
нет. Договор: судья consent_buttons.judge отдаёт в вердикте поле "kick" — текст
пинка смене — на КАЖДОЕ нажатие владельца (записанное «да»/«нет» и отвергнутое с
причиной); текст называет нажатое, вид и цель из «отправлено» и nonce[:8]. Чужому
нажатию kick нет. Диспетчер в handle_callback: kick непуст → agent_kick "$kick".

Стенд — Stand из test_consent_buttons.py: живой tg-dispatcher.sh на своём LOG_DIR,
своём порту подставного Bot API и своём каталоге во /tmp. Пинок ловит подставной
tmux стенда (bin/tmux в PATH диспетчера): каждый вызов ложится строкой в
<стенд>/tmux-calls, текст пинка — в строке `send-keys … -l -- <текст>`. Боевой
tmux не вызывается вовсе. Что перехват жив, проба доказывает до проб кнопок:
сообщение владельца обязано дать пинок (иначе ФИКСТУРА, а не красная проба).

Двери для копии (мутацию ставят в копию scripts/, не в живой файл):
  BUTTON_KICK_DISPATCHER — проверяемый tg-dispatcher.sh (по умолчанию проектный);
  BUTTON_KICK_LIB        — каталог lib с consent_buttons.py для юнита судьи
                           (по умолчанию lib рядом с диспетчером). Диспетчер берёт
                           судью из своего $HERE/lib, поэтому lib обязан совпасть с
                           ним — иначе ФИКСТУРА.

    python3 scripts/test_button_press_wakes_shift.py [--случай <имя>]...
Случаи: юнит да нет отказ чужой секрет.
Код возврата: 0 — «падений 0», 1 — красная (или отказ фикстуры: строка «ФИКСТУРА»).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_consent_buttons as tcb  # noqa: E402
from selftest_cases import Cases  # noqa: E402

DISPATCHER = Path(os.environ.get("BUTTON_KICK_DISPATCHER") or ROOT / "scripts" / "tg-dispatcher.sh").resolve()
LIB = Path(os.environ.get("BUTTON_KICK_LIB") or DISPATCHER.parent / "lib").resolve()
OWNER, STRANGER = tcb.OWNER, tcb.STRANGER
KICK_WAIT_S = 10
# Столько ждём после того, как нажатие обработано: пинок уходит фоном (kick_async),
# и «нет пинка» чужому или «ровно один» владельцу без паузы были бы ложными.
SILENCE_S = 4
SENT_REPLY = '{"ok":true,"result":{"message_id":%d}}'
KICK_MARK = " -l -- "
CASE_NAMES = ("юнит", "да", "нет", "отказ", "чужой", "секрет")
# Договор вердикта с диспетчером: контекст судьи (nonce, «отправлено», решение) наружу не идёт.
VERDICT_KEYS = {"answer", "log", "edit", "kick"}

cases = Cases()
check = cases.check


def word(text: str, w: str) -> bool:
    """Слово целиком: «да» не должно находиться в «отдан» или «задача»."""
    return re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text) is not None


# ── юнит судьи ──────────────────────────────────────────────────────────────
def judge_run(log: Path, callback: dict) -> dict:
    done = subprocess.run(["python3", str(LIB / "consent_buttons.py"), "judge", str(log), str(OWNER)],
                          input=json.dumps(callback, ensure_ascii=False), capture_output=True,
                          text=True, timeout=30)
    try:
        verdict = json.loads(done.stdout)
    except ValueError:
        verdict = {"ошибка": (done.stdout + done.stderr)[-300:]}
    return verdict if isinstance(verdict, dict) else {}


def sent_run(log: Path, nonce: str, target: str, message_id: int) -> int:
    return subprocess.run(["python3", str(LIB / "consent_buttons.py"), "sent", str(log), nonce, target],
                          input=SENT_REPLY % message_id, capture_output=True, text=True,
                          timeout=30).returncode


def callback(nonce: str, answer: str, message_id: int, from_id: int = OWNER, chat_id: int = OWNER,
             data: str | None = None) -> dict:
    return {"id": "cb-unit", "from": {"id": from_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
            "data": data if data is not None else f"c:{nonce}:{answer}"}


def probe_unit(yard: tcb.Yard) -> None:
    print("— д. юнит судьи: kick в вердикте")
    log = yard.root / "unit" / "log"
    log.mkdir(parents=True)
    goal = str(yard.root / "unit" / "staraja-kopija.dump")
    yes_n, no_n, lost_n = (secrets.token_urlsafe(16) for _ in range(3))
    check("д) условие: «отправлено» записано писателем судьи (да, нет)", (0, 0),
          (sent_run(log, yes_n, f"удаление:{goal}", 5), sent_run(log, no_n, f"удаление:{goal}", 6)))

    # Чужие — первыми: запрос ещё не решён, отказ только по чужому id.
    for label, cb in (("чужой from.id", callback(yes_n, "y", 5, from_id=STRANGER)),
                      ("чужой chat.id", callback(yes_n, "y", 5, chat_id=STRANGER))):
        verdict = judge_run(log, cb)
        check(f"д) {label}: вердикт есть, kick нет, служебных ключей нет", (True, "", set()),
              ("answer" in verdict, verdict.get("kick") or "", set(verdict) - VERDICT_KEYS))

    verdict = judge_run(log, callback(yes_n, "y", 5))
    kick = str(verdict.get("kick") or "")
    check("д) владелец «Да»: служебных ключей в вердикте нет", set(), set(verdict) - VERDICT_KEYS)
    check("д) владелец «Да»: kick называет «да», вид, цель, nonce[:8]; «нет» в нём нет",
          (True, True, True, True, False),
          (word(kick, "да"), "удаление" in kick, goal in kick, yes_n[:8] in kick, word(kick, "нет")))
    verdict = judge_run(log, callback(no_n, "n", 6))
    kick = str(verdict.get("kick") or "")
    check("д) владелец «Нет»: kick называет «нет», вид, цель, nonce[:8]; «да» в нём нет",
          (True, True, True, True, False),
          (word(kick, "нет"), "удаление" in kick, goal in kick, no_n[:8] in kick, word(kick, "да")))

    # Отвергнутые нажатия владельца: в пинке «отвергнуто», причина судьи и nonce[:8].
    for label, cb, nonce, reason in (
            ("уже решено", callback(yes_n, "n", 5), yes_n, "уже записано"),
            ("не последнее сообщение", callback(no_n, "y", 4), no_n, "не последнее"),
            ("не найден", callback(lost_n, "y", 7), lost_n, "нет «отправлено»"),
            ("не по шаблону", callback("", "", 5, data="c:битое"), "", "не по шаблону")):
        kick = str(judge_run(log, cb).get("kick") or "")
        check(f"д) владелец, отказ «{label}»: kick с «отвергнуто», причиной и nonce[:8]",
              (True, True, True), ("отвергнуто" in kick, reason in kick, nonce[:8] in kick))


# ── стенд: пинок через живой диспетчер ──────────────────────────────────────
def kicks(st: tcb.Stand) -> list[str]:
    """Тексты, набранные в панель подставным tmux (send-keys -l -- <текст>)."""
    out = []
    for line in st.text("tmux-calls").splitlines():
        if line.startswith("send-keys") and KICK_MARK in line:
            out.append(line.split(KICK_MARK, 1)[1])
    return out


def kicks_naming(st: tcb.Stand, nonce: str | None, since: int) -> list[str]:
    return [k for k in kicks(st)[since:] if nonce and nonce[:8] in k]


def wait_kick(st: tcb.Stand, nonce: str | None, since: int) -> list[str]:
    # Пинок уходит фоном: второй, лишний, приходит позже первого, и «ровно один»
    # без паузы после первого был бы зелёным на двойном пинке.
    if tcb.wait_for(lambda: bool(kicks_naming(st, nonce, since)), KICK_WAIT_S):
        time.sleep(SILENCE_S)
    return kicks_naming(st, nonce, since)


def old_backup(st: tcb.Stand, tag: str) -> Path:
    """Цель «удаление», которую ask-consent примет и чьё имя печатать можно: старая копия."""
    path = st.backups / f"{tag}-{secrets.token_hex(3)}.dump"
    path.write_text("x", encoding="utf-8")
    long_ago = time.time() - 40 * 86400
    os.utime(path, (long_ago, long_ago))
    return path


def prove_interception(st: tcb.Stand) -> None:
    """Перехват жив: сообщение владельца даёт пинок в подставной tmux стенда."""
    before = len(kicks(st))
    st.say_owner(f"проба перехвата пинка {secrets.token_hex(3)}")
    if not tcb.wait_for(lambda: len(kicks(st)) > before, KICK_WAIT_S):
        tcb.fixture_fail("сообщение владельца не дало пинка в подставной tmux стенда — "
                         "перехват не жив, пробы кнопок судить нечем", st)
    print(f"  СТЕНД: перехват жив — пинок о сообщении владельца пойман ({st.dir}/tmux-calls)")


def pressed(st: tcb.Stand, tag: str, answer: str) -> tuple[str | None, Path, list[str]]:
    target = old_backup(st, tag)
    nonce, mid = st.ask_sent(target)
    check(f"{tag}) условие: запрос отправлен, message_id есть", True, mid is not None)
    since = len(kicks(st))
    st.press(tcb.yes_data(nonce, answer), mid) if nonce else None
    event = tcb.YES if answer == "y" else tcb.NO
    check(f"{tag}) условие: решение записано", [event], [r.get("event") for r in st.records(nonce)])
    return nonce, target, wait_kick(st, nonce, since)


def probe_yes(st: tcb.Stand) -> None:
    print("— а. «Да» владельца будит смену")
    nonce, target, got = pressed(st, "а", "y")
    kick = got[0] if got else ""
    check("а) пинок о нажатии дошёл до панели ровно один раз", 1, len(got))
    check("а) пинок называет «да», вид, цель, nonce[:8]; «нет» в нём нет",
          (True, True, True, True, False),
          (word(kick, "да"), "удаление" in kick, str(target.resolve()) in kick,
           bool(nonce) and nonce[:8] in kick, word(kick, "нет")))


def probe_no(st: tcb.Stand) -> None:
    print("— б. «Нет» владельца будит смену")
    nonce, target, got = pressed(st, "б", "n")
    kick = got[0] if got else ""
    check("б) пинок о нажатии дошёл до панели ровно один раз", 1, len(got))
    check("б) пинок называет «нет», вид, цель, nonce[:8]; «да» в нём нет",
          (True, True, True, True, False),
          (word(kick, "нет"), "удаление" in kick, str(target.resolve()) in kick,
           bool(nonce) and nonce[:8] in kick, word(kick, "да")))


def probe_refused(st: tcb.Stand) -> None:
    print("— в. отвергнутое нажатие владельца будит смену с причиной")
    target = old_backup(st, "в1")
    nonce, mid = st.ask_sent(target)
    st.press(tcb.yes_data(nonce), mid) if nonce else None
    check("в1) условие: первое «Да» записано", [tcb.YES], [r.get("event") for r in st.records(nonce)])
    wait_kick(st, nonce, 0)
    since = len(kicks(st))
    st.press(tcb.yes_data(nonce, "n"), mid) if nonce else None
    got = wait_kick(st, nonce, since)
    kick = got[0] if got else ""
    check("в1) уже решено: один пинок с «отвергнуто», причиной, целью и nonce[:8]",
          (1, True, True, True), (len(got), "отвергнуто" in kick, "уже записано" in kick,
                                  str(target.resolve()) in kick))

    ttl = tcb.machine_ttl()
    target = old_backup(st, "в2")
    nonce, mid = st.ask_sent(target)
    check("в2) условие: запрос отправлен", True, mid is not None)
    if nonce and mid is not None:
        tcb.age_sent(st, nonce, ttl + 5)
    since = len(kicks(st))
    st.press(tcb.yes_data(nonce), mid) if nonce else None
    check("в2) условие: решения нет (срок вышел)", [], st.records(nonce))
    got = wait_kick(st, nonce, since)
    kick = got[0] if got else ""
    check("в2) устарел: один пинок с «отвергнуто», «срок», целью и nonce[:8]",
          (1, True, True, True), (len(got), "отвергнуто" in kick, "срок" in kick,
                                  str(target.resolve()) in kick))


def probe_stranger(st: tcb.Stand) -> None:
    print("— г. чужое нажатие смену не будит")
    target = old_backup(st, "г")
    nonce, mid = st.ask_sent(target)
    check("г) условие: запрос отправлен", True, mid is not None)
    for label, kw in (("from.id", {"from_id": STRANGER}), ("chat.id", {"chat_id": STRANGER})):
        since, said = len(kicks(st)), st.text("log/tg-dispatcher.log").count(f"чужой {label}")
        st.press(tcb.yes_data(nonce), mid, **kw) if nonce else None
        handled = tcb.wait_for(lambda: st.text("log/tg-dispatcher.log").count(f"чужой {label}") > said)
        time.sleep(SILENCE_S)
        check(f"г) чужой {label}: диспетчер отверг нажатие, записи нет, пинков в панель нет",
              (True, [], []), (handled, st.records(nonce), kicks(st)[since:]))


def probe_secret(st: tcb.Stand) -> None:
    # И-3: имя файла секрета не печатается — сообщение владельцу его прячет, пинок тоже обязан.
    print("— е. пинок о секрете не печатает имя файла")
    name = f"p-kick-kljuch-{secrets.token_hex(4)}.env"
    target = st.secret(name)
    nonce, mid = st.ask_sent(target)
    check("е) условие: запрос отправлен", True, mid is not None)
    since = len(kicks(st))
    st.press(tcb.yes_data(nonce), mid) if nonce else None
    got = wait_kick(st, nonce, since)
    check("е) пинок о «Да» на секрет дошёл; имени файла нет ни в одном вызове tmux",
          (1, False), (len(got), name in st.text("tmux-calls")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--случай", dest="only", action="append", choices=CASE_NAMES)
    only = set(parser.parse_args().only or CASE_NAMES)
    if not DISPATCHER.is_file() or not (LIB / "consent_buttons.py").is_file():
        tcb.fixture_fail(f"нет {DISPATCHER} или {LIB}/consent_buttons.py")
    if (DISPATCHER.parent / "lib").resolve() != LIB:
        tcb.fixture_fail(f"диспетчер {DISPATCHER} берёт судью из {DISPATCHER.parent / 'lib'}, "
                         f"а юнит — из {LIB}: мутация одной копии судилась бы другой")
    print(f"  ПРЕДМЕТ: диспетчер {DISPATCHER}, судья {LIB}/consent_buttons.py")
    tcb.DISPATCHER = DISPATCHER                       # Stand запускает диспетчер по этому имени
    tcb.PREFIX = "/tmp/proba-button-kick-"            # свой каталог и своя уборка стендов
    yard = tcb.Yard()
    if "юнит" in only:
        probe_unit(yard)
    stand_runs = [(n, f) for n, f in (("да", probe_yes), ("нет", probe_no), ("отказ", probe_refused),
                                      ("чужой", probe_stranger), ("секрет", probe_secret)) if n in only]
    stand = None
    if stand_runs:
        stand = tcb.Stand(yard)
        prove_interception(stand)
        for _name, run in stand_runs:
            run(stand)
    if stand and not cases.ok:
        stand.show()
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
