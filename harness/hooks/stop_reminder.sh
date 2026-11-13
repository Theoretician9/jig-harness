#!/usr/bin/env bash
# Взят из UNIFIED/templates/hooks/stop_reminder.sh; добавлено: конфиг /etc/harness/install.conf,
# протухшая передача (через scripts/handover-check.sh — единый источник, В-5),
# очередь dev-map.yaml; проверка трейлера Dev-Map убрана — её замещает очередь манифеста.
#
# Хук Stop: напомнить о хвостах, которые регулярно забываются.
# Молчит, когда всё в порядке. Найденные хвосты уходят в stderr с exit 2 (В-1):
# stdout Stop-хука до модели НЕ доходит, а exit 2 блокирует завершение и
# показывает stderr модели — напоминание, которое некому прочитать, не работает.
#
# set без -e — осознанно (fail-open): Stop-хук не имеет права ронять завершение
# сессии из-за отказа одной проверки; недоступное просто пропускаем молча.
set -uo pipefail

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib/config.sh"
konf_dveri_puti "${BASH_SOURCE[0]}"
conf_path="$INSTALL_CONF"
project_dir=""
if [ -r "$conf_path" ]; then
  # Окружение старше конфига: общий загрузчик вместо голого source (улика
  # 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
  konf_zagruzit_dveri PROJECT_DIR LOG_DIR DAEMON_OWNED_PATHS DEVMAP_TRACE_FRESH_MIN
  project_dir="${PROJECT_DIR:-}"
fi

# Метка «ход агента закончен, он ждёт ввода» — для session-warden.
#
# Улика 13.08.2026 (владелец видел это в терминале): ротация решала, что агент
# отработал регламент, по молчанию tmux-панели — 288с без вывода. Но панель
# молчит и пока идёт долгий инструмент (`make smoke` — 5 мин 40 с): вывода нет,
# работа есть. Сторож ввёл «/exit» посреди хода и оборвал незавершённую работу.
#
# Молчание панели признаком быть не может в принципе — оно не отличает «жду
# ввода» от «занят». Признак есть точный: сам факт завершения хода, а это и
# есть событие Stop. Пишем его время; сторож сверяет метку с моментом, когда
# отдал поручение о ротации.
#
# Пишем ТОЛЬКО там, где ход действительно заканчивается (выход с кодом 0). При
# exit 2 хук блокирует завершение и агент продолжает работу — метка там солгала
# бы ровно тем же, чем лгало молчание панели.
отметить_конец_хода() {
  [ -n "${LOG_DIR:-}" ] || return 0
  date +%s > "$LOG_DIR/turn-end.stamp" 2>/dev/null || true
}

# stop_hook_active=true в JSON на stdin — модель УЖЕ продолжила работу после
# нашей блокировки. Блокировать снова = бесконечный цикл «стоп → напоминание»;
# один показ хвостов за остановку достаточен.
payload=$(cat 2>/dev/null || true)
if printf '%s' "$payload" | python3 -I -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("stop_hook_active") else 1)' 2>/dev/null; then
  отметить_конец_хода
  exit 0
fi
# Некуда смотреть — нечего напоминать: молчим (это Stop, не отчёт о состоянии).
# Ход при этом всё равно закончен, и сторож обязан это знать.
[ -n "$project_dir" ] && cd "$project_dir" 2>/dev/null || { отметить_конец_хода; exit 0; }

# ── Пауза владельца: не просить того, что запрещено ─────────────────────────
# СЛУЧАЙ 26.09.2026. На паузе этот хук требовал обновить передачу, а сторож
# паузы (pause_guard.py) отменяет Write и Edit — два механизма противоречили
# друг другу, и смена выбирала вниманием, кто прав. Правило тут же, выше по
# файлу, уже названо про push: «Сторож, который просит невозможного, учит себя
# не слушать». На паузе хвостов не показываем: ход закончен, и это верно.
if [ -n "$project_dir" ] \
   && python3 -I "$project_dir/scripts/pause.py" --идёт-ли >/dev/null 2>&1; then
  отметить_конец_хода
  exit 0
fi

msgs=()

# СЛЕДУЮЩАЯ РАБОТА. Владелец 26.09.2026: «если у тебя блокер, но при этом есть
# любая другая работа, она должна исполняться, вплоть до следующего блокера».
# Хук называет её ИМЕНЕМ на завершении хода: «свободной работы нет» перестаёт
# быть выводом из головы — прибор отвечает раньше, чем смена успеет так решить.
next_work=""
if [ -f "$project_dir/dev-map.yaml" ]; then
  next_work=$(python3 -I "$project_dir/scripts/work-queue.py" \
      "$project_dir/dev-map.yaml" --следующая 2>/dev/null | head -1)
fi

# Неотправленные коммиты — напоминание, но ТОЛЬКО когда отправка вообще
# возможна. В полуавтомате push ждёт свежего «да» владельца, и пока его нет,
# окрик «сделай push» требует невыполнимого: 11.09.2026 он повторился четыре
# хода подряд, хотя вопрос владельцу был задан и ответа не было. Сторож,
# который просит невозможного, учит себя не слушать.
ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
if [ "${ahead:-0}" -gt 0 ]; then
    # Спрашиваем ту же ручку, что и сторож выката: PUSH_NEEDS_OWNER=0 значит
    # «владелец отпустил push, согласия не спрашивать» (его решение 03.09.2026).
    # Улика 12.09.2026: ключ стоял в 0, deploy_guard отвечал «согласие есть», а
    # этот хук требовал слова владельца — два механизма судили об одном
    # по-разному, и напоминание врало. То же самое в ту же ночь нашлось в
    # стороже сессий про лимит.
    # Спрашиваем ИСТОЧНИК, а не читаем ключ своими руками: копия суждения
    # расходится с оригиналом молча. Вызов дешёвый — 0,11 с; без --просить кнопку
    # он не просит и в сеть не ходит, а кнопка не тратится: её гасит само действие.
    SCRIPTS_REAL="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
    if python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие push \
            --только-согласие >/dev/null 2>&1; then
        msgs+=("не отправлено в удалённый репозиторий: $ahead коммит(ов) — git push (сторож выката: можно)")
    else
        msgs+=("$ahead коммит(ов) ждут отправки: нужна кнопка «Да» владельца, текст не засчитывается — python3 scripts/ask-consent.py --вид пуш --предмет \"<удалённый> <ветка>\" --почему …; не пушь до неё")
    fi
fi

# Файлы, которые пишет демон, — не работа смены: список путей — данные
# (DAEMON_OWNED_PATHS в harness.conf, «/» на конце — каталог целиком). Ключа
# нет — список пуст, и считается всё.
dirty=$(git status --porcelain -z 2>/dev/null \
  | python3 -I -c '
import sys
владеет = sys.argv[1].split()
записи = sys.stdin.buffer.read().decode("utf-8", "replace").split("\0")
счёт, i = 0, 0
while i < len(записи):
    запись = записи[i]
    i += 1
    if len(запись) < 4:
        continue
    if запись[0] in "RC":
        i += 1
    путь = запись[3:]
    if запись.startswith("??"):
        continue
    if any(путь.startswith(в) if в.endswith("/") else путь == в for в in владеет):
        continue
    счёт += 1
print(счёт)' "${DAEMON_OWNED_PATHS:-}" 2>/dev/null || echo 0)
[ "${dirty:-0}" -gt 0 ] && msgs+=("изменённых файлов без коммита: $dirty")

# Протухшая передача: вердикт даёт scripts/handover-check.sh — ЕДИНЫЙ источник
# правила (В-5); порог живёт в harness.conf (HANDOVER_MAX_AGE_MIN), не здесь.
hc="$project_dir/scripts/handover-check.sh"
if [ -x "$hc" ]; then
  hc_out=$("$hc" 2>/dev/null)
  hc_rc=$?
  # rc=1 — протухло; 0 — свежо/не требуется; 2 — нет конфига (fail-open: молчим).
  [ "$hc_rc" -eq 1 ] && [ -n "$hc_out" ] && msgs+=("$hc_out")
fi

# Незакрытая работа в dev-map.yaml: задачи в wip. Нет файла — молчим (не каждый
# проект ведёт манифест с первого дня).
#
# Считается ТОЛЬКО wip. Раньше считались все задачи не в done — вместе с plan
# (будущая работа) и test (ждут проверки владельца). В живом проекте таких
# всегда больше нуля, поэтому напоминание срабатывало на КАЖДОМ завершении и
# приучало себя не читать: предупреждение, которое горит всегда, не несёт
# сведений. Улика 11.08.2026: за сессию хук трижды сказал «очередь не пуста»,
# и все три раза делать было нечего — работа ждала владельца. Парсер: python3 (yaml, а при отсутствии PyYAML —
# подсчёт строк status по регулярке). Ветка yq убрана (В-6): apt ставит
# python-yq с ДРУГИМ синтаксисом — мёртвый код, который однажды молча врал бы.
devmap="dev-map.yaml"
notes=()
abandoned=0
active_ids=" "
if [ -f "$devmap" ]; then
  # «Работа идёт» — след свежее порога: коммит с трейлером Dev-Map: <id> или
  # правка .tasks/<id>/журнал.md. Без этого хук требовал «доведи» от задачи,
  # по которой смена только что закоммитила, и сигнал горел на каждом ходе.
  # Порог — данные (DEVMAP_TRACE_FRESH_MIN), не число в коде.
  wip_report=$(python3 -I - "$devmap" "${DEVMAP_TRACE_FRESH_MIN:-120}" <<'PY' 2>/dev/null
import os, re, subprocess, sys, time
path, fresh_min = sys.argv[1], int(sys.argv[2])
try:
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    def walk(x, out):
        if isinstance(x, dict):
            if str(x.get("status")) == "wip":
                out.append(x)
            for v in x.values():
                walk(v, out)
        elif isinstance(x, list):
            for v in x:
                walk(v, out)
        return out
    cards = walk(yaml.load(open(path, encoding="utf-8"), Loader=loader), [])
except Exception:
    # Нет PyYAML или битая карта: считаем все wip брошенными — лишнее
    # напоминание честнее молчания.
    text = open(path, encoding="utf-8").read()
    print("ABANDONED\t%d" % len(re.findall(r"""status:\s*["']?wip\b""", text)))
    sys.exit()

now = time.time()
last_trace = {}
log = subprocess.run(
    ["git", "log", "--since=%d minutes ago" % (fresh_min * 4), "--format=%ct\t%(trailers:key=Dev-Map,valueonly,separator=%x20)"],
    capture_output=True, text=True).stdout
for line in log.splitlines():
    ts, _, ids = line.partition("\t")
    for token in ids.replace(",", " ").split():
        if ts.isdigit():
            last_trace[token] = max(last_trace.get(token, 0), int(ts))
for card in cards:
    cid = str(card.get("id") or "")
    journal = os.path.join(os.path.dirname(os.path.abspath(path)), ".tasks", cid, "журнал.md")
    try:
        last_trace[cid] = max(last_trace.get(cid, 0), int(os.stat(journal).st_mtime))
    except OSError:
        pass
for card in cards:
    cid = str(card.get("id") or "?")
    age = int((now - last_trace[cid]) / 60) if cid in last_trace else None
    blocked = " ".join(str(card.get("blocked") or "").split())
    age_text = "-" if age is None else str(age)
    if blocked:
        print("BLOCKED\t%s\t%s\t%s" % (cid, age_text, blocked))
    elif age is not None and age <= fresh_min:
        print("ACTIVE\t%s\t%s" % (cid, age_text))
    else:
        print("ABANDONED\t1")
PY
)
  while IFS=$'\t' read -r kind cid age text; do
    case "$kind" in
      ACTIVE)    notes+=("идёт: $cid, последний след ${age} мин назад"); active_ids+="$cid " ;;
      BLOCKED)   notes+=("ждёт: $cid — $text (последний след: $([ "$age" = "-" ] && echo 'не найден' || echo "${age} мин назад"))"); active_ids+="$cid " ;;
      ABANDONED) abandoned=$((abandoned + ${cid:-0})) ;;
    esac
  done <<<"$wip_report"
  [ "$abandoned" -gt 0 ] && msgs+=("незакрытая работа в dev-map.yaml: $abandoned задач(и) в статусе wip — доведи до test или назови блокер")
fi

# Следующую работу не требуем, если она уже идёт или названо, чего она ждёт.
case "$next_work" in
  "СЛЕДУЮЩАЯ РАБОТА:"*)
    next_id=${next_work#СЛЕДУЮЩАЯ РАБОТА: }; next_id=${next_id%% *}
    case "$active_ids" in
      *" $next_id "*) ;;
      *) msgs+=("${next_work#СЛЕДУЮЩАЯ РАБОТА: } — бери её, если стоишь") ;;
    esac ;;
esac

# Накопленные входящие: ОДИН сортированный список на рубеже хода. Толчок о
# каждом сообщении идёт сразу (дверь scripts/lib/pane.sh); свод страхует то,
# что сессия не взяла сама. Свод считает scripts/inbox-digest.py: порядок он
# берёт у work-queue.py, второй сортировщик разошёлся бы с первым молча.
svodka=""
if [ -f "$project_dir/scripts/inbox-digest.py" ]; then
  svodka=$(python3 -I "$project_dir/scripts/inbox-digest.py" 2>/dev/null || true)
fi

# Заметки («идёт», «ждёт») информируют и ход не блокируют.
if [ ${#msgs[@]} -eq 0 ] && [ -z "$svodka" ]; then
  for n in "${notes[@]}"; do echo "  · $n" >&2; done
  отметить_конец_хода
  exit 0
fi

# stderr + exit 2 (В-1): stdout Stop-хука модель не видит; код 2 блокирует
# завершение, и stderr попадает модели — хвосты будут прочитаны, не потеряны.
{
  echo "=== перед завершением ==="
  [ -n "$svodka" ] && printf '%s\n' "$svodka"
  for m in "${msgs[@]}"; do echo "  ⚠ $m"; done
  for n in "${notes[@]}"; do echo "  · $n"; done
} >&2
exit 2
