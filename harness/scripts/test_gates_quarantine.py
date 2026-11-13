#!/usr/bin/env python3
"""Пробы суда итога ворот с карантином (scripts/gates-quarantine.py) — до кода.

Договор — .tasks/hr.quarantine-red-checks/спека.md: суд читает вердикты прогона
из журнала gates-timing.jsonl (строки gate.check с data.run == --прогон), файл
карантина и карту; печатает «=== карантин: красных всего M, в карантине K, новых
красных N, бед карантина F ===» и выходит кодом min(N+F, 255). Нет файла или
пуст список — молчит и выходит кодом min(M, 255), как ворота до карантина.

Каждый случай строит своё условие в своём временном каталоге: свой карантин,
своя карта, свой журнал прогона. Боевые данные не читаются. «Сегодня» пробы —
далёкий день, передаётся аргументом --сегодня, и все даты записей считаются от
него: суд, который взял бы системные часы, проваливает случай просрочки.

Прогон (только фоном): python3 scripts/test_gates_quarantine.py
Итог: «КАРАНТИН ВОРОТ: случаев N, неудач M», код 0/1.
"""
import json
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGE = HERE / "gates-quarantine.py"
RUN = "2026-11-13T02:00:00+05:00 4242"
OTHER_RUN = "2026-11-12T02:00:00+05:00 4141"
TODAY = date(2030, 1, 10)
CARD = "hr.qr-probe-card"
LINE_ID = "ln.qr-probe"
MILESTONE_ID = "ms.qr-probe"


def day(shift: int) -> str:
    return (TODAY + timedelta(days=shift)).isoformat()


def journal_line(name: str, outcome: str, run: str = RUN) -> str:
    rc = 0 if outcome == "ok" else 1
    return json.dumps({"v": 1, "ts": "2026-11-13T02:00:01+05:00", "src": "scripts/gates.sh",
                       "event": "gate.check", "outcome": outcome,
                       "data": {"name": name, "run": run, "secs": 0.1, "rc": rc}},
                      ensure_ascii=False)


def entry(gate: str, card: str | None = CARD, made: str = day(-1),
          due: str = day(6)) -> str:
    lines = [f'  - гейт: "{gate}"']
    if card is not None:
        lines.append(f"    карточка: {card}")
    lines += [f'    заведено: "{made}"', f'    срок: "{due}"', '    причина: "проба"']
    return "\n".join(lines)


def judge(stand: Path, entries: list[str] | None, verdicts: list[str], red: int,
          need_summary: bool = True):
    """entries None — файла карантина нет вовсе. Код ≠ 0 от упавшего суда
    иначе сошёл бы за «красный», потому суд обязан существовать и, если
    карантин не пуст, печатать строку итога (кроме случая, где суд роняют)."""
    assert JUDGE.is_file(), f"суда нет: {JUDGE}"
    quarantine = stand / "gates-quarantine.yaml"
    if entries is not None:
        quarantine.write_text("карантин:\n" + "\n".join(entries) + ("\n" if entries else " []\n"),
                              encoding="utf-8")
    devmap = stand / "dev-map.yaml"
    # Карта устроена как настоящая: id есть у вех, линий и задач; карточка — только задача.
    devmap.write_text(f"version: 1\nmilestones:\n  - id: {MILESTONE_ID}\nlines:\n"
                      f"  - id: {LINE_ID}\n    tasks:\n"
                      f"      - id: {CARD}\n        status: plan\n        title: проба\n",
                      encoding="utf-8")
    journal = stand / "gates-timing.jsonl"
    journal.write_text("".join(v + "\n" for v in verdicts), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(JUDGE), "--карантин", str(quarantine), "--карта", str(devmap),
         "--журнал", str(journal), "--прогон", RUN, "--красных", str(red), "--сегодня", TODAY.isoformat()],
        capture_output=True, text=True, timeout=60)
    out = proc.stdout + proc.stderr
    if entries and need_summary:
        assert summary(out), f"нет строки «=== карантин …» (код {proc.returncode}):\n{out}"
    return proc.returncode, out


def summary(out: str) -> str:
    return next((l for l in out.splitlines() if l.startswith("=== карантин")), "")


def case_a_fresh_quarantine_green(stand):
    rc, out = judge(stand, [entry("след доставки")],
                    [journal_line("след доставки", "refused"), journal_line("секреты", "ok")], 1)
    assert rc == 0, f"красный под свежим карантином дал код {rc}:\n{out}"
    line = summary(out)
    assert "красных всего 1" in line and "в карантине 1" in line and "новых красных 0" in line, \
        f"строка итога карантина не та: «{line}»\n{out}"


def case_b_new_red_outside(stand):
    rc, out = judge(stand, [entry("след доставки")],
                    [journal_line("след доставки", "refused"), journal_line("секреты", "refused")], 2)
    assert rc != 0, f"новый красный вне карантина, а код 0:\n{out}"
    assert "новых красных 1" in summary(out), f"«новых красных 1» нет: «{summary(out)}»\n{out}"


def case_c_expired(stand):
    rc, out = judge(stand, [entry("след доставки", made=day(-8), due=day(-1))],
                    [journal_line("след доставки", "refused")], 1)
    assert rc != 0, f"просроченный карантин прикрыл красный, код 0:\n{out}"
    assert "в карантине 0" in summary(out), f"просроченный посчитан в карантине: «{summary(out)}»"


def case_d_longer_than_week(stand):
    rc, out = judge(stand, [entry("след доставки", made=day(-1), due=day(7))],
                    [journal_line("след доставки", "refused")], 1)
    assert rc != 0, f"срок 8 дней от заведения принят, код 0:\n{out}"
    assert "в карантине 0" in summary(out), f"запись длиннее недели прикрыла: «{summary(out)}»"


def case_e_card_not_in_map(stand):
    rc, out = judge(stand, [entry("след доставки", card="hr.no-such-card")],
                    [journal_line("след доставки", "refused")], 1)
    assert rc != 0, f"карточки нет в карте, а код 0:\n{out}"
    assert "hr.no-such-card" in out, f"беда не называет карточку:\n{out}"
    rc2, out2 = judge(stand, [entry("след доставки", card=None)],
                      [journal_line("след доставки", "refused")], 1)
    assert rc2 != 0, f"запись без карточки прикрыла красный, код 0:\n{out2}"


def case_f_green_under_quarantine(stand):
    rc, out = judge(stand, [entry("quotepath")],
                    [journal_line("quotepath", "ok"), journal_line("секреты", "ok")], 0)
    assert rc == 0, f"зелёный гейт в карантине покраснел, код {rc}:\n{out}"
    assert "карантин можно снять" in out and "quotepath" in out, f"не сказано «можно снять»:\n{out}"


def case_g_no_file_as_before(stand):
    verdicts = [journal_line("секреты", "refused"), journal_line("устройство", "refused")]
    for entries, label in ((None, "файла нет"), ([], "список пуст")):
        rc, out = judge(stand, entries, verdicts, 2)
        assert rc == 2, f"{label}: код {rc}, а до карантина был бы 2:\n{out}"
        assert "карантин" not in out, f"{label}: суд заговорил, а должен молчать:\n{out}"


def case_h_other_run_ignored(stand):
    rc, out = judge(stand, [entry("след доставки")],
                    [journal_line("след доставки", "refused", run=OTHER_RUN),
                     journal_line("секреты", "refused")], 1)
    assert rc != 0, f"красный ЧУЖОГО прогона прикрыл красный этого, код 0:\n{out}"
    assert "в карантине 0" in summary(out), f"чужой прогон в счёте: «{summary(out)}»"


def case_i_red_without_journal_line(stand):
    rc, out = judge(stand, [entry("след доставки")], [journal_line("след доставки", "refused")], 2)
    assert rc != 0, f"красный без строки журнала пропал, код 0:\n{out}"
    assert "красных всего 2" in summary(out) and "новых красных 1" in summary(out), \
        f"счёт не от RED ворот: «{summary(out)}»"


def case_j_due_today_valid(stand):
    rc, out = judge(stand, [entry("след доставки", made=day(-7), due=day(0))],
                    [journal_line("след доставки", "refused")], 1)
    assert rc == 0, f"срок = сегодня ещё годен, а код {rc}:\n{out}"
    assert "в карантине 1" in summary(out), f"запись со сроком сегодня не прикрыла: «{summary(out)}»"


def case_k_two_entries_one_gate(stand):
    rc, out = judge(stand, [entry("след доставки"), entry("след доставки")],
                    [journal_line("след доставки", "refused")], 1)
    assert rc != 0, f"две записи на гейт прикрыли красный, код 0:\n{out}"
    line = summary(out)
    assert "в карантине 0" in line, f"дубль посчитан в карантине: «{line}»"
    assert "две записи" in out, f"беда дубля не названа:\n{out}"
    assert "бед карантина 1" in line, f"дубль — одна беда на гейт: «{line}»"


def case_l_bad_without_new_red(stand):
    rc, out = judge(stand, [entry("quotepath"), entry("устройство", made=day(-8), due=day(-1))],
                    [journal_line("quotepath", "ok")], 0)
    line = summary(out)
    assert "новых красных 0" in line and "бед карантина 1" in line, f"счёт не тот: «{line}»"
    assert rc == 1, f"одна беда без новых красных — код 1, а {rc}:\n{out}"


def case_m_date_not_parsed(stand):
    rc, out = judge(stand, [entry("след доставки", due="не-дата")],
                    [journal_line("след доставки", "refused")], 1)
    assert rc != 0, f"срок «не-дата» прикрыл красный, код 0:\n{out}"
    assert "дата не разбирается" in out, f"беда даты не названа:\n{out}"


def case_n_judge_crash_is_red(stand):
    rc, out = judge(stand, [entry("след доставки")], ["[]"], 0, need_summary=False)
    assert rc == 1, f"суд упал, а код {rc}:\n{out}"
    assert "суд карантина упал" in out, f"падение суда не названо:\n{out}"


def case_o_code_capped(stand):
    rc, out = judge(stand, None, [], 300)
    assert rc == 255, f"файла нет, M=300: код {rc}, а не 255:\n{out}"
    rc, out = judge(stand, [entry("след доставки")], [journal_line("след доставки", "refused")], 300)
    assert rc == 255, f"карантин, M=300: код {rc}, а не 255:\n{out}"


def case_p_count_mismatch(stand):
    rc, out = judge(stand, [entry("след доставки"), entry("устройство")],
                    [journal_line("след доставки", "refused"), journal_line("устройство", "refused")], 1)
    assert rc != 0, f"под карантином больше, чем красных у ворот, а код 0:\n{out}"
    assert "счёт расходится" in out, f"расхождение счёта не названо:\n{out}"
    assert "новых красных -" not in summary(out), f"отрицательное число новых: «{summary(out)}»"
    assert "бед карантина 1" in summary(out), f"расхождение не посчитано бедой: «{summary(out)}»"


def case_q_card_is_task_only(stand):
    for card in (LINE_ID, MILESTONE_ID):
        rc, out = judge(stand, [entry("след доставки", card=card)],
                        [journal_line("след доставки", "refused")], 1)
        assert rc != 0, f"id не задачи ({card}) принят за карточку, код 0:\n{out}"
        assert "карточки нет в карте" in out, f"{card}: беда не та:\n{out}"


def case_r_torn_journal_line_skipped(stand):
    torn = journal_line("секреты", "refused")[:25]
    rc, out = judge(stand, [entry("след доставки")], [torn, journal_line("след доставки", "refused")], 1)
    assert rc == 0, f"оборванная строка журнала уронила суд, код {rc}:\n{out}"
    assert "в карантине 1" in summary(out), f"после оборванной строки счёт не тот: «{summary(out)}»"


CASES = [
    ("а) свежий карантин → зелёный", case_a_fresh_quarantine_green),
    ("б) новый красный вне карантина", case_b_new_red_outside),
    ("в) просроченный карантин красный", case_c_expired),
    ("г) срок > 7 дней красный", case_d_longer_than_week),
    ("д) карточки нет в карте / нет поля", case_e_card_not_in_map),
    ("е) зелёный в карантине: можно снять", case_f_green_under_quarantine),
    ("ж) нет файла / пусто: как раньше", case_g_no_file_as_before),
    ("з) чужой прогон не в счёт", case_h_other_run_ignored),
    ("и) красный без строки журнала — новый", case_i_red_without_journal_line),
    ("к) срок = сегодня ещё годен", case_j_due_today_valid),
    ("л) две записи на гейт — одна беда", case_k_two_entries_one_gate),
    ("м) беда без нового красного — код 1", case_l_bad_without_new_red),
    ("н) дата не разбирается — беда", case_m_date_not_parsed),
    ("о) суд упал — код 1", case_n_judge_crash_is_red),
    ("п) код не выше 255", case_o_code_capped),
    ("р) счёт расходится — беда", case_p_count_mismatch),
    ("с) карточка — только id задачи", case_q_card_is_task_only),
    ("т) оборванная строка журнала пропускается", case_r_torn_journal_line_skipped),
]


def main() -> int:
    failed = 0
    for title, case in CASES:
        with tempfile.TemporaryDirectory(prefix="gates-quarantine-probe-") as tmp:
            try:
                case(Path(tmp))
                print(f"  ок    {title}")
            except AssertionError as err:
                failed += 1
                print(f"  ПЛОХО {title}: {err}")
    print(f"КАРАНТИН ВОРОТ: случаев {len(CASES)}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
