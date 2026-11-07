#!/usr/bin/env python3
"""Предмет согласия на пуш: что именно уйдёт и куда, одной строкой.

Строку считают ворота (deploy_guard.py) и запрос кнопки (ask-consent.py) ОДНОЙ
функцией: нажатие «Да» привязано к этой строке, и если бы они считали по-разному,
кнопка либо не открывала бы дверь, либо открывала чужую.

    "<base>..<head> <адрес без учётки> refs/heads/<ветка> <репо>"

base — вершина ветки НА УДАЛЁННОМ (ls-remote), а не локальная ссылка
refs/remotes: ссылку сессия переставит, вершину на удалённом — нет. Хук ворот в
сессии сети не трогает и передаёт base=локальную ссылку: пуш не force, и git
отвергнет не-ff, так что уйдёт ровно одобренный head.
Отказ — PushSubjectError с причиной для человека; «кнопкой нельзя» не пропуск.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess

GIT_TIMEOUT_SEC = 5
HEX40 = re.compile(r"[0-9a-f]{40}")
USERINFO = re.compile(r"^((?:[A-Za-z][A-Za-z0-9+.-]*://)?)[^/\s]*@")
URL_USERINFO = re.compile(r"://[^/\s]*@")


class PushSubjectError(Exception):
    """Предмет пуша не посчитан; текст — причина, которую увидит человек."""


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    """git с урезанной средой. Висящий git (ssh к недоступному хосту) убивается
    вместе с детьми: иначе ssh держит каналы и ответ ждёт его, а не наш срок."""
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    proc = subprocess.Popen(["git", "-C", repo, *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=GIT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise PushSubjectError(f"git {args[0]} не ответил за {GIT_TIMEOUT_SEC} с") from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _out(repo: str, *args: str) -> str:
    done = _git(repo, *args)
    if done.returncode != 0:
        # stderr ls-remote может нести адрес с токеном: учётка срезается и тут
        raise PushSubjectError(f"git {args[0]}: {URL_USERINFO.sub('://', done.stderr.strip())[:160] or 'ошибка'}")
    return done.stdout.strip()


def hide_userinfo(address: str) -> str:
    """Адрес без учётных данных: токен в https-адресе — секрет (И-3)."""
    return USERINFO.sub(r"\1", address)


def _refuse_redirects(repo: str, remote: str) -> None:
    """pushurl, remote.<r>.push и insteadOf уводят пуш не туда, куда видит владелец."""
    for key in (f"remote.{remote}.pushurl", f"remote.{remote}.push"):
        if _git(repo, "config", "--get-all", key).stdout.strip():
            raise PushSubjectError("перенаправление пуша — не кнопкой "
                                   f"(задано {key})")
    rewrites = _git(repo, "config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$")
    if rewrites.stdout.strip():
        raise PushSubjectError("перенаправление пуша — не кнопкой (задан url.*.insteadOf)")


def _refuse_many_addresses(repo: str, remote: str) -> None:
    """git push уходит во все адреса удалённого, а предмет согласия видит один."""
    for key in (f"remote.{remote}.pushurl", f"remote.{remote}.url"):
        found = _git(repo, "config", "--get-all", key).stdout.split("\n")
        count = len([line for line in found if line.strip()])
        if count > 1:
            raise PushSubjectError(f"у удалённого {count} адресов — согласие кнопкой не применяется")
        if count:
            return


def _remote_tip(toplevel: str, address: str, remote: str, branch: str, base: str | None) -> str:
    """Вершина ветки на удалённом: base=None — по сети (ls-remote), иначе локальная ссылка base без сети."""
    ref = f"refs/heads/{branch}"
    if base is not None:
        tip = _git(toplevel, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        if tip.returncode != 0 or not HEX40.fullmatch(tip.stdout.strip()):
            raise PushSubjectError(f"локальной ссылки {base} нет — git fetch {remote}")
        return tip.stdout.strip()
    lines = _out(toplevel, "ls-remote", address, ref).splitlines()
    tips = [line.split("\t") for line in lines]
    if len(tips) != 1 or len(tips[0]) != 2 or tips[0][1] != ref or not HEX40.fullmatch(tips[0][0]):
        raise PushSubjectError(f"на удалённом {ref}: строк {len(lines)}, нужна одна "
                               "(новая ветка — не кнопкой)")
    return tips[0][0]


def push_subject(repo: str, remote: str, head: str, branch: str, base: str | None = None) -> str:
    if not HEX40.fullmatch(head):
        raise PushSubjectError(f"head не 40-значный хэш: {head}")
    if _git(repo, "check-ref-format", f"refs/heads/{branch}").returncode != 0:
        raise PushSubjectError(f"имя ветки негодное: {branch}")
    toplevel = os.path.realpath(_out(repo, "rev-parse", "--show-toplevel"))
    if _git(toplevel, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0:
        raise PushSubjectError(f"коммита {head} нет в {toplevel}")
    if remote.startswith("-") or _git(toplevel, "remote", "get-url", remote).returncode != 0:
        raise PushSubjectError(f"удалённого {remote} нет в {toplevel}")
    _refuse_many_addresses(toplevel, remote)
    _refuse_redirects(toplevel, remote)
    address = _out(toplevel, "remote", "get-url", "--push", remote)
    if not address or address.startswith("-") or re.search(r"\s", address):
        raise PushSubjectError("адрес удалённого пуст или с пробелами — не кнопкой")
    ref = f"refs/heads/{branch}"
    base = _remote_tip(toplevel, address, remote, branch, base)
    if _git(toplevel, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        raise PushSubjectError(f"вершины удалённого {base[:8]} нет локально — сделай fetch")
    if base == head:
        raise PushSubjectError("пустой диапазон: head уже на удалённом")
    ancestry = _git(toplevel, "merge-base", "--is-ancestor", base, head).returncode
    if ancestry == 1:
        raise PushSubjectError("head не потомок вершины удалённого (force — не кнопкой)")
    if ancestry != 0:
        raise PushSubjectError("merge-base не отработал")
    return f"{base}..{head} {hide_userinfo(address)} {ref} {toplevel}"


def parse_subject(subject: str) -> dict:
    """Строка предмета → {base, head, address, ref, repo}; негодная — PushSubjectError."""
    parts = subject.split(" ", 3)
    span = parts[0].split("..")
    if len(parts) != 4 or len(span) != 2 or not all(HEX40.fullmatch(h) for h in span):
        raise PushSubjectError(f"предмет пуша не разобран: {subject[:80]}")
    return {"base": span[0], "head": span[1], "address": parts[1], "ref": parts[2], "repo": parts[3]}
