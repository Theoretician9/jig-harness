#!/usr/bin/env bash
# Проба обёртки коммита git-commit-locked.sh: гонка общего .git/COMMIT_EDITMSG.
#
# Откуда взята. Коммит df0551c1 ушёл с чужим сообщением: `git commit -F` пишет
# текст в общий .git/COMMIT_EDITMSG, гонит хук commit-msg и ПЕРЕЧИТЫВАЕТ файл.
# При личном индексе (GIT_INDEX_FILE) и `-- <пути>` index.lock у каждого свой,
# и соседний помощник переписывает файл в чужом окне (разбор:
# разбор задачи — 135 подмен из 200 с хуком sleep 0.2).
#
# Проба СОЗДАЁТ своё условие: временный каталог, свои репозитории, свой хук
# commit-msg. Боевой репозиторий не трогается.
#   A  голый `git commit -F` двумя коммитерами — подмены ОБЯЗАНЫ быть (иначе
#      стенд слеп, и ноль подмен в B ничего не доказывает);
#   B  те же коммитеры через обёртку — 40 коммитов, 0 подмен;
#   C  хук переписывает файл сообщения — обёртка: код ≠ 0, «расхождение», хеш;
#   D  `-F -` через обёртку — сообщение из stdin дошло как есть;
#   E  комментарии и хвостовые пробелы — штатная очистка git, не расхождение;
#   F  нечего коммитить — код git пробрасывается, его текст не проглочен;
#   G  -m: сверка идёт и для него; H  формы, где сверка солгала бы, — отказ до коммита;
#   I  повторный вход по пути своего замка; J  личный индекс без путей, -F -;
#   K  чужой коммит мимо замка после нашего — не расхождение;
#   L  замок занят дольше предела или не открывается — коммита нет.
#
# Дверь мутаций: GIT_COMMIT_LOCKED_UNDER_TEST — путь к копии обёртки.
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="${GIT_COMMIT_LOCKED_UNDER_TEST:-$HERE/git-commit-locked.sh}"
source "$HERE/lib/selftest_cases.sh"

N=20                 # коммитов на коммитера; судим ровно 2*N
MAX_TRIES=60         # потолок повторов одного коммита: сломанная обёртка не вешает пробу

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

# Репозиторий стенда с базовым коммитом и хуком commit-msg из $2 (тело sh).
new_repo() { # $1 = каталог, $2 = тело хука commit-msg
    mkdir -p "$1"
    git -C "$1" init -q .
    git -C "$1" config user.name t
    git -C "$1" config user.email t@t
    echo base > "$1/base.txt"
    git -C "$1" add base.txt
    git -C "$1" commit -qm base --no-verify
    printf '#!/bin/sh\n%s\n' "$2" > "$1/.git/hooks/commit-msg"
    chmod +x "$1/.git/hooks/commit-msg"
}

# Коммитер: N коммитов своего пути с личным индексом. $3 — чем коммитить.
# Повтор — только при провале блокировки (сосед сдвинул HEAD); иной отказ — коммит
# пропущен, суд увидит нехватку до 2*N.
committer() { # $1 = репозиторий, $2 = номер коммитера, $3… = команда коммита
    local repo=$1 w=$2; shift 2
    local idx="$repo/.git/index-$w" msg="$STAND/msg-$(basename "$repo")-$w" err i tries
    err="$msg.err"
    cd "$repo" || return
    for ((i = 1; i <= N; i++)); do
        printf 'W%s-%s сообщение коммитера %s\n\nDev-Map: hr.w%s\n' "$w" "$i" "$w" "$w" > "$msg"
        echo "$w $i" > "f-$w-$i.txt"
        tries=0
        until GIT_INDEX_FILE=$idx git read-tree HEAD \
              && GIT_INDEX_FILE=$idx git add -- "f-$w-$i.txt" \
              && GIT_INDEX_FILE=$idx "$@" -q -F "$msg" -- "f-$w-$i.txt" >/dev/null 2>"$err"; do
            tries=$((tries + 1))
            # Слово «lock» само по себе не годится: оно есть в имени обёртки, и
            # отсутствующая обёртка крутила бы повторы до потолка.
            grep -qE "cannot lock ref|\.lock'" "$err" && [ "$tries" -lt "$MAX_TRIES" ] || break
            sleep "0.0$((RANDOM % 9))"
        done
    done
}

# Суд стенда: печатает «коммитов подмен»; подмена — тема не того файла.
judge() { # $1 = репозиторий
    local repo=$1 total=0 bad=0 c subj files want
    for c in $(git -C "$repo" rev-list HEAD --not "$(git -C "$repo" rev-list --max-parents=0 HEAD)"); do
        total=$((total + 1))
        subj=$(git -C "$repo" log -1 --format=%s "$c")
        files=$(git -C "$repo" diff-tree --no-commit-id --name-only -r "$c" | tr '\n' ' ')
        want=$(sed -E 's/^W([0-9]+)-([0-9]+) .*/f-\1-\2.txt /' <<< "$subj")
        [ "$files" = "$want" ] || bad=$((bad + 1))
    done
    echo "$total $bad"
}

race() { # $1 = каталог стенда, $2… = команда коммита
    local repo=$1; shift
    new_repo "$repo" 'sleep 0.2'
    committer "$repo" 1 "$@" &
    committer "$repo" 2 "$@" &
    wait
    judge "$repo"
}

echo "== A: голый git commit -F, два коммитера с личным индексом"
read -r total_a bad_a <<< "$(race "$STAND/a" git commit)"
echo "голый git commit -F: подмен $bad_a из $total_a"
case_check "A стенд видит гонку: голый git дал подмены ≥ 1" "да" \
    "$([ "${bad_a:-0}" -ge 1 ] && echo да || echo "нет ($bad_a из $total_a)")"

echo "== B: те же коммитеры через обёртку ($WRAPPER)"
read -r total_b bad_b <<< "$(race "$STAND/b" bash "$WRAPPER")"
echo "подмен через обёртку: $bad_b из $total_b"
case_check "B коммитов через обёртку ровно $((2 * N))" "$((2 * N))" "$total_b"
case_check "B подмен через обёртку" "0" "$bad_b"

echo "== C: хук commit-msg переписывает файл сообщения"
repo_c="$STAND/c"
new_repo "$repo_c" 'printf "ЧУЖОЕ сообщение\n" > "$1"'
echo c > "$repo_c/c.txt"; git -C "$repo_c" add c.txt
printf 'Своё сообщение C\n' > "$STAND/msg-c"
# Контроль стенда: голый git молча коммитит чужой текст.
(cd "$repo_c" && git commit -q -F "$STAND/msg-c" -- c.txt >/dev/null 2>&1)
case_check "C стенд: голый git молча коммитит переписанное" "ЧУЖОЕ сообщение" \
    "$(git -C "$repo_c" log -1 --format=%s)"
echo c2 > "$repo_c/c.txt"; git -C "$repo_c" add c.txt
head_before=$(git -C "$repo_c" rev-parse HEAD)
(cd "$repo_c" && bash "$WRAPPER" -q -F "$STAND/msg-c" -- c.txt) >"$STAND/c.out" 2>"$STAND/c.err"
rc_c=$?
head_c=$(git -C "$repo_c" rev-parse HEAD)
short_c=$(git -C "$repo_c" rev-parse --short=7 HEAD)
case_check "C обёртка выходит с кодом ≠ 0" "да" "$([ "$rc_c" -ne 0 ] && echo да || echo "нет (rc=$rc_c)")"
case_check "C коммит сделан (HEAD сдвинут)" "да" "$([ "$head_c" != "$head_before" ] && echo да || echo нет)"
case_check "C в stderr слово «расхождение»" "да" "$(grep -q 'расхождение' "$STAND/c.err" && echo да || echo нет)"
case_check "C в stderr хеш коммита" "да" "$([ "$head_c" != "$head_before" ] && grep -q "$short_c" "$STAND/c.err" && echo да || echo нет)"
case_check "C история не тронута: HEAD — тот же коммит с чужим текстом" "ЧУЖОЕ сообщение" \
    "$(git -C "$repo_c" log -1 --format=%s)"

echo "== D: -F - (сообщение из stdin) через обёртку"
repo_d="$STAND/d"
new_repo "$repo_d" 'sleep 0.2'
echo d > "$repo_d/d.txt"; git -C "$repo_d" add d.txt
msg_d=$'Тема D из stdin\n\nтело D\n\nDev-Map: hr.d'
(cd "$repo_d" && printf '%s\n' "$msg_d" | bash "$WRAPPER" -q -F - -- d.txt) >"$STAND/d.out" 2>"$STAND/d.err"
rc_d=$?
case_check "D обёртка с -F - выходит 0" "0" "$rc_d"
case_check "D сообщение коммита — то, что подано в stdin" "$(printf '%s\n' "$msg_d" | git stripspace)" \
    "$(git -C "$repo_d" log -1 --format=%B | git stripspace)"

echo "== E: комментарии и хвостовые пробелы — не расхождение"
repo_e="$STAND/e"
new_repo "$repo_e" 'true'
echo e > "$repo_e/e.txt"; git -C "$repo_e" add e.txt
printf 'Тема E   \n\n# комментарий\nтело E  \n\n\n' > "$STAND/msg-e"
(cd "$repo_e" && bash "$WRAPPER" -q -F "$STAND/msg-e" -- e.txt) >"$STAND/e.out" 2>"$STAND/e.err"
rc_e=$?
case_check "E штатная очистка: обёртка выходит 0" "0" "$rc_e"
case_check "E штатная очистка: слова «расхождение» нет" "нет" \
    "$(grep -q 'расхождение' "$STAND/e.err" && echo да || echo нет)"
case_check "E коммит сделан с темой E" "Тема E" "$(git -C "$repo_e" log -1 --format=%s)"

echo "== F: нечего коммитить — код git пробрасывается"
repo_f="$STAND/f"
new_repo "$repo_f" 'true'
printf 'Тема F\n' > "$STAND/msg-f"
(cd "$repo_f" && git commit -F "$STAND/msg-f") >/dev/null 2>&1
rc_bare_f=$?
(cd "$repo_f" && bash "$WRAPPER" -F "$STAND/msg-f") >"$STAND/f.out" 2>&1
rc_f=$?
case_check "F код обёртки = код голого git ($rc_bare_f)" "$rc_bare_f" "$rc_f"
case_check "F текст git не проглочен (nothing to commit)" "да" \
    "$(grep -q 'nothing to commit' "$STAND/f.out" && echo да || echo нет)"
case_check "F слова «расхождение» нет (сверки без коммита нет)" "нет" \
    "$(grep -q 'расхождение' "$STAND/f.out" && echo да || echo нет)"

echo "== G: -m через обёртку — сверка идёт и для -m"
repo_g="$STAND/g"
new_repo "$repo_g" 'printf "ЧУЖОЕ сообщение\n" > "$1"'
echo g > "$repo_g/g.txt"; git -C "$repo_g" add g.txt
head_before=$(git -C "$repo_g" rev-parse HEAD)
(cd "$repo_g" && bash "$WRAPPER" -q -m 'Своё сообщение G' -m '# не подмена' -- g.txt) >"$STAND/g.out" 2>"$STAND/g.err"
rc_g=$?
case_check "G -m: обёртка выходит с кодом ≠ 0 при подмене" "да" "$([ "$rc_g" -ne 0 ] && echo да || echo "нет (rc=$rc_g)")"
case_check "G -m: в stderr слово «расхождение»" "да" "$(grep -q 'расхождение' "$STAND/g.err" && echo да || echo нет)"
case_check "G -m: коммит сделан (HEAD сдвинут)" "да" "$([ "$(git -C "$repo_g" rev-parse HEAD)" != "$head_before" ] && echo да || echo нет)"
repo_g2="$STAND/g2"
new_repo "$repo_g2" 'sleep 0.1'
echo g2 > "$repo_g2/g2.txt"; git -C "$repo_g2" add g2.txt
(cd "$repo_g2" && bash "$WRAPPER" -q -m 'Тема G2' -m '# комментарий' -m 'тело G2' -- g2.txt) >"$STAND/g2.out" 2>"$STAND/g2.err"
case_check "G -m без подмены: обёртка выходит 0" "0" "$?"
case_check "G -m без подмены: коммит с темой G2" "Тема G2" "$(git -C "$repo_g2" log -1 --format=%s)"

echo "== H: формы, при которых сверка солгала бы, — отказ до коммита"
repo_h="$STAND/h"
new_repo "$repo_h" 'true'
echo h > "$repo_h/h.txt"; git -C "$repo_h" add h.txt
head_before=$(git -C "$repo_h" rev-parse HEAD)
printf 'Тема H\n' > "$STAND/msg-h"
for form in "--cleanup=verbatim" "--signoff" "-s" "--no-verify" "-n" "-e" "--amend" "-qF$STAND/msg-h"; do
    (cd "$repo_h" && bash "$WRAPPER" -F "$STAND/msg-h" $form -- h.txt) >"$STAND/h.out" 2>&1
    case_check "H $form: отказ с кодом 2" "2" "$?"
    case_check "H $form: коммита нет (HEAD тот же)" "да" \
        "$([ "$(git -C "$repo_h" rev-parse HEAD)" = "$head_before" ] && echo да || echo нет)"
done
(cd "$repo_h" && bash "$WRAPPER" -q -- h.txt) >"$STAND/h.out" 2>&1
case_check "H без -F и -m: отказ с кодом 2" "2" "$?"
(cd "$repo_h" && bash "$WRAPPER" -q --cleanup=whitespace -F "$STAND/msg-h" -- h.txt) >"$STAND/h.out" 2>&1
case_check "H --cleanup=whitespace допустим: код 0" "0" "$?"

echo "== I: повторный вход — по пути СВОЕГО замка"
repo_i="$STAND/i"
new_repo "$repo_i" 'true'
echo i > "$repo_i/i.txt"; git -C "$repo_i" add i.txt
head_before=$(git -C "$repo_i" rev-parse HEAD)
lock_i="$(git -C "$repo_i" rev-parse --path-format=absolute --git-common-dir)/harness-commit.lock"
printf 'Тема I\n' > "$STAND/msg-i"
(cd "$repo_i" && HARNESS_COMMIT_LOCK_HELD="$lock_i" bash "$WRAPPER" -q -F "$STAND/msg-i" -- i.txt) >"$STAND/i.out" 2>&1
case_check "I свой замок в переменной: отказ с кодом 2" "2" "$?"
case_check "I свой замок: текст «изнутри коммита»" "да" "$(grep -q 'изнутри коммита' "$STAND/i.out" && echo да || echo нет)"
case_check "I свой замок: коммита нет" "да" "$([ "$(git -C "$repo_i" rev-parse HEAD)" = "$head_before" ] && echo да || echo нет)"
(cd "$repo_i" && HARNESS_COMMIT_LOCK_HELD="/нет/чужой/harness-commit.lock" bash "$WRAPPER" -q -F "$STAND/msg-i" -- i.txt) >"$STAND/i.out" 2>&1
case_check "I чужой замок в переменной: коммит идёт, код 0" "0" "$?"
case_check "I чужой замок: коммит с темой I" "Тема I" "$(git -C "$repo_i" log -1 --format=%s)"

echo "== J: личный индекс без путей, -F - (способ оркестратора)"
repo_j="$STAND/j"
new_repo "$repo_j" 'sleep 0.1'
idx_j="$repo_j/.git/index-j"
echo j > "$repo_j/j.txt"
GIT_INDEX_FILE=$idx_j git -C "$repo_j" read-tree HEAD
GIT_INDEX_FILE=$idx_j git -C "$repo_j" add -- j.txt
msg_j=$'Тема J из stdin\n\nтело J\n\nDev-Map: hr.j'
(cd "$repo_j" && printf '%s\n' "$msg_j" | GIT_INDEX_FILE=$idx_j bash "$WRAPPER" -q -F -) >"$STAND/j.out" 2>"$STAND/j.err"
case_check "J личный индекс без путей, -F -: код 0" "0" "$?"
case_check "J сообщение — из stdin" "$(printf '%s\n' "$msg_j" | git stripspace)" \
    "$(git -C "$repo_j" log -1 --format=%B | git stripspace)"
case_check "J в коммите файл j.txt" "j.txt" "$(git -C "$repo_j" show --name-only --format= HEAD)"

echo "== K: мимо замка лёг чужой коммит после нашего — не расхождение"
repo_k="$STAND/k"
new_repo "$repo_k" 'true'
# post-commit: один голый коммит в общий индекс (метка не даёт зациклиться).
cat > "$repo_k/.git/hooks/post-commit" <<'HOOK'
#!/bin/sh
[ -e .git/k-done ] && exit 0
: > .git/k-done
unset GIT_INDEX_FILE
git commit -q --allow-empty -m 'Чужой коммит мимо замка' --no-verify
HOOK
chmod +x "$repo_k/.git/hooks/post-commit"
echo k > "$repo_k/k.txt"; git -C "$repo_k" add k.txt
printf 'Тема K\n' > "$STAND/msg-k"
(cd "$repo_k" && bash "$WRAPPER" -q -F "$STAND/msg-k" -- k.txt) >"$STAND/k.out" 2>"$STAND/k.err"
case_check "K чужой коммит после нашего: обёртка выходит 0" "0" "$?"
case_check "K стенд: HEAD — чужой коммит" "Чужой коммит мимо замка" "$(git -C "$repo_k" log -1 --format=%s)"
case_check "K слова «расхождение» нет" "нет" "$(grep -q 'расхождение' "$STAND/k.err" && echo да || echo нет)"

echo "== L: замок не взят — коммита нет"
repo_l="$STAND/l"
new_repo "$repo_l" 'true'
echo l > "$repo_l/l.txt"; git -C "$repo_l" add l.txt
head_before=$(git -C "$repo_l" rev-parse HEAD)
lock_l="$(git -C "$repo_l" rev-parse --path-format=absolute --git-common-dir)/harness-commit.lock"
printf 'Тема L\n' > "$STAND/msg-l"
# Держатель — сам sleep (exec): его смерть снимает замок, сирот с дескриптором нет.
( exec 8>> "$lock_l"; flock 8; exec sleep 60 ) &
holder=$!
for _ in $(seq 100); do flock -n "$lock_l" true || break; sleep 0.05; done
(cd "$repo_l" && HARNESS_COMMIT_LOCK_WAIT=1 bash "$WRAPPER" -q -F "$STAND/msg-l" -- l.txt) >"$STAND/l.out" 2>&1
rc_l=$?
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
case_check "L замок занят дольше предела: код 4" "4" "$rc_l"
case_check "L замок занят: текст «занят»" "да" "$(grep -q 'занят' "$STAND/l.out" && echo да || echo нет)"
case_check "L замок занят: коммита нет (HEAD тот же)" "да" \
    "$([ "$(git -C "$repo_l" rev-parse HEAD)" = "$head_before" ] && echo да || echo нет)"
# Файл замка не открыть (на его месте каталог): flock не взят — коммита тоже нет.
rm -f "$lock_l"; mkdir "$lock_l"
(cd "$repo_l" && HARNESS_COMMIT_LOCK_WAIT=1 bash "$WRAPPER" -q -F "$STAND/msg-l" -- l.txt) >"$STAND/l.out" 2>&1
case_check "L замок не открыть: код ≠ 0" "да" "$([ "$?" -ne 0 ] && echo да || echo нет)"
case_check "L замок не открыть: коммита нет (HEAD тот же)" "да" \
    "$([ "$(git -C "$repo_l" rev-parse HEAD)" = "$head_before" ] && echo да || echo нет)"

printf '\nКОММИТ-ОБЁРТКА: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
