#!/usr/bin/env python3
"""Проба суточной сводки ресурсов: resource_report.py и обёртка resource-report.sh.

Что судится. Разбор ГОТОВОГО текста docker stats / ps / top / du (единицы, имена
с пробелом, кривые строки, битая строка docker stats не снимает соседей), отбор
(пороги включительно, топ, родитель каталога не дублирует ребёнка, процесс
контейнера не назван дважды), формат из двух блоков «За сутки» и «Сейчас» и
потолок 700 знаков при длинных именах. Итог за сутки: журнал выборок (--sample
дописывает ровно одну строку, чистит по KEEP_HOURS и битые строки), агрегация
(среднее процессора по ВСЕМ выборкам окна, пик и среднее памяти), честные
«данных пока N ч» / «меньше часа» / «данных пока нет». Сбор `measure` идёт на
заглушках команд: свой процесс сборщика и сам top в сводку не попадают. Обёртка —
на стенде: выборка до проверки времени, но не в --dry-run и --selftest; окно
времени, в том числе «9:00» и «08:05» на подменённых часах; отметка дня;
недоставка не ставит отметку; --force шлёт мимо окна и отметки.

Стенд — свой временный каталог на случай: LOG_DIR, HEARTBEAT_DIR, PROJECT_DIR с
заглушкой tg_send.sh, конфиги стенда, журнал выборок через LOG_DIR стенда или
RESOURCE_REPORT_SAMPLES, заглушки docker/top/du/df/date первыми в PATH. Живой
LOG_DIR, /etc/harness и канал не трогаются.

Двери для мутаций: RESOURCE_REPORT_PY_UNDER_TEST, RESOURCE_REPORT_SH_UNDER_TEST
(обёртка зовёт resource_report.py, лежащий рядом с ней).

    python3 scripts/test_resource_report.py
Итог: «СВОДКА РЕСУРСОВ: случаев N, неудач M», код 0/1.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def demon_file(name: str) -> Path:
    # Проект: scripts/ рядом с harness/demons; пакет: harness/scripts/ рядом с demons.
    for candidate in (HERE.parent / "harness/demons" / name, HERE.parent / "demons" / name):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness/demons" / name


REPORT_PY = Path(os.environ.get("RESOURCE_REPORT_PY_UNDER_TEST") or demon_file("resource_report.py"))
REPORT_SH = Path(os.environ.get("RESOURCE_REPORT_SH_UNDER_TEST") or demon_file("resource-report.sh"))
CHANNEL_GUARD = HERE / "check-channel-messages.py"
REAL_DATE = shutil.which("date") or "/usr/bin/date"
TIMEOUT = 60
HOUR = 3600

failures = 0
total = 0


def check(ok: bool, what: str, detail: object = "") -> None:
    global failures, total
    total += 1
    if ok:
        print(f"  ок    {what}")
    else:
        failures += 1
        print(f"  ПЛОХО {what}" + (f" — {detail!r}" if detail != "" else ""))


def load(path: Path):
    spec = importlib.util.spec_from_file_location("resource_report_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(rr, **extra: str) -> dict:
    # Ключи из DEFAULTS проверяемого модуля: проба не держит свою копию списка.
    return dict(rr.DEFAULTS, LOG_DIR="/nonexistent-log-dir", **extra)


def sample_line(now: float, age_h: float, items: list[dict]) -> str:
    return json.dumps({"ts": now - age_h * HOUR, "items": items})


def one(name: str, cpu: float, mem: float) -> list[dict]:
    return [{"name": name, "cpu": cpu, "mem_mb": mem}]


# ── разбор ──────────────────────────────────────────────────────────────────

def case_parsers(rr) -> None:
    print("разбор docker stats / ps / top / du")
    docker = ("game-server|36.22%|738.8MiB / 3.418GiB\n"
              "harness-db|0.50%|1.5GiB / 7.6GiB\n"
              "tiny|0.00%|512KiB / 1GiB\n"
              "stopped|0.00%|0B / 0B\n\n")
    rows = {r["name"]: r for r in rr.parse_docker_stats(docker)}
    check(sorted(rows) == ["game-server", "harness-db", "stopped", "tiny"],
          "docker: четыре строки, пустая отброшена", sorted(rows))
    check(abs(rows["harness-db"]["mem_mb"] - 1536) < 0.01 and rows["harness-db"]["cpu"] == 0.5,
          "docker: 1.5GiB = 1536 МБ, 0.50% = 0.5", rows.get("harness-db"))
    check(abs(rows["tiny"]["mem_mb"] - 0.5) < 1e-9 and rows["stopped"]["mem_mb"] == 0,
          "docker: KiB и 0B", (rows.get("tiny"), rows.get("stopped")))
    check(abs(rr.to_mb("100kB") - 0.1) < 1e-9 and abs(rr.to_mb("3.418GiB") - 3500.0) < 1,
          "to_mb: kB и GiB")
    broken = ("web|1.00%|20MiB / 1GiB\n"
              "x|1%|мусор / 2GiB\n"
              "y|--|30MiB / 1GiB\n"
              "db|2.00%|40MiB / 1GiB\n")
    check([r["name"] for r in rr.parse_docker_stats(broken)] == ["web", "db"],
          "docker: битые строки (размер, процент) выпали, соседи до и после целы",
          rr.parse_docker_stats(broken))

    # Так ps печатает на боевом: у tmux и npm exec имя с пробелом.
    ps = ("tmux: server     1.0  7420\n"
          "tmux: server     0.5  4000\n"
          "npm exec @playw  0.1 89612\n"
          "claude           6.4 424420\n"
          "claude           0.6 180352\n"
          "кривая строка\n\n")
    procs = {p["name"]: p for p in rr.parse_ps(ps)}
    check(sorted(procs) == ["claude", "npm exec @playw", "tmux: server"],
          "ps: имена с пробелом не теряются, кривая строка отброшена", sorted(procs))
    tmux = procs.get("tmux: server", {"cpu": -1, "mem_mb": -1})
    check(abs(tmux["cpu"] - 1.5) < 1e-9 and abs(tmux["mem_mb"] - 11420 / 1024) < 1e-9,
          "ps: одноимённые сложены (процессор и память)", tmux)

    top = parse_top_sample(rr)
    check(set(top) == {"claude", "tmux: server"},
          "top: только вторая выборка, без себя и без процесса skip_pid", sorted(top))
    claude = top.get("claude", {"cpu": -1, "mem_mb": -1})
    check(abs(claude["cpu"] - 9.0) < 1e-9 and abs(claude["mem_mb"] - 2048) < 1e-6,
          "top: одноимённые сложены, RES «1.5g»/«512m» переведены", claude)

    du = "8371\t/srv/game\nx\t/bad\n\n12\t/srv/путь с пробелом\n"
    dirs = rr.parse_du(du)
    check(dirs == [{"path": "/srv/game", "mb": 8371}, {"path": "/srv/путь с пробелом", "mb": 12}],
          "du: кривые строки отброшены, путь с пробелом цел", dirs)


def parse_top_sample(rr) -> dict:
    text = ("top - 10:00:00 up 1 day\n  PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND\n"
            "    1 root 20 0 1 99 9 S 99.0 0.1 0:01.00 first\n"
            "top - 10:00:01 up 1 day\n  PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND\n"
            "   10 a 20 0 1 7420 9 S 1.0 0.1 0:01.00 tmux: server\n"
            "   11 a 20 0 1 1.5g 9 R 5.0 0.1 0:01.00 claude\n"
            "   12 a 20 0 1 512m 9 S 4.0 0.1 0:01.00 claude\n"
            "   13 a 20 0 1 1000 9 R 80.0 0.1 0:00.01 top\n"
            "  777 a 20 0 1 9000 9 R 99.0 0.1 0:00.01 python3\n")
    return {r["name"]: r for r in rr.parse_top(text, skip_pid=777)}


# ── отбор ───────────────────────────────────────────────────────────────────

def case_selection(rr) -> None:
    print("отбор: каталоги, пороги, топ, двойники")
    dirs = rr.parse_du("8371\t/srv/game\n8371\t/srv\n5918\t/var\n2069\t/var/lib\n"
                       "1500\t/var/lib/docker\n1200\t/var/lib2\n999\t/var/log\n1000\t/home/a\n")
    leaves = [r["path"] for r in rr.top_leaves(dirs, ["/srv", "/var", "/home"], 1000, 5)]
    check(leaves == ["/srv/game", "/var/lib/docker", "/var/lib2", "/home/a"],
          "каталоги: корни и родители выпали, сосед-префикс /var/lib2 остался, 1000 включительно",
          leaves)
    check(len(rr.top_leaves(dirs, ["/srv", "/var", "/home"], 1000, 2)) == 2, "каталоги: топ режет")

    cfg = config(rr)
    procs = [{"name": "at5", "cpu": 5.0, "mem_mb": 200.0},
             {"name": "below", "cpu": 4.9, "mem_mb": 199.9}]
    msg = rr.build_message([], procs, [], 50, cfg)
    check("at5 0,1 ядра" in msg and "at5 200 МБ" in msg and "below" not in msg,
          "пороги процессора и памяти включительно, ниже — выпадает", msg)

    many = [{"name": f"p{i}", "cpu": 10.0 + i, "mem_mb": 300.0 + i} for i in range(5)]
    msg = rr.build_message([], many, [], 50, dict(cfg, RESOURCE_REPORT_TOP="2"), ncpu=4)
    check("p4 0,1 ядра (4 %), p3 0,1 ядра (3 %);" in msg and "p2" not in msg, "топ из конфига: два самых крупных", msg)

    cont = [{"name": "game-server", "cpu": 36.2, "mem_mb": 738.8}]
    procs = [{"name": "game_serv", "cpu": 36.6, "mem_mb": 696.1},
             {"name": "claude", "cpu": 7.0, "mem_mb": 590.6}]
    msg = rr.build_message(cont, procs, [], 61, cfg)
    check("game_serv" not in msg and msg.count("game-server") == 2,
          "процесс контейнера не назван дважды", msg)
    items = {i["name"] for i in rr.sample_items(cont, procs)}
    check(items == {"game-server", "claude"}, "в выборку процесс контейнера тоже не идёт дважды", items)


# ── формат ──────────────────────────────────────────────────────────────────

def case_format(rr) -> None:
    print("формат, потолок, пустой docker")
    cont = rr.parse_docker_stats("game-server|36.22%|738.8MiB / 3.418GiB\nweb|0.10%|20MiB / 1GiB\n")
    procs = rr.parse_ps("game_serv 36.6 712840\nclaude 6.4 424420\nclaude 0.6 180352\n")
    dirs = rr.parse_du("8371\t/srv/game\n8371\t/srv\n2069\t/var/lib\n300\t/var/log\n")
    msg = rr.build_message(cont, procs, dirs, 61, config(rr), ncpu=4)
    expected = ("За сутки: данных пока нет.\n\n"
                "Сейчас:\nпроцессор — game-server 0,4 ядра (9 %), claude 0,1 ядра (2 %);\n"
                "память — game-server 739 МБ, claude 591 МБ;\n"
                "диск занят на 61%, крупнее всего — /srv/game 8,2 ГБ, /var/lib 2,0 ГБ.")
    check(msg == expected, "готовый текст слово в слово (без журнала выборок)", msg)

    quiet = rr.build_message([], [], [], 10, config(rr))
    check(quiet.count("ничего заметного") == 3 and "диск занят на 10%" in quiet,
          "всё пусто — три «ничего заметного»", quiet)
    no_docker = rr.build_message([], procs, dirs, 61, config(rr))
    check("game_serv 0,4 ядра" in no_docker and "claude 0,1 ядра" in no_docker,
          "без docker процессы остаются в сводке", no_docker)
    # Владелец: «Как это 175%» — проценты одного ядра суммой по одноимённым; сейчас ядра и доля машины.
    busy = rr.build_message([], [{"name": "python3", "cpu": 175.0, "mem_mb": 10.0}], [], 61,
                            config(rr), ncpu=4)
    check("процессор — python3 1,8 ядра (44 %);" in busy and "175" not in busy,
          "«Сейчас»: 175% одного ядра = 1,8 ядра, 44 % машины на 4 ядрах", busy)

    long_name = "x" * 30 + "-very-long-compose-project-name-service-replica-1"
    long_path = "/srv/" + "/".join(["очень-длинный-каталог"] * 6)
    cont = [{"name": f"{long_name}-{i}", "cpu": 90.0 - i, "mem_mb": 4000.0 + i} for i in range(3)]
    dirs = [{"path": f"{long_path}-{i}", "mb": 900000 + i} for i in range(3)]
    now = 1_000_000.0
    day = rr.aggregate([sample_line(now, 24 - h, [dict(c, cpu=300.0) for c in cont]) for h in range(25)],
                       now, 24)
    msg = rr.build_message(cont, [], dirs, 100, config(rr), day)
    check(len(msg) <= rr.MAX_CHARS, f"длинные имена в обоих блоках: длина {len(msg)} не выше {rr.MAX_CHARS}",
          msg)
    check("…" in msg and msg.endswith(".") and "За сутки (24 ч):" in msg and "Сейчас:" in msg,
          "длинные имена обрезаны «…», оба блока и конец целы", msg)
    check(f"{long_name}-0" not in msg and "-0 0,9 ядра" in msg,
          "обрезанное имя сохраняет хвост, по которому его отличают", msg)

    guard = subprocess.run([sys.executable, "-B", str(CHANNEL_GUARD), "--текст", msg],
                           capture_output=True, text=True, timeout=TIMEOUT)
    check(guard.returncode == 0, "сторож канала пропускает сводку", (guard.returncode, guard.stdout[-300:]))


# ── итог за сутки ───────────────────────────────────────────────────────────

def case_daily(rr) -> None:
    print("итог за сутки: агрегация, чистка, честное «данных пока»")
    now = 1_000_000.0
    # Четыре выборки за 3 ч; «gpu» есть в одной: 200% / 4 выборки = 0,5 ядра, а не 2.
    lines = [sample_line(now, 3, one("gpu", 200, 300) + one("claude", 100, 100)),
             sample_line(now, 2, one("claude", 100, 300)),
             sample_line(now, 1, one("claude", 100, 200)),
             "битая строка",
             '{"ts": "вчера", "items": []}',
             sample_line(now, 0, []),
             sample_line(now, 30, one("old", 900, 9000))]
    day = rr.aggregate(lines, now, 24)
    items = {i["name"]: i for i in day["items"]}
    check(day["samples"] == 4 and abs(day["hours"] - 3) < 1e-9,
          "окно: 4 целые выборки за 3 ч, битые и старше окна не в счёт", (day["samples"], day["hours"]))
    check("old" not in items, "выборка старше окна не входит в итог", sorted(items))
    gpu = items.get("gpu", {"cores": -1})
    check(abs(gpu["cores"] - 0.5) < 1e-9, "процессор делится на ВСЕ выборки окна (нули в счёт)", gpu)
    claude = items.get("claude", {"cores": -1, "peak_mb": -1, "avg_mb": -1})
    check(abs(claude["cores"] - 0.75) < 1e-9, "процессор: 300% за 4 выборки = 0,75 ядра", claude)
    check(claude["peak_mb"] == 300 and abs(claude["avg_mb"] - 200) < 1e-9,
          "память: пик 300 и среднее 200 по выборкам, где имя есть", claude)

    twice = rr.aggregate([sample_line(now, 0, one("node", 30, 100) + one("node", 20, 150))], now, 24)
    node = twice["items"][0] if twice["items"] else {}
    check(abs(node.get("cores", -1) - 0.5) < 1e-9 and node.get("peak_mb") == 250,
          "одноимённые в одной выборке складываются", node)

    msg = rr.build_message([], [], [], 10, config(rr), day)
    check(msg.startswith("За сутки (данных пока 3 ч):") and "claude 0,8 ядра" in msg
          and "gpu 0,5 ядра" in msg and "claude 300 МБ (в среднем 200 МБ)" in msg,
          "неполный день: «данных пока 3 ч», ядра, пик и среднее названы", msg)
    tiny = rr.aggregate([sample_line(now, 0.2, one("a", 10, 100)), sample_line(now, 0, one("a", 10, 100))],
                        now, 24)
    msg = rr.build_message([], [], [], 10, config(rr), tiny)
    check(msg.startswith("За сутки (данных пока меньше часа):"), "двенадцать минут — «меньше часа»", msg)
    msg = rr.build_message([], [], [], 10, config(rr), rr.aggregate(["мусор"], now, 24))
    check(msg.startswith("За сутки: данных пока нет.\n\nСейчас:"), "журнал из одного мусора — «данных пока нет»",
          msg)
    full = rr.aggregate([sample_line(now, 24 - h, one("a", 10, 100)) for h in range(25)], now, 24)
    msg = rr.build_message([], [], [], 10, config(rr), full)
    check(msg.startswith("За сутки (24 ч):"), "полный день — «За сутки (24 ч):» без «пока»", msg)
    quiet = rr.aggregate([sample_line(now, 24 - h, one("a", 1, 10)) for h in range(25)], now, 24)
    msg = rr.build_message([], [], [], 10, config(rr), quiet)
    check(msg.count("ничего заметного") == 5, "мелочь ниже порогов итога — «ничего заметного»", msg)

    kept = rr.prune_samples([sample_line(now, 49, []), sample_line(now, 47, []), "{плохо",
                             '{"ts": 1}', sample_line(now, 0, [])], now, 48)
    check(len(kept) == 2, "чистка: старше KEEP и битые выброшены, свежие оставлены", kept)


# ── сбор на заглушках ───────────────────────────────────────────────────────

# Как настоящий top -b -n 2: первая выборка — доля за жизнь, вторая — сейчас;
# себя и вызвавший процесс ($PPID — сборщик) top тоже видит.
STUB_TOP = """#!/usr/bin/env bash
head='  PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND'
printf 'top - 10:00:00 up 1 day\\n%s\\n  100 a 20 0 1 424420 9 S 99.0 1.0 0:01.00 claude\\n' "$head"
printf 'top - 10:00:01 up 1 day\\n%s\\n' "$head"
printf '  100 a 20 0 1 424420 9 S 6.4 1.0 0:01.00 claude\\n'
printf '  %s a 20 0 1 9000 9 R 99.0 0.1 0:00.01 python3\\n' "$PPID"
printf '  101 a 20 0 1 1000 9 R 80.0 0.1 0:00.01 top\\n'
"""
STUB_DU = "#!/usr/bin/env bash\nprintf '2048\\t%s/data\\n4096\\t%s\\n' \"${@: -1}\" \"${@: -1}\"\n"
STUB_DF = '#!/usr/bin/env bash\nprintf "Use%%\\n %s\\n" "${STUB_DF_PCT:-42%}"\n'
# Часы стенда: STUB_HOUR/STUB_MIN подменяют только `date +%H` и `date +%M`.
STUB_DATE = f"""#!/usr/bin/env bash
case "$*" in
  +%H) [ -n "${{STUB_HOUR:-}}" ] && {{ echo "$STUB_HOUR"; exit 0; }} ;;
  +%M) [ -n "${{STUB_MIN:-}}" ] && {{ echo "$STUB_MIN"; exit 0; }} ;;
esac
exec {REAL_DATE} "$@"
"""
STUB_DOCKER = "#!/usr/bin/env bash\nprintf 'web|12.00%%|300MiB / 1GiB\\nbad|x%%|1MiB / 1GiB\\ndb|20.00%%|80MiB / 1GiB\\n'\n"


def make_stubs(stand: Path, docker: str | None) -> Path:
    stubs = stand / "stubs"
    stubs.mkdir()
    files = {"top": STUB_TOP, "du": STUB_DU, "df": STUB_DF, "date": STUB_DATE}
    if docker is not None:
        files["docker"] = docker
    for name, body in files.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    return stubs


def measure_with(rr, stubs: Path, path_tail: str, cfg: dict) -> str:
    saved = os.environ["PATH"]
    os.environ["PATH"] = f"{stubs}{path_tail}"
    try:
        return rr.measure(cfg)
    finally:
        os.environ["PATH"] = saved


def case_measure(rr) -> None:
    print("сбор на заглушках")
    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        samples = Path(d) / "samples.jsonl"
        cfg = config(rr, RESOURCE_REPORT_DU_ROOTS="/srv", RESOURCE_REPORT_SAMPLES=str(samples))
        msg = measure_with(rr, make_stubs(Path(d), STUB_DOCKER), os.pathsep + os.environ["PATH"], cfg)
        check("python3" not in msg and "top 0,8" not in msg and "web 0,1 ядра" in msg and "claude 0,1 ядра" in msg,
              "свой процесс сборщика и сам top не в сводке, docker и процессы есть", msg)
        check("claude 414 МБ" in msg, "процесс взят из второй выборки top", msg)
        check("web 300 МБ" in msg and "db 0,2 ядра" in msg,
              "битая строка docker stats не сняла соседа после неё", msg)
        check(f"db 0,2 ядра ({20 / os.cpu_count():.0f} %)" in msg,
              "сбор без ncpu: доля от числа ядер этой машины", msg)
        check("/srv/data 2,0 ГБ" in msg and "диск занят на 42%" in msg, "du и df разобраны", msg)
        check(msg.startswith("За сутки: данных пока нет.") and not samples.exists(),
              "нет журнала — «данных пока нет», сбор журнал не создаёт", msg)
        now = time.time()
        samples.write_text("\n".join(sample_line(now, 5 - h, one("claude", 150, 700)) for h in range(6)) + "\n")
        msg = measure_with(rr, Path(d) / "stubs", os.pathsep + os.environ["PATH"], cfg)
        check(msg.startswith("За сутки (данных пока 5 ч):\nпроцессор — claude 1,5 ядра;"),
              "сбор читает журнал из RESOURCE_REPORT_SAMPLES", msg)

    empty = "#!/usr/bin/env bash\nexit 0\n"
    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        cfg = config(rr, RESOURCE_REPORT_DU_ROOTS="/srv", RESOURCE_REPORT_SAMPLES=str(Path(d) / "s.jsonl"))
        msg = measure_with(rr, make_stubs(Path(d), empty), os.pathsep + os.environ["PATH"], cfg)
    check("Сейчас:\nпроцессор — claude 0,1 ядра" in msg, "docker без контейнеров: сводка из процессов и диска", msg)

    # PATH из одних заглушек: docker нет вовсе. python и env берутся по полному пути.
    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        stubs = make_stubs(Path(d), None)
        for tool in ("bash", "env", "printf", "head"):
            target = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True,
                                    text=True).stdout.strip()
            if target.startswith("/"):
                (stubs / tool).symlink_to(target)
        cfg = config(rr, RESOURCE_REPORT_DU_ROOTS="/srv", RESOURCE_REPORT_SAMPLES=str(Path(d) / "s.jsonl"))
        msg = measure_with(rr, stubs, "", cfg)
    check("claude 0,1 ядра" in msg and "диск занят на 42%" in msg, "docker не установлен: сводка есть", msg)


# ── выборка отдельным процессом ─────────────────────────────────────────────

def run_sample(root: Path, stubs: Path, **extra: str) -> subprocess.CompletedProcess:
    (root / "harness.conf").write_text('RESOURCE_REPORT_TIME="09:00"\n')
    env = dict(os.environ, HARNESS_CONF=str(root / "harness.conf"), HARNESS_HOME=str(root),
               LOG_DIR=str(root / "log-not-used"),
               PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}", **extra)
    return subprocess.run([sys.executable, str(REPORT_PY), "--sample"], env=env, capture_output=True,
                          text=True, timeout=TIMEOUT)


def case_sample() -> None:
    print("--sample: одна строка, чистка по KEEP_HOURS")
    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        root = Path(d)
        stubs = make_stubs(root, STUB_DOCKER)
        samples = root / "deep" / "samples.jsonl"
        before = time.time()
        r = run_sample(root, stubs, RESOURCE_REPORT_SAMPLES=str(samples))
        lines = samples.read_text().splitlines() if samples.exists() else []
        check(r.returncode == 0 and len(lines) == 1, "пустой журнал: ровно одна строка, каталог создан",
              (r.returncode, lines, r.stderr[-300:]))
        row = json.loads(lines[0]) if lines else {"ts": 0, "items": []}
        names = {i["name"] for i in row["items"]}
        check(before - 1 <= row["ts"] <= time.time() + 1 and names == {"web", "db", "claude"},
              "строка: время сейчас, контейнеры и процессы, без себя, top и битой строки docker", row)
        check(not (root / "log-not-used").exists(), "RESOURCE_REPORT_SAMPLES старше LOG_DIR")

        now = time.time()
        seed = [sample_line(now, 49, []), "{битая", sample_line(now, 47, one("a", 1, 1)),
                sample_line(now, 1, one("b", 1, 1))]
        samples.write_text("\n".join(seed) + "\n")
        r = run_sample(root, stubs, RESOURCE_REPORT_SAMPLES=str(samples))
        lines = samples.read_text().splitlines()
        check(r.returncode == 0 and len(lines) == 3 and lines[:2] == seed[2:],
              "KEEP 48: старше 48 ч и битая ушли, две свежие на месте и в том же порядке, новая в конце",
              lines)
        r = run_sample(root, stubs, RESOURCE_REPORT_SAMPLES=str(samples), RESOURCE_REPORT_KEEP_HOURS="2")
        lines = samples.read_text().splitlines()
        check(r.returncode == 0 and len(lines) == 3 and lines[0] == seed[3],
              "KEEP 2 из окружения: осталась часовая и две новые", lines)
        leftovers = [p.name for p in samples.parent.iterdir() if p.name != samples.name]
        check(leftovers == [], "временных файлов рядом с журналом нет", leftovers)


# ── обёртка на стенде ───────────────────────────────────────────────────────

TG_STUB = """#!/usr/bin/env bash
printf '%s\\n---\\n' "$*" >> "$(dirname "$0")/../tg-calls.log"
exit "${STUB_TG_RC:-0}"
"""


class Stand:
    def __init__(self, root: Path):
        self.root = root
        self.log = root / "log"
        self.beat = root / "beat"
        self.project = root / "project"
        for d in (self.log, self.beat, self.project / "scripts", root / "etc"):
            d.mkdir(parents=True)
        tg = self.project / "scripts" / "tg_send.sh"
        tg.write_text(TG_STUB)
        tg.chmod(0o755)
        (root / "etc" / "install.conf").write_text(
            f'PROJECT_DIR="{self.project}"\nLOG_DIR="{self.log}"\nHEARTBEAT_DIR="{self.beat}"\n')
        (root / "etc" / "harness.conf").write_text('RESOURCE_REPORT_TIME="09:00"\n')
        self.stubs = make_stubs(root, STUB_DOCKER)
        self.sent = self.log / "resource-report.sent"
        self.samples = self.log / "resource-samples.jsonl"
        # Обёртка ищет config.sh в ../../scripts/lib — так она лежит только после
        # раскладки; стенд раскладывает её сам, и проба одинакова в проекте и пакете.
        demons = root / "tree" / "harness" / "demons"
        demons.mkdir(parents=True)
        (root / "tree" / "scripts").mkdir()
        (root / "tree" / "scripts" / "lib").symlink_to(HERE / "lib")
        for f in (REPORT_SH, REPORT_SH.parent / "resource_report.py"):
            shutil.copy2(f, demons / f.name)
        self.wrapper = demons / REPORT_SH.name

    def run(self, *args: str, at: str, now: str = "", **extra: str) -> subprocess.CompletedProcess:
        hour, _, minute = now.partition(":")
        env = dict(os.environ, HARNESS_HOME=str(self.root / "etc"),
                   HARNESS_CONF=str(self.root / "etc" / "harness.conf"),
                   HARNESS_INSTALL_CONF=str(self.root / "etc" / "install.conf"),
                   PROJECT_DIR=str(self.project), LOG_DIR=str(self.log),
                   HEARTBEAT_DIR=str(self.beat), RESOURCE_REPORT_TIME=at,
                   RESOURCE_REPORT_DU_ROOTS="/srv", STUB_HOUR=hour, STUB_MIN=minute,
                   PATH=f"{self.stubs}{os.pathsep}{os.environ['PATH']}", **extra)
        env.pop("RESOURCE_REPORT_SAMPLES", None)
        return subprocess.run(["bash", str(self.wrapper), *args], env=env, capture_output=True,
                              text=True, timeout=TIMEOUT)

    def calls(self) -> list[str]:
        log = self.project / "tg-calls.log"
        if not log.exists():
            return []
        return [c.strip() for c in log.read_text().split("---\n") if c.strip()]

    def sample_count(self) -> int:
        return len(self.samples.read_text().splitlines()) if self.samples.exists() else 0


def case_wrapper() -> None:
    print("обёртка: выборка, окно времени, отметка дня, недоставка, ключи")
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    # «24:00» больше любого часа дня, «00:00» не больше никакого.
    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        s = Stand(Path(d))
        r = s.run(at="24:00")
        check(r.returncode == 0 and not s.calls() and not s.sent.exists()
              and (s.beat / "resource-report").exists(),
              "до часа отправки: тишина, отметки нет, метка свежести есть", (r.returncode, r.stdout, r.stderr))
        check(s.sample_count() == 1, "до часа отправки выборка всё равно записана (до проверки времени)",
              (s.sample_count(), r.stdout, r.stderr[-300:]))

        r = s.run(at="00:00")
        calls = s.calls()
        check(r.returncode == 0 and len(calls) == 1 and calls[0].startswith("За сутки (данных пока меньше часа):")
              and "\n\nСейчас:\n" in calls[0] and "web 0,1 ядра" in calls[0],
              "час пришёл: ровно одна отправка, оба блока, итог честно «меньше часа»",
              (r.returncode, calls, r.stderr))
        check(s.sample_count() == 2, "отправляющий проход тоже пишет выборку", s.sample_count())
        check(s.sent.read_text().strip() == today if s.sent.exists() else False,
              "после доставки отметка дня = сегодня", s.sent.read_text() if s.sent.exists() else "нет")

        r = s.run(at="00:00")
        check(r.returncode == 0 and len(s.calls()) == 1 and s.sample_count() == 3,
              "слали сегодня — второй проход молчит, выборку пишет", (r.returncode, len(s.calls())))

        s.sent.write_text(yesterday + "\n")
        r = s.run(at="00:00")
        check(r.returncode == 0 and len(s.calls()) == 2 and s.sent.read_text().strip() == today,
              "вчерашняя отметка не мешает: шлёт и ставит сегодняшнюю", (r.returncode, len(s.calls())))

    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        s = Stand(Path(d))
        r = s.run(at="00:00", STUB_TG_RC="1")
        check(r.returncode != 0 and len(s.calls()) == 1 and not s.sent.exists(),
              "канал не доставил: код не ноль, отметки дня нет", (r.returncode, r.stdout))

        r = s.run(at="00:00", STUB_DF_PCT="мусор")
        check(r.returncode != 0 and len(s.calls()) == 1 and not s.sent.exists(),
              "сбор сломан: не шлёт, отметки нет", (r.returncode, r.stdout[-300:]))

    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        s = Stand(Path(d))
        r = s.run("--dry-run", at="00:00")
        check(r.returncode == 0 and r.stdout.startswith("За сутки: данных пока нет.\n\nСейчас:")
              and not s.calls() and not s.sent.exists(),
              "--dry-run печатает, не шлёт, отметку не ставит", (r.returncode, r.stdout[-200:]))
        r2 = s.run("--selftest", at="00:00")
        check(r.returncode == 0 and r2.returncode == 0 and "зелёный" in r2.stdout,
              "--selftest зелёный", (r2.returncode, r2.stdout[-300:]))
        check(not s.samples.exists() and not s.calls(), "--dry-run и --selftest выборку не пишут",
              s.sample_count())

    with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
        s = Stand(Path(d))
        s.sent.write_text(today + "\n")
        r = s.run("--force", at="24:00")
        check(r.returncode == 0 and len(s.calls()) == 1 and s.sent.read_text().strip() == today,
              "--force шлёт мимо окна и отметки, отметка = сегодня", (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        r = s.run(at="00:00")
        check(r.returncode == 0 and len(s.calls()) == 1, "после --force обычный проход молчит",
              (r.returncode, len(s.calls())))

    print("обёртка: время без ведущего нуля и с нулём на подменённых часах")
    # (час отправки, часы стенда, ждём отправку)
    for at, now, sends in (("9:00", "08:59", False), ("9:00", "09:00", True),
                           ("08:05", "08:04", False), ("08:05", "08:05", True),
                           ("08:05", "09:00", True), ("9:00", "10:08", True)):
        with tempfile.TemporaryDirectory(prefix="ресурсы-стенд-") as d:
            s = Stand(Path(d))
            r = s.run(at=at, now=now)
            check(r.returncode == 0 and len(s.calls()) == int(sends) and s.sample_count() == 1,
                  f"RESOURCE_REPORT_TIME={at}, сейчас {now}: " + ("шлёт" if sends else "молчит"),
                  (r.returncode, len(s.calls()), r.stdout[-200:], r.stderr[-300:]))


def main() -> int:
    rr = load(REPORT_PY)
    case_parsers(rr)
    case_selection(rr)
    case_format(rr)
    case_daily(rr)
    case_measure(rr)
    case_sample()
    case_wrapper()
    print(f"СВОДКА РЕСУРСОВ: случаев {total}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
