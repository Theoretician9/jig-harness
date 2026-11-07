#!/usr/bin/env python3
"""test_consent_ask_signs.py — пробы 14-20 задачи hr.consent-buttons-for-every-yes: канал сам
отказывает текстовому вопросу о «да» владельца на пуш и удаление.

Владелец 05.11.2026: «все вопросы по поводу моего Да должны приходить кнопками в телегу»;
«нужно не как правило, а кодом». check-channel-messages.py режет текст на фрагменты и
судит фрагмент с просьбой «да» по признакам вида из harness/config/consent-ask-signs.yaml:
вид с `отказ: да` — отказ (rc 1) с подсказкой ask-consent.py, `отказ: нет` — замечание.
Договор — .tasks/hr.consent-buttons-for-every-yes/spec.md §5а, §7.

Тексты — настоящие сообщения канала (tg_out.jsonl) с указанными датами; путь секрета
в 04.11 22:17 подменён выдуманным. Сторож канала зовётся процессом (`--текст`), как его
зовёт tg_send.sh; PROJECT_DIR — пустой каталог стенда, чтобы потолки длины были
запасными, а не из документа тона машины. Битый файл признаков — в копии дерева.

    python3 scripts/test_consent_ask_signs.py [--случай 14 … 20]...
Код возврата: 0 — «падений 0», 1 — красная.
"""
from __future__ import annotations

import argparse
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "lib"
CHECKER = ROOT / "scripts" / "check-channel-messages.py"
# Проект: <корень>/harness/config; пакет: harness/scripts → harness/config.
SIGNS = next((p for p in (ROOT / "harness" / "config" / "consent-ask-signs.yaml",
                          ROOT / "config" / "consent-ask-signs.yaml") if p.is_file()), None)
sys.path.insert(0, str(LIB))
from selftest_cases import Cases  # noqa: E402

CASE_NAMES = tuple(str(n) for n in range(14, 21))
NOTE = "кнопки на этот вид ещё нет"
# Любой след суда о «да»: отказ, замечание, подсказка.
CONSENT_MARKS = ("«да» текстом", "ask-consent", NOTE)

PUSH_TEXTS = (
    ("05.11 14:37", "Закоммичено: режимы скорости (05a2affa) — сторож добирает задачи до режима, судья в "
                    "кроне, /speed в боте; ночные проверки 8 из 14 зелёные + доска задач не наслаивается "
                    "(841dc771). Приёмщик перезапущен 14:27. Пуш 3 коммитов в гитхаб — нужно слово «да»."),
    ("05.11 10:41", "Два решения, ответь номером:\n1. Включить шаг «безопасность» в пайплайне (задачи, "
                    "задевшие вход/секреты/права, получают проверку помощником)? Рекомендую да. 1 — да, "
                    "2 — нет.\n2. Пуш 20 новых коммитов в гитхаб? Нужно слово «да»."),
    ("05.11 09:17", "По плану дальше: 1) прогон мутаций: второй проход на 871 непрогнанные (ночью, оценка "
                    "16 часов) и проверка, что красных проб стало меньше; 2) подключение строки состояния "
                    "с лимитом в боевую сессию (на ближайшей ротации); 3) 13 задач без полного пайплайна: "
                    "довести спеки и ревью по очереди; 4) самотест прибора мутаций. Сейчас жду твоё «да» "
                    "на пуш 17 коммитов и нажатие кнопки на проверке."),
)
DELETE_TEXTS = (
    ("04.11 22:17", "Удалить копию пароля охрана не даёт: нужно твоё слово с названием файла. Ответь "
                    "словом «да» — удалить /var/lib/harness/secrets/proba-kopija.tmp и "
                    "proba-kopija.tmp-new. Живой пароль в /srv/proba/.env не трогаю."),
    ("03.10 15:02", "Мёртвая копия готова к удалению. Числа перед необратимым:\n\nУйдёт загрузка от 30.09: "
                    "6231 товар и 58 папок. Видно покупателю из них НОЛЬ. Ни одного заказа, закупки, брони, "
                    "избранного или подборки на эти строки не ссылается — проверил. Останется загрузка от "
                    "01.10: 6251 товар, 5926 видно.\n\nСвежая копия базы снята: 35,5 МБ, 78 таблиц, 12 482 "
                    "строки товара — дочитана до конца.\n\nПодтверди словом «да», и удаляю. Это нужно коду: "
                    "без записи твоего согласия удаление данных не запускается."),
)
# Вид с кнопкой и дверью кодом (hr.consent-buttons-doors): вопрос о «да» текстом — отказ.
DOOR_TEXTS = (
    ("06.10 10:11", "выкат",
     "Каталог готов к выкату. Всё, что держало, закрыто.\n\nЧисла: прогон 1616 из 1616 зелёных (было "
     "1615 — прибавилась новая проба), типы код 0, миграций 41 из 41, живая установка с нуля зелёная за "
     "65 минут.\n\nТри замечания закрыл:\n1. Ручка здоровья называла виды по-английски — «учёт назвал "
     "folder 2, product 9». Теперь «папок 2, товаров 9». Проба доказана поломкой.\n2. Совет клиенту "
     "закрыть ручку в nginx закрыл бы её и от него самого: замерил — служба видит не петлю, а адрес "
     "шлюза. Документ теперь учит спросить свой адрес и проверить.\n3. Документ приёмки требовал 40 "
     "файлов изменений базы, а их 41 — поправил и назвал причину.\n\nВЫКАТЫВАТЬ? Ответь словом «да» — "
     "проверка считает только слово, не цифру.\n\nИ отдельно, твоё решение: после ревью остались две "
     "служебные базы, 49 МБ и 7 МБ, живых данных в них нет. Удалять их самому я не вправе — удаление "
     "необратимо. Сносить?"),
    ("10.10 05:30", "выкат",
     "Статус: фаза 0 первой загрузки покупателей закрыта кодом целиком — 1738 проверок зелёные, доказана "
     "поломкой (7 покраснели). Ждёт только твоего «да» на выкат.\n\nПараллельно: по задаче «копия и "
     "восстановление» написан план первой фазы, сейчас её разбирает аналитик перед кодом — ищет "
     "расхождения плана с живым кодом. Идёт 12 минут."),
    ("18.10 09:05", "публикация",
     "Ревизия правок с сервера CMS готова, разбор вложением.\n1. Из 94 правок 71 уже у нас: забрали "
     "14–15.10. Ещё 23 лежат только на том сервере, в общий гит не ушли — взять неоткуда, пока он их не "
     "отправит.\n2. Из 16 дефектов у нас есть 9, наполовину 2, закрыты 2, к нам не относятся 3.\n3. Первым "
     "возьму: сводка при отказе сторожа канала уходит в Telegram мимо него; ворота выката тратят твоё "
     "«да» на простой проверке.\n4. Ревизия нашла, что публикация упала бы на сборке: твоя фраза без "
     "обезличенной пары. Уже исправил, сборка проходит.\n5. На том сервере 3 твоих указания, которых нет "
     "здесь: root-команд не слать, ошибки харнеса чинить в харнесе, доска задач без косяков. Записать их и "
     "сюда?\n\nПубликация ждёт твоего «да»."),
)
# Вид без кнопки или без отказа канала: замечание, сообщение уходит. Спека — замечание с
# подсказкой кнопки: фраза демона speed_mode.py:675 иначе молча не дошла бы до владельца.
NOTE_TEXTS = (
    ("снос базы", "снос-базы", "удалить базу — нужно твоё «да»", NOTE),
    ("перепись", "перепись", "перепись истории — нужно твоё «да»", NOTE),
    ("speed_mode.py:675", "спека", "2 спеки ждут твоего «да», новых не начинаю", "spec_approval.py --отправить"),
)
HEALTHY_TEXTS = (
    ("05.11 10:42", "Пуш сделан: 20 коммитов в гитхабе, проверка секретов чистая. Шаг «безопасность» включаю."),
    ("05.11 01:56", "Статус: 7 проб диспетчера починены (все зелёные, сирот 0), идёт коммит. Параллельно "
                    "заношу кнопки подтверждения в карту: рабочая карточка «простая версия» и карточка в "
                    "беклоге со всеми доделками. Дальше — очистка пароля из истории и пуш (ты сказал «да»), "
                    "после коммита проб."),
    ("05.11 10:41 строка 1", "Два решения, ответь номером:\n1. Включить шаг «безопасность» в пайплайне "
                             "(задачи, задевшие вход/секреты/права, получают проверку помощником)? Рекомендую "
                             "да. 1 — да, 2 — нет."),
    ("05.11 09:26", "Принял «да»: меняю сессию на Opus сразу, как закончит разбор карты (помощников нет — "
                    "тогда ротация). Передача уже записана."),
    ("05.11 14:40", "Понял: кодом. Канал сам не пропустит текстовый вопрос о твоём «да» — только кнопками. "
                    "Проверка пуша и выката будет принимать нажатие кнопки. Делает оркестратор, проверю на "
                    "живом пуше."),
    ("spec_approval 05.11 08:59", "Спека задачи hr.consent-buttons — Подтверждение необратимых действий "
                                  "кнопкой в Telegram (простая версия).\n\nПодтверждение необратимых действий "
                                  "кнопкой в Telegram (простая версия): перед удалением секрета владельцу "
                                  "приходит сообщение с кнопками «Да» и «Нет», нажатие записывает согласие на "
                                  "ровно названный файл на 15 минут. Код уже сделан и закоммичен (51ce5f3f, "
                                  "6e2a268e), спека отправляется задним числом; проверяется пробой на 97 путей "
                                  "и живой проверкой твоим нажатием.\n\nРевью сделано, правки внесены. Работа "
                                  "начнётся после твоего слова — ответь «да» или напиши правки."),
)

cases = Cases()
check = cases.check
STAND = Path(tempfile.mkdtemp(prefix="proba-consent-ask-signs-"))
atexit.register(shutil.rmtree, STAND, True)
(STAND / "project").mkdir()


def judge(text: str, checker: Path = CHECKER) -> tuple[int | None, str]:
    env = dict(os.environ, PROJECT_DIR=str(STAND / "project"))
    try:
        done = subprocess.run(["python3", str(checker), "--текст", text], capture_output=True,
                              text=True, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return None, "сторож канала висит дольше 60 с"
    return done.returncode, (done.stdout + done.stderr).strip()


def short(out: str) -> str:
    return out.replace("\n", " | ")[:220]


def load_signs() -> dict:
    """Файл признаков разбором; не прочитан — пусто, и судящие по нему случаи краснеют."""
    import yaml
    try:
        data = yaml.safe_load(SIGNS.read_text(encoding="utf-8")) if SIGNS else None
    except (OSError, yaml.YAMLError):
        data = None
    return data if isinstance(data, dict) else {}


def probe_14() -> None:
    print("— 14. вопрос о «да» на пуш — отказ с подсказкой кнопками")
    for when, text in PUSH_TEXTS:
        rc, out = judge(text)
        check(f"14) {when}: rc 1 [{short(out)}]", 1, rc)
        check(f"14) {when}: «вид: пуш» и «ask-consent.py --вид пуш»", (True, True),
              ("вид: пуш" in out, "ask-consent.py --вид пуш" in out))


def probe_15() -> None:
    print("— 15. вопрос о «да» на удаление — отказ (Н-6)")
    for when, text in DELETE_TEXTS:
        rc, out = judge(text)
        check(f"15) {when}: rc 1 [{short(out)}]", 1, rc)
        check(f"15) {when}: «--вид удаление» в подсказке", True, "--вид удаление" in out)


def probe_16() -> None:
    print("— 16. выкат и публикация — отказ с подсказкой без --предмет; прочие виды — замечание")
    for when, kind, text in DOOR_TEXTS:
        rc, out = judge(text)
        check(f"16) {when}: rc 1 [{short(out)}]", 1, rc)
        check(f"16) {when}: «вид: {kind}» и подсказка «ask-consent.py --вид {kind} --почему»", (True, True),
              (f"вид: {kind}" in out, f"ask-consent.py --вид {kind} --почему" in out))
    for when, kind, text, note in NOTE_TEXTS:
        rc, out = judge(text)
        check(f"16) {when}: rc 0 [{short(out)}]", 0, rc)
        check(f"16) {when}: замечание «{note}» с видом «{kind}»", (True, True),
              (note in out, f"вид: {kind}" in out))


def probe_17() -> None:
    print("— 17. охранный: здоровые тексты без суда о «да»")
    for when, text in HEALTHY_TEXTS:
        rc, out = judge(text)
        check(f"17) {when}: rc 0 и ни отказа, ни замечания о «да» [{short(out)}]", (0, []),
              (rc, [m for m in CONSENT_MARKS if m in out]))


def probe_18() -> None:
    print("— 18. файл признаков: виды с отказом ⊆ ВИДЫ_СОГЛАСИЯ, каждый вид согласия в файле, регулярки живые")
    import owner_word
    kinds = getattr(owner_word, "ВИДЫ_СОГЛАСИЯ", None)
    check(f"18а) файл признаков найден рядом ({SIGNS})", True, SIGNS is not None)
    signs = load_signs()
    table = signs.get("виды") if isinstance(signs.get("виды"), dict) else {}
    refusing = {k for k, v in table.items() if isinstance(v, dict) and v.get("отказ") == "да"}
    check("18б) виды с «отказ: да» ⊆ ВИДЫ_СОГЛАСИЯ (и непусты)", (True, []),
          (bool(refusing), sorted(refusing - set(kinds or ()))))
    check("18б) каждый вид из ВИДЫ_СОГЛАСИЯ есть в файле признаков", (True, []),
          (bool(kinds), sorted(set(kinds or ()) - set(table))))
    asks = signs.get("просьба") or []
    check("18в) «просьба» непуста и компилируется", True, bool(asks) and all(compiles(p) for p in asks))
    for kind, spec in table.items():
        spec = spec if isinstance(spec, dict) else {}
        signs_of = spec.get("признаки") or []
        sample = str(spec.get("образец") or "")
        check(f"18г) {kind}: отказ ∈ {{да, нет}}, признаки непусты и компилируются", True,
              spec.get("отказ") in ("да", "нет") and bool(signs_of) and all(compiles(p) for p in signs_of))
        check(f"18г) {kind}: образец «{sample}» ловится своим признаком", True,
              bool(sample) and any(compiles(p) and re.search(p, sample, re.I) for p in signs_of))

    # Битый файл признаков — в копии дерева: сторож ищет файл рядом с собой.
    tree = STAND / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(CHECKER, tree / "scripts" / CHECKER.name)
    shutil.copytree(LIB, tree / "scripts" / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    for config_dir in (tree / "harness" / "config", tree / "config"):
        config_dir.mkdir(parents=True)
        (config_dir / "consent-ask-signs.yaml").write_text("просьба: [\n  'битый\n", encoding="utf-8")
    rc, out = judge(PUSH_TEXTS[0][1], tree / "scripts" / CHECKER.name)
    check(f"18д) битый файл признаков: rc 0 и замечание о файле [{short(out)}]", (0, True),
          (rc, "consent-ask-signs" in out))


def compiles(pattern: object) -> bool:
    try:
        re.compile(str(pattern))
    except re.error:
        return False
    return isinstance(pattern, str)


def probe_19() -> None:
    print("— 19. каждый вид из файла: образец с просьбой «да» судится по полю «отказ»")
    table = load_signs().get("виды")
    table = table if isinstance(table, dict) else {}
    check("19) виды разобраны из файла признаков (пусто — провал)", True, bool(table))
    for kind, spec in table.items():
        spec = spec if isinstance(spec, dict) else {}
        text = f"{spec.get('образец', '')} — нужно твоё «да»"
        rc, out = judge(text)
        if spec.get("отказ") == "да":
            check(f"19) {kind} (отказ: да): rc 1 и «--вид {kind}» [{short(out)}]", (1, True),
                  (rc, f"--вид {kind}" in out))
        else:
            note = spec.get("подсказка") or NOTE
            check(f"19) {kind} (отказ: нет): rc 0 и замечание с видом и «{note[:40]}» [{short(out)}]", (0, True, True),
                  (rc, note in out, f"вид: {kind}" in out))


def probe_20() -> None:
    print("— 20. отрицание с просьбой «да» — отказ, подсказка «перепиши фразу» (Н-18)")
    rc, out = judge("Не нужно твоё «да» на пуш")
    check(f"20) rc 1 [{short(out)}]", 1, rc)
    check("20) подсказка содержит «перепиши фразу»", True, "перепиши фразу" in out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--случай", dest="only", action="append", choices=CASE_NAMES)
    only = set(parser.parse_args().only or CASE_NAMES)
    for name, run in zip(CASE_NAMES, (probe_14, probe_15, probe_16, probe_17, probe_18, probe_19, probe_20)):
        if name in only:
            run()
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
