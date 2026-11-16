#!/usr/bin/env python3
"""Пробы разборщика файла проверок продукта (scripts/lib/server_checks.py).

Тестовый этап двери (scripts/lib/door_checks.sh) верит этому модулю в трёх
местах: какие наборы гонять, можно ли взять записку deploy-checks.jsonl вместо
прогона и сошлись ли списки служб отката (подкоманда «службы» нужна только при
STACK_ROLLBACK_CMD). Ошибка любого из них — ложное «зелено» перед выкатом на
прод (И-4), поэтому больные случаи судятся точными значениями, а не «что-то
напечатал». Перенос пробы Content Factory (16d0ced), случаи те же, плюс
поле image набора (решение управляющего 16.11 03:25, п. 1).

Модуль зовётся как CLI через subprocess; путь — из SERVER_CHECKS_PY (дверь для
мутаций копии), иначе lib/server_checks.py рядом с самим тестом.
Образцы пишет сам тест в свой временный каталог.

Прогон (только фоном):
    python3 scripts/test_server_checks_lib.py
Итог: «SERVER-CHECKS-LIB: тестов N, неудач M»; код 0 — неудач нет, 1 — есть.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(os.environ.get("SERVER_CHECKS_PY")
              or Path(__file__).resolve().parent / "lib" / "server_checks.py")
# Клон репозитория продукта с настоящими rollback.sh и tag-current.sh; без него
# проба службы судит синтетические скрипты с тем же SERVICES="...".
DOOR_CLONE = os.environ.get("DOOR_CLONE", "")
DOOR_SHA = os.environ.get("DOOR_SHA", "HEAD")

# Восемь наборов поимённо из спеки §4.4: файл продукта обязан нести именно их.
EIGHT = ["frontend-check", "config-check", "script-tests", "leak-guard",
         "batch-tests", "api-tests", "integration-tests", "svc-tests"]
REAL_SERVICES = set(os.environ.get("DOOR_SERVICES", "svc-a svc-b svc-c svc-d").split())
SERVICES_RE = re.compile(r'^\s*SERVICES="([^"]*)"', re.M)

SHA = "a" * 40
FILE_SHA = "b" * 64


def run(*args, stdin=None):
    return subprocess.run([sys.executable, str(MODULE), *map(str, args)],
                          input=stdin, capture_output=True, text=True, timeout=60)


def suite_yaml(name, extra=""):
    # Первый шаг без dir/env, второй со всем: так судятся и умолчания, и разбор.
    return (f"  - name: {name}\n"
            f"    base: python:3.12-slim\n"
            f"    install: pip install -r /m/requirements.txt\n"
            f"    manifests: [requirements.txt]\n"
            f"    steps:\n"
            f"      - cmd: python -m pytest -q\n"
            f"      - dir: backend\n"
            f"        cmd: python -m pytest tests\n"
            f"        env: {{PYTHONPATH: /w}}\n"
            f"{extra}")


def eight_yaml():
    body = "".join(suite_yaml(n, "    stand: pg-redis\n    timeout_s: 1800\n"
                              if n == "integration-tests" else "") for n in EIGHT)
    return "suites:\n" + body


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="srv-checks-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def refused(self, proc, what):
        # Отказ — код 1, слова в stderr и пустой stdout: дверь не должна принять
        # полвывода за список наборов.
        self.assertEqual(proc.returncode, 1, f"{what}: rc={proc.returncode} out={proc.stdout!r}")
        self.assertTrue(proc.stderr.strip(), f"{what}: отказ без слов")
        self.assertEqual(proc.stdout.strip(), "", f"{what}: при отказе что-то в stdout")


class Suites(Base):
    def test_eight_suites_parsed(self):
        # Почему: пустой или урезанный разбор дал бы «все наборы зелены» на нуле
        # наборов; имена и порядок берутся разбором, умолчания судятся явно.
        path = self.write("server-checks.yaml", eight_yaml())
        proc = run("наборы", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suites = json.loads(proc.stdout)
        self.assertEqual([s["name"] for s in suites], EIGHT)
        first = suites[0]
        self.assertEqual(first["base"], "python:3.12-slim")
        self.assertEqual(first["install"], "pip install -r /m/requirements.txt")
        self.assertEqual(first["manifests"], ["requirements.txt"])
        self.assertEqual(first["stand"], "none")
        self.assertEqual(first["timeout_s"], 900)
        self.assertEqual(first["steps"], [
            {"dir": ".", "cmd": "python -m pytest -q", "env": {}},
            {"dir": "backend", "cmd": "python -m pytest tests", "env": {"PYTHONPATH": "/w"}}])
        integ = suites[EIGHT.index("integration-tests")]
        self.assertEqual((integ["stand"], integ["timeout_s"]), ("pg-redis", 1800))

    def test_empty_file_fails(self):
        # Почему: спека §4.2 — «пустой список — провал»; молчаливый ноль наборов
        # прошёл бы как tests=success без единого теста.
        self.refused(run("наборы", self.write("empty.yaml", "")), "пустой файл")
        self.refused(run("наборы", self.write("zero.yaml", "suites: []\n")), "suites: []")
        self.refused(run("наборы", self.tmp / "absent.yaml"), "нет файла")

    def test_bad_suites_fail(self):
        # Почему: повтор имени делает «2 из 8» неоднозначным, а шаг без cmd
        # превратился бы в пустой docker run, который «проходит».
        dup = "suites:\n" + suite_yaml("script-tests") + suite_yaml("script-tests")
        self.refused(run("наборы", self.write("dup.yaml", dup)), "повтор имени")
        nocmd = ("suites:\n  - name: script-tests\n    base: python:3.12-slim\n"
                 "    install: 'true'\n    manifests: []\n    steps:\n      - dir: backend\n")
        self.refused(run("наборы", self.write("nocmd.yaml", nocmd)), "шаг без cmd")

    def test_only_unknown_name_fails(self):
        # Почему: опечатка в STACK_CHECKS_ONLY тихо выбросила бы набор, и итог
        # «наборов 1 из 8» выдал бы себя за выбранные два.
        path = self.write("server-checks.yaml", eight_yaml())
        self.refused(run("наборы", path, "--только", "script-tests no-such-tests"),
                     "неизвестное имя в --только")

    def test_bad_stand_and_timeout_fail(self):
        # Почему: опечатка в stand (pg_redis) тихо запустила бы набор без стенда
        # PG+Redis, и его тесты судили бы не тот стек; timeout_s не положительное
        # целое ломает предел прогона уже в двери, а не при разборе.
        cases = {"stand pg_redis": "    stand: pg_redis\n",
                 "timeout_s abc": '    timeout_s: "abc"\n',
                 "timeout_s 0": "    timeout_s: 0\n"}
        for what, extra in cases.items():
            path = self.write("bad.yaml", "suites:\n" + suite_yaml("script-tests", extra))
            self.refused(run("наборы", path), what)

    def test_only_selects_two(self):
        # Почему: режим «2 из 8» должен гнать ровно выбранные, в порядке файла;
        # пустой --только (ключ не задан) — все наборы.
        path = self.write("server-checks.yaml", eight_yaml())
        proc = run("наборы", path, "--только", "api-tests  script-tests")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([s["name"] for s in json.loads(proc.stdout)],
                         ["script-tests", "api-tests"])
        proc = run("наборы", path, "--только", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([s["name"] for s in json.loads(proc.stdout)], EIGHT)

    def test_image_passed_through(self):
        # Почему: по полю image дверь решает, на каком образе идёт набор:
        # «артефакт» — на образе этапа B, то есть на том, что едет (спека §3
        # стр. 8). Разбор, выбросивший поле, тихо отправил бы набор на base —
        # проверено не то, что выкатывается. Значение отдаётся как есть, и
        # «артефакт», и произвольный образ; набор без поля его не получает.
        body = ("suites:\n"
                + suite_yaml("art-tests", "    image: артефакт\n")
                + suite_yaml("own-tests", "    image: registry.local/tests:1.2\n")
                + suite_yaml("plain-tests"))
        proc = run("наборы", self.write("server-checks.yaml", body))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = {s["name"]: s.get("image") for s in json.loads(proc.stdout)}
        self.assertEqual(got, {"art-tests": "артефакт",
                               "own-tests": "registry.local/tests:1.2",
                               "plain-tests": None})


class Hashes(Base):
    def test_image_hash_changes_with_manifest(self):
        # Почему: хэш, не видящий правки requirements.txt, оставил бы старый
        # образ набора со старыми зависимостями — тесты судят не тот стек.
        path = self.write("server-checks.yaml", "suites:\n" + suite_yaml("script-tests"))
        root = self.tmp / "copy"
        req = self.write("copy/requirements.txt", "pytest==8.0\n")
        install = "pip install -r /m/requirements.txt"
        expected = hashlib.sha256((install + "pytest==8.0\n").encode()).hexdigest()[:12]
        first = run("хэш-образа", path, "script-tests", root)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), expected)
        req.write_text("pytest==8.1\n", encoding="utf-8")
        second = run("хэш-образа", path, "script-tests", root)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertRegex(second.stdout.strip(), r"^[0-9a-f]{12}$")
        self.assertNotEqual(second.stdout.strip(), expected)
        req.unlink()
        # Пропавший манифест — отказ, а не хэш одной строки установки.
        self.refused(run("хэш-образа", path, "script-tests", root), "нет манифеста")
        self.refused(run("хэш-образа", path, "no-such", root), "нет набора")

    def test_file_hash_is_sha256(self):
        # Почему: этот хэш связывает записку с файлом проверок; любой другой
        # алгоритм разошёлся бы с полем checks_file_sha записки.
        path = self.write("server-checks.yaml", eight_yaml())
        proc = run("хэш-файла", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), hashlib.sha256(path.read_bytes()).hexdigest())


def note(ts, sha=SHA, file_sha=FILE_SHA, tests="success", only=()):
    return json.dumps({"ts": ts, "event": "deploy.checks", "outcome": "ok",
                       "data": {"sha": sha, "branch": "feat/x", "checks_file_sha": file_sha,
                                "tests": tests, "only": list(only), "suites": []}},
                      ensure_ascii=False)


class Notes(Base):
    def found(self, journal, only=""):
        proc = run("записка", journal, SHA, FILE_SHA, only)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_note_requires_same_checks_file(self):
        # Почему: ветка, правящая файл проверок, не должна жить на старом
        # «зелено»; годится только записка по той же тройке, последняя success.
        journal = self.write("deploy-checks.jsonl", "\n".join([
            note("2026-11-12T10:00:00+05:00"),
            note("2026-11-12T11:00:00+05:00"),
            "{оборванная строка",
            note("2026-11-12T12:00:00+05:00", tests="failure"),
            note("2026-11-12T13:00:00+05:00", file_sha="c" * 64),
            note("2026-11-12T14:00:00+05:00", sha="d" * 40),
        ]) + "\n")
        self.assertEqual(self.found(journal), f"{journal}\t2026-11-12T11:00:00+05:00")
        only_other = self.write("other.jsonl", note("2026-11-12T10:00:00+05:00", file_sha="c" * 64) + "\n")
        self.assertEqual(self.found(only_other), "")
        # Журнала ещё нет (первый прогон) — не ошибка, просто записки нет.
        self.assertEqual(self.found(self.tmp / "absent.jsonl"), "")

    def test_note_requires_same_only(self):
        # Почему: «зелено» по двум наборам не доказывает восемь; записка режима
        # «2 из 8» годится только тому же выбору (порядок слов не важен).
        journal = self.write("deploy-checks.jsonl", "\n".join([
            note("2026-11-12T10:00:00+05:00", only=["script-tests", "api-tests"]),
            note("2026-11-12T11:00:00+05:00"),
        ]) + "\n")
        self.assertEqual(self.found(journal, "api-tests script-tests"),
                         f"{journal}\t2026-11-12T10:00:00+05:00")
        self.assertEqual(self.found(journal, ""), f"{journal}\t2026-11-12T11:00:00+05:00")
        self.assertEqual(self.found(journal, "script-tests"), "")


def collect(selected, suites, only=()):
    payload = {"sha": SHA, "branch": "feat/x", "checks_file_sha": FILE_SHA,
               "selected": list(selected), "only": list(only),
               "suites": [{"name": n, "result": r, "passed": 1, "skipped": 0, "seconds": 1,
                           "log": f"/var/log/harness/door-checks/aaaaaaaa-{n}.log"}
                          for n, r in suites],
               "mem_min_mb": 9100, "load_max": 7.9, "seconds": 12}
    proc = run("записка-собрать", stdin=json.dumps(payload))
    if proc.returncode != 0:
        raise AssertionError(f"записка-собрать rc={proc.returncode}: {proc.stderr}")
    return json.loads(proc.stdout)


class Collect(Base):
    def test_success_needs_all_selected(self):
        # Почему: tests=success открывает выкат; он честен только когда каждый
        # выбранный набор passed ровно один раз, а «не отработало» — не «красно».
        two = ["script-tests", "api-tests"]
        ok = collect(two, [(n, "passed") for n in two], only=two)
        self.assertEqual(ok["tests"], "success")
        for key in ("sha", "branch", "checks_file_sha", "suites", "mem_min_mb",
                    "load_max", "seconds"):
            self.assertIn(key, ok)
        self.assertEqual(ok["sha"], SHA)
        self.assertEqual(ok["checks_file_sha"], FILE_SHA)
        self.assertEqual(sorted(ok["only"]), sorted(two))
        self.assertEqual([s["name"] for s in ok["suites"]], two)
        self.assertEqual(collect(two, [("script-tests", "passed"),
                                       ("api-tests", "failed")])["tests"], "failure")
        self.assertEqual(collect(two, [("script-tests", "passed"),
                                       ("api-tests", "error")])["tests"], "error")
        # error важнее failed: проверка не отработала, судить код нельзя.
        self.assertEqual(collect(two, [("script-tests", "failed"),
                                       ("api-tests", "error")])["tests"], "error")
        # Набор не прогнан — «не отработало», а не success.
        self.assertEqual(collect(two, [("script-tests", "passed")])["tests"], "error")
        # Число совпало, но один набор дважды, второго нет — не success.
        self.assertEqual(collect(two, [("script-tests", "passed"),
                                       ("script-tests", "passed")])["tests"], "error")
        # Ноль выбранных — не success, даже если «все ноль прошли»: отказ (rc 1)
        # или не-success одинаково честны.
        proc = run("записка-собрать", stdin=json.dumps({"sha": SHA, "branch": "feat/x",
                   "checks_file_sha": FILE_SHA, "selected": [], "only": [], "suites": [],
                   "mem_min_mb": 0, "load_max": 0, "seconds": 0}))
        if proc.returncode == 0:
            self.assertNotEqual(json.loads(proc.stdout)["tests"], "success")
        else:
            self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_unknown_result_is_error(self):
        # Почему: success открывает выкат и честен только при passed у каждого
        # выбранного набора; skipped, timeout или опечатка — «не отработало»,
        # то есть error, а не зелёный свет по умолчанию.
        two = ["script-tests", "api-tests"]
        self.assertNotEqual(collect(two, [("script-tests", "passed"),
                                          ("api-tests", "skipped")])["tests"], "success")
        self.assertEqual(collect(two, [("script-tests", "passed"),
                                       ("api-tests", "timeout")])["tests"], "error")


class Services(Base):
    def real_scripts(self):
        paths = []
        for name in ("rollback.sh", "tag-current.sh"):
            if not DOOR_CLONE:
                paths.append(self.write(name, f'SERVICES="{" ".join(sorted(REAL_SERVICES))}"\n'))
                continue
            proc = subprocess.run(["git", "-C", DOOR_CLONE, "show", f"{DOOR_SHA}:scripts/{name}"],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, f"git show {name}: {proc.stderr}")
            paths.append(self.write(name, proc.stdout))
        return paths

    def test_services_agree_on_real_scripts(self):
        # Почему: итог отката называет «откатаны 6 служб»; список берётся
        # разбором настоящих скриптов продукта, а не верой в константу.
        paths = self.real_scripts()
        parsed = [set(SERVICES_RE.search(p.read_text()).group(1).split()) for p in paths]
        self.assertEqual(parsed[0], parsed[1])
        self.assertEqual(parsed[0], REAL_SERVICES)
        proc = run("службы", *paths)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), " ".join(sorted(REAL_SERVICES)))

    def test_services_extra_fails(self):
        # Почему: служба, которую снимок помечает, а откат не возвращает, —
        # это «откатано» на словах при старом образе в деле.
        rollback, tag = self.real_scripts()
        text = tag.read_text()
        tag.write_text(SERVICES_RE.sub(lambda m: f'SERVICES="{m.group(1)} svc-extra"', text, count=1))
        proc = run("службы", rollback, tag)
        self.refused(proc, "лишняя служба")
        self.assertIn("svc-extra", proc.stderr)
        self.assertIn(tag.name, proc.stderr)

    def test_services_empty_fails(self):
        # Почему: пустое множество «сходится» само с собой, и итог сказал бы
        # «откатаны 0 служб» как успех.
        empty = self.write("empty.sh", '#!/bin/bash\nSERVICES=""\n')
        self.refused(run("службы", empty, empty), "пустой SERVICES")
        none = self.write("none.sh", "#!/bin/bash\necho нет списка\n")
        self.refused(run("службы", none), "нет строки SERVICES")


def main():
    if not MODULE.is_file():
        print(f"нет модуля: {MODULE}")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    bad = len(result.failures) + len(result.errors)
    print(f"SERVER-CHECKS-LIB: тестов {result.testsRun}, неудач {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
