#!/usr/bin/env python3
"""Проба: обычный прогон sentinel гасит залипшую sentinel:start-red — до кода.

Карточка hr.stale-alerts-fix, диагноз .tasks/hr.stale-alerts-03-11/отчёт.md:
тревогу sentinel:start-red заводит прогон --boot, а гасит тоже только --boot —
обычный прогон по таймеру (каждые 5 мин) выходит раньше всех --resolve. Причина
снята, а красное висит до следующей перезагрузки.

Стенд — стенд доноса (test_sentinel_drain.SentinelStand): свои паспорт, LOG_DIR,
карта и журнал тревог, НАСТОЯЩИЙ писатель alert-task.py. Ключ открывается
настоящим зовом писателя. Условия подъёма судит настоящий boot_check.py по
своему перечню (стена, docker, nginx — юниты); systemctl — заглушка, «inactive»
отвечает тем юнитам, что названы в $STUB/inactive. Ни один настоящий юнит не
спрашивается и не трогается. Боевая карта сверяется по sha1 до и после.

    python3 scripts/test_sentinel_resolve.py

Двери мутаций ([[probe-loads-the-live-mechanism]]):
    HARNESS_SENTINEL     путь sentinel.sh под пробой
    HARNESS_BOOT_CHECK   путь boot_check.py, который кладётся в проект-стенд
Последняя строка: `sentinel гасит залипшее: случаев N, падений F`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import test_alert_task as alerts  # noqa: E402
import test_sentinel_drain as drain  # noqa: E402
from selftest_cases import Cases  # noqa: E402

SENTINEL = drain.SENTINEL
BOOT_CHECK = Path(os.environ.get("HARNESS_BOOT_CHECK") or HERE / "boot_check.py")
KEY = "sentinel:start-red"

cases = Cases()
check = cases.check

STUB_SYSTEMCTL = """#!/bin/sh
case "$1" in
    is-active)
        if grep -qx "$2" "$STUB/inactive" 2>/dev/null; then echo inactive; exit 3; fi
        echo active ;;
esac
exit 0
"""

# Условия из диагноза: стена, docker и nginx подняты — значит красного нет.
BOOT_LIST = """проверки:
  - { имя: стена,  вид: юнит, юнит: harness-firewall }
  - { имя: docker, вид: юнит, юнит: docker }
  - { имя: nginx,  вид: юнит, юнит: nginx }
"""


class ResolveStand(drain.SentinelStand):
    def __init__(self, base: Path):
        super().__init__(base)
        self.stub = base / "stub"
        self.stub.mkdir()
        (self.bin / "systemctl").write_text(STUB_SYSTEMCTL, encoding="utf-8")
        scripts = self.project / "scripts"
        shutil.copy(HERE / "live-check.sh", scripts / "live-check.sh")
        shutil.copy(BOOT_CHECK, scripts / "boot_check.py")
        self.list = base / "boot-check.yaml"
        self.list.write_text(BOOT_LIST, encoding="utf-8")

    def stop_unit(self, unit: str) -> None:
        (self.stub / "inactive").write_text(unit + "\n", encoding="utf-8")

    def extra_env(self) -> dict:
        return {"PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "STUB": str(self.stub),
                "SENTINEL_UNITS": "harness-agent", "HEARTBEAT_DIR": str(self.heartbeat),
                "HARNESS_BOOT_CHECK_LIST": str(self.list)}

    def boot_check(self) -> int:
        """Тот же boot_check, что позовёт sentinel, — чтобы стенд судил условие, а не пустоту."""
        done = subprocess.run([sys.executable, str(self.project / "scripts" / "boot_check.py")],
                              env=self.env(**self.extra_env()), capture_output=True, text=True,
                              timeout=60, cwd=str(self.project))
        return done.returncode

    def sentinel_plain(self) -> dict:
        """Один ОБЫЧНЫЙ прогон — как от таймера, без --boot."""
        try:
            done = subprocess.run(["bash", str(SENTINEL)], env=self.env(**self.extra_env()),
                                  capture_output=True, text=True, timeout=180, cwd=str(self.base))
            rc, out = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            rc, out = -9, "предел 180 с истёк"
        return {"rc": rc, "out": out, "reached_end": (self.heartbeat / "sentinel").exists()}

    def open_key(self) -> None:
        rc, _, err = self.raise_alert(KEY, "Сервер перезагружен, но не всё поднялось: docker")
        check("стенд: писатель завёл ключ (код 0), и сам писатель зовёт его красным — иначе случай ни о чём",
              (0, "КРАСНОЕ"), (rc, self.key_state()))
        if rc != 0:
            print(f"  писатель: {err.strip()[:200]}")

    def key_state(self) -> str:
        """Состояние ключа словами самого писателя (--is-green): КРАСНОЕ | ЗЕЛЁНОЕ | НЕТ."""
        _, out, _ = self.run("--is-green", KEY)
        return (out.split() or ["пусто"])[0]


def section_green(tmp: Path) -> None:
    print("── ключ открыт, причина снята: ОДИН обычный прогон гасит ──")
    st = ResolveStand(tmp / "green")
    st.open_key()
    check("стенд: boot_check на стенде зелёный (код 0)", 0, st.boot_check())
    got = st.sentinel_plain()
    check("sentinel дошёл до конца (метка heartbeat поставлена)", True, got["reached_end"])
    check("после обычного прогона в журнале тревог «зелёное» по sentinel:start-red", True,
          "зелёное" in st.kinds(KEY))
    check("писатель зовёт ключ зелёным (--is-green)", "ЗЕЛЁНОЕ", st.key_state())
    if "зелёное" not in st.kinds(KEY):
        print(f"  события ключа: {st.kinds(KEY)}")


def section_red(tmp: Path) -> None:
    print("── ключ открыт, docker стоит: обычный прогон НЕ гасит ──")
    st = ResolveStand(tmp / "red")
    st.open_key()
    st.stop_unit("docker")
    check("стенд: boot_check на стенде красный (код 1) — docker inactive", 1, st.boot_check())
    got = st.sentinel_plain()
    check("sentinel дошёл до конца (метка heartbeat поставлена)", True, got["reached_end"])
    check("красное условие — «зелёного» по sentinel:start-red нет", False, "зелёное" in st.kinds(KEY))
    check("писатель по-прежнему зовёт ключ красным (--is-green)", "КРАСНОЕ", st.key_state())


def main() -> int:
    live_before = alerts.sha1(alerts.LIVE_MAP)
    check(f"sentinel на месте: {SENTINEL.name}", True, SENTINEL.is_file())
    check(f"носитель проверки на месте: {BOOT_CHECK.name}", True, BOOT_CHECK.is_file())
    # Писатель отказывает зову с предком claude: под сессией стенд не откроет ключ,
    # а sentinel не погасит его, — случаи судили бы отказ, а не sentinel.
    under_session = alerts.ancestors_named("claude")
    check("проба сама не под сессией (запуск фоном: setsid nohup … &)", [], under_session)
    if under_session:
        print(f"sentinel гасит залипшее: случаев {cases.paths}, падений {cases.failures}")
        return 1
    with tempfile.TemporaryDirectory(prefix="sentinel-resolve-probe-") as tmp:
        for section in (section_green, section_red):
            try:
                section(Path(tmp))
            except Exception as error:  # упавший раздел — неудача, а не тишина
                check(f"раздел {section.__name__} дошёл до конца", "дошёл",
                      f"упал: {type(error).__name__}: {error}")
            check(f"{section.__name__}: боевая карта не тронута", live_before,
                  alerts.sha1(alerts.LIVE_MAP))
    print(f"sentinel гасит залипшее: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
