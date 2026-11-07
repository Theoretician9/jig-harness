#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаг «безопасность», часть 1 (код): задетые классы, 9 правил, отпечаток кода.

Модель (помощник по скилу security-check) отвечает на то, что образцом не
выразить; здесь — то, что выражается. Задача: hr.pipeline-cybersecurity-check.

Режимы:
  --staged                правила по индексу, жёлтый (код 0)
  --task <id>             правила по всей задаче → .tasks/<id>/безопасность-код.json
  --fingerprint <id>      отпечаток кода задачи (его же считает гейт закрытия)
  --по-истории --с ДАТА   замер: сколько задач задето
  --selftest              исполняет test_check_security.py рядом
  --root <каталог>        корень репозитория (по умолчанию — корень git из cwd)

Двери стенда: SECURITY_CHECKLIST (путь чек-листа), SECURITY_NPM (исполняемый
npm). Пустое значение двери значит «не задано».

Код 3 — «не прочитано»: git, чек-лист или сеть для npm audit молчат. Это не
«классов нет» и не зелёный.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKLIST_NAME = "security-check-list.yaml"
MARKER = re.compile(r"(?:#|//|--)\s*не-уязвимость(?P<rest>.*)$")
SERVICE_TIMEOUT = 120


class Unreadable(Exception):
    """Источник не прочитан: отдельный исход, а не «нарушений нет»."""


# ── двери ───────────────────────────────────────────────────────────────────
def env_door(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def npm_cmd() -> str:
    return env_door("SECURITY_NPM")


def checklist_path() -> Path:
    given = env_door("SECURITY_CHECKLIST")
    if given:
        return Path(given)
    for candidate in (HERE.parent / "harness" / "config" / CHECKLIST_NAME,
                      HERE.parent / "config" / CHECKLIST_NAME):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "config" / CHECKLIST_NAME


def load_checklist() -> dict:
    path = checklist_path()
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as err:        # noqa: BLE001 — любая беда чтения = «не прочитан»
        raise Unreadable(f"чек-лист не прочитан: {path} ({err.__class__.__name__})")
    classes = (data or {}).get("classes") if isinstance(data, dict) else None
    if not isinstance(classes, dict) or not classes:
        raise Unreadable(f"чек-лист не прочитан: {path} (нет классов)")
    return data


# ── git ─────────────────────────────────────────────────────────────────────
def git(root, *args) -> str | None:
    """Вывод git либо None: ненулевой код — это «не прочитано»."""
    try:
        done = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(root), *args],
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=SERVICE_TIMEOUT)  # битый байт в чужом журнале не должен ронять гейт
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def task_commits(task_id, root) -> list[str] | None:
    if not task_id:
        return []
    pattern = "^Dev-Map: " + str(task_id).replace(".", r"\.") + "$"
    out = git(root, "log", "--format=%H", "--extended-regexp", f"--grep={pattern}")
    return None if out is None else out.split()


HUNK = re.compile(r"^@@ -\S+ \+(\d+)")


def parse_added(diff: str) -> dict[str, list[tuple[int, str]]]:
    """{путь: [(номер строки в файле, текст)]}; номер берётся из заголовка hunk-а,
    иначе находка указывала бы на порядок среди добавленных, а не в файл."""
    lines: dict[str, list[tuple[int, str]]] = {}
    current, number = None, 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            name = row[4:]
            current = name[2:] if name.startswith("b/") else None
            if current is not None:
                lines.setdefault(current, [])
        elif row.startswith("@@"):
            hunk = HUNK.match(row)
            number = int(hunk.group(1)) if hunk else 0
        elif row.startswith("+") and current is not None:
            lines[current].append((number, row[1:]))
            number += 1
    return lines


def added_lines(task_id, root) -> dict[str, list[tuple[int, str]]] | None:
    """{путь: [(номер, добавленная строка)]} по коммитам задачи и индексу; None — git молчит.
    task_id None — только индекс (режим --staged)."""
    commits = task_commits(task_id, root)
    if commits is None:
        return None
    diffs = [git(root, "show", "-U0", "--format=", "--no-renames", sha) for sha in commits]
    diffs.append(git(root, "diff", "--cached", "-U0", "--no-renames"))
    if any(d is None for d in diffs):
        return None
    merged: dict[str, list[str]] = {}
    for diff in diffs:
        for path, rows in parse_added(diff).items():
            merged.setdefault(path, []).extend(rows)
    return merged


# ── исключения и классы ─────────────────────────────────────────────────────
def is_excluded(path: str, checklist: dict) -> bool:
    for mask in checklist.get("excluded_paths") or []:
        if mask.endswith("/"):
            if path.startswith(mask):
                return True
        elif fnmatch.fnmatch(os.path.basename(path), mask) or fnmatch.fnmatch(path, mask):
            return True
    return False


def is_comment(row: str) -> bool:
    return row.lstrip().startswith(("#", "//", "--", "*"))


def numbered(rows) -> list[tuple[int, str]]:
    """Строка без номера (проба кормит чистым текстом) получает номер 0."""
    return [row if isinstance(row, tuple) else (0, row) for row in rows]


def effective_lines(lines: dict, checklist: dict) -> dict[str, list[tuple[int, str]]]:
    """Добавленные строки в файлах, которые судятся, без строк-комментариев.
    Комментарии выбрасываются ПОСЛЕ нумерации, иначе номера съезжают."""
    return {path: [(n, r) for n, r in numbered(rows) if not is_comment(r)]
            for path, rows in lines.items() if not is_excluded(path, checklist)}


def path_matches(path: str, masks) -> bool:
    return any(fnmatch.fnmatch(os.path.basename(path), m) or fnmatch.fnmatch(path, m)
               for m in masks or [])


def touched_classes(lines: dict, checklist: dict, findings=()) -> dict[str, list[str]]:
    """класс → что сработало (триггер, путь или находка правила кода).
    Находка правила задевает свой класс сама: триггеры и правила — два набора
    образцов, и там, где они расходятся, находка иначе терялась без бумаги."""
    judged = effective_lines(lines, checklist)
    touched: dict[str, list[str]] = {}
    for cls, spec in checklist["classes"].items():
        hits = []
        for path in judged:
            if path_matches(path, spec.get("paths")):
                hits.append(f"путь:{path}")
        patterns = [re.compile(t, re.I) for t in spec.get("triggers") or []]
        for path, rows in judged.items():
            for _number, row in rows:
                hits += [f"{p.pattern}@{path}" for p in patterns if p.search(row)]
        hits += [f"правило:{f['rule']}@{f['file']}" for f in findings if f["class"] == cls]
        if hits:
            touched[cls] = hits
    return touched


# ── правила ─────────────────────────────────────────────────────────────────
class Ctx:
    """Что видят правила: добавленные строки, файлы задачи и способ прочитать файл."""

    def __init__(self, root, lines, checklist, task_id=None, raw=None):
        self.root = Path(root)
        self.lines = lines
        self.checklist = checklist
        self.task_id = task_id
        self.raw = raw or {}        # добавленные строки с комментариями: секрет в комментарии тоже секрет

    def read(self, path: str) -> str | None:
        return git(self.root, "show", f":{path}")


def finding(rule, cls, path, line, text):
    return {"rule": rule, "class": cls, "file": path, "line": line, "text": text.strip()[:160]}


def marker_has_reason(row: str):
    """None — маркера нет; True/False — есть ли после двоеточия причина."""
    hit = MARKER.search(row)
    if not hit:
        return None
    rest = hit.group("rest").strip()
    return rest.startswith(":") and bool(rest[1:].strip())


def line_rule(rule, cls, pattern):
    """Правило по добавленным строкам: образец → находка."""
    regex = re.compile(pattern, re.I)

    def judge(ctx: Ctx):
        found = []
        for path, rows in ctx.lines.items():
            for number, row in rows:
                if not regex.search(row):
                    continue
                if marker_has_reason(row):
                    continue            # маркер с причиной гасит находку
                found.append(finding(rule, cls, path, number, row))
        return found
    return judge


SQL_FORM = r"\b(SELECT\b.*\bFROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b"
# Подстановка в одном выражении с SQL: f-строка, ${}, склейка, format, оператор %.
# Голое %s с параметрами отдельным аргументом — законная связка и не находка.
SQL_INTERPOLATION = (r"(\bf[\"'].*\{|\$\{|[\"']\s*\+|\+\s*[\"']|\.format\(|"
                     r"[\"']\s*%\s*[\(\w])")
RULE_SQL = line_rule("sql-concat", "injection", rf"^(?=.*{SQL_FORM}).*{SQL_INTERPOLATION}")
RULE_SHELL = line_rule(
    "shell-input", "injection",
    r"shell\s*=\s*True|\bos\.system\(|(?<![\w.])eval\(|(?<![\w.])exec\(|"
    r"child_process\.exec(Sync)?\(")
RULE_TLS = line_rule("tls-off", "access",
                     r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|"
                     r"NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0")
RULE_CHMOD = line_rule("chmod-wide", "access",
                       r"chmod\s+(-R\s+)?0?(777|666)\b|chmod\s+-R\s+\S+\s+(\.|/)(\s|$)")


def marker_findings(ctx: Ctx):
    """Маркер без причины сам становится находкой."""
    found = []
    for path, rows in ctx.lines.items():
        for number, row in rows:
            if marker_has_reason(row) is False:
                found.append(finding("marker-no-reason", "injection", path, number, row))
    return found


def rule_secrets(ctx: Ctx):
    """Индекс судится режимом --staged. Коммиты задачи индексу не принадлежат
    (--task требует пустой индекс), поэтому там скан всего репо, а находка
    берётся, только если её строку добавила задача: сверка по (путь, номер),
    потому что check-secrets.sh не печатает текст строки (И-3)."""
    script = HERE / "check-secrets.sh"
    mode = ["--staged"] if ctx.task_id is None else []
    try:
        done = subprocess.run(["bash", str(script), *mode, str(ctx.root)],
                              capture_output=True, text=True, timeout=SERVICE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise Unreadable("check-secrets.sh не запустился")
    if done.returncode not in (0, 1):
        raise Unreadable(f"check-secrets.sh вернул {done.returncode}")
    found = []
    for row in done.stdout.splitlines():
        if row.startswith("СЕКРЕТ? "):
            path, _, rest = row[len("СЕКРЕТ? "):].partition(":")
            number, _, kind = rest.partition(":")
            if ctx.task_id is not None and (
                    not number.isdigit()
                    or int(number) not in {n for n, _r in ctx.raw.get(path, [])}):
                continue
            found.append(finding("secrets", "secrets", path,
                                 int(number) if number.isdigit() else 0, kind.strip()))
    return found


def class_paths(ctx: Ctx, cls: str, only=None) -> list[str]:
    masks = ctx.checklist["classes"][cls].get("paths")
    names = [p for p in ctx.lines if not is_excluded(p, ctx.checklist)]
    return [p for p in names if path_matches(p, masks)
            and (only is None or only(os.path.basename(p)))]


def is_dockerfile(name: str) -> bool:
    return name.startswith("Dockerfile")


def is_compose(name: str) -> bool:
    return not is_dockerfile(name) and name.endswith((".yml", ".yaml"))


def image_unpinned(image: str) -> bool:
    if "@sha256:" in image or image == "scratch" or image.startswith("$"):
        return False
    tail = image.rsplit("/", 1)[-1]
    return ":" not in tail or tail.endswith(":latest")


def dockerfile_findings(path, text):
    rows = text.splitlines()
    found, aliases, last_user = [], set(), None
    for number, row in enumerate(rows, 1):
        match = re.match(r"\s*FROM\s+(?:--\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", row, re.I)
        if match:
            if match.group(1) not in aliases and image_unpinned(match.group(1)):
                found.append(finding("docker-root", "containers", path, number,
                                     f"образ без версии или latest: {match.group(1)}"))
            if match.group(2):
                aliases.add(match.group(2))
        user = re.match(r"\s*USER\s+(\S+)", row, re.I)
        if user:
            last_user = user.group(1)
    if last_user is None or last_user in ("root", "0"):
        found.append(finding("docker-root", "containers", path, 1, "контейнер работает от root: нет USER"))
    return found


def compose_services(text):
    import yaml
    data = yaml.safe_load(text)
    services = (data or {}).get("services") if isinstance(data, dict) else None
    return services if isinstance(services, dict) else {}


def line_of(text, needle):
    for number, row in enumerate(text.splitlines(), 1):
        if needle in row:
            return number
    return 1


def rule_docker_root(ctx: Ctx):
    found, judged = [], False
    for path in class_paths(ctx, "containers", is_dockerfile):
        text = ctx.read(path)
        if text is not None:
            judged = True
            found += dockerfile_findings(path, text)
    for path in class_paths(ctx, "containers", is_compose):
        text = ctx.read(path)
        if text is None:
            continue
        judged = True
        for name, service in compose_services(text).items():
            service = service if isinstance(service, dict) else {}
            if service.get("privileged") is True:
                found.append(finding("docker-root", "containers", path, line_of(text, "privileged"), f"{name}: privileged"))
            if any("docker.sock" in str(v) for v in service.get("volumes") or []):
                found.append(finding("docker-root", "containers", path, line_of(text, "docker.sock"), f"{name}: сокет docker внутри"))
            if str(service.get("network_mode") or "") == "host":
                found.append(finding("docker-root", "containers", path, line_of(text, "network_mode"), f"{name}: network_mode host"))
            image = str(service.get("image") or "")
            if image and image_unpinned(image):
                found.append(finding("docker-root", "containers", path, line_of(text, image), f"{name}: образ без версии или latest"))
    return found if judged else None


def port_open(entry) -> bool:
    if isinstance(entry, dict):
        return str(entry.get("host_ip") or "") not in ("127.0.0.1", "::1")
    return not str(entry).startswith(("127.0.0.1:", "[::1]:"))


def rule_port_published(ctx: Ctx):
    found, judged = [], False
    for path in class_paths(ctx, "containers", is_compose):
        text = ctx.read(path)
        if text is None:
            continue
        judged = True
        for name, service in compose_services(text).items():
            for entry in (service or {}).get("ports") or []:
                if port_open(entry):
                    found.append(finding("port-published", "containers", path,
                                         line_of(text, str(entry)), f"{name}: порт {entry} наружу"))
    return found if judged else None


def server_blocks(text):
    """(номер строки, тело) каждого server { … } по счёту скобок."""
    blocks, depth, start, body = [], 0, None, []
    for number, row in enumerate(text.splitlines(), 1):
        if start is None and re.match(r"\s*server\s*\{", row):
            start, depth, body = number, 0, []
        if start is not None:
            body.append(row)
            depth += row.count("{") - row.count("}")
            if depth <= 0:
                blocks.append((start, "\n".join(body)))
                start = None
    return blocks


NGINX_REQUIRED = (
    ("Strict-Transport-Security", r"Strict-Transport-Security"),
    ("X-Content-Type-Options", r"X-Content-Type-Options"),
    ("frame-ancestors или X-Frame-Options", r"frame-ancestors|X-Frame-Options"),
)


def rule_nginx_headers(ctx: Ctx):
    found, judged = [], False
    for path in class_paths(ctx, "web"):
        text = ctx.read(path)
        if text is None:
            continue
        judged = True
        for start, body in server_blocks(text):
            missing = [label for label, pattern in NGINX_REQUIRED
                       if not re.search(pattern, body, re.I)]
            if not re.search(r"server_tokens\s+off", body, re.I):
                missing.append("server_tokens off")
            if missing:
                found.append(finding("nginx-headers", "web", path, start,
                                     "нет: " + ", ".join(missing)))
    return found if judged else None


def npm_severity(output: str) -> int:
    data = json.loads(output)
    counts = (data.get("metadata") or {}).get("vulnerabilities")
    if isinstance(counts, dict):
        return int(counts.get("high", 0)) + int(counts.get("critical", 0))
    return sum(1 for v in (data.get("vulnerabilities") or {}).values()
               if v.get("severity") in ("high", "critical"))


def rule_npm_audit(ctx: Ctx):
    locks = [p for p in ctx.lines if os.path.basename(p) == "package-lock.json"
             and not is_excluded(p, ctx.checklist)]
    if not locks:
        return None
    command = npm_cmd() or shutil.which("npm")
    if not command:
        raise Unreadable("npm audit не прочитано: npm на машине нет")
    found = []
    for path in locks:
        folder = (ctx.root / path).parent
        try:
            done = subprocess.run([command, "audit", "--json", "--package-lock-only"],
                                  cwd=folder, capture_output=True, text=True, timeout=SERVICE_TIMEOUT)
            high = npm_severity(done.stdout)
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            raise Unreadable(f"npm audit не прочитано: {path} (сеть или ответ)")
        if high:
            found.append(finding("npm-audit", "deps", path, 1, f"high/critical: {high}"))
    return found


# Таблица правил: имя, класс, функция. Правило без предмета в диффе возвращает
# None («не судило»), с предметом — список находок (возможно пустой).
RULES = (
    ("secrets", "secrets", rule_secrets),
    ("sql-concat", "injection", RULE_SQL),
    ("shell-input", "injection", RULE_SHELL),
    ("tls-off", "access", RULE_TLS),
    ("docker-root", "containers", rule_docker_root),
    ("port-published", "containers", rule_port_published),
    ("nginx-headers", "web", rule_nginx_headers),
    ("npm-audit", "deps", rule_npm_audit),
    ("chmod-wide", "access", RULE_CHMOD),
)


def run_rules(task_id, root, checklist) -> tuple[list[dict], dict[str, int]]:
    lines = added_lines(task_id, root)
    if lines is None:
        raise Unreadable("изменения задачи не прочитаны: git молчит")
    ctx = Ctx(root, effective_lines(lines, checklist), checklist, task_id,
              {p: numbered(r) for p, r in lines.items() if not is_excluded(p, checklist)})
    found: list[dict] = []
    judged = {cls: 0 for cls in checklist["classes"]}
    for _name, cls, rule in RULES:
        result = rule(ctx)
        if result is None:
            continue
        judged[cls] = judged.get(cls, 0) + 1
        found += result
    found += marker_findings(ctx)
    return found, judged


# ── отпечаток ───────────────────────────────────────────────────────────────
def task_files(task_id, root, source="index") -> list[str] | None:
    commits = task_commits(task_id, root)
    if commits is None:
        return None
    names: set[str] = set()
    outputs = [git(root, "show", "--name-only", "--format=", sha) for sha in commits]
    if source == "index":
        outputs.append(git(root, "diff", "--cached", "--name-only"))
    if any(o is None for o in outputs):
        return None
    for out in outputs:
        names.update(n for n in out.splitlines() if n)
    return sorted(names)


def blob_of(root, path, source, commits):
    if source == "history" and commits:
        out = git(root, "ls-tree", commits[0], "--", path)
        return (out.split()[2] if out and out.strip() else "-") if out is not None else None
    out = git(root, "ls-files", "-s", "--", path)
    if out is None:
        return None
    return out.split()[1] if out.strip() else "-"


def code_fingerprint(task_id, root, source="index") -> str | None:
    """sha256 от «путь\\tblob» файлов задачи без служебных; None — git молчит."""
    try:
        checklist = load_checklist()
    except Unreadable:
        return None
    names = task_files(task_id, root, source)
    if names is None:
        return None
    commits = task_commits(task_id, root) if source == "history" else []
    rows = []
    for path in names:
        if is_excluded(path, checklist):
            continue
        blob = blob_of(root, path, source, commits)
        if blob is None:
            return None
        rows.append(f"{path}\t{blob}")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


# ── режимы ──────────────────────────────────────────────────────────────────
def say(text: str) -> None:
    print(text, flush=True)


def report(findings) -> None:
    for item in findings:
        say(f"{item['rule']}\t{item['file']}:{item['line']}\t{item['text']}")


def mode_staged(root) -> int:
    checklist = load_checklist()
    lines = added_lines(None, root)
    if lines is None:
        say("безопасность: изменения не прочитаны: git молчит (жёлтый)")
        return 0
    try:
        findings, _ = run_rules(None, root, checklist)
    except Unreadable as err:
        say(f"безопасность: {err} (жёлтый)")
        findings = []
    touched = touched_classes(lines, checklist, findings)
    report(findings)
    say(f"безопасность: задето классов {len(touched)}, находок {len(findings)}")
    if touched:
        sys.path.insert(0, str(HERE))
        spec = importlib.util.spec_from_file_location("pipeline_for_security", HERE / "pipeline.py")
        pipeline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pipeline)
        pipeline.отметить_прогон("check-security", len(findings))
    return 0


def mode_task(task_id, root) -> int:
    checklist = load_checklist()
    staged = git(root, "diff", "--cached", "--name-only")
    if staged is None:
        raise Unreadable("изменения задачи не прочитаны: git молчит")
    if staged.strip():
        raise Unreadable(f"в индексе чужое: {len(staged.split())} файлов — прогон только при пустом индексе")
    lines = added_lines(task_id, root)
    if lines is None:
        raise Unreadable("изменения задачи не прочитаны: git молчит")
    findings, judged = run_rules(task_id, root, checklist)
    fingerprint = code_fingerprint(task_id, root)
    if fingerprint is None:
        raise Unreadable("изменения задачи не прочитаны: git молчит")
    data = {"fingerprint": fingerprint, "touched": touched_classes(lines, checklist, findings),
            "rules_judged": judged, "files": len(task_files(task_id, root) or []),
            "findings": findings}
    target = Path(root) / ".tasks" / task_id / "безопасность-код.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    report(findings)
    say(f"безопасность: задето классов {len(data['touched'])}, находок {len(findings)}")
    return 0


def mode_fingerprint(task_id, root) -> int:
    value = code_fingerprint(task_id, root)
    if value is None:
        raise Unreadable("изменения задачи не прочитаны: git молчит")
    say(value)
    return 0


def mode_history(since, root) -> int:
    checklist = load_checklist()
    out = git(root, "log", f"--since={since}", "--format=%H%x09%B%x1e")
    if out is None:
        raise Unreadable("история не прочитана: git молчит")
    tasks: dict[str, list[str]] = {}
    for record in out.split("\x1e"):
        match = re.search(r"^Dev-Map:\s*(\S+)\s*$", record, re.M)
        if match and record.strip():
            tasks.setdefault(match.group(1), []).append(record.split("\t", 1)[0].strip())
    touched_tasks, by_class = {}, {}
    for task_id in sorted(tasks):
        lines = added_lines(task_id, root)
        if lines is None:
            raise Unreadable("история не прочитана: git молчит")
        classes = touched_classes(lines, checklist)
        if classes:
            touched_tasks[task_id] = sorted(classes)
            for cls in classes:
                by_class[cls] = by_class.get(cls, 0) + 1
    say(f"задач {len(tasks)}, задето {len(touched_tasks)}, по классам {json.dumps(by_class, ensure_ascii=False)}")
    for task_id, classes in touched_tasks.items():
        say(f"  {task_id} {classes}")
    return 0


def default_root() -> Path:
    out = git(Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(out.strip()) if out and out.strip() else Path.cwd()


def selftest() -> int:
    return subprocess.run([sys.executable, str(HERE / "test_check_security.py")]).returncode


FLAGS = {"--staged", "--task", "--fingerprint", "--по-истории", "--с", "--selftest", "--root"}


def main(argv) -> int:
    args = list(argv)
    unknown = [a for a in args if a.startswith("--") and a not in FLAGS]
    if unknown:
        print(f"check-security: незнакомый ключ {unknown[0]}", file=sys.stderr)
        return 2

    def value(flag):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    if "--selftest" in args:
        return selftest()
    root = Path(value("--root")) if value("--root") else default_root()
    try:
        if "--staged" in args:
            return mode_staged(root)
        if "--task" in args and value("--task"):
            return mode_task(value("--task"), root)
        if "--fingerprint" in args and value("--fingerprint"):
            return mode_fingerprint(value("--fingerprint"), root)
        if "--по-истории" in args and value("--с"):
            return mode_history(value("--с"), root)
    except Unreadable as err:
        say(f"не прочитано: {err}")
        return 3
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
