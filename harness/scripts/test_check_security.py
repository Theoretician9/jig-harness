#!/usr/bin/env python3
"""Пробы части 1 шага «безопасность» (scripts/check-security.py) — пишутся ДО кода.

Контракт: .tasks/hr.pipeline-cybersecurity-check/pretask.yaml (sick_cases,
probe_cases), план — Task 2. Каждое из 9 правил имеет больной случай (в выводе
id правила) и здорового двойника (правило судило: rules_judged[класс] >= 1).

Стенды — свои временные git-репозитории на каждый случай, секреты
генерируются, живой LOG_DIR не трогается. Модуль грузится ЛЕНИВО из тела
случая: пока кода нет, красен каждый случай, а не весь файл разом.
"""
import importlib.util
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "check-security.py"
TASK = "t.sec-1"
CHECKLIST_NAME = "security-check-list.yaml"


def probe_checklist() -> Path:
    """Чек-лист для стендов. Рядом с механизмом — как в дереве и в пакете; в
    копии дерева (прибор мутаций копирует только scripts/) его рядом нет, и
    тогда берётся чек-лист из каталога запуска. Это ДАННЫЕ, не код механизма:
    мутация живёт в check-security.py, а не здесь."""
    for candidate in (HERE.parent / "harness" / "config" / CHECKLIST_NAME,
                      HERE.parent / "config" / CHECKLIST_NAME,
                      Path.cwd() / "harness" / "config" / CHECKLIST_NAME):
        if candidate.is_file():
            return candidate
    raise AssertionError(f"чек-лист {CHECKLIST_NAME} не найден ни рядом, ни в каталоге запуска")

RESULTS = []          # (имя, ок, причина)
_MODULE = None
_CLEANUP = []


def module():
    global _MODULE
    if _MODULE is None:
        if not MODULE_PATH.exists():
            raise AssertionError(f"нет модуля {MODULE_PATH.name}")
        spec = importlib.util.spec_from_file_location("check_security", MODULE_PATH)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def tmp_dir(prefix="безопасность-стенд-"):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _CLEANUP.append(path)
    return path


def git(root, *args, check=True):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.quotepath=false",
         "-C", str(root), *args],
        capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def make_repo():
    root = tmp_dir()
    git(root, "init", "-q")
    return root


def put(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def stage(root, files):
    put(root, files)
    git(root, "add", "-A")


def commit(root, files, task=TASK, message="правка"):
    stage(root, files)
    trailer = f"\n\nDev-Map: {task}\n" if task else "\n"
    git(root, "commit", "-q", "-m", message + trailer)


def run_rules_on(files, env=None):
    """Правила по ИНДЕКСУ стенда (task_id=None): (находки, rules_judged)."""
    mod = module()
    root = make_repo()
    stage(root, files)
    saved = {}
    for key, value in (env or {}).items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        return mod.run_rules(None, root, mod.load_checklist())
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def rules_of(findings):
    return [f["rule"] for f in findings]


def cli(args, root, extra_env=None, stdin=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("SECURITY_NPM", None)
    env["SECURITY_CHECKLIST"] = str(probe_checklist())
    env.update(extra_env or {})
    done = subprocess.run([sys.executable, str(MODULE_PATH), *args, "--root", str(root)],
                          capture_output=True, text=True, env=env, timeout=120, input=stdin)
    return done.returncode, done.stdout + done.stderr


def fake_npm(high):
    """Скрипт-двойник npm: ответ как у `npm audit --json` (код 1 при находках)."""
    path = tmp_dir("безопасность-npm-") / "npm"
    count = 1 if high else 0
    body = json.dumps({"metadata": {"vulnerabilities": {
        "info": 0, "low": 0, "moderate": 0, "high": count, "critical": 0, "total": count}}})
    path.write_text(f"#!/bin/sh\necho '{body}'\nexit {1 if high else 0}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def offline_npm():
    path = tmp_dir("безопасность-npm-") / "npm"
    path.write_text("#!/bin/sh\necho 'npm ERR! code ENOTFOUND' >&2\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# ── данные больных случаев и здоровых двойников ─────────────────────────────
def sick_secret():
    return {"app.py": f'api_key = "{secrets.token_hex(8)}"\n'}


COMPOSE_PINNED = "services:\n"
RULE_CASES = {
    # правило: (класс, больные файлы, сколько находок, здоровые файлы, окружение)
    "secrets": ("secrets", sick_secret, 1,
                {"app.py": 'api_key = os.environ["API_KEY"]\n'}, None),
    "sql-concat": ("injection",
                   lambda: {"db.py": 'cur.execute(f"SELECT * FROM t WHERE id={x}")\n'}, 1,
                   {"db.py": 'cur.execute("SELECT * FROM t WHERE id=%s", (x,))\n'}, None),
    "shell-input": ("injection",
                    lambda: {"run.py": "subprocess.run(cmd, shell=True)\n"}, 1,
                    {"run.py": 'subprocess.run(["ls", d], check=True)\n'}, None),
    "tls-off": ("access",
                lambda: {"net.py": "requests.get(u, verify=False)\n"}, 1,
                {"net.py": "requests.get(u, timeout=5)\n"}, None),
    "docker-root": ("containers",
                    lambda: {"Dockerfile": 'FROM python:3.12\nCOPY . /app\nCMD ["python","a.py"]\n',
                             "Dockerfile.web": "FROM node:latest\nUSER app\n"}, 2,
                    {"Dockerfile": "FROM python:3.12\nUSER app\n"}, None),
    "port-published": ("containers",
                       lambda: {"docker-compose.yml": (
                           "services:\n"
                           "  a:\n    image: nginx:1.25\n    ports: [\"8080:80\"]\n"
                           "  b:\n    image: nginx:1.25\n    ports:\n      - \"8081:80\"\n")}, 2,
                       {"docker-compose.yml": (
                           "services:\n"
                           "  a:\n    image: nginx:1.25\n    ports: [\"127.0.0.1:8080:80\"]\n"
                           "  b:\n    image: nginx:1.25\n    ports:\n      - \"127.0.0.1:8081:80\"\n")}, None),
    "nginx-headers": ("web",
                      lambda: {"deploy/nginx/site.conf": (
                          "server {\n  listen 80;\n  server_tokens on;\n  location / { }\n}\n")}, 1,
                      {"deploy/nginx/site.conf": (
                          "server {\n  listen 443 ssl;\n  server_tokens off;\n"
                          "  add_header Strict-Transport-Security \"max-age=63072000\";\n"
                          "  add_header X-Content-Type-Options nosniff;\n"
                          "  add_header X-Frame-Options DENY;\n  location / { }\n}\n")}, None),
    "npm-audit": ("deps",
                  lambda: {"package-lock.json": "{}\n"}, 1,
                  {"package-lock.json": "{}\n"}, "npm"),
    "chmod-wide": ("access",
                   lambda: {"deploy.sh": "chmod 777 x\nchmod -R 666 .\n"}, 2,
                   {"deploy.sh": "chmod 600 x\n"}, None),
}


def case_sick(rule):
    cls, sick, count, _, env_kind = RULE_CASES[rule]
    env = {"SECURITY_NPM": fake_npm(True)} if env_kind == "npm" else None
    findings, judged = run_rules_on(sick(), env)
    got = [r for r in rules_of(findings) if r == rule]
    assert len(got) == count, f"{rule}: находок {len(got)}, ждали {count}; все: {rules_of(findings)}"
    assert judged.get(cls, 0) >= 1, f"rules_judged[{cls}] = {judged.get(cls)}"


def case_healthy(rule):
    cls, _, _, healthy, env_kind = RULE_CASES[rule]
    env = {"SECURITY_NPM": fake_npm(False)} if env_kind == "npm" else None
    findings, judged = run_rules_on(healthy, env)
    assert not [r for r in rules_of(findings) if r == rule], \
        f"{rule}: здоровый двойник покраснел: {findings}"
    assert judged.get(cls, 0) >= 1, f"{rule}: правило не судило, rules_judged[{cls}]={judged.get(cls)}"


def case_s6_network_host():
    findings, _ = run_rules_on({"docker-compose.yml": (
        "services:\n  c:\n    image: nginx:1.25\n    network_mode: host\n")})
    assert rules_of(findings).count("docker-root") == 1, rules_of(findings)


NGINX_ALL_HEADERS = (
    "  add_header Strict-Transport-Security \"max-age=63072000\";\n"
    "  add_header X-Content-Type-Options nosniff;\n"
    "  add_header X-Frame-Options DENY;\n")

# Ветки правил, которых больной S-случай не различает: у каждой свой случай,
# иначе снятая ветка не краснит ничего. (имя, файлы, правило, сколько находок)
BRANCH_CASES = (
    ("W2 compose: privileged — docker-root",
     {"docker-compose.yml": "services:\n  c:\n    image: nginx:1.25\n    privileged: true\n"},
     "docker-root", 1),
    ("W3 compose: docker.sock внутри — docker-root",
     {"docker-compose.yml": ("services:\n  c:\n    image: nginx:1.25\n    volumes:\n"
                             "      - /var/run/docker.sock:/var/run/docker.sock\n")},
     "docker-root", 1),
    ("W4 compose: image latest — docker-root",
     {"docker-compose.yml": "services:\n  c:\n    image: nginx:latest\n"},
     "docker-root", 1),
    ("W5 Dockerfile: FROM своей стадии — не образ без версии",
     {"Dockerfile": "FROM python:3.12 AS build\nFROM build\nUSER app\n"},
     "docker-root", 0),
    ("W6 nginx: заголовки есть, server_tokens on — nginx-headers",
     {"deploy/nginx/site.conf": ("server {\n  listen 443 ssl;\n  server_tokens on;\n"
                                 + NGINX_ALL_HEADERS + "}\n")},
     "nginx-headers", 1),
    ("W7 nginx: нет только HSTS — nginx-headers",
     {"deploy/nginx/site.conf": ("server {\n  listen 443 ssl;\n  server_tokens off;\n"
                                 + NGINX_ALL_HEADERS.split("\n", 1)[1] + "}\n")},
     "nginx-headers", 1),
    ("W8 chmod 0666 и chmod -R на корень — по находке",
     {"deploy.sh": "chmod 0666 f\nchmod -R u+rwx /\n"}, "chmod-wide", 2),
    ("W9 ports длинной записью: наружу — да, на 127.0.0.1 — нет",
     {"docker-compose.yml": (
         "services:\n"
         "  a:\n    image: nginx:1.25\n    ports:\n      - target: 80\n        published: 8080\n"
         "  b:\n    image: nginx:1.25\n    ports:\n      - target: 80\n        published: 8081\n"
         "        host_ip: 127.0.0.1\n")},
     "port-published", 1),
)


def case_branch(files, rule, count):
    findings, _ = run_rules_on(files)
    got = rules_of(findings).count(rule)
    assert got == count, f"{rule}: находок {got}, ждали {count}; все: {findings}"


def case_marker_with_reason():
    findings, _ = run_rules_on({"run.py": "subprocess.run(c, shell=True)  # не-уязвимость: команда — константа\n"})
    assert not findings, f"маркер с причиной не погасил: {findings}"


def case_marker_without_reason():
    findings, _ = run_rules_on({"run.py": "subprocess.run(c, shell=True)  # не-уязвимость:\n"})
    assert "marker-no-reason" in rules_of(findings), rules_of(findings)


def case_npm_high_via_cli():
    root = make_repo()
    commit(root, {"package-lock.json": "{}\n"})
    code, out = cli(["--task", TASK], root, {"SECURITY_NPM": fake_npm(True)})
    assert code == 0, (code, out)
    data = json.loads((root / ".tasks" / TASK / "безопасность-код.json").read_text(encoding="utf-8"))
    assert "npm-audit" in [f["rule"] for f in data["findings"]], data["findings"]


def case_npm_offline_is_unread():
    root = make_repo()
    commit(root, {"package-lock.json": "{}\n"})
    code, out = cli(["--task", TASK], root, {"SECURITY_NPM": offline_npm()})
    assert code == 3, (code, out)
    assert "не прочитано" in out, out


def case_touched(lines, expect):
    mod = module()
    got = sorted(mod.touched_classes(lines, mod.load_checklist()))
    assert got == expect, f"задеты {got}, ждали {expect}"


def committed_repo():
    root = make_repo()
    commit(root, {"app.py": "x = 1\n", "dev-map.yaml": "a: 1\n"})
    return root


def case_fp_ignores_service_files():
    mod = module()
    root = committed_repo()
    before = mod.code_fingerprint(TASK, root)
    assert before and len(before) == 64, before
    commit(root, {"dev-map.yaml": "a: 2\n", "STATE.md": "s\n", "docs/h.md": "h\n"})
    assert mod.code_fingerprint(TASK, root) == before


def case_fp_moves_with_task_file():
    mod = module()
    root = committed_repo()
    before = mod.code_fingerprint(TASK, root)
    commit(root, {"app.py": "x = 2\n"})
    assert mod.code_fingerprint(TASK, root) != before


def case_fp_ignores_foreign_file():
    mod = module()
    root = committed_repo()
    before = mod.code_fingerprint(TASK, root)
    commit(root, {"other.py": "y = 1\n"}, task="t.other")
    assert mod.code_fingerprint(TASK, root) == before


def case_git_refuses():
    mod = module()
    root = tmp_dir()            # не репозиторий
    assert mod.added_lines(TASK, root) is None, "added_lines на не-репозитории должен дать None"
    assert mod.code_fingerprint(TASK, root) is None
    code, out = cli(["--task", TASK], root)
    assert code == 3, (code, out)
    assert "изменения задачи не прочитаны: git молчит" in out, out


def case_history_count():
    root = make_repo()
    commit(root, {"a.py": "x = 1\n"}, task="t.h1")
    commit(root, {"b.py": "subprocess.run(c, shell=True)\n"}, task="t.h2")
    commit(root, {"c.py": "y = 2\n"}, task="t.h3")
    commit(root, {"d.py": f'password = "{secrets.token_hex(8)}"\n'}, task="t.h4")
    commit(root, {"e.css": "a{color:red}\n"}, task="t.h5")
    code, out = cli(["--по-истории", "--с", "2000-01-01"], root)
    assert code == 0, (code, out)
    assert "задач 5, задето 2" in out, out


def case_staged_marks_run_when_touched():
    root = make_repo()
    log = tmp_dir("безопасность-лог-")
    stage(root, {"run.py": "subprocess.run(c, shell=True)\n"})
    code, out = cli(["--staged"], root, {"LOG_DIR": str(log)})
    assert code == 0, (code, out)
    assert "shell-input" in out and "задето классов 1" in out, out
    assert "check-security" in (log / "пайплайн-состояние.json").read_text(encoding="utf-8")


def case_staged_silent_without_class():
    root = make_repo()
    log = tmp_dir("безопасность-лог-")
    stage(root, {"style.css": "a{color:red}\n"})
    code, out = cli(["--staged"], root, {"LOG_DIR": str(log)})
    assert code == 0 and "задето классов 0" in out, (code, out)
    assert not (log / "пайплайн-состояние.json").exists(), "прогон отмечен без задетого класса"


def case_task_json_shape():
    root = make_repo()
    commit(root, {"run.py": "subprocess.run(c, shell=True)\n"})
    code, out = cli(["--task", TASK], root)
    assert code == 0, (code, out)
    data = json.loads((root / ".tasks" / TASK / "безопасность-код.json").read_text(encoding="utf-8"))
    assert set(data) >= {"fingerprint", "touched", "rules_judged", "files", "findings"}, sorted(data)
    code, out = cli(["--fingerprint", TASK], root)
    assert code == 0 and out.strip() == data["fingerprint"], (code, out, data["fingerprint"])
    assert "injection" in data["touched"], data["touched"]


def case_foreign_index_refused():
    root = make_repo()
    commit(root, {"run.py": "x = 1\n"})
    stage(root, {"foreign.py": "z = 1\n"})
    code, out = cli(["--task", TASK], root)
    assert code == 3 and "в индексе чужое" in out, (code, out)


def case_checklist_unreadable():
    root = make_repo()
    commit(root, {"run.py": "x = 1\n"})
    broken = tmp_dir("безопасность-чек-") / "list.yaml"
    broken.write_text("classes: [unclosed\n", encoding="utf-8")
    code, out = cli(["--task", TASK], root, {"SECURITY_CHECKLIST": str(broken)})
    assert code == 3 and "чек-лист не прочитан" in out, (code, out)


def case_empty_env_is_unset():
    # Судится ПУТЬ, а не чтение: в копии дерева чек-листа рядом нет, а правило
    # «пустая дверь = не задано» от этого не меняется.
    mod = module()
    saved = os.environ.get("SECURITY_CHECKLIST")
    os.environ["SECURITY_CHECKLIST"] = ""
    try:
        got = mod.checklist_path()
        assert got.name == CHECKLIST_NAME and got.parent.name == "config", \
            f"пустая дверь должна означать «не задано», путь: «{got}»"
    finally:
        if saved is None:
            os.environ.pop("SECURITY_CHECKLIST", None)
        else:
            os.environ["SECURITY_CHECKLIST"] = saved


def case_unknown_flag():
    root = make_repo()
    code, out = cli(["--нет-такого"], root)
    assert code == 2 and "незнакомый ключ" in out, (code, out)


# ── круг 2: находки ревью кода r1 (C1, M2, M3) ──────────────────────────────
def github_token():
    """Токен формы ghp_ без слова-ключа рядом: его видит check-secrets.sh,
    а триггер класса secrets (password|api_key|…) молчит. Генерируется."""
    alphabet = string.ascii_letters + string.digits
    return "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))


# Образцы, где правило кода находит дыру, а триггер её класса молчит.
# (имя, файлы, правило, класс)
SILENT_TRIGGER_CASES = (
    ("SQL склейкой в одинарных кавычках",
     {"app/db.py": "return cur.execute('SELECT a FROM t WHERE id=' + uid)\n"},
     "sql-concat", "injection"),
    ("SQL оператором % с %d",
     {"app/db.py": 'return cur.execute("SELECT a FROM t WHERE id=%d" % uid)\n'},
     "sql-concat", "injection"),
    ("chmod -R 755 на корень",
     {"app/perm.sh": "chmod -R 755 /\n"}, "chmod-wide", "access"),
)


def task_json(root, extra_env=None):
    code, out = cli(["--task", TASK], root, extra_env)
    assert code == 0, (code, out)
    data = json.loads((root / ".tasks" / TASK / "безопасность-код.json").read_text(encoding="utf-8"))
    return data, out


def case_finding_means_touched(files, rule, cls):
    """C1: находка правила класса — это задетый класс, даже когда триггер молчит."""
    root = make_repo()
    commit(root, files)
    data, out = task_json(root)
    assert rule in [f["rule"] for f in data["findings"]], \
        f"стенд не дал находки {rule}: {data['findings']}"
    assert cls in data["touched"], \
        f"находка {rule} есть, а класс {cls} не задет: touched={sorted(data['touched'])}; {out.strip()}"
    assert "задето классов 0" not in out, out


def case_staged_token_without_keyword_touches():
    """C1 на пре-коммите: токен ghp_ без слова-ключа — находка secrets, класс задет."""
    root = make_repo()
    log = tmp_dir("безопасность-лог-")
    stage(root, {"app/conf.py": f'GH = "{github_token()}"\n'})
    code, out = cli(["--staged"], root, {"LOG_DIR": str(log)})
    assert code == 0, (code, out)
    assert "secrets\t" in out, f"check-secrets не нашёл токен в индексе: {out}"
    assert "задето классов 0" not in out, f"находка есть, классов 0: {out}"


def case_every_sick_rule_touches_its_class(rule):
    """C1 сплошняком: у каждого больного образца класс найденного правила задет
    (по --task, где судит весь путь: коммит → находки → touched)."""
    cls, sick, _, _, env_kind = RULE_CASES[rule]
    root = make_repo()
    commit(root, sick())
    env = {"SECURITY_NPM": fake_npm(True)} if env_kind == "npm" else None
    data, _ = task_json(root, env)
    found = {f["class"] for f in data["findings"] if f["rule"] == rule}
    assert found, f"{rule}: в --task находки нет: {data['findings']}"
    assert found <= set(data["touched"]), \
        f"{rule}: классы находок {sorted(found)} не в touched {sorted(data['touched'])}"


def case_secret_in_task_commit(body, line):
    """M2: секрет лежит в КОММИТЕ задачи, индекс пуст (--task требует пустой) —
    правило secrets обязано его найти, и на верной строке файла."""
    root = make_repo()
    commit(root, {"app/conf.py": body})
    data, out = task_json(root)
    got = [f for f in data["findings"] if f["rule"] == "secrets"]
    assert got, (f"секрет в коммите задачи не найден, rules_judged.secrets="
                 f"{data['rules_judged'].get('secrets')}; {out.strip()}")
    assert [f["line"] for f in got] == [line], f"строка находки {[f['line'] for f in got]}, ждали {line}"


def case_line_after_comments():
    """M3: перед находкой три строки-комментария — line = 4 (строка в файле)."""
    findings, _ = run_rules_on({"db.py": (
        "# первая\n# вторая\n# третья\n"
        'cur.execute(f"SELECT * FROM t WHERE id={x}")\n')})
    got = [f["line"] for f in findings if f["rule"] == "sql-concat"]
    assert got == [4], f"sql-concat на строке {got}, в файле это строка 4"


def case_line_in_middle_hunk():
    """M3: строка вставлена в середину существующего файла — номер по заголовку hunk-а."""
    mod = module()
    root = make_repo()
    commit(root, {"run.py": "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"}, task=None)
    stage(root, {"run.py": "a = 1\nb = 2\nc = 3\nsubprocess.run(c, shell=True)\nd = 4\ne = 5\n"})
    findings, _ = mod.run_rules(None, root, mod.load_checklist())
    got = [f["line"] for f in findings if f["rule"] == "shell-input"]
    assert got == [4], f"shell-input на строке {got}, в файле это строка 4"


def case_line_of_marker_without_reason():
    """M3: marker-no-reason нумерует так же — строкой файла."""
    findings, _ = run_rules_on({"run.py": (
        "# a\n# b\nsubprocess.run(c, shell=True)  # не-уязвимость:\n")})
    got = sorted((f["rule"], f["line"]) for f in findings)
    assert got == [("marker-no-reason", 3), ("shell-input", 3)], got


def case_line_in_task_report():
    """M3 по --task: и json, и строка отчёта «файл:строка» указывают в файл."""
    root = make_repo()
    commit(root, {"app/db.py": ("# доступ к базе\n# без ORM\n"
                                'cur.execute(f"SELECT * FROM t WHERE id={x}")\n')})
    data, out = task_json(root)
    got = [f["line"] for f in data["findings"] if f["rule"] == "sql-concat"]
    assert got == [3], f"json: строка {got}, ждали 3"
    assert "app/db.py:3\t" in out, f"в отчёте не app/db.py:3: {out}"


def run_case(name, func, *args):
    try:
        func(*args)
        RESULTS.append((name, True, ""))
        print(f"  ок    {name}")
    except Exception as err:        # noqa: BLE001 — любой отказ случая это красный случай
        RESULTS.append((name, False, str(err) or err.__class__.__name__))
        print(f"  ПЛОХО {name}: {err}")
        if not isinstance(err, AssertionError):
            traceback.print_exc(limit=2)


def main():
    os.environ["SECURITY_CHECKLIST"] = str(probe_checklist())
    rules = list(RULE_CASES)
    for number, rule in enumerate(rules, 1):
        run_case(f"S{number} {rule}: больной красный, в выводе id правила", case_sick, rule)
    run_case("W1 compose: network_mode host — docker-root", case_s6_network_host)
    for name, files, rule, count in BRANCH_CASES:
        run_case(name, case_branch, files, rule, count)
    for number, rule in enumerate(rules, 1):
        run_case(f"H{number} {rule}: здоровый двойник зелёный и правило судило", case_healthy, rule)
    run_case("M1 маркер с причиной гасит находку", case_marker_with_reason)
    run_case("M2 маркер без причины — marker-no-reason", case_marker_without_reason)
    run_case("N1 npm audit: фикстура high через SECURITY_NPM", case_npm_high_via_cli)
    run_case("N2 npm audit: «нет сети» — код 3, не зелёный", case_npm_offline_is_unread)
    touched = [
        ("T1 .css — классов нет", {"style.css": ["a{color:red}"]}, []),
        ("T2 Dockerfile — containers", {"Dockerfile": ["FROM x:1"]}, ["containers"]),
        ("T3 password= в .py — secrets", {"app.py": ["password = x"]}, ["secrets"]),
        ("T4 голое token — нет", {"app.py": ["token = 1"]}, []),
        ("T5 author — нет", {"app.py": ["author = 'a'"]}, []),
        ("T6 password в test_x.py — нет", {"test_x.py": ["password = 'a'"]}, []),
        ("T7 password в docs/x.md — нет", {"docs/x.md": ["password = 'a'"]}, []),
        ("T8 строка-комментарий — нет", {"a.py": ["# password = 1"]}, []),
        # docs/x.md гасит и маска «*.md»; каталог судится файлом не-.md.
        ("T9 password в docs/x.py (исключён каталог) — нет", {"docs/x.py": ["password = 'a'"]}, []),
    ]
    for name, lines, expect in touched:
        run_case(name, case_touched, lines, expect)
    run_case("F1 правка dev-map/STATE/docs не двигает отпечаток", case_fp_ignores_service_files)
    run_case("F2 правка файла задачи двигает отпечаток", case_fp_moves_with_task_file)
    run_case("F3 правка чужого файла не двигает отпечаток", case_fp_ignores_foreign_file)
    run_case("G1 git отказал — None и код 3 со словом «git молчит»", case_git_refuses)
    run_case("P1 --по-истории: задач 5, задето 2", case_history_count)
    run_case("R1 --staged отмечает прогон при задетом классе", case_staged_marks_run_when_touched)
    run_case("R2 --staged без класса состояние не трогает", case_staged_silent_without_class)
    run_case("J1 --task пишет json нужной формы, отпечаток тот же", case_task_json_shape)
    run_case("X1 --task при чужом индексе — код 3", case_foreign_index_refused)
    run_case("K1 чек-лист не читается — код 3", case_checklist_unreadable)
    run_case("E1 пустая дверь SECURITY_CHECKLIST = не задано", case_empty_env_is_unset)
    run_case("U1 незнакомый ключ — код 2", case_unknown_flag)
    for name, files, rule, cls in SILENT_TRIGGER_CASES:
        run_case(f"C1 {name}: триггер молчит, находка {rule} — класс {cls} задет",
                 case_finding_means_touched, files, rule, cls)
    run_case("C1 --staged: ghp_ без слова-ключа — не «задето классов 0»",
             case_staged_token_without_keyword_touches)
    for rule in rules:
        run_case(f"C1 {rule}: класс находки больного образца задет (--task)",
                 case_every_sick_rule_touches_its_class, rule)
    run_case("M2 ghp_ в коммите задачи, индекс пуст — находка secrets на строке 2",
             case_secret_in_task_commit, f'# конфиг\nGH = "{github_token()}"\n', 2)
    run_case("M2 api_key в коммите задачи, индекс пуст — находка secrets",
             case_secret_in_task_commit, f'api_key = "{secrets.token_hex(8)}"\n', 1)
    run_case("M3 номер после трёх комментариев — строка файла", case_line_after_comments)
    run_case("M3 вставка в середину файла — номер по hunk-у", case_line_in_middle_hunk)
    run_case("M3 marker-no-reason — строка файла", case_line_of_marker_without_reason)
    run_case("M3 --task: json и отчёт «файл:строка» верны", case_line_in_task_report)

    for path in _CLEANUP:
        shutil.rmtree(path, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    sick_red = sum(1 for n, ok, _ in RESULTS if n.startswith("S") and n[1].isdigit() and ok)
    healthy_green = sum(1 for n, ok, _ in RESULTS if n.startswith("H") and ok)
    print(f"правил {len(rules)}, больных красно {sick_red}, здоровых зелено {healthy_green}")
    print(f"БЕЗОПАСНОСТЬ-КОД: случаев {len(RESULTS)}, неудач {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
