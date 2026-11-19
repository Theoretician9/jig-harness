#!/usr/bin/env python3
"""Проба: очередь слияния не гоняет pre-commit-gate.sh второй раз на том же дереве.

Откуда взята. Спека эталонного выката §3 строка 13, фаза 7: коммит уже прогнал
pre-commit-gate.sh (в среднем 2,37 раза на HEAD), а merge-queue.py стр. ~284 гонит его
снова на вершине ветки — 60 с медиана × 77 слияний за две недели ≈ 77 мин того же
дерева. Очередь остаётся вторым слоем против коммита мимо хука: пропуск законен только
при ЗЕЛЁНОЙ записи gates.jsonl с ТЕМ ЖЕ деревом, что развёрнуто в очереди.

Контракт, который судится:
  * журнал — `<LOG_DIR>/gates.jsonl` (LOG_DIR / HARNESS_LOG_DIR очереди), строки
    pre-commit-gate.sh: {gate:"pre-commit", verdict, tree, secs, …};
  * дерево очереди — `git write-tree` того, что развёрнуто во временном дереве (то же,
    что `<вершина>^{tree}` после rebase);
  * ПОСЛЕДНЯЯ строка gate "pre-commit" для пары (tree = дерево вершины, base = текущий
    master) — verdict "pass" → pre-commit-gate.sh не запускается (спека
    тяжёлых прогонов, раздел В: base — полный `git rev-parse HEAD` хука; в
    дереве очереди HEAD = master, значит своя строка очереди годится),
    `ворота_с` = 0, запись merge-queue.jsonl (data) называет «то же дерево», слияние идёт
    как обычно (код 0, ff);
  * записи нет / запись красная (verdict "fail") / дерево другое / master ушёл вперёд и
    rebase дал новое дерево / base строки — не текущий master (родитель вершины, старый
    master) / строка без base / после зелёной записи — красная / запись других ворот → ворота
    гоняются как сейчас (заглушка стенда видела прогон; она спит MQ_STUB_SLEEP=1,
    и `ворота_с` ≥ 1; «то же дерево» не названо).

Стенд — Stand из scripts/test_merge_queue.py (свой git-проект, пакет, LOG_DIR, заглушка
ворот, которая краснеет по файлу GATE_RED в дереве). Боевые журналы не трогаются.
Дверь мутаций: HARNESS_MERGE_QUEUE — путь механизма (мутировать КОПИЮ).

Самотест хука: `pre-commit-gate.sh --selftest-base` (случай т12) — base в строке журнала
равен HEAD, когда HEAD — коммит ветки, а не master (ревью-3 спеки, п. 12). Дверь мутаций
хука: HARNESS_PRE_COMMIT_GATE.

Прогон: python3 scripts/test_merge_queue_same_tree.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_merge_queue as mq  # noqa: E402

SAME_TREE = "то же дерево"
HOOK = Path(os.environ.get("HARNESS_PRE_COMMIT_GATE") or mq.HERE / "pre-commit-gate.sh")
MASTER = "master"   # base строки = текущий master стенда (то, что пишет хук в дереве очереди)


def record(s: mq.Stand, tree: str, verdict: str = "pass", gate: str = "pre-commit",
           base: str | None = MASTER) -> None:
    """Строка хука в gates.jsonl. base=None — строка старого вида, без поля base."""
    line = {"ts": "2026-11-16T00:00:00+00:00", "gate": gate, "head": "0000000",
            "staged": 1, "verdict": verdict, "failed": "" if verdict == "pass" else "x",
            "secs": 42, "tree": tree}
    if base is not None:
        line["base"] = s.sha() if base == MASTER else base
    with (s.logs / "gates.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def written_tree(where: Path) -> str:
    """Дерево индекса помощника сразу после коммита — то, что записал бы его хук."""
    return mq.git(where, "write-tree").strip()


def last_row(s: mq.Stand) -> dict:
    rows = s.rows()
    return rows[-1] if rows else {}


def names_same_tree(row: dict) -> bool:
    return SAME_TREE in json.dumps(row, ensure_ascii=False)


def gates_ran(s: mq.Stand, case: str, data: dict, row: dict) -> None:
    mq.check(f"{case}: заглушка ворот видела прогон", 1, len(s.gate_blocks()))
    mq.check(f"{case}: ворота_с — секунды прогона (заглушка спит 1 с)", True,
             isinstance(data.get("ворота_с"), (int, float)) and data["ворота_с"] >= 1)
    mq.check(f"{case}: запись не называет «{SAME_TREE}»", False, names_same_tree(row))


def case_same_tree_green(tmp: Path) -> None:
    # Поломка, которую ловит: очередь всё равно зовёт ворота. GATE_RED в дереве — улика:
    # прогон ворот дал бы код 3, пропуск — код 0.
    print("т1) зелёная запись того же дерева → ворота не гоняются")
    s = mq.Stand(tmp / "т1")
    branch, wt = s.tree("t1")
    tip = s.change(wt, "GATE_RED", "красная заглушка, если ворота пойдут\n")
    tree = written_tree(wt)
    record(s, tree)
    mq.check("т1: стенд — дерево записи = дерево вершины",
             mq.git(s.proj, "rev-parse", f"{tip}^{{tree}}").strip(), tree)
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    row = last_row(s)
    mq.check("т1: код 0 (слито без прогона)", 0, code)
    mq.check("т1: master == вершина ветки", tip, s.sha())
    mq.check("т1: заглушка ворот не запускалась", 0, len(s.gate_blocks()))
    mq.check("т1: ворота_с = 0", True, data.get("ворота_с") == 0 and data.get("ворота_с") is not None)
    mq.check(f"т1: запись mq.слито называет «{SAME_TREE}»", ("mq.слито", True),
             (row.get("event"), names_same_tree(row)))


def case_no_record(tmp: Path) -> None:
    # Поломка: пропуск без записи (журнал пуст — «нечего проверять» прочитано как зелёное).
    print("т2) записи нет → ворота гоняются")
    s = mq.Stand(tmp / "т2")
    branch, wt = s.tree("t2")
    s.change(wt, "a.txt", "a1\n")
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т2: код 0", 0, code)
    gates_ran(s, "т2", data, last_row(s))


def case_red_record(tmp: Path) -> None:
    # Поломка: вердикт не судится — красная запись того же дерева пропускает ворота.
    print("т3) красная запись того же дерева → ворота гоняются")
    s = mq.Stand(tmp / "т3")
    branch, wt = s.tree("t3")
    s.change(wt, "a.txt", "a1\n")
    record(s, written_tree(wt), verdict="fail")
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т3: код 0", 0, code)
    gates_ran(s, "т3", data, last_row(s))


def case_other_tree(tmp: Path) -> None:
    # Поломка: дерево не судится — любая зелёная запись пропускает ворота.
    print("т4) зелёная запись другого дерева → ворота гоняются, красные ворота не сливают")
    s = mq.Stand(tmp / "т4")
    branch, wt = s.tree("t4")
    other = written_tree(wt)                      # дерево master: не вершина ветки
    s.change(wt, "GATE_RED", "красная заглушка\n")
    record(s, other)
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т4: код 3 (ворота шли и красные)", 3, code)
    mq.check("т4: заглушка ворот видела прогон", 1, len(s.gate_blocks()))
    mq.check(f"т4: запись не называет «{SAME_TREE}»", False, names_same_tree(last_row(s)))


def case_master_moved(tmp: Path) -> None:
    # Поломка: очередь судит дерево коммита помощника, а не то, что развернула после
    # rebase: master ушёл, вершина стала новым деревом, которого хук не видел.
    print("т5) master ушёл после коммита помощника → rebase, новое дерево, ворота гоняются")
    s = mq.Stand(tmp / "т5")
    branch, wt = s.tree("t5")
    s.change(wt, "a.txt", "a1\n")
    record(s, written_tree(wt))
    s.change(s.proj, "b.txt", "b1\n", "master ушёл")
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т5: код 0", 0, code)
    mq.check("т5: слито с правкой master (b1) и ветки (a1)", ("a1\n", "b1\n"),
             (s.read("a.txt"), s.read("b.txt")))
    gates_ran(s, "т5", data, last_row(s))


def case_two_commits(tmp: Path) -> None:
    # В(б). Поломка «пропуск по зелёным коммитам»: хук второго коммита писал base =
    # родитель вершины (HEAD в момент коммита), строки с base = master нет. Ломка
    # «base = tip^» в checked_tree тоже оставила бы ворота пропущенными.
    print("т6) два коммита над master, зелёная строка хука с base = родитель → ворота гоняются")
    s = mq.Stand(tmp / "т6")
    branch, wt = s.tree("t6")
    s.change(wt, "a.txt", "a1\n")
    tip = s.change(wt, "GATE_RED", "красная заглушка: ворота шли\n")
    tree = written_tree(wt)
    record(s, tree, base=mq.git(s.proj, "rev-parse", f"{tip}^").strip())
    mq.check("т6: стенд — родитель вершины не master",
             True, mq.git(s.proj, "rev-parse", f"{tip}^").strip() != s.sha())
    mq.check("т6: стенд — дерево записи = дерево вершины",
             mq.git(s.proj, "rev-parse", f"{tip}^{{tree}}").strip(), tree)
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т6: код 3 (ворота шли и красные)", 3, code)
    gates_ran(s, "т6", data, last_row(s))


def case_red_after_green(tmp: Path) -> None:
    # Поломка: судится любая зелёная запись, а не последняя — хук пересудил то же
    # дерево красным, а очередь пропускает ворота по старой зелёной.
    print("т7) зелёная, затем красная запись того же дерева → ворота гоняются")
    s = mq.Stand(tmp / "т7")
    branch, wt = s.tree("t7")
    s.change(wt, "GATE_RED", "красная заглушка: ворота шли\n")
    tree = written_tree(wt)
    record(s, tree)
    record(s, tree, verdict="fail")
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т7: код 3 (ворота шли и красные)", 3, code)
    gates_ran(s, "т7", data, last_row(s))


def case_other_gate(tmp: Path) -> None:
    # Поломка: имя ворот не судится — зелёная запись другого прибора на то же дерево
    # пропускает ворота коммита.
    print("т8) зелёная запись того же дерева от других ворот → ворота гоняются")
    s = mq.Stand(tmp / "т8")
    branch, wt = s.tree("t8")
    s.change(wt, "GATE_RED", "красная заглушка: ворота шли\n")
    record(s, written_tree(wt), gate="gates")
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т8: код 3 (ворота шли и красные)", 3, code)
    gates_ran(s, "т8", data, last_row(s))


def case_two_commits_master_base(tmp: Path) -> None:
    # В(а). Поломка: осталось условие «один коммит над master». Строка с base = master и
    # деревом вершины (своя строка очереди или хук в дереве, где HEAD = master) — тот же
    # предмет проверки, что у очереди, и ворота второй раз не нужны.
    print("т9) два коммита над master, зелёная строка base = master и дерево вершины → ворота 0 с")
    s = mq.Stand(tmp / "т9")
    branch, wt = s.tree("t9")
    s.change(wt, "a.txt", "a1\n")
    tip = s.change(wt, "GATE_RED", "красная заглушка, если ворота пойдут\n")
    record(s, written_tree(wt))
    mq.check("т9: стенд — родитель вершины не master",
             True, mq.git(s.proj, "rev-parse", f"{tip}^").strip() != s.sha())
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т9: код 0 (слито без прогона)", 0, code)
    mq.check("т9: master == вершина ветки", tip, s.sha())
    mq.check("т9: заглушка ворот не запускалась", 0, len(s.gate_blocks()))
    mq.check("т9: ворота_с = 0", True, data.get("ворота_с") == 0 and data.get("ворота_с") is not None)
    mq.check(f"т9: запись называет «{SAME_TREE}»", True, names_same_tree(last_row(s)))


def case_old_master_base(tmp: Path) -> None:
    # В(в). Поломка: base не судится или судится «предок master» — строка, снятая против
    # старого master, пропускает ворота, хотя master с тех пор принёс другое.
    print("т10) один коммит, зелёная строка дерева вершины, base = старый master → ворота гоняются")
    s = mq.Stand(tmp / "т10")
    old = s.sha()
    s.change(s.proj, "b.txt", "b1\n", "master ушёл до ветки")
    branch, wt = s.tree("t10")
    s.change(wt, "GATE_RED", "красная заглушка: ворота шли\n")
    record(s, written_tree(wt), base=old)
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т10: код 3 (ворота шли и красные)", 3, code)
    gates_ran(s, "т10", data, last_row(s))


def case_no_base(tmp: Path) -> None:
    # В(г). Поломка: строка без base (хук до правки В) принята как годная — неизвестно,
    # против чего её судили.
    print("т11) один коммит, зелёная строка дерева вершины без base → ворота гоняются")
    s = mq.Stand(tmp / "т11")
    branch, wt = s.tree("t11")
    s.change(wt, "GATE_RED", "красная заглушка: ворота шли\n")
    record(s, written_tree(wt), base=None)
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLEEP="1")
    mq.check("т11: код 3 (ворота шли и красные)", 3, code)
    gates_ran(s, "т11", data, last_row(s))


def case_hook_writes_base(tmp: Path) -> None:
    # Хук. Поломка: pre-commit-gate.sh не пишет base или пишет `rev-parse master` вместо
    # HEAD — самотест держит HEAD на коммите ветки (≠ master), поэтому вторую ломку видно.
    print("т12) хук пишет base = HEAD (HEAD — коммит ветки, не master)")
    proc = subprocess.run(["bash", str(HOOK), "--selftest-base"], capture_output=True,
                          text=True, errors="replace", cwd=tmp, timeout=120,
                          env={**os.environ, **mq.GIT_ENV})
    tail = " | ".join((proc.stdout + proc.stderr).strip().splitlines()[-3:])
    mq.check(f"т12: самотест хука --selftest-base зелёный ({tail})", 0, proc.returncode)


CASES = (case_same_tree_green, case_no_record, case_red_record, case_other_tree,
         case_master_moved, case_two_commits, case_red_after_green, case_other_gate,
         case_two_commits_master_base, case_old_master_base, case_no_base,
         case_hook_writes_base)


def main() -> int:
    if not mq.QUEUE.exists():
        print(f"нет {mq.QUEUE}: случаи идут, механизма нет — красные")
    with tempfile.TemporaryDirectory(prefix="proba-mq-same-tree-") as raw:
        for case in CASES:
            try:
                case(Path(raw))
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                mq.checked += 1
                mq.failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    if not mq.checked:
        print("SELFTEST: КРАСНЫЙ (ни одной проверки)")
        return 1
    print(f"SELFTEST: {'зелёный' if not mq.failed else 'КРАСНЫЙ'} ({mq.checked} проверок, "
          f"{len(CASES)} случаев, неудач {mq.failed})")
    return 1 if mq.failed else 0


if __name__ == "__main__":
    sys.exit(main())
