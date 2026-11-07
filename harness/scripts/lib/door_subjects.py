#!/usr/bin/env python3
"""Предметы согласия на выкат, публикацию и одобрение спеки — одна строка на предмет.

Строку считают ворота (deploy_guard.py), publish.sh и запрос кнопки (ask-consent.py)
ОДНОЙ функцией из этого файла: нажатие «Да» привязано к строке, и если бы они
считали по-разному, кнопка либо не открывала бы дверь, либо открывала чужую.
Пуш считает свой push_subject.py; его git-вызовы (срок 5 с, без подмены объектов)
переиспользуются отсюда.

    выкат        "<sha HEAD> <realpath дерева>"
    публикация   "<sha HEAD пакета> <адрес без учётки> refs/heads/<ветка> <вершина на удалённом>
                  <вершина ветки сборки или -> <пакет> <сборка>"
    перепись     "<адрес без учётки> refs/heads/<ветка> <вершина удалённого до>..<новый sha>
                  <sha256 списка «sha ref» удалённого до, 12 знаков>"
    спека        "<id задачи> <sha256 файла спеки>"

Отказ — SubjectError с причиной для человека; «кнопкой нельзя» не пропуск: дверь
идёт дальше текстовым путём.

    python3 door_subjects.py грязь <дерево>
        причины, по которым дерево не равно коммиту (пусто — чисто); для deploy.sh
    python3 door_subjects.py сверить-публикацию <предмет замка 2> <пакет> <сборка> <удалённый> <ветка>
        0 — предмет тот же, коммит сборки стоит на вершине, одобренной владельцем
    python3 door_subjects.py сверить-перепись <предмет ворот> <копия> <пакет> <удалённый> <ветка>
        0 — удалённый и копия те же, что на кнопке: повторный force-push тем же «Да» не пройдёт
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import push_subject  # noqa: E402
from push_subject import HEX40, _git, _out  # noqa: E402

SubjectError = push_subject.PushSubjectError
PUBLISH_FIELDS = ("sha пакета", "адрес", "ветка", "вершина удалённого", "вершина сборки", "пакет", "сборка")
NO_TIP = "-"


def _whitespace_free(path: str, what: str) -> str:
    """Путь в строке предмета разделяется пробелом: путь с пробелом разобрать нельзя."""
    if re.search(r"\s", path):
        raise SubjectError(f"{what} с пробелом в пути — не кнопкой")
    return path


def tree_dirt(tree: str) -> list[str]:
    """Чем дерево отличается от своего HEAD: правки, неотслеженное, спрятанное флагами git.

    assume-unchanged и skip-worktree прячут правку и от status, и от diff, а сборка
    выката идёт из рабочего дерева: спрятанное уехало бы под sha, который видел владелец.
    """
    status = _out(tree, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    hidden = [f"скрыто флагом git: {line[2:]}" for line in _out(tree, "ls-files", "-v").splitlines()
              if line[:1].islower() or line[:1] == "S"]
    return [line for line in status.splitlines() if line.strip()] + hidden


def deploy_subject(tree: str) -> str:
    real = os.path.realpath(tree)
    _whitespace_free(real, "дерево")
    dirt = tree_dirt(real)
    if dirt:
        raise SubjectError(f"в {real} не закоммичено {len(dirt)}: {'; '.join(dirt[:3])} — "
                           "выкатывается только закоммиченное")
    sha = _out(real, "rev-parse", "--verify", "HEAD")
    if not HEX40.fullmatch(sha):
        raise SubjectError(f"HEAD дерева {real} не 40-значный хэш")
    return f"{sha} {real}"


def deploy_tree(install_conf: dict, harness_conf: dict) -> str:
    """Дерево выката: продукт, если он объявлен, иначе сам проект. Так же выбирает deploy.sh."""
    return (harness_conf.get("PRODUCT_REPO") or install_conf.get("PROJECT_DIR") or "").strip()


def parse_deploy_subject(subject: str) -> dict:
    sha, _, tree = subject.partition(" ")
    if not HEX40.fullmatch(sha) or not tree.startswith("/"):
        raise SubjectError(f"предмет выката не разобран: {subject[:80]}")
    return {"sha": sha, "tree": tree}


def publish_settings(harness_conf: dict) -> tuple[str, str, str]:
    """→ (каталог сборки, удалённый, ветка) из harness.conf; пустое — отказ, своих умолчаний нет."""
    settings = tuple((harness_conf.get(key) or "").strip()
                     for key in ("PUBLIC_BUILD_DIR", "PUBLIC_REMOTE", "PUBLIC_BRANCH"))
    if not all(settings):
        raise SubjectError("в harness.conf пусты PUBLIC_BUILD_DIR, PUBLIC_REMOTE или PUBLIC_BRANCH")
    return settings


def publish_subject(package: str, build: str, remote: str, branch: str) -> str:
    pack = _whitespace_free(os.path.realpath(package), "пакет")
    work = _whitespace_free(os.path.realpath(build), "каталог сборки")
    if _git(pack, "check-ref-format", f"refs/heads/{branch}").returncode != 0:
        raise SubjectError(f"имя ветки негодное: {branch}")
    if remote.startswith("-") or _git(pack, "remote", "get-url", remote).returncode != 0:
        raise SubjectError(f"у пакета нет удалённого {remote}")
    sha = _out(pack, "rev-parse", "--verify", "HEAD")
    # Пуш идёт из сборки, а адрес берётся у пакета: перенаправления опасны в обоих.
    repos = [pack] + ([work] if os.path.isdir(os.path.join(work, ".git")) else [])
    for repo in repos:
        push_subject._refuse_many_addresses(repo, remote)
        push_subject._refuse_redirects(repo, remote)
    address = _out(pack, "remote", "get-url", "--push", remote)
    if not address or address.startswith("-") or re.search(r"\s", address):
        raise SubjectError("адрес удалённого пуст или с пробелами — не кнопкой")
    ref = f"refs/heads/{branch}"
    lines = _out(pack, "ls-remote", address, ref).splitlines()
    tips = [line.split("\t") for line in lines]
    if len(tips) != 1 or len(tips[0]) != 2 or tips[0][1] != ref or not HEX40.fullmatch(tips[0][0]):
        raise SubjectError(f"на удалённом {ref}: строк {len(lines)}, нужна одна "
                           "(первая публикация — не кнопкой)")
    built = _git(work, "rev-parse", "--verify", "-q", ref) if len(repos) > 1 else None
    build_tip = built.stdout.strip() if built is not None and built.returncode == 0 else NO_TIP
    return " ".join([sha, push_subject.hide_userinfo(address), ref, tips[0][0], build_tip, pack, work])


def rewrite_address(package: str, remote: str) -> str:
    """Адрес публичного удалённого у пакета, как его пишет предмет переписи (без учётки)."""
    pack = _whitespace_free(os.path.realpath(package), "пакет")
    if remote.startswith("-") or _git(pack, "remote", "get-url", remote).returncode != 0:
        raise SubjectError(f"у пакета нет удалённого {remote}")
    push_subject._refuse_many_addresses(pack, remote)
    push_subject._refuse_redirects(pack, remote)
    address = _out(pack, "remote", "get-url", "--push", remote)
    if not address or address.startswith("-") or re.search(r"\s", address):
        raise SubjectError("адрес удалённого пуст или с пробелами — не кнопкой")
    return address


def remote_refs(address: str, repo: str) -> list[str]:
    """«sha ref» веток и тегов удалённого, отсортированно; «^{}» отброшено (так же считает publish.sh)."""
    lines = _out(repo, "ls-remote", address, "refs/heads/*", "refs/tags/*").splitlines()
    pairs = [line.split("\t") for line in lines]
    if not pairs or any(len(p) != 2 or not HEX40.fullmatch(p[0]) for p in pairs):
        raise SubjectError("ссылки публичного удалённого прочитать не удалось (или их нет)")
    return sorted(f"{sha} {ref}" for sha, ref in pairs if not ref.endswith("^{}"))


def rewrite_subject(copy: str, address: str, branch: str) -> str:
    """Предмет переписи: вершина удалённого ДО входит в строку, поэтому прежнее «Да» после
    переписи (вершина и ссылки другие) не открывает повторный force-push."""
    work = _whitespace_free(os.path.realpath(copy), "копия")
    ref = f"refs/heads/{branch}"
    if _git(work, "check-ref-format", ref).returncode != 0:
        raise SubjectError(f"имя ветки негодное: {branch}")
    new = _out(work, "rev-parse", "--verify", ref)
    refs = remote_refs(address, work)
    tips = [line.split(" ")[0] for line in refs if line.endswith(f" {ref}")]
    if len(tips) != 1:
        raise SubjectError(f"на удалённом {ref}: строк {len(tips)}, нужна одна")
    digest = hashlib.sha256(("\n".join(refs) + "\n").encode()).hexdigest()[:12]
    return f"{push_subject.hide_userinfo(address)} {ref} {tips[0]}..{new} {digest}"


def parse_rewrite_subject(subject: str) -> dict:
    parts = subject.split(" ")
    span = parts[2].split("..") if len(parts) == 4 else []
    if len(span) != 2 or not all(HEX40.fullmatch(h) for h in span) or not parts[1].startswith("refs/heads/") \
            or not re.fullmatch(r"[0-9a-f]{12}", parts[3]):
        raise SubjectError(f"предмет переписи не разобран: {subject[:80]}")
    return {"address": parts[0], "ref": parts[1], "tip": span[0], "new": span[1], "digest": parts[3]}


def verify_rewrite(locked: str, copy: str, package: str, remote: str, branch: str) -> None:
    """Прямо перед force-push: удалённый и копия те же, что были на кнопке."""
    now = rewrite_subject(copy, rewrite_address(package, remote), branch)
    if now != locked:
        raise SubjectError(f"после согласия изменилось: предмет переписи ({locked[-60:]} → {now[-60:]})")


def protected_roots(install: dict, harness: dict) -> tuple[list[str], list[str]]:
    """→ (корни защиты, корни над рабочим местом). Над рабочим — корень, внутри которого лежат
    проект, журналы, домашний каталог или /tmp: он отменял бы запись в журнал и правку проекта,
    поэтому из корней убран; след об этом пишет сторож сам."""
    raw = re.split(r"[\s,]+", harness.get("PROTECTED_PATHS", ""))
    raw += [install.get("SECRETS_DIR", ""), install.get("BACKUP_DIR", ""), harness.get("BACKUP_DIR", "")]
    roots = {os.path.realpath(os.path.expanduser(p)) for p in raw if p.startswith(("/", "~"))}
    working = [os.path.realpath(os.path.expanduser(p)) for p in
               (install.get("PROJECT_DIR", ""), install.get("LOG_DIR", ""), "~", "/tmp")
               if p.startswith(("/", "~"))]
    above = {r for r in roots - {"/"} if any(w == r or w.startswith(r.rstrip(os.sep) + os.sep) for w in working)}
    return sorted(roots - {"/"} - above), sorted(above)


def parse_publish_subject(subject: str) -> dict:
    parts = subject.split(" ")
    if len(parts) != len(PUBLISH_FIELDS) or not HEX40.fullmatch(parts[0]) or not HEX40.fullmatch(parts[3]) \
            or not (HEX40.fullmatch(parts[4]) or parts[4] == NO_TIP):
        raise SubjectError(f"предмет публикации не разобран: {subject[:80]}")
    return {"sha": parts[0], "address": parts[1], "ref": parts[2], "remote_tip": parts[3],
            "build_tip": parts[4], "package": parts[5], "build": parts[6]}


def verify_publish(locked: str, package: str, build: str, remote: str, branch: str) -> None:
    """Прямо перед пушем: предмет тот же, что открыл замок 2, а в сборке нет чужих коммитов.

    Вершина сборки к этому часу уже сдвинута своим же коммитом версии, поэтому она
    сверяется не строкой, а родством: у HEAD сборки родитель — одобренная вершина
    (или HEAD и есть она — повтор упавшей отправки).
    """
    was, now = parse_publish_subject(locked), parse_publish_subject(
        publish_subject(package, build, remote, branch))
    for key in was.keys() - {"build_tip"}:
        if was[key] != now[key]:
            raise SubjectError(f"после согласия изменилось: {key} ({was[key][:40]} → {now[key][:40]})")
    work = was["build"]
    head = _out(work, "rev-parse", "--verify", "HEAD")
    parent = _git(work, "rev-parse", "--verify", "-q", "HEAD^")
    parent_sha = parent.stdout.strip() if parent.returncode == 0 else NO_TIP
    if was["build_tip"] not in (head, parent_sha):
        raise SubjectError("в сборке есть коммиты, которых владелец не одобрял")


def spec_file(task_id: str, root=None) -> Path:
    """Файл спеки задачи. Карточку и путь ищет spec_approval (корни: харнес и
    PRODUCT_REPO) — импорт ленивый, ему самому этот модуль понадобится."""
    if not task_id or re.search(r"\s", task_id):
        raise SubjectError(f"id задачи негоден: {task_id!r}")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import spec_approval
    card = spec_approval.карточка_задачи(task_id, root)
    if card is None:
        raise SubjectError(f"нет такой задачи в карте: {task_id}")
    path = spec_approval.путь_спеки(card.get("spec") or "", root)
    if path is None:
        raise SubjectError(f"у задачи {task_id} нет файла спеки")
    return path


def spec_subject(task_id: str, root=None) -> str:
    """"<id> <sha256 файла спеки>": правка файла меняет предмет, и нажатие её не покрывает."""
    try:
        digest = hashlib.sha256(spec_file(task_id, root).read_bytes()).hexdigest()
    except OSError as err:
        raise SubjectError(f"спека {task_id} не прочитана: {type(err).__name__}") from None
    return f"{task_id} {digest}"


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "грязь":
            print("\n".join(tree_dirt(argv[2])))
        elif len(argv) == 7 and argv[1] == "сверить-публикацию":
            verify_publish(*argv[2:])
        elif len(argv) == 7 and argv[1] == "сверить-перепись":
            verify_rewrite(*argv[2:])
        else:
            print(__doc__)
            return 2
    except SubjectError as err:
        print(str(err))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
