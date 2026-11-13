#!/usr/bin/env python3
"""Проба формы библиотеки процессов (hr.process-dispatcher, часть А1, спека §А.1–А.2).

Библиотека — реестр ВСЕХ процессов харнеса: process-library.json (свойства) и
process-library.yaml (расписание и выключатель). Реестр, который отстал от
машины, хуже отсутствующего: диспетчер покажет «всё учтено», а хук или
порождение службы останутся невидимыми. Поэтому списки, которые проба судит,
берутся РАЗБОРОМ источников (system-jobs.yaml, .claude/settings.json,
.git/hooks, tg-dispatcher.sh), а не вшитым перечнем; пустой разбор — красный.

Двери (умолчания — файлы проекта рядом с пробой):
    HARNESS_PROCESS_LIBRARY        process-library.yaml (её же читает scripts/lib/process_library.py)
    HARNESS_PROCESS_LIBRARY_JSON   process-library.json
    HARNESS_SYSTEM_JOBS            system-jobs.yaml
    HARNESS_CLAUDE_SETTINGS        .claude/settings.json
    HARNESS_GIT_HOOKS_DIR          каталог хуков git
    HARNESS_TG_DISPATCHER          tg-dispatcher.sh
    HARNESS_MECHANISMS             mechanisms.yaml (id механизма — его поле `файл`)

Прогон (только фоном):
    python3 scripts/test_process_library.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def config_dir() -> Path:
    # Раскладка проекта и пакета разные: конфиги лежат там, где config/system-jobs.yaml.
    for folder in HERE.parents:
        if (folder / "config" / "system-jobs.yaml").is_file():
            return folder / "config"
    return HERE.parent / "harness" / "config"


CONFIG = config_dir()
ROOT = CONFIG.parent.parent
JSON_PATH = Path(os.environ.get("HARNESS_PROCESS_LIBRARY_JSON") or CONFIG / "process-library.json")
YAML_PATH = Path(os.environ.get("HARNESS_PROCESS_LIBRARY") or CONFIG / "process-library.yaml")
JOBS_PATH = Path(os.environ.get("HARNESS_SYSTEM_JOBS") or CONFIG / "system-jobs.yaml")
SETTINGS_PATH = Path(os.environ.get("HARNESS_CLAUDE_SETTINGS") or ROOT / ".claude" / "settings.json")
GIT_HOOKS = Path(os.environ.get("HARNESS_GIT_HOOKS_DIR") or ROOT / ".git" / "hooks")
DISPATCHER = Path(os.environ.get("HARNESS_TG_DISPATCHER") or HERE / "tg-dispatcher.sh")
MECHANISMS_PATH = Path(os.environ.get("HARNESS_MECHANISMS") or CONFIG / "mechanisms.yaml")
# Боевой слой установки не должен подмешиваться в судимый реестр: дверь на заведомо несуществующий путь.
os.environ["HARNESS_PROCESS_LIBRARY_LOCAL"] = "/nonexistent/harness-probe/process-library.local.json"

CLASSES = {"фоновый", "по внутреннему действию", "сторонний"}
LAUNCHES = {"крон", "таймер", "загрузка", "служба", "хук", "агент-сессии",
            "команда-сессии", "вызов-модели", "контейнер", "служба-проверок"}
MINIMUM = ("id", "название", "класс", "запуск", "статус", "файлы", "доля_нагрузки")
JOB_FIELDS = ("расписание", "команда", "журнал", "если_задан")
MIRRORED = ("метки", "запас-порога", "метки_без_задания")
INTERNAL = "по внутреннему действию"
REQUIRED_IDS = ("sentinel", "process-dispatcher", "helpers", "ask-the-model", "claude-session",
                "logrotate-harness")

paths = failures = 0


def report(ok: bool, text: str, bad=()) -> None:
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    bad = list(bad)
    tail = f": {', '.join(map(str, bad[:12]))}" + (f" … ещё {len(bad) - 12}" if len(bad) > 12 else "") if bad else ""
    print("НЕ ок · " + text + tail)


def read(path: Path, parse, what: str):
    try:
        return parse(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError, yaml.YAMLError) as error:
        return None, f"{what} не читается ({path}): {error}"


library_json, json_error = read(JSON_PATH, json.loads, "process-library.json")
library_yaml, yaml_error = read(YAML_PATH, yaml.safe_load, "process-library.yaml")
system_jobs, jobs_error = read(JOBS_PATH, yaml.safe_load, "system-jobs.yaml")

records = [r for r in ((library_json or {}).get("процессы") or []) if isinstance(r, dict)]
by_id = {r.get("id"): r for r in records}
processes = (library_yaml or {}).get("процессы") or {}
jobs_parsed = (system_jobs or {}).get("задания") or []


def files_of(record: dict) -> set:
    return {str(f) for f in (record.get("файлы") or [])}


print("── разбор файлов ──")
report(not json_error, "json.loads process-library.json", [json_error] if json_error else ())
report(not yaml_error, "yaml.safe_load process-library.yaml", [yaml_error] if yaml_error else ())
report(not jobs_error and bool(jobs_parsed),
       f"разбор system-jobs.yaml даёт задания ({len(jobs_parsed)})",
       [jobs_error or "пустой список `задания`"] if jobs_error or not jobs_parsed else ())

print("── 1. задания system-jobs → записи обоих файлов ──")
# Задание system-jobs крутит либо крон, либо служба потока проверок — третьего хозяина у него нет.
JOB_LAUNCHES = {"крон", "служба-проверок"}
job_names = [j.get("имя") for j in jobs_parsed]
missing_json = [n for n in job_names if n not in by_id]
report(bool(job_names) and not missing_json, "у каждого задания есть запись в process-library.json", missing_json)
wrong_kind = [n for n in job_names if n in by_id and
              (by_id[n].get("класс") != "фоновый" or by_id[n].get("запуск") not in JOB_LAUNCHES)]
report(bool(job_names) and not wrong_kind,
       "запись задания — класс «фоновый», запуск «крон» или «служба-проверок»", wrong_kind)
missing_yaml = [n for n in job_names if n not in processes]
report(bool(job_names) and not missing_yaml, "у каждого задания есть запись в process-library.yaml `процессы:`",
       missing_yaml)
diverged = []
for job in jobs_parsed:
    entry = processes.get(job.get("имя"))
    if not isinstance(entry, dict):
        continue
    diverged += [f"{job['имя']}.{field}" for field in JOB_FIELDS if entry.get(field) != job.get(field)]
    if entry.get("запуск") not in JOB_LAUNCHES:
        diverged.append(f"{job['имя']}.запуск")
report(bool(job_names) and not missing_yaml and not diverged,
       "расписание/команда/журнал/если_задан равны поштучно, запуск «крон» или «служба-проверок»", diverged)
order = [name for name in processes if name in set(job_names)]
report(bool(job_names) and order == job_names, "порядок `процессы:` = порядку `задания:`",
       [f"первое расхождение: {a} ≠ {b}" for a, b in zip(order, job_names) if a != b][:1]
       or ([f"заданий в `процессы:` {len(order)} из {len(job_names)}"] if order != job_names else ()))
strangers = [n for n in processes if (by_id.get(n) or {}).get("класс") != "фоновый"]
report(not strangers, "каждый ключ `процессы:` — id записи класса «фоновый» в JSON", strangers)

print("── 2. разделы меток равны system-jobs ──")
for section in MIRRORED:
    mine, theirs = (library_yaml or {}).get(section), (system_jobs or {}).get(section)
    report(theirs is not None and mine == theirs, f"раздел `{section}` равен разделу system-jobs.yaml",
           sorted(set(mine or {}) ^ set(theirs or {})) or (["разделы различаются значениями"] if mine != theirs else ()))

print("── 3. поля записей ──")
report(bool(records), f"записей в process-library.json: {len(records)}")
bad_class = [f"{r.get('id')}={r.get('класс')!r}" for r in records if r.get("класс") not in CLASSES]
report(bool(records) and not bad_class, "`класс` ∈ {фоновый, по внутреннему действию, сторонний}", bad_class)
bad_launch = [f"{r.get('id')}={r.get('запуск')!r}" for r in records if r.get("запуск") not in LAUNCHES]
report(bool(records) and not bad_launch, "`запуск` ∈ десяти значений §А.1 и §Г.1", bad_launch)
no_minimum = [f"{r.get('id')}: нет {', '.join(f for f in MINIMUM if f not in r)}"
              for r in records if any(f not in r for f in MINIMUM)]
report(bool(records) and not no_minimum, "минимум полей " + ", ".join(MINIMUM), no_minimum)
ids = [r.get("id") for r in records]
report(bool(records) and len(ids) == len(set(ids)), "id уникальны",
       sorted({i for i in ids if ids.count(i) > 1}))

print("── 4. исключения выключателя ──")
exemptions = ((library_yaml or {}).get("выключатель") or {}).get("исключения") or []
report(bool(exemptions), f"исключений выключателя: {len(exemptions)}")
no_basis = [e.get("id") for e in exemptions if not str(e.get("основание") or "").strip()]
report(bool(exemptions) and not no_basis, "у каждого исключения есть `основание`", no_basis)
unknown = [e.get("id") for e in exemptions if e.get("id") not in by_id]
report(bool(exemptions) and not unknown, "id каждого исключения есть в process-library.json", unknown)

sys.path.insert(0, str(HERE / "lib"))
try:
    import process_library
    library_error = ""
except Exception as error:  # проба называет причину, а не падает трассой
    process_library, library_error = None, f"{type(error).__name__}: {error}"

print("── 5. строки crontab ──")
try:
    lines = process_library.cron_lines("/P", "/L")
    lines_error = ""
except Exception as error:
    lines, lines_error = [], f"cron_lines упал: {library_error or type(error).__name__}: {error}"
line_ids = [re.search(r" /P/scripts/run-background\.sh (\S+) -- ", line) for line in lines]
orphan = [found.group(1) if found else f"без обёртки: {line[:50]}"
          for line, found in zip(lines, line_ids) if not found or found.group(1) not in by_id]
report(bool(lines) and not orphan, f"каждая строка cron_lines ({len(lines)}) относится к id библиотеки",
       [lines_error] if lines_error else (orphan or ["строк нет"] if not lines or orphan else ()))

print("── 6. хуки .claude/settings.json ──")
settings, settings_error = read(SETTINGS_PATH, json.loads, "settings.json")
hook_scripts, unparsed = [], []
for groups in ((settings or {}).get("hooks") or {}).values():
    for group in groups:
        for hook in group.get("hooks") or []:
            found = re.search(r"\$\{?CLAUDE_PROJECT_DIR\}?/([\w./-]+\.(?:py|sh))", hook.get("command") or "")
            if found:
                hook_scripts.append(found.group(1))
            else:
                unparsed.append((hook.get("command") or "")[:50])
hook_scripts = list(dict.fromkeys(hook_scripts))
report(not settings_error and bool(hook_scripts) and not unparsed,
       f"разбор хуков settings.json: скриптов {len(hook_scripts)}",
       [settings_error] if settings_error else (unparsed or ["ни одного хука"]))
hook_records = [r for r in records if (r.get("класс"), r.get("запуск")) == (INTERNAL, "хук")]
no_hook_record, shared = [], []
for script in hook_scripts:
    owners = [r.get("id") for r in hook_records if script in files_of(r)]
    if not owners:
        no_hook_record.append(script)
    elif len(owners) > 1:
        shared.append(f"{script} у {owners}")
report(bool(hook_scripts) and not no_hook_record,
       "у каждого скрипта-хука есть запись «по внутреннему действию»/«хук» (по полю `файлы`)", no_hook_record)
owners_per_record = {}
for script in hook_scripts:
    for r in hook_records:
        if script in files_of(r):
            owners_per_record.setdefault(r.get("id"), []).append(script)
merged = [f"{rid}: {scripts}" for rid, scripts in owners_per_record.items() if len(scripts) > 1]
report(not shared and not merged, "по записи на скрипт-хук", shared + merged)

print("── 7. хуки git ──")
try:
    git_hooks = sorted(p for p in GIT_HOOKS.iterdir() if not p.name.endswith(".sample"))
    git_error = ""
except OSError as error:
    git_hooks, git_error = [], f"{GIT_HOOKS} не читается: {error}"
report(bool(git_hooks), f"разбор {GIT_HOOKS}: хуков {len(git_hooks)}", [git_error or "ни одного хука"])
no_git_record = []
for hook in git_hooks:
    record = by_id.get(f"git-{hook.name}")
    target = os.path.relpath(hook.resolve(), ROOT) if hook.is_symlink() else None
    if not record or (record.get("класс"), record.get("запуск")) != (INTERNAL, "хук"):
        no_git_record.append(f"git-{hook.name}")
    elif target and target not in files_of(record):
        no_git_record.append(f"git-{hook.name}: в `файлы` нет {target}")
report(bool(git_hooks) and not no_git_record,
       "у каждого хука git есть запись git-<имя> («по внутреннему действию»/«хук»)", no_git_record)

print("── 8. порождения службы tg-dispatcher.sh ──")


def spawned_scripts(text: str) -> tuple:
    """Скрипты, которые служба запускает `setsid nohup`: (пути от корня, нераспознанные)."""
    text = text.replace("\\\n", " ")
    assigned = dict(re.findall(r"^\s*([A-Z_]+)=\"?([^\"\s]+)\"?\s*$", text, re.M))
    found, unknown = [], []
    for line in text.splitlines():
        if "setsid nohup" not in line or line.lstrip().startswith("#"):
            continue
        # Тело `bash -c '…'` — обвязка; запускаемый скрипт стоит в доводах после него.
        command = re.sub(r"'[^']*'", "", line.split("setsid nohup", 1)[1])
        command = re.sub(r"\$\{?([A-Z_]+)\}?", lambda m: assigned.get(m.group(1), m.group(0))
                         if m.group(1) not in ("HERE", "PROJECT_DIR") else m.group(0), command)
        script = re.search(r"\$\{?(HERE|PROJECT_DIR)\}?/([\w./-]+\.(?:py|sh))", command)
        if not script:
            unknown.append(command.strip()[:50])
            continue
        found.append(("scripts/" if script.group(1) == "HERE" else "") + script.group(2))
    return found, unknown


dispatcher_text, dispatcher_error = read(DISPATCHER, str, "tg-dispatcher.sh")
spawns, spawn_unknown = spawned_scripts(dispatcher_text or "")
report(bool(spawns) and not spawn_unknown and not dispatcher_error,
       f"разбор `setsid nohup` в tg-dispatcher.sh: порождений {len(spawns)}",
       [dispatcher_error] if dispatcher_error else (spawn_unknown or ["ни одного порождения"]))
service_children = [r for r in records if (r.get("класс"), r.get("запуск")) == (INTERNAL, "служба")
                    and "scripts/tg-dispatcher.sh" not in files_of(r)]
no_spawn_record = [s for s in spawns if not any(s in files_of(r) for r in service_children)]
report(bool(spawns) and not no_spawn_record,
       "у каждого порождения есть своя запись «по внутреннему действию»/«служба» (по `файлы`, не запись диспетчера)",
       no_spawn_record)

print("── 9. фоновые без расписания ──")
unscheduled = [r.get("id") for r in records if r.get("класс") == "фоновый"
               and not ({"расписание", "юнит"} & set(processes.get(r.get("id")) or {}))
               and not str(r.get("статус") or "").startswith(("снят", "строится"))]
report(bool(records) and not unscheduled,
       "фоновый без расписания/юнита в YAML — только со статусом «снят…» или «строится…»", unscheduled)

print("── 10. обязательные записи и счёт ──")
absent = [i for i in REQUIRED_IDS if i not in by_id]
report(not absent, "есть записи " + ", ".join(REQUIRED_IDS), absent)
timer = (by_id.get("sentinel") or {}).get("запуск")
report(timer == "таймер", "sentinel — запуск «таймер»", [f"запуск={timer!r}"])
active = [r for r in records if not str(r.get("статус") or "").startswith("снят")]
print(f"записей всего {len(records)}, действующих (без «снят») {len(active)}")

print("── 11. process_library: load / jobs / is_exempt ──")


def call(function, *args):
    try:
        return function(*args), ""
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


if process_library is None:
    report(False, "scripts/lib/process_library.py импортируется", [library_error])
else:
    loaded, error = call(process_library.load)
    report(not error and isinstance(loaded, dict) and bool(loaded.get("процессы")),
           "load() отдаёт библиотеку с `процессы:`", [error or "нет `процессы:`"])
    listed, error = call(process_library.jobs)
    form_bad = [str((j or {}).get("имя")) for j in (listed or [])
                if not isinstance(j, dict) or set(j) - {"имя", *JOB_FIELDS}
                or not {"имя", "расписание", "команда", "журнал"} <= set(j)]
    report(not error and bool(listed) and not form_bad, "jobs() — задания в форме system-jobs",
           [error] if error else (form_bad or ["пусто"]))
    report(not error and listed == jobs_parsed, "jobs() равен `задания` system-jobs.yaml",
           [error] if error else [f"заданий {len(listed or [])} против {len(jobs_parsed)}"])
    # jobs() обязан читать библиотеку: расписание живёт ТОЛЬКО в process-library.yaml.
    with tempfile.TemporaryDirectory() as tmp:
        probe_yaml = Path(tmp) / "process-library.yaml"
        changed = json.loads(json.dumps(library_yaml or {}))
        first = next(iter(changed.get("процессы") or {}), None)
        if first:
            changed["процессы"][first]["расписание"] = "1 2 3 4 5"
        probe_yaml.write_text(yaml.safe_dump(changed, allow_unicode=True), encoding="utf-8")
        saved = os.environ.get("HARNESS_PROCESS_LIBRARY")
        os.environ["HARNESS_PROCESS_LIBRARY"] = str(probe_yaml)
        followed, error = call(process_library.jobs)
        os.environ.pop("HARNESS_PROCESS_LIBRARY")
        if saved is not None:
            os.environ["HARNESS_PROCESS_LIBRARY"] = saved
    hit = [j for j in (followed or []) if j.get("имя") == first and j.get("расписание") == "1 2 3 4 5"]
    report(bool(first) and bool(hit), "jobs() берёт расписание из process-library.yaml (дверь HARNESS_PROCESS_LIBRARY)",
           [error] if error else [f"правка расписания «{first}» в копии библиотеки не дошла до jobs()"])
    exempt, error = call(process_library.is_exempt, "backup")
    report(exempt is True, "is_exempt('backup') истинно", [error or repr(exempt)])
    exempt, error = call(process_library.is_exempt, "checks-stream")
    report(exempt is False, "is_exempt('checks-stream') ложно", [error or repr(exempt)])

print("── 12. механизмы ──")
# Поле `механизмы` связывает процесс с паспортами mechanisms.yaml: так нагрузку
# гейтов можно показать внутри прогона ворот и дописать гейты позже, не меняя форму.
mechanisms, mechanisms_error = read(MECHANISMS_PATH, yaml.safe_load, "mechanisms.yaml")
mechanism_ids = {str(m.get("файл")) for m in ((mechanisms or {}).get("механизмы") or [])
                 if isinstance(m, dict) and m.get("файл")}
report(not mechanisms_error and bool(mechanism_ids),
       f"разбор mechanisms.yaml даёт id механизмов ({len(mechanism_ids)})",
       [mechanisms_error or "пустой список `механизмы`"])
linked = [r for r in records if "механизмы" in r]
report(bool(linked), f"записей с полем `механизмы`: {len(linked)}", ["поля нет ни у одной записи"])
bad_form = [r.get("id") for r in linked if not isinstance(r["механизмы"], list) or not r["механизмы"]]
report(bool(linked) and not bad_form, "`механизмы` — непустой список", bad_form)
dangling = [f"{r.get('id')}: {m}" for r in linked if isinstance(r["механизмы"], list)
            for m in r["механизмы"] if str(m) not in mechanism_ids]
report(bool(linked) and bool(mechanism_ids) and not dangling,
       "каждый id в `механизмы` есть в mechanisms.yaml", dangling)

print("── 11. слой установки: слияние по id ──")
# Слой правит человек руками, а читают его крон и панель: любой вид порчи — строка
# в stderr с путём и пакетный реестр, иначе один опечатанный файл роняет всех читателей.
BROKEN_LAYERS = {
    "не JSON": lambda path: path.write_text("{не json", encoding="utf-8"),
    "запись без id": lambda path: path.write_text(
        json.dumps({"процессы": [{"название": "х"}]}, ensure_ascii=False), encoding="utf-8"),
    "список наверху": lambda path: path.write_text("[]", encoding="utf-8"),
    "каталог вместо файла": lambda path: path.mkdir(),
    "нечитаемый файл": lambda path: (path.write_text("{}", encoding="utf-8"), path.chmod(0)),
}


def registry_with_stderr(base: Path):
    captured = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    saved_stderr, sys.stderr = sys.stderr, captured
    try:
        return [r["id"] for r in process_library.registry(base)["процессы"]], "", captured
    except Exception as raised:
        return [], f"упал: {type(raised).__name__}: {raised}", captured
    finally:
        sys.stderr = saved_stderr
        captured.seek(0)


with tempfile.TemporaryDirectory(prefix="pl-layer-") as layer_dir:
    base_path, layer_path = Path(layer_dir) / "base.json", Path(layer_dir) / "layer.local.json"
    base_path.write_text(json.dumps({"процессы": [
        {"id": "probe-a", "название": "пакетная А"}, {"id": "probe-b", "название": "пакетная Б"}]}), encoding="utf-8")
    saved_door = os.environ.get("HARNESS_PROCESS_LIBRARY_LOCAL")
    os.environ["HARNESS_PROCESS_LIBRARY_LOCAL"] = str(layer_path)
    try:
        without = [(r["id"], r["название"]) for r in process_library.registry(base_path)["процессы"]]
        report(without == [("probe-a", "пакетная А"), ("probe-b", "пакетная Б")],
               "слоя нет — реестр как в пакете", [str(without)])
        layer_path.write_text(json.dumps({"процессы": [
            {"id": "probe-b", "название": "слой Б"}, {"id": "probe-c", "название": "слой В"}]}), encoding="utf-8")
        merged = [(r["id"], r["название"]) for r in process_library.registry(base_path)["процессы"]]
        report(merged == [("probe-a", "пакетная А"), ("probe-b", "слой Б"), ("probe-c", "слой В")],
               "слой заменяет запись по id на месте и дописывает новую", [str(merged)])
        for kind, spoil in BROKEN_LAYERS.items():
            spoiled = Path(layer_dir) / kind.replace(" ", "-") / "layer.local.json"
            spoiled.parent.mkdir()
            spoil(spoiled)
            if spoiled.is_file() and os.access(spoiled, os.R_OK) and spoiled.stat().st_mode & 0o777 == 0:
                print(f"пропуск · битый слой «{kind}»: под root режим 0 читается")
                continue
            os.environ["HARNESS_PROCESS_LIBRARY_LOCAL"] = str(spoiled)
            ids, error, captured = registry_with_stderr(base_path)
            said = captured.read()
            problems = [error] if error else []
            if not error and ids != ["probe-a", "probe-b"]:
                problems.append(f"реестр {ids}")
            if str(spoiled) not in said:
                problems.append(f"в stderr нет пути слоя: {said.strip()[:200]!r}")
            report(not problems, f"битый слой «{kind}»: не падает, реестр пакета, путь слоя в stderr", problems)
    finally:
        if saved_door is None:
            os.environ.pop("HARNESS_PROCESS_LIBRARY_LOCAL")
        else:
            os.environ["HARNESS_PROCESS_LIBRARY_LOCAL"] = saved_door

print("── 13. судья поля «состояние_из» (hr.zapuskov-none-net-informacii-znachit §3.5) ──")
# Поле говорит разрешителю состояния, откуда брать живость записи вне соглашения harness-<id>.*;
# кривое или мёртвое поле дало бы вечное «неизвестно» без единой красной строки в воротах.
JUDGE = HERE / "check-process-library.py"


def judged(records_list: list, unit_lines: list) -> tuple:
    with tempfile.TemporaryDirectory(prefix="pl-judge-") as stand:
        cfg = Path(stand) / "harness" / "config"
        cfg.mkdir(parents=True)
        (cfg / "process-library.json").write_text(json.dumps({"процессы": records_list}, ensure_ascii=False),
                                                  encoding="utf-8")
        (cfg / "process-library.yaml").write_text("процессы: {}\n", encoding="utf-8")
        (Path(stand) / "crontab.txt").write_text("", encoding="utf-8")
        (Path(stand) / "units.txt").write_text("".join(u + " enabled enabled\n" for u in unit_lines), encoding="utf-8")
        import subprocess
        done = subprocess.run([sys.executable, str(JUDGE), "--root", stand, "--crontab", str(Path(stand) / "crontab.txt"),
                               "--units", str(Path(stand) / "units.txt"), "--project-dir", stand],
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr


def judged_record(ident: str, launch: str, source) -> dict:
    return {"id": ident, "название": ident, "класс": "сторонний", "запуск": launch, "статус": "работает", "файлы": [],
            "доля_нагрузки": {"приписка": {"способ": "cgroup", "признак": "-"}, "замер": "не измерено"},
            "состояние_из": source}


field_records = [judged_record("rot", "таймер", {"systemd": ["logrotate.timer", "logrotate.service"]}),
                 judged_record("bent-empty", "контейнер", {}),
                 judged_record("bent-two", "контейнер", {"docker": "a", "cgroup": "b"}),
                 judged_record("bent-blank", "контейнер", {"systemd": []}),
                 judged_record("fine", "контейнер", {"docker": "game-server"})]
rc, said = judged(field_records, ["logrotate.service"])
lines = said.splitlines()
report(rc == 1 and any(l.startswith("НАРУШЕНИЕ мёртвая запись: rot :: logrotate.timer") for l in lines),
       "--units без logrotate.timer → «мёртвая запись: rot :: logrotate.timer» (полный список юнитов, не только harness-*)",
       [f"rc={rc}"] + lines[-4:])
report(not any(":: logrotate.service" in l for l in lines),
       "logrotate.service есть в списке юнитов → нарушения по нему нет, «без записи» по-прежнему только harness-*",
       [l for l in lines if "logrotate.service" in l])
crooked = [i for i in ("bent-empty", "bent-two", "bent-blank")
           if not any(l.startswith(f"НАРУШЕНИЕ кривая запись: {i} :: состояние_из") for l in lines)]
report(rc == 1 and not crooked,
       "состояние_из: {} / два ключа / пустое значение → «кривая запись: <id> :: состояние_из»", crooked + lines[-3:])
report(not any(": fine ::" in l for l in lines), "состояние_из: {docker: …} ровно с одним ключом — нарушений нет",
       [l for l in lines if ": fine ::" in l])
rc, said = judged(field_records[:1], ["logrotate.timer", "logrotate.service"])
report(rc == 0, "оба юнита состояние_из.systemd в списке → чисто", [f"rc={rc}"] + said.splitlines()[-3:])

print(f"БИБЛИОТЕКА ПРОЦЕССОВ: путей {paths}, неудач {failures}")
if failures:
    sys.exit(1)
print(json.dumps({"проверка": "библиотека процессов", "путей": paths, "неудач": 0}, ensure_ascii=False))
