"""Ключи конфига клона, которые уводят fetch и push в чужой репозиторий или исполняют чужой код.

Одна регулярка на всех, кто ходит в сеть по закреплённому адресу (сеть серверов, клиент runner):
копия разошлась бы с оригиналом на первом же новом ключе, и один из двух путей остался бы открыт.
include.* — потому что в подключённом файле лежит то же самое, а `git config --local` без
--includes его не видит.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

CLONE_REDIRECT = r"^(include\..*|includeif\..*|url\..*|remote\..*\.pushurl|core\.hookspath)$"


def redirects(repo: Path) -> list[str]:
    """Имена уводящих ключей локального конфига `repo` (с подключёнными файлами); пусто — чисто.

    Код git 1 значит «таких ключей нет»; любой другой — конфиг не прочитан, и пустой ответ
    прочёлся бы как «чисто».
    """
    done = subprocess.run(["git", "-C", str(repo), "config", "--local", "--includes", "--name-only",
                           "--get-regexp", CLONE_REDIRECT], capture_output=True, text=True)
    if done.returncode not in (0, 1):
        raise OSError(f"git config в {repo}: код {done.returncode}: {done.stderr.strip()[:200]}")
    return sorted(set(done.stdout.split()))
