#!/usr/bin/env bash
# post-commit: лечит общий индекс после коммита ЛИЧНЫМ индексом.
# Почему: помощники коммитят так: GIT_INDEX_FILE=<личный>; git read-tree HEAD;
# git add -- <пути>; git commit. Коммит ложится в HEAD, а общий .git/index
# хранит старые блобы тех же путей, и `git diff --cached` показывает откат
# коммита (случаи 12.11: staged-откат чужой работы при следующем коммите).
# Что делает: пути коммита HEAD в общем индексе сбрасываются к HEAD. Чужие
# staged-пути и рабочее дерево не трогаются. Обычный коммит (индекс общий) —
# выход молча.
# Установка: ln -sf ../../scripts/post-commit-heal-index.sh .git/hooks/post-commit
set -u

heal_shared_index() {
    local shared idx
    shared=$(readlink -f "$(git rev-parse --git-dir)/index")
    idx=${GIT_INDEX_FILE:-}
    [[ -n $idx ]] || return 0
    # git запускает хук в корне рабочего дерева и отдаёт путь индекса как есть
    # (обычный коммит — «.git/index»): readlink -f разрешает его от корня.
    [[ $(readlink -f "$idx") != "$shared" ]] || return 0

    local paths=() p
    while IFS= read -r -d '' p; do paths+=("$p"); done \
        < <(git diff-tree --no-commit-id --name-only -r -z --root HEAD)
    [[ ${#paths[@]} -gt 0 ]] || return 0
    # --literal-pathspecs: путь «a*» иначе стал бы шаблоном и сбросил чужие staged.
    env -u GIT_INDEX_FILE git --literal-pathspecs reset -q HEAD -- "${paths[@]}"
}

heal_shared_index || echo "post-commit: общий индекс не вылечен; руками: git reset -q HEAD -- <пути коммита>" >&2
exit 0
