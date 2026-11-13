#!/usr/bin/env python3
"""Проба-контракт режима «хакатон»: библиотека, CLI и гейт пайплайна.

Исполнитель A2. Файлы: scripts/hackathon.py (новый), scripts/check-pipeline.py,
harness/config/pipeline.yaml. Спека: docs/specs/2026-10-28-режим-хакатон.md.

КОНТРАКТ scripts/hackathon.py (имена точные, проба зовёт именно их):

  LEVEL = "хакатон"                     значение поля глубина.уровень
  until(conf_path=None) -> datetime | None
      HACKATHON_UNTIL из harness.conf (conf_path, иначе config.путь_harness_conf(),
      то есть env HARNESS_CONF). Значение — ISO 8601 С ПОЯСОМ, в кавычках или без
      (write-key.заменить пишет без кавычек). Пусто, нет ключа, не разбирается,
      нет пояса — None.
  active(now=None, conf_path=None) -> bool
      until() не None и now < until(). now — aware datetime; None — сейчас.
  is_hackathon(card) -> bool
      card["глубина"] — словарь и его «уровень» == LEVEL; иначе False.
  touches_harness_guards(paths, conf_path=None) -> list[str]
      пути из paths (порядок сохранён), подпадающие под GUARD_PATHS ИЛИ под маски
      paths: из harness/config/hackathon-forbidden.yaml (рядом со скриптом:
      <родитель scripts/>/harness/config/). Сопоставление одно для обоих списков —
      pipeline._под_путями (без «*» — префикс или полное имя, со «*» — fnmatch);
      GUARD_PATHS — pipeline.пути_установки(conf). Пустой/нечитаемый yaml — не
      «можно всё»: исключение или отказ гейта.
  debt_id(task_id) -> str               task_id + "-prod-debt"
  make_debt_card(task_id, today) -> dict
      карточка долга «довести до продакшна»: id=debt_id(task_id), status="plan",
      title со словом «продакшн», summary называет task_id; проходит схему карты
      (id, status, title, summary).
  debt_open(cards, task_id) -> bool
      cards — список карточек (словарей). True, если карточка debt_id(task_id)
      есть и её статус НЕ в cards.CLOSED_STATUSES (scripts/lib/cards.py).
      Долга нет — False (отсутствие долга судит --проверить-карту отдельно).

  CLI (python3 scripts/hackathon.py …):
  --активен                 код 0 — режим жив, 1 — нет (срок истёк или пуст).
  --активен --карта <путь>  код 0 только если режим жив И единственная карточка
                            в статусе wip этой карты — уровня хакатон (этим
                            спрашивает deploy.sh, см. test_hackathon_gates.sh).
  --включить [часы]         пишет HACKATHON_UNTIL = сейчас + часы (умолч. 72),
                            ISO с поясом, в harness.conf (env HARNESS_CONF);
                            строка ключа одна, остальной файл не тронут; запись
                            через write-key.заменить + scripts/lib/panel_writer
                            (копия прежнего файла в $LOG_DIR/панель-копии — И-1).
  --включить-задачу <id> --карта <путь>
                            ОДНОЙ записью под локом карты (cards.lock_path() из
                            LOG_DIR; cards.write_cards_locked или map_lock):
                            глубина.уровень карточки <id> := хакатон (прочие
                            ключи глубины целы; нет глубины — {уровень: хакатон,
                            характер: разработка, кем: hackathon.py, когда:
                            сегодня}) и карточка долга make_debt_card в той же
                            линии. Повтор не дублирует долг. Правка текстом:
                            комментарии карты живы. Нет такой задачи — код ≠ 0,
                            карта не тронута. Запись АТОМАРНАЯ (m-2): новый
                            текст во временный файл в каталоге карты, затем
                            os.replace — inode карты после записи новый, в
                            каталоге карты не остаётся хвостов; падение посреди
                            записи (проба подменяет open/os.write и рвёт запись
                            текста с id долга) оставляет карту байт в байт прежней.
  --проверить-карту <путь>  код 1, если у карточки хакатона нет долга ИЛИ её
                            статус done при открытом долге; иначе 0. Тексты (их
                            же показывает pre-commit-gate.sh, исполнитель C):
                            без долга — «hackathon.py --включить-задачу <id>»;
                            done при открытом — слово «долг» и id долга.

КОНТРАКТ scripts/check-pipeline.py (гейт pre-commit, зовётся как есть;
круг по ревью 29.10 — C-1, M-1, M-2 docs/specs/2026-10-28-режим-хакатон-CODE-REVIEW.md):
  * Карточка уровня берётся из ИНДЕКСА (git show :dev-map.yaml) — единственная
    wip там; рабочее дерево карты гейт не читает (M-2). Случай F: индекс
    «обычная/test», рабочее дерево «хакатон/wip» без долга → судится как
    обычная («спека не требуется», слова «хакатон» нет); индекс «хакатон/wip»,
    рабочее дерево «обычная/wip», задет сторож → отказ хакатона. Бумаги шагов
    судятся по карточке ИНДЕКСА же: индекс «хакатон/wip» без spec:, рабочее
    дерево со spec: → код 1, отказ про spec: (случай F3).
  * Хакатон судится НЕ ранним выходом (M-2): карточка проходит общие проверки
    закрытия, шагов и слова владельца, как задача в работе, с шагами
    шаги.хакатон; в wip из них требуются спека и ревью_спеки (поле spec: и файл
    <спека>-REVIEW.md), остальные — при переходе в test. Нет spec: → код 1,
    в выводе «spec:». Уровень автоматизации «частичный» (AUTOMATION_LEVEL нет)
    и спека не отправлена → код 1, «НЕ ОТПРАВЛЕНА». Характер «разработка»
    шагов не сужает.
  * Затем: режим не жив (срок истёк или HACKATHON_UNTIL пуст) → код 1, в выводе
    «срок хакатона»;
  * в индексе есть путь из touches_harness_guards (GUARD_PATHS ∪
    hackathon-forbidden.yaml, C-1) → код 1, в выводе «хакатон не трогает
    сторожей/гейты/хуки» и сам путь; для КАЖДОЙ маски файла данных;
  * иначе код 0 и строка «шаги: <шаги.хакатон из pipeline.yaml через «, »>».
  Карточки других уровней судятся как прежде.
  pipeline.yaml (M-1): шаги.хакатон: [спека, ревью_спеки, контракт, план, тесты,
  ревью_кода]; мутации — в шаги_приёмки (ночью), не в шагах хакатона.

КОНТРАКТ harness/config/hackathon-forbidden.yaml (данные C-1):
  paths: [маски] с комментарием «почему». Минимум (проба сверяет вхождение):
  scripts/deploy.sh, harness/config/harness.conf, harness/config/pipeline.yaml,
  harness/config/gates-hackathon.yaml, harness/config/hackathon-forbidden.yaml,
  scripts/hackathon.py, .claude/settings.json, scripts/lib/*, scripts/hooks/*.
  Пустой список — провал пробы. На каждую маску (для «*» — настоящий файл под
  ней из дерева предмета) проба зовёт touches_harness_guards и гейт.
  Новой env-двери check-pipeline.py не нужно: проба кладёт КОПИЮ scripts/ и
  pipeline.yaml в свой git-репозиторий (КОРЕНЬ = родитель scripts/), конфиг —
  через HARNESS_CONF, журналы — LOG_DIR, доска — HARNESS_DOSKA.

КРУГ ПРАВОК 3 (повторное ревью 29.10, N-1 и N-2; исполнитель C3, пробы P3):

N-1. Запреты хакатона = GUARD_PATHS ∪ маски hackathon-forbidden.yaml ∪
  judge_dependencies(root). Имя держать:
  judge_dependencies(root) -> list[str]
      пути от корня root (родитель scripts/), отсортированы, без повторов:
      КОРНИ суда — scripts/check-pipeline.py, scripts/hackathon.py,
      scripts/pre-commit-gate.sh, scripts/commit-msg-gate.sh (те из них, что
      есть на диске) — и всё, что они исполняют, ТРАНЗИТИВНО, выводом из
      текста, а не перечнем. Ребро (то же правило разбирает проба):
        .py — ast: import X / from X import … (без относительных), X.py есть
              в scripts/ или scripts/lib/; путь, составленный «<выр> / "<имя>"»
              или os.path.join(…, "<имя>"), где <имя> — голое имя *.py|*.sh,
              есть в scripts/ или scripts/lib/ (так берутся pipeline.py через
              spec_from_file_location и write-key.py);
        .sh — строки без ведущего «#»: каждое «/<имя>.py|sh» или
              «/lib/<имя>.py|sh», которое есть в scripts/ (scripts/lib/).
      Нечитаемый корень или пустой итог — исключение (гейт: код 1), не «можно».
  Явно в данных hackathon-forbidden.yaml: scripts/spec_approval.py (слово
  владельца) и scripts/tg_send.sh (метка И-4).
  touches_harness_guards: путь из judge_dependencies сравнивается ТОЧНЫМ
  равенством, не через pipeline._под_путями — иначе коммит, который ломает
  pipeline.py (сверка путей), сам отключает свой суд (живой случай ревьюера B:
  pipeline.py с «return False» первой строкой _под_путями + deploy.sh — отказ).
  Проба: свой разбор (список не пуст, в нём pipeline.py, spec_approval.py,
  lib/cards.py) ⊆ judge_dependencies(предмет); на КАЖДЫЙ файл разбора и
  tg_send.sh — touches_harness_guards и отказ гейта в песочнице при живом сроке.

N-2. Хакатон узнаётся по карточкам, которые ЗАДЕВАЕТ коммит, а не по «ровно одна
  wip в индексе». Задетые карточки стадии pre-commit: все wip карты индекса и
  все, чья запись в индексе отличается от HEAD (статус, уровень — любая правка).
  Карточка — хакатон, если уровень хакатон в индексе ИЛИ в HEAD (строже).
  Хоть одна задетая — хакатон → суд хакатона (срок, запреты), а его
  HARNESS_PIPELINE_FORCE не открывает. Задетых хакатонов нет — как прежде.
  Случаи: wip→test карточки хакатона (E1, E2), две wip, одна из них хакатон
  (E3), понижение уровня хакатон→обычная в том же коммите (E6).
  ТРЕЙЛЕР «Dev-Map: <id>». Выбор стадии: commit-msg. Замер 29.10 (свой
  репозиторий с двумя хуками): в pre-commit .git/COMMIT_EDITMSG несёт сообщение
  ПРЕДЫДУЩЕГО коммита (git пишет новое после pre-commit), поэтому чтение его там
  приписало бы чужой трейлер; на стадии commit-msg git передаёт файл сообщения
  первым аргументом. В проекте стоит один хук (.git/hooks/pre-commit →
  scripts/pre-commit-gate.sh); C3 заводит второй:
    scripts/commit-msg-gate.sh  — exec python3 <scripts>/check-pipeline.py
                                  --commit-msg "$1"; код возврата — код гейта;
    раскладка (пакет harness/razlozhit-harnes.sh, рядом с pre-commit):
      ln -sf ../../scripts/commit-msg-gate.sh .git/hooks/commit-msg.
  check-pipeline.py --commit-msg <файл>: трейлеры «Dev-Map: <id>» сообщения;
  карточка каждого id — из карты ИНДЕКСА (и HEAD, строже); хоть одна —
  хакатон → суд хакатона по индексу (срок, запреты), иначе код 0 без вывода
  о хакатоне. Общие проверки стадия commit-msg не повторяет (их судил pre-commit).
  Случаи E4 (трейлер хакатона, карточка в карте не меняется, 0 wip): deploy.sh —
  код 1 с отказом хакатона; срок истёк — код 1 «срок хакатона»; безобидный
  файл при живом сроке — 0; трейлер обычной карточки и нет трейлера — 0.
  E5: настоящий git commit в песочнице с хуком commit-msg → commit-msg-gate.sh,
  трейлер хакатона + deploy.sh — коммит не встал (HEAD прежний).

КРУГ ПРАВОК 4 (ревью круга 3, 30.10: N-4, n-6; исполнитель C4, пробы P4).
Абзац N-2 выше о стадии pre-commit ОТМЕНЁН этим кругом; трейлер остаётся.

N-4. Суд хакатона (срок, запреты) — ТОЛЬКО коммиту, чей трейлер «Dev-Map: <id>»
  называет карточку уровня хакатон (в индексе или HEAD), то есть на стадии
  commit-msg. Стадия pre-commit суда хакатона не ведёт вовсе: карточка хакатона
  в wip (две wip, wip→test, смена уровня) чужой коммит не судит — код и вывод
  как у того же коммита, где карточка обычная. Общие проверки pre-commit
  (spec:, ревью спеки, слово владельца, шаги.хакатон для wip-хакатона индекса)
  остаются, как были. Строка «шаги: …» печатается судом стадии commit-msg.
  ДВЕРЬ ВЫХОДА (без HARNESS_PIPELINE_FORCE): коммит с трейлером на карточку
  хакатона, у которого в индексе изменён ТОЛЬКО dev-map.yaml, а карточка
  в индексе либо уже не хакатон (уровень понижен), либо ушла из wip в статус,
  разрешённый правилами долга (test при открытом долге; done при открытом
  долге — нет, его судит --проверить-карту), — проходит суд кодом 0 и после
  срока, и при выключенном режиме. Хоть один путь кроме dev-map.yaml или
  карточка осталась хакатоном в wip — суд как прежде (срок, запреты).
  Случаи: (1) карточка хакатона в wip, срок вышел / режим выключен / срок
  жив и задет scripts/lib/cards.py; коммит с трейлером другой карточки или без
  трейлера — код как у двойника, где hr.w обычная (двойник — код 0), слов
  «срок хакатона» и отказа хакатона нет; (2) тот же коммит с трейлером hr.w —
  код 1 «срок хакатона»; (3) дверь выхода после срока — код 0 без FORCE;
  (4) дверь выхода с docs/x.md, и только карта без выхода из хакатона — код 1;
  (5) срок жив, трейлер hr.w, файл суда — отказ (N-1 цел).

(а) hackathon.forbidden_masks() без явного пути: корень (родитель scripts/)
  внутри git-репозитория — файл из ИНДЕКСА (гейт: в коммит уходит индекс;
  рабочий файл без git add не влияет); вне git-репозитория (копия дерева
  мутаций, git rev-parse не находит репозиторий) — файл с диска. Пустой или
  нечитаемый — по-прежнему RuntimeError.

n-6. scripts/install-new.py ставит хуки git проекта (идёт после каждого
  обновления, update.py), как раскладчик: если <корень>/.git — каталог,
  .git/hooks/pre-commit -> ../../scripts/pre-commit-gate.sh и
  .git/hooks/commit-msg -> ../../scripts/commit-msg-gate.sh (симлинки,
  относительные; верный симлинк не трогается, повтор ничего не меняет).
  Имя держать: install_git_hooks(root: Path) -> list[str] — имена хуков,
  которые поставлены этим вызовом; .git нет — [] и ничего не создано.
  main() зовёт её как атрибут модуля с корнем проекта, который main уже
  считает (PROJECT_DIR или КОРЕНЬ). Проба: scripts/test_install_new.py (свой
  git-репозиторий в tmp; в случае выключателя main проба её глушит).

КРУГ ПРАВОК 5 (ревью круга 4, 30.10: N-5, n-8, n-10; исполнитель C5, пробы P5).

N-5. Режим активен = срок жив (hackathon.active()) И в карте индекса или HEAD
  есть карточка уровня хакатон (любой статус). Пока режим активен, стадия
  commit-msg отказывает (код 1) коммиту, в сообщении которого НЕТ трейлера
  Dev-Map; отказ называет «Dev-Map: <id>» каждой такой карточки и дверь.
  Дверь — устоявшаяся дверь гейта (дверь_открыта в check-pipeline.py):
  HARNESS_PIPELINE_FORCE=1 и HARNESS_PIPELINE_WHY='причина' — коммит проходит,
  в журнале обходов $LOG_DIR/пайплайн-обходы.jsonl новая строка: «почему» —
  причина, «задача» — id карточки хакатона. FORCE без причины — отказ, как у
  всех дверей гейта. Трейлер на любую карточку (свою или чужую) — суд как в
  круге 4: чужой трейлер виден в истории. Режим не активен (срок вышел, пуст,
  или карточек хакатона нет) — коммит без трейлера проходит как прежде, без
  вывода о хакатоне. Случаи: без трейлера при активном режиме — код 1 и
  «Dev-Map: hr.w»; то же с дверью — код 0 и строка журнала; FORCE без
  причины — код 1; без трейлера после срока / при пустом сроке / без карточки
  хакатона — код 0.

n-8. Чей коммит, знает только трейлер, а его видит лишь стадия commit-msg.
  Шаги хакатона (шаги.хакатон, spec:, ревью спеки, слово владельца) судятся
  ТОЛЬКО коммиту, чей трейлер называет карточку хакатона. Чужой коммит
  (трейлер hr.p) при единственной wip хакатона hr.w — код И сложенный вывод
  обеих стадий (pre-commit, затем commit-msg) как у двойника, где hr.w
  обычная (с spec: и без). Как разложить по стадиям — дело кода (например,
  pre-commit при единственной wip-хакатоне общие проверки откладывает, а
  commit-msg судит их по трейлеру: хакатон — шагами хакатона, иначе как
  двойник). Свой коммит без spec: — по-прежнему отказ про spec: (случаи M-1,
  F3 гоняют обе стадии с трейлером hr.w).

n-10. Трейлер Dev-Map разбирается в ОДНОМ месте. Имя держать:
  scripts/lib/cards.py: dev_map_ids(text: str) -> list[str]
      id из строк сообщения, которые НАЧИНАЮТСЯ с «Dev-Map:»; пробелов после
      двоеточия сколько угодно (и ноль); в строке несколько id через запятую
      и/или пробелы; порядок первого появления, без повторов; «Dev-Map:» без
      id и «Dev-Map:» не с начала строки — ничего.
  check-pipeline.py (commit_msg_gate и сверка коммита находки безопасности)
  и acceptance.py (dev_map_commit_times) зовут её; своих регулярок трейлера
  (вызов re.* с образцом, где есть «Dev-Map») в этих двух файлах нет — проба
  разбирает их ast. Строки git log --grep сюда не входят (это поиск, не разбор).
  Случаи: набор строк — dev_map_ids и разбор приёмки (dev_map_commit_times на
  настоящих коммитах) дают одно; суд хакатона видит hr.w в «Dev-Map:hr.w»,
  «Dev-Map:   hr.w», «Dev-Map: hr.p, hr.w», «Dev-Map: hr.p hr.w».

КРУГ ПРАВОК 6 (ревью круга 5, 30.10: N-8; исполнитель C6, пробы P6).

N-8. Стадия commit-msg читала карту раньше проверки трейлера и режима: в
  репозитории БЕЗ dev-map.yaml (самотест раскладчика, свежий проект) любой
  коммит получал отказ «карта не прочитана». Контракт commit_msg_gate:
  - коммит БЕЗ трейлера Dev-Map и hackathon.active() ложно (срок пуст, вышел,
    без пояса) — код 0 сразу, карта не читается вовсе;
  - dev-map.yaml нет ни в индексе, ни в HEAD (git show отвечает «нет пути»,
    git при этом жив) — код 0 с трейлером и без: карточек хакатона нет, режим
    не может быть активен, судить нечего;
  - трейлер называет карточку хакатона — суд как в кругах 4-5 и при выключенном
    режиме: отказ «срок хакатона вышел или не задан» (N-4 (2), «гейт: хакатон,
    срок пуст») НЕ отменяется; трейлер обычной или несуществующей карточки — 0;
  - карта ЕСТЬ, но не читается (битый YAML, git не ответил) — код 1, как
    прежде, но только там, где карта нужна: трейлер есть или срок жив.
    Почему отказ, а не пропуск: битая карта не даёт узнать, хакатон ли
    названная карточка и активен ли режим, — пропуск открыл бы суд хакатона
    порчей карты. Честному коммиту это не мешает: битую карту раньше ловит
    pre-commit (гейт схемы карты), до commit-msg такой коммит не доходит.
  Случаи (настоящий git commit с хуком commit-msg-gate.sh): (1) без карты, без
  трейлера, срок жив и срок пуст — встал; (2) без карты, трейлер на
  несуществующую карточку — встал; (3) карта с хакатоном, срок вышел / пуст,
  без трейлера — встал; (4) то же, трейлер обычной hr.p — встал; (5) НЕИЗМЕННО:
  срок жив, карточка хакатона в карте, без трейлера — не встал (N-5 цел);
  (6) битая карта в индексе: срок пуст, без трейлера — встал (карта не
  читается); срок жив, трейлер hr.w — не встал.

КЭШ judge_dependencies (живая проверка ч.12, 12.11.2026): разбор 431 файла
  стоил 46-131 с на КАЖДЫЙ вызов; touches_harness_guards зовёт его на каждый
  путь, а гейт (judge_gate_cases) зовёт check-pipeline.py ~430 процессами на
  одном корне, дописывая в каждый случай комментарий в ОДИН файл — проба висела.
  Контракт: кэшируется РАЗБОР ФАЙЛА (сырые имена-кандидаты рёбер, до проверки
  «файл есть») по ключу (абсолютный путь файла, st_mtime_ns, st_size); есть ли
  кандидат на диске — проверяется при каждом вызове (stat), поэтому появившийся
  файл виден без сброса. Корень сводится Path(root).resolve(). Кэш живёт в
  памяти процесса; если задан HARNESS_JUDGE_CACHE (каталог прогона) — ещё и в
  файле там, общий процессам прогона. Отказ (RuntimeError) не кэшируется.
  Вызов из кэша НЕ открывает файлов под корнем на чтение (stat — можно); после
  правки одного файла читается ровно он. Случаи КЭШ 0-8 (judge_cache_cases) —
  один процесс; КЭШ 5а, 5б, 3а (judge_cache_key_cases) — каждая часть ключа
  (mtime, размер, корень) видна своей правкой; КЭШ 9-12
  (judge_cache_process_cases) — отдельные процессы.

Окружение пробы своё: tmp-каталог, свой harness.conf (HARNESS_CONF), свой
install.conf, LOG_DIR, HARNESS_HOME, копия-образец карты. Боевые harness.conf,
dev-map.yaml и лок карты не трогаются; путь лока, вышедший за tmp, — отказ.
Дверь мутаций: HACKATHON_PROBE_SRC — корень дерева, которое судится (копия
проекта с мутацией); по умолчанию — проект, где лежит проба.

    python3 scripts/test_hackathon.py
"""
import ast
import datetime as dt
import fcntl
import fnmatch
import builtins
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("HACKATHON_PROBE_SRC") or HERE.parent).resolve()
sys.path.insert(0, str(HERE / "lib"))
from config import из_текста                              # noqa: E402
from selftest_cases import Cases                          # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="hackathon-probe-"))
LOG_DIR = WORK / "log"
CONF = WORK / "harness.conf"
LOG_DIR.mkdir()
(WORK / "install.conf").write_text(f'LOG_DIR="{LOG_DIR}"\n', encoding="utf-8")

# Окружение ставится ДО импорта предмета: config и panel_writer читают его при
# импорте, и боевой LOG_DIR иначе успел бы стать путём копий и лока.
SANDBOX_ENV = {
    "HARNESS_HOME": str(WORK / "home"),
    "HARNESS_INSTALL_CONF": str(WORK / "install.conf"),
    "HARNESS_CONF": str(CONF),
    "LOG_DIR": str(LOG_DIR),
    # spec_approval читает журнал отправок и inbox владельца отсюда.
    "HARNESS_LOG_DIR": str(LOG_DIR),
    "HARNESS_DOSKA": str(WORK / "doska.json"),
    "PANEL_NO_SUDO": "1",
    # Кэш разбора суда хакатона — общий процессам ЭТОГО прогона (check-pipeline.py гейта).
    "HARNESS_JUDGE_CACHE": str(WORK / "judge-cache"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}
os.environ.update(SANDBOX_ENV)
for door in ("HARNESS_PIPELINE_FORCE", "HARNESS_PIPELINE_WHY", "HARNESS_BACKLOG_OWNER_WORD",
             "SERVICE_LOCKS_SUBDIR", "AUTOMATION_LEVEL"):
    os.environ.pop(door, None)

TZ = dt.timezone(dt.timedelta(hours=5))
NOW = dt.datetime.now(TZ).replace(microsecond=0)
TODAY = NOW.date().isoformat()
cases = Cases()


def guard_paths_of_project() -> str:
    """GUARD_PATHS берётся разбором образца конфига проекта, а не вписан сюда."""
    text = (SOURCE / "harness" / "config" / "harness.conf").read_text(encoding="utf-8")
    return из_текста(text).get("GUARD_PATHS", "").strip()


GUARD_PATHS = guard_paths_of_project()
FORBIDDEN_YAML = SOURCE / "harness" / "config" / "hackathon-forbidden.yaml"
# Минимум запретов из ревью C-1: выпавшая из данных маска — провал, а не «меньше судить».
FORBIDDEN_MINIMUM = ("scripts/deploy.sh", "harness/config/harness.conf",
                     "harness/config/pipeline.yaml", "harness/config/gates-hackathon.yaml",
                     "harness/config/hackathon-forbidden.yaml", "scripts/hackathon.py",
                     ".claude/settings.json", "scripts/lib/*", "scripts/hooks/*")
HACKATHON_STEPS = ["спека", "ревью_спеки", "контракт", "план", "тесты", "ревью_кода"]


def forbidden_masks_of_source():
    """Маски — разбором данных предмета; нет файла или ключа — пустой список (провал)."""
    try:
        data = yaml.safe_load(FORBIDDEN_YAML.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    masks = data.get("paths") if isinstance(data, dict) else None
    return [m for m in masks if isinstance(m, str) and m] if isinstance(masks, list) else []


def harness_files():
    """Файлы scripts/ и harness/ предмета — обходом диска: копия для мутаций git не несёт."""
    return sorted(str(path.relative_to(SOURCE)) for top in ("scripts", "harness")
                  for path in (SOURCE / top).rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts)


def concrete_path(mask, files):
    """Настоящий путь под маской: для «*» — первый файл дерева предмета под ней."""
    if "*" not in mask:
        return mask
    return next((path for path in files if fnmatch.fnmatch(path, mask)), None)


JUDGE_ROOTS = ("scripts/check-pipeline.py", "scripts/hackathon.py",
               "scripts/pre-commit-gate.sh", "scripts/commit-msg-gate.sh")
# Без этих трёх вывод зависимостей разбором — не вывод: так узнаётся, что разбор пробы жив.
DERIVED_MINIMUM = ("scripts/pipeline.py", "scripts/spec_approval.py", "scripts/lib/cards.py")
# Явно в данных, не выводом: слово владельца и метка И-4.
EXPLICIT_FORBIDDEN = ("scripts/spec_approval.py", "scripts/tg_send.sh")
BARE_NAME = re.compile(r"[\w.-]+\.(?:py|sh)$")
SHELL_CALL = re.compile(r"/((?:lib/)?[\w.-]+\.(?:py|sh))\b")


def _in_scripts(root, name):
    for folder in ("scripts", "scripts/lib"):
        if (root / folder / name).is_file():
            return f"{folder}/{name}"
    return None


def _python_edges(root, path):
    found = set()
    for node in ast.walk(ast.parse((root / path).read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] + ".py" for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module.split(".")[0] + ".py"]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            names = [node.right.value] if isinstance(node.right, ast.Constant) else []
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "join" and node.args and isinstance(node.args[-1], ast.Constant)):
            names = [node.args[-1].value]
        found |= {hit for name in names
                  if isinstance(name, str) and BARE_NAME.match(name)
                  for hit in [_in_scripts(root, name)] if hit}
    return found


def _shell_edges(root, path):
    lines = (root / path).read_text(encoding="utf-8").splitlines()
    return {f"scripts/{rel}" for line in lines if not line.lstrip().startswith("#")
            for rel in SHELL_CALL.findall(line) if (root / "scripts" / rel).is_file()}


def judge_files_by_parsing(root):
    """Корни суда и всё, что они исполняют транзитивно, — своим разбором текста."""
    seen, todo = set(), [r for r in JUDGE_ROOTS if (root / r).is_file()]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        edges = _python_edges if path.endswith(".py") else _shell_edges
        todo.extend(edges(root, path) - seen)
    return sorted(seen)


def write_conf(until_value, quoted=True, path=CONF, extra=()):
    lines = ["# образец конфига пробы — комментарий обязан пережить запись",
             'OTHER_KEY="keep-me"', f'GUARD_PATHS="{GUARD_PATHS}"', *extra]
    if until_value is not None:
        lines.append(f'HACKATHON_UNTIL="{until_value}"' if quoted
                     else f"HACKATHON_UNTIL={until_value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def until_values(text):
    """Значения строк HACKATHON_UNTIL — по одному на строку: дубль ключа виден числом."""
    return [из_текста(line)["HACKATHON_UNTIL"] for line in text.splitlines()
            if line.split("=", 1)[0].strip() == "HACKATHON_UNTIL"]


def iso(moment):
    return moment.isoformat(timespec="seconds")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guarded(name, func):
    """Случай, упавший исключением, — ПЛОХО с причиной, а не обрыв всей пробы."""
    try:
        func()
    except Exception as error:                            # noqa: BLE001
        cases.check(f"{name} (исключение)", "без исключения",
                    f"{type(error).__name__}: {str(error)[:160]}")


def hackathon_cli(*args, timeout=60):
    done = subprocess.run([sys.executable, str(SOURCE / "scripts" / "hackathon.py"), *args],
                          capture_output=True, text=True, timeout=timeout, cwd=WORK)
    return done.returncode, done.stdout + done.stderr


# ── образец карты в стиле живой: линия, задачи с отступом 6, глубина строкой ──
def card(task_id, status, depth=None, extra=(), character="починка"):
    rows = [f"      - id: {task_id}", f"        status: {status}",
            f'        title: "Задача {task_id}"', f'        summary: "образец пробы {task_id}"']
    if depth:
        rows.append(f"        глубина: {{ уровень: {depth}, характер: {character}, кем: смена, "
                    f'когда: "{TODAY}" }}')
    rows.extend(f"        {row}" for row in extra)
    return "\n".join(rows)


def map_text(*cards_text):
    return ("version: 1\nqueue: []\nlayers:\n  - { id: infra, label: \"Инфра\", color: \"#5aa06a\" }\n"
            "lines:\n  - id: core\n    layer: infra\n    name: \"Ядро\"\n    tasks:\n"
            "      # якорь-комментарий: правка текстом обязана его сохранить\n"
            + "\n".join(cards_text) + "\n")


def tasks_of(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [t for line in data.get("lines") or [] for t in line.get("tasks") or []]


def by_id(path, task_id):
    return [t for t in tasks_of(path) if t.get("id") == task_id]


DONE_EXTRA = (f'tested_at: "{TODAY}"',)
ACCEPTED_EXTRA = ('проверка: { команда: "true" }', f'принято_когда: "{TODAY}"')

lib = None


def lib_cases():
    global lib
    lib = load("hackathon", SOURCE / "scripts" / "hackathon.py")
    cards_lib = load("cards_lib", SOURCE / "scripts" / "lib" / "cards.py")
    cases.check("LEVEL — «хакатон»", "хакатон", lib.LEVEL)

    future, past = NOW + dt.timedelta(hours=3), NOW - dt.timedelta(hours=3)
    cases.check("until: срок в будущем разобран с поясом", future,
                lib.until(conf_path=str(write_conf(iso(future)))))
    cases.check("until: значение без кавычек (так пишет write-key)", future,
                lib.until(conf_path=str(write_conf(iso(future), quoted=False))))
    cases.check("until: пустое значение — None", None, lib.until(conf_path=str(write_conf(""))))
    cases.check("until: ключа нет — None", None, lib.until(conf_path=str(write_conf(None))))
    cases.check("until: без пояса — None", None,
                lib.until(conf_path=str(write_conf(future.replace(tzinfo=None).isoformat()))))
    cases.check("until: путь из HARNESS_CONF, когда conf_path не дан", future,
                (write_conf(iso(future)), lib.until())[1])

    conf = str(write_conf(iso(future)))
    cases.check("active: срок в будущем — жив", True, lib.active(conf_path=conf))
    cases.check("active: now за минуту до срока — жив", True,
                lib.active(now=future - dt.timedelta(minutes=1), conf_path=conf))
    cases.check("БОЛЬНОЙ СЛУЧАЙ active: now через минуту после срока — не жив", False,
                lib.active(now=future + dt.timedelta(minutes=1), conf_path=conf))
    cases.check("active: срок истёк — не жив", False,
                lib.active(conf_path=str(write_conf(iso(past)))))
    cases.check("active: срок пуст — не жив", False, lib.active(conf_path=str(write_conf(""))))

    cases.check("is_hackathon: уровень хакатон", True,
                lib.is_hackathon({"id": "a", "глубина": {"уровень": "хакатон"}}))
    cases.check("is_hackathon: уровень обычная", False,
                lib.is_hackathon({"id": "a", "глубина": {"уровень": "обычная"}}))
    cases.check("is_hackathon: глубины нет", False, lib.is_hackathon({"id": "a"}))
    cases.check("is_hackathon: глубина строкой, не словарь", False,
                lib.is_hackathon({"id": "a", "глубина": "хакатон"}))

    conf = str(write_conf(iso(future)))
    cases.check("GUARD_PATHS образца проекта не пуст (судить есть по чему)", True, bool(GUARD_PATHS))
    cases.check("touches_harness_guards: guard_bash.py задевает, обычный файл нет",
                ["scripts/hooks/guard_bash.py"],
                lib.touches_harness_guards(["docs/x.md", "scripts/hooks/guard_bash.py"],
                                           conf_path=conf))
    cases.check("touches_harness_guards: безобидные пути — пусто", [],
                lib.touches_harness_guards(["docs/x.md", "scripts/who-writes.py"], conf_path=conf))
    pipeline = load("pipeline_probe", SOURCE / "scripts" / "pipeline.py")
    listed = harness_files()
    masks = forbidden_masks_of_source()
    guard = pipeline.пути_установки(conf)["GUARD_PATHS"]
    # Список зависимостей судит judge_dependency_cases; здесь — только сложение трёх частей.
    judged = set(lib.judge_dependencies(SOURCE))
    expected = [p for p in listed if p in judged
                or pipeline._под_путями(p, guard) or pipeline._под_путями(p, " ".join(masks))]
    cases.check("сверка с pipeline: файлов харнеса разобрано, запретных и прочих — оба > 0",
                True, bool(masks) and bool(expected) and len(expected) < len(listed))
    cases.check(f"touches_harness_guards = GUARD_PATHS ∪ hackathon-forbidden.yaml ∪ "
                f"judge_dependencies на {len(listed)} путях",
                expected, lib.touches_harness_guards(listed, conf_path=conf))

    # C-1: запреты — данные. Пустой разбор — провал, минимум ревью — в данных.
    cases.check("C-1 hackathon-forbidden.yaml: paths разобран и не пуст", True, bool(masks))
    cases.check("C-1 hackathon-forbidden.yaml: минимум ревью весь в данных",
                [], [m for m in FORBIDDEN_MINIMUM if m not in masks])
    for mask in masks:
        path = concrete_path(mask, listed)
        cases.check(f"C-1 touches_harness_guards: маска {mask} → {path} в списке", [path],
                    lib.touches_harness_guards([path], conf_path=conf) if path else ["<файла нет>"])

    cases.check("debt_id", "hr.x-prod-debt", lib.debt_id("hr.x"))
    debt = lib.make_debt_card("hr.x", TODAY)
    cases.check("make_debt_card: id долга", "hr.x-prod-debt", debt.get("id"))
    cases.check("make_debt_card: статус plan (открыт)", "plan", debt.get("status"))
    cases.check("make_debt_card: title про продакшн, summary называет задачу", True,
                "продакшн" in str(debt.get("title")) and "hr.x" in str(debt.get("summary")))

    def debt_with(status):
        return [{"id": "hr.x", "status": "test"}, {"id": "hr.x-prod-debt", "status": status}]
    for status, expected_open in (("plan", True), ("test", True), ("wip", True),
                                  ("принято", False), ("done", False)):
        cases.check(f"debt_open: долг в «{status}» — {'открыт' if expected_open else 'закрыт'}",
                    expected_open, lib.debt_open(debt_with(status), "hr.x"))
    cases.check("debt_open: CLOSED_STATUSES взяты из cards.py, а не свои", True,
                all(not lib.debt_open(debt_with(s), "hr.x") for s in cards_lib.CLOSED_STATUSES))
    cases.check("debt_open: долга нет — False", False,
                lib.debt_open([{"id": "hr.x", "status": "wip"}], "hr.x"))


def judge_dependency_cases():
    """N-1: всё, что исполняет суд хакатона, запрещено хакатону — выводом, а не перечнем."""
    conf = str(write_conf(iso(NOW + dt.timedelta(hours=3))))
    parsed = judge_files_by_parsing(SOURCE)
    cases.check("N-1 разбор пробы: файлов суда > 0 и в нём pipeline.py, spec_approval.py, lib/cards.py",
                (True, []), (bool(parsed), [p for p in DERIVED_MINIMUM if p not in parsed]))
    judged = lib.judge_dependencies(SOURCE)
    cases.check("N-1 judge_dependencies(предмет): список не пуст", True, bool(judged))
    cases.check(f"N-1 judge_dependencies ⊇ разбор пробы ({len(parsed)} файлов): не выведено",
                [], [p for p in parsed if p not in judged])
    for path in sorted(set(parsed) | set(EXPLICIT_FORBIDDEN) | {"scripts/commit-msg-gate.sh"}):
        cases.check(f"N-1 touches_harness_guards: {path} запрещён", [path],
                    lib.touches_harness_guards([path], conf_path=conf))


def _judge_tree(name, modules):
    """Дерево суда в песочнице: корни суда, их зависимости — modules {имя: текст}."""
    root = WORK / name
    (root / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
    texts = {"scripts/check-pipeline.py": "import pipeline\n",
             "scripts/hackathon.py": "import cards\n",
             "scripts/pre-commit-gate.sh": 'python3 "$HERE/gate_helper.py"\n',
             "scripts/commit-msg-gate.sh": "true\n",
             "scripts/pipeline.py": "x = 1\n", "scripts/lib/cards.py": "y = 2\n",
             "scripts/gate_helper.py": "z = 3\n", **modules}
    for rel, text in texts.items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


class _ReadCounter:
    """Сколько раз за вызов открыт на чтение файл под корнем: пересчёт без открытия не бывает.

    Считает открытия (io.open — через него идёт Path.read_text), а не время и не
    внутренние имена предмета: кэш вправе звать stat, но не читать текст заново.
    """

    def __init__(self, root):
        self.root, self.count = str(Path(root).resolve()), 0

    def __enter__(self):
        self.saved = io.open
        def counting(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and os.path.abspath(file).startswith(self.root + os.sep):
                self.count += 1
            return self.saved(file, *args, **kwargs)
        io.open = builtins.open = counting
        return self

    def __exit__(self, *exc):
        io.open = builtins.open = self.saved


def judge_cache_cases():
    """Кэш judge_dependencies: на один корень — один разбор каждого файла, пока он тот же."""
    judge = load("hackathon_cache", SOURCE / "scripts" / "hackathon.py").judge_dependencies

    def call(root, spelled=None):
        with _ReadCounter(root) as reads:
            return judge(spelled if spelled is not None else root), reads.count

    # fresh.py лежит с начала, но на него никто не ссылается: в итог он попадёт
    # только правкой содержимого extra.py (КЭШ 5), а не появлением файла.
    first = _judge_tree("cache-a", {"scripts/pipeline.py": "import extra\n", "scripts/extra.py": "w = 4\n",
                                    "scripts/fresh.py": "u = 6\n"})
    second = _judge_tree("cache-b", {})
    deps_a, reads = call(first)
    cases.check("КЭШ 0: первый вызов на корень A читает файлы (счётчик жив) и видит extra.py",
                (True, True), (reads > 0, "scripts/extra.py" in deps_a))
    again, reads = call(first)
    cases.check("КЭШ 1: второй вызов на тот же корень — ни одного чтения, тот же итог",
                (0, deps_a), (reads, again))
    spelled, reads = call(first, str(first / "scripts" / ".."))
    cases.check("КЭШ 2: тот же корень другим написанием (строка, «scripts/..») — ключ один, чтений 0",
                (0, deps_a), (reads, spelled))
    deps_b, reads = call(second)
    cases.check("КЭШ 3: другой корень B — свой разбор и свой итог (без extra.py)",
                (True, False, True), (reads > 0, "scripts/extra.py" in deps_b, deps_b != deps_a))
    back, reads = call(first)
    cases.check("КЭШ 4: снова A после B — итог A, чтений 0 (кэш по корню, не одна ячейка)",
                (0, deps_a), (reads, back))

    (first / "scripts" / "extra.py").write_text("import fresh\nimport later\n", encoding="utf-8")
    grown, reads = call(first)
    cases.check("КЭШ 5: изменён файл-вход A (extra.py импортирует fresh) — пересчёт, fresh.py в итоге",
                (True, False), ("scripts/fresh.py" in grown, "scripts/later.py" in grown))
    (first / "scripts" / "later.py").write_text("v = 5\n", encoding="utf-8")
    born, reads = call(first)
    cases.check("КЭШ 6: появился файл, на который вход уже ссылался (later.py) — он в итоге",
                True, "scripts/later.py" in born)
    settled, reads = call(first)
    cases.check("КЭШ 7: после пересчёта — снова из кэша: чтений 0, итог тот же",
                (0, born), (reads, settled))

    empty = WORK / "cache-empty"
    (empty / "scripts").mkdir(parents=True)
    failures = []
    for _ in range(2):
        try:
            failures.append(("итог", judge(empty)))
        except RuntimeError:
            failures.append("отказ")
    cases.check("КЭШ 8: корень без корней суда — отказ и на повторе (отказ не кэшируется в «можно»)",
                ["отказ", "отказ"], failures)
    judge_cache_key_cases(judge)
    judge_cache_process_cases()


def judge_cache_key_cases(judge):
    """Ключ разбора видит каждую из трёх частей: mtime, размер, абсолютный путь.

    Каждый случай — правка, которую видит ровно одна часть ключа: ключ без неё
    отдаёт устаревший разбор. Деревья свои, чтобы чужие ключи не совпали случайно.
    """
    root = _judge_tree("cache-key", {"scripts/pipeline.py": "import aaaaa\n", "scripts/aaaaa.py": "a = 1\n",
                                     "scripts/bbbbb.py": "b = 1\n", "scripts/ccccccc.py": "c = 1\n"})
    entry = root / "scripts" / "pipeline.py"
    judge(root)
    entry.write_text("import bbbbb\n", encoding="utf-8")
    cases.check("КЭШ 5а: правка входа того же размера — пересчёт (ключ видит mtime)",
                True, "scripts/bbbbb.py" in judge(root))
    old = entry.stat()
    entry.write_text("import ccccccc\n", encoding="utf-8")
    os.utime(entry, ns=(old.st_atime_ns, old.st_mtime_ns))
    cases.check("КЭШ 5б: правка входа с прежним mtime (cp -p, грубые часы ФС) — пересчёт (ключ видит размер)",
                True, "scripts/ccccccc.py" in judge(root))

    # Ни одно дерево ещё не разбиралось, mtime назначен один и свой: совпадают
    # относительный путь, размер и mtime — различает только корень.
    same = 1_234_567_890_123_456_789
    pair = []
    for name, imported in (("cache-key-a", "aaaaa"), ("cache-key-b", "bbbbb")):
        tree = _judge_tree(name, {"scripts/pipeline.py": f"import {imported}\n",
                                  "scripts/aaaaa.py": "a = 1\n", "scripts/bbbbb.py": "b = 1\n"})
        os.utime(tree / "scripts" / "pipeline.py", ns=(same, same))
        pair.append(tree)
    deps = [judge(tree) for tree in pair]
    cases.check("КЭШ 3а: другой корень, тот же путь, размер и mtime — свой разбор (ключ видит корень)",
                (True, False, False, True),
                ("scripts/aaaaa.py" in deps[0], "scripts/bbbbb.py" in deps[0],
                 "scripts/aaaaa.py" in deps[1], "scripts/bbbbb.py" in deps[1]))


# Отдельный процесс считает свои открытия под корнем: так зовёт суд check-pipeline.py
# из гейта (judge_gate_cases — сотни процессов на одном корне песочницы).
COUNTED_JUDGE = """
import builtins, importlib.util, io, json, os, sys
source, root = sys.argv[1], os.path.realpath(sys.argv[2])
spec = importlib.util.spec_from_file_location("hackathon_counted", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
saved, reads = io.open, []
def counting(file, *args, **kwargs):
    if isinstance(file, (str, os.PathLike)) and os.path.abspath(file).startswith(root + os.sep):
        reads.append(str(file))
    return saved(file, *args, **kwargs)
io.open = builtins.open = counting
deps = module.judge_dependencies(root)
io.open = builtins.open = saved
print(json.dumps([len(reads), deps]))
"""


def judge_cache_process_cases():
    """Носитель кэша на прогон: каталог из HARNESS_JUDGE_CACHE, общий процессам прогона."""
    root = _judge_tree("cache-c", {})

    def run():
        done = subprocess.run([sys.executable, "-c", COUNTED_JUDGE,
                               str(SOURCE / "scripts" / "hackathon.py"), str(root)],
                              capture_output=True, text=True, timeout=120, cwd=WORK)
        if done.returncode != 0:
            return -1, done.stderr.strip()[-200:]
        return tuple(json.loads(done.stdout.strip().splitlines()[-1]))

    first_reads, deps = run()
    cases.check("КЭШ 9: процесс 1 на корень C — читает файлы, итог не пуст (счётчик жив)",
                (True, True), (first_reads > 0, bool(deps)))
    cases.check("КЭШ 10: процесс 2 того же прогона на корень C — чтений 0, тот же итог",
                (0, deps), run())
    with open(root / "scripts" / "pipeline.py", "a", encoding="utf-8") as handle:
        handle.write("# правка случая\n")
    cases.check("КЭШ 11: процесс 3 после правки ОДНОГО файла (комментарий) — прочитан ровно он, итог тот же",
                (1, deps), run())
    (root / "scripts" / "extra2.py").write_text("t = 7\n", encoding="utf-8")
    (root / "scripts" / "pipeline.py").write_text("import extra2\n", encoding="utf-8")
    cases.check("КЭШ 12: процесс 4 после правки входа (import extra2) — итог не устаревший",
                True, "scripts/extra2.py" in run()[1])


def cli_cases():
    future, past = NOW + dt.timedelta(hours=3), NOW - dt.timedelta(hours=3)
    write_conf(iso(future))
    cases.check("--активен: срок жив — код 0", 0, hackathon_cli("--активен")[0])
    write_conf(iso(past))
    cases.check("--активен: срок истёк — код 1", 1, hackathon_cli("--активен")[0])
    write_conf("")
    cases.check("--активен: срок пуст — код 1", 1, hackathon_cli("--активен")[0])

    for until_value, wip_depth, want, name in (
            (iso(future), "хакатон", 0, "срок жив и wip уровня хакатон — код 0"),
            (iso(future), "обычная", 1, "срок жив, wip обычная — код 1"),
            (iso(past), "хакатон", 1, "срок истёк, wip хакатон — код 1")):
        write_conf(until_value)
        probe_map = WORK / "active-map.yaml"
        probe_map.write_text(map_text(card("hr.w", "wip", wip_depth),
                                      card("hr.p", "plan", "обычная")), encoding="utf-8")
        cases.check(f"--активен --карта: {name}", want,
                    hackathon_cli("--активен", "--карта", str(probe_map))[0])

    write_conf(None)
    # Мерка — миг вызова, а не NOW начала пробы: под нагрузкой до этих случаев идут десятки минут.
    called = dt.datetime.now(TZ)
    rc, out = hackathon_cli("--включить")
    text = CONF.read_text(encoding="utf-8")
    values = until_values(text)
    cases.check("--включить: код 0", 0, rc)
    cases.check("--включить: строка ключа одна", 1, len(values))
    stamp = dt.datetime.fromisoformat(values[0]) if values else None
    cases.check("--включить: значение с поясом", True, bool(stamp and stamp.tzinfo))
    cases.check("--включить без часов: срок ≈ сейчас + 72 ч (±10 мин)", True,
                bool(stamp and stamp.tzinfo
                     and abs(stamp - (called + dt.timedelta(hours=72))) < dt.timedelta(minutes=10)))
    cases.check("--включить: ключ дописан, прочие строки и комментарий целы", True,
                bool(values) and 'OTHER_KEY="keep-me"' in text and "# образец конфига пробы" in text)
    copies = list((LOG_DIR / "панель-копии").glob("harness.conf.*"))
    cases.check("--включить: копия прежнего конфига в $LOG_DIR/панель-копии (И-1)", True,
                bool(copies))
    called = dt.datetime.now(TZ)
    hackathon_cli("--включить", "2")
    values = until_values(CONF.read_text(encoding="utf-8"))
    stamp = dt.datetime.fromisoformat(values[0]) if len(values) == 1 else None
    cases.check("--включить 2: строка одна, срок ≈ сейчас + 2 ч", True,
                bool(stamp and stamp.tzinfo
                     and abs(stamp - (called + dt.timedelta(hours=2))) < dt.timedelta(minutes=10)))


def enable_task_cases():
    probe_map = WORK / "enable-map.yaml"
    probe_map.write_text(map_text(card("hr.t1", "wip", "обычная"), card("hr.t2", "plan"),
                                  card("hr.t3", "plan", "мелкая")), encoding="utf-8")
    before = tasks_of(probe_map)
    rc, out = hackathon_cli("--включить-задачу", "hr.t1", "--карта", str(probe_map))
    after = tasks_of(probe_map)
    t1 = (by_id(probe_map, "hr.t1") or [{}])[0]
    cases.check("--включить-задачу: код 0", 0, rc)
    cases.check("--включить-задачу: уровень стал «хакатон», прочие ключи глубины целы",
                ("хакатон", "починка", "смена"),
                tuple((t1.get("глубина") or {}).get(key) for key in ("уровень", "характер", "кем")))
    cases.check("--включить-задачу: заведён ровно один долг", 1,
                len(by_id(probe_map, "hr.t1-prod-debt")))
    cases.check("--включить-задачу: задач стало на одну больше", len(before) + 1, len(after))
    cases.check("--включить-задачу: долг открыт", "plan",
                (by_id(probe_map, "hr.t1-prod-debt") or [{}])[0].get("status"))
    cases.check("--включить-задачу: долг записан, комментарий карты жив (правка текстом)",
                (True, True), (len(after) == len(before) + 1,
                               "якорь-комментарий" in probe_map.read_text(encoding="utf-8")))
    cases.check("--включить-задачу: долг записан, соседние карточки не тронуты",
                (True, [t for t in before if t["id"] != "hr.t1"]),
                (len(after) == len(before) + 1,
                 [t for t in after if t["id"] not in ("hr.t1", "hr.t1-prod-debt")]))
    once = probe_map.read_bytes()
    rc, out = hackathon_cli("--включить-задачу", "hr.t1", "--карта", str(probe_map))
    cases.check("повтор --включить-задачу: долг не задвоен", (1, len(before) + 1),
                (len(by_id(probe_map, "hr.t1-prod-debt")), len(tasks_of(probe_map))))
    # Отказ повтора тоже оставил бы долг один: судим код и байты, а не только счёт.
    cases.check("БОЛЬНОЙ СЛУЧАЙ повтор --включить-задачу: код 0, карта байт в байт прежняя",
                (0, True), (rc, probe_map.read_bytes() == once))
    hackathon_cli("--включить-задачу", "hr.t2", "--карта", str(probe_map))
    depth = ((by_id(probe_map, "hr.t2") or [{}])[0].get("глубина") or {})
    cases.check("--включить-задачу без глубины: {хакатон, разработка, hackathon.py, сегодня}",
                ("хакатон", "разработка", "hackathon.py", TODAY),
                (depth.get("уровень"), depth.get("характер"), depth.get("кем"),
                 str(depth.get("когда"))))
    frozen = probe_map.read_bytes()
    rc, out = hackathon_cli("--включить-задачу", "hr.net-takoj", "--карта", str(probe_map))
    cases.check("БОЛЬНОЙ СЛУЧАЙ: нет такой задачи — код ≠ 0, отказ называет id, карта не тронута",
                (True, True, True),
                (rc != 0, "hr.net-takoj" in out, probe_map.read_bytes() == frozen))

    # Лок: держим его сами — запись обязана ждать, а не идти мимо.
    cards_lib = load("cards_lock", SOURCE / "scripts" / "lib" / "cards.py")
    lock = Path(cards_lib.lock_path())
    if WORK not in lock.parents:
        raise SystemExit(f"ОТКАЗ пробы: путь лока карты вне песочницы ({lock}) — боевой лок не трогаю")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        frozen = probe_map.read_bytes()
        child = subprocess.Popen([sys.executable, str(SOURCE / "scripts" / "hackathon.py"),
                                  "--включить-задачу", "hr.t3", "--карта", str(probe_map)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=WORK)
        time.sleep(2)
        waited = child.poll() is None and probe_map.read_bytes() == frozen
        fcntl.flock(handle, fcntl.LOCK_UN)
    try:
        rc = child.wait(timeout=60)
    except subprocess.TimeoutExpired:
        child.kill()
        rc = "висит"
    cases.check("БОЛЬНОЙ СЛУЧАЙ: при занятом локе карты запись ждёт", True, waited)
    cases.check("после снятия лока запись прошла", (0, 1), (rc, len(by_id(probe_map, "hr.t3-prod-debt"))))


# Подмена записи: любой write, несущий id долга, пишет половину и падает — так
# выглядит обрыв (диск кончился, процесс убит) посреди записи новой карты.
FAULT_WRAPPER = r'''
import _io, builtins, io, os, runpy, sys
NEEDLE, MARK, SCRIPT = sys.argv[1], sys.argv[2], sys.argv[3]
real_open, real_fdopen, real_write = io.open, os.fdopen, os.write

def fire(data, write):
    write(data[:len(data) // 2])
    with real_open(MARK, "a", encoding="utf-8") as mark:
        mark.write("fired\n")
    raise OSError(28, "проба: запись оборвана посреди")

def carries(data):
    text = data if isinstance(data, str) else bytes(data).decode("utf-8", "replace")
    return NEEDLE in text

class Torn:
    def __init__(self, handle):
        self._handle = handle
    def write(self, data):
        if carries(data):
            fire(data, self._handle.write)
        return self._handle.write(data)
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)
    def __getattr__(self, name):
        return getattr(self._handle, name)

def writing(mode):
    return any(flag in mode for flag in "wax+")

def torn_open(file, mode="r", *args, **kwargs):
    handle = real_open(file, mode, *args, **kwargs)
    return Torn(handle) if writing(mode) else handle

def torn_fdopen(fd, mode="r", *args, **kwargs):
    handle = real_fdopen(fd, mode, *args, **kwargs)
    return Torn(handle) if writing(mode) else handle

def torn_write(fd, data):
    if carries(data):
        fire(data, lambda part: real_write(fd, part))
    return real_write(fd, data)

builtins.open = io.open = _io.open = torn_open
os.fdopen, os.write = torn_fdopen, torn_write
sys.argv = [SCRIPT, *sys.argv[4:]]
runpy.run_path(SCRIPT, run_name="__main__")
'''


def atomic_cases():
    """m-2: запись карты — временный файл и os.replace, обрыв не режет карту."""
    folder = WORK / "atomic"
    folder.mkdir()
    probe_map = folder / "dev-map.yaml"
    probe_map.write_text(map_text(card("hr.a1", "wip", "обычная"), card("hr.c1", "plan")),
                         encoding="utf-8")
    before_names, before_inode = sorted(os.listdir(folder)), probe_map.stat().st_ino
    rc, out = hackathon_cli("--включить-задачу", "hr.a1", "--карта", str(probe_map))
    cases.check("m-2 --включить-задачу: код 0, долг записан", (0, 1),
                (rc, len(by_id(probe_map, "hr.a1-prod-debt"))))
    cases.check("m-2 запись заменой файла: inode карты новый (os.replace, а не запись поверх)",
                True, rc == 0 and probe_map.stat().st_ino != before_inode)
    cases.check("m-2 после записи в каталоге карты нет хвостовых tmp-файлов",
                before_names, sorted(os.listdir(folder)))

    wrapper, mark = WORK / "torn_write.py", WORK / "torn.mark"
    wrapper.write_text(FAULT_WRAPPER, encoding="utf-8")
    frozen = probe_map.read_bytes()
    done = subprocess.run([sys.executable, str(wrapper), "hr.c1-prod-debt", str(mark),
                           str(SOURCE / "scripts" / "hackathon.py"),
                           "--включить-задачу", "hr.c1", "--карта", str(probe_map)],
                          capture_output=True, text=True, timeout=60, cwd=WORK)
    fired = mark.exists()
    cases.check("m-2 подмена записи сработала (обрыв случился, иначе судить нечего)", True, fired)
    cases.check("БОЛЬНОЙ СЛУЧАЙ m-2: обрыв посреди записи — код ≠ 0, карта байт в байт прежняя",
                (True, True, True),
                (fired, done.returncode != 0, probe_map.read_bytes() == frozen))


def check_map_cases():
    variants = (
        ("карточка хакатона в wip с открытым долгом — 0", 0,
         [card("hr.h", "wip", "хакатон"), card("hr.h-prod-debt", "plan")]),
        ("БОЛЬНОЙ СЛУЧАЙ: done при открытом долге — 1", 1,
         [card("hr.h", "done", "хакатон", DONE_EXTRA), card("hr.h-prod-debt", "test")]),
        ("долг принят — done разрешён, 0", 0,
         [card("hr.h", "done", "хакатон", DONE_EXTRA),
          card("hr.h-prod-debt", "принято", extra=ACCEPTED_EXTRA)]),
        ("БОЛЬНОЙ СЛУЧАЙ: карточка хакатона без долга — 1", 1,
         [card("hr.h", "test", "хакатон")]),
        ("карточек хакатона нет — 0", 0, [card("hr.o", "done", "обычная", DONE_EXTRA)]),
    )
    for name, want, cards_text in variants:
        probe_map = WORK / "check-map.yaml"
        probe_map.write_text(map_text(*cards_text), encoding="utf-8")
        rc, out = hackathon_cli("--проверить-карту", str(probe_map))
        cases.check(f"--проверить-карту: {name}", want, rc)
        if "без долга" in name:
            cases.check("  отказ — подсказка «hackathon.py --включить-задачу hr.h»", True,
                        "hackathon.py --включить-задачу hr.h" in out)
        elif want == 1:
            cases.check("  отказ называет долг hr.h-prod-debt и слово «долг»", True,
                        "hr.h-prod-debt" in out and "долг" in out)


# ── гейт пайплайна в своём git-репозитории ────────────────────────────────────
REPO = WORK / "repo"


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), "-c", "user.name=proba", "-c", "user.email=p@p",
                           *args], capture_output=True, text=True, timeout=60)


def build_repo():
    (REPO / "scripts" / "hooks").mkdir(parents=True)
    # *.sh тоже: judge_dependencies песочницы выводит запреты из pre-commit-gate.sh.
    for source in [*(SOURCE / "scripts").glob("*.py"), *(SOURCE / "scripts").glob("*.sh")]:
        shutil.copy2(source, REPO / "scripts" / source.name)
    shutil.copytree(SOURCE / "scripts" / "lib", REPO / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(SOURCE / "scripts" / "hooks" / "guard_bash.py", REPO / "scripts" / "hooks")
    (REPO / "harness" / "config").mkdir(parents=True)
    shutil.copy2(SOURCE / "harness" / "config" / "pipeline.yaml", REPO / "harness" / "config")
    if FORBIDDEN_YAML.exists():
        shutil.copy2(FORBIDDEN_YAML, REPO / "harness" / "config")
    (REPO / "docs").mkdir()
    (REPO / "docs" / "x.md").write_text("безобидный файл\n", encoding="utf-8")
    # Бумаги шагов wip хакатона (спека и ревью спеки): общие проверки им проходимы.
    (REPO / "docs" / "spec.md").write_text("спека случая\n", encoding="utf-8")
    (REPO / "docs" / "spec-REVIEW.md").write_text("ревью спеки случая\n", encoding="utf-8")
    (REPO / "dev-map.yaml").write_text(map_text(card("hr.p", "plan", "обычная")), encoding="utf-8")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "стенд")


STARTED = f"when: {{ started: \"{TODAY}\" }}"
TRAILER_W = "правка случая\n\nDev-Map: hr.w\n"


def wip_card(depth, status="wip", spec=True):
    """Карточка hr.w для гейта: характер «разработка» шагов не сужает."""
    extra = (STARTED, 'spec: "docs/spec.md"') if spec else (STARTED,)
    return card("hr.w", status, depth, extra, character="разработка")


def pipeline_run(index_card, staged_path, until_value, worktree_card=None, automation="full"):
    """Карта с index_card уходит в HEAD, правка staged_path — в индекс.

    worktree_card — карта рабочего дерева, расходящаяся с индексом (M-2): гейт
    обязан судить индекс. automation — AUTOMATION_LEVEL конфига; None — ключа нет
    (уровень «частичный», слово владельца требуется)."""
    write_conf(until_value, extra=(f'AUTOMATION_LEVEL="{automation}"',) if automation else ())
    (REPO / "dev-map.yaml").write_text(map_text(index_card, card("hr.p", "plan", "обычная")),
                                       encoding="utf-8")
    git("add", "dev-map.yaml")
    git("commit", "-qm", "карта случая")
    (REPO / staged_path).parent.mkdir(parents=True, exist_ok=True)
    with open(REPO / staged_path, "a", encoding="utf-8") as handle:
        handle.write("# правка случая\n")
    git("add", staged_path)
    if worktree_card is not None:
        (REPO / "dev-map.yaml").write_text(map_text(worktree_card, card("hr.p", "plan", "обычная")),
                                           encoding="utf-8")
    # Коммит своей карточки — обе стадии git: суд хакатона ведёт commit-msg по трейлеру (N-4).
    rc, out = gate_stage([])
    if rc == 0:
        (WORK / "commit-message.txt").write_text(TRAILER_W, encoding="utf-8")
        rc, more = gate_stage(["--commit-msg", str(WORK / "commit-message.txt")])
        out += more
    git("commit", "-qm", "правка случая")
    return rc, out


def pipeline_cases():
    build_repo()
    steps = ((yaml.safe_load((REPO / "harness/config/pipeline.yaml").read_text(encoding="utf-8"))
              or {}).get("шаги") or {}).get("хакатон")
    cases.check(f"M-1 pipeline.yaml: шаги.хакатон = {HACKATHON_STEPS}", HACKATHON_STEPS, steps)
    future, past = iso(NOW + dt.timedelta(hours=3)), iso(NOW - dt.timedelta(hours=3))
    hackathon_card = wip_card("хакатон")

    rc, out = pipeline_run(hackathon_card, "docs/x.md", past)
    cases.check("гейт: хакатон после срока — код 1", 1, rc)
    cases.check("гейт: отказ говорит «срок хакатона»", True, "срок хакатона" in out)
    rc, out = pipeline_run(hackathon_card, "docs/x.md", "")
    cases.check("гейт: хакатон, срок пуст — код 1 со словами о сроке", (1, True),
                (rc, "срок хакатона" in out))
    rc, out = pipeline_run(hackathon_card, "scripts/hooks/guard_bash.py", future)
    cases.check("БОЛЬНОЙ СЛУЧАЙ гейт: хакатон задел guard_bash.py — код 1", 1, rc)
    cases.check("гейт: отказ словами «хакатон не трогает сторожей/гейты/хуки» и называет файл",
                True, "хакатон не трогает сторожей/гейты/хуки" in out
                and "scripts/hooks/guard_bash.py" in out)
    rc, out = pipeline_run(hackathon_card, "docs/x.md", future)
    want_line = "шаги: " + ", ".join(steps or ["<шаги.хакатон пусты>"])
    cases.check(f"гейт: срок жив, безобидный файл — код 0 и «{want_line}»", (0, True),
                (rc, bool(steps) and want_line in out))

    # C-1: каждая маска данных — отказ гейта при живом сроке.
    files = harness_files()
    masks = forbidden_masks_of_source()
    cases.check("C-1 гейт: масок для суда разобрано > 0", True, bool(masks))
    for mask in masks:
        path = concrete_path(mask, files)
        if path is None:
            cases.check(f"C-1 гейт: под маской {mask} есть файл предмета", True, False)
            continue
        rc, out = pipeline_run(hackathon_card, path, future)
        cases.check(f"C-1 гейт: маска {mask} → {path} — код 1, отказ хакатона называет файл",
                    (1, True), (rc, "хакатон не трогает сторожей/гейты/хуки" in out and path in out))

    # C-1: пустой список запретов — отказ гейта, а не «можно всё». Гейт читает
    # файл из индекса песочницы (n-3), поэтому порча кладётся в индекс.
    sandbox_forbidden = REPO / "harness" / "config" / "hackathon-forbidden.yaml"
    for name, text in (("файл пуст", ""), ("paths: []", "paths: []\n")):
        sandbox_forbidden.write_text(text, encoding="utf-8")
        git("add", "harness/config/hackathon-forbidden.yaml")
        rc, out = pipeline_run(hackathon_card, "docs/x.md", future)
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ C-1 гейт: hackathon-forbidden.yaml {name} — код 1, «шаги:» нет",
                    (1, False), (rc, "шаги:" in out))
    shutil.copy2(FORBIDDEN_YAML, sandbox_forbidden)
    git("add", "harness/config/hackathon-forbidden.yaml")
    git("commit", "-qm", "список запретов возвращён")

    # M-1/M-2: общие проверки до суда хакатона, а не ранний выход.
    rc, out = pipeline_run(wip_card("хакатон", spec=False), "docs/x.md", future)
    cases.check("M-1 гейт: хакатон без поля spec: — код 1, отказ про spec:", (1, True),
                (rc, "spec:" in out))
    rc, out = pipeline_run(hackathon_card, "docs/x.md", future, automation=None)
    cases.check("M-2 гейт: хакатон, уровень «частичный», спека не отправлена — код 1 «НЕ ОТПРАВЛЕНА»",
                (1, True), (rc, "НЕ ОТПРАВЛЕНА" in out))

    # M-2 случай F: уровень — из индекса, рабочее дерево карты гейт не судит.
    rc, out = pipeline_run(card("hr.w", "test", "обычная", (STARTED,)), "docs/x.md", future,
                           worktree_card=wip_card("хакатон", spec=False))
    cases.check("M-2 случай F1: индекс обычная/test, дерево хакатон/wip — судится как обычная",
                (0, True, False), (rc, "спека не требуется" in out, "хакатон" in out))
    rc, out = pipeline_run(hackathon_card, "scripts/hooks/guard_bash.py", future,
                           worktree_card=wip_card("обычная"))
    cases.check("M-2 случай F2: индекс хакатон/wip, дерево обычная/wip, задет сторож — отказ хакатона",
                (1, True), (rc, "хакатон не трогает сторожей/гейты/хуки" in out))
    # Бумаги судятся по карточке индекса тоже: spec: только в рабочем дереве не в счёт.
    rc, out = pipeline_run(wip_card("хакатон", spec=False), "docs/x.md", future,
                           worktree_card=hackathon_card)
    cases.check("M-2 случай F3: индекс хакатон без spec:, дерево хакатон со spec: — код 1, отказ про spec:",
                (1, True), (rc, "spec:" in out))

    # Шаги — данные пульта, а не строка в коде: пульт песочницы меняем на [тесты].
    console = REPO / "harness" / "config" / "pipeline.yaml"
    data = yaml.safe_load(console.read_text(encoding="utf-8")) or {}
    data.setdefault("шаги", {})["хакатон"] = ["тесты"]
    console.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    git("add", "harness/config/pipeline.yaml")
    git("commit", "-qm", "пульт случая")
    rc, out = pipeline_run(hackathon_card, "docs/x.md", future)
    cases.check("гейт: шаги берутся из pipeline.yaml (пульт [тесты] → «шаги: тесты»)",
                (0, True, False), (rc, "шаги: тесты" in out, "ревью_кода" in out))

    # Обычная задача судится как прежде: эти два случая зелёные и до кода.
    plain = card("hr.w", "wip", "обычная", (STARTED,))
    rc, out = pipeline_run(plain, "scripts/hooks/guard_bash.py", future)
    cases.check("НЕИЗМЕННО: обычная задача, задет guard_bash.py — код 0, без слова «хакатон»",
                (0, False), (rc, "хакатон" in out))
    rc, out = pipeline_run(plain, "docs/x.md", future)
    cases.check("НЕИЗМЕННО: обычная задача, безобидный файл — «спека не требуется»",
                (0, True), (rc, "спека не требуется" in out))


REFUSAL = "хакатон не трогает сторожей/гейты/хуки"
PLAIN_PLAN = card("hr.p", "plan", "обычная")
DOOR_ENV = {"HARNESS_PIPELINE_FORCE": "1",
            "HARNESS_PIPELINE_WHY": "проба N-2: дверь общих проверок открыта"}


def gate_run(head_cards, index_cards, staged, until_value, door=False, message=None,
             pre_commit=True):
    """HEAD несёт head_cards, индекс — index_cards и правки staged.

    Стадии — как у git: pre-commit, затем, если он пропустил и message дан,
    commit-msg с этим сообщением. door — честная дверь общих проверок открыта
    (суд хакатона она открывать не вправе). pre_commit=False — судится одна
    стадия commit-msg (общие проверки закрытия здесь не предмет)."""
    write_conf(until_value, extra=('AUTOMATION_LEVEL="full"',))
    (REPO / "dev-map.yaml").write_text(map_text(*head_cards), encoding="utf-8")
    git("add", "dev-map.yaml")
    git("commit", "-qm", "карта случая")
    (REPO / "dev-map.yaml").write_text(map_text(*index_cards), encoding="utf-8")
    git("add", "dev-map.yaml")
    for path in staged:
        (REPO / path).parent.mkdir(parents=True, exist_ok=True)
        with open(REPO / path, "a", encoding="utf-8") as handle:
            handle.write("# правка случая\n")
        git("add", path)
    # door — True (дверь DOOR_ENV) или словарь своей двери (N-5: своя причина, FORCE без причины).
    env = {**os.environ, **(door if isinstance(door, dict) else DOOR_ENV if door else {})}
    rc, out = gate_stage([], env) if pre_commit else (0, "")
    if rc == 0 and message is not None:
        message_file = WORK / "commit-message.txt"
        message_file.write_text(message, encoding="utf-8")
        rc, more = gate_stage(["--commit-msg", str(message_file)], env)
        out += more
    git("commit", "-qm", "правка случая")
    return rc, out


def gate_stage(args, env=None):
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "check-pipeline.py"), *args],
                          capture_output=True, text=True, timeout=120, cwd=REPO,
                          env=env or dict(os.environ))
    return done.returncode, done.stdout + done.stderr


def refused(result, path):
    rc, out = result
    return rc, REFUSAL in out and path in out


def refused_lines(result, paths):
    """Отказ хакатона и пути отказа, которых нет отдельной строкой «    <путь>».

    Строка целиком, не подстрока: «scripts/lib/x.py» иначе засчитался бы в
    строке «scripts/lib/x.py.bak», а один путь — в перечне чужих."""
    rc, out = result
    lines = set(out.splitlines())
    return rc, REFUSAL in out, [path for path in paths if f"    {path}" not in lines]


def says_hackathon(result):
    rc, out = result
    return rc, "хакатон" in out


def entry_class(path, masks):
    """Класс входа файла суда — признаки, по которым гейт узнаёт запрет.

    Признаки разбором предмета (корни суда, явные запреты, GUARD_PATHS, маски
    данных), а не списком имён: новый класс появляется сам."""
    guarded_path = any(fnmatch.fnmatch(path, mask + "*" if mask.endswith("/") else mask)
                       for mask in GUARD_PATHS.split())
    return (Path(path).suffix, str(Path(path).parent), path in JUDGE_ROOTS,
            path in EXPLICIT_FORBIDDEN, guarded_path,
            any(fnmatch.fnmatch(path, mask) for mask in masks))


def judge_gate_cases():
    """N-1 в гейте: на каждый файл суда — отказ хакатона при живом сроке.

    Каждый файл судится ОДНИМ коммитом со всеми, а не своим: шаг гейта — два
    процесса python (~0.6 с), и 440 поштучных коммитов съедали 260 с из 360 с
    предела живой проверки. Поштучно каждый файл держит библиотечный уровень
    (judge_dependency_cases: touches_harness_guards на каждый путь); здесь гейт
    обязан назвать КАЖДЫЙ файл своей строкой отказа. Одиночный коммит — по
    представителю на класс входа: так виден отказ, который зависит от соседей."""
    future = iso(NOW + dt.timedelta(hours=3))
    hackathon_card = wip_card("хакатон")
    cards_pair = [hackathon_card, PLAIN_PLAN]
    files = sorted(set(judge_files_by_parsing(SOURCE)) | set(EXPLICIT_FORBIDDEN))
    cases.check("N-1 гейт: файлов суда для прогона > 0", True, bool(files))
    cases.check(f"N-1 гейт: все {len(files)} файлов суда одним коммитом — код 1, отказ хакатона "
                "называет КАЖДЫЙ файл своей строкой", (1, True, []),
                refused_lines(gate_run(cards_pair, cards_pair, files, future, message=TRAILER_W),
                              files))
    masks = forbidden_masks_of_source()
    classes = {}
    for path in files:
        classes.setdefault(entry_class(path, masks), path)
    cases.check("N-1 гейт: классов входа разобрано > 0", True, bool(classes))
    for path in classes.values():
        cases.check(f"N-1 гейт: {path} (класс входа) — код 1, отказ хакатона называет файл",
                    (1, True, []),
                    refused_lines(gate_run(cards_pair, cards_pair, [path], future,
                                           message=TRAILER_W), [path]))

    # Живой случай ревьюера B: сверка путей отключена тем же коммитом, что трогает deploy.sh.
    target = REPO / "scripts" / "pipeline.py"
    original = target.read_text(encoding="utf-8")
    head = "def _под_путями(путь: str, список: str) -> bool:\n"
    target.write_text(original.replace(head, head + "    return False\n", 1), encoding="utf-8")
    cases.check("N-1 случай B: «return False» лёг первой строкой _под_путями", True, head in original)
    # Сломанная сверка масок deploy.sh уже не узнаёт: отказ обязан назвать сам pipeline.py.
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-1 B: pipeline.py без сверки путей + deploy.sh — код 1, отказ называет pipeline.py",
                (1, True),
                refused(gate_run([hackathon_card, PLAIN_PLAN], [hackathon_card, PLAIN_PLAN],
                                 ["scripts/pipeline.py", "scripts/deploy.sh"], future,
                                 message=TRAILER_W),
                        "scripts/pipeline.py"))
    target.write_text(original, encoding="utf-8")
    git("add", "scripts/pipeline.py")
    git("commit", "-qm", "сверка путей возвращена")


def trailer_cases():
    """N-2/N-4: суд хакатона ведёт трейлер «Dev-Map: <id>» на стадии commit-msg, и только он."""
    future, past = iso(NOW + dt.timedelta(hours=3)), iso(NOW - dt.timedelta(hours=3))
    h_wip, h_test = wip_card("хакатон"), wip_card("хакатон", status="test")
    o_wip = card("hr.w", "wip", "обычная", (STARTED,))
    o_test = card("hr.w", "test", "обычная", (STARTED,))
    other_wip = card("hr.v", "wip", "обычная", (STARTED,), character="разработка")
    deploy = "scripts/deploy.sh"
    to_v = "правка случая\n\nDev-Map: hr.v\n"

    closing = ([h_wip, PLAIN_PLAN], [h_test, PLAIN_PLAN])
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-2 E1: wip→test хакатона + deploy.sh, трейлер hr.w, дверь открыта — "
                "код 1, отказ хакатона",
                (1, True), refused(gate_run(*closing, [deploy], future, door=True, message=TRAILER_W),
                                   deploy))
    rc, out = gate_run(*closing, ["docs/x.md"], past, door=True, message=TRAILER_W)
    cases.check("N-2 E2: wip→test хакатона с docs/x.md после срока, трейлер hr.w — код 1 «срок хакатона»",
                (1, True), (rc, "срок хакатона" in out))
    two = ([h_wip, other_wip, PLAIN_PLAN], [h_wip, other_wip, PLAIN_PLAN])
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-2 E3: две wip (одна хакатон) + deploy.sh, трейлер hr.w, дверь — "
                "код 1, отказ хакатона",
                (1, True), refused(gate_run(*two, [deploy], future, door=True, message=TRAILER_W),
                                   deploy))
    cases.check("N-2 E6: уровень хакатон→обычная тем же коммитом + deploy.sh, трейлер hr.w, дверь — "
                "код 1, отказ хакатона", (1, True),
                refused(gate_run([h_wip, PLAIN_PLAN], [o_wip, PLAIN_PLAN], [deploy], future,
                                 door=True, message=TRAILER_W), deploy))

    # N-4 (1): чужой коммит при карточке хакатона в wip судится как без хакатона.
    # Двойник — тот же коммит, где hr.w обычная: его код — мерка «как обычный».
    twin = ([o_wip, other_wip, PLAIN_PLAN], [o_wip, other_wip, PLAIN_PLAN])
    for name, staged, until_value, message in (
            ("срок вышел, deploy.sh, трейлер hr.v", deploy, past, to_v),
            ("срок вышел, docs/x.md, без трейлера", "docs/x.md", past, "правка случая\n"),
            ("режим выключен, docs/x.md, трейлер hr.v", "docs/x.md", "", to_v),
            ("срок жив, scripts/lib/cards.py, трейлер hr.v", "scripts/lib/cards.py", future, to_v)):
        twin_rc = gate_run(*twin, [staged], until_value, message=message)[0]
        rc, out = gate_run(*two, [staged], until_value, message=message)
        cases.check(f"N-4 (1) двойник без хакатона ({name}) — код 0", 0, twin_rc)
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ N-4 (1): хакатон hr.w в wip, чужой коммит ({name}) — код как у "
                    "двойника, суда хакатона нет", (twin_rc, False, False),
                    (rc, "срок хакатона" in out, REFUSAL in out))

    # N-4 (2): тот же коммит, названный трейлером карточки хакатона, — отказ по сроку.
    for staged in ("docs/x.md", deploy):
        rc, out = gate_run(*two, [staged], past, message=TRAILER_W)
        cases.check(f"N-4 (2): хакатон hr.w в wip, срок вышел, трейлер hr.w, {staged} — код 1 "
                    "«срок хакатона»", (1, True), (rc, "срок хакатона" in out))

    # N-4 (3): дверь выхода — только карта, карточка выходит из хакатона; FORCE не нужен.
    debt = card("hr.w-prod-debt", "plan")
    lowering = ([h_wip, debt, PLAIN_PLAN], [o_wip, debt, PLAIN_PLAN])
    for name, until_value in (("срок вышел", past), ("режим выключен", "")):
        rc, out = gate_run(*lowering, [], until_value, message=TRAILER_W)
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ N-4 (3): дверь выхода — уровень хакатон→обычная, только карта, "
                    f"трейлер hr.w, {name}, без FORCE — код 0", (0, False), (rc, "срок хакатона" in out))
    rc, out = gate_run([h_wip, debt, PLAIN_PLAN], [h_test, debt, PLAIN_PLAN], [], past,
                       message=TRAILER_W, pre_commit=False)
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-4 (3): дверь выхода — wip→test при открытом долге, только карта, "
                "трейлер hr.w, срок вышел — commit-msg код 0", (0, False), (rc, "срок хакатона" in out))

    # N-4 (4): дверь не шире выхода — путь кроме карты или карточка осталась хакатоном в wip.
    rc, out = gate_run([h_wip, debt, PLAIN_PLAN], [o_wip, debt, PLAIN_PLAN], ["docs/x.md"], past,
                       message=TRAILER_W)
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-4 (4): понижение уровня + docs/x.md после срока, трейлер hr.w — "
                "код 1 «срок хакатона»", (1, True), (rc, "срок хакатона" in out))
    rc, out = gate_run([h_wip, debt, PLAIN_PLAN], [h_wip, card("hr.w-prod-debt", "wip"), PLAIN_PLAN],
                       [], past, message=TRAILER_W, pre_commit=False)
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-4 (4): только карта, hr.w осталась хакатоном в wip, срок вышел, "
                "трейлер hr.w — код 1 «срок хакатона»", (1, True), (rc, "срок хакатона" in out))
    cases.check("N-4 (4): дверь выхода не снимает запреты — понижение уровня + deploy.sh, срок жив, "
                "трейлер hr.w — код 1, отказ хакатона", (1, True),
                refused(gate_run([h_wip, debt, PLAIN_PLAN], [o_wip, debt, PLAIN_PLAN], [deploy],
                                 future, message=TRAILER_W), deploy))

    # N-4 (5): при живом сроке трейлер хакатона и файл суда — отказ (N-1 цел и на стадии commit-msg).
    cases.check("N-4 (5): две wip, срок жив, трейлер hr.w, scripts/lib/cards.py — код 1, отказ хакатона",
                (1, True), refused(gate_run(*two, ["scripts/lib/cards.py"], future, message=TRAILER_W),
                                   "scripts/lib/cards.py"))

    # Трейлер: карточка хакатона в карте не меняется и не в wip — узнать её можно
    # только из сообщения коммита, то есть на стадии commit-msg.
    still = ([h_test, PLAIN_PLAN], [h_test, PLAIN_PLAN])
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-2 E4: commit-msg, трейлер хакатона, 0 wip, deploy.sh — код 1, отказ хакатона",
                (1, True), refused(gate_run(*still, [deploy], future, message=TRAILER_W,
                                            pre_commit=False), deploy))
    rc, out = gate_run(*still, ["docs/x.md"], past, message=TRAILER_W, pre_commit=False)
    cases.check("N-2 E4: commit-msg, трейлер хакатона после срока — код 1 «срок хакатона»", (1, True),
                (rc, "срок хакатона" in out))
    cases.check("N-2 E4: commit-msg, трейлер хакатона, безобидный файл, срок жив — код 0", 0,
                gate_run(*still, ["docs/x.md"], future, message=TRAILER_W, pre_commit=False)[0])
    rc, out = gate_run(*still, [deploy], future, message="правка случая\n\nDev-Map: hr.p\n",
                       pre_commit=False)
    cases.check("НЕИЗМЕННО N-2 E4: commit-msg, трейлер обычной карточки, deploy.sh — код 0 без «хакатон»",
                (0, False), (rc, "хакатон" in out))
    # Без трейлера при живом сроке и карточке хакатона — отказ N-5 (round5_cases);
    # здесь карточек хакатона нет, и режим не активен.
    rc, out = gate_run([o_test, PLAIN_PLAN], [o_test, PLAIN_PLAN], [deploy], future,
                       message="правка случая без трейлера\n", pre_commit=False)
    cases.check("НЕИЗМЕННО N-2 E4: commit-msg без трейлера, карточек хакатона нет, deploy.sh — "
                "код 0 без «хакатон»", (0, False), (rc, "хакатон" in out))

    # E5: настоящий git commit через хук commit-msg — стадия подключена, а не только написана.
    hook = REPO / ".git" / "hooks" / "commit-msg"
    hook.unlink(missing_ok=True)
    hook.symlink_to("../../scripts/commit-msg-gate.sh")
    gate_run(*still, [], future)
    with open(REPO / deploy, "a", encoding="utf-8") as handle:
        handle.write("# правка случая E5\n")
    git("add", deploy)
    (WORK / "e5-message.txt").write_text(TRAILER_W, encoding="utf-8")
    before = git("rev-parse", "HEAD").stdout
    done = git("commit", "-q", "-F", str(WORK / "e5-message.txt"))
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-2 E5: git commit с хуком commit-msg, трейлер хакатона + deploy.sh — "
                "коммит не встал", (True, True),
                (done.returncode != 0, git("rev-parse", "HEAD").stdout == before))
    hook.unlink()
    git("commit", "-qm", "уборка E5")

    # Обычные задачи — как прежде, и с задетой картой.
    cases.check("НЕИЗМЕННО N-2: обычная wip→test + guard_bash.py, дверь — код 0 без «хакатон»", (0, False),
                says_hackathon(gate_run([o_wip, PLAIN_PLAN], [o_test, PLAIN_PLAN],
                                        ["scripts/hooks/guard_bash.py"], future, door=True)))
    cases.check("НЕИЗМЕННО N-2: две обычные wip + guard_bash.py, дверь — код 0 без «хакатон»", (0, False),
                says_hackathon(gate_run([o_wip, other_wip, PLAIN_PLAN], [o_wip, other_wip, PLAIN_PLAN],
                                        ["scripts/hooks/guard_bash.py"], future, door=True)))


NO_TRAILER = "правка случая без трейлера\n"
TO_P = "правка случая\n\nDev-Map: hr.p\n"
BYPASS_LOG = LOG_DIR / "пайплайн-обходы.jsonl"


def bypass_rows():
    """Строки журнала обходов песочницы; битая строка — пропуск, журнала нет — пусто."""
    try:
        lines = BYPASS_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def require_trailer_cases():
    """N-5: пока режим активен, коммит без трейлера Dev-Map не встаёт без двери с причиной."""
    future, past = iso(NOW + dt.timedelta(hours=3)), iso(NOW - dt.timedelta(hours=3))
    h_wip, o_wip = wip_card("хакатон"), wip_card("обычная")
    active = ([h_wip, PLAIN_PLAN], [h_wip, PLAIN_PLAN])
    for staged in ("docs/x.md", "scripts/pipeline.py"):
        rc, out = gate_run(*active, [staged], future, message=NO_TRAILER, pre_commit=False)
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ N-5: режим активен, {staged}, без трейлера — код 1, подсказка "
                    "«Dev-Map: hr.w» и дверь HARNESS_PIPELINE_WHY", (1, True, True),
                    (rc, "Dev-Map: hr.w" in out, "HARNESS_PIPELINE_WHY" in out))
    rc, out = gate_run([h_wip, PLAIN_PLAN], [o_wip, PLAIN_PLAN], ["docs/x.md"], future,
                       message=NO_TRAILER, pre_commit=False)
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-5: хакатон только в HEAD (уровень понижен в индексе), срок жив, "
                "без трейлера — код 1", (1, True), (rc, "Dev-Map: hr.w" in out))

    why = f"проба N-5: коммит без трейлера {NOW.strftime('%H%M%S')}"
    before = len(bypass_rows())
    rc, out = gate_run(*active, ["docs/x.md"], future, message=NO_TRAILER, pre_commit=False,
                       door={"HARNESS_PIPELINE_FORCE": "1", "HARNESS_PIPELINE_WHY": why})
    fresh = bypass_rows()[before:]
    cases.check("N-5 дверь: FORCE и причина — код 0, в журнале обходов строка с причиной и id hr.w",
                (0, True), (rc, any(row.get("почему") == why and "hr.w" in str(row.get("задача"))
                                    for row in fresh)))
    before = len(bypass_rows())
    rc, out = gate_run(*active, ["docs/x.md"], future, message=NO_TRAILER, pre_commit=False,
                       door={"HARNESS_PIPELINE_FORCE": "1", "HARNESS_PIPELINE_WHY": ""})
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-5 дверь без причины — код 1, журнал обходов не пополнен",
                (1, 0), (rc, len(bypass_rows()) - before))

    for name, until_value, cards_pair in (
            ("срок вышел", past, active), ("срок пуст", "", active),
            ("карточек хакатона нет, срок жив", future, ([o_wip, PLAIN_PLAN], [o_wip, PLAIN_PLAN]))):
        rc, out = gate_run(*cards_pair, ["docs/x.md"], until_value, message=NO_TRAILER,
                           pre_commit=False)
        cases.check(f"N-5 режим не активен ({name}): без трейлера — код 0, подсказки трейлера нет",
                    (0, False), (rc, "Dev-Map: hr.w" in out))

    # Живой git commit через хук commit-msg: случай 6b ревью (pipeline.py без трейлера).
    hook = REPO / ".git" / "hooks" / "commit-msg"
    hook.unlink(missing_ok=True)
    hook.symlink_to("../../scripts/commit-msg-gate.sh")
    gate_run(*active, [], future)
    with open(REPO / "scripts" / "pipeline.py", "a", encoding="utf-8") as handle:
        handle.write("# правка случая 6b\n")
    git("add", "scripts/pipeline.py")
    (WORK / "6b-message.txt").write_text(NO_TRAILER, encoding="utf-8")
    head = git("rev-parse", "HEAD").stdout
    done = git("commit", "-q", "-F", str(WORK / "6b-message.txt"))
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-5 6b: git commit с хуком, режим активен, pipeline.py без трейлера — "
                "коммит не встал", (True, True),
                (done.returncode != 0, git("rev-parse", "HEAD").stdout == head))
    hook.unlink()
    git("commit", "-qm", "уборка 6b")


def hooked_commit(repo, staged, message, name):
    """Настоящий git commit через хук commit-msg: (код git, HEAD сдвинулся)."""
    run = ["git", "-C", str(repo), "-c", "user.name=proba", "-c", "user.email=p@p"]
    hook = repo / ".git" / "hooks" / "commit-msg"
    hook.unlink(missing_ok=True)
    hook.symlink_to("../../scripts/commit-msg-gate.sh")
    try:
        with open(repo / staged, "a", encoding="utf-8") as handle:
            handle.write(f"# правка случая {name}\n")
        subprocess.run([*run, "add", staged], capture_output=True, timeout=60)
        (WORK / f"{name}-message.txt").write_text(message, encoding="utf-8")
        head = subprocess.run([*run, "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=60).stdout
        done = subprocess.run([*run, "commit", "-q", "-F", str(WORK / f"{name}-message.txt")],
                              capture_output=True, text=True, timeout=120)
        moved = subprocess.run([*run, "rev-parse", "HEAD"], capture_output=True, text=True,
                               timeout=60).stdout != head
        return done.returncode, moved
    finally:
        hook.unlink()


def build_mapless_repo():
    """Репозиторий без dev-map.yaml (как у самотеста раскладчика): скрипты и пульт песочницы."""
    bare = WORK / "nomap"
    shutil.copytree(REPO / "scripts", bare / "scripts", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "harness", bare / "harness")
    (bare / "docs").mkdir()
    (bare / "docs" / "x.md").write_text("безобидный файл\n", encoding="utf-8")
    run = ["git", "-C", str(bare), "-c", "user.name=proba", "-c", "user.email=p@p"]
    subprocess.run([*run, "init", "-q"], capture_output=True, timeout=60)
    subprocess.run([*run, "add", "-A"], capture_output=True, timeout=60)
    subprocess.run([*run, "commit", "-qm", "стенд без карты"], capture_output=True, timeout=60)
    return bare


def mode_off_cases():
    """N-8 круга 6: без карты и при неактивном режиме commit-msg пропускает коммит без суда."""
    future, past = iso(NOW + dt.timedelta(hours=3)), iso(NOW - dt.timedelta(hours=3))
    bare = build_mapless_repo()
    cases.check("N-8 стенд: в репозитории без карты dev-map.yaml нет ни в HEAD, ни в дереве",
                (False, True), (bare.joinpath("dev-map.yaml").exists(),
                                (bare / "scripts" / "commit-msg-gate.sh").exists()))
    for name, until_value in (("срок жив", future), ("срок пуст", "")):
        write_conf(until_value)
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ N-8 (1): репозиторий без карты, {name}, без трейлера — "
                    "git commit с хуком встал (код 0, HEAD сдвинулся)", (0, True),
                    hooked_commit(bare, "docs/x.md", NO_TRAILER, "n8-1"))
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ N-8 (2): репозиторий без карты, {name}, трейлер на несуществующую "
                    "карточку — git commit встал", (0, True),
                    hooked_commit(bare, "docs/x.md", "правка случая\n\nDev-Map: hr.net-takoj\n", "n8-2"))

    still = [wip_card("хакатон", status="test"), PLAIN_PLAN]
    for name, until_value in (("срок вышел", past), ("срок пуст", "")):
        gate_run(still, still, [], until_value)
        cases.check(f"N-8 (3): карта с хакатоном, режим выключен ({name}), без трейлера — git commit встал",
                    (0, True), hooked_commit(REPO, "docs/x.md", NO_TRAILER, "n8-3"))
        cases.check(f"N-8 (4): карта с хакатоном, режим выключен ({name}), трейлер обычной hr.p — "
                    "git commit встал", (0, True), hooked_commit(REPO, "docs/x.md", TO_P, "n8-4"))
    gate_run(still, still, [], future)
    rc, moved = hooked_commit(REPO, "docs/x.md", NO_TRAILER, "n8-5")
    cases.check("НЕИЗМЕННО N-8 (5): карта с хакатоном, режим активен, без трейлера — git commit не встал",
                (True, False), (rc != 0, moved))

    # (6) битая карта в индексе: без трейлера при пустом сроке карту не читают вовсе,
    # при живом сроке судить нечем — отказ (битую карту раньше ловит pre-commit схемы).
    (REPO / "dev-map.yaml").write_text("tasks: [\n", encoding="utf-8")
    git("add", "dev-map.yaml")
    write_conf("")
    cases.check("БОЛЬНОЙ СЛУЧАЙ N-8 (6): битая карта в индексе, срок пуст, без трейлера — git commit встал",
                (0, True), hooked_commit(REPO, "docs/x.md", NO_TRAILER, "n8-6"))
    (REPO / "dev-map.yaml").write_text("tasks: [\n", encoding="utf-8")
    git("add", "dev-map.yaml")
    write_conf(future)
    rc, moved = hooked_commit(REPO, "docs/x.md", "правка случая\n\nDev-Map: hr.w\n\nбитая карта\n",
                              "n8-6b")
    cases.check("N-8 (6): битая карта в индексе, срок жив, трейлер hr.w — git commit не встал",
                (True, False), (rc != 0, moved))
    (REPO / "dev-map.yaml").write_text(map_text(*still), encoding="utf-8")
    git("add", "dev-map.yaml")
    git("commit", "-qm", "карта после битой")


def foreign_commit_cases():
    """n-8: чужой коммит при единственной wip хакатона судится как у двойника — код и вывод."""
    future, past = iso(NOW + dt.timedelta(hours=3)), iso(NOW - dt.timedelta(hours=3))
    # Пульт песочницы pipeline_cases оставил шаги.хакатон = [тесты]: без полного списка
    # случай «без spec:» не отличим от «со spec:».
    shutil.copy2(SOURCE / "harness" / "config" / "pipeline.yaml", REPO / "harness" / "config")
    git("add", "harness/config/pipeline.yaml")
    git("commit", "-qm", "пульт возвращён")
    for spec, until_value, name in ((False, future, "без spec:, срок жив"),
                                    (True, future, "со spec:, срок жив"),
                                    (True, past, "со spec:, срок вышел")):
        twin_cards = [wip_card("обычная", spec=spec), PLAIN_PLAN]
        twin = gate_run(twin_cards, twin_cards, ["docs/x.md"], until_value, message=TO_P)
        own_cards = [wip_card("хакатон", spec=spec), PLAIN_PLAN]
        own = gate_run(own_cards, own_cards, ["docs/x.md"], until_value, message=TO_P)
        cases.check(f"n-8 двойник ({name}): hr.w обычная, чужой docs/x.md, трейлер hr.p — код 0",
                    0, twin[0])
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ n-8 ({name}): hr.w хакатон — единственная wip, чужой коммит "
                    "с трейлером hr.p — код и вывод как у двойника", twin, own)


TRAILER_SAMPLES = (
    ("правка\n\nDev-Map: hr.a\n", ["hr.a"]),
    ("правка\n\nDev-Map:hr.a\n", ["hr.a"]),
    ("правка\n\nDev-Map:   hr.a  \n", ["hr.a"]),
    ("правка\n\nDev-Map: hr.a, hr.b\n", ["hr.a", "hr.b"]),
    ("правка\n\nDev-Map: hr.a hr.b\n", ["hr.a", "hr.b"]),
    ("правка\n\nDev-Map: hr.a,hr.b,\n", ["hr.a", "hr.b"]),
    ("правка\n\nDev-Map: hr.a\nCo-Authored-By: p <p@s>\nDev-Map: hr.d\n", ["hr.a", "hr.d"]),
    ("правка\n\nDev-Map: hr.a\nDev-Map: hr.a\n", ["hr.a"]),
    ("правка\n\nтекст Dev-Map: hr.c\n", []),
    ("правка\n\nDev-Map:\n", []),
)
RE_CALLS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "split"}
REGEX_LOOKS = re.compile(r"Dev-Map:\s*(?:\\s|\(\\S|\(\[)")


def own_trailer_regexes(path):
    """Свои разборы трейлера в исходнике: вызов re.* с «Dev-Map» в образце или
    строка-образец «Dev-Map:» с регулярными знаками. Строки git --grep сюда не попадают."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
                and node.func.attr in RE_CALLS and node.args
                and any(isinstance(part, ast.Constant) and isinstance(part.value, str)
                        and "Dev-Map" in part.value for part in ast.walk(node.args[0]))):
            found.append(f"{path.name}:{node.lineno} re.{node.func.attr}")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and REGEX_LOOKS.search(node.value)):
            found.append(f"{path.name}:{node.lineno} образец {node.value[:40]!r}")
    return sorted(set(found))


def calls_name(path, name):
    return any(isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        or (isinstance(node.func, ast.Name) and node.func.id == name))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))


def acceptance_ids(acceptance, message, index):
    """Разбор приёмки на настоящем коммите: {id: число отметок} из dev_map_commit_times."""
    repo = WORK / f"trailers-{index}"
    repo.mkdir()
    run = ["git", "-C", str(repo), "-c", "user.name=proba", "-c", "user.email=p@p"]
    subprocess.run([*run, "init", "-q"], capture_output=True, timeout=60)
    (WORK / f"trailers-{index}.txt").write_text(message, encoding="utf-8")
    subprocess.run([*run, "commit", "-q", "--allow-empty", "--cleanup=verbatim",
                    "-F", str(WORK / f"trailers-{index}.txt")], capture_output=True, timeout=60)
    found = acceptance.dev_map_commit_times(repo, 1)
    return None if found is None else {task_id: len(stamps) for task_id, stamps in found.items()}


def trailer_parse_cases():
    """n-10: трейлер разбирается одной функцией cards.dev_map_ids — суд и приёмка видят одно."""
    cards_lib = load("cards_trailer", SOURCE / "scripts" / "lib" / "cards.py")
    acceptance = load("acceptance_probe", SOURCE / "scripts" / "acceptance.py")
    parse = getattr(cards_lib, "dev_map_ids", None)
    cases.check("n-10 cards.dev_map_ids есть", True, callable(parse))
    for index, (message, expected) in enumerate(TRAILER_SAMPLES):
        shown = message.split("\n\n", 1)[1].strip().replace("\n", " ⏎ ")
        cases.check(f"n-10 dev_map_ids({shown!r})", expected,
                    parse(message) if callable(parse) else "<нет функции>")
        cases.check(f"n-10 приёмка на коммите ({shown!r}) — те же id, по отметке на коммит",
                    {task_id: 1 for task_id in expected}, acceptance_ids(acceptance, message, index))

    past = iso(NOW - dt.timedelta(hours=3))
    still = ([wip_card("хакатон", status="test"), PLAIN_PLAN],) * 2
    for message in ("правка\n\nDev-Map:hr.w\n", "правка\n\nDev-Map:   hr.w\n",
                    "правка\n\nDev-Map: hr.p, hr.w\n", "правка\n\nDev-Map: hr.p hr.w\n"):
        rc, out = gate_run(*still, ["docs/x.md"], past, message=message, pre_commit=False)
        shown = message.split("\n\n", 1)[1].strip()
        cases.check(f"БОЛЬНОЙ СЛУЧАЙ n-10 суд видит hr.w в «{shown}» — срок вышел, код 1 "
                    "«срок хакатона»", (1, True), (rc, "срок хакатона" in out))

    for name in ("check-pipeline.py", "acceptance.py"):
        path = SOURCE / "scripts" / name
        cases.check(f"n-10 {name}: своих регулярок трейлера Dev-Map нет", [], own_trailer_regexes(path))
        cases.check(f"n-10 {name}: зовёт dev_map_ids", True, calls_name(path, "dev_map_ids"))


FORBIDDEN_PROBE = ("import json, sys; sys.path.insert(0, sys.argv[1]); import hackathon; "
                   "print(json.dumps(hackathon.forbidden_masks()))")


def masks_of(scripts_dir):
    """forbidden_masks() предмета, вызванный из scripts_dir: (код, маски или вывод)."""
    done = subprocess.run([sys.executable, "-c", FORBIDDEN_PROBE, str(scripts_dir)],
                          capture_output=True, text=True, timeout=60, cwd=scripts_dir,
                          env={**os.environ, "GIT_CEILING_DIRECTORIES": str(WORK)})
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        return done.returncode, (done.stdout + done.stderr)[-200:]


def forbidden_source_cases():
    """(а): вне git — запреты с диска; в git — из индекса, рабочий файл без git add не в счёт."""
    plain = WORK / "nogit"
    shutil.copytree(SOURCE / "scripts", plain / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    (plain / "harness" / "config").mkdir(parents=True)
    shutil.copy2(FORBIDDEN_YAML, plain / "harness" / "config")
    masks = forbidden_masks_of_source()
    outside = git_root_of(plain)
    cases.check("(а) копия дерева без .git: git репозитория не находит", "", outside)
    cases.check("БОЛЬНОЙ СЛУЧАЙ (а): вне git forbidden_masks() — маски с диска, не пусто",
                (0, masks, True), (*masks_of(plain / "scripts"), bool(masks)))

    sandbox = REPO / "harness" / "config" / "hackathon-forbidden.yaml"
    sandbox.write_text("paths: [docs/только-в-дереве.md]\n", encoding="utf-8")
    try:
        cases.check("(а) в git forbidden_masks() — из индекса: испорченный рабочий файл без git add "
                    "не влияет", (0, masks), masks_of(REPO / "scripts"))
    finally:
        shutil.copy2(FORBIDDEN_YAML, sandbox)


def status_line_cases():
    """Строка оглядки старта: режим и число карточек уровня хакатон."""
    path = WORK / "status-map.yaml"
    path.write_text(map_text(card("hr.h1", "wip", "хакатон"), card("hr.h2", "plan", "хакатон"),
                             card("hr.o1", "wip", "обычная"), card("hr.h1-prod-debt", "plan")),
                    encoding="utf-8")
    future, past = iso(NOW + dt.timedelta(hours=3)), iso(NOW - dt.timedelta(hours=3))
    for name, value, mode, other in (("ключа нет", None, "выкл", "до "),
                                     ("срок истёк", past, "выкл", "до "),
                                     ("срок в будущем", future, f"до {future}", "выкл")):
        write_conf(value)
        code, out = hackathon_cli("--строка", "--карта", str(path))
        lines = out.strip().splitlines()
        cases.check(f"--строка ({name}): код 0, одна строка «хакатон: … {mode} …»",
                    (0, 1, True, True, False, True),
                    (code, len(lines), bool(lines) and lines[0].startswith("хакатон:"),
                     bool(lines) and mode in lines[0], bool(lines) and other in lines[0],
                     bool(lines) and "уровней хакатон 2" in lines[0]))
    # Блок оглядки исполняется, а не ищется: вызов с выводом в /dev/null тоже «есть в тексте».
    shell = (SOURCE / "scripts" / "hooks" / "session_state.sh").read_text(encoding="utf-8").splitlines()
    calls = [i for i, line in enumerate(shell)
             if not line.lstrip().startswith("#") and "hackathon.py" in line and "--строка" in line]
    start = next((i for i in range(calls[0], -1, -1) if shell[i].startswith("if ")), 0) if calls else 0
    end = next((i for i in range(start, len(shell)) if shell[i] == "fi"), start) if calls else -1
    project = WORK / "oglyadka"
    shutil.copytree(SOURCE / "scripts" / "lib", project / "scripts" / "lib", dirs_exist_ok=True)
    shutil.copy2(SOURCE / "scripts" / "hackathon.py", project / "scripts")
    shutil.copy2(path, project / "dev-map.yaml")
    done = subprocess.run(["bash", "-c", 'project_dir="$1"\n' + "\n".join(shell[start:end + 1]),
                           "_", str(project)], capture_output=True, text=True, timeout=60)
    cases.check("session_state.sh: блок оглядки печатает строку режима по карте проекта",
                [f"хакатон: до {future}; уровней хакатон 2"], done.stdout.splitlines())


def git_root_of(folder):
    done = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "GIT_CEILING_DIRECTORIES": str(WORK)})
    return done.stdout.strip()


def main():
    print(f"── предмет: {SOURCE}; песочница: {WORK} ──")
    try:
        # Первым: без кэша каждый вызов суда ниже стоит десятки секунд.
        print("── кэш judge_dependencies: один разбор на корень за процесс ──")
        guarded("кэш зависимостей суда", judge_cache_cases)
        print("── библиотека scripts/hackathon.py ──")
        guarded("библиотека", lib_cases)
        print("── N-1: зависимости суда хакатона (judge_dependencies) ──")
        guarded("зависимости суда", judge_dependency_cases)
        print("── CLI: --активен, --включить ──")
        guarded("CLI", cli_cases)
        print("── CLI: --включить-задачу (долг, повтор, лок) ──")
        guarded("--включить-задачу", enable_task_cases)
        print("── CLI: атомарная запись карты (m-2) ──")
        guarded("атомарная запись", atomic_cases)
        print("── CLI: --проверить-карту ──")
        guarded("--проверить-карту", check_map_cases)
        print("── гейт пайплайна check-pipeline.py ──")
        guarded("гейт пайплайна", pipeline_cases)
        print("── N-1 гейт: каждый файл суда и живой случай B ──")
        guarded("N-1 гейт", judge_gate_cases)
        print("── N-2/N-4 гейт: трейлер Dev-Map, чужие коммиты, дверь выхода ──")
        guarded("N-4 гейт", trailer_cases)
        print("── N-5: трейлер обязателен, пока режим активен ──")
        guarded("N-5 трейлер обязателен", require_trailer_cases)
        print("── N-8 круга 6: без карты и при выключенном режиме commit-msg молчит ──")
        guarded("N-8 режим выключен", mode_off_cases)
        print("── n-8: чужой коммит при wip хакатона — как двойник ──")
        guarded("n-8 чужой коммит", foreign_commit_cases)
        print("── n-10: разбор трейлера в одном месте ──")
        guarded("n-10 разбор трейлера", trailer_parse_cases)
        print("── (а) запреты: вне git — с диска, в git — из индекса ──")
        guarded("источник запретов", forbidden_source_cases)
        print("── --строка: режим и уровни хакатон в оглядке старта ──")
        guarded("--строка", status_line_cases)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    if cases.paths == 0:
        print("ПРОБА КРАСНАЯ: ни одного случая не прошло")
        return 1
    if cases.ok:
        print(f"\nПРОБА ЗЕЛЁНАЯ: случаев {cases.paths}")
        return 0
    print(f"\nПРОБА КРАСНАЯ: неудач {cases.failures} из {cases.paths}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
