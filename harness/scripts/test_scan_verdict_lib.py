#!/usr/bin/env python3
"""Пробы разборщика вывода сканера образа (scripts/lib/scan_verdict.py).

Этапы D/I выката (спека эталонного выката §2.2, план фаза 4) верят этому модулю
в одном месте: есть ли в образе :<sha12> уязвимость, которая останавливает выкат
при политике critical-fixable. Ошибка в любую сторону дорога: «блокирующих нет»
на непрочитанном выводе — ложное зелёное (И-4), а код 1 на падении самого
модуля читался бы как «есть блокирующие» и отказывал без причины. Поэтому коды
судятся точными значениями.

Контракт (CLI, Trivy JSON на stdin: Results[].Vulnerabilities[] с Severity и
FixedVersion):
  код 0 — блокирующих нет; CRITICAL без FixedVersion названы в stdout (id);
  код 1 — есть CRITICAL с непустым FixedVersion; каждый назван в stdout:
          VulnerabilityID, PkgName и FixedVersion;
  код 2 — вывод не прочитан: не JSON, пусто, не объект, Results или
          Vulnerabilities не списки, объект без SchemaVersion и без Results.
Объект Trivy без Results (SchemaVersion есть) — чистый образ: Trivy опускает
пустой Results.

Модуль зовётся как CLI через subprocess; путь — из SCAN_VERDICT_PY (дверь для
мутаций копии), иначе lib/scan_verdict.py рядом с самим тестом.

Прогон (только фоном):
    python3 scripts/test_scan_verdict_lib.py
Итог: «SCAN-VERDICT-LIB: тестов N, неудач M»; код 0 — неудач нет, 1 — есть.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

MODULE = Path(os.environ.get("SCAN_VERDICT_PY")
              or Path(__file__).resolve().parent / "lib" / "scan_verdict.py")


def run(text):
    return subprocess.run([sys.executable, str(MODULE)], input=text,
                          capture_output=True, text=True, timeout=60)


def vuln(vid, severity, fixed=None, pkg="openssl"):
    item = {"VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": "1.0.0",
            "Severity": severity}
    if fixed is not None:
        item["FixedVersion"] = fixed
    return item


def report(*results):
    return json.dumps({"SchemaVersion": 2, "ArtifactName": "proba-api:0123456789ab",
                       "ArtifactType": "container_image", "Results": list(results)})


def result(target, *vulns):
    data = {"Target": target, "Class": "os-pkgs", "Type": "alpine"}
    if vulns:
        data["Vulnerabilities"] = list(vulns)
    return data


class Verdict(unittest.TestCase):
    def code(self, proc, want, why):
        # Нет модуля — python выходит кодом 2, и «не прочитано» зеленело бы без модуля.
        self.assertTrue(MODULE.is_file(), f"нет модуля: {MODULE}")
        self.assertNotIn("Traceback", proc.stderr, f"{why}: модуль упал\n{proc.stderr}")
        self.assertEqual(want, proc.returncode, f"{why}: stdout={proc.stdout!r} stderr={proc.stderr!r}")

    def test_critical_fixable_blocks(self):
        proc = run(report(result("alpine", vuln("CVE-2099-0001", "CRITICAL", "1.0.1", "libssl3"))))
        self.code(proc, 1, "CRITICAL с исправлением")
        for word in ("CVE-2099-0001", "libssl3", "1.0.1"):
            self.assertIn(word, proc.stdout, f"блокирующая названа: {word}")

    def test_critical_without_fix_passes_named(self):
        # Почему: исправления нет — выкат его не принесёт и не уберёт; отказ
        # остановил бы любой выкат образа навсегда. Но молчать о нём нельзя.
        proc = run(report(result("alpine", vuln("CVE-2099-0002", "CRITICAL", ""))))
        self.code(proc, 0, "CRITICAL с пустым FixedVersion")
        self.assertIn("CVE-2099-0002", proc.stdout)
        proc = run(report(result("alpine", vuln("CVE-2099-0003", "CRITICAL"))))
        self.code(proc, 0, "CRITICAL без поля FixedVersion")
        self.assertIn("CVE-2099-0003", proc.stdout)

    def test_high_fixable_passes(self):
        self.code(run(report(result("alpine", vuln("CVE-2099-0004", "HIGH", "2.0")))), 0,
                  "HIGH с исправлением — не CRITICAL")

    def test_clean_image(self):
        self.code(run(report(result("alpine"))), 0, "Results без Vulnerabilities")
        self.code(run(json.dumps({"SchemaVersion": 2, "ArtifactName": "x"})), 0,
                  "Trivy опустил пустой Results")

    def test_blocking_in_second_result(self):
        proc = run(report(result("alpine"),
                          result("app/requirements.txt", vuln("CVE-2099-0005", "CRITICAL", "3.1", "jinja2"))))
        self.code(proc, 1, "CRITICAL с исправлением во втором Results")
        self.assertIn("CVE-2099-0005", proc.stdout)

    def test_mixed_names_blocking(self):
        proc = run(report(result("alpine", vuln("CVE-2099-0006", "CRITICAL", ""),
                                 vuln("CVE-2099-0007", "CRITICAL", "4.2", "zlib"))))
        self.code(proc, 1, "без исправления и с исправлением вместе")
        self.assertIn("CVE-2099-0007", proc.stdout)

    def test_unreadable_is_two(self):
        # Почему: «не прочитано» не зелёное (И-4) и не «блокирующие есть».
        for text, why in (("{не json", "битый JSON"), ("", "пустой вывод"),
                          ("[]", "список вместо объекта"), ("{}", "объект без SchemaVersion и Results"),
                          (json.dumps({"SchemaVersion": 2, "Results": "x"}), "Results не список"),
                          (json.dumps({"SchemaVersion": 2, "Results": [{"Target": "a", "Vulnerabilities": {"a": 1}}]}),
                           "Vulnerabilities не список")):
            with self.subTest(why=why):
                self.code(run(text), 2, why)

    def test_deep_nesting_is_two(self):
        # Почему: json бросает RecursionError на глубокой вложенности — не ValueError; без
        # разбора её модуль падал кодом 1, и «блокирующие есть» отказывало без причины.
        depth = 200000
        self.code(run("[" * depth + "]" * depth), 2, "вложенность глубже предела рекурсии")


def main():
    if not MODULE.is_file():
        print(f"нет модуля: {MODULE}")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    res = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    bad = len(res.failures) + len(res.errors)
    print(f"SCAN-VERDICT-LIB: тестов {res.testsRun}, неудач {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
