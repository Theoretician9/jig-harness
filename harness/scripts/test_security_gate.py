#!/usr/bin/env python3
"""Пробы гейта закрытия шага «безопасность» (check-pipeline.py) — ДО кода.

Контракт: pretask.yaml (reasons_verbatim, probe_cases), план — Task 3.
Судится СЛОВО причины: каждый красный случай даёт ровно свою причину и никакой
другой. Стенд — свой git-репозиторий, журналы печатей и скилов — во временном
LOG_DIR, живой не трогается.

Договор интерфейса, которого проба требует от кода:
* security_debts(card, root) -> list[str]
* STEPS_JUDGED_AT_ANY_LEVEL = ("безопасность",), STEPS_NOT_NARROWED = (...)
* шаги_для_задачи(настройки, уровень, карточка, корень=None)
* проверить_закрытие(цена, настройки, корень=None)
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIPELINE = HERE / "check-pipeline.py"
SECURITY = HERE / "check-security.py"
TASK = "t.sec-gate"
PAPER = f".tasks/{TASK}/безопасность.yaml"

REASONS = {
    "none_read": "изменения задачи не прочитаны: git молчит",
    "no_file": "нет бумаги шага «безопасность»",
    "not_sealed": "файл не запечатан: писан не помощником или правлен после",
    "not_opus": "печать сделана не моделью opus",
    "no_skill": "нет вызова скила security-check тем же помощником",
    "stale": "бумага от прошлого кода: отпечаток не совпал",
    "class_unanswered": "класс задет, ответа нет",
    "where_bad": "место ответа вне задачи",
    "open_major": "находка без closed",
    "closed_foreign": "closed указывает не на коммит этой задачи",
    "too_many_runs": "прогонов больше max_runs_per_task — нужен ответ владельца",
}

RESULTS = []
_CLEAN = []
_MODS = {}


def load(name, path):
    if name not in _MODS:
        if not path.exists():
            raise AssertionError(f"нет модуля {path.name}")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return _MODS[name]


def pipeline():
    return load("check_pipeline_under_probe", PIPELINE)


def security():
    return load("check_security_under_probe", SECURITY)


def tmp(prefix="безопасность-стенд-"):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _CLEAN.append(path)
    return path


def git(root, *args, date=None):
    env = dict(os.environ)
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "core.quotepath=false", "-C", str(root), *args],
                          capture_output=True, text=True, env=env)
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr.strip()}")
    return done.stdout.strip()


def commit(root, files, task=TASK, date=None):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "правка\n\nDev-Map: " + task, date=date)
    return git(root, "rev-parse", "HEAD")


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def question_ids(cls):
    checklist = security().load_checklist()
    return [q["id"] for q in checklist["classes"][cls]["questions"]]


class Stand:
    """Репозиторий задачи, затрагивающей класс access, и журналы хуков."""

    def __init__(self, code=None, with_paper=True):
        self.root = tmp()
        self.log = tmp("безопасность-лог-")
        self.card = {"id": TASK, "status": "test"}
        git(self.root, "init", "-q")
        self.code = code or {"net.py": 'headers = {"Access-Control-Allow-Origin": "x"}\n'
                                       'requests.get(u, timeout=5)\n'}
        self.sha = commit(self.root, self.code)
        self.agent = "agent-aaa"
        self.model = "claude-opus-5-5"
        self.paper = {}
        if with_paper:
            self.write_paper()
            self.seal()
            self.skill()

    def fingerprint(self):
        return security().code_fingerprint(TASK, self.root)

    def good_paper(self):
        answers = [{"id": q, "verdict": "ok", "where": "net.py:1"}
                   for q in question_ids("access")]
        return {"code_fingerprint": self.fingerprint(),
                "checklist": "security-check-list.yaml@x",
                "classes": {"access": {"touched": True, "answers": answers}},
                "findings": [], "code_findings": [], "verdict": "clean"}

    def write_paper(self, data=None, raw=None):
        import yaml
        path = self.root / PAPER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None
                        else yaml.safe_dump(data or self.good_paper(), allow_unicode=True),
                        encoding="utf-8")

    def append(self, name, record):
        with open(self.log / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def seal(self, agent=None, model=None, sha=None, source="хук", ts="2026-10-25T10:00:00Z"):
        self.append("artifact-seals.jsonl", {
            "ts": ts, "file": PAPER, "sha256": sha or sha_of(self.root / PAPER),
            "agent": self.agent if agent is None else agent,
            "model": model or self.model, "источник": source})

    def skill(self, agent=None, ts="2026-10-25T09:59:00Z", source="хук", ok=True):
        self.append("skills.jsonl", {
            "ts": ts, "skill": "security-check", "task": TASK, "источник": source,
            "успех": ok, "агент": self.agent if agent is None else agent})

    def debts(self):
        os.environ["LOG_DIR"] = str(self.log)
        os.environ.pop("SECURITY_CHECKLIST", None)
        return pipeline().security_debts(self.card, self.root)


def only(debts, key):
    text = " | ".join(debts)
    assert REASONS[key] in text, f"нет причины «{REASONS[key]}»: {debts}"
    others = [k for k, v in REASONS.items() if k != key and v in text]
    assert not others, f"лишние причины {others}: {debts}"


def green(debts):
    assert debts == [], f"ждали зелёный, получили {debts}"


# ── C: гейт закрытия ────────────────────────────────────────────────────────
def c0():
    green(Stand().debts())


def c1():
    only(Stand(with_paper=False).debts(), "no_file")


def c2():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.skill()
    only(stand.debts(), "not_sealed")           # файл писан не помощником: печати нет


def c3():
    stand = Stand()
    (stand.root / PAPER).write_text((stand.root / PAPER).read_text(encoding="utf-8")
                                    + "# правлено после печати\n", encoding="utf-8")
    only(stand.debts(), "not_sealed")


def c3b():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.seal(agent="")
    stand.skill()
    only(stand.debts(), "not_sealed")           # печать смены без agent_id


def c3c_seal_not_by_hook():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.seal(source="рука")
    stand.skill()
    only(stand.debts(), "not_sealed")           # строка печати дописана не хуком


def c4():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.seal(model="claude-sonnet-5-5")
    stand.skill()
    only(stand.debts(), "not_opus")


def c5():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.seal()
    stand.skill(agent="agent-other")
    only(stand.debts(), "no_skill")


def c5b_skill_after_seal():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.seal()
    stand.skill(ts="2026-10-25T10:05:00Z")      # скил позван ПОСЛЕ печати бумаги
    only(stand.debts(), "no_skill")


def c5c_failed_skill():
    stand = Stand(with_paper=False)
    stand.write_paper()
    stand.seal()
    stand.skill(ok=False)
    only(stand.debts(), "no_skill")


def c6():
    stand = Stand()
    commit(stand.root, {"net.py": "requests.get(u, timeout=9)\n"})
    only(stand.debts(), "stale")


def c7():
    stand = Stand(with_paper=False)
    data = stand.good_paper()
    data["classes"]["access"]["answers"] = data["classes"]["access"]["answers"][:-1]
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    only(stand.debts(), "class_unanswered")


def tls_stand(answer_finding):
    """Код задачи даёт находку правила tls-off; бумага отвечает на неё или нет."""
    stand = Stand(code={"net.py": 'headers = {"Access-Control-Allow-Origin": "x"}\n'
                                  'requests.get(u, verify=False)\n'}, with_paper=False)
    data = stand.good_paper()
    if answer_finding:
        data["code_findings"] = [{"rule": "tls-off", "file": "net.py", "verdict": "ok"}]
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    return stand


def c7b_code_finding_without_answer():
    green(tls_stand(answer_finding=True).debts())       # находка есть и отвечена
    only(tls_stand(answer_finding=False).debts(), "class_unanswered")


def c8():
    stand = Stand(with_paper=False)
    data = stand.good_paper()
    data["classes"]["access"]["answers"][0]["where"] = "ghost.py:1"
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    only(stand.debts(), "where_bad")


def c8b():
    stand = Stand(with_paper=False)
    data = stand.good_paper()
    data["classes"]["access"]["answers"][0]["where"] = "net.py:999"
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    only(stand.debts(), "where_bad")


def c8c_where_on_foreign_file():
    stand = Stand(with_paper=False)
    commit(stand.root, {"other.py": "x = 1\n"}, task="t.other")   # файл есть, но не задачи
    data = stand.good_paper()
    data["classes"]["access"]["answers"][0]["where"] = "other.py:1"
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    only(stand.debts(), "where_bad")


def paper_with_finding(closed):
    stand = Stand(with_paper=False)
    data = stand.good_paper()
    finding = {"finding_id": "F1", "severity": "major", "location": "net.py:1",
               "issue": "x", "rule_ref": "CWE-942", "reproducer": "y"}
    if closed:
        finding["closed"] = closed(stand)
    data["findings"] = [finding]
    data["verdict"] = "has_findings"
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    return stand


def c9():
    only(paper_with_finding(None).debts(), "open_major")


def c10():
    stand = Stand(with_paper=False)
    # чужой коммит делается ДО бумаги: иначе он двигает отпечаток задачи
    foreign_sha = commit(stand.root, {"net.py": "requests.get(u, timeout=1)\n"}, task="t.other")
    data = stand.good_paper()
    data["findings"] = [{"finding_id": "F1", "severity": "major", "location": "net.py:1",
                         "issue": "x", "rule_ref": "CWE-942", "reproducer": "y",
                         "closed": foreign_sha}]
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    only(stand.debts(), "closed_foreign")


def c10b_closed_own_is_green():
    stand = Stand(with_paper=False)
    data = stand.good_paper()
    data["findings"] = [{"finding_id": "F1", "severity": "major", "location": "net.py:1",
                         "issue": "x", "rule_ref": "CWE-942", "reproducer": "y",
                         "closed": stand.sha}]
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    green(stand.debts())


def c10c_closed_own_commit_not_touching_location():
    stand = Stand(with_paper=False)
    own_sha = commit(stand.root, {"other.py": "x = 1\n"})        # свой коммит, но не net.py
    data = stand.good_paper()
    data["findings"] = [{"finding_id": "F1", "severity": "major", "location": "net.py:1",
                         "issue": "x", "rule_ref": "CWE-942", "reproducer": "y",
                         "closed": own_sha}]
    stand.write_paper(data)
    stand.seal()
    stand.skill()
    only(stand.debts(), "closed_foreign")


def c11():
    # M1 ревью r1: «прогон» — это помощник, а не печать. Четыре РАЗНЫХ агента
    # запечатали бумагу — четыре прогона; последний позвал скил сам.
    stand = Stand()
    for number, agent in enumerate(("agent-bbb", "agent-ccc", "agent-ddd"), 1):
        stand.skill(agent=agent, ts=f"2026-10-25T10:0{number}:00Z")
        stand.seal(agent=agent, ts=f"2026-10-25T10:0{number}:30Z")
    only(stand.debts(), "too_many_runs")


def c11b_one_helper_many_edits():
    # M1: один помощник — Write и три Edit своей бумаги — это ОДИН прогон.
    stand = Stand()
    for number in range(3):                     # итого четыре печати одного агента
        stand.seal(ts=f"2026-10-25T10:0{number + 1}:00Z")
    green(stand.debts())


def c16_rule_finding_without_trigger():
    # C1 ревью r1: правило кода нашло дыру, триггер класса молчит — бумага нужна.
    for code in ({"app/perm.sh": "chmod -R 755 /\n"},
                 {"app/db.py": "return cur.execute('SELECT a FROM t WHERE id=' + uid)\n"},
                 {"app/db.py": 'return cur.execute("SELECT a FROM t WHERE id=%d" % uid)\n'}):
        debts = Stand(code=code, with_paper=False).debts()
        assert debts and REASONS["no_file"] in " | ".join(debts), \
            f"находка правила в {sorted(code)} без бумаги прошла: {debts}"


def c17_token_in_task_commit_needs_paper():
    # C1+M2: токен ghp_ без слова-ключа в КОММИТЕ задачи — класс secrets задет
    # находкой правила, бумага нужна. Зелёный только после обеих починок.
    import secrets as _secrets
    import string as _string
    token = "ghp_" + "".join(_secrets.choice(_string.ascii_letters + _string.digits)
                             for _ in range(36))
    only(Stand(code={"app/conf.py": f'GH = "{token}"\n'}, with_paper=False).debts(), "no_file")


def c12():
    stand = Stand()
    commit(stand.root, {"dev-map.yaml": "a: 2\n", "STATE.md": "s\n", "docs/h.md": "h\n"})
    green(stand.debts())


def c13():
    stand = Stand(code={"style.css": "a{color:red}\n"}, with_paper=False)
    green(stand.debts())


def c15():
    stand = Stand(with_paper=False)
    shutil.rmtree(stand.root / ".git")
    debts = stand.debts()
    only(debts, "none_read")
    # Своё слово гейта, а не обёртка чтения правил («правила кода не прочитаны: …»): она несёт ту же фразу.
    assert debts == [REASONS["none_read"]], f"«git молчит» не своей причиной гейта: {debts}"


def settings(level_steps, enabled=None, characters=None):
    return {"шаги": level_steps, "шаги_включены": enabled or {},
            "характеры": characters or {}, "веса": {}, "пороги": {}, "строгость": {}}


def c14():
    mod = pipeline()
    cfg = settings({"обычная": ["тесты", "ревью_кода", "безопасность"]},
                   characters={"починка": {"шаги": ["тесты", "ревью_кода"]}})
    card = {"id": TASK, "глубина": {"характер": "починка"}}
    steps = mod.шаги_для_задачи(cfg, "обычная", card, tmp())
    assert "безопасность" in steps, steps


# ── D: дата включения ───────────────────────────────────────────────────────
def date_case(started, first_commit_date, level="крупная"):
    mod = pipeline()
    stand = Stand(code={"run.py": "subprocess.run(c, shell=True)\n"}, with_paper=False)
    if first_commit_date:
        git(stand.root, "commit", "-q", "--amend", "--no-edit", date=first_commit_date)
    card = {"id": TASK, "status": "test", "when": {"started": started}}
    cfg = settings({level: ["безопасность"]}, enabled={level: {"безопасность": "2026-10-20"}})
    steps = mod.шаги_для_задачи(cfg, level, card, stand.root)
    os.environ["LOG_DIR"] = str(stand.log)
    return mod.проверить(card, stand.root, steps)


def d1():
    assert date_case("2026-10-10", None) == []


def d2():
    debts = date_case("2026-10-22", None)
    assert any(REASONS["no_file"] in d for d in debts), debts


def d3():
    assert date_case(None, "2026-10-10T12:00:00") == []


def d3b_late_commit_without_started_is_judged():
    debts = date_case(None, "2026-10-23T12:00:00")
    assert any(REASONS["no_file"] in d for d in debts), debts


def d4():
    mod = pipeline()
    stand = Stand(code={"run.py": "subprocess.run(c, shell=True)\n"}, with_paper=False)
    card = {"id": TASK, "status": "test", "when": {"started": "2026-10-22"}}
    cfg = settings({"обычная": ["безопасность"]}, enabled={"обычная": {"безопасность": "2026-10-20"}})

    class Price:
        @staticmethod
        def признаки_задачи(id_задачи):
            return {"коммитов": 1}

        @staticmethod
        def цена(признаки, веса):
            return 1

        @staticmethod
        def уровень(значение, пороги):
            return "обычная"

    mod.закрываемые = lambda: [(TASK, card)]
    os.environ["LOG_DIR"] = str(stand.log)
    debts = mod.проверить_закрытие(Price, cfg, stand.root)
    assert any(REASONS["no_file"] in text for _, text in debts), debts


def d5_other_steps_still_skipped_below_large():
    mod = pipeline()
    stand = Stand(with_paper=False)
    card = {"id": TASK, "status": "test", "when": {"started": "2026-10-22"}}
    cfg = settings({"обычная": ["контракт", "безопасность"]})

    class Price:
        @staticmethod
        def признаки_задачи(id_задачи):
            return {"коммитов": 1}

        @staticmethod
        def цена(признаки, веса):
            return 1

        @staticmethod
        def уровень(значение, пороги):
            return "обычная"

    mod.закрываемые = lambda: [(TASK, card)]
    os.environ["LOG_DIR"] = str(stand.log)
    debts = mod.проверить_закрытие(Price, cfg, stand.root)
    assert not any("контракт" in text for _, text in debts), debts


def run_case(name, func):
    try:
        func()
        RESULTS.append((name, True))
        print(f"  ок    {name}")
    except Exception as err:        # noqa: BLE001
        RESULTS.append((name, False))
        print(f"  ПЛОХО {name}: {err or err.__class__.__name__}")


def main():
    cases = [
        ("C0 полный файл — зелёный", c0),
        ("C1 нет файла", c1),
        ("C2 нет печати (файл писан не помощником)", c2),
        ("C3 sha после печати не совпал", c3),
        ("C3b печать без agent (смена)", c3b),
        ("C3c печать записана не хуком", c3c_seal_not_by_hook),
        ("C4 модель печати не opus", c4),
        ("C5 скил позвал другой агент", c5),
        ("C5b скил позван после печати", c5b_skill_after_seal),
        ("C5c вызов скила неудачен", c5c_failed_skill),
        ("C6 отпечаток устарел (правка кода)", c6),
        ("C7 класс без ответа на вопрос", c7),
        ("C7b находка кода без ответа", c7b_code_finding_without_answer),
        ("C8 where на несуществующий файл", c8),
        ("C8b where на несуществующую строку", c8b),
        ("C8c where на файл не этой задачи", c8c_where_on_foreign_file),
        ("C9 открытый major без closed", c9),
        ("C10 closed на коммит чужой задачи", c10),
        ("C10b closed на свой коммит — зелёный", c10b_closed_own_is_green),
        ("C10c closed на свой коммит, не трогавший файл находки", c10c_closed_own_commit_not_touching_location),
        ("C11 четыре помощника — прогонов больше трёх", c11),
        ("C11b один помощник, четыре печати — один прогон, зелёный", c11b_one_helper_many_edits),
        ("C16 находка правила без триггера класса — нужна бумага", c16_rule_finding_without_trigger),
        ("C17 ghp_ в коммите задачи — нужна бумага (C1+M2)", c17_token_in_task_commit_needs_paper),
        ("C12 после бумаги только служебные файлы — не устарел", c12),
        ("C13 классов не задето — бумаги не требует", c13),
        ("C14 характер «починка» шаг не снимает", c14),
        ("C15 git молчит — красный none_read", c15),
        ("D1 начата до даты включения — зелёная", d1),
        ("D2 начата после даты, без бумаги — красная", d2),
        ("D3 без when.started, первый коммит до даты — зелёная", d3),
        ("D3b без when.started, первый коммит после даты — красная", d3b_late_commit_without_started_is_judged),
        ("D4 уровень «обычная» после даты — судится", d4),
        ("D5 прочие шаги ниже «крупной» по-прежнему не судятся", d5_other_steps_still_skipped_below_large),
    ]
    for name, func in cases:
        run_case(name, func)
    for path in _CLEAN:
        shutil.rmtree(path, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"ГЕЙТ-БЕЗОПАСНОСТЬ: случаев {len(RESULTS)}, неудач {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
