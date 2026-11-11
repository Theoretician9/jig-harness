#!/usr/bin/env bash
# self-seal — печать эталона личности и стандартов (слой 1) в /etc/harness/self.
#
# Зовётся root через sudo NOPASSWD без доводов от агента, кроме каталога выкладки:
# любой лишний довод или недосверенная выкладка — это запись root куда угодно.
# Поэтому порядок такой: довод и дерево судятся ДО записи; файлы копируются во
# временный каталог РЯДОМ с эталоном и сверяются по sha256 уже там (агент мог
# подменить файл выкладки между проверкой и копией); и только целый, сверенный
# набор заменяет эталон. Отказ на любом шаге оставляет эталон и выкладку как были.
#
#   self-seal.sh <абсолютный каталог выкладки>
# Окружение: SELF_ETALON_DIR (по умолчанию /etc/harness/self) — дверь пробы;
# под sudo окружение сброшено, и эталон лежит в /etc/harness/self.
set -euo pipefail
# Явный PATH: под NOPASSWD нельзя доверять путям вызывающего.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
export LC_ALL=C
umask 022

ETALON="${SELF_ETALON_DIR:-/etc/harness/self}"
STAGE=""
OLD=""

refuse() { printf 'self-seal: отказ: %s\n' "$*" >&2; exit 1; }
cleanup() {
    [ -z "$STAGE" ] || rm -rf -- "$STAGE"
    [ -z "$OLD" ] || rm -rf -- "$OLD"
}
trap cleanup EXIT

[ "$#" -eq 1 ] || refuse "нужен ровно один довод — абсолютный каталог выкладки"
RELEASE="$1"
case "$RELEASE" in /*) ;; *) refuse "довод не абсолютный: $RELEASE" ;; esac
case "/$RELEASE/" in */../*) refuse "в доводе «..»: $RELEASE" ;; esac
# Равенство своему realpath убирает и симлинки, и «.», и «//», и хвостовой «/».
[ -d "$RELEASE" ] || refuse "довод не каталог: $RELEASE"
[ "$(realpath -e -- "$RELEASE")" = "$RELEASE" ] || refuse "довод не канонический путь (симлинк или лишние части): $RELEASE"

SELF="$RELEASE/harness/self"
MANIFEST="$SELF/MANIFEST.sha256"
[ "$(realpath -e -- "$SELF" 2>/dev/null)" = "$SELF" ] || refuse "нет канонического каталога $SELF"
[ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || refuse "нет манифеста $MANIFEST"

# Манифест: строка формата sha256sum, путь от harness/self/ без «..» и без корня.
HASHES=()
PATHS=()
while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^([0-9a-f]{64})\ \ (.+)$ ]] || refuse "строка манифеста негодна: $line"
    hash="${BASH_REMATCH[1]}"
    rel="${BASH_REMATCH[2]}"
    case "$rel" in
        /*|*//*|./*|*/.|*/./*|..|../*|*/..|*/../*) refuse "путь в манифесте негоден: $rel" ;;
        instance.yaml|MANIFEST.sha256) refuse "в манифест не входит $rel" ;;
    esac
    HASHES+=("$hash")
    PATHS+=("$rel")
done < "$MANIFEST"
[ "${#PATHS[@]}" -gt 0 ] || refuse "манифест пуст"

# Лишнее в self/ — отказ; instance.yaml — слой 2, он в эталон не идёт и не лишний.
LISTED="$(printf '%s\n' "${PATHS[@]}" | sort)"
FOUND="$(cd "$SELF" && find . -mindepth 1 ! -type d ! -path ./MANIFEST.sha256 ! -path ./instance.yaml -printf '%P\n' | sort)"
[ "$LISTED" = "$FOUND" ] || refuse "набор файлов в $SELF разошёлся с манифестом"

for rel in "${PATHS[@]}"; do
    [ -f "$SELF/$rel" ] && [ ! -L "$SELF/$rel" ] || refuse "не обычный файл: $rel"
    [ "$(realpath -e -- "$SELF/$rel")" = "$SELF/$rel" ] || refuse "путь идёт через симлинк: $rel"
done

PARENT="$(dirname -- "$ETALON")"
mkdir -p -- "$PARENT"
STAGE="$(mktemp -d "$PARENT/.self-seal.XXXXXX")"
OLD="$STAGE.old"

for i in "${!PATHS[@]}"; do
    rel="${PATHS[$i]}"
    mkdir -p -- "$STAGE/$(dirname -- "$rel")"
    # install создаёт НОВЫЙ файл сразу с правами 0644: цель симлинком (подмена после
    # проверки выше) не получает ни записи, ни chmod вне STAGE.
    install -m 0644 -T -- "$SELF/$rel" "$STAGE/$rel"
    # Сверка идёт по ТОМУ, что скопировано: именно оно ляжет в эталон.
    [ -f "$STAGE/$rel" ] && [ ! -L "$STAGE/$rel" ] || refuse "файл подменён при копии: $rel"
    actual="$(sha256sum < "$STAGE/$rel")"
    [ "${actual%% *}" = "${HASHES[$i]}" ] || refuse "sha256 не сошёлся: $rel"
done
chmod 0755 -- "$STAGE"

# Две замены подряд: между ними эталона нет доли секунды, но полу-эталона нет никогда.
if [ -e "$ETALON" ]; then
    mv -T -- "$ETALON" "$OLD"
fi
if ! mv -T -- "$STAGE" "$ETALON"; then
    [ ! -e "$OLD" ] || mv -T -- "$OLD" "$ETALON"
    refuse "замена эталона не удалась"
fi
STAGE=""
