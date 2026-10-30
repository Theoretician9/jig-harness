#!/usr/bin/env bash
#
# commit-msg-gate.sh — стадия commit-msg: суд хакатона по трейлеру «Dev-Map: <id>».
#
# Зачем отдельный хук: на стадии pre-commit сообщение коммита ещё не написано
# (.git/COMMIT_EDITMSG там несёт сообщение ПРЕДЫДУЩЕГО коммита), а карточка,
# которую коммит задевает без правки в карте, называется только трейлером.
# Git передаёт файл сообщения первым аргументом.
# Установка: ln -sf ../../scripts/commit-msg-gate.sh .git/hooks/commit-msg
# Ненулевой код возврата отменяет коммит.
set -euo pipefail

# Путь по РЕАЛЬНОМУ файлу: хук подключён симлинком из .git/hooks.
exec python3 "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/check-pipeline.py" --commit-msg "$1"
