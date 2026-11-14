#!/usr/bin/env python3
"""Проба: у ворот есть предел времени и ворота не гонят себя сами изнутри
(карточка, спека § «Пределы и вложенность»).

    python3 scripts/test_gates_bounds.py

Почему. 13.11.2026 на машине висели 92 прогона ворот до 13 часов, родитель — init:
гейт «публикация» звал publish.sh --selftest, самотест звал ЖИВОЙ gates.sh, тот снова
гейт «публикация»… Вложенный прогон при GATES_LEAD_PID шёл сам с пулом 1, и предела
не было ни у гейта, ни у прогона.

Вход — обёртка scripts/lib/gates_check.sh на стенде из фиктивных проверок (стенд
пробы scripts/test_gates_parallel_cache.py); полные ворота не запускаются. Каждый
случай — свой каталог, свои LOG_DIR и GATES_TIMING_DIR, своя сессия процессов:
висящий стенд добивается целиком и не переживает пробу.

Контракт, который судится:
  GATES_CHECK_TIMEOUT_SEC   — предел одного гейта, с (нет ключа — 3600, 0 — без предела);
  GATES_CHECK_TIMEOUT_LONG  — «имя=сек|имя=сек»: свой предел законно долгих гейтов;
  GATES_RUN_TIMEOUT_SEC     — предел всего прогона, с; взводит ведущий (gates_join_or_lead);
                              нет ключа — 5400; не число — строка «предел
                              GATES_RUN_TIMEOUT_SEC не число: X — взят предел по
                              умолчанию 5400 с» и сторож взведён с 5400 (сторож —
                              потомок ведущего `sleep N`, его видит гейт-наблюдатель);
  GATES_LEAD_PID, GATES_LEAD_ROOT — ставит ведущий; вложенный прогон того же корня
                              при живом ведущем не гонит ворота;
  строки: «превысил предел N с», «ворота превысили общий предел N с»,
          «уже внутри ворот»; по общему пределу — строка итога с "предел": true
          в ITOG_LOG (ворота-итог.jsonl);
  коды:   гейт по пределу — 124; прогон по общему пределу — 124 (и в файле .rc
          замка, его читают присоединившиеся); вложенный отказ — 75.

Дверь для мутаций: GATES_CHECK_LIB — путь обёртки (мутировать КОПИЮ); PUBLISH_SH —
путь publish.sh для случая самотеста публикации.
Код возврата: 0 — все случаи зелёные, 1 — есть красный.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gates_parallel_cache import LIB, Stand, stand_processes, tail  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_SH = Path(os.environ.get("PUBLISH_SH") or ROOT / "scripts/publish.sh")
NPROC = os.cpu_count() or 1
NESTED_CODE = 75
OVERRUN_CODE = 124
HUNG = "проба: стенд не уложился"

# Гейт, который не кончается сам и держит внука: убит должен быть и внук, иначе
# предел убил бы одну оболочку, а сон жил бы сиротой — ровно то, что висело 13.11.
FORKER_SH = r"""#!/usr/bin/env bash
name="$1"
echo run >> "$MARKS/$name"
( exec sleep 300 ) &
echo "$!" > "$MARKS/$name.grandchild"
echo "внук $!"
sleep 300
"""

# Подставной publish.sh --selftest в нынешнем виде: зовёт ворота ТОГО ЖЕ корня и
# судит их код. Предохранитель глубины — только чтобы красный прогон пробы не стал
# той же вилкой, которую ловит; при живом правиле он не срабатывает.
PUBLISH_FAKE_SH = r"""#!/usr/bin/env bash
depth=${STAND_DEPTH:-0}
echo run >> "$MARKS/publish"
if [ "$depth" -ge 3 ]; then echo "предохранитель пробы: глубина $depth"; exit 9; fi
STAND_DEPTH=$((depth + 1)) bash "$GATES_ROOT/stand.sh" "$STAND_LIB"
rc=$?
echo "вложенные ворота: код $rc"
exit "$rc"
"""

# Ворота внутри снимка: тот же порядок, что у scripts/gates.sh (конфиг, обёртка,
# корень от своего файла, замок прогона), одна медленная проверка с меткой.
SNAP_GATES_SH = r"""#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C.UTF-8
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
konf_zagruzit
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/gates_check.sh"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
gates_join_or_lead "$@"
проверка "снимок-медленная" bash -c 'echo run >> "$MARKS/снимок"; sleep 3; echo готово'
gates_wait_all
exit "$RED"
"""

failed: list[str] = []
passed: list[str] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  {'ок' if ok else 'ПЛОХО'} · {name}" + ("" if ok else f" — {detail}"))


def sweep(marks: Path) -> int:
    """Добивает всё, что стенд оставил; возвращает, сколько было живо."""
    left = stand_processes(marks)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(left)


def run_stand(st: Stand, limit: float, script: str = "stand.sh", **extra: str) -> tuple[str, int, float]:
    """Стенд своей сессией: по пределу пробы гаснет вся группа, а не одна оболочка."""
    started = time.monotonic()
    proc = subprocess.Popen(["bash", str(st.dir / script), LIB], cwd=st.dir,
                            env=st.env(**extra), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        out += f"\n[{HUNG} в {limit:.0f} с]"
    return out, proc.returncode, time.monotonic() - started


def alive(pid: int) -> bool:
    """Жив и не зомби: убитый внук, ещё не прибранный init, считается мёртвым."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def with_stand(checks: str, body, files: dict[str, str] | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="gates-bounds-") as tmp:
        st = Stand(Path(tmp), checks)
        for rel, text in (files or {}).items():
            (st.dir / rel).write_text(text, encoding="utf-8")
        try:
            body(st)
        finally:
            sweep(st.marks)


# ── (1) предел одного гейта ──────────────────────────────────────────────────

FOREVER_CHECKS = ('проверка "вечная" bash chk/forker.sh вечная\n'
                  'проверка "после" bash chk/run.sh после 0\n')


def judge_check_limit(st: Stand, mode: str, out: str, rc: int, took: float) -> None:
    grandchild = st.marks / "вечная.grandchild"
    gpid = int(grandchild.read_text()) if grandchild.is_file() else 0
    case(f"(1) {mode}: гейт дольше предела 2 с — прогон кончился сам меньше чем за 20 с",
         took < 20, f"{took:.1f} с; {tail(out)}")
    case(f"(1) {mode}: гейт по пределу красный со строкой «превысил предел 2 с»",
         "ПЛОХО · вечная" in out and "превысил предел 2 с" in out, tail(out))
    # Внук судится, только если стенд кончился САМ: по пределу пробы его гасит проба.
    case(f"(1) {mode}: внук гейта убит вместе с ним",
         HUNG not in out and gpid > 0 and not alive(gpid),
         f"стенд висел до предела пробы; {tail(out)}" if HUNG in out
         else f"внук {gpid or 'не записан'} жив={gpid and alive(gpid)}")
    case(f"(1) {mode}: следующий гейт после убитого прошёл, счёт 1 зелёный 1 красный",
         st.runs("после") == 1 and "СЧЁТ 1 1 0" in out, tail(out))


def case_check_limit_inline(st: Stand) -> None:
    out, rc, took = run_stand(st, 25, GATES_CHECK_TIMEOUT_SEC="2")
    judge_check_limit(st, "синхронно", out, rc, took)


def case_check_limit_pool(st: Stand) -> None:
    out, rc, took = run_stand(st, 25, GATES_CHECK_TIMEOUT_SEC="2", GATES_JOBS="3")
    judge_check_limit(st, "пулом", out, rc, took)


LONG_CHECKS = ('проверка "законная" bash chk/run.sh законная 2\n'
               'проверка "незаконная" bash chk/run.sh незаконная 2\n')


def case_check_limit_long(st: Stand) -> None:
    out, _, _ = run_stand(st, 25, GATES_CHECK_TIMEOUT_SEC="1",
                          GATES_CHECK_TIMEOUT_LONG="законная=10|иная=5")
    case("(1) свой предел из GATES_CHECK_TIMEOUT_LONG: законно долгий гейт зелёный",
         "ок · законная" in out and "превысил предел" not in out.split("незаконная")[0], tail(out))
    case("(1) соседний гейт без своего предела — красный по общему пределу 1 с",
         "ПЛОХО · незаконная" in out and "превысил предел 1 с" in out and "СЧЁТ 1 1 0" in out,
         tail(out))


# Гейт, глухой к TERM (ловушка «""» наследуется сном): его добивает только KILL
# через 5 с после предела (`timeout -k 5`). Без -k timeout ждал бы его вечно.
DEAF_CHECKS = ('проверка "глухая" bash -c \'trap "" TERM; echo run >> "$MARKS/глухая"; sleep 300\'\n'
               'проверка "после" bash chk/run.sh после 0\n')


def case_check_limit_deaf(st: Stand) -> None:
    out, _, took = run_stand(st, 25, GATES_CHECK_TIMEOUT_SEC="2")
    case("(1) гейт, глухой к TERM, добит KILL через 5 с после предела: прогон кончился меньше чем за 20 с",
         took < 20 and HUNG not in out, f"{took:.1f} с; {tail(out)}")
    case("(1) глухой гейт, добитый KILL (код 137), — со строкой «превысил предел 2 с»",
         "превысил предел 2 с" in out, tail(out))
    case("(1) глухой гейт красный, следующий прошёл: счёт 1 зелёный 1 красный",
         "ПЛОХО · глухая" in out and st.runs("после") == 1 and "СЧЁТ 1 1 0" in out, tail(out))


# ── (1) предел всего прогона ─────────────────────────────────────────────────

def judge_run_limit(st: Stand, mode: str, out: str, rc: int, took: float) -> None:
    time.sleep(0.5)
    left = sweep(st.marks)
    rc_files = [p.read_text().strip() for p in st.log.glob("gates-run-*.rc")]
    itog = st.log / "ворота-итог.jsonl"
    rows = [json.loads(x) for x in itog.read_text(encoding="utf-8").splitlines()] if itog.is_file() else []
    case(f"(1) {mode}: прогон дольше общего предела 3 с вышел меньше чем за 15 с",
         took < 15, f"{took:.1f} с; {tail(out)}")
    case(f"(1) {mode}: по общему пределу — строка «ворота превысили общий предел 3 с» и код {OVERRUN_CODE}",
         "ворота превысили общий предел 3 с" in out and rc == OVERRUN_CODE, f"код {rc}; {tail(out)}")
    case(f"(1) {mode}: пул погашен — процессов стенда не осталось",
         HUNG not in out and left == 0,
         "стенд висел до предела пробы" if HUNG in out else f"живых процессов стенда {left}")
    case(f"(1) {mode}: присоединившимся оставлен красный итог (файл .rc замка = {OVERRUN_CODE})",
         rc_files == [str(OVERRUN_CODE)], f"файлы .rc: {rc_files}")
    # Без строки итога панель показала бы владельцу прежний прогон как последний.
    case(f"(1) {mode}: по общему пределу в ворота-итог.jsonl строка «предел» с красными ≥ 1",
         len(rows) == 1 and rows[0].get("предел") is True and rows[0].get("красных", 0) >= 1
         and rows[0].get("режим") == "проба", f"строки итога: {rows}")


RUN_LIMIT_POOL = "".join(f'проверка "долгая-{i}" bash chk/forker.sh долгая-{i}\n' for i in range(1, 4))
RUN_LIMIT_INLINE = 'проверка "долгая-1" bash chk/forker.sh долгая-1\n'


# ITOG_LOG и REZHIM — переменные gates.sh; стенд получает их окружением.
def run_limit_env(st: Stand) -> dict[str, str]:
    return {"ITOG_LOG": str(st.log / "ворота-итог.jsonl"), "REZHIM": "проба"}


def case_run_limit_pool(st: Stand) -> None:
    out, rc, took = run_stand(st, 30, STAND_JOIN="1", GATES_JOBS="3",
                              GATES_RUN_TIMEOUT_SEC="3", GATES_CHECK_TIMEOUT_SEC="600", **run_limit_env(st))
    judge_run_limit(st, "пулом", out, rc, took)


def case_run_limit_inline(st: Stand) -> None:
    out, rc, took = run_stand(st, 30, STAND_JOIN="1", GATES_JOBS="1",
                              GATES_RUN_TIMEOUT_SEC="3", GATES_CHECK_TIMEOUT_SEC="600", **run_limit_env(st))
    judge_run_limit(st, "синхронно", out, rc, took)


# Гейт-наблюдатель: сторож общего предела — потомок ведущего, который спит N с.
# Взведённый сторож виден как `sleep N` под GATES_LEAD_PID; выключенного нет вовсе.
# Ждать 5400 с пробе нельзя, а процесс сторожа — дешёвый и прямой след взвода.
WATCHDOG_PY = r"""import os
lead = os.environ.get("GATES_LEAD_PID", "")

def parent(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[1]
    except (OSError, IndexError):
        return "0"

def under_lead(pid):
    for _ in range(64):
        pid = parent(pid)
        if pid == lead:
            return True
        if pid in ("0", "1"):
            return False
    return False

found = []
for p in os.listdir("/proc"):
    if not p.isdigit():
        continue
    try:
        with open(f"/proc/{p}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        continue
    if len(argv) >= 2 and os.path.basename(argv[0]) == b"sleep" and under_lead(p):
        found.append(argv[1].decode())
print("СТОРОЖ " + (",".join(sorted(found)) or "нет"))
"""

WATCHDOG_CHECKS = ('проверка "сторож" python3 chk/watchdog.py\n'
                   'проверка "после" bash chk/run.sh после 0\n')
BAD_LIMIT_LINE = "предел GATES_RUN_TIMEOUT_SEC не число: три — взят предел по умолчанию 5400 с"


def case_run_limit_default_armed(st: Stand) -> None:
    out, rc, _ = run_stand(st, 25, STAND_JOIN="1", GATES_JOBS="1")
    case("(1) контроль пробы: без GATES_RUN_TIMEOUT_SEC сторож взведён с 5400 с (СТОРОЖ 5400)",
         "СТОРОЖ 5400" in out and rc == 0, f"код {rc}; {tail(out)}")


def case_run_limit_not_number(st: Stand) -> None:
    out, rc, _ = run_stand(st, 25, STAND_JOIN="1", GATES_JOBS="1", GATES_RUN_TIMEOUT_SEC="три")
    case(f"(1) GATES_RUN_TIMEOUT_SEC=«три» — строка «{BAD_LIMIT_LINE}»",
         BAD_LIMIT_LINE in out, tail(out))
    case("(1) GATES_RUN_TIMEOUT_SEC=«три» — ворота проходят: код 0, счёт 2 зелёных",
         rc == 0 and "СЧЁТ 2 0 0" in out, f"код {rc}; {tail(out)}")
    # Молчаливое выключение — ровно то, что было: «-gt» на не числе падал, сторож не взводился.
    case("(1) GATES_RUN_TIMEOUT_SEC=«три» — сторож взведён с пределом по умолчанию (СТОРОЖ 5400), не выключен",
         "СТОРОЖ 5400" in out, tail(out))


# ── (3) вложенный прогон ─────────────────────────────────────────────────────

SLOW_CHECK = 'проверка "медленная" bash chk/run.sh медленная 1\n'


def case_nested_live_same_root(st: Stand) -> None:
    lead = subprocess.Popen(["sleep", "60"])
    try:
        out, rc, took = run_stand(st, 25, STAND_JOIN="1", GATES_LEAD_PID=str(lead.pid),
                                  GATES_LEAD_ROOT=str(st.dir))
    finally:
        lead.kill()
        lead.wait()
    case("(3) вложенный при живом ведущем того же корня: команда гейта не шла",
         st.runs("медленная") == 0, f"запусков {st.runs('медленная')}; {tail(out)}")
    case(f"(3) вложенный отказ — строка «уже внутри ворот» и код {NESTED_CODE}, сразу",
         "уже внутри ворот" in out and rc == NESTED_CODE and took < 10,
         f"код {rc}, {took:.1f} с; {tail(out)}")


def case_nested_live_other_root(st: Stand) -> None:
    lead = subprocess.Popen(["sleep", "60"])
    try:
        out, rc, _ = run_stand(st, 25, STAND_JOIN="1", GATES_LEAD_PID=str(lead.pid),
                               GATES_LEAD_ROOT=str(st.dir.parent / "другой-корень"))
    finally:
        lead.kill()
        lead.wait()
    case("(3) прогон копии в ДРУГОМ корне внутри ворот гонит сам, пул 1 (стенды проб)",
         st.runs("медленная") == 1 and "уже внутри ворот" not in out and "ПУЛ 1" in out
         and rc == 0, f"код {rc}; {tail(out)}")


def case_nested_dead_lead(st: Stand) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    out, rc, _ = run_stand(st, 25, STAND_JOIN="1", GATES_LEAD_PID=str(gone.pid),
                           GATES_LEAD_ROOT=str(st.dir))
    case("(3) устаревший GATES_LEAD_PID (ведущий мёртв): ворота идут, отказа нет",
         st.runs("медленная") == 1 and "уже внутри ворот" not in out and rc == 0,
         f"код {rc}; {tail(out)}")
    case(f"(3) при мёртвом ведущем прогон сам становится ведущим: пул ядра−2 (ПУЛ {max(1, NPROC - 2)})",
         f"ПУЛ {max(1, NPROC - 2)}" in out, tail(out))


ROOT_CHECK = 'проверка "корень ведущего" bash -c \'echo "КОРЕНЬ=${GATES_LEAD_ROOT:-нет}"\'\n'


def case_lead_exports_root(st: Stand) -> None:
    out, _, _ = run_stand(st, 25, STAND_JOIN="1")
    case("(3) ведущий отдаёт гейтам GATES_LEAD_ROOT = свой корень",
         f"КОРЕНЬ={st.dir}" in out, tail(out))


PUBLISH_CHECKS = 'проверка "публикация" bash chk/publish.sh\n'


def case_recursion(st: Stand) -> None:
    out, rc, took = run_stand(st, 40, STAND_JOIN="1", GATES_JOBS="2", STAND_LIB=LIB)
    case("(3) рекурсия ворота→публикация→ворота обрывается за один уровень",
         st.runs("publish") == 1, f"самотест публикации шёл {st.runs('publish')} раз; {tail(out)}")
    case(f"(3) оборванная рекурсия не зелёная: вложенные ворота дали {NESTED_CODE}, гейт красный",
         f"вложенные ворота: код {NESTED_CODE}" in out and "ПЛОХО · публикация" in out and rc == 1,
         f"код {rc}, {took:.1f} с; {tail(out)}")


# ── (2) снимок под тем же замком ─────────────────────────────────────────────

def build_snapshot_repo(base: Path) -> Path:
    repo = base / "снимок-стенд"
    lib = repo / "scripts/lib"
    lib.mkdir(parents=True)
    (repo / "harness/config").mkdir(parents=True)
    for f in (ROOT / "scripts/lib").iterdir():
        if f.is_file():
            (lib / f.name).symlink_to(f)
    (lib / "gates_check.sh").unlink()
    shutil.copy(LIB, lib / "gates_check.sh")
    helper = Path(LIB).resolve().parent.parent / "gates_cache.py"
    (repo / "scripts/gates_cache.py").symlink_to(helper if helper.is_file() else ROOT / "scripts/gates_cache.py")
    shutil.copy(ROOT / "scripts/gates-snapshot.sh", repo / "scripts/gates-snapshot.sh")
    (repo / "scripts/gates.sh").write_text(SNAP_GATES_SH, encoding="utf-8")
    (repo / "harness/config/gates-live.yaml").write_text("живое: []\n", encoding="utf-8")
    git = ["git", "-c", "user.name=проба", "-c", "user.email=probe@localhost", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "стенд снимка"], check=True)
    return repo


def wait_end(log: Path, limit: float) -> str:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.is_file() else ""
        if "ВОРОТА-КОНЕЦ" in text:
            return text
        time.sleep(0.3)
    return (log.read_text(encoding="utf-8") if log.is_file() else "") + "\n[проба: нет ВОРОТА-КОНЕЦ]"


def case_snapshot_joins() -> None:
    with tempfile.TemporaryDirectory(prefix="gates-bounds-snap-") as tmp:
        base = Path(tmp)
        marks, log, home = base / "marks", base / "log", base / "дом"
        for d in (marks, log, home):
            d.mkdir()
        # Место снимков — не под /tmp и не под LOG_DIR: gates-snapshot.sh такое отвергает.
        snaps = Path(tempfile.mkdtemp(prefix="gates-bounds-snap-", dir="/var/tmp"))
        repo = build_snapshot_repo(base)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GATES_", "HARNESS_", "GIT_")) and k not in ("LOG_DIR", "ROOT")}
        env.update(HARNESS_HOME=str(home), LOG_DIR=str(log), GATES_SNAPSHOT_DIR=str(snaps),
                   MARKS=str(marks), LC_ALL="C.UTF-8")
        try:
            logs = []
            for _ in range(2):
                res = subprocess.run(["bash", str(repo / "scripts/gates-snapshot.sh")], cwd=repo,
                                     env=env, capture_output=True, text=True, timeout=30)
                m = re.search(r"^журнал: (.+)$", res.stdout, flags=re.M)
                logs.append(Path(m.group(1)) if m else log / "нет-журнала")
                deadline = time.monotonic() + 10
                while not (marks / "снимок").is_file() and time.monotonic() < deadline:
                    time.sleep(0.1)
                # Имя снимка — метка до секунды: второй запуск в ту же секунду упал бы на
                # занятом каталоге, и случай судил бы это, а не замок.
                time.sleep(1.1)
            texts = [wait_end(p, 60) for p in logs]
            runs = len((marks / "снимок").read_text().splitlines()) if (marks / "снимок").is_file() else 0
            case("(2) два gates-snapshot.sh одного HEAD: ворота шли один раз, второй присоединился",
                 runs == 1 and "присоединяюсь к прогону" in texts[1],
                 f"запусков {runs}; 2: {tail(texts[1])}")
            case("(2) оба снимка кончились «ВОРОТА-КОНЕЦ rc=0»",
                 all("ВОРОТА-КОНЕЦ rc=0" in t for t in texts), " | ".join(tail(t) for t in texts))
        finally:
            sweep(marks)
            subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
            shutil.rmtree(snaps, ignore_errors=True)


# ── (3) самотест publish.sh не зовёт живые ворота ────────────────────────────

def case_publish_selftest_uses_stand() -> None:
    text = PUBLISH_SH.read_text(encoding="utf-8")
    m = re.search(r"^zamok_vorot\(\) \{.*?^\}", text, flags=re.M | re.S)
    body = m.group(0) if m else ""
    case("(3) publish.sh: замок ворот зовёт gates.sh не только из $SCRIPTS_REAL — каталог скриптов доводом",
         bool(body) and "$SCRIPTS_REAL/gates.sh" not in body,
         "функция zamok_vorot не найдена" if not body else "в теле zamok_vorot стоит \"$SCRIPTS_REAL/gates.sh\"")
    case("(3) publish.sh --selftest: двор замка ворот передаёт свои подставные скрипты",
         re.search(r'zamok_vorot "\$d/лог" "\$d/проект/scripts"', text) is not None,
         "в proba_vorota нет вызова zamok_vorot \"$d/лог\" \"$d/проект/scripts\"")


def main() -> int:
    print(f"обёртка: {LIB}")
    print(f"публикация: {PUBLISH_SH}")
    forker = {"chk/forker.sh": FORKER_SH}
    scenarios = [
        (FOREVER_CHECKS, case_check_limit_inline, forker),
        (FOREVER_CHECKS, case_check_limit_pool, forker),
        (LONG_CHECKS, case_check_limit_long, None),
        (DEAF_CHECKS, case_check_limit_deaf, None),
        (RUN_LIMIT_POOL, case_run_limit_pool, forker),
        (RUN_LIMIT_INLINE, case_run_limit_inline, forker),
        (WATCHDOG_CHECKS, case_run_limit_default_armed, {"chk/watchdog.py": WATCHDOG_PY}),
        (WATCHDOG_CHECKS, case_run_limit_not_number, {"chk/watchdog.py": WATCHDOG_PY}),
        (SLOW_CHECK, case_nested_live_same_root, None),
        (SLOW_CHECK, case_nested_live_other_root, None),
        (SLOW_CHECK, case_nested_dead_lead, None),
        (ROOT_CHECK, case_lead_exports_root, None),
        (PUBLISH_CHECKS, case_recursion, {"chk/publish.sh": PUBLISH_FAKE_SH}),
    ]
    for checks, body, files in scenarios:
        try:
            with_stand(checks, body, files)
        except Exception as exc:  # стенд упал — красный случай, а не тихий пропуск
            case(f"стенд {body.__name__}", False, f"{type(exc).__name__}: {exc}")
    for body in (case_snapshot_joins, case_publish_selftest_uses_stand):
        try:
            body()
        except Exception as exc:
            case(f"стенд {body.__name__}", False, f"{type(exc).__name__}: {exc}")
    total = len(passed) + len(failed)
    if total == 0:
        print("ПЛОХО: ни одного случая не прогнано")
        return 1
    print(f"итог: зелёных {len(passed)}, красных {len(failed)} из {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
