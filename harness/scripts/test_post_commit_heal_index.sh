#!/usr/bin/env bash
# Проба хука post-commit-heal-index.sh: после коммита ЛИЧНЫМ индексом
# (GIT_INDEX_FILE=<свой>; read-tree HEAD; add; commit) общий .git/index по путям
# коммита догоняет HEAD, чужие staged-пути общего индекса не тронуты; обычный
# коммит хук не трогает и молчит.
# Условие своё: временные репозитории, свой HOME, хук ставится симлинком.
# Мутация: HOOK=<копия хука с мутацией>.
set -uo pipefail
unset GIT_INDEX_FILE GIT_DIR GIT_WORK_TREE
HOOK="${HOOK:-$(dirname "$(readlink -f "$0")")/post-commit-heal-index.sh}"
[ -f "$HOOK" ] && [ -x "$HOOK" ] || {
    echo "ПРОБА КРАСНАЯ: хука нет или он не исполняемый: $HOOK"; exit 1; }
HOOK=$(readlink -f "$HOOK")
T=$(mktemp -d /tmp/proba-heal-index-XXXXXX); trap 'rm -rf "$T"' EXIT
export HOME="$T/home" GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=p GIT_AUTHOR_EMAIL=p@p \
    GIT_COMMITTER_NAME=p GIT_COMMITTER_EMAIL=p@p
mkdir -p "$HOME"
CYR="данные/мой файл.md"   # кириллица и пробел: ловят разбор путей без -z

bad=0
check() { [ "$2" = "$3" ] || { echo "КРАСНО: $1: ждали «$2», вышло «$3»"; bad=$((bad+1)); }; }

new_repo() {  # имя → путь репозитория с поставленным хуком
    local r="$T/$1"
    git init -q "$r"   # quotepath по умолчанию: без -z кириллица придёт в кавычках
    ln -s "$HOOK" "$r/.git/hooks/post-commit"
    echo "$r"
}
# Коммит личным индексом: priv <репо> <сообщение> <команда-подготовки…>;
# вывод хука (stdout+stderr) — в $T/out.
priv() {
    local r=$1 msg=$2; shift 2
    ( cd "$r" && export GIT_INDEX_FILE="$r/.git/priv-index" && rm -f "$GIT_INDEX_FILE" &&
      { git rev-parse -q --verify HEAD >/dev/null && git read-tree HEAD || true; } &&
      "$@" && git commit -q -m "$msg" ) > "$T/out" 2>&1
}
cached() { git -C "$1" diff --cached --name-only -z | tr '\0' ' '; }       # общий индекс против HEAD
stage_of() { git -C "$1" ls-files -s -- "$2" | awk '{print $2}'; }       # блоб пути в общем индексе
head_of() { git -C "$1" rev-parse -q --verify "HEAD:$2" 2>/dev/null; }    # блоб пути в HEAD

# --- а) личный коммит меняет a.txt и кириллический путь ---
R=$(new_repo a); mkdir -p "$R/данные"
echo 1 > "$R/a.txt"; echo 1 > "$R/$CYR"; git -C "$R" add -A; git -C "$R" commit -q -m init
echo 2 > "$R/a.txt"; echo 2 > "$R/$CYR"
add_a() { git add -- a.txt "$CYR" && echo грязь > a.txt; }   # в дереве — незакоммиченная правка
priv "$R" "личный" add_a
check "а: коммит личным индексом прошёл" "2" "$(git -C "$R" show HEAD:a.txt)"
check "а: общий индекс пуст против HEAD" "" "$(cached "$R")"
check "а: блоб a.txt в общем индексе = HEAD" "$(head_of "$R" a.txt)" "$(stage_of "$R" a.txt)"
check "а: блоб $CYR в общем индексе = HEAD" "$(head_of "$R" "$CYR")" "$(stage_of "$R" "$CYR")"
check "а: личный коммит — хук молчит" "" "$(cat "$T/out")"
check "а: рабочее дерево не тронуто" "грязь" "$(cat "$R/a.txt")"
check "а: личный индекс цел" "$(head_of "$R" a.txt)" \
    "$(GIT_INDEX_FILE="$R/.git/priv-index" git -C "$R" ls-files -s -- a.txt | awk '{print $2}')"

# --- б) чужой staged-путь общего индекса не тронут ---
R=$(new_repo b)
echo 1 > "$R/a.txt"; echo 1 > "$R/other.txt"; git -C "$R" add -A; git -C "$R" commit -q -m init
echo чужое > "$R/other.txt"; git -C "$R" add other.txt
OTHER=$(stage_of "$R" other.txt)
echo 2 > "$R/a.txt"
priv "$R" "личный" git add -- a.txt
check "б: other.txt staged с тем же блобом" "$OTHER" "$(stage_of "$R" other.txt)"
check "б: в общем индексе staged только other.txt" "other.txt " "$(cached "$R")"
check "б: a.txt в общем индексе = HEAD" "$(head_of "$R" a.txt)" "$(stage_of "$R" a.txt)"

# --- в) удаление в личном коммите удаляет путь и в общем индексе ---
R=$(new_repo v)
echo 1 > "$R/keep.txt"; echo 1 > "$R/del.txt"; git -C "$R" add -A; git -C "$R" commit -q -m init
rm "$R/del.txt"
priv "$R" "удаление" git rm -q --cached -- del.txt
check "в: del.txt ушёл из HEAD" "" "$(head_of "$R" del.txt)"
check "в: del.txt ушёл из общего индекса" "" "$(stage_of "$R" del.txt)"
check "в: общий индекс пуст против HEAD" "" "$(cached "$R")"
check "в: keep.txt в общем индексе на месте" "$(head_of "$R" keep.txt)" "$(stage_of "$R" keep.txt)"

# --- г) первый коммит репозитория (нет родителя) личным индексом ---
R=$(new_repo g); mkdir -p "$R/данные"
echo 1 > "$R/a.txt"; echo 1 > "$R/$CYR"
priv "$R" "первый" git add -- a.txt "$CYR"
check "г: первый коммит есть" "1" "$(git -C "$R" rev-list --count HEAD 2>/dev/null)"
check "г: a.txt в общем индексе = HEAD" "$(head_of "$R" a.txt)" "$(stage_of "$R" a.txt)"
check "г: $CYR в общем индексе = HEAD" "$(head_of "$R" "$CYR")" "$(stage_of "$R" "$CYR")"
check "г: общий индекс пуст против HEAD" "" "$(cached "$R")"

# --- е) путь коммита с символами глоба не задевает чужие staged-пути ---
# Без --literal-pathspecs путь «a*» стал бы шаблоном и сбросил чужой ab.
R=$(new_repo e)
echo 1 > "$R/a*"; echo 1 > "$R/ab"; git -C "$R" add -A; git -C "$R" commit -q -m init
echo чужое > "$R/ab"; git -C "$R" add ab
AB=$(stage_of "$R" ab)
echo 2 > "$R/a*"
priv "$R" "глоб" git --literal-pathspecs add -- 'a*'
check "е: ab staged с тем же блобом" "$AB" "$(stage_of "$R" ab)"
check "е: в общем индексе staged только ab" "ab " "$(cached "$R")"
check "е: личный коммит — хук молчит" "" "$(cat "$T/out")"

# --- ж) связанный worktree, личный индекс задан относительно подкаталога ---
# Общий индекс worktree — .git/worktrees/<имя>/index; git отдаёт хуку
# GIT_INDEX_FILE=../wt-priv как есть, хотя хук стоит в корне.
R=$(new_repo zh); mkdir -p "$R/sub"
echo 1 > "$R/sub/a.txt"; git -C "$R" add -A; git -C "$R" commit -q -m init
W="$T/zh-wt"; git -C "$R" worktree add -q "$W" 2>/dev/null
echo 2 > "$W/sub/a.txt"
( cd "$W/sub" && export GIT_INDEX_FILE=../wt-priv && git read-tree HEAD &&
  git add -- a.txt && git commit -q -m личный ) > "$T/out" 2>&1
check "ж: коммит в worktree прошёл" "2" "$(git -C "$W" show HEAD:sub/a.txt)"
check "ж: общий индекс worktree пуст против HEAD" "" "$(cached "$W")"
check "ж: хук молчит" "" "$(cat "$T/out")"

# --- д) обычный коммит: хук молчит и общий индекс не трогает ---
R=$(new_repo d)
echo 1 > "$R/a.txt"; echo 1 > "$R/c.txt"; git -C "$R" add -A
git -C "$R" commit -q -m init > "$T/out" 2>&1
check "д: обычный коммит — хук молчит" "" "$(cat "$T/out")"
echo 2 > "$R/a.txt"; echo 2 > "$R/c.txt"; git -C "$R" add c.txt
C_BLOB=$(stage_of "$R" c.txt)
git -C "$R" commit -q -m частичный -- a.txt > "$T/out" 2>&1   # частичный коммит: c.txt остаётся staged
check "д: частичный коммит — хук молчит" "" "$(cat "$T/out")"
check "д: c.txt остался staged с тем же блобом" "$C_BLOB" "$(stage_of "$R" c.txt)"
check "д: staged только c.txt" "c.txt " "$(cached "$R")"
# Хук сам по себе на общем индексе под разными записями пути: индекс не переписан
# (тот же inode), вывода нет.
mkdir -p "$T/via"; ln -s "$R" "$T/via/r"
for idx in ".git/index" "$R/.git/index" "$T/via/r/.git/index"; do
    ino=$(stat -c %i "$R/.git/index")
    ( cd "$R" && GIT_INDEX_FILE="$idx" "$HOOK" ) > "$T/out" 2>&1
    check "д: хук на общем индексе ($idx) молчит" "" "$(cat "$T/out")"
    check "д: общий индекс не переписан ($idx)" "$ino" "$(stat -c %i "$R/.git/index")"
done

[ "$bad" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: 7 кейсов (а–ж) и наличие хука"; exit 0; }
echo "ПРОБА КРАСНАЯ: $bad"; exit 1
