#!/usr/bin/env bash
# Проба обёртки коммита git-commit-locked.sh: индекс, собранный от ушедшего HEAD.
#
# Откуда взята. b0d76f00 снял правку памяти e3f05b16, ddbda2c1 — строку STATE.md
# из e348b915: личный индекс собран `read-tree HEAD`, обёртка ждала замок, сосед
# закоммитил, и дерево коммита (весь индекс) вернуло его файлы к старой версии.
#
# Проба СОЗДАЁТ своё условие: временный каталог, свои репозитории, свой замок.
#   0  стенд: голый git с индексом от старого HEAD откатывает правку соседа
#      (иначе стенд слеп, и отказ в S ничего не доказывает);
#   S1 сосед коммитит, пока обёртка ждёт замок, без --base — отказ кодом 5;
#   S2 сосед коммитит до вызова, --base <старый HEAD> — отказ кодом 5;
#   C1 индекс от текущего HEAD — коммит идёт;
#   C2 индекс от старого HEAD, форма `-- <свой путь>` — коммит идёт, правка соседа цела;
#   C3 индекс от старого HEAD, но путь соседа в нём уже версии соседа — коммит идёт.
#
# Дверь мутаций: GIT_COMMIT_LOCKED_UNDER_TEST — путь к копии обёртки.
set -uo pipefail
export LC_ALL=C.UTF-8
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR HARNESS_COMMIT_LOCK_HELD

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="${GIT_COMMIT_LOCKED_UNDER_TEST:-$HERE/git-commit-locked.sh}"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
holder=""
trap '[ -n "$holder" ] && kill "$holder" 2>/dev/null; rm -rf "$STAND"' EXIT

# Репозиторий стенда: базовый коммит с a.txt и o.txt (o.txt — путь соседа).
new_repo() { # $1 = каталог
    mkdir -p "$1"
    git -C "$1" init -q .
    git -C "$1" config user.name t
    git -C "$1" config user.email t@t
    echo a-base > "$1/a.txt"
    echo o-base > "$1/o.txt"
    git -C "$1" add a.txt o.txt
    git -C "$1" commit -qm base --no-verify
}

# Личный индекс от текущего HEAD со своей правкой a.txt; печатает путь индекса.
own_index() { # $1 = репозиторий; $2… = доп. пути в индекс из рабочего дерева
    local repo=$1 idx="$1/.git/index-own"; shift
    echo a-own > "$repo/a.txt"
    GIT_INDEX_FILE=$idx git -C "$repo" read-tree HEAD
    GIT_INDEX_FILE=$idx git -C "$repo" add a.txt "$@"
    printf '%s\n' "$idx"
}

# Сосед коммитит o.txt мимо замка, в общий индекс.
neighbour_commit() { # $1 = репозиторий
    echo o-neighbour > "$1/o.txt"
    git -C "$1" commit -q --no-verify -m сосед -- o.txt
}

blob_of() { # $1 = репозиторий, $2 = путь в HEAD
    git -C "$1" show "HEAD:$2"
}

# Держит замок обёртки, пока не убит: обёртка встанет в ожидание.
hold_lock() { # $1 = репозиторий
    local lock
    lock="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)/harness-commit.lock"
    ( exec 8>> "$lock"; flock 8; exec sleep 120 ) &
    holder=$!
    for _ in $(seq 200); do flock -n "$lock" true || return 0; sleep 0.05; done
}

release_lock() {
    kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null; holder=""
}

# Обёртка с личным индексом фоном, за занятым замком; сосед коммитит, пока она ждёт.
# Ожидание видно по дочернему flock обёртки — без него сосед мог бы успеть раньше,
# чем обёртка прочла HEAD, и случай судил бы не то окно. Код обёртки — в $2.rc.
commit_while_waiting() { # $1 = репозиторий, $2 = префикс файлов вывода, $3 = индекс, $4… = доводы обёртки
    local repo=$1 out=$2 idx=$3 wpid
    shift 3
    hold_lock "$repo"
    (cd "$repo" && GIT_INDEX_FILE=$idx HARNESS_COMMIT_LOCK_WAIT=60 exec bash "$WRAPPER" "$@") \
        > "$out.out" 2>&1 &
    wpid=$!
    for _ in $(seq 400); do pgrep -P "$wpid" -x flock > /dev/null && break; sleep 0.05; done
    neighbour_commit "$repo"
    release_lock
    wait "$wpid"
    echo $? > "$out.rc"
}

printf 'Свой коммит\n' > "$STAND/msg"

echo "== 0: стенд видит откат голым git"
repo0="$STAND/r0"; new_repo "$repo0"
idx0=$(own_index "$repo0")
neighbour_commit "$repo0"
GIT_INDEX_FILE=$idx0 git -C "$repo0" commit -q --no-verify -F "$STAND/msg"
case_check "0 голый git с индексом от старого HEAD вернул o.txt к базе" "o-base" "$(blob_of "$repo0" o.txt)"

echo "== S1: сосед коммитит, пока обёртка ждёт замок"
repo1="$STAND/s1"; new_repo "$repo1"
idx1=$(own_index "$repo1")
commit_while_waiting "$repo1" "$STAND/s1" "$idx1" -q -F "$STAND/msg"
case_check "S1 отказ кодом 5" "5" "$(cat "$STAND/s1.rc")"
case_check "S1 коммита нет: HEAD — коммит соседа" "сосед" "$(git -C "$repo1" log -1 --format=%s)"
case_check "S1 правка соседа цела" "o-neighbour" "$(blob_of "$repo1" o.txt)"
case_check "S1 отказ называет путь o.txt" "да" "$(grep -q 'o\.txt' "$STAND/s1.out" && echo да || echo нет)"
case_check "S1 отказ велит пересобрать индекс" "да" "$(grep -q 'пересобери' "$STAND/s1.out" && echo да || echo нет)"

echo "== S2: сосед коммитит до вызова, --base <старый HEAD>"
repo2="$STAND/s2"; new_repo "$repo2"
base2=$(git -C "$repo2" rev-parse HEAD)
idx2=$(own_index "$repo2")
neighbour_commit "$repo2"
(cd "$repo2" && GIT_INDEX_FILE=$idx2 bash "$WRAPPER" --base "$base2" -q -F "$STAND/msg") > "$STAND/s2.out" 2>&1
case_check "S2 отказ кодом 5" "5" "$?"
case_check "S2 правка соседа цела" "o-neighbour" "$(blob_of "$repo2" o.txt)"
case_check "S2 отказ называет путь o.txt" "да" "$(grep -q 'o\.txt' "$STAND/s2.out" && echo да || echo нет)"

echo "== C1: индекс от текущего HEAD"
repoc1="$STAND/c1"; new_repo "$repoc1"
neighbour_commit "$repoc1"
idxc1=$(own_index "$repoc1")
(cd "$repoc1" && GIT_INDEX_FILE=$idxc1 bash "$WRAPPER" -q -F "$STAND/msg") > "$STAND/c1.out" 2>&1
case_check "C1 код 0" "0" "$?"
case_check "C1 своя правка в коммите" "a-own" "$(blob_of "$repoc1" a.txt)"
case_check "C1 правка соседа цела" "o-neighbour" "$(blob_of "$repoc1" o.txt)"

echo "== C2: индекс от старого HEAD, форма -- <свой путь>"
repoc2="$STAND/c2"; new_repo "$repoc2"
idxc2=$(own_index "$repoc2")
commit_while_waiting "$repoc2" "$STAND/c2" "$idxc2" -q -F "$STAND/msg" -- a.txt
case_check "C2 код 0" "0" "$(cat "$STAND/c2.rc")"
case_check "C2 своя правка в коммите" "a-own" "$(blob_of "$repoc2" a.txt)"
case_check "C2 правка соседа цела" "o-neighbour" "$(blob_of "$repoc2" o.txt)"

echo "== C3: индекс от старого HEAD, путь соседа в нём уже версии соседа"
repoc3="$STAND/c3"; new_repo "$repoc3"
echo o-neighbour > "$repoc3/o.txt"
idxc3=$(own_index "$repoc3" o.txt)
commit_while_waiting "$repoc3" "$STAND/c3" "$idxc3" -q -F "$STAND/msg"
case_check "C3 код 0" "0" "$(cat "$STAND/c3.rc")"
case_check "C3 своя правка в коммите" "a-own" "$(blob_of "$repoc3" a.txt)"
case_check "C3 правка соседа цела" "o-neighbour" "$(blob_of "$repoc3" o.txt)"

printf '\nИНДЕКС-ОТ-УШЕДШЕГО-HEAD: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
