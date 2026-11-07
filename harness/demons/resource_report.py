#!/usr/bin/env python3
"""resource_report.py — суточная сводка владельцу: на что уходят ресурсы сервера.

Что делает: снимает топ потребителей процессора, памяти и диска (контейнеры —
docker stats, процессы — top, каталоги — du) и собирает ОДНО короткое сообщение:
итог за сутки по журналу выборок и срез «сейчас». Отправка и метка свежести —
в обёртке resource-report.sh; здесь только сбор.

Почему разбор отдельно от вызова команд: разборщики берут готовый текст, и
самотест гоняет их на образцах без docker и без диска.

Режимы:
  (без ключей)  печатает сообщение в stdout;
  --sample      дописывает одну выборку в журнал выборок и чистит старые строки;
  --selftest    проверка разборщиков и формата на образцах, ничего не снимает.

Пороги, размер топа, окно итога и срок хранения выборок — ключи RESOURCE_REPORT_*
в harness.conf (данные, не код); окружение старше файла.

Коды выхода: 0 — сообщение напечатано; 1 — самотест красный; 2 — сборка сломана.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Потолок мягкий по TONE.md: длиннее 700 знаков сообщение уже «длинное».
MAX_CHARS = 700
# Девять имён по 40 знаков с рамкой укладываются в MAX_CHARS при топе 3; имя
# контейнера compose или путь глубже этого — шум, а не сведение.
NAME_MAX = 40

DEFAULTS = {
    "RESOURCE_REPORT_TOP": "3",
    "RESOURCE_REPORT_MIN_CPU_PCT": "5",
    "RESOURCE_REPORT_MIN_MEM_MB": "200",
    "RESOURCE_REPORT_MIN_DISK_MB": "1000",
    "RESOURCE_REPORT_DU_ROOTS": "/srv /var /home",
    "RESOURCE_REPORT_KEEP_HOURS": "48",
    "RESOURCE_REPORT_WINDOW_HOURS": "24",
    "RESOURCE_REPORT_SAMPLES": "",
}
# В журнал выборок идут только заметные потребители, иначе файл пухнет.
SAMPLE_MIN_CPU_PCT = 0.5
SAMPLE_MIN_MEM_MB = 50
# В итог за сутки: ниже этого потребитель не назван.
DAILY_MIN_CORES = 0.05
DAILY_MIN_MEM_MB = 50
# Меньше окна больше чем на столько часов — итог честно называется неполным.
GAP_HOURS = 1

UNIT_MB = {"B": 1 / 1048576, "KIB": 1 / 1024, "KB": 1 / 1000, "MIB": 1.0, "MB": 1.0,
           "GIB": 1024.0, "GB": 1000.0, "TIB": 1048576.0}


# ── разбор готового текста ──────────────────────────────────────────────────

def to_mb(text: str) -> float:
    """«738.8MiB» / «3.418GiB» → мегабайты."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", text)
    if not m or m.group(2).upper() not in UNIT_MB:
        raise ValueError(f"не размер: {text!r}")
    return float(m.group(1)) * UNIT_MB[m.group(2).upper()]


def parse_docker_stats(text: str) -> list[dict]:
    """Строки `Имя|CPU%|Занято / Лимит` → [{name, cpu, mem_mb}]."""
    rows = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name, cpu, mem = parts
        # Битая строка пропускается одна: остальные контейнеры остаются в сводке.
        try:
            rows.append({"name": name, "cpu": float(cpu.rstrip("%")),
                         "mem_mb": to_mb(mem.split("/")[0])})
        except ValueError:
            continue
    return rows


def parse_ps(text: str) -> list[dict]:
    """Строки `comm %cpu rss_kb` → по имени процесса суммы: у claude, node и
    прочих десятки одноимённых процессов, и по одному они ничего не говорят."""
    totals: dict[str, dict] = {}
    for line in text.splitlines():
        # Справа: у «tmux: server» и «npm exec @playw» в имени пробел.
        parts = line.rsplit(None, 2)
        if len(parts) != 3 or not parts[2].isdigit():
            continue
        row = totals.setdefault(parts[0], {"name": parts[0], "cpu": 0.0, "mem_mb": 0.0})
        row["cpu"] += float(parts[1])
        row["mem_mb"] += int(parts[2]) / 1024
    return list(totals.values())


TOP_LINE = re.compile(
    r"^\s*(\d+)\s+\S+\s+-?\d+\s+-?\d+\s+\S+\s+(\S+)\s+\S+\s+\w\s+([\d.,]+)\s+[\d.,]+\s+\S+\s+(.+?)\s*$")
TOP_UNIT_MB = {"": 1 / 1024, "k": 1 / 1024, "m": 1.0, "g": 1024.0, "t": 1048576.0, "p": 1073741824.0}


def top_res_mb(text: str) -> float:
    """RES из top: «712840» (КБ), «1.2g», «512m»."""
    m = re.fullmatch(r"([\d.,]+)([kmgtp]?)", text)
    if not m:
        raise ValueError(f"не размер top: {text!r}")
    return float(m.group(1).replace(",", ".")) * TOP_UNIT_MB[m.group(2)]


def parse_top(text: str, skip_pid: int = -1) -> list[dict]:
    """Вывод `top -b -n 2` → по имени процесса суммы из ВТОРОЙ выборки: первая у
    top — доля за жизнь процесса, и только вторая показывает загрузку сейчас.
    Сам top и процесс `skip_pid` (нас) в сводку не идут."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("top - ")]
    totals: dict[str, dict] = {}
    for line in lines[starts[-1]:] if starts else lines:
        m = TOP_LINE.match(line)
        if not m or int(m.group(1)) == skip_pid or m.group(4) == "top":
            continue
        try:
            cpu, mem = float(m.group(3).replace(",", ".")), top_res_mb(m.group(2))
        except ValueError:
            continue
        row = totals.setdefault(m.group(4), {"name": m.group(4), "cpu": 0.0, "mem_mb": 0.0})
        row["cpu"] += cpu
        row["mem_mb"] += mem
    return list(totals.values())


def parse_du(text: str) -> list[dict]:
    """Строки `МБ<TAB>путь` (du -B1M) → [{path, mb}]."""
    rows = []
    for line in text.splitlines():
        size, _, path = line.partition("\t")
        if size.strip().isdigit() and path:
            rows.append({"path": path.strip(), "mb": int(size)})
    return rows


# ── отбор и формат ──────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    return name.replace("_", "-").lower()


def drop_container_procs(procs: list[dict], containers: list[dict]) -> list[dict]:
    """Процесс контейнера виден и в ps, и в docker stats: без этого он назван
    дважды. ps режет имя до 15 знаков, поэтому сверка по началу имени."""
    names = [_norm(c["name"]) for c in containers]
    return [p for p in procs if not any(n.startswith(_norm(p["name"])) for n in names)]


def top_leaves(rows: list[dict], root_paths: list[str], min_mb: int, top: int) -> list[dict]:
    """Самые крупные каталоги не ниже порога. Родитель, чей ребёнок уже в списке,
    выпадает: «/var 5918» и «/var/lib 2069» — это одни и те же гигабайты."""
    big = [r for r in rows if r["mb"] >= min_mb and r["path"].rstrip("/") not in root_paths]
    paths = [r["path"].rstrip("/") + "/" for r in big]
    leaves = [r for r in big
              if not any(p != r["path"].rstrip("/") + "/" and p.startswith(r["path"].rstrip("/") + "/")
                         for p in paths)]
    return sorted(leaves, key=lambda r: -r["mb"])[:top]


def short(name: str) -> str:
    """Режет середину: у реплик и каталогов различает хвост, у путей — корень."""
    if len(name) <= NAME_MAX:
        return name
    half = (NAME_MAX - 1) // 2
    return name[:half] + "…" + name[-(NAME_MAX - 1 - half):]


def fmt_mb(mb: float) -> str:
    return f"{mb / 1024:.1f} ГБ".replace(".", ",") if mb >= 1024 else f"{mb:.0f} МБ"


def pick(rows: list[dict], key: str, minimum: float, top: int) -> list[dict]:
    return sorted((r for r in rows if r[key] >= minimum), key=lambda r: -r[key])[:top]


def join_or_none(items: list[str]) -> str:
    return ", ".join(items) if items else "ничего заметного"


def sample_items(containers: list[dict], procs: list[dict]) -> list[dict]:
    """Что пишется в выборку: заметные контейнеры и процессы, без дублей."""
    both = containers + drop_container_procs(procs, containers)
    return [{"name": r["name"], "cpu": round(r["cpu"], 1), "mem_mb": round(r["mem_mb"], 1)}
            for r in both if r["cpu"] >= SAMPLE_MIN_CPU_PCT or r["mem_mb"] >= SAMPLE_MIN_MEM_MB]


def parse_sample_line(line: str):
    """Строка журнала → выборка или None, если строка битая."""
    try:
        row = json.loads(line)
        if isinstance(row["ts"], (int, float)) and isinstance(row["items"], list):
            return row
    except (ValueError, KeyError, TypeError):
        pass
    return None


def prune_samples(lines: list[str], now: float, keep_hours: float) -> list[str]:
    """Оставляет целые строки не старше срока хранения; битые выбрасывает."""
    kept = []
    for line in lines:
        row = parse_sample_line(line)
        if row is not None and row["ts"] >= now - keep_hours * 3600:
            kept.append(line.strip())
    return kept


def aggregate(lines: list[str], now: float, window_hours: float) -> dict:
    """Журнал выборок → {samples, hours, items: [{name, cores, peak_mb, avg_mb}]}.
    Процессор делится на ВСЕ выборки окна (где имени нет — там ноль), память —
    только на выборки, где имя есть, иначе редкий гость выглядел бы постоянным."""
    rows = [r for r in map(parse_sample_line, lines) if r and r["ts"] >= now - window_hours * 3600]
    if not rows:
        return {"samples": 0, "hours": 0.0, "items": []}
    cpu: dict[str, float] = {}
    mem: dict[str, list[float]] = {}
    for row in rows:
        per_name: dict[str, list[float]] = {}
        for it in row["items"]:
            cell = per_name.setdefault(it["name"], [0.0, 0.0])
            cell[0] += it["cpu"]
            cell[1] += it["mem_mb"]
        for name, (c, m) in per_name.items():
            cpu[name] = cpu.get(name, 0.0) + c
            mem.setdefault(name, []).append(m)
    items = [{"name": n, "cores": cpu[n] / len(rows) / 100,
              "peak_mb": max(mem[n]), "avg_mb": sum(mem[n]) / len(mem[n])} for n in cpu]
    hours = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)) / 3600
    return {"samples": len(rows), "hours": hours, "items": items}


def fmt_cores(cores: float) -> str:
    return f"{cores:.1f} ядра".replace(".", ",")


def daily_block(daily: dict, top: int, window_hours: float) -> str:
    if daily["samples"] == 0:
        return "За сутки: данных пока нет."
    hours = round(daily["hours"])
    if window_hours - daily["hours"] > GAP_HOURS:
        head = "За сутки (данных пока " + (f"{hours} ч" if hours else "меньше часа") + "):"
    else:
        head = f"За сутки ({hours} ч):"
    by_cpu = sorted((i for i in daily["items"] if i["cores"] >= DAILY_MIN_CORES),
                    key=lambda i: -i["cores"])[:top]
    by_mem = sorted((i for i in daily["items"] if i["peak_mb"] >= DAILY_MIN_MEM_MB),
                    key=lambda i: -i["peak_mb"])[:top]
    cpu = [f"{short(i['name'])} {fmt_cores(i['cores'])}" for i in by_cpu]
    mem = [f"{short(i['name'])} {fmt_mb(i['peak_mb'])} (в среднем {fmt_mb(i['avg_mb'])})"
           for i in by_mem]
    return f"{head}\nпроцессор — {join_or_none(cpu)};\nпамять, пик — {join_or_none(mem)}."


def processes_line(processes: dict, top: int) -> str:
    """Доли по процессам харнеса из журнала диспетчера (process_load.summarize)."""
    if not processes["items"]:
        return "по процессам — данных пока нет."
    items = [i for i in processes["items"] if i["cores"] >= DAILY_MIN_CORES][:top]
    parts = []
    for k, i in enumerate(items):
        cores = fmt_cores(i["cores"]) if k == 0 else f"{i['cores']:.1f}".replace(".", ",")
        parts.append(f"{short(i['name'])} {cores} ({i['share_pct']:.0f} %)")
    return f"по процессам — {join_or_none(parts)}; не разобрано {processes['unparsed_pct']:.0f} %."


def build_message(containers: list[dict], procs: list[dict], dirs: list[dict],
                  disk_pct: int, cfg: dict, daily: dict | None = None,
                  processes: dict | None = None, ncpu: int | None = None) -> str:
    """Два блока: итог за сутки и срез сейчас. Не влезает в потолок — урезаем топ."""
    window = float(cfg["RESOURCE_REPORT_WINDOW_HOURS"])
    ncpu = ncpu or os.cpu_count()
    daily = daily or {"samples": 0, "hours": 0.0, "items": []}
    for n in range(int(cfg["RESOURCE_REPORT_TOP"]), 0, -1):
        # Без выборок за сутки блок уже говорит «данных пока нет»: вторая такая строка лишняя.
        by_process = "\n" + processes_line(processes, n) if processes and daily["samples"] else ""
        msg = (daily_block(daily, n, window) + by_process + "\n\n"
               + now_block(containers, procs, dirs, disk_pct, cfg, n, ncpu))
        if len(msg) <= MAX_CHARS:
            return msg
    return msg[:MAX_CHARS - 1] + "…"


def now_block(containers: list[dict], procs: list[dict], dirs: list[dict],
              disk_pct: int, cfg: dict, top: int, ncpu: int) -> str:
    min_cpu = float(cfg["RESOURCE_REPORT_MIN_CPU_PCT"])
    min_mem = float(cfg["RESOURCE_REPORT_MIN_MEM_MB"])
    min_disk = int(cfg["RESOURCE_REPORT_MIN_DISK_MB"])
    procs = drop_container_procs(procs, containers)
    roots = cfg["RESOURCE_REPORT_DU_ROOTS"].split()

    # top и docker stats дают проценты ОДНОГО ядра, суммой по одноимённым это
    # «python3 175%»; владельцу понятны ядра и доля машины, как в «За сутки».
    def cpu_item(r): return f"{short(r['name'])} {fmt_cores(r['cpu'] / 100)} ({r['cpu'] / ncpu:.0f} %)"
    def mem_item(r): return f"{short(r['name'])} {fmt_mb(r['mem_mb'])}"

    # Контейнеры и процессы вместе, одним рейтингом: владельцу важно «кто ест»,
    # а не чем это запущено.
    both = containers + procs
    cpu = [cpu_item(r) for r in pick(both, "cpu", min_cpu, top)]
    mem = [mem_item(r) for r in pick(both, "mem_mb", min_mem, top)]
    disk = [f"{short(r['path'])} {fmt_mb(r['mb'])}" for r in top_leaves(dirs, roots, min_disk, top)]
    return (f"Сейчас:\nпроцессор — {join_or_none(cpu)};\n"
            f"память — {join_or_none(mem)};\n"
            f"диск занят на {disk_pct}%, крупнее всего — {join_or_none(disk)}.")


# ── вызов команд ────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 60) -> str:
    # LC_ALL=C: top в русской локали печатает десятичную запятую.
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, LC_ALL="C")).stdout


def add_lib_path() -> bool:
    """Кладёт scripts/lib раскладки в путь импорта; в пакете и в проекте он на разной глубине."""
    for d in Path(__file__).resolve().parents:
        if (d / "scripts" / "lib" / "config.py").is_file():
            sys.path.insert(0, str(d / "scripts" / "lib"))
            return True
    return False


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if add_lib_path():
        import config                                        # noqa: PLC0415
        file_values = config.из_файла(config.путь_harness_conf())
        cfg.update({k: v for k, v in file_values.items() if k in DEFAULTS and v != ""})
        # Каталог журналов отдаёт загрузчик: окружение, файл и дефолт он уже сложил.
        cfg["LOG_DIR"] = config.log_dir()
    cfg.update({k: os.environ[k] for k in DEFAULTS if os.environ.get(k)})
    return cfg


def samples_path(cfg: dict) -> Path:
    return Path(cfg["RESOURCE_REPORT_SAMPLES"] or Path(cfg["LOG_DIR"]) / "resource-samples.jsonl")


def snapshot() -> tuple[list[dict], list[dict]]:
    """Контейнеры (docker stats) и процессы (вторая выборка top) сейчас."""
    # docker может быть выключен: тогда остаются процессы.
    try:
        containers = parse_docker_stats(run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"]))
    except (OSError, subprocess.SubprocessError):
        containers = []
    procs = parse_top(run(["top", "-b", "-n", "2", "-d", "1", "-w", "512"], timeout=30),
                      skip_pid=os.getpid())
    return containers, procs


def write_sample(cfg: dict, now: float) -> None:
    """Дописывает выборку и в том же проходе чистит старые строки; файл заменяется
    целиком через временный, чтобы обрыв посреди записи не оставил половину."""
    containers, procs = snapshot()
    line = json.dumps({"ts": int(now), "items": sample_items(containers, procs)},
                      ensure_ascii=False)
    path = samples_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = prune_samples(old, now, float(cfg["RESOURCE_REPORT_KEEP_HOURS"])) + [line]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)


def measure(cfg: dict) -> str:
    containers, procs = snapshot()
    dirs: list[dict] = []
    for root in cfg["RESOURCE_REPORT_DU_ROOTS"].split():
        dirs += parse_du(run(["du", "-x", "-d2", "-B1M", root], timeout=300))
    disk_pct = int(run(["df", "--output=pcent", "/"]).split()[-1].rstrip("%"))
    path = samples_path(cfg)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    now, window = time.time(), float(cfg["RESOURCE_REPORT_WINDOW_HOURS"])
    daily = aggregate(lines, now, window)
    add_lib_path()
    import process_load                                      # noqa: PLC0415
    by_process = process_load.summarize(process_load.read_lines(path.parent), now, window)
    return build_message(containers, procs, dirs, disk_pct, cfg, daily, by_process)


# ── самотест ────────────────────────────────────────────────────────────────

def selftest_daily(check) -> None:
    """Выборки, чистка и итог за сутки на образцах, без docker и диска."""
    now = 1_000_000.0
    hour = 3600

    def line(age_h, items):
        return json.dumps({"ts": now - age_h * hour, "items": items})

    cfg = dict(DEFAULTS)
    items = lambda cpu, mem: [{"name": "claude", "cpu": cpu, "mem_mb": mem}]  # noqa: E731
    lines = [line(24 - i, items(100, 1000 + i)) if i < 2 else line(24 - i, []) for i in range(25)]
    lines.append("битая строка")
    day = aggregate(lines, now, 24)
    claude = next(i for i in day["items"] if i["name"] == "claude")
    check(day["samples"] == 25 and abs(day["hours"] - 24) < 1e-6, f"окно посчитано неверно: {day}")
    check(abs(claude["cores"] - 2 / 25) < 1e-9, f"среднее по пустым выборкам не ноль: {claude}")
    check(claude["peak_mb"] == 1001 and abs(claude["avg_mb"] - 1000.5) < 1e-9,
          f"пик или среднее памяти неверны: {claude}")
    short_day = aggregate([line(3, items(150, 600)), line(0, items(50, 800))], now, 24)
    msg = build_message([], [], [], 10, cfg, short_day)
    check("данных пока 3 ч" in msg and "claude 1,0 ядра" in msg, "неполный день не назван: " + msg)
    tiny = aggregate([line(0.2, items(10, 100)), line(0, items(10, 100))], now, 24)
    check("меньше часа" in build_message([], [], [], 10, cfg, tiny), "«меньше часа» не названо")
    empty = build_message([], [], [], 10, cfg, aggregate([], now, 24))
    check("За сутки: данных пока нет." in empty and "Сейчас:" in empty, "пустой журнал: " + empty)
    full = build_message([], [], [], 10, cfg, day)
    check("За сутки (24 ч):" in full, "полный день назван неверно: " + full)
    old = [line(49, items(1, 1)), line(47, items(1, 1)), "{плохо", '{"ts": "x", "items": []}']
    check(len(prune_samples(old, now, 48)) == 1, "чистка по возрасту и битым строкам неверна")
    top_text = ("top - 10:00:00 up 1 day\n  PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND\n"
                "    1 root 20 0 1 99 9 S 99.0 0.1 0:01.00 first\n"
                "top - 10:00:01 up 1 day\n  PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND\n"
                "  10 a 20 0 1 712840 9 S 36.0 9.5 0:01.00 tmux: server\n"
                "  11 a 20 0 1 1.5g 9 R 5.0 0.1 0:01.00 claude\n"
                "  12 a 20 0 1 512m 9 S 4.0 0.1 0:01.00 claude\n"
                "  13 a 20 0 1 1000 9 R 80.0 0.1 0:00.01 top\n")
    parsed = {r["name"]: r for r in parse_top(top_text)}
    check(set(parsed) == {"tmux: server", "claude"}, f"top: имена неверны {set(parsed)}")
    check(abs(parsed["claude"]["cpu"] - 9.0) < 1e-9 and abs(parsed["claude"]["mem_mb"] - 2048) < 1e-6,
          "top: процессы одного имени не сложены")
    many = [{"name": "n" * 40 + str(k), "cpu": 300.0 - k, "mem_mb": 9000.0 - k} for k in range(9)]
    big = aggregate([line(0, many), line(23, many)], now, 24)
    check(len(build_message(many, many, [{"path": "/p" * 20, "mb": 5000}], 99, cfg, big)) <= MAX_CHARS,
          "длинное сообщение выше потолка")


def selftest() -> int:
    cfg = dict(DEFAULTS)
    docker = "game-server|36.22%|738.8MiB / 3.418GiB\nweb|0.10%|20MiB / 1GiB\n"
    ps = ("game_serv 36.6 712840\nclaude 6.4 424420\nclaude 0.6 180352\n"
          "bash 0.0 3000\nкривая строка\n")
    du = ("8371\t/srv/game\n8371\t/srv\n5918\t/var\n2069\t/var/lib\n"
          "1500\t/var/lib/docker\n300\t/var/log\n")
    cont, procs, dirs = parse_docker_stats(docker), parse_ps(ps), parse_du(du)
    problems = []

    def check(ok, what):
        if not ok:
            problems.append(what)

    check(len(cont) == 2 and cont[0]["mem_mb"] == 738.8 * 1 and cont[1]["mem_mb"] == 20,
          "docker stats разобран неверно")
    check(abs(to_mb("3.418GiB") - 3500.0) < 1, "GiB переведён неверно")
    claude = next(p for p in procs if p["name"] == "claude")
    check(abs(claude["cpu"] - 7.0) < 0.01 and abs(claude["mem_mb"] - 590.8) < 1,
          "процессы одного имени не сложены")
    check(len(procs) == 3, "кривая строка ps не отброшена или хорошая потеряна")
    leaves = top_leaves(dirs, ["/srv", "/var"], 1000, 3)
    check([r["path"] for r in leaves] == ["/srv/game", "/var/lib/docker"],
          f"каталоги отобраны неверно: {[r['path'] for r in leaves]}")
    msg = build_message(cont, procs, dirs, 61, cfg, ncpu=4)
    check("game-server 0,4 ядра (9 %)" in msg and msg.count("game-server") == 2,
          "контейнер не назван ровно раз в процессоре и памяти: " + msg)
    check("claude 0,1 ядра (2 %)" in msg, "claude нет в процессоре")
    check("/srv/game 8,2 ГБ" in msg, "диск без каталога-лидера")
    check(len(msg) <= MAX_CHARS, f"длина {len(msg)} выше {MAX_CHARS}")
    quiet = build_message([], [], [], 10, cfg)
    check(quiet.count("ничего заметного") == 3, "пустой топ не назван пустым")
    mixed = parse_docker_stats("x|1%|мусор / 2GiB\nweb|0.10%|20MiB / 1GiB\n")
    check([r["name"] for r in mixed] == ["web"], "битая строка docker stats сняла соседа")
    selftest_daily(check)
    for p in problems:
        print("КРАСНО:", p)
    print("resource_report selftest:", "красный" if problems else "зелёный")
    return 1 if problems else 0


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    try:
        if "--sample" in sys.argv[1:]:
            write_sample(load_config(), time.time())
            return 0
        print(measure(load_config()))
    except Exception:                                        # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
