#!/usr/bin/env python3
"""Вид панели: тёмные карточки, крупные числа, пилюли-переключатели.

Владелец 11.09.2026 прислал снимок панели, которая ему нравится, со словами
«Не забудь еще сделать всё это красиво, в каком нибудь таком стиле»: тёмный
фон, карточки со скруглёнными углами, крупные цифры, акценты оранжевым,
фиолетовым и зелёным.

Ограничение, из которого растёт вся вёрстка: панель не грузит НИЧЕГО извне —
ни шрифтов, ни библиотек, ни картинок. Это часть её защиты (CSP запрещает
чужие источники и inline-скрипты), поэтому здесь нет ни одного скрипта:
переключатели — обычные формы, кольца и полоски — CSS-градиенты.
"""
import datetime as dt
import html
import sys
import traceback
import urllib.parse

import settings  # noqa: F401 — кладёт каталог scripts/lib в sys.path
import process_state

СТИЛЬ = """
:root{
  --фон:#0e0f12; --карта:#17191f; --карта2:#1d2028; --рамка:#252932;
  --текст:#f2f4f8; --тихо:#9aa3b2;
  --оранж:#ff5a1f; --фиолет:#7c5cff; --зелён:#22c55e; --красн:#ef4444;
}
*{box-sizing:border-box}
body{margin:0;padding:18px;background:var(--фон);color:var(--текст);
  font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.шапка{display:flex;align-items:center;justify-content:space-between;
  gap:12px;margin:4px 2px 18px}
.шапка h1{font-size:1.25rem;margin:0;font-weight:650;letter-spacing:-.01em}
.значок{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--оранж),var(--фиолет));font-size:1.1rem}
.сетка{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.карта{background:var(--карта);border:1px solid var(--рамка);border-radius:20px;
  padding:16px 18px}
.карта h2{margin:0 0 12px;font-size:.95rem;font-weight:600;color:var(--тихо);
  text-transform:none;letter-spacing:.01em}
.число{font-size:2rem;font-weight:700;letter-spacing:-.02em;line-height:1.1}
.мелко{color:var(--тихо);font-size:.86rem}
.строка{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
  padding:7px 0;border-bottom:1px solid var(--рамка)}
.строка:last-child{border-bottom:0}
.метка{color:var(--тихо);font-size:.9rem}
.полоса{height:8px;border-radius:99px;background:var(--карта2);overflow:hidden;
  margin-top:10px}
.полоса > i{display:block;height:100%;border-radius:99px}
.пилюля{display:inline-block;padding:5px 12px;border-radius:99px;font-size:.8rem;
  background:var(--карта2);color:var(--тихо);border:1px solid var(--рамка)}
.зелёная{background:rgba(34,197,94,.15);color:#7ee2a8;border-color:rgba(34,197,94,.35)}
.красная{background:rgba(239,68,68,.15);color:#ffa4a4;border-color:rgba(239,68,68,.35)}
.жёлтая{background:rgba(255,90,31,.15);color:#ffb38f;border-color:rgba(255,90,31,.4)}
.ручка{background:var(--карта);border:1px solid var(--рамка);border-radius:20px;
  padding:15px 18px}
.ручка .имя{font-weight:650;font-size:1.02rem}
.ручка.неизвестно{opacity:.55;border-style:dashed}
.ручка .пояснение{color:var(--тихо);font-size:.88rem;margin:6px 0 12px}
.кнопки{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button{font:inherit;font-size:.9rem;padding:8px 16px;border-radius:99px;
  border:1px solid var(--рамка);background:var(--карта2);color:var(--текст);
  cursor:pointer}
button.выбрано{background:linear-gradient(135deg,var(--оранж),var(--фиолет));
  border-color:transparent;color:#fff;font-weight:600}
button:disabled{opacity:.55;cursor:default}
form{display:inline}
input[type=number]{font:inherit;width:6.5rem;padding:8px 12px;border-radius:12px;
  border:1px solid var(--рамка);background:var(--карта2);color:var(--текст)}
.подсказка{color:var(--тихо);font-size:.78rem;margin-top:9px}
.вход{max-width:26rem;margin:12vh auto;text-align:center}
.вход button{padding:12px 26px;font-size:1rem}
"""


def _страница(заголовок: str, тело: str) -> str:
    return (f'<!doctype html><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(заголовок)}</title><style>{СТИЛЬ}</style>{тело}')


def гость() -> str:
    return _страница("Панель харнеса", f"""
<div class="вход">
  <div class="значок" style="margin:0 auto 14px">✳</div>
  <h1 style="font-size:1.2rem;margin:0 0 10px">Панель управления харнесом</h1>
  <p class="мелко">Чтобы войти, напишите боту слово <b>панель</b> — он пришлёт
  ссылку. Она живёт десять минут и срабатывает один раз.</p>
  <p class="подсказка">Бот не отвечает? <a href="/zapasnoj"
  style="color:var(--оранж)">Запасной вход</a> — по длинному коду,
  выданному при установке.</p>
</div>""")


def вход_по_ссылке(живой: bool) -> str:
    """Страница видна ДВУМ: предпросмотру и хозяину протухшей ссылки.

    Владелец 27.09.2026: «я бы убрал кнопку "войти", она не нужна, по ссылке
    надо сразу попадать в панель» — человек с браузером входит прямо из
    обработчика GET (server.py), сюда он больше не попадает. Кнопки нет и в
    теле: нажать её мог бы только предпросмотр, а ему вход не нужен.
    """
    if живой:
        тело = ('<p class="мелко">Ссылка жива. Откройте её в браузере — '
                'панель откроется сразу.</p>')
    else:
        тело = ('<p class="мелко">Ссылка устарела или уже использована. '
                'Напишите боту «панель» ещё раз.</p>')
    return _страница("Вход в панель", f"""
<div class="вход">
  <div class="значок" style="margin:0 auto 14px">✳</div>
  <h1 style="font-size:1.2rem;margin:0 0 14px">Вход в панель харнеса</h1>
  {тело}
</div>""")


def запасной_вход(сообщение: str = "") -> str:
    """Вход долгим кодом: когда бот молчит и ссылку прислать некому."""
    полоска = (f'<p class="подсказка" style="color:var(--оранж)">'
               f'{html.escape(сообщение)}</p>' if сообщение else "")
    return _страница("Запасной вход", f"""
<div class="вход">
  <div class="значок" style="margin:0 auto 14px">✳</div>
  <h1 style="font-size:1.2rem;margin:0 0 10px">Запасной вход</h1>
  <p class="мелко">Он нужен, только если бот не отвечает и прислать ссылку
  некому. Введите длинный код, выданный при установке.</p>
  {полоска}
  <form method="post" action="/zapasnoj">
    <input type="text" name="код" autocomplete="off" placeholder="XXXXX-XXXXX-…"
      style="font:inherit;width:100%;padding:10px 14px;margin:10px 0;
      border-radius:99px;border:1px solid var(--рамка);
      background:var(--карта2);color:var(--текст)">
    <button type="submit" class="выбрано">Войти</button>
  </form>
  <p class="подсказка">Вход открывается через минуту после ввода — введите код
  ещё раз. О каждой попытке владельцу уходит сообщение. Опасные переключатели
  такой сессии недоступны: подтверждать их в боте некому.</p>
</div>""")


def _цвет_доли(доля: float) -> str:
    """Чем ближе к пределу, тем тревожнее цвет. Порог — не вкус, а смысл:
    выше 85 % диска уборка уже не справляется (улика 03.08.2026)."""
    if доля >= 85:
        return "var(--красн)"
    if доля >= 70:
        return "var(--оранж)"
    return "var(--зелён)"


def _карта_работы(работа: dict) -> str:
    if работа.get("нет данных"):
        return (f'<div class="карта"><h2>Сейчас в работе</h2>'
                f'<div class="мелко">{html.escape(работа["нет данных"])}</div></div>')
    блокер = (f'<div class="пилюля жёлтая" style="margin-top:10px">'
              f'ждёт: {html.escape(работа["блокер"][:80])}</div>'
              if работа.get("блокер") else "")
    return (f'<div class="карта"><h2>Сейчас в работе</h2>'
            f'<div class="число" style="font-size:1.15rem">'
            f'{html.escape(работа.get("название") or работа["id"])}</div>'
            f'<div class="мелко">начата {html.escape(str(работа.get("начата") or "—"))}'
            f' · всего задач {работа.get("всего задач", "—")}</div>{блокер}</div>')


def _карта_проверок(проверки: dict) -> str:
    if проверки.get("нет данных"):
        return (f'<div class="карта"><h2>Проверки перед выкладкой</h2>'
                f'<div class="мелко">{html.escape(проверки["нет данных"])}</div></div>')
    зелёных, красных = проверки.get("зелёных", 0), проверки.get("красных", 0)
    всего = max(зелёных + красных, 1)
    пилюля = ("пилюля зелёная" if not красных else "пилюля красная")
    слово = "все прошли" if not красных else f"не прошли: {красных}"
    return (f'<div class="карта"><h2>Проверки перед выкладкой</h2>'
            f'<div class="число">{зелёных}<span class="мелко"> из {зелёных + красных}</span></div>'
            f'<div class="{пилюля}" style="margin-top:8px">{слово}</div>'
            f'<div class="полоса"><i style="width:{зелёных / всего * 100:.0f}%;'
            f'background:linear-gradient(90deg,var(--зелён),var(--фиолет))"></i></div>'
            f'<div class="подсказка">последний прогон {проверки.get("минут назад", "?")} мин назад'
            f'{" · устарело" if проверки.get("устарело") else ""}</div></div>')


def _карта_сторожей(сторожа: dict) -> str:
    if сторожа.get("нет данных"):
        return (f'<div class="карта"><h2>Сторожа</h2>'
                f'<div class="мелко">{html.escape(сторожа["нет данных"])}</div></div>')
    строки = сторожа["строки"]
    # Порог считает витрина (state.ПОТОЛКИ_ВОЗРАСТА), а не вид: иначе
    # «сутки» тут и «три часа» там — две правды об одном (спека, фаза 4).
    молчат = [с for с in строки if с.get("устарело")]
    доля = (len(строки) - len(молчат)) / max(len(строки), 1) * 100
    пилюля = "пилюля зелёная" if not молчат else "пилюля жёлтая"
    слово = "все отмечаются" if not молчат else f"устарели: {len(молчат)}"
    верх = sorted(строки, key=lambda с: -с["минут"])[:3]
    список = "".join(
        f'<div class="строка"><span class="метка">{html.escape(с["имя"])}</span>'
        f'<span title="{html.escape(str(с.get("пояснение", "")))}">'
        f'{с["минут"]} мин{" · устарело" if с.get("устарело") else ""}</span></div>'
        for с in верх)
    return (f'<div class="карта"><h2>Сторожа</h2>'
            f'<div class="число">{len(строки)}</div>'
            f'<div class="{пилюля}" style="margin-top:8px">{слово}</div>'
            f'<div class="полоса"><i style="width:{доля:.0f}%;'
            f'background:linear-gradient(90deg,var(--зелён),var(--оранж))"></i></div>'
            f'<div style="margin-top:10px">{список}</div></div>')


def _карта_канала(канал: dict) -> str:
    сколько = канал.get("в очереди", 0)
    когда = канал.get("последнее сообщение, минут назад")
    пилюля = "пилюля зелёная" if not сколько else "пилюля жёлтая"
    return (f'<div class="карта"><h2>Канал с вами</h2>'
            f'<div class="число">{сколько}</div>'
            f'<div class="{пилюля}" style="margin-top:8px">'
            f'{"всё разобрано" if not сколько else "ждут разбора"}</div>'
            f'<div class="подсказка">последнее сообщение '
            f'{html.escape(str(когда)) + " мин назад" if когда is not None else "не найдено"}</div></div>')


def _карта_машины(машина: dict, расход: dict) -> str:
    доля = машина.get("диск занято %", 0)
    # Числа расхода машинные: проценты собирает экран, а не сборщик. И если
    # замера ещё нет (сборщик ходит раз в час), карточка НАЗЫВАЕТ причину —
    # два прочерка владелец читал как «всё по нулям» (ревью 13.09.2026).
    нет_данных = расход.get("нет данных")
    доля_кэша = расход.get("доля попаданий кэша")
    попаданий = нет_данных or (f"{доля_кэша * 100:.1f}%".replace(".", ",")
                               if доля_кэша is not None else "—")
    шагов = нет_данных or расход.get("шагов")
    шагов = "—" if шагов is None else шагов
    return (f'<div class="карта"><h2>Машина и расход</h2>'
            f'<div class="число">{доля} %<span class="мелко"> диска занято</span></div>'
            f'<div class="полоса"><i style="width:{доля}%;background:{_цвет_доли(доля)}"></i></div>'
            f'<div class="строка" style="margin-top:12px">'
            f'<span class="метка">обращений за сутки</span><span>{html.escape(str(шагов))}</span></div>'
            f'<div class="строка"><span class="метка">экономия на памяти</span>'
            f'<span>{html.escape(str(попаданий))}</span></div>'
            f'<div class="строка"><span class="метка">службы</span>'
            f'<span>{html.escape(str(машина.get("системные службы", "—")))}</span></div></div>')


def _term_state(сейчас: str) -> str:
    """Слова о режиме с ограниченным сроком: выключен / включён до / срок истёк."""
    try:
        end = dt.datetime.fromisoformat(сейчас) if сейчас else None
    except ValueError:
        end = None
    if end is None or end.tzinfo is None:
        return "выключен"
    if end <= dt.datetime.now(dt.timezone.utc):
        return "срок истёк"
    return f"включён до {end:%d.%m %H:%M}"


def _ручка(строка: dict, csrf: str) -> str:
    имя = html.escape(строка["имя"])
    пояснение = html.escape(строка.get("простыми словами", "").strip())
    сейчас = "" if строка.get("сейчас") is None else str(строка.get("сейчас"))
    # Ручка, чьё значение панели неизвестно, не притворяется настроенной:
    # серая, без кнопок, с причиной сверху (спека §10.1.4). Прежде здесь
    # печаталось слово «None», и владелец видел его как значение.
    if строка.get("состояние") == "неизвестно":
        почему = html.escape(str(строка.get("почему", "панель не смогла прочитать настройку")))
        return (f'<div class="ручка неизвестно"><div class="имя">{имя}</div>'
                f'<div class="пояснение">{пояснение}</div>'
                f'<div class="подсказка">Панель не знает: {почему}. '
                f'Переключать нечего, пока настройка не читается.</div></div>')
    опасно = ('<div class="подсказка">переключение подтверждается кодом в боте</div>'
              if строка.get("опасный") else
              f'<div class="подсказка">применяется {html.escape(строка["применяется"])}</div>')
    term_state = ""
    if строка["вид"] == "срок":
        term_state = f'<div class="подсказка">{html.escape(_term_state(сейчас))}</div>'
        # В конфиге лежит момент, а не «0»/«72»: «выключен» недоступен только
        # при пустом значении — истёкший или негодный момент надо уметь стереть.
        сейчас = "" if сейчас else "0"
    if строка["вид"] in ("выбор", "срок"):
        кнопки = []
        for значение in строка.get("значения") or []:
            з = str(значение.get("значение"))
            подпись = html.escape(str(значение.get("подпись", з)))
            выбрано = "выбрано" if з == сейчас else ""
            отключено = "disabled" if з == сейчас else ""
            кнопки.append(
                f'<form method="post" action="/kljuch">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
                f'<input type="hidden" name="ключ" value="{html.escape(строка["ключ"])}">'
                f'<input type="hidden" name="значение" value="{html.escape(з)}">'
                f'<button class="{выбрано}" {отключено}>{подпись}</button></form>')
        управление = "".join(кнопки)
    else:
        управление = (
            f'<form method="post" action="/kljuch" class="кнопки">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            f'<input type="hidden" name="ключ" value="{html.escape(строка["ключ"])}">'
            f'<input type="number" name="значение" value="{html.escape(сейчас)}" '
            f'min="{строка.get("от", 0)}" max="{строка.get("до", 100)}">'
            f'<button class="выбрано">Сохранить</button></form>'
            f'<div class="подсказка">от {строка.get("от")} до {строка.get("до")}'
            f'{" " + html.escape(строка["единица"]) if строка.get("единица") else ""}</div>')
    метка = ('<div class="подсказка">сейчас умолчание: ключа в настройках нет</div>'
             if строка.get("состояние") == "умолчание" else "")
    return (f'<div class="ручка"><div class="имя">{имя}</div>'
            f'<div class="пояснение">{пояснение}</div>'
            f'{term_state}<div class="кнопки">{управление}</div>{метка}{опасно}</div>')


def _шаги(шаги: dict, csrf: str) -> str:
    """Шаги работы по уровням задачи, каждый — включаемый.

    Концов пайплайна (проверок перед выкладкой и живой пробы на бою) здесь нет
    вовсе: ими держатся инварианты, и выключателя у них не существует.
    """
    if шаги.get("нет данных"):
        return (f'<div class="карта"><h2>Шаги работы</h2>'
                f'<div class="мелко">{html.escape(шаги["нет данных"])}</div></div>')
    подписи = {"мелкая": "мелкая правка", "обычная": "обычная задача",
               "крупная": "крупная задача"}
    куски = []
    for уровень, включённые in шаги["уровни"].items():
        строки = []
        for шаг in шаги["все шаги"]:
            включён = шаг in включённые
            действие = "выключить" if включён else "включить"
            строки.append(
                f'<form method="post" action="/shag">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
                f'<input type="hidden" name="уровень" value="{html.escape(уровень)}">'
                f'<input type="hidden" name="шаг" value="{html.escape(шаг)}">'
                f'<input type="hidden" name="действие" value="{действие}">'
                f'<button class="{"выбрано" if включён else ""}" '
                f'title="{html.escape(шаги["поясняет"].get(шаг, ""))}">'
                f'{html.escape(шаг.replace("_", " "))}</button></form>')
        куски.append(f'<div class="карта"><h2>{подписи[уровень]}</h2>'
                     f'<div class="кнопки">{"".join(строки)}</div>'
                     f'<div class="подсказка">подсвеченные — обязательны; '
                     f'нажатие включает или выключает</div></div>')
    пояснения = "".join(
        f'<div class="строка"><span class="метка">{html.escape(ш.replace("_", " "))}</span>'
        f'<span class="мелко" style="text-align:right;max-width:60%">'
        f'{html.escape(т)}</span></div>'
        for ш, т in шаги["поясняет"].items())
    куски.append(f'<div class="карта"><h2>Что значит каждый шаг</h2>{пояснения}'
                 f'<div class="подсказка">Проверки перед выкладкой и живая проба '
                 f'на рабочем сервере выключателя не имеют вовсе — ими держится '
                 f'обещание «готово значит проверено».</div></div>')
    return "".join(куски)


def _карта_обновлений(данные: dict) -> str:
    версия = (данные.get("версия") or {}).get("коммит", "")
    подпись = f"версия {версия[:7]}" if версия else "версия ещё не записана"
    if данные.get("нет данных"):
        return (f'<div class="карта"><h2>Версия харнеса</h2>'
                f'<div class="мелко">{html.escape(данные["нет данных"])}</div>'
                f'<div class="подсказка">{html.escape(подпись)}</div></div>')
    цвет = {"есть новая версия": "var(--оранж)",
            "спросить не удалось": "var(--тихо)",
            "обновление откатилось": "var(--красн)"}.get(данные["состояние"],
                                                         "var(--зелён)")
    return (f'<div class="карта"><h2>Версия харнеса</h2>'
            f'<div class="число" style="font-size:1.1rem;color:{цвет}">'
            f'{html.escape(данные["состояние"])}</div>'
            f'<div class="мелко">{html.escape(подпись)} · проверено '
            f'{данные.get("проверено минут назад", "?")} мин назад</div>'
            f'<div class="подсказка">{html.escape(данные.get("строка", ""))}</div></div>')


def панель(снимок: dict, ручки: list[dict], csrf: str, сообщение: str = "") -> str:
    шапка = ('<div class="шапка"><div style="display:flex;align-items:center;gap:12px">'
             '<div class="значок">✳</div><h1>Панель управления харнесом</h1></div>'
             '<div class="кнопки">'
             '<a href="/karta"><button>Карта работы</button></a>'
             '<a href="/ustrojstvo"><button>Как устроено</button></a>'
             '<a href="/processy"><button>Процессы</button></a>'
             '<a href="/pamyat"><button>Память</button></a>'
             '<form method="post" action="/vyjti">'
             f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
             '<button>Выйти</button></form></div></div>')
    полоска = (f'<div class="карта" style="border-color:var(--оранж);margin-bottom:14px">'
               f'{html.escape(сообщение)}</div>' if сообщение else "")
    витрина = ('<div class="сетка">'
               + _карта_работы(снимок["работа"])
               + _карта_проверок(снимок["проверки"])
               + _карта_сторожей(снимок["сторожа"])
               + _карта_канала(снимок["канал"])
               + _карта_машины(снимок["машина"], снимок["расход"])
               + _карта_обновлений(снимок.get("обновления", {"нет данных": "нет данных"}))
               + '</div>')
    заголовок_шагов = ('<h2 style="font-size:1rem;color:var(--тихо);margin:22px 2px 12px">'
                       'Как я работаю над задачей</h2>')
    экран_шагов = ('<div class="сетка">' + _шаги(снимок.get("шаги", {}), csrf)
                   + '</div>')
    заголовок_ручек = ('<h2 style="font-size:1rem;color:var(--тихо);margin:22px 2px 12px">'
                       'Чем можно управлять</h2>')
    список = ('<div class="сетка">'
              + "".join(_ручка(с, csrf) for с in ручки) + '</div>')
    низ = (f'<div class="подсказка" style="margin:18px 2px">Снято в '
           f'{html.escape(снимок["снято"])}. Опасные переключатели спрашивают '
           f'подтверждение в боте — даже с открытой панелью их нельзя переключить '
           f'без доступа к вашему чату.</div>')
    return _страница("Панель харнеса", шапка + полоска + витрина +
                     заголовок_шагов + экран_шагов +
                     заголовок_ручек + список + низ)


def память(список: dict, csrf: str, искать: str = "", сообщение: str = "") -> str:
    """Список записей памяти: поиск, признак набора, переход к правке."""
    if список.get("нет данных"):
        тело = f'<div class="карта">{html.escape(список["нет данных"])}</div>'
        return _страница("Память харнеса", тело)
    строки = []
    for з in список["строки"]:
        пилюля = ('<span class="пилюля зелёная">в наборе новой установки</span>'
                  if з["в наборе"] else '<span class="пилюля">только здесь</span>')
        действие = "исключить" if з["в наборе"] else "включить"
        строки.append(
            f'<div class="ручка"><div class="имя">'
            f'<a href="/pamyat?имя={html.escape(з["имя"])}" '
            f'style="color:inherit">{html.escape(з["имя"])}</a></div>'
            f'<div class="пояснение">{html.escape(з["тема"] or "—")}</div>'
            f'<div class="кнопки">{пилюля}'
            f'<form method="post" action="/nabor">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            f'<input type="hidden" name="имя" value="{html.escape(з["имя"])}">'
            f'<input type="hidden" name="действие" value="{действие}">'
            f'<button>{действие} в набор</button></form>'
            f'<span class="мелко">{з["знаков"]} знаков</span></div></div>')
    полоска = (f'<div class="карта" style="border-color:var(--оранж);margin-bottom:14px">'
               f'{html.escape(сообщение)}</div>' if сообщение else "")
    шапка = ('<div class="шапка"><div style="display:flex;align-items:center;gap:12px">'
             '<div class="значок">✳</div><h1>Память харнеса</h1></div>'
             '<a href="/"><button>На панель</button></a></div>')
    поиск = (f'<form method="get" action="/pamyat" class="кнопки" '
             f'style="margin:0 2px 14px">'
             f'<input type="text" name="искать" value="{html.escape(искать)}" '
             f'placeholder="искать по словам" '
             f'style="font:inherit;flex:1;min-width:12rem;padding:8px 14px;'
             f'border-radius:99px;border:1px solid var(--рамка);'
             f'background:var(--карта2);color:var(--текст)">'
             f'<button class="выбрано">Найти</button></form>')
    сводка = (f'<div class="карта" style="margin-bottom:14px">'
              f'<div class="число">{len(список["строки"])}'
              f'<span class="мелко"> из {список["всего"]} записей</span></div>'
              f'<div class="подсказка">В наборе новой установки: '
              f'{список["в наборе"]}. Эти записи достанутся новому харнесу, '
              f'когда его поставят с нуля.</div></div>')
    return _страница("Память харнеса",
                     шапка + полоска + поиск + сводка +
                     '<div class="сетка">' + "".join(строки) + '</div>')


def правка_записи(запись: dict, csrf: str) -> str:
    if запись.get("нет данных"):
        return _страница("Запись памяти",
                         f'<div class="карта">{html.escape(запись["нет данных"])}</div>')
    шапка = ('<div class="шапка"><div style="display:flex;align-items:center;gap:12px">'
             '<div class="значок">✳</div>'
             f'<h1>{html.escape(запись["имя"])}</h1></div>'
             '<a href="/pamyat"><button>К списку</button></a></div>')
    форма = (f'<form method="post" action="/pamyat">'
             f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
             f'<input type="hidden" name="имя" value="{html.escape(запись["имя"])}">'
             f'<textarea name="текст" rows="24" style="width:100%;font:14px/1.5 '
             f'ui-monospace,SFMono-Regular,Menlo,monospace;padding:14px;'
             f'border-radius:16px;border:1px solid var(--рамка);'
             f'background:var(--карта2);color:var(--текст)">'
             f'{html.escape(запись["текст"])}</textarea>'
             f'<div class="кнопки" style="margin-top:12px">'
             f'<button class="выбрано">Сохранить</button>'
             f'<span class="подсказка">прежний текст уходит в копию, '
             f'правка сама попадёт в хранилище кода</span></div></form>')
    return _страница(запись["имя"], шапка + f'<div class="карта">{форма}</div>')

ПОДПИСИ_ВИДОВ = {
    "демон": ("Работают сами по расписанию",
              "Никто их не запускает: следят за машиной, памятью, рабочими "
              "окнами и копиями данных."),
    "хук": ("Срабатывают прямо во время работы",
            "Перехватывают момент, когда я запускаю команду, пишу файл или "
            "заканчиваю ход."),
    "гейт": ("Проверки перед записью и выкатом",
             "Красная проверка останавливает работу — в этом её смысл."),
    "скил": ("Правила шагов работы",
             "Пошаговые инструкции: как принять задачу, описать её, проверить "
             "и закрыть."),
}


def _механизм(строка: dict) -> str:
    return (f'<div class="ручка"><div class="имя">{html.escape(строка["имя"])}</div>'
            f'<div class="пояснение">{html.escape(строка["роль"])}</div>'
            f'<div class="пояснение" style="margin-top:8px">'
            f'<b>Когда:</b> {html.escape(строка["когда"])}<br>'
            f'<b>Без него:</b> {html.escape(строка["без него"])}<br>'
            f'<b>Что пишет:</b> {html.escape(строка["пишет"])}</div>'
            f'<div class="мелко" style="margin-top:8px">'
            f'{html.escape(строка["файл"])}</div></div>')


# ── доска (канбан) и страница задачи — спека hr.kanban §2, §5 ───────────────
# Колонки идут вбок и прокручиваются пальцем: на телефоне 390 px десять колонок
# в ряд не помещаются, а перенос строки смешал бы порядок шагов.
BOARD_STYLE = """
.board{display:flex;align-items:flex-start;gap:12px;overflow-x:auto;padding-bottom:10px;
  scroll-snap-type:x proximity;-webkit-overflow-scrolling:touch}
.board-column{flex:0 0 250px;background:var(--карта);border:1px solid var(--рамка);
  border-radius:18px;padding:12px;scroll-snap-align:start;
  max-height:calc(100vh - 32px);overflow-y:auto}
.board-column h3{margin:0 0 10px;font-size:.92rem;color:var(--тихо);font-weight:600}
.board-card{display:block;background:var(--карта2);border:1px solid var(--рамка);
  border-radius:14px;padding:10px 12px;margin-bottom:8px;color:inherit;
  text-decoration:none}
.board-card .пилюля{margin-top:6px;white-space:normal;border-radius:12px;display:block}
.board-steps{margin-top:6px;line-height:1.5}
.problem{border-color:var(--красн);margin-bottom:14px}
.stale{border-color:var(--оранж);margin-bottom:14px}
pre{white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,Menlo,monospace}
textarea{width:100%;font:inherit;padding:10px 14px;border-radius:14px;
  border:1px solid var(--рамка);background:var(--карта2);color:var(--текст)}
a{color:var(--оранж)}
"""
UNASSIGNED_COLUMN = "колонка не названа"
STEP_MARKS = {True: "✓", False: "✗", None: "○"}


def _board_page(title: str, body: str) -> str:
    return _страница(title, f"<style>{BOARD_STYLE}</style>{body}")


def _header(title: str, back: str, back_label: str) -> str:
    return ('<div class="шапка"><div style="display:flex;align-items:center;gap:12px">'
            f'<div class="значок">✳</div><h1>{html.escape(title)}</h1></div>'
            f'<a href="{back}"><button>{back_label}</button></a></div>')


def _pills(pills: list) -> str:
    return "".join(f'<div class="пилюля">{html.escape(str(p))}</div>' for p in pills or [])


def _clock(moment) -> str:
    """ЧЧ:ММ из ISO-времени; непонятное время — как есть."""
    text = str(moment or "")
    return text[11:16] if len(text) >= 16 and text[10] == "T" else (text or "—")


def _or_dash(value) -> str:
    """Величины нет (снимок не прочитал её) — прочерк, а не «None» и не 0."""
    return "—" if value is None else str(value)


def task_href(task_id: str) -> str:
    return "/karta/" + urllib.parse.quote(str(task_id), safe="")


def _card_steps(task: dict) -> str:
    """Пройденный путь на самой карточке: колонка называет, где задача сейчас,
    а владелец спрашивает и про то, что пройдено."""
    steps = [s for s in task.get("шаги") or [] if isinstance(s, dict)]
    if not steps:
        return ""
    marks = " · ".join(f'{STEP_MARKS.get(s.get("есть"), "○")} '
                       f'{html.escape(str(s.get("шаг", "")).replace("_", " "))}' for s in steps)
    return f'<div class="мелко board-steps">{marks}</div>'


def board(snapshot: dict) -> str:
    """Канбан: колонки из снимка в его порядке, все задачи без обрезки."""
    header = _header("Карта работы", "/", "На панель")
    if snapshot.get("нет данных"):
        return _board_page("Карта работы", header + (
            f'<div class="карта problem">Доска не показана: '
            f'{html.escape(snapshot["нет данных"])}; задач в карте не видно, '
            f'пока снимка нет.</div>'))
    tasks = [t for t in snapshot["задачи"] if isinstance(t, dict)]
    columns = list(snapshot.get("колонки") or [])
    by_column = {column: [] for column in columns}
    for task in tasks:
        by_column.setdefault(task.get("колонка") if task.get("колонка") in by_column
                             else UNASSIGNED_COLUMN, []).append(task)
    stale = (f'<div class="карта stale">доска устарела: снимок '
             f'{html.escape(_clock(snapshot.get("снято")))}, демон build-board молчит'
             f'</div>' if snapshot.get("устарела") else "")
    problem = (f'<div class="карта problem">{html.escape(str(snapshot["беда"]))}</div>'
               if snapshot.get("беда") else "")
    summary = (f'<div class="карта" style="margin-bottom:14px">'
               f'<div class="число">{len(tasks)}<span class="мелко"> задач в карте</span></div>'
               f'<div class="подсказка">направление '
               f'{html.escape(str(snapshot.get("направление") or "все"))} · '
               f'в архиве: {html.escape(_or_dash(snapshot.get("архив")))} · снято '
               f'{html.escape(_clock(snapshot.get("снято")))}. Стадию ставит гейт '
               f'пайплайна, а не рука: доска — его снимок.</div></div>')
    rendered = []
    for column, column_tasks in by_column.items():
        cards = "".join(
            f'<a class="board-card" href="{task_href(t.get("id", ""))}">'
            f'<div style="font-weight:600">{html.escape(str(t.get("название") or t.get("id")))}</div>'
            f'<div class="мелко">{html.escape(str(t.get("id", "")))}</div>'
            f'{_card_steps(t)}{_pills(t.get("пилюли"))}</a>' for t in column_tasks)
        rendered.append(f'<section class="board-column"><h3>{html.escape(column)} · '
                        f'{len(column_tasks)}</h3>'
                        f'{cards or "<div class=мелко>пусто</div>"}</section>')
    return _board_page("Карта работы", header + stale + problem + summary
                       + f'<div class="board">{"".join(rendered)}</div>')


def _section(title: str, body: str) -> str:
    return (f'<div class="карта" style="margin-bottom:14px"><h2>{html.escape(title)}</h2>'
            f'{body}</div>')


def _steps_block(task: dict) -> str:
    rows = []
    for number, step in enumerate(task.get("шаги") or []):
        where = (f'<a href="{task_href(task["id"])}/fajl?n={number}">'
                 f'{html.escape(str(step["путь"]))}</a>' if step.get("путь") else "")
        why = html.escape(str(step.get("почему") or ""))
        rows.append(f'<div class="строка"><span>{STEP_MARKS.get(step.get("есть"), "○")} '
                    f'{html.escape(str(step.get("шаг", "")).replace("_", " "))}</span>'
                    f'<span class="мелко" style="text-align:right">'
                    f'{" · ".join(part for part in (where, why) if part)}</span></div>')
    return "".join(rows) or '<div class="мелко">шагов в снимке нет</div>'


def _sources_block(task: dict) -> str:
    rows = []
    for source in task.get("источники") or []:
        line = html.escape(str(source.get("строка") or source.get("id") or ""))
        text = (f'<pre>{html.escape(str(source["текст"]))}</pre>'
                if source.get("текст") else "")
        rows.append(f'<div class="мелко">{line}</div>{text}')
    return "".join(rows) or '<div class="мелко">источник не назван</div>'


def _commits_block(task: dict) -> str:
    return "".join(
        f'<div class="строка"><span><code>{html.escape(str(c.get("хеш", "")))}</code> '
        f'{html.escape(str(c.get("тема", "")))}</span>'
        f'<span class="мелко">{html.escape(str(c.get("дата", ""))[:16])}</span></div>'
        for c in task.get("коммиты") or []) or '<div class="мелко">коммитов с меткой задачи нет</div>'


def _skills_block(task: dict) -> str:
    rows = []
    for call in task.get("скилы") or []:
        attributed = (" · приписано по истории"
                      if call.get("откуда_задача") == "история" else "")
        rows.append(f'<div class="строка"><span>{html.escape(str(call.get("скил", "")))}'
                    f'<span class="мелко">{attributed}</span></span>'
                    f'<span class="мелко">{html.escape(str(call.get("ts", ""))[:16])}</span></div>')
    return "".join(rows) or '<div class="мелко">вызовов скилов нет</div>'


def _delivery_word(row: dict | None) -> str:
    if row is None:
        return "ждёт доставки"
    if row.get("исход") == "битая":
        return html.escape(f"не доставлено: {row.get('причина') or 'заявка негодна'}")
    return f'доставлено агенту {html.escape(_clock(row.get("доставлено")))}'


def _comments_block(task: dict, comments: dict, csrf: str) -> str:
    if comments.get("нет данных"):
        rows = f'<div class="мелко">{html.escape(comments["нет данных"])}</div>'
    else:
        rows = "".join(
            f'<div class="строка"><span style="white-space:pre-wrap">'
            f'{html.escape(str(c.get("текст", "")))}</span><span class="мелко">'
            + _delivery_word(c.get("доставка"))
            + '</span></div>' for c in comments["строки"])
    form = (f'<form method="post" action="/kommentarij" style="display:block;margin-top:12px">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            f'<input type="hidden" name="id" value="{html.escape(str(task["id"]))}">'
            f'<textarea name="текст" rows="4" placeholder="комментарий к задаче"></textarea>'
            f'<div class="кнопки" style="margin-top:8px">'
            f'<button class="выбрано">Отправить агенту</button></div></form>')
    return (rows or '<div class="мелко">комментариев нет</div>') + form


def task_page(task: dict, comments: dict, csrf: str, message: str = "") -> str:
    """Страница задачи: шапка, слова владельца, шаги, журнал, коммиты, скилы,
    комментарии (спека §5)."""
    notice = (f'<div class="карта stale">{html.escape(message)}</div>' if message else "")
    head = (f'<div class="карта" style="margin-bottom:14px">'
            f'<div class="число" style="font-size:1.15rem">'
            f'{html.escape(str(task.get("название") or task["id"]))}</div>'
            f'<div class="мелко">{html.escape(str(task["id"]))} · статус '
            f'{html.escape(str(task.get("статус", "")))} · колонка '
            f'{html.escape(str(task.get("колонка", "")))} · уровень объявлен '
            f'{html.escape(str(task.get("уровень_объявлен") or "—"))}, по коммитам '
            f'{html.escape(str(task.get("уровень_по_коммитам") or "—"))} · цена '
            f'{html.escape(str(task.get("цена") if task.get("цена") is not None else "—"))}</div>'
            f'<div class="кнопки" style="margin-top:8px">{_pills(task.get("пилюли"))}</div></div>')
    journal = (f'<pre>{html.escape(str(task["журнал"]))}</pre>' if task.get("журнал")
               else '<div class="мелко">журнала нет</div>')
    return _board_page(str(task.get("название") or task["id"]),
                       _header("Задача", "/karta", "К доске") + notice + head
                       + _section("Слова владельца", _sources_block(task))
                       + _section("Шаги пайплайна", _steps_block(task))
                       + _section("Журнал", journal)
                       + _section("Коммиты", _commits_block(task))
                       + _section("Скилы", _skills_block(task))
                       + _section("Комментарии", _comments_block(task, comments, csrf)))


def paper(task: dict, number: str, code: int, text: str) -> str:
    """Бумага задачи текстом; отказ — причиной словами."""
    body = (f'<pre>{html.escape(text)}</pre>' if code == 200
            else f'<div class="мелко">{html.escape(text)}</div>')
    return _board_page(f"Бумага {task['id']}",
                       _header(f"Бумага {number}", task_href(task["id"]), "К задаче")
                       + f'<div class="карта">{body}</div>')


def notice(title: str, message: str, back: str = "/karta") -> str:
    return _board_page(title, _header(title, back, "Назад")
                       + f'<div class="карта">{html.escape(message)}</div>')


def устройство(данные: dict, искать: str = "") -> str:
    """Экран «как всё устроено»: тот же реестр, что и в документе."""
    шапка = ('<div class="шапка"><div style="display:flex;align-items:center;gap:12px">'
             '<div class="значок">✳</div><h1>Как устроен харнес</h1></div>'
             '<a href="/"><button>На панель</button></a></div>')
    if данные.get("нет данных"):
        return _страница("Устройство харнеса",
                         шапка + f'<div class="карта">'
                         f'{html.escape(данные["нет данных"])}</div>')
    поиск = (f'<form method="get" action="/ustrojstvo" class="кнопки" '
             f'style="margin:0 2px 14px">'
             f'<input type="text" name="искать" value="{html.escape(искать)}" '
             f'placeholder="искать по словам" '
             f'style="font:inherit;flex:1;min-width:12rem;padding:8px 14px;'
             f'border-radius:99px;border:1px solid var(--рамка);'
             f'background:var(--карта2);color:var(--текст)">'
             f'<button class="выбрано">Найти</button></form>')
    сводка = (f'<div class="карта" style="margin-bottom:14px">'
              f'<div class="число">{данные["найдено"]}'
              f'<span class="мелко"> из {данные["всего"]} частей</span></div>'
              f'<div class="подсказка">Каждая часть описана в одном месте, '
              f'и проверка не даёт списку отстать от того, что есть на самом '
              f'деле.</div></div>')
    разделы = []
    for группа in данные["группы"]:
        заголовок, пояснение = ПОДПИСИ_ВИДОВ.get(
            группа["вид"], (группа["вид"], ""))
        разделы.append(
            f'<h2 style="font-size:1rem;color:var(--тихо);margin:22px 2px 6px">'
            f'{html.escape(заголовок)} — {len(группа["строки"])}</h2>'
            f'<div class="подсказка" style="margin:0 2px 12px">'
            f'{html.escape(пояснение)}</div>'
            f'<div class="сетка">'
            + "".join(_механизм(с) for с in группа["строки"]) + '</div>')
    return _страница("Устройство харнеса",
                     шапка + поиск + сводка + "".join(разделы))


# ───────────────────────── раздел «Процессы» ─────────────────────────

# Ширины: ничего жёстче 360 px вне @media (min-width…); таблицы на узком экране
# складываются в блоки по data-label, горизонтальной прокрутки у страницы нет.
PROCESS_STYLE = """
html{scroll-behavior:smooth}
a{color:var(--оранж)}
a.пилюля{color:var(--текст);text-decoration:none}
a.пилюля.вкл{background:linear-gradient(135deg,var(--оранж),var(--фиолет));
  border-color:transparent;color:#fff;font-weight:600}
.раздел .пилюля,.якоря .пилюля{font-size:.88rem}
.раздел .мелко{font-size:.9rem}
.якоря{display:flex;flex-wrap:wrap;gap:8px;margin:0 2px 18px}
.раздел{margin-top:16px;scroll-margin-top:12px}
.раздел .карта{padding:12px 14px}
.раздел .карта h2{margin:0 0 6px}
.раздел > h2{font-size:1.1rem;margin:0 2px 4px}
.раздел > .мелко{margin:0 2px 12px}
.фильтры{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px}
.фильтры form{display:flex;flex:1 1 200px;gap:8px;min-width:0}
.фильтры input[type=text]{font:inherit;flex:1;min-width:0;padding:8px 14px;border-radius:99px;
  border:1px solid var(--рамка);background:var(--карта2);color:var(--текст)}
.карточки{display:grid;gap:14px;grid-template-columns:minmax(0,1fr)}
.карточка{background:var(--карта);border:1px solid var(--рамка);border-radius:20px;
  padding:14px 16px;overflow-wrap:anywhere;min-width:0}
.карточка h3{margin:0 0 8px;font-size:1.02rem;font-weight:650;line-height:1.3}
.метки{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.серая{opacity:.8}
.расписание{color:var(--тихо);font-size:.9rem;margin-bottom:6px}
.что{margin:0 0 8px;font-size:.92rem}
details{margin-top:8px}
summary{cursor:pointer;color:var(--тихо);font-size:.9rem}
.класс{background:var(--карта2);border:1px solid var(--рамка);border-radius:20px;padding:12px 14px;margin:0 0 12px}
.класс > summary{color:var(--текст);font-size:1rem;font-weight:600;line-height:1.35}
.класс .итог-класса{display:block;color:var(--тихо);font-size:.9rem;font-weight:400}
.класс > .карточки,.класс > .мелко{margin-top:12px}
.подробно{margin-top:8px;font-size:.9rem}
.подробно dt{color:var(--тихо);margin-top:8px}
.подробно dd{margin:2px 0 0}
.подробно ul{margin:2px 0 0;padding-left:18px}
.файлы{font-size:.75rem;color:var(--тихо);word-break:break-all}
.итоги{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(0,1fr));margin:12px 0}
.итог{background:var(--карта);border:1px solid var(--рамка);border-radius:16px;padding:10px 12px;min-width:0}
.итог .число{font-size:1.4rem;line-height:1.2;overflow-wrap:anywhere}
.группы{display:grid;gap:10px;grid-template-columns:minmax(0,1fr);margin:12px 0}
.доля{margin-top:3px;padding:2px 8px;border-radius:8px;background-color:var(--карта2);
  background-image:linear-gradient(90deg,rgba(255,90,31,.45),rgba(124,92,255,.45));
  background-repeat:no-repeat;background-size:var(--ширина,0) 100%}
.доля-строка{display:flex;justify-content:space-between;gap:10px;font-size:.9rem;line-height:1.3}
.доля-строка span:first-child{min-width:0;overflow-wrap:anywhere}
.доля-строка span:last-child{white-space:nowrap}
.таблица{width:100%;border-collapse:collapse;font-size:.9rem}
.таблица th{text-align:left;color:var(--тихо);font-weight:500;padding:6px 8px;
  border-bottom:1px solid var(--рамка)}
.таблица td{padding:7px 8px;border-bottom:1px solid var(--рамка);vertical-align:top;overflow-wrap:anywhere}
.тихо{color:var(--тихо)}
.пусто{padding:14px 16px;color:var(--тихо)}
@media (min-width:700px){
  .карточки{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
  .итоги{grid-template-columns:repeat(4,minmax(0,1fr))}
}
@media (min-width:900px){
  .группы{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:600px){
  .таблица,.таблица tbody,.таблица tr{display:block}
  .таблица thead{display:none}
  .таблица tr{background:var(--карта);border:1px solid var(--рамка);border-radius:16px;
    padding:6px 12px;margin-bottom:8px}
  .таблица td{display:grid;grid-template-columns:minmax(0,7.5em) minmax(0,1fr);gap:6px;
    border-bottom:0;padding:2px 0;line-height:1.3}
  .таблица td::before{content:attr(data-label);color:var(--тихо)}
  .таблица td:first-child{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px;font-weight:600;padding-bottom:5px}
  .таблица td:first-child details{margin-top:0;font-weight:400}
  .таблица td:first-child::before{display:none}
  .таблица td.пуст{display:none}
  .расходы tr{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0 10px;padding:4px 10px;font-size:13px}
  .расходы td{display:block;overflow-wrap:anywhere;padding:0;line-height:1.25}
  .расходы td::before{content:attr(data-label) ": ";color:var(--тихо)}
  .расходы td:first-child{display:block;font-weight:600;padding-bottom:0}
  .расходы td[data-label="Запуски"]{grid-column:1 / -1}
  .расходы td[data-label="Состояние"]::before,.расходы td:first-child::before{display:none}
  .расходы td[data-label="Состояние"]{text-align:right}
  .расходы td[data-label="Состояние"] .мелко{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
}
"""

PROCESS_WINDOWS = {"24ch": ("24ч", "24 ч"), "7d": ("7д", "7 сут")}
PROCESS_NOW_WINDOW = "сейчас"          # «10 мин» — отдельный столбец таблицы, не вкладка
PROCESS_SHOWN = 20
PROCESS_ROLLUP = "Системные службы сервера"
PROCESS_TABLE_KINDS = ("constant", "scheduled", "boot")
PROCESS_SUMMED = ("доля_ёмкости_проц", "ядро_ч", "память_пик_мб", "токены", "доля_токенов_проц", "запусков")
PROCESS_WHY_LIMIT = 120
PROCESS_WINDOW_HOURS = {"24ч": 24, "7д": 168}
PROCESS_PROTECTED_STATES = ("failed", "skipped", "stopped", "unknown")
PROCESS_STATE_COLORS = {"running": "зелёная", "done": "зелёная", "failed": "красная", "stopped": "жёлтая",
                        "skipped": "жёлтая", "disabled": "жёлтая"}
PROCESS_SORTS = {"cpu": "ядро_ч", "mem": "память_пик_мб", "runs": "запусков", "tok": "токены"}
PROCESS_FILTER_CLASSES = ("фоновый", "по внутреннему действию", "сторонний")
PROCESS_FILTER_STATES = {"idut": "включены", "pogasheny": "погашены ключом", "isklyucheniya": "исключения"}
PROCESS_CLASS_WORDS = {"фоновый": "фоновый: работает сам, по расписанию или службой",
                       "по внутреннему действию": "по внутреннему действию: запускается действием, не по времени",
                       "сторонний": "сторонний: чужая программа на сервере"}
PROCESS_LABELS = {"claude-session": "Основная сессия Claude", "helpers": "Помощники",
                  "helpers: без метки": "Помощники без метки", "модель: голова без метки": "Основная модель без метки",
                  "ask-the-model": "Вопрос модели", "cgroup-корень": "Корень cgroup",
                  "прочее": "Прочие процессы сервера"}
PROCESS_CRON_PREFIX = "крон:"
PROCESS_CLASS_TITLES = {"фоновый": "Фоновые процессы", "по внутреннему действию": "Процессы по внутреннему действию",
                        "сторонний": "Сторонние процессы"}
PROCESS_BARS = (("cpu", "Ядра: доля ёмкости", "доля_ёмкости_проц", "%"),
                ("mem", "Память: пик", "память_пик_мб", "МБ"),
                ("runs", "Запуски: доля времени", "доля_запусков_проц", "%"),
                ("tok", "Токены: доля", "доля_токенов_проц", "%"))
PROCESS_BARS_SHOWN = 8
PROCESS_CORE_SLACK = 1.1
PROCESS_PARALLEL = "helpers"
PROCESS_ACCURACY = {"сервер": "доля делит итог cgroup по дереву процессов",
                    "запуски": "время запусков оценено по меткам и транскриптам",
                    "токены": "владелец вызова определён по журналам и транскриптам"}


def _e(value) -> str:
    return html.escape(str(value))


def _p_number(value):
    if value is None:
        return None
    if isinstance(value, int) or value >= 1000:
        return f"{round(value):,}".replace(",", " ")
    text = f"{value:.3g}"
    return (f"{value:.0f}" if "e" in text else text).replace(".", ",")


def _p_gb(value) -> str:
    return f"{value / 2**30:.1f}".replace(".", ",").removesuffix(",0") + " ГБ"


def _p_missing() -> str:
    return '<span class="тихо">не измерено</span>'


def _p_cell(text, accuracy: str = "точно", kind: str = "сервер", unit: str = "") -> str:
    """Число ячейки: None — «не измерено», оценка — «≈» с пояснением в title."""
    if text is None:
        return _p_missing()
    text = f"{text}{unit}"
    if accuracy == "≈":
        return f'<span title="оценка: {_e(PROCESS_ACCURACY[kind])}">≈ {_e(text)}</span>'
    return _e(text)


def _p_href(params: dict, fragment: str, **change) -> str:
    merged = {**params, **change}
    shown = {k: v for k, v in merged.items() if v}
    return "/processy" + ("?" + _e(urllib.parse.urlencode(shown)) if shown else "") + "#" + fragment


def _p_name(ident: str, names: dict) -> str:
    if ident in PROCESS_LABELS:                       # короткие подписи владельцу важнее длинных названий библиотеки
        return PROCESS_LABELS[ident]
    if ident in names:
        return names[ident]
    unit, sep, tail = ident.partition(": ")
    if sep and tail == "неразмечено":
        return f"Не разобрано ({unit})"
    if ident.startswith(PROCESS_CRON_PREFIX):
        return f"Задание крона без метки ({ident.removeprefix(PROCESS_CRON_PREFIX)})"
    return ident


def _p_when(moment: float, now: float) -> str:
    stamp, today = dt.datetime.fromtimestamp(moment), dt.datetime.fromtimestamp(now)
    days = (stamp.date() - today.date()).days
    day = {0: "сегодня", 1: "завтра", -1: "вчера"}.get(days, stamp.strftime("%d.%m"))
    return f"{day} {stamp:%H:%M}"


def _p_span(seconds: float) -> str:
    minutes = round(abs(seconds) / 60)
    return f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч {minutes % 60} мин" if minutes < 1440 else f"{minutes // 1440} сут"


def _p_duration(seconds) -> str:
    if seconds is None:
        return "не измерено"
    seconds = float(seconds)
    return f"{seconds:.0f} с" if seconds < 90 else _p_span(seconds)


# ── библиотека ──

def _p_state_pill(card: dict) -> str:
    state = card["состояние"]
    if state == "идёт":
        return '<span class="пилюля зелёная">идёт</span>'
    if state == "погашен":
        return '<span class="пилюля жёлтая">погашен выключателем</span>'
    if state == "снят":
        return '<span class="пилюля красная">снят</span>'
    if state == "иное":
        return f'<span class="пилюля жёлтая">{_e(card["статус"])}</span>'
    return '<span class="пилюля серая">вне выключателя</span>'


def _p_measure(measure) -> str:
    week = measure.get("неделя") if isinstance(measure, dict) else None
    if not week:
        return "не измерено"
    parts = []
    server, runs, tokens = week.get("сервер"), week.get("запуски"), week.get("токены")
    if server:
        parts.append(f"ядро-часов {_p_number(server['ядро_часов'])}, память пик {_p_number(server['память_пик_мб'])} МБ")
    if runs:
        parts.append(f"запусков {_p_number(runs['запусков'])}")
    if tokens:
        parts.append(f"токены {_p_number(tokens['всего'])} ({_p_number(tokens['доля_проц'])} %)")
    covered = week.get("покрытие_ч") or 0
    note = f" · неделя неполная: данных {_p_number(covered)} ч из 168" if covered < 168 else ""
    return ("; ".join(parts) or "не измерено") + note


def _p_details(card: dict) -> str:
    rows = [("Класс", PROCESS_CLASS_WORDS.get(card["класс"], card["класс"])),
            ("Как запускается", f"{card['запуск']}, {card['расписание']}")]
    if card["исключение"]:
        rows.append(("Основание исключения", card["исключение"]))
    if len(card["что_делает"]) > 150:
        rows.append(("Полностью", card["что_делает"]))
    out = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in rows)
    if card["шаги"]:
        out += "<dt>Что делает по шагам</dt><dd><ul>" + "".join(f"<li>{_e(s)}</li>" for s in card["шаги"]) + "</ul></dd>"
    out += "".join(f"<dt>{_e(k)}</dt><dd>{_e(card[f])}</dd>" for k, f in (("Когда", "когда"), ("Без него", "без_него")) if card[f])
    if card["файлы"]:
        out += f'<dt>Файлы</dt><dd class="файлы">{_e(", ".join(card["файлы"]))}</dd>'
    out += f"<dt>Ресурсы</dt><dd>{_e(_p_measure(card['замер']))}</dd>"
    out += f'<dt>id</dt><dd class="файлы">{_e(card["id"])}</dd>'
    return f'<details><summary>подробнее</summary><dl class="подробно">{out}</dl></details>'


def _p_card(card: dict) -> str:
    short = card["что_делает"] if len(card["что_делает"]) <= 150 else card["что_делает"][:147].rstrip() + "…"
    tags = _p_state_pill(card) + ('<span class="пилюля">исключение</span>' if card["исключение"] else "")
    own = ('<div class="мелко">своя служба, ключом не гасится</div>'
           if card["запуск"] == "служба-проверок" else "")
    return (f'<article class="карточка" data-id="{_e(card["id"])}"><h3>{_e(card["название"])}</h3>'
            f'<div class="метки">{tags}</div><div class="расписание">{_e(card["расписание"])}</div>'
            f'<p class="что">{_e(short)}</p>{own}{_p_details(card)}</article>')


def _p_match(card: dict, query: str) -> bool:
    return query in " ".join((card["название"], card["id"], card["что_делает"])).lower()


def _p_filters(cards: list, params: dict) -> str:
    def pill(label: str, count: int, key: str, value: str) -> str:
        chosen = params.get(key) == value
        link = _p_href(params, "lib", **{key: "" if chosen else value})
        return f'<a class="пилюля{" вкл" if chosen else ""}" href="{link}">{_e(label)} {count}</a>'
    counts = {"фоновый": 0, "по внутреннему действию": 0, "сторонний": 0}
    for card in cards:
        counts[card["класс"]] = counts.get(card["класс"], 0) + 1
    states = {"idut": sum(c["состояние"] == "идёт" for c in cards),
              "pogasheny": sum(c["состояние"] == "погашен" for c in cards),
              "isklyucheniya": sum(bool(c["исключение"]) for c in cards)}
    labels = {"фоновый": "фоновые", "по внутреннему действию": "по действию", "сторонний": "сторонние"}
    all_pill = (f'<a class="пилюля{"" if params.get("klass") else " вкл"}" '
                f'href="{_p_href(params, "lib", klass="")}">все {len(cards)}</a>')
    klass = "".join(pill(labels.get(k, k), n, "klass", k) for k, n in counts.items())
    state = "".join(pill(PROCESS_FILTER_STATES[k], n, "vkl", k) for k, n in states.items())
    hidden = "".join(f'<input type="hidden" name="{_e(k)}" value="{_e(v)}">'
                     for k, v in params.items() if v and k != "q")
    search = (f'<form method="get" action="/processy#lib">{hidden}'
              f'<input type="text" name="q" value="{_e(params.get("q", ""))}" placeholder="поиск">'
              f'<button>Найти</button></form>')
    return f'<div class="фильтры">{all_pill}{klass}</div><div class="фильтры">{state}{search}</div>'


def _p_group(cls: str, cards: list, filtered: bool) -> str:
    """Класс одной раскрывающейся группой: итог «идёт · погашено · исключений» виден и в закрытом виде."""
    running = sum(c["состояние"] == "идёт" for c in cards)
    stopped = sum(c["состояние"] == "погашен" for c in cards)
    exempt = sum(bool(c["исключение"]) for c in cards)
    title = PROCESS_CLASS_TITLES.get(cls, cls)
    parts = [f"{word} {count}" for word, count in (("идёт", running), ("погашено", stopped), ("исключений", exempt)) if count]
    summary = (f'{_e(title)} · {len(cards)}<span class="итог-класса">'
               f'{" · ".join(parts) or "ключ выключателя их не гасит"}</span>')
    meaning = f'<div class="мелко">{_e(PROCESS_CLASS_WORDS.get(cls, ""))}</div>' if cls in PROCESS_CLASS_WORDS else ""
    return (f'<details class="класс"{" open" if filtered else ""}><summary>{summary}</summary>{meaning}'
            f'<div class="карточки">{"".join(_p_card(c) for c in cards)}</div></details>')


def _p_library(library: dict, params: dict) -> str:
    if library.get("нет данных"):
        return f'<div class="карта">{_e(library["нет данных"])}</div>'
    cards = library["записи"]
    key = ("выключены ключом, работают только исключения" if library["ключ"] == "off"
           else "включены")
    note = (f'<div class="мелко">Фоновые процессы: {key} (исключений {library["исключений"]}). '
            f'Всё, что запускает работа сессии, ключ не трогает.</div>')
    shown = [c for c in cards
             if (not params.get("klass") or c["класс"] == params["klass"])
             and (params.get("vkl") != "idut" or c["состояние"] == "идёт")
             and (params.get("vkl") != "pogasheny" or c["состояние"] == "погашен")
             and (params.get("vkl") != "isklyucheniya" or c["исключение"])
             and (not params.get("q") or _p_match(c, params["q"].strip().lower()))]
    filtered = any(params.get(k) for k in ("klass", "vkl", "q"))
    by_class = {}
    for card in shown:
        by_class.setdefault(card["класс"], []).append(card)
    body = ("".join(_p_group(cls, group, filtered) for cls, group in by_class.items()) if shown
            else '<div class="карта пусто">Под фильтр ничего не попало</div>')
    return note + _p_filters(cards, params) + body


# ── расходы ──

def _p_sum(rows: list, field: str):
    values = [r[field] for r in rows if r.get(field) is not None]
    return sum(values) if values else None


def _p_age(view: dict, now: float):
    try:
        taken = dt.datetime.fromisoformat(view["снято"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None, None
    return taken, (now - taken) / 60


def _p_stamp(view: dict, now: float) -> str:
    taken, age = _p_age(view, now)
    if taken is None:
        return '<span class="тихо">время снятия неизвестно</span>'
    stale = ' <span class="пилюля жёлтая">витрина устарела</span>' if age > process_state.PROCESS_STALE_MIN else ""
    return f'снято {dt.datetime.fromtimestamp(taken):%H:%M} ({round(age)} мин назад){stale}'


def _p_windows(params: dict) -> str:
    pills = "".join(f'<a class="пилюля{" вкл" if params["okno"] == key else ""}" '
                    f'href="{_p_href(params, "cost", okno=key)}">{_e(label)}</a>'
                    for key, (_, label) in PROCESS_WINDOWS.items())
    return f'<div class="фильтры">{pills}</div>'


def _p_broken(row: dict, covered, cores) -> bool:
    """Невозможное число — сбой замера: доля ёмкости больше 100 % или ядро-часов больше, чем ядер × часов покрытия."""
    share, core_hours = row.get("доля_ёмкости_проц"), row.get("ядро_ч")
    over_capacity = share is not None and share > 100
    over_cores = bool(cores) and covered is not None and core_hours is not None \
        and core_hours > cores * covered * PROCESS_CORE_SLACK
    return over_capacity or over_cores


def _p_failure() -> str:
    return '<span class="пилюля красная">сбой замера</span>'


def _p_more(count: int, total: float, unit: str) -> str:
    text = f"{_p_number(total)} {unit}" + (" пиков" if unit == "МБ" else "")
    return (f'<div class="доля"><div class="доля-строка"><span>остальные {count}</span>'
            f'<span>— ≈ {_e(text)}</span></div></div>')


def _p_bars(group: str, title: str, rows: list, field: str, unit: str, names: dict, failed: list) -> str:
    """Верхние 8 процессов полосами; хвост — одной строкой «остальные N — ≈X»."""
    rated = sorted((r for r in rows if r.get(field)), key=lambda r: -r[field])
    peak = rated[0][field] if rated else 0
    lines = []
    for row in rated[:PROCESS_BARS_SHOWN]:
        name = _e(_p_name(row["id"], names)) + (" · параллельно" if row["id"] == PROCESS_PARALLEL else "")
        width = 100 * row[field] / peak if unit == "МБ" else min(100, row[field])
        lines.append(f'<div class="доля" style="--ширина:{width:.0f}%"><div class="доля-строка"><span>{name}</span>'
                     f'<span>{_e(_p_number(row[field]))} {unit}</span></div></div>')
    if len(rated) > PROCESS_BARS_SHOWN:
        tail = rated[PROCESS_BARS_SHOWN:]
        lines.append(_p_more(len(tail), sum(r[field] for r in tail), unit))
    if failed:
        lines.append(f'<div class="доля">{_p_failure()} <span class="мелко">'
                     f'{_e(", ".join(_p_name(r["id"], names) for r in failed))}</span></div>')
    if not lines:
        lines.append('<div class="мелко">не измерено</div>')
    return f'<div class="карта" data-group="{group}"><h2>{_e(title)}</h2>{"".join(lines)}</div>'


def _p_groups(rows: list, names: dict, failed: list) -> str:
    return '<div class="группы">' + "".join(
        _p_bars(group, title, rows, field, unit, names, failed if group == "cpu" else [])
        for group, title, field, unit in PROCESS_BARS) + '</div>'


def _p_dash(reason: str) -> str:
    return f'<span class="тихо" title="нет данных: {_e(reason)}">—</span>'


def _p_sum_rows(rows: list) -> dict:
    """Сумма числовых полей строк: свёрнутые службы показываются одной строкой."""
    total = {}
    for field in PROCESS_SUMMED:
        values = [r[field] for r in rows if r.get(field) is not None]
        total[field] = sum(values) if values else None
    return total


def _p_window_rows(view: dict, key: str) -> dict:
    return {r["id"]: r for r in (view["окна"].get(key) or {}).get("строки") or []}


def _p_entries(view: dict, params: dict, states: dict) -> list:
    """Строки таблицы: выборки двух окон, записи библиотеки постоянных, плановых и загрузочных процессов
    (без выборок они остаются строкой без чисел) и упавшие юниты вне библиотеки."""
    label, _ = PROCESS_WINDOWS[params["okno"]]
    now_rows, long_rows = _p_window_rows(view, PROCESS_NOW_WINDOW), _p_window_rows(view, label)
    listed = {i for i, entry in states.items() if entry["kind"] in PROCESS_TABLE_KINDS or entry["kind"] is None}
    return [{"id": i, "now": now_rows.get(i), "long": long_rows.get(i), "state": states.get(i)}
            for i in sorted({*now_rows, *long_rows, *listed})]


def _p_is_system(entry: dict, names: dict) -> bool:
    ident = entry["id"]
    state = entry["state"] or {}
    return (ident.endswith((".service", ".scope")) and ident not in names and state.get("value") != "failed"
            and not ident.startswith(("harness-", "run-u")))


def _p_rollup(entries: list, names: dict) -> list:
    """Системные службы вне библиотеки — одна строка; упавшие и прочие остаются своими."""
    system = [e for e in entries if _p_is_system(e, names)]
    if not system:
        return entries
    total = {key: _p_sum_rows([e[key] or {} for e in system]) for key in ("now", "long")}
    rolled = {"id": PROCESS_ROLLUP, "now": total["now"], "long": total["long"], "state": None,
              "members": sorted(e["id"] for e in system)}
    return [e for e in entries if e not in system] + [rolled]


def _p_rank(entry: dict) -> int:
    """0 — упал; 1 — пропущен, не запущен или неизвестен у постоянных, плановых и загрузочных; 2 — прочее."""
    state = entry["state"]
    if state and state["value"] == "failed":
        return 0
    if state and state["kind"] in PROCESS_TABLE_KINDS and state["value"] in PROCESS_PROTECTED_STATES:
        return 1
    return 2


def _p_protected(entry: dict) -> bool:
    return _p_rank(entry) < 2


def _p_order(entries: list, sort: str) -> list:
    field = PROCESS_SORTS.get(sort, "ядро_ч")

    def amount(entry):
        value = (entry["long"] or {}).get(field)
        return (value is None, -(value or 0))
    if sort:
        return sorted(entries, key=lambda e: (*amount(e), e["id"]))
    return sorted(entries, key=lambda e: (_p_rank(e), *amount(e), e["id"]))


def _p_state_cell(state, reason: str, now: float) -> str:
    if state is None:
        return _p_dash(reason)
    value, why = state["value"], state["why"][:PROCESS_WHY_LIMIT]
    word = f"идёт: {state['running_n']}" if state["kind"] == "on_demand" and state["running_n"] else \
        process_state.LABELS[value]
    pill = f'<span class="пилюля {PROCESS_STATE_COLORS.get(value, "серая")}">{_e(word)}</span>'
    notes = [_e(why)] if why else []
    if state["source"] in ("docker", "витрина диспетчера") and state["checked"]:
        notes.append(f"проверено {round((now - state['checked']) / 60)} мин назад")
    # На телефоне строка причины режется многоточием: полный текст остаётся в title.
    return pill + "".join(f'<div class="мелко" title="{note}">{note}</div>' for note in notes)


def _p_last_run(last: dict, now: float) -> str:
    at = process_state.clock(dt.datetime.fromisoformat(last["at"]).timestamp(), now)
    if last["outcome"] == "running":
        return f"идёт с {at}"
    mark = "✓" if last["rc"] == 0 else "✗"
    took = f" {_p_duration(last['seconds'])}" if last.get("seconds") is not None else ""
    return f"посл. {at} {mark}{took}"


def _p_runs_cell(entry: dict, window_label: str, schedule_words: str, now: float) -> str:
    state, row = entry["state"], entry["long"] or {}
    kind = state["kind"] if state else None
    count = row.get("запусков")
    per_window = f"{_p_number(count)} за {window_label}" if count is not None else None
    if kind == "constant":
        since = state["since"]
        stopped, running = state["value"] == "stopped", state["value"] == "running"
        text = ("работает" if running else "не запущен") + (f" с {dt.datetime.fromtimestamp(since):%d.%m %H:%M}" if since and (running or stopped) else "")
        return _e(text) if running or stopped else _p_dash("состояние неизвестно")
    if kind == "boot":
        since = state["since"]
        return _e(f"при загрузке {process_state.clock(since, now)}") if since else _p_dash("время загрузочного запуска неизвестно")
    parts = [per_window] if per_window else []
    if kind == "scheduled":
        skipped = row.get("пропусков")
        if state["last_run"]:
            parts.append(_p_last_run(state["last_run"], now))
        text = _e(" · ".join(parts)) + (f" · пропусков {_p_number(skipped)}" if skipped else "")
        if state["next_run"]:
            text += f'<div class="мелко">след. {_e(_p_when(state["next_run"], now))} ({_e(schedule_words)})</div>'
        elif state["next_note"]:
            text += f'<div class="мелко">{_e(state["next_note"])}</div>'
        return text if parts or state["next_run"] or state["next_note"] else _p_dash("запусков за окно не было")
    return _e(per_window) if per_window else _p_dash("запусков за окно нет")


def _p_runs_title(row: dict) -> str:
    parts = []
    if row.get("в_работе_ч") is not None:
        parts.append(f"в работе {_p_duration(row['в_работе_ч'] * 3600)}")
    if row.get("доля_запусков_проц") is not None:
        parts.append(f"доля запусков {_p_number(row['доля_запусков_проц'])} %")
    return f' title="{_e(" · ".join(parts))}"' if parts else ""


def _p_cpu_cell(row, broken: bool, reason: str) -> str:
    if row is None or row.get("доля_ёмкости_проц") is None:
        return _p_dash(reason)
    if broken:
        return _p_failure()
    return _p_cell(_p_number(row["доля_ёмкости_проц"]), (row.get("точность") or {}).get("сервер", "точно"), "сервер", " %")


def _p_tokens_cell(row) -> str:
    tokens = (row or {}).get("токены")
    if tokens is None:
        return _p_dash("токенов за окно нет")
    amount = f"{_p_number(tokens / 1e6)} млн" if tokens >= 1e6 else _p_number(tokens)
    share = row.get("доля_токенов_проц")
    if share is None:
        return _e(amount)
    return f"{_e(amount)} · " + _p_cell(_p_number(share), (row.get("точность") or {}).get("токены", "точно"), "токены", " %")


def _p_name_cell(entry: dict, names: dict) -> str:
    if "members" in entry:
        listing = ", ".join(entry["members"])
        return (f'{_e(PROCESS_ROLLUP)} ({len(entry["members"])})<details><summary>какие службы</summary>'
                f'<div class="файлы">{_e(listing)}</div></details>')
    parallel = (' <span class="мелко" title="помощники работают одновременно: время запусков суммируется, '
                'доля по времени выходит больше доли по ядрам">параллельно</span>' if entry["id"] == PROCESS_PARALLEL else "")
    return _e(_p_name(entry["id"], names)) + parallel


def _p_row(entry: dict, ctx: dict) -> str:
    """Одна строка таблицы расходов; ctx — общее для всех строк (подписи, окна, время)."""
    row, short = entry["long"] or {}, entry["now"]
    window, accuracy = ctx["window_name"], row.get("точность") or {}
    broken = {key: entry[key] is not None and _p_broken(entry[key], ctx["covered"][key], ctx["cores"])
              for key in ("now", "long")}
    peak = row.get("память_пик_мб")
    memory = _p_dash("памяти за окно нет") if peak is None else _p_cell(
        f"{_p_number(peak)} МБ", accuracy.get("сервер", "точно"), "сервер")
    reason_state = ("сводная строка: у каждой службы своё состояние" if "members" in entry
                    else ctx["states_reason"] or "вне библиотеки: состояние не определяется")
    schedule_words = ctx["schedules"].get(entry["id"], "")
    cells = [("Название", _p_name_cell(entry, ctx["names"]), ""),
             ("Состояние", _p_state_cell(entry["state"], reason_state, ctx["now"]), ""),
             ("Запуски", _p_runs_cell(entry, window, schedule_words, ctx["now"]), _p_runs_title(row)),
             ("ЦП 10 мин", _p_cpu_cell(short, broken["now"], "замера за 10 минут нет"), ""),
             (f"ЦП {ctx['window_name']}", _p_cpu_cell(entry["long"], broken["long"], "замера за окно нет"), ""),
             ("Память пик", memory, ""), (f"Токены {ctx['window_name']}", _p_tokens_cell(entry["long"]), "")]
    return "<tr>" + "".join(f'<td data-label="{_e(name)}"{title}>{cell}</td>' for name, cell, title in cells) + "</tr>"


def _p_table(entries: list, ctx: dict) -> str:
    head = "<thead><tr>" + "".join(f"<th>{_e(name)}</th>" for name in ctx["columns"]) + "</tr></thead>"
    body = "".join(_p_row(e, ctx) for e in entries)
    return f'<table class="таблица расходы">{head}<tbody>{body}</tbody></table>'


def _p_sort_links(params: dict) -> str:
    sorts = "".join(f'<a class="пилюля{" вкл" if params["sort"] == key else ""}" href="{_p_href(params, "cost", sort=key)}">'
                    f'{label}</a>' for key, label in (("cpu", "по ядрам"), ("mem", "по памяти"),
                                                     ("runs", "по запускам"), ("tok", "по токенам")))
    return f'<div class="фильтры">{sorts}</div>'


def _p_tables(entries: list, params: dict, ctx: dict) -> str:
    """Первые 20 строк и все не-зелёные видны, остальное — под «ещё N процессов»."""
    ordered = _p_order(entries, params["sort"])
    rest = [e for e in ordered[PROCESS_SHOWN:] if not _p_protected(e)]
    shown = [e for e in ordered if not any(e is r for r in rest)]
    out = _p_sort_links(params) + _p_table(shown, ctx)
    if rest:
        share = _p_sum([e["long"] or {} for e in rest], "доля_ёмкости_проц")
        tail = f" — ≈ {_p_number(share)} % ядра за {ctx['window_name']}" if share is not None else ""
        out += (f'<details><summary>ещё {len(rest)} процессов{_e(tail)}</summary>{_p_table(rest, ctx)}</details>')
    return out


def _p_unmarked(view: dict, window: dict, rows: list) -> dict:
    """Не разобранные доли юнитов за ЭТО окно: подвал не меньше строки «<юнит>: неразмечено» того же окна,
    иначе одно слово «не разобрано» называло бы две величины. Окно новой витрины даёт долю само,
    у старой остаток юнита лежит в корне витрины."""
    own = window.get("неразмечено")
    result = dict(own if isinstance(own, dict) else view.get("неразмечено") or {})
    for row in rows:
        unit, sep, tail = row["id"].partition(": ")
        if sep and tail == "неразмечено" and row.get("доля_ёмкости_проц") is not None:
            result[unit] = max(result.get(unit) or 0, row["доля_ёмкости_проц"])
    return result


def _p_footer(view: dict, window: dict, rows: list) -> str:
    check = view.get("сверка") or {}
    gap = check.get("расхождение_токенов")
    gap_text = (_p_missing() if gap is None else f'<span class="пилюля {"зелёная" if gap == 0 else "красная"}">'
                f'расхождение с tokens-collector: {_e(_p_number(abs(gap)) or 0)} токенов</span>')
    lost = check.get("без_метки_проц")
    unmarked = ", ".join(f'{_e(unit.replace("_", " "))} {_e(_p_number(share))} %'
                         for unit, share in _p_unmarked(view, window, rows).items() if share is not None)
    return (f'<div class="мелко" style="margin-top:12px">{gap_text} · без метки {_p_number(lost) if lost is not None else "не измерено"} % токенов'
            f' · не разобрано: {unmarked or "—"}</div>')


def _p_coverage(covered, label: str) -> str:
    if covered is None:
        return "данных нет"
    return f"данных {_p_number(covered)} ч из {PROCESS_WINDOW_HOURS[label]}"


def _p_tile(title: str, value: str, note: str) -> str:
    return (f'<div class="итог"><div class="мелко">{_e(title)}</div><div class="число">{value}</div>'
            f'<div class="мелко">{note}</div></div>')


def _p_tiles(window: dict, rows: list, good: list, failed: list, label: str) -> str:
    """Ядра и память — замер сервера (его покрытие подписано у них); запуски и токены — по журналам окна."""
    covering = _e(_p_coverage(window.get("покрытие_ч"), label))
    share = _p_sum(good, "доля_ёмкости_проц")
    cut = f" · без {len(failed)} строк со сбоем замера" if failed else ""
    cores = (_p_missing() if share is None else _p_failure() if share > 100 else _e(_p_number(share)) + " %")
    tiles = [_p_tile("Ядра (доля ёмкости)", cores, covering + _e(cut))]
    memory = window.get("память_пик_мб")
    if memory is not None:
        tiles.append(_p_tile("Память (пик)", _e(_p_number(memory)) + " МБ", covering))
    runs, skipped = _p_sum(rows, "запусков"), _p_sum(rows, "пропусков")
    skipped_text = "пропуски не измерены" if skipped is None else \
        f"пропущено ключом {_p_number(skipped)}"
    tiles.append(_p_tile("Запуски", _p_missing() if runs is None else _e(_p_number(runs)),
                         skipped_text))
    tokens = _p_sum(rows, "токены")
    tiles.append(_p_tile("Токены", _p_missing() if tokens is None else _e(_p_number(tokens)), "по журналам"))
    return f'<div class="итоги">{"".join(tiles)}</div>'


def _p_cost(view: dict, names: dict, states: dict, schedules: dict, params: dict, now: float, cores) -> str:
    if view.get("нет данных"):
        return f'<div class="карта пусто">{_e(view["нет данных"])}</div>'
    label, window_name = PROCESS_WINDOWS[params["okno"]]
    window = view["окна"].get(label) or {}
    rows = window.get("строки") or []
    note = f'<div class="мелко">{_p_stamp(view, now)}</div>'
    states_reason = states.get("нет данных")
    entries = _p_rollup(_p_entries(view, params, {} if states_reason else states), names)
    if not entries:
        return _p_windows(params) + note + '<div class="карта пусто">За это окно данных пока нет</div>'
    covered = window.get("покрытие_ч")
    broken = {id(r) for r in rows if _p_broken(r, covered, cores)}
    good = [r for r in rows if id(r) not in broken]
    failed = [r for r in rows if id(r) in broken]
    dropped = window.get("отброшено_строк", view.get("отброшено_строк"))
    dropped_pill = (f' <span class="пилюля жёлтая">отброшено {_e(_p_number(dropped))} выборок со сбоем замера</span>'
                    if isinstance(dropped, (int, float)) and dropped > 0 else "")
    columns = ("Название", "Состояние", "Запуски", "ЦП 10 мин", f"ЦП {window_name}", "Память пик", f"Токены {window_name}")
    ctx = {"names": names, "schedules": schedules, "now": now, "cores": cores, "columns": columns,
           "window_name": window_name, "states_reason": states_reason,
           "covered": {"now": (view["окна"].get(PROCESS_NOW_WINDOW) or {}).get("покрытие_ч"), "long": covered}}
    return (_p_windows(params) + f'<div class="мелко">{_p_stamp(view, now)}{dropped_pill}</div>'
            + _p_tiles(window, rows, good, failed, label) + _p_groups(good, names, failed)
            + _p_tables(entries, params, ctx) + _p_footer(view, window, rows))


# ── служба проверок ──

def _p_slice(cut: dict) -> str:
    cores, total = cut["ядер_потолок"], cut["ядер_всего"]
    ceiling = (f'Потолок ядер {_p_number(cores)} из {total} ({_p_number(cores * 100)} %)' if cores
               else '<span class="пилюля красная">без потолка</span> по ядрам')
    soft, hard, now_, peak = cut["память_мягкий"], cut["память_жёсткий"], cut["память_сейчас"], cut["память_пик"]
    memory = "Память: " + (f"мягкий {_p_gb(soft)}" if soft else "мягкого потолка нет") + " / " + (
        f"жёсткий {_p_gb(hard)}" if hard else '<span class="пилюля красная">без потолка</span>')
    bar = ""
    if now_ is not None and hard:
        share = 100 * now_ / hard
        bar = (f'<div class="полоса"><i style="width:{min(100, share):.0f}%;background:{_цвет_доли(share)}"></i></div>'
               f'<div class="мелко">занято сейчас {_p_gb(now_)} из {_p_gb(hard)}</div>')
    elif now_ is not None:
        bar = f'<div class="мелко">занято сейчас {_p_gb(now_)}</div>'
    throttled = "не измерено" if cut["торможений"] is None else _p_number(cut["торможений"])
    extra = f'пик памяти {_p_gb(peak)}' if peak is not None else "пик памяти не измерен"
    return (f'<div class="карта"><div>{ceiling}</div><div style="margin-top:6px">{memory}</div>{bar}'
            f'<div class="мелко" style="margin-top:8px">торможений по ядрам: {throttled} · {extra}</div></div>')


def _p_exit_pill(code, record: dict, limit: int) -> str:
    """Подпись кода выхода — та же, что в таблице расходов: подписывает process_state."""
    if code is None:
        return '<span class="пилюля серая">без кода</span>'
    if code == 0:
        return '<span class="пилюля зелёная">успешно</span>'
    value, why = process_state.exit_state(record, code, limit)
    if value == "skipped":
        return f'<span class="пилюля жёлтая">пропущен: {_e(why)}</span>'
    return f'<span class="пилюля красная">{_e(why)}</span>'


def _p_runs(runs: list, now: float, record: dict, limit: int) -> str:
    if not runs:
        return '<span class="тихо">прогонов ещё не было</span>'

    def line(run):
        pill = _p_exit_pill(run["rc"], record, limit)
        cpu = "" if run["cpu_с"] is None else f', CPU {_p_number(run["cpu_с"])} с'
        return f'{_e(_p_when(run["ts"], now))}, {_e(_p_duration(run["с"]))}{_e(cpu)} {pill}'
    more = (f"<details><summary>последние прогоны ({len(runs)})</summary>"
            + "".join(f'<div class="мелко">{line(r)}</div>' for r in runs) + "</details>") if len(runs) > 1 else ""
    return line(runs[0]) + more


def _p_timer_row(timer: dict, now: float, record: dict, limit: int) -> str:
    if timer["идёт_с"]:
        upcoming = f'<span class="пилюля зелёная">идёт с {_e(timer["идёт_с"])}</span>'
    elif not timer["включён"]:
        upcoming = '<span class="пилюля серая">выключен</span>'
    elif timer["следующий"]:
        upcoming = f'{_e(_p_when(timer["следующий"], now))}, через {_e(_p_span(timer["следующий"] - now))}'
    else:
        upcoming = _p_missing()
    cells = [("процесс", f'{_e(timer["название"])}<details><summary>подробнее</summary>'
                         f'<div class="файлы">{_e(timer["юнит"])}</div></details>'),
             ("расписание", _e(timer["расписание"])), ("следующий", upcoming),
             ("последний", _p_runs(timer["прогоны"], now, record, limit))]
    return "<tr>" + "".join(f'<td data-label="{label}">{cell}</td>' for label, cell in cells) + "</tr>"


def _p_checks(checks: dict, now: float, cards: dict) -> str:
    note = '<div class="мелко">Отдельная служба со своим потолком ядер и памяти: ключ выключателя её не гасит.</div>'
    if checks.get("нет данных"):
        return note + f'<div class="карта пусто">{_e(checks["нет данных"])}</div>'
    # Без записи библиотеки (не прочиталась) прогон подписывается как у не-демона: блок от неё не падает.
    limit = process_state.limit_code()
    table = ('<table class="таблица"><thead><tr><th>процесс</th><th>расписание</th><th>следующий запуск</th>'
             '<th>последний прогон</th></tr></thead><tbody>'
             + "".join(_p_timer_row(t, now, cards.get(t["id"], {}), limit) for t in checks["таймеры"]) + "</tbody></table>")
    return note + _p_slice(checks["срез"]) + '<div style="margin-top:14px">' + table + "</div>"


def _p_section(anchor: str, title: str, subject: str, build) -> str:
    """Сбой отрисовки одного блока не роняет страницу: на его месте причина, остальные блоки целы."""
    try:
        inner = build()
    except Exception as error:                      # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        inner = (f'<div class="карта пусто">Блок не показан: {_e(subject)} не удалось разобрать '
                 f'({_e(type(error).__name__)}).</div>')
    return f'<section class="раздел" id="{anchor}"><h2>{_e(title)}</h2>{inner}</section>'


def processes_page(data: dict, window: str = "24ch", klass: str = "", vkl: str = "", sort: str = "",
                   query: str = "") -> str:
    """Раздел «Процессы». Параметры уже разобраны сервером; неизвестное окно и сортировка — значения по умолчанию."""
    params = {"okno": window if window in PROCESS_WINDOWS else "24ch", "klass": klass, "vkl": vkl,
              "sort": sort if sort in PROCESS_SORTS else "", "q": query}
    names = data.get("названия") or {}
    cards = {c["id"]: c for c in data["библиотека"].get("записи") or []}
    schedules = {ident: card["расписание"] for ident, card in cards.items()}
    now = data.get("снято") or dt.datetime.now().timestamp()
    head = ('<div class="шапка"><div style="display:flex;align-items:center;gap:12px">'
            '<div class="значок">✳</div><h1>Процессы харнеса</h1></div>'
            '<a href="/"><button>На панель</button></a></div>'
            '<div class="якоря"><a class="пилюля" href="#lib">Библиотека</a>'
            '<a class="пилюля" href="#cost">Расходы</a><a class="пилюля" href="#checks">Проверки</a></div>')
    body = (head
            + _p_section("lib", "Библиотека процессов", "данные библиотеки", lambda: _p_library(data["библиотека"], params))
            + _p_section("cost", "Расходы: диспетчер", "строку в витрине диспетчера",
                         lambda: _p_cost(data["диспетчер"], names, data["состояния"], schedules, params, now, data.get("ядер")))
            + _p_section("checks", "Служба проверок", "данные службы проверок", lambda: _p_checks(data["проверки"], now, cards)))
    return _страница("Процессы харнеса", f"<style>{PROCESS_STYLE}</style>{body}")
