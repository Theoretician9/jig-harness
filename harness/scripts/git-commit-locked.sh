#!/usr/bin/env bash
# Коммит помощника под замком и со сверкой сообщения.
#
# Зачем. `git commit -F` пишет текст в общий <git-dir>/COMMIT_EDITMSG, гонит хук
# commit-msg и ПЕРЕЧИТЫВАЕТ файл. При личном индексе (GIT_INDEX_FILE) и при
# `-- <пути>` index.lock у каждого коммитера свой, и сосед переписывает файл в чужом
# окне: сообщение уходит не тому коммиту (гонка общего файла сообщения, 11.2026).
# Здесь коммиты идут по одному, а после коммита видно, то ли сообщение легло в
# историю, что подали.
#
# Вызов (из каталога репозитория или его подкаталога):
#   bash scripts/git-commit-locked.sh [--base <sha>] -F <файл> [-- <пути>]
#   bash scripts/git-commit-locked.sh -m <тема> [-m <тело>] [-- <пути>]
#   GIT_INDEX_FILE=<личный> bash scripts/git-commit-locked.sh -F - <<'EOF' … EOF
# Вызов Bash с timeout не меньше предела ожидания замка (HARNESS_COMMIT_LOCK_WAIT,
# 300 с): иначе очередь за долгим гейтом обернётся обрывом вызова.
#
# Коды: 0 — коммит и сверка в порядке; 2 — отказ ДО коммита (форма доводов);
# 5 — отказ под замком: индекс собран от ушедшего HEAD, коммит откатил бы чужие
# правки (--base <sha> — база индекса, без него HEAD до ожидания замка);
# 3 — расхождение: коммит сделан, сообщение в нём не то, что подано (история не
# трогается, только хеши и темы в stderr); 4 — замок занят дольше предела, коммита
# нет; иной код — код git commit как есть.
#
# Дверь мутаций: проба test_git_commit_locked.sh берёт обёртку из
# GIT_COMMIT_LOCKED_UNDER_TEST.
set -uo pipefail

refuse() { # $1 = форма, $2 = что делать
    printf 'git-commit-locked: отказ до коммита: %s — %s\n' "$1" "$2" >&2
    exit 2
}

args=()        # доводы git commit (с подставленным -F вместо -F -)
msg_file=""    # источник сообщения -F
msg_texts=()   # источник сообщения -m
tmp_msg=""

trap '[ -n "$tmp_msg" ] && rm -f "$tmp_msg"' EXIT

# Файл сообщения: `-` — stdin в свой временный файл (до замка).
take_file() { # $1 = значение -F
    [ -z "$msg_file" ] && [ "${#msg_texts[@]}" -eq 0 ] || refuse "второй источник сообщения" "нужен ровно один: -F или -m"
    if [ "$1" = "-" ]; then
        tmp_msg=$(mktemp -t harness-commit-msg.XXXXXX)
        cat > "$tmp_msg"
        msg_file=$tmp_msg
    else
        msg_file=$1
    fi
    args+=(-F "$msg_file")
}

take_text() { # $1 = значение -m
    [ -z "$msg_file" ] || refuse "второй источник сообщения" "нужен ровно один: -F или -m"
    msg_texts+=("$1")
    args+=(-m "$1")
}

check_cleanup() { # $1 = значение --cleanup
    case $1 in
        strip | default | whitespace) ;;
        *) refuse "--cleanup=$1" "очистка git отличалась бы от сверки; допустимы strip, default, whitespace" ;;
    esac
}

# Склеенные короткие флаги (-qF, -am, -sq): имя формы в отказ, если флаг опасен.
check_cluster() { # $1 = слово вида -xyz
    local rest=${1#-} ch
    case ${rest:0:1} in u | S) return ;; esac    # -u<режим>, -S<ключ>: значение приклеено
    while [ -n "$rest" ]; do
        ch=${rest:0:1}; rest=${rest:1}
        case $ch in
            F | m) refuse "$1" "разнеси флаги: -q -$ch" ;;
            s) refuse "$1" "-s (--signoff) дописывает трейлер: сверка солгала бы" ;;
            e) refuse "$1" "-e (--edit): помощник не интерактивен" ;;
            n) refuse "$1" "-n (--no-verify): ворота не обходятся" ;;
            t | c | C) refuse "$1" "сообщение — только -F или -m" ;;
        esac
    done
}

parse_args() {
    local a
    while [ $# -gt 0 ]; do
        a=$1; shift
        case $a in
            --) [ $# -gt 0 ] && paths=1; args+=("$a" "$@"); return ;;
            -i | --include) include=1; args+=("$a") ;;
            -F | --file)
                [ $# -gt 0 ] || refuse "$a" "нет значения"
                take_file "$1"; shift ;;
            --file=*) take_file "${a#--file=}" ;;
            -F?*) take_file "${a#-F}" ;;
            -m | --message)
                [ $# -gt 0 ] || refuse "$a" "нет значения"
                take_text "$1"; shift ;;
            --message=*) take_text "${a#--message=}" ;;
            -m?*) take_text "${a#-m}" ;;
            --cleanup)
                [ $# -gt 0 ] || refuse "$a" "нет значения"
                check_cleanup "$1"; args+=("$a" "$1"); shift ;;
            --cleanup=*) check_cleanup "${a#--cleanup=}"; args+=("$a") ;;
            -e | --edit) refuse "$a" "помощник не интерактивен: редактор повесил бы его" ;;
            -s | --signoff) refuse "$a" "дописывает трейлер: сверка солгала бы" ;;
            --trailer | --trailer=*) refuse "$a" "дописывает трейлер: сверка солгала бы" ;;
            -t | --template | --template=*) refuse "$a" "шаблон меняет сообщение: сверка солгала бы" ;;
            --fixup | --fixup=* | --squash | --squash=*) refuse "$a" "сообщение — только -F или -m" ;;
            -c | -C | -c?* | -C?* | --reuse-message | --reuse-message=* | --reedit-message | --reedit-message=*)
                refuse "$a" "сообщение — только -F или -m" ;;
            --amend) refuse "$a" "историю не правим" ;;
            -n | --no-verify) refuse "$a" "ворота не обходятся" ;;
            --author | --date | --pathspec-from-file)
                [ $# -gt 0 ] || refuse "$a" "нет значения"
                args+=("$a" "$1"); shift ;;
            --base)
                [ $# -gt 0 ] || refuse "$a" "нет значения"
                base=$1; shift ;;
            --base=*) base=${a#--base=} ;;
            --*) args+=("$a") ;;
            -?) args+=("$a") ;;
            -*) check_cluster "$a"; args+=("$a") ;;
            *) args+=("$a") ;;
        esac
    done
}

base=""; paths=0; include=0
parse_args "$@"
if [ -n "$base" ]; then
    base=$(git rev-parse -q --verify "$base^{commit}") || refuse "--base $base" "не коммит"
else
    base=$(git rev-parse -q --verify HEAD || true)
fi
[ -n "$msg_file" ] || [ "${#msg_texts[@]}" -gt 0 ] || refuse "нет -F и -m" "сообщение — только -F или -m"

lock_dir=$(git rev-parse --path-format=absolute --git-common-dir) || exit $?
lock="$lock_dir/harness-commit.lock"

# Повторный вход: замок держит родитель, ребёнок ждал бы до предела. Сравнение по
# пути своего замка: переменную наследуют все потомки коммита, в том числе обёртка
# в чужом репозитории, и она не должна отказывать ложно.
if [ "${HARNESS_COMMIT_LOCK_HELD:-}" = "$lock" ]; then
    refuse "обёртка вызвана изнутри коммита" "замок уже держит родитель; вложенный коммит не нужен"
fi
export HARNESS_COMMIT_LOCK_HELD=$lock

wait_s=${HARNESS_COMMIT_LOCK_WAIT:-300}
exec 9>> "$lock"
if ! flock -w "$wait_s" 9; then
    printf 'git-commit-locked: замок коммита занят %s с, коммита нет.\n' "$wait_s" >&2
    # Файл не очищается после снятия замка: в нём последний взявший, не обязательно держащий.
    [ -r "$lock.who" ] && printf 'последний взявший замок (мог уже отпустить): %s\n' "$(cat "$lock.who")" >&2
    exit 4
fi
printf '%s %s %s\n' "$$" "$(date '+%F %T')" "$PWD" > "$lock.who"
head_now=$(git rev-parse -q --verify HEAD || true)
if [ -n "$base" ] && [ "$head_now" != "$base" ] && { [ "$paths" = 0 ] || [ "$include" = 1 ]; }; then
    lost=$(comm -12 <(git diff --no-renames --name-only "$base" "$head_now" | sort) <(git diff --cached --no-renames --name-only "$head_now" | sort))
    if [ -n "$lost" ]; then
        printf 'git-commit-locked: отказ: индекс собран на %s, HEAD ушёл на %s; коммит откатил бы: %s — пересобери индекс от HEAD\n' "$base" "$head_now" "$(echo $lost)" >&2
        exit 5
    fi
fi

# Подано: сообщение после той же очистки, что у committed (см. ниже).
if [ -n "$msg_file" ]; then
    submitted=$(git stripspace --strip-comments < "$msg_file" 2>/dev/null)
else
    submitted=$(printf '%s\n\n' "${msg_texts[@]}" | git stripspace --strip-comments)
fi

before=$(git rev-parse -q --verify HEAD || true)

git commit "${args[@]}" 9>&-
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

# Кандидаты — всё, что легло после запомненного HEAD: мимо замка коммитят демон
# памяти и голый коммит в общий индекс, и один HEAD мог бы оказаться чужим.
if [ -n "$before" ]; then
    candidates=$(git rev-list "$before..HEAD")
else
    candidates=$(git rev-list HEAD)
fi

for c in $candidates; do
    committed=$(git log -1 --format=%B "$c" | git stripspace --strip-comments)
    [ "$committed" = "$submitted" ] && exit 0
done

{
    printf 'git-commit-locked: расхождение: в историю легло не то сообщение, что подано.\n'
    printf 'подано (тема): %s\n' "$(head -n 1 <<< "$submitted")"
    for c in $candidates; do
        printf 'коммит %s (тема): %s\n' "$c" "$(git log -1 --format=%s "$c")"
    done
    printf 'история не тронута; сообщи оркестратору.\n'
} >&2
exit 3
