#!/usr/bin/env python3
"""Неполная (shallow) история пакета раскрывается до разбора обновления.

Откуда взято. Копия bkb 13.11.2026: пакет — клон `--depth 1`. Затравка
раскладки (lib/layout_history.py) судит «наш ли файл» по ИСТОРИИ пакета, а
сверка «есть новое / свои коммиты» — по merge-base; на неполной истории первая
слепа к старым версиям, вторая ломается на переписанной публичной ветке.
Решение (спека, раздел «История
пакета и своё копии»): update.py, спрашивая обновление, при
`git rev-parse --is-shallow-repository` = true делает `git fetch --unshallow`
до разбора; не вышло — называет это в ответе, а не молчит.

Случаи:
  U1 клон --depth 1, в публичной ветке новый коммит → после спросить_обновления()
     пакет не shallow и в нём вся история публичной ветки (счёт коммитов равен);
     либо, если раскрыть не удалось, ответ называет неполную историю;
  U2 «есть новое» по-прежнему верно (True) на том же стенде;
  U3 пара: полный клон — ответ без беды (`--unshallow` на полном клоне git
     отвергает — звать его без проверки нельзя).

Стенд — свой mkdtemp: «публичный» репозиторий и клоны по file://. Паспорт и
конфиг — пустые файлы стенда; живые пакет, /etc и журналы не трогаются.
Итог: «НЕПОЛНАЯ ИСТОРИЯ: путей N, неудач M», код 0/1.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPDATE = HERE / "update.py"

paths = failures = 0


def check(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + text + ("" if ok else f": {detail}"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=proba",
                           "-c", "user.email=proba@stand", "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, rel: str, text: str, message: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def load_update(root: Path):
    conf = root / "empty.conf"
    conf.write_text("", encoding="utf-8")
    os.environ.update({"HARNESS_INSTALL_CONF": str(conf), "HARNESS_CONF": str(conf),
                       "HARNESS_PUBLIC_BRANCH": "master", "HARNESS_PUBLIC_REMOTE": "origin",
                       "HARNESS_VERSION_FILE": str(root / "version"), "LOG_DIR": str(root / "logs")})
    spec = importlib.util.spec_from_file_location("update_stand", UPDATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="proba-shallow-history-"))
    try:
        public = root / "public"
        public.mkdir()
        git(public, "init", "-q")
        git(public, "symbolic-ref", "HEAD", "refs/heads/master")
        for number in range(1, 6):
            commit(public, "harness/demons/a.sh", f"echo {number}\n", f"версия {number}")
        url = f"file://{public}"
        shallow = root / "package-shallow"
        full = root / "package-full"
        subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(shallow)], check=True)
        subprocess.run(["git", "clone", "-q", url, str(full)], check=True)
        commit(public, "harness/demons/a.sh", "echo 6\n", "версия 6")
        want = git(public, "rev-list", "--count", "master")
        check(git(shallow, "rev-parse", "--is-shallow-repository") == "true",
              "стенд: клон --depth 1 неполный")

        update = load_update(root)

        os.environ["HARNESS_STARTER"] = str(shallow)
        answer = update.спросить_обновления()
        now_shallow = git(shallow, "rev-parse", "--is-shallow-repository")
        have = git(shallow, "rev-list", "--count", "FETCH_HEAD") if now_shallow == "false" else "?"
        trouble = str(answer.get("беда") or "")
        named = "неполн" in trouble or "shallow" in trouble
        check((now_shallow == "false" and have == want) or named,
              "U1 после спросить_обновления история раскрыта (или неполнота названа)",
              f"shallow={now_shallow}, коммитов {have} из {want}, ответ {answer!r}")
        check(answer.get("есть новое") is True, "U2 «есть новое» на раскрытом пакете", f"ответ {answer!r}")

        os.environ["HARNESS_STARTER"] = str(full)
        answer = update.спросить_обновления()
        check(not answer.get("беда") and answer.get("есть новое") is True,
              "U3 полный клон — без беды, «есть новое»", f"ответ {answer!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"НЕПОЛНАЯ ИСТОРИЯ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
