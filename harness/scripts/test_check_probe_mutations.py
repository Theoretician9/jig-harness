#!/usr/bin/env python3
"""Probes for the gate «a probe file without a mutation record turns red».

Card hr.probe-without-mutation-gate. The owner asked on 24.10: «is there a
guarantee that mutations are made for every test». Measured 27.10: four probe
files of the harness tree have no record in harness/config/probe-mutations.yaml
(test_environment.sh, test_install_voice.sh, test_work_queue.py,
test_layout_journal.py), and both existing mutation gates stay green
(`mutation-probe.sh --сверить-тексты`: 1263 mutations, rc 0;
`build-live-check-list.py --проверить`: rc 0) — they judge only the rows that
ARE in the data, so a new probe without a row is silent.

Contract of the carrier `scripts/check-probe-mutations.py` (written after
these probes):

  * scope — every file of the tree whose basename is `test_*.py` or
    `test_*.sh`, tracked or untracked-but-not-ignored
    (`git ls-files --cached --others --exclude-standard`): a probe that was
    just written and not yet added is exactly the silent case;
  * a probe is covered when
      - a key of `пробы:` in probe-mutations.yaml names it with a non-empty
        `мутация` (the key is normalised: `#suffix` cut, `os.path.normpath` —
        the live data spells one probe as `scripts/./test_x.py`,
        `harness/../scripts/test_x.py`, `scripts/test_x.sh#name` to give it
        several mechanisms), or
      - an invariant of invariants.yaml with a non-empty `мутация` names it as
        a separate WORD of `доказательство` (not a substring), or
      - it falls under a declared exception: top-level key `без_мутаций:` of
        probe-mutations.yaml, a mapping «path or directory prefix ending in /»
        → reason (a non-empty string);
  * output: always `судимых проб: N`; each uncovered file —
    `без записи мутаций: <path>`; an exception with no reason —
    `исключение без причины: <key>`; an exception that matches no probe file —
    `исключение ни к чему не относится: <key>`;
  * exit 0 only when every judged probe is covered, every exception has a
    reason and matches something, and N > 0. An empty set is red: an empty
    scope is indistinguishable from a broken read ([[a-gate-alive-with-empty-data]]).
    A missing carrier is red and named, never green
    ([[unreadable-source-turns-check-green]]).

Doors (the probe never reads the live data — [[a-check-creates-its-own-condition]]):
  HARNESS_PROBE_MUTATIONS — path to the probe-mutations copy
  HARNESS_INVARIANTS      — path to the invariants copy
  HARNESS_PROBE_TREE      — root of the tree to scan (a git repository)
  HARNESS_PROBE_COVERAGE_TOOL — path of the carrier itself (a copy under
                            mutation), default scripts/check-probe-mutations.py

Every case builds its own yard: a temporary git repository with its own probe
files and its own data copies. The live carriers are compared by md5 before
and after the run.

Run:    python3 scripts/test_check_probe_mutations.py
Last line: «ПРОБЫ-БЕЗ-МУТАЦИЙ: случаев N, неудач M»; exit 0 — all green.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = Path(os.environ.get("HARNESS_PROBE_COVERAGE_TOOL")
            or ROOT / "scripts" / "check-probe-mutations.py")
LIVE_DATA = [ROOT / "harness" / "config" / "probe-mutations.yaml",
             ROOT / "harness" / "config" / "invariants.yaml"]

results = {"good": 0, "bad": 0}


def md5_of(paths):
    return {str(p): hashlib.md5(p.read_bytes()).hexdigest()
            for p in paths if p.exists()}


def verdict(name, ok, why):
    if ok:
        print(f"  ок    {name}")
        results["good"] += 1
    else:
        print(f"  ПЛОХО {name}: {why}")
        results["bad"] += 1


class Yard:
    """A temporary tree with probe files and its own data copies."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="probe-cov-"))
        self.tree = self.dir / "tree"
        self.tree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.tree)], check=True)
        self.mutations = self.dir / "probe-mutations.yaml"
        self.invariants = self.dir / "invariants.yaml"
        self.invariants.write_text("инварианты: []\n", encoding="utf-8")

    def probe(self, rel, tracked=True):
        path = self.tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# probe\n", encoding="utf-8")
        if tracked:
            subprocess.run(["git", "-C", str(self.tree), "add", rel], check=True)

    def data(self, text):
        self.mutations.write_text(text, encoding="utf-8")

    def run(self, **override):
        env = dict(os.environ)
        env.update({
            "HARNESS_PROBE_MUTATIONS": str(self.mutations),
            "HARNESS_INVARIANTS": str(self.invariants),
            "HARNESS_PROBE_TREE": str(self.tree),
        })
        env.update(override)
        try:
            done = subprocess.run([sys.executable, str(TOOL)], env=env,
                                  capture_output=True, text=True, timeout=60,
                                  cwd=str(self.dir))
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return done.returncode, done.stdout + done.stderr

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def entry(key, mutation="'s/a/b/'"):
    return (f"  {key}:\n"
            f"    механизм: scripts/mech.sh\n"
            f"    доказательство: bash {key.split('#')[0]}\n"
            f"    подмена: '-'\n"
            f"    мутация: {mutation}\n"
            f"    мутация_случай: 'случай'\n")


HEALTHY = ("пробы:\n"
           + entry("scripts/test_a.py")
           + entry("scripts/test_b.sh")
           + entry("harness/panel/test_c.py"))


def healthy_yard():
    yard = Yard()
    yard.probe("scripts/test_a.py")
    yard.probe("scripts/test_b.sh")
    yard.probe("harness/panel/test_c.py")
    yard.data(HEALTHY)
    return yard


# ── SICK FIRST: a new probe without a record ────────────────────────────────

def sick_untracked_new_probe():
    """The silent case of 24.10: a just-written probe, not yet added to git."""
    yard = healthy_yard()
    try:
        yard.probe("scripts/test_new.py", tracked=False)
        rc, out = yard.run()
        verdict("больной: новая проба (не в git) без записи — красный с именем",
                rc == 1 and "без записи мутаций: scripts/test_new.py" in out
                and "судимых проб: 4" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def sick_tracked_probe():
    yard = healthy_yard()
    try:
        yard.probe("scripts/hooks/test_hook.sh")
        rc, out = yard.run()
        verdict("больной: проба в git без записи — красный с именем",
                rc == 1 and "без записи мутаций: scripts/hooks/test_hook.sh" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def sick_names_only_the_uncovered():
    """The verdict names the guilty file and nobody else."""
    yard = healthy_yard()
    try:
        yard.probe("scripts/test_new.py")
        rc, out = yard.run()
        verdict("больной: назван ТОЛЬКО непокрытый файл",
                rc == 1 and out.count("без записи мутаций:") == 1,
                f"rc={rc}, строк «без записи»: {out.count('без записи мутаций:')}")
    finally:
        yard.close()


def sick_entry_without_mutation():
    """A record with an empty `мутация` promises a mutation nobody runs."""
    yard = healthy_yard()
    try:
        yard.data(HEALTHY.replace(entry("scripts/test_b.sh"),
                                  entry("scripts/test_b.sh", mutation="[]")))
        rc, out = yard.run()
        verdict("больной: запись с пустой мутацией не покрывает",
                rc == 1 and "без записи мутаций: scripts/test_b.sh" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def sick_substring_is_not_a_word():
    """An invariant proving `test_x.py.bak` does not cover `test_x.py`."""
    yard = Yard()
    try:
        yard.probe("scripts/test_x.py")
        yard.data("пробы: {}\n")
        yard.invariants.write_text(
            "инварианты:\n"
            "  - id: и-1\n"
            "    файл: scripts/mech.sh\n"
            "    доказательство: python3 scripts/test_x.py.bak\n"
            "    мутация: 's/a/b/'\n", encoding="utf-8")
        rc, out = yard.run()
        verdict("больной: подстрока в доказательстве инварианта не покрывает",
                rc == 1 and "без записи мутаций: scripts/test_x.py" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


# ── HEALTHY ─────────────────────────────────────────────────────────────────

def healthy_all_covered():
    yard = healthy_yard()
    try:
        rc, out = yard.run()
        verdict("здоровый: у каждой пробы запись — зелёный, счёт назван",
                rc == 0 and "судимых проб: 3" in out
                and "без записи мутаций" not in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def healthy_spelling_variants():
    """The live data spells one probe several ways to give it several
    mechanisms; each spelling must count, or the gate reddens the live tree."""
    yard = Yard()
    try:
        yard.probe("scripts/test_a.py")
        yard.probe("scripts/test_b.sh")
        yard.probe("harness/panel/test_c.py")
        yard.data("пробы:\n"
                  + entry("scripts/./test_a.py")
                  + entry("scripts/test_b.sh#second")
                  + entry("harness/../harness/panel/test_c.py"))
        rc, out = yard.run()
        verdict("здоровый: написания ключа (./, ../, #имя) покрывают файл",
                rc == 0 and "судимых проб: 3" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def healthy_invariant_word():
    yard = Yard()
    try:
        yard.probe("scripts/test_x.py")
        yard.data("пробы: {}\n")
        yard.invariants.write_text(
            "инварианты:\n"
            "  - id: и-1\n"
            "    файл: scripts/mech.sh\n"
            "    доказательство: python3 scripts/test_x.py --selftest\n"
            "    мутация: 's/a/b/'\n", encoding="utf-8")
        rc, out = yard.run()
        verdict("здоровый: проба названа словом доказательства инварианта",
                rc == 0 and "судимых проб: 1" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def healthy_not_a_probe():
    """`test_x.sh.wip` and `mytest_y.py` are not probe files."""
    yard = healthy_yard()
    try:
        yard.probe("scripts/test_x.sh.wip", tracked=False)
        yard.probe("scripts/mytest_y.py")
        rc, out = yard.run()
        verdict("здоровый: .wip и mytest_* не судятся",
                rc == 0 and "судимых проб: 3" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def healthy_ignored_not_judged():
    """A file ignored by .gitignore is not part of the tree."""
    yard = healthy_yard()
    try:
        (yard.tree / ".gitignore").write_text("junk/\n", encoding="utf-8")
        yard.probe("junk/test_z.py", tracked=False)
        rc, out = yard.run()
        verdict("здоровый: файл под .gitignore не судится",
                rc == 0 and "судимых проб: 3" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


# ── THE LEGAL DOOR: a declared exception WITH a reason ──────────────────────

def door_exception_with_reason_file():
    yard = healthy_yard()
    try:
        yard.probe("scripts/test_env.sh")
        yard.data(HEALTHY + "без_мутаций:\n"
                  "  scripts/test_env.sh: 'замер окружения машины, механизма нет'\n")
        rc, out = yard.run()
        verdict("дверь: исключение файла с причиной — зелёный",
                rc == 0 and "судимых проб: 4" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def door_exception_with_reason_dir():
    yard = healthy_yard()
    try:
        yard.probe(".tasks/t1/probes/test_p.py")
        yard.probe(".tasks/t2/probes/test_q.sh")
        yard.data(HEALTHY + "без_мутаций:\n"
                  "  .tasks/: 'пробы задачи до шага кода, переезжают в scripts/'\n")
        rc, out = yard.run()
        verdict("дверь: исключение каталога (префикс с /) с причиной — зелёный",
                rc == 0 and "судимых проб: 5" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def door_prefix_needs_slash():
    """`scripts/test_e` must not cover `scripts/test_env.sh` by prefix."""
    yard = healthy_yard()
    try:
        yard.probe("scripts/test_env.sh")
        yard.data(HEALTHY + "без_мутаций:\n"
                  "  scripts/test_e: 'не проба'\n")
        rc, out = yard.run()
        verdict("дверь: исключение без / — точный путь, не префикс",
                rc == 1 and "без записи мутаций: scripts/test_env.sh" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def sick_exception_without_reason():
    for label, value in (("пустая строка", "''"), ("null", "null"),
                         ("пробелы", "'   '")):
        yard = healthy_yard()
        try:
            yard.probe("scripts/test_env.sh")
            yard.data(HEALTHY + f"без_мутаций:\n  scripts/test_env.sh: {value}\n")
            rc, out = yard.run()
            verdict(f"больной: исключение без причины ({label}) — красный с именем",
                    rc == 1 and "исключение без причины: scripts/test_env.sh" in out,
                    f"rc={rc}, вывод: {out.strip()[-300:]}")
        finally:
            yard.close()


def sick_dead_exception():
    """An exception matching no probe file lies about what it guards."""
    yard = healthy_yard()
    try:
        yard.data(HEALTHY + "без_мутаций:\n"
                  "  scripts/test_gone.sh: 'был когда-то'\n")
        rc, out = yard.run()
        verdict("больной: исключение ни к чему не относится — красный",
                rc == 1
                and "исключение ни к чему не относится: scripts/test_gone.sh" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


# ── NO WORK MUST NOT LOOK LIKE SUCCESS ──────────────────────────────────────

def sick_empty_set():
    yard = Yard()
    try:
        yard.data("пробы: {}\n")
        rc, out = yard.run()
        verdict("больной: ни одной пробы в дереве — красный, счёт 0",
                rc == 1 and "судимых проб: 0" in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def sick_missing_carrier():
    yard = healthy_yard()
    try:
        gone = yard.dir / "нет-такого.yaml"
        rc, out = yard.run(HARNESS_PROBE_MUTATIONS=str(gone))
        verdict("больной: носителя мутаций нет — отказ с путём, не зелёный",
                rc not in (0, 127) and str(gone) in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def sick_tree_not_git():
    """An unreadable tree is a refusal, not an empty set read as zero."""
    yard = healthy_yard()
    try:
        plain = yard.dir / "plain"
        plain.mkdir()
        rc, out = yard.run(HARNESS_PROBE_TREE=str(plain))
        verdict("больной: дерево не читается git — отказ с путём",
                rc not in (0, 127) and str(plain) in out,
                f"rc={rc}, вывод: {out.strip()[-300:]}")
    finally:
        yard.close()


def main():
    before = md5_of(LIVE_DATA)
    print(f"проба ворот «проба без записи мутаций»: носитель {TOOL}")
    for fn in (sick_untracked_new_probe, sick_tracked_probe,
               sick_names_only_the_uncovered, sick_entry_without_mutation,
               sick_substring_is_not_a_word,
               healthy_all_covered, healthy_spelling_variants,
               healthy_invariant_word, healthy_not_a_probe,
               healthy_ignored_not_judged,
               door_exception_with_reason_file, door_exception_with_reason_dir,
               door_prefix_needs_slash, sick_exception_without_reason,
               sick_dead_exception, sick_empty_set, sick_missing_carrier,
               sick_tree_not_git):
        try:
            fn()
        except Exception as error:  # a case that crashed judged nothing
            verdict(fn.__name__, False, f"случай упал: {error!r}")
    verdict("боевые носители не тронуты", md5_of(LIVE_DATA) == before,
            "md5 probe-mutations.yaml / invariants.yaml изменился")
    total = results["good"] + results["bad"]
    print(f"ПРОБЫ-БЕЗ-МУТАЦИЙ: случаев {total}, неудач {results['bad']}")
    return 0 if results["bad"] == 0 and total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
