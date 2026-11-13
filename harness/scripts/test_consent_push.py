#!/usr/bin/env python3
"""test_consent_push.py — проба 12 задачи hr.consent-buttons-for-every-yes: ворота судят пуш кнопкой.

Владелец 05.11.2026: «все вопросы по поводу моего Да должны приходить кнопками в телегу».
Ворота `deploy_guard.py --действие push --пуш <удалённый> <sha>:refs/heads/<ветка> <репо>`
считают предмет пуша сами (base — локальная refs/remotes/<удалённый>/<ветка> БЕЗ сети, head
из команды, адрес, ветка, репо) и пропускают, только если владелец нажал «Да» на ЭТОТ
предмет; текст владельца не открывает ничего (hr.consent-buttons-doors §2, §4). Пуш не force:
если удалённый ушёл вперёд, git отвергнет не-ff, а новая кнопка нужна после git fetch.

Стенд свой: временный репо с голым удалённым (file://), свой LOG_DIR, свои
harness.conf (PUSH_NEEDS_OWNER=1, DEPLOY_CONSENT_STANDING=0) и install.conf
(AUTONOMY=semi). Судье они приходят НОСИТЕЛЕМ: раннер пробы подменяет owner_word.носитель
в процессе ворот (окружение судья не слушает). Записи consent-buttons.jsonl кладёт сама
проба — это журнал стенда, не боевой. git идёт через обёртку-счётчик в PATH: она
считает ls-remote и по флагу стенда виснет на нём.
Ворота зовутся процессом, но «две смены» и занятость машины сняты подменой в этом
процессе: это состояние машины, а не предмет пробы, и от него зависел бы ответ.

    python3 scripts/test_consent_push.py
Код возврата: 0 — «падений 0», 1 — красная.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS / "lib"))
import config  # noqa: E402
from selftest_cases import Cases  # noqa: E402

OWNER = 777000
TTL_MIN = 15
REAL_GIT = shutil.which("git") or "/usr/bin/git"
# Глобальный и системный конфиг git машины пробе чужие: url.*.insteadOf в
# ~/.gitconfig агента сделал бы любой предмет «перенаправлением».
GIT_ENV = {"GIT_AUTHOR_NAME": "proba", "GIT_AUTHOR_EMAIL": "proba@stand.invalid",
           "GIT_COMMITTER_NAME": "proba", "GIT_COMMITTER_EMAIL": "proba@stand.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
YES, NO = "consent.yes", "consent.no"

cases = Cases()
check = cases.check


def now_iso(ago_s: float = 0) -> str:
    when = dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)
    return when.isoformat(timespec="seconds")


def git(*args: str, cwd: Path) -> str:
    """Настоящий git мимо обёртки стенда: подготовка условия, а не предмет пробы."""
    done = subprocess.run([REAL_GIT, *args], cwd=cwd, capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV), timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


class GitBench:
    """Репо с голым удалённым по file://: на удалённом база, поверх неё свои коммиты."""

    def __init__(self, root: Path, ahead: int = 3) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.remote = root / "remote.git"
        self.work = root / "work"
        self.url = f"file://{self.remote}"
        git("init", "-q", "--bare", "-b", "master", str(self.remote), cwd=root)
        git("clone", "-q", self.url, str(self.work), cwd=root)
        git("checkout", "-q", "-B", "master", cwd=self.work)
        self.commit("база")
        git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=self.work)
        self.base = self.head()
        for number in range(1, ahead + 1):
            self.commit(f"коммит {number}")

    def commit(self, message: str) -> str:
        git("commit", "-q", "--allow-empty", "-m", message, cwd=self.work)
        return self.head()

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.work)

    def repo(self) -> str:
        return os.path.realpath(self.work)

    def subject(self, head: str | None = None, url: str | None = None) -> str:
        """Предмет по договору §5 — посчитан пробой независимо от механизма."""
        return f"{self.base}..{head or self.head()} {url or self.url} refs/heads/master {self.repo()}"

    def other_clone(self, name: str) -> Path:
        path = self.root / name
        git("clone", "-q", self.url, str(path), cwd=self.root)
        return path


def envelope(event: str, ts: str, data: dict) -> str:
    return json.dumps({"v": 1, "ts": ts, "src": "scripts/lib/consent_buttons.py", "event": event,
                       "outcome": "ok", "data": data}, ensure_ascii=False)


def request(log: Path, kind: str, target: str, ago_s: float, nonce: str = "") -> None:
    """Просьба кнопки, как её кладёт ask-consent (вид у неё есть всегда)."""
    asked = {"nonce": nonce or secrets.token_urlsafe(16), "вид": kind, "цель": target, "почему": "проба"}
    with open(log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"v": 1, "ts": now_iso(ago_s), "src": "scripts/ask-consent.py",
                             "event": "consent.request", "outcome": "ok", "data": asked},
                            ensure_ascii=False) + "\n")


def button(log: Path, event: str, kind: str | None, target: str, ago_s: float) -> None:
    """Решение владельца в журнале кнопок стенда; kind=None — запись старой формы без вида.

    Живой порядок: просьба ask-consent на минуту раньше нажатия.
    """
    data = {"nonce": secrets.token_urlsafe(16), "цель": target, "message_id": 5000 + int(ago_s),
            "from_id": OWNER}
    request(log, kind or "удаление", target, ago_s + 60, data["nonce"])
    if kind is not None:
        data["вид"] = kind
    with open(log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
        fh.write(envelope(event, now_iso(ago_s), data) + "\n")


GIT_WRAPPER = """#!/usr/bin/env bash
# Обёртка стенда: считает ls-remote и по флагу виснет на нём; прочее — настоящему git.
for a in "$@"; do
    if [ "$a" = ls-remote ]; then
        echo 1 >> "$PROBA_PUSH_DIR/ls-remote.calls"
        [ -e "$PROBA_PUSH_DIR/hang-ls-remote" ] && exec sleep 30
        break
    fi
done
exec {git} "$@"
"""

# Ворота процессом; «две смены» и занятость — состояние машины, на стенде их нет. Носитель
# стенда — подменой owner_word.носитель в процессе ворот: дверь зовёт судью без носителя.
RUNNER = """import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import deploy_guard as g
spec = json.loads(sys.argv[2])
carrier = g.слово_вл.Носитель(папка=pathlib.Path(spec["папка"]), uid=None, паспорт=spec["паспорт"],
                              граница=pathlib.Path(spec["граница"]), окно_мин=spec["окно_мин"],
                              срок_кнопки_мин=spec["срок_кнопки_мин"], ручки=spec["ручки"])
g.слово_вл.носитель = lambda: carrier
g.две_смены = lambda: None
g.лок_файлы = lambda: []
g.долгие_процессы = lambda: []
sys.argv = ["deploy_guard.py", *sys.argv[3:]]
sys.exit(g.main())
"""


class PushStand:
    """Ворота на своём LOG_DIR и своих конфигах, репо — GitBench."""

    def __init__(self, root: Path, ahead: int = 3) -> None:
        self.dir = root
        self.bench = GitBench(root / "git", ahead)
        self.log = root / "log"
        bin_dir = root / "bin"
        for d in (self.log, bin_dir, root / "project"):
            d.mkdir(parents=True, exist_ok=True)
        # Журнал входящих на машине есть всегда: без него отзыв прочитать нечем — это отказ.
        (self.log / "inbox.jsonl").write_text("", encoding="utf-8")
        (bin_dir / "git").write_text(GIT_WRAPPER.format(git=REAL_GIT), encoding="utf-8")
        (bin_dir / "git").chmod(0o755)
        (root / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nAUTONOMY="semi"\nPROJECT_DIR="{root}/project"\nTG_CHAT_ID="{OWNER}"\n',
            encoding="utf-8")
        (root / "harness.conf").write_text(
            f"PUSH_NEEDS_OWNER=1\nDEPLOY_CONSENT_STANDING=0\nCONFIRM_MAX_AGE_MIN=60\n"
            f"CONSENT_BUTTON_TTL_MIN={TTL_MIN}\nSERVICE_LOCKS_SUBDIR=services\n", encoding="utf-8")
        # Окружение — для своих журналов и ключей ВОРОТ; судье стенд даёт носитель.
        self.env = dict(os.environ, **GIT_ENV, HARNESS_HOME=str(root),
                        HARNESS_CONF=str(root / "harness.conf"), LOG_DIR=str(self.log),
                        HARNESS_LOG_DIR=str(self.log), PROBA_PUSH_DIR=str(root),
                        PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
        self.carrier = json.dumps({"папка": str(self.log), "граница": str(root), "окно_мин": 60,
                                   "срок_кнопки_мин": TTL_MIN,
                                   "паспорт": config.из_файла(str(root / "install.conf")),
                                   "ручки": config.из_файла(str(root / "harness.conf"))}, ensure_ascii=False)

    def gate(self, refspec: str | None = None, remote: str = "origin") -> tuple[int | None, str, float]:
        """(rc, вывод, секунды) ворот на команду `git push <remote> <refspec>`."""
        refspec = refspec or f"{self.bench.head()}:refs/heads/master"
        start = time.time()
        try:
            done = subprocess.run(
                ["python3", "-c", RUNNER, str(SCRIPTS), self.carrier, "--действие", "push",
                 "--пуш", remote, refspec, self.bench.repo()],
                cwd=self.bench.work, capture_output=True, text=True, env=self.env, timeout=60)
        except subprocess.TimeoutExpired:
            return None, "ворота висят дольше 60 с", time.time() - start
        return done.returncode, (done.stdout + done.stderr).strip(), time.time() - start

    def yes(self, ago_s: float = 60, kind: str | None = "пуш", target: str | None = None) -> None:
        button(self.log, YES, kind, target or self.bench.subject(), ago_s)

    def no(self, ago_s: float = 60) -> None:
        button(self.log, NO, "пуш", self.bench.subject(), ago_s)

    def text(self, words: str, ago_s: float) -> None:
        """Слово владельца текстом — так его кладёт диспетчер в inbox.jsonl (с message_id)."""
        with open(self.log / "inbox.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(ago_s), "message_id": str(7000 + int(ago_s)),
                                 "text": words}, ensure_ascii=False) + "\n")

    def ls_remote_calls(self) -> int:
        try:
            return len((self.dir / "ls-remote.calls").read_text().splitlines())
        except OSError:
            return 0


def short(out: str) -> str:
    return out.replace("\n", " | ")[:300]


def probe_push(root: Path) -> None:
    print("— 12. ворота --действие push --пуш судят кнопку")
    counter = iter(range(1, 100))

    def stand(ahead: int = 3) -> PushStand:
        return PushStand(root / f"s{next(counter)}", ahead)

    s = stand()
    rc, out, _ = s.gate()
    check(f"12а) нажатия нет → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes()
    rc, out, _ = s.gate()
    check(f"12б) «Да» на тот же предмет → rc 0 [{short(out)}]", 0, rc)
    check("12б) в выводе «согласие кнопкой»", True, "согласие кнопкой" in out)
    check("12б) кнопка не пишет согласие.json", False, (s.log / "согласие.json").exists())

    s = stand()
    s.yes()
    s.bench.commit("соседний коммит после «Да»")
    rc, out, _ = s.gate()
    check(f"12в) новый коммит, команда с новым sha → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes()
    other = s.bench.other_clone("other")
    git("commit", "-q", "--allow-empty", "-m", "чужой коммит", cwd=other)
    git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=other)
    rc, out, _ = s.gate()
    check(f"12г) удалённый сдвинут другим клоном, fetch не делали → rc 0: предмет по локальной ссылке, "
          f"сеть не трогали, git отвергнет не-ff [{short(out)}]", 0, rc)
    git("fetch", "-q", "origin", cwd=s.bench.work)
    rc, out, _ = s.gate()
    check(f"12г2) после git fetch вершина удалённого другая → rc 1, отказ советует git fetch/новую кнопку "
          f"[{short(out)}]", (1, True), (rc, "git fetch" in out))

    s = stand()
    s.yes(ago_s=120)
    s.no(ago_s=60)
    rc, out, _ = s.gate()
    check(f"12д) «Да», затем «Нет» вторым запросом → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes(ago_s=(TTL_MIN + 5) * 60)
    rc, out, _ = s.gate()
    check(f"12е) «Да» старше {TTL_MIN} мин → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes()
    git("push", "-q", "origin", f"{s.bench.head()}:refs/heads/master", cwd=s.bench.work)
    rc, out, _ = s.gate()
    check(f"12ж) после настоящего пуша тот же «Да» → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    # Просьба на пуш этого предмета была: отказ обязан дать сверка вида «Да», а не суд парной просьбы.
    request(s.log, "пуш", s.bench.subject(), 120)
    s.yes(kind="удаление")
    rc, out, _ = s.gate()
    check(f"12з) «Да» вида «удаление» с той же строкой цели → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes()
    with open(s.log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{битая строка\n")
    rc, out, _ = s.gate()
    check(f"12и) битый consent-buttons.jsonl → rc 1 [{short(out)}]", 1, rc)
    check("12и) причина названа: «журнал кнопок не прочитан» и имя журнала", (True, True),
          ("журнал кнопок не прочитан" in out, "consent-buttons" in out))

    s = stand()
    s.yes()
    git("config", "remote.origin.pushurl", s.bench.url, cwd=s.bench.work)
    rc, out, _ = s.gate()
    check(f"12л) pushurl задан после «Да» (тот же адрес) → rc 1 [{short(out)}]", 1, rc)
    check("12л) причина — «перенаправление»", True, "перенаправлен" in out)

    # Адрес входит в предмет: тот же base на другом удалённом — другой предмет.
    s = stand()
    s.yes()
    twin = s.bench.root / "twin.git"
    git("clone", "-q", "--bare", s.bench.url, str(twin), cwd=s.bench.root)
    git("remote", "set-url", "origin", f"file://{twin}", cwd=s.bench.work)
    rc, out, _ = s.gate()
    check(f"12л2) адрес удалённого сменён после «Да» (set-url, base тот же) → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes()
    git("config", "url.file:///proba-nowhere/.pushInsteadOf", "proba-zzz:", cwd=s.bench.work)
    rc, out, _ = s.gate()
    check(f"12м) url.*.pushInsteadOf после «Да» → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes()
    git("config", "remote.origin.push", "refs/heads/master:refs/heads/master", cwd=s.bench.work)
    rc, out, _ = s.gate()
    check(f"12н) remote.origin.push после «Да» → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.yes(ago_s=120)
    s.text("стоп, не пушь", ago_s=60)
    rc, out, _ = s.gate()
    check(f"12о) «Да» кнопкой, затем текстом «стоп, не пушь» → rc 1 [{short(out)}]", 1, rc)

    s = stand()
    s.text("да, пуш", ago_s=120)
    s.no(ago_s=60)
    rc, out, _ = s.gate()
    check(f"12п) текстовое «да» на push, затем кнопка «Нет» на предмет → rc 1 [{short(out)}]", 1, rc)
    s = stand()
    s.text("да, запушь на гит", ago_s=60)
    rc, out, _ = s.gate()
    check(f"12п2) только текстовое «да» → rc 1, отказ называет кнопку [{short(out)}]", (1, True),
          (rc, "кнопк" in out))

    s = stand()
    s.no(ago_s=120)
    s.text("да, пуш", ago_s=60)
    rc, out, _ = s.gate()
    check(f"12р) кнопка «Нет», затем текстовое «да» → rc 1: текст не открывает [{short(out)}]", 1, rc)

    # Ворота в хуке — без сети: ls-remote не зовётся вовсе, и висящий удалённый им не помеха.
    s = stand()
    s.yes()
    (s.dir / "hang-ls-remote").write_text("", encoding="utf-8")
    rc, out, took = s.gate()
    check(f"12с) ls-remote «висит», но не зовётся: «Да» → rc 0 [{short(out)}]", 0, rc)
    check(f"12с) ворота кончают ≤ 12 с (вышло {took:.1f} с)", True, took <= 12)
    check("12с) ls-remote вызван 0 раз", 0, s.ls_remote_calls())

    s = stand()
    (s.dir / "hang-ls-remote").write_text("", encoding="utf-8")
    s.text("да, пуш", ago_s=60)
    rc, out, took = s.gate()
    check(f"12с2) текстовое «да» без кнопки → rc 1 [{short(out)}]", 1, rc)
    check("12с2) ls-remote вызван 0 раз", 0, s.ls_remote_calls())
    check(f"12с2) ≤ 12 с (вышло {took:.1f} с)", True, took <= 12)

    s = stand()
    other = s.bench.other_clone("other")
    git("commit", "-q", "--allow-empty", "-m", "ветка-тёзка", cwd=other)
    git("push", "-q", "origin", "HEAD:refs/heads/feature/master", cwd=other)
    git("push", "-q", "origin", "HEAD:refs/tags/master", cwd=other)
    s.yes()
    rc, out, _ = s.gate()
    check(f"12т) на удалённом refs/heads/feature/master и тег master → «Да» → rc 0 [{short(out)}]", 0, rc)

    s = stand()
    s.yes()
    rc, out, _ = s.gate(refspec="master:refs/heads/master")
    check(f"12у) refspec master:refs/heads/master → rc 1 [{short(out)}]", 1, rc)
    check("12у) в выводе «кнопки нет»", True, "кнопки нет" in out)

    import push_subject as ps
    saved = dict(os.environ)
    os.environ.update(GIT_ENV)       # ~/.gitconfig агента не должен решать за стенд
    try:
        s = stand()
        bench = s.bench
        subject = ps.push_subject(bench.repo(), "origin", bench.head(), "master")
        check("12ф) контроль: один адрес — предмет как по договору", bench.subject(), subject)
        second = bench.root / "second.git"
        git("init", "-q", "--bare", "-b", "master", str(second), cwd=bench.root)
        git("config", "--add", "remote.origin.url", f"file://{second}", cwd=bench.work)
        try:
            refused = f"предмет {ps.push_subject(bench.repo(), 'origin', bench.head(), 'master')}"
        except ps.PushSubjectError as err:
            refused = str(err)
        check(f"12ф) два remote.origin.url → PushSubjectError «у удалённого 2 адресов» [{refused[:120]}]",
              True, refused.startswith("у удалённого 2 адресов"))

        s = stand()
        bench = s.bench
        orphan = git("commit-tree", "HEAD^{tree}", "-m", "сирота", cwd=bench.work)
        git("replace", "--graft", orphan, bench.base, cwd=bench.work)
        try:
            refused = f"предмет {ps.push_subject(bench.repo(), 'origin', orphan, 'master')}"
        except ps.PushSubjectError as err:
            refused = str(err)
        check(f"12х) git replace пришил сироту к base → всё равно «не потомок» [{refused[:120]}]",
              True, refused.startswith("head не потомок"))

        # Сетевой путь (base не передан) — им считает предмет ask-consent: вершина берётся
        # у удалённого, а не у локальной refs/remotes, которую сессия не обновляла.
        s = stand()
        bench = s.bench
        other = bench.other_clone("other")
        git("commit", "-q", "--allow-empty", "-m", "чужой коммит", cwd=other)
        git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=other)
        try:
            refused = f"предмет {ps.push_subject(bench.repo(), 'origin', bench.head(), 'master')}"
        except ps.PushSubjectError as err:
            refused = str(err)
        check(f"12г3) сеть: удалённый сдвинут другим клоном, fetch не было → «вершины удалённого … нет "
              f"локально» [{refused[:120]}]", True, refused.startswith("вершины удалённого"))

        s = stand()
        bench = s.bench
        other = bench.other_clone("other")
        git("commit", "-q", "--allow-empty", "-m", "ветка-тёзка", cwd=other)
        git("push", "-q", "origin", "HEAD:refs/heads/feature/master", cwd=other)
        git("push", "-q", "origin", "HEAD:refs/tags/master", cwd=other)
        try:
            subject = ps.push_subject(bench.repo(), "origin", bench.head(), "master")
        except ps.PushSubjectError as err:
            subject = str(err)
        check("12т2) сеть: refs/heads/feature/master и тег master на удалённом — предмет по одной "
              "refs/heads/master", bench.subject(), subject)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="proba-consent-push-"))
    atexit.register(shutil.rmtree, root, True)
    try:
        probe_push(root)
    except Exception as err:          # фикстура упала — проба красная, а не молчит
        check("12) фикстура стенда отработала", "без исключения", repr(err)[:300])
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
