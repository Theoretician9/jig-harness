#!/usr/bin/env python3
"""Проба scripts/standard-current.py: актуальный RFC и цепочка замен по индексу rfc-editor.

Индекс — ЗАПИСАННЫЙ фрагмент настоящего https://www.rfc-editor.org/rfc-index.txt
(скачан 19.11.2026, «CREATED ON: 11/18/2026», шапка и записи дословно):
scripts/fixtures/rfc-index-fragment.txt. Выдуманный индекс проверял бы моё
представление о формате, а не формат ([[capture-foreign-output-do-not-recall-it]]).

Сеть пробе не нужна: стенд кладёт фрагмент кэшем в свой LOG_DIR, а дверь
RFC_INDEX_URL ведёт перекачку кэша на file:// стенда.

Дверь мутации: STANDARD_CURRENT_UNDER_TEST=<путь> — какой файл гнать вместо
scripts/standard-current.py рядом с пробой.

    python3 scripts/test_standard_current.py
Итог — последняя строка «ИТОГО: случаев N, неудач K»; код 1 при K>0.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBJECT = Path(os.environ.get("STANDARD_CURRENT_UNDER_TEST") or HERE / "standard-current.py")
FRAGMENT = HERE / "fixtures" / "rfc-index-fragment.txt"
CACHE_NAME = "rfc-index.txt"
WEEK_AND_DAY = 8 * 24 * 3600

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f" — {detail[-600:]}"))


class Stand:
    """Свой LOG_DIR, свой паспорт и своё «сетевое» место индекса."""

    def __init__(self, cache: str | None, remote: str | None, cache_age: float = 0):
        self.root = Path(tempfile.mkdtemp(prefix="rfc-stand-"))
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.cache = self.logs / CACHE_NAME
        if cache is not None:
            self.cache.write_text(cache, encoding="utf-8")
            if cache_age:
                old = time.time() - cache_age
                os.utime(self.cache, (old, old))
        place = self.root / "remote-index.txt"
        if remote is not None:
            place.write_text(remote, encoding="utf-8")
        conf = self.root / "install.conf"
        conf.write_text(f'LOG_DIR="{self.logs}"\nPROJECT_DIR="{self.root}"\n', encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HARNESS_", "RFC_", "LOG_DIR", "PROJECT_DIR"))}
        self.env.update(HARNESS_INSTALL_CONF=str(conf), LOG_DIR=str(self.logs),
                        RFC_INDEX_URL=place.as_uri(), PYTHONDONTWRITEBYTECODE="1")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SUBJECT), *args], capture_output=True,
                              text=True, errors="replace", env=self.env, timeout=60)

    def drop(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def current_part(out: str) -> str:
    """Часть вывода после «актуальные:» — там судится, что выдано актуальным."""
    return out.split("актуальные:", 1)[1] if "актуальные:" in out else ""


def both(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def cases_on_fragment(fragment: str) -> None:
    s = Stand(fragment, None)
    try:
        r = s.run("2616")
        cur = current_part(r.stdout)
        check("2616: код 0", r.returncode == 0, both(r))
        check("2616: актуальные — 9110, 9111, 9112",
              all(f"RFC {n}" in cur for n in ("9110", "9111", "9112")), both(r))
        check("2616: заменённые 2616/7230/7231 актуальными не названы",
              not any(f"RFC {n}" in cur for n in ("2616", "7230", "7231")), both(r))
        check("2616: цепочки 2616 → 7230 → 9112 и 2616 → 7234 → 9111",
              "2616 → 7230 → 9112" in r.stdout and "2616 → 7234 → 9111" in r.stdout, both(r))
        check("9112: «обновлён» из Updated by — RFC 9931", "9931" in cur, both(r))

        r = s.run("8446")
        cur = current_part(r.stdout)
        check("8446 (TLS 1.3) заменён 9846 — замена 2026 года",
              r.returncode == 0 and "RFC 9846" in cur and "RFC 8446" not in cur, both(r))

        r = s.run("2119")
        cur = current_part(r.stdout)
        check("2119 актуален, но обновлён 8174",
              r.returncode == 0 and "RFC 2119" in cur and "8174" in cur and "→" not in r.stdout,
              both(r))

        r = s.run("RFC9110")
        check("«RFC9110» принят как номер",
              r.returncode == 0 and "RFC 9110" in current_part(r.stdout), both(r))

        r = s.run("json")
        cur = current_part(r.stdout)
        check("«json»: актуальные 8259 и 7519",
              r.returncode == 0 and "RFC 8259" in cur and "RFC 7519" in cur, both(r))
        check("«json»: заменённые 7159/7158/4627 актуальными не выданы",
              not any(f"RFC {n}" in cur for n in ("7159", "7158", "4627")), both(r))

        r = s.run("Internet", "Message", "Format")
        cur = current_part(r.stdout)
        check("«Internet Message Format»: 5322, не 2822",
              r.returncode == 0 and "RFC 5322" in cur and "RFC 2822" not in cur, both(r))

        r = s.run("99999")
        check("номера нет в индексе — код 1, без трассы",
              r.returncode == 1 and "Traceback" not in r.stderr, both(r))
        r = s.run("14")
        check("«Not Issued» — код 1 и слова «не издан»",
              r.returncode == 1 and "не издан" in both(r), both(r))
        r = s.run("слововкотороговнетнигде")
        check("ключевое слово без находок — код 1", r.returncode == 1, both(r))
        r = s.run()
        check("без довода — код 2", r.returncode == 2, both(r))
    finally:
        s.drop()


def cases_on_cache(fragment: str) -> None:
    header_only = fragment.split("\n14 Not Issued.", 1)[0] + "\n"

    # Свежий кэш не перекачивается: место «сети» пусто, а ответ есть.
    s = Stand(fragment, None)
    try:
        r = s.run("9110")
        check("свежий кэш: сеть не нужна", r.returncode == 0, both(r))
    finally:
        s.drop()

    # Свежий кэш НЕ перекачивается, даже когда «сеть» отдаёт другое.
    s = Stand(header_only, fragment)
    try:
        r = s.run("2616")
        check("свежий кэш не перекачан: ответ по кэшу (в нём записей нет)",
              r.returncode == 1 and s.cache.read_text(encoding="utf-8") == header_only, both(r))
    finally:
        s.drop()

    # Протухший кэш (8 суток) перекачивается: в кэше одна шапка, в «сети» — фрагмент.
    s = Stand(header_only, fragment, cache_age=WEEK_AND_DAY)
    try:
        r = s.run("2616")
        check("протухший кэш перекачан, ответ по новому индексу",
              r.returncode == 0 and "RFC 9110" in current_part(r.stdout), both(r))
        check("перекачанный индекс лёг в кэш LOG_DIR",
              s.cache.read_text(encoding="utf-8") == fragment
              and time.time() - s.cache.stat().st_mtime < 600, "кэш не обновлён")
    finally:
        s.drop()

    # Протухший кэш и сеть недоступна — ответ по старому кэшу с предупреждением.
    s = Stand(fragment, None, cache_age=WEEK_AND_DAY)
    try:
        r = s.run("2616")
        check("сеть недоступна: ответ по старому кэшу и предупреждение о кэше",
              r.returncode == 0 and "RFC 9110" in current_part(r.stdout) and "кэш" in r.stderr,
              both(r))
    finally:
        s.drop()

    # Ни кэша, ни сети — код 1 словами, не трассой.
    s = Stand(None, None)
    try:
        r = s.run("2616")
        check("нет кэша и сети — код 1 словами, без трассы",
              r.returncode == 1 and "Traceback" not in r.stderr and bool(r.stderr.strip()), both(r))
    finally:
        s.drop()


def main() -> int:
    print(f"проба: {SUBJECT}")
    fragment = FRAGMENT.read_text(encoding="utf-8")
    cases_on_fragment(fragment)
    cases_on_cache(fragment)
    bad = sum(1 for _, ok in RESULTS if not ok)
    print(f"ИТОГО: случаев {len(RESULTS)}, неудач {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
