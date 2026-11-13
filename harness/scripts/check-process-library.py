#!/usr/bin/env python3
"""Судья полноты библиотеки процессов (hr.process-library-completeness-gate).

Двусторонняя сверка источников запуска с библиотекой процессов
(harness/config/process-library.json и .yaml) и поля «механизмы» с mechanisms.yaml:
  источник без записи   -> «без записи»      (демон, хук, строка крона, юнит, расписание);
  запись без источника  -> «мёртвая запись»  (нет расписания, нет файла, нет юнита);
  механизм не в реестре -> «нет механизма».
Запись со статусом «снят …» судья не трогает: её файлов и расписания уже нет.

Применение:
  check-process-library.py [--root R] [--crontab FILE] [--units FILE] [--project-dir P]
    R — корень проекта (умолчание — проект судьи); FILE — подставной crontab / список
    юнитов по одному имени в строке (без ключа — `crontab -l` и `systemctl list-unit-files`);
    P — префикс своих путей в строках крона (умолчание R).
Коды: 0 чисто · 1 нарушения · 2 кривой вызов (нет библиотеки под R) ·
3 источник недоступен (crontab или systemctl нет / отдал ошибку).
Последняя строка: `check-process-library: чисто, записей N` или `... нарушений N`.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# Слово командной строки либо разделитель команд: пути судятся по словам, а не по подстроке.
CRON_TOKEN = re.compile(r"[;&|]+|[^\s;&|]+")
WRAPPER = "scripts/run-background.sh"
LIVE_NOTE = " (живая машина; коммит ни при чём — внеси в библиотеку или сними строку)"
LIVE_SOURCES = ("crontab", "systemd")
HOOK_PATH = re.compile(r"\$\{?CLAUDE_PROJECT_DIR\}?\"?/([^\s\"']+)")
UNIT_SUFFIXES = (".service", ".timer")
STATE_SOURCES = ("systemd", "docker", "cgroup")


def config_dir(root: Path):
    # Проект кладёт конфиги в harness/config, пакет (его корень — каталог harness) — в config.
    for candidate in (root / "harness" / "config", root / "config"):
        if (candidate / "process-library.json").is_file():
            return candidate
    return None


def default_root() -> Path:
    here = Path(__file__).resolve()
    for folder in here.parents:
        if config_dir(folder):
            return folder
    return here.parent.parent


def read_lines(option, command):
    """Строки источника; None — источник недоступен (нет программы или она отдала ошибку)."""
    if option:
        return Path(option).read_text(encoding="utf-8").splitlines()
    if not shutil.which(command[0]):
        return None
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def relative(path: str, prefix: str) -> str:
    return path[len(prefix):].lstrip("/") if path.startswith(prefix) else path


def cron_refs(text: str, project_dir: str) -> list:
    """Свои пути строки крона: ("путь", rel) и ("обёртка", id, rel команды или None), по порядку."""
    prefix = project_dir.rstrip("/") + "/"
    tokens = CRON_TOKEN.findall(text.replace("'", " ").replace('"', " "))
    refs, cwd, i = [], None, 0

    def own(word):
        if word.startswith(prefix):
            return word[len(prefix):]
        if word.startswith("./") and cwd == prefix.rstrip("/"):
            return word[2:]
        return None

    while i < len(tokens):
        word = tokens[i]
        if word == "cd" and i + 1 < len(tokens):
            cwd = tokens[i + 1].rstrip("/")
            i += 2
            continue
        rel = own(word)
        if rel == WRAPPER and i + 2 < len(tokens):
            command = own(tokens[i + 3]) if tokens[i + 2] == "--" and i + 3 < len(tokens) else None
            refs.append(("обёртка", tokens[i + 1], command))
            i += 4 if tokens[i + 2] == "--" else 2
            continue
        if rel:
            refs.append(("путь", rel))
        i += 1
    return refs


def git_hook_files(project: Path) -> set:
    hooks = project / ".git" / "hooks"
    found = set()
    for hook in hooks.glob("*") if hooks.is_dir() else []:
        if hook.is_symlink():
            target = hook.resolve()
            if target.is_relative_to(project.resolve()):
                found.add(target.relative_to(project.resolve()).as_posix())
    return found


def conf_value(cfg: Path, key: str) -> str:
    match = re.search(rf'(?m)^{re.escape(key)}=["\']?([^"\'\n#]*)', (cfg / "harness.conf").read_text(encoding="utf-8")) \
        if (cfg / "harness.conf").is_file() else None
    return match.group(1).strip() if match else ""


def state_source_flaw(source, all_units: set):
    """(вид нарушения, ключ) у поля «состояние_из» или None: ровно один из systemd/docker/cgroup и значение непусто;
    юнит из systemd обязан быть среди юнит-файлов (контейнер и cgroup судья не спрашивает: docker в воротах может молчать)."""
    if not isinstance(source, dict) or len(source) != 1 or next(iter(source)) not in STATE_SOURCES \
            or not next(iter(source.values())):
        return "кривая запись", "состояние_из"
    missing = [u for u in source.get("systemd") or [] if u not in all_units]
    return ("мёртвая запись", missing[0]) if missing else None


def judge(root: Path, cron_lines: list, unit_lines: list, project_dir: str) -> tuple:
    cfg = config_dir(root)
    records = json.loads((cfg / "process-library.json").read_text(encoding="utf-8"))["процессы"]
    schedule = (yaml.safe_load((cfg / "process-library.yaml").read_text(encoding="utf-8")) or {}).get("процессы") or {}
    mech_file = cfg / "mechanisms.yaml"
    mechanisms = {m.get("файл") for m in (yaml.safe_load(mech_file.read_text(encoding="utf-8")) or {}).get("механизмы") or []} \
        if mech_file.is_file() else set()
    active = [r for r in records if not str(r.get("статус", "")).startswith("снят")]
    by_id = {r["id"]: r for r in records}
    dropped = [r for r in records if r not in active]
    ids = set(by_id)
    owned_files = {f for r in records for f in r.get("файлы", [])}
    bad = []

    def violation(kind, source, key):
        bad.append(f"НАРУШЕНИЕ {kind}: {source} :: {key}" + (LIVE_NOTE if source in LIVE_SOURCES else ""))

    # Источники → записи.
    demons = sorted(p for p in (root / "harness" / "demons").glob("*") if p.suffix in (".sh", ".py")) \
        if (root / "harness" / "demons").is_dir() else []
    for demon in demons:
        rel = demon.relative_to(root).as_posix()
        if rel not in owned_files:
            violation("без записи", "harness/demons", rel)
    live_cron = set()   # id и файлы, на которые ссылается живая строка крона
    for line in cron_lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        seen = set()
        for ref in cron_refs(text, project_dir):
            if ref[0] == "обёртка":
                _, ident, command = ref
                live_cron.add(ident)
                wanted = by_id.get(ident)
                if wanted is None or wanted in dropped:
                    key = ident
                elif command and command not in wanted.get("файлы", []):
                    key = command
                else:
                    key = None
                    live_cron.add(command)
            else:
                key = ref[1] if ref[1] not in owned_files else None
                live_cron.add(ref[1])
            if key and key not in seen:
                seen.add(key)
                violation("без записи", "crontab", key)
    settings = root / ".claude" / "settings.json"
    # Git-хуки живут в .git/hooks симлинками на скрипты проекта; префикс — боевой проект, не снимок.
    hook_files = git_hook_files(Path(project_dir))
    if settings.is_file():
        hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        commands = [h.get("command", "") for groups in hooks.values() for g in groups for h in g.get("hooks", [])]
        claude_hooks = {m.group(1) for c in commands for m in [HOOK_PATH.search(c)] if m}
        hook_files |= claude_hooks
        for rel in sorted(claude_hooks):
            if rel not in owned_files:
                violation("без записи", ".claude/settings.json", rel)
    all_units = {u.split()[0] for u in unit_lines if u.strip()}
    units = {u.split()[0] for u in unit_lines if u.strip().startswith("harness-") and u.split()[0].endswith(UNIT_SUFFIXES)}
    claimed = {u for r in records for u in r.get("юниты", [])}
    for rec in records:
        claimed |= {f"{prefix}{rec['id']}{suffix}" for prefix in ("", "harness-") for suffix in UNIT_SUFFIXES}
    for unit in sorted(units - claimed):
        violation("без записи", "systemd", unit)
    for ident in schedule:
        if ident not in ids:
            violation("без записи", "process-library.yaml", ident)

    # Записи → источники.
    for rec in active:
        if rec.get("запуск") in ("крон", "служба-проверок") and rec["id"] not in schedule:
            violation("мёртвая запись", "process-library.json", rec["id"])
        for rel in rec.get("файлы", []):
            if not rel.startswith("/") and not (root / rel).exists():
                violation("мёртвая запись", rec["id"], rel)
        wanted_units = rec.get("юниты") or [f"{p}{rec['id']}{x}" for p in ("", "harness-") for x in UNIT_SUFFIXES]
        if rec.get("запуск") == "служба":
            if rec.get("юниты"):
                for unit in rec["юниты"]:
                    if unit not in units:
                        violation("мёртвая запись", rec["id"], unit)
            elif not units.intersection(wanted_units):
                violation("мёртвая запись", rec["id"], wanted_units[0])
        elif rec.get("юниты"):
            for unit in rec["юниты"]:
                if unit not in units:
                    violation("мёртвая запись", rec["id"], unit)
        flaw = state_source_flaw(rec["состояние_из"], all_units) if "состояние_из" in rec else None
        if flaw:
            violation(flaw[0], rec["id"], flaw[1])
        if rec.get("запуск") == "хук" and not hook_files.intersection(rec.get("файлы", [])):
            violation("мёртвая запись", rec["id"], ".claude/settings.json")
        # «если_задан»: строка крона ставится только при заполненном ключе — без него её отсутствие законно.
        gate_key = (schedule.get(rec["id"]) or {}).get("если_задан")
        off_by_conf = bool(gate_key) and not conf_value(cfg, gate_key)
        if rec.get("запуск") == "крон" and not off_by_conf and not live_cron.intersection({rec["id"], *rec.get("файлы", [])}):
            violation("мёртвая запись", rec["id"], "crontab")
        for mech in rec.get("механизмы", []):
            if mech not in mechanisms:
                violation("нет механизма", rec["id"], mech)
    return bad, len(records)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root")
    ap.add_argument("--crontab")
    ap.add_argument("--units")
    ap.add_argument("--project-dir")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else default_root()
    if not config_dir(root):
        print(f"check-process-library: под {root} нет process-library.json", file=sys.stderr)
        return 2
    cron = read_lines(args.crontab, ["crontab", "-l"])
    units = read_lines(args.units, ["systemctl", "list-unit-files", "--no-legend", "--no-pager"])
    missing = [name for name, lines in (("crontab", cron), ("systemctl", units)) if lines is None]
    if missing:
        for name in missing:
            print(f"check-process-library: источник недоступен: {name}")
        return 3
    bad, total = judge(root, cron, units, args.project_dir or str(root))
    print("\n".join(bad))
    print(f"check-process-library: нарушений {len(bad)}" if bad else f"check-process-library: чисто, записей {total}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
