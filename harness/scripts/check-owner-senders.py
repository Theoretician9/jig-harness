#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт отправителей владельцу: правило канала держит КОД, а не внимание.

БОЛЬНОЙ СЛУЧАЙ 17.10.2026. Владелец о сообщениях при лимите подписки: «надо
проверить чтобы все механизмы этому следовали принципу, а то хук или демон что
там, дёргает и дёргает какого то хрена, хотя известно что лимит». Правило
поставили одной дверью (`scripts/tg_send.sh`), и в ту же смену ДВА ревью нашли,
что дверь обходят: три демона при любом её отказе уходили в прямой `curl` к Bot
API, а четвёртый делает это до сих пор — законно, как запасной путь на случай
мёртвой двери.

Отправителей владельцу 24. Правило, записанное у каждого, держится вниманием:
новый демон о нём просто не узнает ([[a-rule-covering-one-path-of-two]]).
Поэтому список — ХРАПОВИК В ДАННЫХ (`harness/config/owner-senders.yaml`):
стоящие пути названы с причиной, а НОВЫЙ краснит гейт. Потолок опускается, а не
поднимается.

Судятся два списка, и это РАЗНЫЕ вопросы:
  * кто ходит к Bot API напрямую (мимо двери);
  * кто вправе звать дверь с ключом «--несмотря-на-лимит», то есть говорить,
    пока лимит держится. Владелец разрешил ровно смену сессии.

Третий вопрос (25.10.2026, слово владельца: «сторож гигиены шлёт сообщение
только мне, и что мне делать с этими сообщениями?»): за вестью сторожа стоит
работа или она законно «только владельцу». Раздел `model_task` данных называет
по КЛЮЧАМ, что делает каждый отправитель (card / kick / board / owner-only /
debt), и сверяется с кодом в обе стороны. Долг (`debt`) только убывает.

Прогон:
    python3 scripts/check-owner-senders.py            # 1 — есть незаявленный
    python3 scripts/check-owner-senders.py --selftest
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parent.parent
ДАННЫЕ = КОРЕНЬ / "harness" / "config" / "owner-senders.yaml"

# Прямой разговор с Bot API: адрес или вызов метода отправки. Имя метода без
# адреса тоже признак — токен может лежать в переменной.
ПРЯМОЙ_API = re.compile(r"api\.telegram\.org|/sendMessage|/sendDocument|/sendPhoto")
КЛЮЧ_ЛИМИТА = "--несмотря-на-лимит"
# Где искать: код харнеса. Данные и документы не отправители.
КАТАЛОГИ = ("scripts", "harness")
РАСШИРЕНИЯ = (".py", ".sh")

# Зов двери канала: по нему файл считается отправителем владельцу.
ЗОВ_ДВЕРИ = re.compile(r"tg_send")
# Ключ тревоги, заведённый (--task/--задача/--key) и снятый (--resolve) в коде.
# Литерал кончается там, где кончается класс знаков ключа: «dead:$NAME» даёт
# префикс «dead:», и он сверяется с записью вида «dead:*».
ПОДНЯТЬ_КЛЮЧ = re.compile(r"(?:--task|--задача|--key)[ \t=]+[\"']?([a-z0-9][a-z0-9:._-]*)")
СНЯТЬ_КЛЮЧ = re.compile(r"--resolve[ \t=]+[\"']?([a-z0-9][a-z0-9:._-]*)")
ВИДЫ_ЗАПИСИ = ("card", "kick", "board", "owner-only", "debt")
МАСКА_ПРОБ_ПО_УМОЛЧАНИЮ = r"(^|/)test_|selftest"


class ОтказGit(Exception):
    """git не ответил. Пустой список файлов и «нарушителей нет» — разные
    факты, и второй печатался за первый."""


def _данные(путь: pathlib.Path) -> dict:
    import yaml
    текст = путь.read_text(encoding="utf-8")
    return yaml.safe_load(текст) or {}


def файлы_кода(корень: pathlib.Path) -> list[pathlib.Path]:
    """Код харнеса из ГИТА, а не обходом дерева.

    Обход подхватил бы копии в /tmp, сборки и чужие деревья; git знает ровно
    то, что уедет на другую машину.
    """
    # core.quotepath=false: иначе кириллические имена придут экранированными
    # байтами, и путь из данных с ними не совпадёт.
    готово = subprocess.run(["git", "-C", str(корень), "-c", "core.quotepath=false",
                             "ls-files", *КАТАЛОГИ],
                            capture_output=True, text=True)
    if готово.returncode != 0:
        # «Не прочитали» ≠ «пусто»: пустой список шёл в приговор «чисто» —
        # гейт зеленел на отказе git (индекс занят соседним коммитом), и так он
        # дважды 17.10.2026 не заметил нарушителя, которого нашёл на третий раз
        # ([[command-failure-read-as-empty]], [[a-gate-alive-with-empty-data]]).
        raise ОтказGit(f"git ls-files: код {готово.returncode}, "
                       f"{готово.stderr.strip()[:120]}")
    пути = [корень / строка for строка in готово.stdout.splitlines() if строка.strip()]
    return [п for п in пути if п.suffix in РАСШИРЕНИЯ and п.is_file()]


def _тексты(корень: pathlib.Path) -> dict[str, str]:
    return {str(файл.relative_to(корень)): файл.read_text(encoding="utf-8", errors="replace")
            for файл in файлы_кода(корень)}


def нарушители(корень: pathlib.Path, данные: dict) -> tuple[list, list]:
    """(незаявленные прямые пути, незаявленные ключи лимита)."""
    прямые = set(данные.get("pryamoj_api") or {})
    ключи = set(данные.get("nesmotrya_na_limit") or {})
    тексты = _тексты(корень)
    чужие_api = [имя for имя, текст in тексты.items()
                 if имя not in прямые and ПРЯМОЙ_API.search(текст)]
    чужие_ключи = [имя for имя, текст in тексты.items()
                   if имя not in ключи and КЛЮЧ_ЛИМИТА in текст]
    return sorted(чужие_api), sorted(чужие_ключи)


def мёртвые_заявки(корень: pathlib.Path, данные: dict) -> tuple[list, list]:
    """(заявленные прямые пути без прямого пути, заявленные права без ключа).

    Обратное направление храповика: заявка на файл без признака, или на файл
    вне гита, лжёт о том, что охраняет, и молча разрешает вернуть обход.
    """
    тексты = _тексты(корень)

    def мёртвые(список: str, признак) -> list:
        return sorted(имя for имя in (данные.get(список) or {})
                      if имя not in тексты or not признак(тексты[имя]))

    return (мёртвые("pryamoj_api", lambda т: bool(ПРЯМОЙ_API.search(т))),
            мёртвые("nesmotrya_na_limit", lambda т: КЛЮЧ_ЛИМИТА in т))


def _ключ_подходит(литерал: str, ключ: str) -> bool:
    return литерал.startswith(ключ[:-1]) if ключ.endswith("*") else литерал == ключ


def судить_ключи(тексты: dict[str, str], данные: dict, закоммиченный_потолок) -> tuple[list[str], dict]:
    """Раздел model_task: (замечания, счёт для печати).

    Чистая функция от текстов и данных: гейт и самотест судят её одинаково.
    Правила 1-6 спеки hr.watchers-report-must-create-model-task, §5.
    """
    замечания: list[str] = []
    записи = данные.get("model_task") or {}
    не_отправители = данные.get("not_senders") or {}
    маска = re.compile(данные.get("probe_mask") or МАСКА_ПРОБ_ПО_УМОЛЧАНИЮ)
    счёт = {"файлов": 0, "записей": 0, "card": 0, "kick": 0, "board": 0,
            "owner-only": 0, "debt": 0}

    звонят = sorted(n for n, т in тексты.items() if ЗОВ_ДВЕРИ.search(т) and not маска.search(n))
    счёт["файлов"] = len(звонят)
    for имя in звонят:
        if имя not in записи and имя not in не_отправители:
            замечания.append(f"{имя}: зовёт дверь канала, а в model_task и not_senders его нет — "
                             f"назови, что за вестью: card, kick, board, owner-only или debt")
    for имя in sorted(set(записи) & set(не_отправители)):
        замечания.append(f"{имя}: стоит и в model_task, и в not_senders — одно из двух")
    for имя in sorted(не_отправители):
        if имя not in тексты:
            замечания.append(f"{имя}: в not_senders, а файла нет в гите")

    допустимые: list[str] = []
    for имя, список in записи.items():
        for запись in список or []:
            if запись.get("key"):
                допустимые.append(запись["key"])
    for имя in sorted(записи):
        if имя not in тексты:
            замечания.append(f"{имя}: в model_task, а файла нет в гите")
            continue
        текст = тексты[имя]
        if not ЗОВ_ДВЕРИ.search(текст):
            замечания.append(f"{имя}: в model_task, а зова двери канала в файле нет")
        поднятые = ПОДНЯТЬ_КЛЮЧ.findall(текст)
        снятые = СНЯТЬ_КЛЮЧ.findall(текст)
        for запись in записи[имя] or []:
            вид = запись.get("kind")
            if вид not in ВИДЫ_ЗАПИСИ:
                замечания.append(f"{имя}: вид записи {вид!r} неизвестен, допустимы {', '.join(ВИДЫ_ЗАПИСИ)}")
                continue
            счёт["записей"] += 1
            счёт[вид] += 1
            if not str(запись.get("why") or "").strip():
                замечания.append(f"{имя}: запись {вид} без причины (why)")
            ключ = запись.get("key")
            if вид == "debt" and not запись.get("card"):
                замечания.append(f"{имя}: долг {ключ or ''} без id карточки долга (card)")
            if вид != "card":
                continue
            if not ключ:
                if not str(запись.get("own_writer") or "").strip():
                    замечания.append(f"{имя}: card без key должна назвать own_writer — кто заводит карточку")
                continue
            if not any(_ключ_подходит(л, ключ) for л in поднятые):
                замечания.append(f"{имя}: ключ {ключ} записан card, а в файле нет зова с ним "
                                 f"(--task/--задача/--key)")
            if not any(_ключ_подходит(л, ключ) for л in снятые):
                замечания.append(f"{имя}: ключ {ключ} без --resolve — без «зелёного» "
                                 f"карточка не позеленеет никогда")
        for литерал in sorted(set(поднятые + снятые)):
            if not any(_ключ_подходит(литерал, к) for к in допустимые):
                замечания.append(f"{имя}: ключ {литерал} есть в файле, а в данных model_task его нет")

    потолок = данные.get("debt_max")
    if not isinstance(потолок, int):
        замечания.append("debt_max не задан числом")
    else:
        if счёт["debt"] > потолок:
            замечания.append(f"долг {счёт['debt']} выше потолка {потолок}: новый долг заводить нельзя")
        if isinstance(закоммиченный_потолок, int) and потолок > закоммиченный_потолок:
            замечания.append(f"debt_max поднят с {закоммиченный_потолок} до {потолок}: долг только убывает")
    счёт["потолок"] = потолок
    return замечания, счёт


def закоммиченный_потолок_долга(корень: pathlib.Path):
    """debt_max из HEAD, а None — если базы для сравнения нет (файл новый)."""
    готово = subprocess.run(["git", "-C", str(корень), "show",
                             "HEAD:harness/config/owner-senders.yaml"],
                            capture_output=True, text=True)
    if готово.returncode != 0:
        return None
    import yaml
    значение = (yaml.safe_load(готово.stdout) or {}).get("debt_max")
    return значение if isinstance(значение, int) else None


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    if not ДАННЫЕ.is_file():
        print(f"[отправители] нет данных {ДАННЫЕ.relative_to(КОРЕНЬ)} — "
              f"судить нечем", file=sys.stderr)
        return 2
    данные = _данные(ДАННЫЕ)
    try:
        чужие_api, чужие_ключи = нарушители(КОРЕНЬ, данные)
        мёртвые_api, мёртвые_ключи = мёртвые_заявки(КОРЕНЬ, данные)
    except ОтказGit as отказ:
        print(f"[отправители] судить нечем: {отказ}", file=sys.stderr)
        return 2
    заявлено = len(данные.get("pryamoj_api") or {})
    ключей = len(данные.get("nesmotrya_na_limit") or {})
    замечания, счёт = судить_ключи(_тексты(КОРЕНЬ), данные, закоммиченный_потолок_долга(КОРЕНЬ))
    # Число судимых образцов вслух: «пустой список» и «нечего проверять» иначе
    # неотличимы ([[a-gate-alive-with-empty-data]]).
    печать = (f"ОТПРАВИТЕЛИ: файлов {счёт['файлов']}, записей {счёт['записей']}, "
              f"card {счёт['card']}, kick {счёт['kick']}, board {счёт['board']}, "
              f"owner-only {счёт['owner-only']}, долг {счёт['debt']} (потолок {счёт['потолок']})")
    if not (чужие_api or чужие_ключи or мёртвые_api or мёртвые_ключи or замечания):
        print(f"[отправители] чисто: прямых путей к Bot API заявлено {заявлено}, "
              f"права говорить при лимите — у {ключей}")
        print(печать)
        return 0
    for замечание in замечания:
        print(f"[отправители] ✗ {замечание}", file=sys.stderr)
    print(печать, file=sys.stderr)
    for имя in чужие_api:
        print(f"[отправители] ✗ {имя}: говорит с Bot API НАПРЯМУЮ, минуя "
              f"scripts/tg_send.sh — правило тишины при лимите на этом пути не "
              f"действует. Либо звать дверь, либо назвать причину в "
              f"harness/config/owner-senders.yaml", file=sys.stderr)
    for имя in чужие_ключи:
        print(f"[отправители] ✗ {имя}: зовёт дверь с «{КЛЮЧ_ЛИМИТА}» — это право "
              f"говорить, пока лимит держится. Владелец разрешил ровно смену "
              f"сессии: назови файл в harness/config/owner-senders.yaml с "
              f"причиной", file=sys.stderr)
    for имя in мёртвые_api:
        print(f"[отправители] ✗ {имя}: заявлен прямым путём к Bot API, а прямого "
              f"пути в нём нет (или файла нет в гите) — снять строку из "
              f"harness/config/owner-senders.yaml", file=sys.stderr)
    for имя in мёртвые_ключи:
        print(f"[отправители] ✗ {имя}: заявлено право «{КЛЮЧ_ЛИМИТА}», а ключа в "
              f"файле нет (или файла нет в гите) — снять строку из "
              f"harness/config/owner-senders.yaml", file=sys.stderr)
    print(f"[отправители] КРАСНЫЙ: незаявленных прямых путей {len(чужие_api)}, "
          f"незаявленных прав говорить при лимите {len(чужие_ключи)}, "
          f"мёртвых заявок {len(мёртвые_api) + len(мёртвые_ключи)}, "
          f"замечаний по ключам задач {len(замечания)}",
          file=sys.stderr)
    return 1


def _selftest() -> int:
    import tempfile
    плохо = путей = 0

    def проба(имя: str, вышло, ждём) -> None:
        nonlocal плохо, путей
        путей += 1
        if вышло == ждём:
            print(f"  ок    {имя}")
        else:
            print(f"  ПЛОХО {имя}: вышло {вышло!r}, ждали {ждём!r}")
            плохо = 1

    with tempfile.TemporaryDirectory() as двор:
        корень = pathlib.Path(двор)
        (корень / "scripts").mkdir()
        subprocess.run(["git", "-C", двор, "init", "-q"], check=True)
        subprocess.run(["git", "-C", двор, "config", "user.email", "p@p"], check=True)
        subprocess.run(["git", "-C", двор, "config", "user.name", "проба"], check=True)

        дверь = корень / "scripts" / "tg_send.sh"
        дверь.write_text('curl "https://api.telegram.org/bot$T/sendMessage"\n',
                         encoding="utf-8")
        данные = {"pryamoj_api": {"scripts/tg_send.sh": "сама дверь"},
                  "nesmotrya_na_limit": {"scripts/tg_send.sh": "дверь разбирает ключ"}}
        subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
        проба("заявленная дверь — чисто", нарушители(корень, данные), ([], []))

        # БОЛЬНОЙ СЛУЧАЙ: новый демон обходит дверь прямым запросом.
        обход = корень / "scripts" / "novyj-demon.sh"
        обход.write_text('curl -d chat_id=1 "https://api.telegram.org/bot$T/sendMessage"\n',
                         encoding="utf-8")
        subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
        проба("БОЛЬНОЙ СЛУЧАЙ: незаявленный прямой путь назван",
              нарушители(корень, данные)[0], ["scripts/novyj-demon.sh"])

        обход.write_text('bash scripts/tg_send.sh "весть"\n', encoding="utf-8")
        subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
        проба("тот же демон через дверь — чисто", нарушители(корень, данные)[0], [])

        # БОЛЬНОЙ СЛУЧАЙ: право говорить при лимите взято молча.
        обход.write_text('bash scripts/tg_send.sh --несмотря-на-лимит "весть"\n',
                         encoding="utf-8")
        subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
        проба("БОЛЬНОЙ СЛУЧАЙ: незаявленное право говорить при лимите названо",
              нарушители(корень, данные)[1], ["scripts/novyj-demon.sh"])

        # Признак — только отправка: получение файла отправкой не считается.
        обход.write_text('curl "https://api.telegram.org/bot$T/getFile"\n',
                         encoding="utf-8")
        subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
        проба("БОЛЬНОЙ СЛУЧАЙ: адрес API сам по себе ловится (токен может быть в переменной)",
              нарушители(корень, данные)[0], ["scripts/novyj-demon.sh"])

        # Файл вне гита не судится: в дереве лежат копии и сборки.
        обход.unlink()
        (корень / "scripts" / "ne-v-gite.sh").write_text(
            'curl "https://api.telegram.org/bot$T/sendMessage"\n', encoding="utf-8")
        subprocess.run(["git", "-C", двор, "add", "-A", "--", "scripts/tg_send.sh"],
                       check=True)
        subprocess.run(["git", "-C", двор, "rm", "-q", "--cached",
                        "scripts/novyj-demon.sh"], check=False)
        проба("незакоммиченный файл вне счёта (в дереве бывают копии)",
              "scripts/ne-v-gite.sh" in нарушители(корень, данные)[0], False)

    # Храповик судится в обе стороны: заявленный прямым путём файл без прямого
    # пути — мёртвая строка, она лжёт о том, что охраняет, и молча разрешает
    # вернуть обход ([[a-two-way-invariant-checked-one-way]]). Обратное
    # направление: заявленный файл без признака (или вне гита) — красный,
    # в ОБОИХ списках.
    судья_мёртвых = globals().get("мёртвые_заявки")
    if судья_мёртвых is None:
        проба("БОЛЬНОЙ СЛУЧАЙ: обратное направление — судья мёртвых заявок есть",
              "нет функции мёртвые_заявки", "есть")
    else:
        with tempfile.TemporaryDirectory() as двор:
            корень = pathlib.Path(двор)
            (корень / "scripts").mkdir()
            subprocess.run(["git", "-C", двор, "init", "-q"], check=True)
            (корень / "scripts" / "tg_send.sh").write_text(
                'curl "https://api.telegram.org/bot$T/sendMessage" # --несмотря-на-лимит\n',
                encoding="utf-8")
            (корень / "scripts" / "tiho.sh").write_text('bash scripts/tg_send.sh "весть"\n',
                                                       encoding="utf-8")
            subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
            данные = {"pryamoj_api": {"scripts/tg_send.sh": "дверь",
                                      "scripts/tiho.sh": "запасной путь",
                                      "scripts/ushel.sh": "файла больше нет"},
                      "nesmotrya_na_limit": {"scripts/tg_send.sh": "дверь",
                                             "scripts/tiho.sh": "право без ключа"}}
            проба("БОЛЬНОЙ СЛУЧАЙ: заявленный прямой путь без прямого пути и файл вне гита названы",
                  судья_мёртвых(корень, данные)[0], ["scripts/tiho.sh", "scripts/ushel.sh"])
            проба("БОЛЬНОЙ СЛУЧАЙ: заявленное право говорить при лимите без ключа названо",
                  судья_мёртвых(корень, данные)[1], ["scripts/tiho.sh"])
            проба("живые заявки не названы мёртвыми",
                  судья_мёртвых(корень, {"pryamoj_api": {"scripts/tg_send.sh": "дверь"},
                                         "nesmotrya_na_limit": {"scripts/tg_send.sh": "дверь"}}),
                  ([], []))

    # Храповик держит сводку ДАННЫМИ: строки heartbeat-watch.sh в нём нет, и
    # прямой sendMessage, вернувшийся в сводку, краснит гейт сам.
    with tempfile.TemporaryDirectory() as двор:
        корень = pathlib.Path(двор)
        (корень / "harness" / "demons").mkdir(parents=True)
        subprocess.run(["git", "-C", двор, "init", "-q"], check=True)
        (корень / "harness" / "demons" / "heartbeat-watch.sh").write_text(
            'curl -K - --data-urlencode "text=$SUMMARY" # api.telegram.org/bot$T/sendMessage\n',
            encoding="utf-8")
        subprocess.run(["git", "-C", двор, "add", "-A"], check=True)
        проба("прямой curl, вернувшийся в сводку, назван",
              нарушители(корень, {"pryamoj_api": {}, "nesmotrya_na_limit": {}})[0],
              ["harness/demons/heartbeat-watch.sh"])
    if ДАННЫЕ.is_file():
        проба("БОЛЬНОЙ СЛУЧАЙ: в данных храповика нет права сводки на прямой путь",
              "harness/demons/heartbeat-watch.sh" in (_данные(ДАННЫЕ).get("pryamoj_api") or {}),
              False)

    # БОЛЬНОЙ СЛУЧАЙ 17.10.2026: git отказал (индекс занят соседним коммитом),
    # список файлов пришёл пустым — и приговор печатался «чисто». Дважды подряд
    # гейт так не заметил нарушителя, которого нашёл третьим прогоном.
    with tempfile.TemporaryDirectory() as не_гит:
        отказал = False
        try:
            нарушители(pathlib.Path(не_гит), {"pryamoj_api": {}, "nesmotrya_na_limit": {}})
        except ОтказGit:
            отказал = True
        проба("БОЛЬНОЙ СЛУЧАЙ: git не ответил — отказ, а не «чисто»", отказал, True)

    # Ключи задач: каждое правило раздела model_task — свой больной случай.
    ок_тексты = {"d/a.sh": 'tg_send.sh --task a:red "x"; alert-task.py --resolve a:red',
                 "d/b.sh": 'tg_send.sh "x"'}
    ок_данные = {"model_task": {"d/a.sh": [{"kind": "card", "key": "a:red", "why": "w"}],
                                "d/b.sh": [{"kind": "owner-only", "why": "w"}]},
                 "debt_max": 0}

    def замечаний(тексты=None, правка=None, потолок=None) -> int:
        import copy
        данные = copy.deepcopy(ок_данные)
        if правка:
            правка(данные)
        return len(судить_ключи(тексты or ок_тексты, данные, потолок)[0])

    проба("model_task: здоровый набор — без замечаний", замечаний(), 0)
    проба("БОЛЬНОЙ СЛУЧАЙ правило 1: новый файл с зовом двери без записи",
          замечаний(dict(ок_тексты, **{"d/new.sh": "tg_send.sh x"})), 1)
    проба("правило 2: запись на файл без зова двери",
          замечаний(dict(ок_тексты, **{"d/b.sh": "echo тихо"})), 1)
    проба("правило 3: card без --resolve",
          замечаний(dict(ок_тексты, **{"d/a.sh": "tg_send.sh --task a:red x"})), 1)
    проба("правило 4: ключ в коде, которого нет в данных",
          замечаний(dict(ок_тексты, **{"d/a.sh": ок_тексты["d/a.sh"] + "; --task a:other"})), 1)
    проба("правило 5: долг выше потолка",
          замечаний(правка=lambda d: d["model_task"]["d/b.sh"].append(
              {"kind": "debt", "key": "b:x", "card": "c", "why": "w"})), 1)
    проба("правило 6: owner-only без причины",
          замечаний(правка=lambda d: d["model_task"]["d/b.sh"].__setitem__(0, {"kind": "owner-only"})), 1)
    проба("храповик: debt_max выше закоммиченного",
          замечаний(правка=lambda d: d.update(debt_max=2), потолок=1), 1)

    print(f"САМОТЕСТ {'ПРОЙДЕН' if not плохо else 'ПРОВАЛЕН'}: {путей} путей, "
          "больной первым — демон, обошедший дверь прямым запросом")
    return 1 if плохо else 0


if __name__ == "__main__":
    sys.exit(main())
