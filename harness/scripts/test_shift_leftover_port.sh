#!/usr/bin/env bash
# test_shift_leftover_port.sh — проба «смена оставила за собой открытый порт».
#
# Под uid агента наружу слушают не только забытые сменой http.server, но и
# идущие пробы исполнителей и многочасовые прогоны мутаций. Поэтому беда —
# только старый слушатель (порог SHIFT_LEFTOVER_MIN_AGE_MIN), а цель уборки —
# старый И осиротевший И без метки SHIFT_LEFTOVER_KEEP; снятым считается лишь
# ушедший процесс. Каждый случай судит СВОЙ pid в своём поддельном /proc
# ([[a-second-harness-on-the-same-machine]]): общий стенд прятал бы, кто кого
# заслонил.
#
# Части: стенд (поддельный /proc; pid выше pid_max, чтобы даже мутант не мог
# попасть сигналом в живой процесс), CLI на стенде, проводка в session-warden,
# живой случай (свой http.server на своём порту, осиротевший через setsid и
# двойной fork; убийца ограничен своим pid, чужие кандидаты только называются).
#
# Двери мутаций: SHIFT_PORT_SUBJECT=<копия прибора>,
#                SHIFT_PORT_WARDEN=<копия session-warden.sh>.
# Запуск только фоном: setsid nohup bash scripts/test_shift_leftover_port.sh \
#   > /var/log/harness/test_shift_leftover_port.log 2>&1 < /dev/null &
set -u

DIR=$(cd "$(dirname "$0")" && pwd)
SUBJECT=${SHIFT_PORT_SUBJECT:-$DIR/check-external-surface.py}
# Раскладка проекта (scripts/ рядом с harness/) и пакета (harness/scripts/).
WARDEN=${SHIFT_PORT_WARDEN:-$DIR/../harness/demons/session-warden.sh}
[ -f "$WARDEN" ] || WARDEN=${SHIFT_PORT_WARDEN:-$DIR/../demons/session-warden.sh}
WORK=$(mktemp -d /tmp/shift-port-probe.XXXXXX)
mkdir -p "$WORK/www"
SRV_PID=""

cleanup() {
    # Снимаем только свой сервер: pid сверяется с каталогом пробы в cmdline,
    # иначе переиспользованный pid получил бы чужой сигнал.
    if [ -n "$SRV_PID" ] && tr '\0' ' ' 2>/dev/null < "/proc/$SRV_PID/cmdline" | grep -qF "$WORK/www"; then
        kill "$SRV_PID" 2>/dev/null
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# Свой порт: свободный, выданный ядром ([[a-probe-takes-its-own-port]]).
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("0.0.0.0", 0)); print(s.getsockname()[1])')
# setsid + фон внутри короткоживущего bash: сервер остаётся без родителя —
# ровно то, что оставляет ушедшая смена.
setsid bash -c 'python3 -m http.server "$1" --bind 0.0.0.0 --directory "$2" >/dev/null 2>&1 < /dev/null & echo $! > "$3"' \
    _ "$PORT" "$WORK/www" "$WORK/srv.pid" < /dev/null
SRV_PID=$(cat "$WORK/srv.pid" 2>/dev/null)
up=0
for _ in $(seq 1 50); do
    [ -n "$SRV_PID" ] && [ -d "/proc/$SRV_PID" ] || break
    if python3 -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 0.2)" 2>/dev/null; then
        up=1; break
    fi
    python3 -c 'import time; time.sleep(0.1)'
done
LIVE_PID=""
[ "$up" = 1 ] && LIVE_PID=$SRV_PID

python3 - "$SUBJECT" "$DIR" "$WARDEN" "$PORT" "$LIVE_PID" "$WORK" <<'PY'
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

subject, script_dir, warden, live_port, live_pid, work = sys.argv[1:7]
live_port = int(live_port)
live_pid = int(live_pid) if live_pid else None
for lib in (Path(subject).resolve().parent / "lib", Path(script_dir) / "lib"):
    sys.path.insert(0, str(lib))
spec = importlib.util.spec_from_file_location("check_external_surface", subject)
surface = importlib.util.module_from_spec(spec)
spec.loader.exec_module(surface)

passed = total = 0


def check(condition, name):
    global passed, total
    total += 1
    passed += bool(condition)
    print(f"  {'ок   ' if condition else 'ПЛОХО'} {name}")


MY_UID = os.getuid()
OTHER_UID = MY_UID + 1
ROOT = "/корень-стенда"
DAY = 24 * 3600
UPTIME = 100000.0
ANY4, LOOP4, ANY6 = "00000000", "0100007F", "0" * 32
# pid выше pid_max не бывает: сигнал мутанта уйдёт в пустоту, а не в процесс.
PID_BASE = int(Path("/proc/sys/kernel/pid_max").read_text()) + 100
TMP = Path(tempfile.mkdtemp(prefix="shift-port-stand-"))


def stand(name, procs, sockets, parents=None):
    """procs: (pid, ppid, age_s|None, command, [inode…]); sockets: (table,
    address, port, inode, uid); parents: pid → comm живого родителя."""
    root = TMP / name
    (root / "net").mkdir(parents=True)
    (root / "uptime").write_text(f"{UPTIME:.2f} 0.00\n")
    tick = os.sysconf("SC_CLK_TCK")
    for pid, ppid, age, command, inodes in procs:
        p = root / str(pid)
        (p / "fd").mkdir(parents=True)
        for n, inode in enumerate(inodes):
            os.symlink(f"socket:[{inode}]", p / "fd" / str(3 + n))
        (p / "cmdline").write_bytes(command.replace(" ", "\0").encode() + b"\0")
        uid = next((s[4] for s in sockets if s[3] in inodes), MY_UID)
        (p / "status").write_text(f"Name:\tpython3\nPid:\t{pid}\nPPid:\t{ppid}\n"
                                  f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        if age is not None:
            start = int((UPTIME - age) * tick)
            (p / "stat").write_text(f"{pid} (python3 (x)) S {ppid} "
                                    + " ".join(["0"] * 17) + f" {start} 0\n")
    for ppid, comm in (parents or {}).items():
        (root / str(ppid)).mkdir()
        (root / str(ppid) / "comm").write_text(comm + "\n")
    head = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
            "retrnsmt   uid  timeout inode")
    for table in ("tcp", "tcp6"):
        lines = [head] + [
            f"   {n}: {addr}:{port:04X} {'0' * len(addr)}:0000 0A 00000000:00000000 "
            f"00:00000000 00000000 {uid:5d}        0 {inode} 1 0000 100 0 0 10 0"
            for n, (t, addr, port, inode, uid) in enumerate(sockets) if t == table]
        (root / "net" / table).write_text("\n".join(lines) + "\n")
    return root


def single(name, pid, port, age=DAY, ppid=1, command=None, uid=MY_UID,
           address=ANY4, parents=None):
    """Один процесс на одном сокете — самый частый стенд."""
    inode = str(70000 + pid - PID_BASE)
    return stand(name, [(pid, ppid, age, command or f"python3 -m http.server {port}", [inode])],
                 [("tcp", address, port, inode, uid)], parents)


def report(proc, declared=None):
    return surface.находки(proc, None, ROOT, declared or {}, 120)


def report_rc(proc):
    with contextlib.redirect_stdout(io.StringIO()):
        return surface.отчёт(proc, None, ROOT, {}, 120)


def sweep(proc, killer="removes", keep=None, declared=None):
    """(снято, вывод, кого звали убийцей). removes — процесс «уходит»."""
    called = []

    def removes(pid):
        called.append(pid)
        shutil.rmtree(proc / str(pid))
        return True

    def stays(pid):
        called.append(pid)
        return True

    def refuses(pid):
        called.append(pid)
        return False

    chosen = {"removes": removes, "stays": stays, "refuses": refuses}[killer]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        count = surface.убрать_мусор_смены(proc, ROOT, declared or {}, убийца=chosen,
                                          мин_возраст_мин=120, не_трогать=keep or [],
                                          ждать_сек=0.3)
    return count, out.getvalue(), called


def has(lines, *parts):
    return [s for s in lines if all(p in s for p in parts)]


P = {name: PID_BASE + n for n, name in enumerate(
    ["old", "fresh", "declared", "loop", "foreign", "rootsvc", "parent", "keep",
     "stays", "refused", "removes", "dual", "h7", "h7other", "noage", "deadpar",
     "sysdpar", "cli"])}

def case(fn):
    """Случай идёт сразу. Прибор без нужной функции или с другой сигнатурой
    (откат) краснит свой случай, а не обрывает пробу без итога."""
    try:
        fn()
    except Exception as e:                  # noqa: BLE001
        check(False, f"{fn.__name__}: прибор упал — {type(e).__name__}: {e}")
    return fn


@case
def _case_00():
    print("── стенд: больные случаи")
    pid = P["old"]
    proc = single("old", pid, 47001)
    bad, foreign, _ = report(proc)
    check(has(bad, "мусор смены", f"pid {pid}", "возраст 1440 мин"),
          f"БОЛЬНОЙ Н1: старый сирота нашего uid вне проекта — «мусор смены» с pid {pid} и возрастом")
    check(not has(foreign, f"pid {pid}") and not has(foreign, "47001", "чужой процесс"),
          "БОЛЬНОЙ Н1: мусор смены не назван «чужим»")
    check(report_rc(proc) == 1, "БОЛЬНОЙ Н1: отчёт с мусором смены — код 1")


@case
def _case_01():
    pid = P["fresh"]
    proc = single("fresh", pid, 47002, age=300)
    bad, foreign, _ = report(proc)
    check(not bad and has(foreign, "свежий слушатель смены", f"pid {pid}", "возраст 5 мин"),
          f"БОЛЬНОЙ Н2: свежий (5 мин) — «свежий слушатель смены» pid {pid}, бед нет")
    check(report_rc(proc) == 0, "БОЛЬНОЙ Н2: свежий слушатель ворота не красит — код 0")
    check(sweep(proc)[2] == [], "БОЛЬНОЙ Н2: свежий не цель уборки")


@case
def _case_02():
    pid = P["parent"]
    proc = single("parent", pid, 47007, ppid=P["parent"] + 1000, parents={P["parent"] + 1000: "bash"})
    count, out, called = sweep(proc)
    check(count == 0 and called == [] and f"родитель жив (pid {pid + 1000})" in out and f"pid {pid}" in out,
          f"БОЛЬНОЙ Н1: старый с живым родителем — не цель, «родитель жив» (pid {pid})")
    check(has(report(proc)[0], "мусор смены", f"pid {pid}"),
          "старый с живым родителем в отчёте назван мусором (отчёт называет всё)")


@case
def _case_03():
    pid = P["keep"]
    proc = single("keep", pid, 47008, command="python3 /tmp/proba-x/api.py")
    count, out, called = sweep(proc, keep=["/tmp/proba-"])
    check(count == 0 and called == [] and "SHIFT_LEFTOVER_KEEP" in out and f"pid {pid}" in out,
          f"БОЛЬНОЙ Н1в: метка из SHIFT_LEFTOVER_KEEP защищает pid {pid}")


@case
def _case_04():
    pid = P["stays"]
    proc = single("stays", pid, 47009)
    count, out, called = sweep(proc, killer="stays")
    check(count == 0 and called == [pid] and has(out.splitlines(), "не ушёл", f"pid {pid}")
          and "снято мусора смены: 0" in out,
          f"БОЛЬНОЙ Н3/Н4: убийца сказал True, каталог pid {pid} остался — счёт 0, «не ушёл»")


@case
def _case_05():
    pid = P["refused"]
    proc = single("refused", pid, 47010)
    count, out, called = sweep(proc, killer="refuses")
    check(count == 0 and called == [pid] and has(out.splitlines(), "не послан", f"pid {pid}"),
          f"БОЛЬНОЙ Н3: убийца отказал pid {pid} — не посчитан, «сигнал не послан»")


@case
def _case_06():
    pid = P["removes"]
    proc = single("removes", pid, 47011)
    count, out, called = sweep(proc)
    check(count == 1 and called == [pid] and "снято мусора смены: 1" in out,
          f"старый сирота ушёл — счёт ровно 1 (pid {pid})")


@case
def _case_07():
    pid = P["dual"]
    proc = stand("dual", [(pid, 1, DAY, "python3 -m http.server 47012", ["81201", "81202"])],
                 [("tcp", ANY4, 47012, "81201", MY_UID), ("tcp6", ANY6, 47012, "81202", MY_UID)])
    bad, _, _ = report(proc)
    count, out, called = sweep(proc)
    check(len(has(bad, "мусор смены", f"pid {pid}")) == 1 and count == 1 and called == [pid],
          f"один процесс на tcp и tcp6 — одна строка, один сигнал (pid {pid})")


@case
def _case_08():
    pid, other = P["h7"], P["h7other"]
    proc = stand("h7", [(other, 1, DAY, "/usr/sbin/pure-ftpd", ["81301"]),
                        (pid, 1, DAY, "python3 -m http.server 47013 --bind ::", ["81302"])],
                 [("tcp", ANY4, 47013, "81301", OTHER_UID), ("tcp6", ANY6, 47013, "81302", MY_UID)])
    bad, _, _ = report(proc)
    count, out, called = sweep(proc)
    check(has(bad, "мусор смены", f"pid {pid}") and called == [pid] and count == 1,
          f"БОЛЬНОЙ Н7: мусор на tcp6 порта, где tcp держит чужой, не теряется (pid {pid})")


@case
def _case_09():
    pid = P["deadpar"]
    proc = single("deadpar", pid, 47016, ppid=pid + 5000)
    check(sweep(proc)[2] == [pid], f"родитель умер (каталога нет) — сирота, цель (pid {pid})")
    pid = P["sysdpar"]
    proc = single("sysdpar", pid, 47017, ppid=pid + 6000, parents={pid + 6000: "systemd"})
    check(sweep(proc)[2] == [pid], f"родитель — systemd пользователя: сирота, цель (pid {pid})")


@case
def _case_10():
    print("── стенд: соседи — не мусор и не цель")
    pid = P["declared"]
    proc = single("declared", pid, 47003)
    bad, foreign, _ = report(proc, {47003: "проба: объявлено"})
    check(not has(bad, f"pid {pid}") and sweep(proc, declared={47003: "x"})[2] == [],
          f"объявленный порт нашего uid — не мусор, не цель (pid {pid})")
    pid = P["loop"]
    proc = single("loop", pid, 47004, address=LOOP4)
    check(not has(report(proc)[0], f"pid {pid}") and sweep(proc)[2] == [],
          f"петля 127.0.0.1 — не мусор, не цель (pid {pid})")
    pid = P["foreign"]
    proc = single("foreign", pid, 47005, uid=OTHER_UID, command="python3 sample-21/bot.py")
    bad, foreign, _ = report(proc)
    check(not has(bad, "47005") and has(foreign, "47005", "чужой процесс") and sweep(proc)[2] == [],
          f"чужой uid (старый сирота) — «чужой процесс», не цель (pid {pid})")
    pid = P["rootsvc"]
    proc = single("rootsvc", pid, 47006, command=f"python3 {ROOT}/scripts/panel.py")
    bad, _, _ = report(proc)
    check(has(bad, "наш процесс", f"pid {pid}") and not has(bad, "мусор смены")
          and sweep(proc)[2] == [],
          f"служба с корнем проекта — прежняя находка «наш процесс», не мусор (pid {pid})")
    pid = P["noage"]
    proc = single("noage", pid, 47015, age=None)
    bad, foreign, _ = report(proc)
    check(not bad and has(foreign, "возраст не виден", f"pid {pid}") and sweep(proc)[2] == [],
          f"возраст не прочитан — «не виден», не беда и не цель (pid {pid})")


@case
def _case_11():
    print("── CLI на стенде")
    pid = P["cli"]
    proc = single("cli", pid, 47101, command="python3 -m http.server 47101 --directory /tmp/черновики-стенда")
    answer = subprocess.run([sys.executable, subject, "--убрать-мусор-смены", "--корень-proc",
                             str(proc), "--только-назвать"], capture_output=True, text=True,
                            timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    check(answer.returncode == 0 and f"снял бы: pid {pid} порт 47101" in answer.stdout
          and "только назвать, целей 1" in answer.stdout and (proc / str(pid)).is_dir(),
          f"БОЛЬНОЙ Н5: --убрать-мусор-смены --только-назвать называет pid {pid} и не убивает")
    if answer.returncode or "снял бы" not in answer.stdout:
        print(f"        вывод CLI: {(answer.stdout + answer.stderr)[-300:]!r}")


@case
def _case_12():
    print("── проводка в session-warden")
    text = Path(warden).read_text(encoding="utf-8") if Path(warden).is_file() else ""
    lines = text.splitlines()
    body = re.search(r"^shift_leftover_sweep\(\) \{\n.*?^\}$", text, re.S | re.M)
    stub = Path(work) / "scripts"
    stub.mkdir(exist_ok=True)
    (stub / "check-external-surface.py").write_text("")
    fn_out = ""
    if body:
        script = ("set -euo pipefail\n"
                  # Вывод прибора уходит в $(…), а наружу — через jlog: печатаем и текст.
                  'jlog() { printf "JLOG %s %s\\n" "$1" "${3:-}"; }\n'
                  'timeout() { printf "TIMEOUT %s\\n" "$1"; shift; "$@"; }\n'
                  'python3() { printf "PY %s\\n" "$*"; echo "снято мусора смены: 0"; }\n'
                  f"{body.group(0)}\nshift_leftover_sweep\n")
        fn = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                            env={**os.environ, "HARNESS_SCRIPTS": str(stub)})
        fn_out = fn.stdout
    check(bool(body) and "TIMEOUT" in fn_out
          and f"PY {stub}/check-external-surface.py --убрать-мусор-смены" in fn_out
          and "JLOG shift_leftover_sweep " in fn_out,
          "БОЛЬНОЙ Н5/Н8: shift_leftover_sweep зовёт прибор с --убрать-мусор-смены через timeout")
    calls = [n for n, s in enumerate(lines)
             if re.search(r"\bshift_leftover_sweep\b", s) and not s.startswith((" ", "\t", "#"))
             and not s.startswith("shift_leftover_sweep()")]
    rescue = [n for n, s in enumerate(lines) if s.strip() == "rotation_queue_rescue"
              and not s.startswith((" ", "\t"))]
    truth = {}
    if len(calls) == 1:
        for need, rc in (("1", "0"), ("0", "0"), ("1", "2"), ("1", None)):
            setup = f"NEED_ROTATE={need}\n" + (f"ROTATE_RC={rc}\n" if rc is not None else "unset ROTATE_RC\n")
            run = subprocess.run(["bash", "-c", "set -euo pipefail\n"
                                  "shift_leftover_sweep() { echo CALLED; }\n"
                                  + setup + lines[calls[0]] + "\necho DONE\n"],
                                 capture_output=True, text=True, timeout=30)
            truth[(need, rc)] = ("CALLED" in run.stdout, "DONE" in run.stdout)
    check(len(calls) == 1 and rescue and calls[0] > rescue[0]
          and truth == {("1", "0"): (True, True), ("0", "0"): (False, True),
                        ("1", "2"): (False, True), ("1", None): (False, True)},
          "БОЛЬНОЙ Н5: вызов один, после rotation_queue_rescue, только при NEED_ROTATE=1 и ROTATE_RC=0")
    if len(calls) != 1 or not rescue:
        print(f"        вызовов {len(calls)}, rotation_queue_rescue на верхнем уровне: {len(rescue)}")


@case
def _case_13():
    print(f"── живой случай: свой http.server pid {live_pid} на 0.0.0.0:{live_port}")
    real_root = str(Path(subject).resolve().parent.parent)
    if live_pid is None:
        check(False, "живой случай: свой http.server не поднялся — проба не судит")
    else:
        ppid = surface.родитель(Path("/proc"), live_pid)
        orphan, _ = surface.сирота(Path("/proc"), live_pid)
        print(f"  инфо: PPid своего сервера {ppid}, сирота: {orphan}")
        bad, foreign, _ = surface.находки(Path("/proc"), None, real_root, {})
        check(not has(bad, f"pid {live_pid}")
              and has(foreign, "свежий слушатель смены", f"pid {live_pid}"),
              f"БОЛЬНОЙ Н2 вживую: при пороге по умолчанию свой сервер — «свежий», не беда")
        bad, _, _ = surface.находки(Path("/proc"), None, real_root, {}, 0)
        check(has(bad, "мусор смены", f"pid {live_pid}", f"порт {live_port}"),
              f"вживую при пороге 0: свой сервер — «мусор смены» с pid {live_pid}")
        refused = []

        def own_only(pid):
            # Сигнал — только своему серверу; чужие кандидаты называются, не трогаются.
            if pid != live_pid:
                refused.append(pid)
                return False
            os.kill(pid, 15)
            return True

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            count = surface.убрать_мусор_смены(Path("/proc"), real_root, {}, убийца=own_only,
                                              мин_возраст_мин=0, не_трогать=[], ждать_сек=5.0)
        if refused:
            print(f"  инфо: в кандидатах чужие pid {refused} — названы, не тронуты")
        if orphan:
            check(count == 1 and not Path(f"/proc/{live_pid}").exists()
                  and "снято мусора смены: 1" in out.getvalue(),
                  f"вживую: уборка сняла свой осиротевший сервер, счёт ровно 1 (pid {live_pid})")
        else:
            print("  инфо: сервер не осиротел (субжнец?) — уборка судится по правилу сироты")
            check(count == 0 and Path(f"/proc/{live_pid}").exists()
                  and f"родитель жив (pid {ppid})" in out.getvalue(),
                  f"вживую: не сирота — не снят, причина названа (pid {live_pid})")
        if count != 1:
            print(f"        вывод уборки: {out.getvalue()[-400:]!r}")

shutil.rmtree(TMP, ignore_errors=True)
print(f"ИТОГ: зелёных {passed} из {total}")
sys.exit(0 if passed == total else 1)
PY
