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
import html
import urllib.parse

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
    if строка["вид"] == "выбор":
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
            f'<div class="кнопки">{управление}</div>{метка}{опасно}</div>')


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
