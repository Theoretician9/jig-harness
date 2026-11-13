#!/usr/bin/env python3
"""Проба: полное чтение помощником SKILL.md = вызов скила (задача
hr.security-skill-unreachable-for-helpers) — ДО кода.

Почему так. Гейт шага «безопасность» (check-pipeline.py, _seal_debts) ждёт в
skills.jsonl машинную запись security-check от того помощника, что запечатал
бумагу. Пишет её только хук skill_log.sh на инструмент Skill, а у
harness-worker этого инструмента нет — шаг помощнику недоступен. Решение
управляющего: Read помощником файла .claude/skills/<имя>/SKILL.md целиком
(без offset и limit) пишется хуком как вызов скила, со «способ»: «чтение».

Стенд — свой: временный LOG_DIR на каждый случай, хук зовётся настоящим
`bash skill_log.sh` с нагрузкой PostToolUse на stdin, гейт — настоящая функция
_seal_debts из check-pipeline.py. Живой журнал не трогается.

Проект стенда — тоже свой (PROJECT_DIR окружением: загрузчик конфига ставит
окружение старше файла, случай «о» это проверяет). Раскладка как в проекте:
.claude/skills/<имя> — symlink на ../../harness/skills/<имя>. Нужен потому, что
путь привязан к проекту: запись только если
  realpath(file_path) == realpath(PROJECT_DIR/.claude/skills/<имя>/SKILL.md),
  этот файл существует И realpath лежит под realpath(PROJECT_DIR).
Без привязки Read подложного /tmp/x/.claude/skills/security-check/SKILL.md
засчитывался шагом безопасности (случай «к»). Без третьего условия symlink
скила наружу проекта проходит равенство сам с собой (случаи «н», «н2»).
«Под» — с разделителем: иначе сосед /tmp/Xsuffix сходит за /tmp/X (случай «п»).

Прогон: python3 scripts/test_security_skill_by_read.py
"""
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Дверь SKILL_LOG_HOOK — для мутаций КОПИИ хука: живой файл не мутируется.
# Без неё — раскладка проекта (scripts/hooks) или пакета (harness/scripts + harness/hooks).
HOOK = (Path(os.environ["SKILL_LOG_HOOK"]) if os.environ.get("SKILL_LOG_HOOK") else
        next(p for p in (HERE / "hooks" / "skill_log.sh", HERE.parent / "hooks" / "skill_log.sh")
             if p.is_file()))
PIPELINE = HERE / "check-pipeline.py"
AGENT = "a1"
HOOK_TIMEOUT_SEC = 10

RESULTS = []
_CLEAN = []


def tmpdir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="ssu-probe-"))
    _CLEAN.append(path)
    return path


def make_stand() -> Path:
    """Проект стенда в раскладке проекта: скилы — symlink в harness/skills."""
    root = tmpdir()
    for name in ("security-check", "spec-writing"):
        real = root / "harness" / "skills" / name
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text(f"---\nname: {name}\n---\nтекст\n", encoding="utf-8")
        link = root / ".claude" / "skills" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(Path("..") / ".." / "harness" / "skills" / name)
    return root


def outside_skill(name: str) -> Path:
    """Настоящий каталог скила ВНЕ проекта стенда."""
    real = tmpdir() / name
    real.mkdir()
    (real / "SKILL.md").write_text(f"---\nname: {name}\n---\nподлог\n", encoding="utf-8")
    return real


STAND = None          # проект стенда; ставит main()
SKILL_MD = None       # законный путь SKILL.md в стенде; ставит main()


def read_payload(path, agent=AGENT, **extra_input):
    """Нагрузка Read в форме оболочки: tool_input + tool_response.file."""
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": path, **extra_input},
        "tool_response": {"type": "text", "file": {
            "filePath": path, "content": "---\nname: security-check\n---\nтекст\n",
            "numLines": 4, "startLine": 1, "totalLines": 4}},
        "session_id": "s1",
    }
    if agent is not None:
        payload["agent_id"] = agent
    return payload


def skill_payload(skill="security-check", agent=AGENT):
    # agent_id обязателен и здесь: без него хук зовёт переключение модели
    # панели оркестратора, а проба живую панель трогать не вправе.
    return {"hook_event_name": "PostToolUse", "tool_name": "Skill",
            "tool_input": {"skill": skill},
            "tool_response": {"success": True, "commandName": skill},
            "session_id": "s1", "agent_id": agent}


def run_hook(payload, log_dir: Path, task="hr.проба", project_dir="стенд"):
    """Зов хука; код возврата и время. Висящий хук убивается группой целиком:
    его дочерний python иначе остался бы читать FIFO вечно."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SKILLS_LOG", "SKILLS_LOG_NAME", "HARNESS_SKILL_PROBE")}
    env.update(LOG_DIR=str(log_dir), MODEL_SWITCH_STAMP=str(log_dir / "метка"))
    if task is None:
        env.pop("HARNESS_TASK", None)
    else:
        env["HARNESS_TASK"] = task
    env["PROJECT_DIR"] = str(STAND if project_dir == "стенд" else project_dir)
    started = time.monotonic()
    proc = subprocess.Popen(["bash", str(HOOK)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, start_new_session=True)
    try:
        proc.communicate(json.dumps(payload, ensure_ascii=False).encode(), timeout=HOOK_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None, time.monotonic() - started
    return proc.returncode, time.monotonic() - started


def records(log_dir: Path) -> list:
    path = log_dir / "skills.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def no_record(payload, why, project_dir="стенд"):
    log = tmpdir()
    code, _ = run_hook(payload, log, project_dir=project_dir)
    assert code == 0, f"хук вернул {code}"
    got = records(log)
    assert got == [], f"{why}: записано {got}"


def pipeline():
    os.environ.setdefault("LOG_DIR", str(tmpdir()))   # импорт не тянется к живому журналу
    spec = importlib.util.spec_from_file_location("check_pipeline_ssu", PIPELINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── случаи ────────────────────────────────────────────────────────────────────

def a_full_read_by_helper_is_skill_call():
    """БОЛЬНОЙ: полное чтение SKILL.md помощником — запись вызова скила."""
    log = tmpdir()
    code, _ = run_hook(read_payload(SKILL_MD), log)
    assert code == 0, f"хук вернул {code}"
    got = records(log)
    assert len(got) == 1, f"ждали одну запись, получили {len(got)}: {got}"
    r = got[0]
    want = {"skill": "security-check", "источник": "хук", "агент": AGENT,
            "успех": True, "способ": "чтение", "task": "hr.проба"}
    wrong = {k: (r.get(k), v) for k, v in want.items() if r.get(k) != v}
    assert not wrong, f"поля записи (было, ждали): {wrong}"


def b_gate_accepts_read_record():
    """БОЛЬНОЙ: гейт _seal_debts принимает печать помощника, читавшего SKILL.md."""
    log = tmpdir()
    run_hook(read_payload(SKILL_MD), log)
    got = records(log)
    skill_ts = got[0]["ts"] if got else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seal_ts = (datetime.strptime(skill_ts, "%Y-%m-%dT%H:%M:%SZ")
               + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    root = tmpdir()
    relative = ".tasks/hr.проба/безопасность.yaml"
    paper = root / relative
    paper.parent.mkdir(parents=True)
    paper.write_text("classes: {}\n", encoding="utf-8")
    with open(log / "artifact-seals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": seal_ts, "file": relative,
            "sha256": hashlib.sha256(paper.read_bytes()).hexdigest(),
            "agent": AGENT, "model": "claude-opus-5-5", "источник": "хук"},
            ensure_ascii=False) + "\n")
    seals, debts = pipeline()._seal_debts(paper, relative, log)
    assert debts == [], f"гейт отказал: {debts}"
    assert seals, "гейт не вернул печать"


def b2_other_skill_name_taken_from_path():
    """Имя скила берётся из пути, а не зашито: spec-writing → skill=spec-writing."""
    log = tmpdir()
    run_hook(read_payload(str(STAND / ".claude/skills/spec-writing/SKILL.md")), log)
    got = records(log)
    assert [r.get("skill") for r in got] == ["spec-writing"], f"записи: {got}"


def c_partial_read_not_logged():
    no_record(read_payload(SKILL_MD, limit=50), "чтение с limit")
    no_record(read_payload(SKILL_MD, offset=10), "чтение с offset")
    no_record(read_payload(SKILL_MD, offset=1, limit=2000), "чтение с offset и limit")
    # Судится НАЛИЧИЕ ключа, а не истинность: offset=0 ложен, но чтение частичное.
    no_record(read_payload(SKILL_MD, offset=0), "чтение с offset=0")


def d_main_session_read_not_logged():
    no_record(read_payload(SKILL_MD, agent=None), "чтение главной сессии (agent_id нет)")
    no_record(read_payload(SKILL_MD, agent=""), "чтение с пустым agent_id")


def e_foreign_file_not_logged():
    for rel in (".claude/skills/security-check/other.md",
                "README.md",
                ".claude/skills/security-check/SKILL.md.bak",
                "skills/security-check/SKILL.md",
                ".claude/skills/SKILL.md",
                ".claude/skills/a/b/SKILL.md"):
        path = str(STAND / rel)
        no_record(read_payload(path), f"чужой файл {path}")


def f_skill_call_still_logged():
    """Регрессия: вызов Skill пишется как раньше, без «способ»: «чтение»."""
    log = tmpdir()
    code, _ = run_hook(skill_payload(), log)
    assert code == 0, f"хук вернул {code}"
    got = records(log)
    assert len(got) == 1, f"ждали одну запись, получили {got}"
    r = got[0]
    assert (r.get("skill"), r.get("источник"), r.get("агент"), r.get("успех")) == \
        ("security-check", "хук", AGENT, True), f"запись: {r}"
    assert r.get("способ") != "чтение", f"вызов Skill помечен как чтение: {r}"


def g_foreign_read_exits_before_map():
    """Хук стоит на КАЖДОМ Read: чужое чтение обязано выйти до разбора карты.
    Карта стенда — FIFO без писателя: кто её откроет, тот повиснет."""
    log = tmpdir()
    project = tmpdir()
    os.mkfifo(project / "dev-map.yaml")
    code, spent = run_hook(read_payload(str(STAND / "README.md")), log,
                           task=None, project_dir=project)
    assert code is not None, f"хук повис на разборе карты ({spent:.1f} с) — фильтр Read стоит после неё"
    assert code == 0 and records(log) == [], f"код {code}, записи {records(log)}"


def h_mention_and_main_session_exit_before_map():
    """Цена: Read файла, где SKILL.md лишь упомянут в тексте, и Read SKILL.md
    главной сессией записи не дают — значит, и до разбора карты (сотни КБ,
    секунды на каждое чтение) им идти незачем."""
    mention = read_payload(str(STAND / "docs" / "скилы.md"))
    mention["tool_response"]["file"]["content"] = "см. .claude/skills/security-check/SKILL.md"
    for payload, why in ((mention, "упоминание SKILL.md в тексте"),
                         (read_payload(SKILL_MD, agent=None), "SKILL.md главной сессией")):
        log = tmpdir()
        project = tmpdir()
        os.mkfifo(project / "dev-map.yaml")
        code, spent = run_hook(payload, log, task=None, project_dir=project)
        assert code is not None, f"{why}: хук повис на разборе карты ({spent:.1f} с)"
        assert code == 0 and records(log) == [], f"{why}: код {code}, записи {records(log)}"


def i_failed_read_not_logged():
    """Неудачное чтение текста скила в контекст не положило — это не вызов."""
    for response, why in (({"error": "File does not exist."}, "ответ с error"),
                          ({"type": "text"}, "ответ без file"),
                          ("Error: File does not exist.", "ответ строкой")):
        payload = read_payload(SKILL_MD)
        payload["tool_response"] = response
        no_record(payload, why)


def k_forged_path_outside_project_not_logged():
    """БОЛЬНОЙ: подложный SKILL.md вне проекта (файл существует, форма пути та же)
    — не шаг безопасности: в контекст лёг не текст скила проекта."""
    forged = tmpdir() / "x" / ".claude" / "skills" / "security-check"
    forged.mkdir(parents=True)
    (forged / "SKILL.md").write_text("подлог\n", encoding="utf-8")
    no_record(read_payload(str(forged / "SKILL.md")), "подложный SKILL.md вне проекта")


def l_legit_symlink_path_logged():
    """Законный путь через symlink проекта — запись есть (привязка не ломает шаг)."""
    log = tmpdir()
    code, _ = run_hook(read_payload(SKILL_MD), log)
    assert code == 0, f"хук вернул {code}"
    got = records(log)
    assert [(r.get("skill"), r.get("способ")) for r in got] == [("security-check", "чтение")], \
        f"записи: {got}"


def m_symlink_target_path_not_logged():
    """Цель symlink (harness/skills/<имя>/SKILL.md): realpath тот же, но путь не
    в форме .claude/skills/<имя>/SKILL.md — имя скила хук берёт из формы пути,
    а равенство realpath — условие НЕОБХОДИМОЕ, не достаточное. Записи нет."""
    no_record(read_payload(str(STAND / "harness/skills/security-check/SKILL.md")),
              "путь цели symlink вместо пути скила")


def n_skill_symlink_outside_project_not_logged():
    """БОЛЬНОЙ: symlink скила НАРУЖУ проекта. realpath(file_path) равен
    realpath(PROJECT_DIR/.claude/skills/<имя>/SKILL.md) сам с собой, и одно
    равенство его пропускает; закрывает только «realpath под realpath(PROJECT_DIR)»."""
    evil = outside_skill("evil")
    link = STAND / ".claude" / "skills" / "evil"
    link.symlink_to(evil)
    try:
        no_record(read_payload(str(link / "SKILL.md")), "скил evil — symlink наружу проекта")
    finally:
        link.unlink()


def n2_security_check_symlink_outside_project_not_logged():
    """БОЛЬНОЙ: тот же подлог под именем, которое ждёт гейт, — свой стенд,
    где .claude/skills/security-check смотрит наружу."""
    stand = tmpdir()
    (stand / ".claude" / "skills").mkdir(parents=True)
    link = stand / ".claude" / "skills" / "security-check"
    link.symlink_to(outside_skill("security-check"))
    no_record(read_payload(str(link / "SKILL.md")), "security-check — symlink наружу проекта",
              project_dir=stand)


def p_neighbour_with_project_prefix_not_logged():
    """БОЛЬНОЙ: сосед, чьё имя начинается с имени проекта (/tmp/X и /tmp/Xsuffix).
    security-check стенда — symlink в соседа: realpath равен эталону, файл есть,
    и «realpath начинается с корня» без разделителя его пропускает."""
    stand = tmpdir()
    neighbour = Path(str(stand) + "suffix")
    _CLEAN.append(neighbour)
    real = neighbour / ".claude" / "skills" / "security-check"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("подлог соседа\n", encoding="utf-8")
    (stand / ".claude" / "skills").mkdir(parents=True)
    link = stand / ".claude" / "skills" / "security-check"
    link.symlink_to(real)
    for path in (link / "SKILL.md", real / "SKILL.md"):
        no_record(read_payload(str(path)), f"сосед-префикс {path}", project_dir=stand)


def r_nested_copy_inside_project_not_logged():
    """Копия .claude/skills/security-check/SKILL.md ГЛУБЖЕ в проекте (вложенный
    проект, каталог задачи): лежит под корнем, форма пути верна, но это не скил
    проекта — равенство с эталоном её отсекает."""
    nested = STAND / "vendor" / ".claude" / "skills" / "security-check"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("чужая копия\n", encoding="utf-8")
    try:
        no_record(read_payload(str(nested / "SKILL.md")), "вложенная копия SKILL.md")
    finally:
        shutil.rmtree(STAND / "vendor")


def o_hook_obeys_project_dir_env():
    """Хук берёт PROJECT_DIR из окружения, а не из install.conf: без переменной
    задачи она берётся с карты СТЕНДА. Иначе все случаи выше судили бы живой
    проект, а не стенд."""
    (STAND / "dev-map.yaml").write_text(
        "tasks:\n  - id: hr.задача-стенда\n    status: wip\n", encoding="utf-8")
    try:
        log = tmpdir()
        code, _ = run_hook(skill_payload(), log, task=None)
        got = records(log)
        assert code == 0 and [(r.get("task"), r.get("откуда_задача")) for r in got] == \
            [("hr.задача-стенда", "карта")], f"код {code}, записи {got}"
    finally:
        (STAND / "dev-map.yaml").unlink()


def run_case(name, func):
    try:
        func()
        RESULTS.append((name, True, ""))
        print(f"  зелёный  {name}")
    except Exception as exc:          # проба судит любое падение случая
        RESULTS.append((name, False, str(exc)))
        print(f"  КРАСНЫЙ  {name}: {exc}")


def main() -> int:
    print(f"хук: {HOOK}")
    # Без lib/config.sh рядом хук молча выходит нулём (так он устроен в дереве
    # пакета, где hooks/ и scripts/lib/ разнесены): случаи «не пишется» стали бы
    # зелёными даром. Такой прогон — не суд, а отказ с отдельным кодом.
    if not (HOOK.parent.parent / "lib" / "config.sh").is_file():
        print(f"НЕ СУДИТ: у хука нет {HOOK.parent.parent / 'lib' / 'config.sh'} — "
              "раскладка не установочная, хук инертен")
        return 2
    global STAND, SKILL_MD
    STAND = make_stand()
    SKILL_MD = str(STAND / ".claude/skills/security-check/SKILL.md")
    cases = [
        ("а полное чтение SKILL.md помощником — запись скила", a_full_read_by_helper_is_skill_call),
        ("б гейт _seal_debts принимает запись чтения", b_gate_accepts_read_record),
        ("б2 имя скила — из пути", b2_other_skill_name_taken_from_path),
        ("в частичное чтение не пишется", c_partial_read_not_logged),
        ("г чтение главной сессии не пишется", d_main_session_read_not_logged),
        ("д чужой файл не пишется", e_foreign_file_not_logged),
        ("е вызов Skill пишется как раньше", f_skill_call_still_logged),
        ("ж чужое чтение выходит до разбора карты", g_foreign_read_exits_before_map),
        ("з упоминание и чтение главной сессией выходят до карты", h_mention_and_main_session_exit_before_map),
        ("и неудачное чтение не пишется", i_failed_read_not_logged),
        ("к подложный SKILL.md вне проекта не пишется", k_forged_path_outside_project_not_logged),
        ("л законный путь через symlink пишется", l_legit_symlink_path_logged),
        ("м путь цели symlink не пишется", m_symlink_target_path_not_logged),
        ("н symlink скила наружу проекта не пишется", n_skill_symlink_outside_project_not_logged),
        ("н2 security-check — symlink наружу не пишется", n2_security_check_symlink_outside_project_not_logged),
        ("о хук подчиняется PROJECT_DIR окружения", o_hook_obeys_project_dir_env),
        ("п сосед с префиксом имени проекта не пишется", p_neighbour_with_project_prefix_not_logged),
        ("р вложенная копия SKILL.md в проекте не пишется", r_nested_copy_inside_project_not_logged),
    ]
    for name, func in cases:
        run_case(name, func)
    for path in _CLEAN:
        shutil.rmtree(path, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"ЧТЕНИЕ-SKILL.md: случаев {len(RESULTS)}, неудач {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
