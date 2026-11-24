#!/usr/bin/env python3
"""Проба «уровни пульта — из данных»: панель переключает шаг ЛЮБОГО уровня.

Приказ владельца 17.11.2026 (память/owner-orders-17-11-verbatim.md): выключатель
в панели для любого шага пайплайна. Уровни перечислены в pipeline.yaml (ключ
«шаги»), а писатель scripts/write-step.py и окно harness/panel/state.py держали
свой список («мелкая», «обычная», «крупная»): уровень «хакатон» есть в пульте,
но кнопкой не переключается и в панели не виден.

Стенд — своя копия пульта (дверь HARNESS_PAJPLAJN, её уже читают оба), свой
LOG_DIR для копий и журнала писателя, PANEL_NO_SUDO (файл свой, sudo не нужен).
Живой pipeline.yaml сверяется хешем до и после.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WRITER = ROOT / "scripts" / "write-step.py"
LIVE = ROOT / "harness" / "config" / "pipeline.yaml"
_YARD = Path(tempfile.mkdtemp(prefix="proba-urovni-pulta-"))
PULT = _YARD / "pipeline.yaml"
LOGS = _YARD / "log"
LOGS.mkdir()
ENV = dict(os.environ, HARNESS_PAJPLAJN=str(PULT), LOG_DIR=str(LOGS), PANEL_NO_SUDO="1")
# Уровень, которого нет ни в одном списке кода: его знает только пульт стенда.
OWN_LEVEL = "особая"

paths = 0
failures = 0


def check(name, ok, details=""):
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name} — {details}")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fresh_pult():
    """Копия живого пульта плюс строка своего уровня — вставкой текста, без yaml.dump."""
    text = LIVE.read_text(encoding="utf-8")
    anchor = "\nшаги:\n"
    assert anchor in text, "в живом пульте нет раздела «шаги:»"
    PULT.write_text(text.replace(anchor, f"{anchor}  {OWN_LEVEL}: [план, тесты]\n", 1), encoding="utf-8")
    return yaml.safe_load(PULT.read_text(encoding="utf-8"))["шаги"]


def write_step(*args):
    done = subprocess.run([sys.executable, str(WRITER), *args], capture_output=True, text=True,
                          env=ENV, timeout=60)
    return done.returncode, (done.stdout + done.stderr).strip()


live_before = digest(LIVE)

# 1. «хакатон»: выключить «тесты» — успех, список уровня без шага, прочие уровни как были.
before = fresh_pult()
check("стенд: в копии пульта есть уровень «хакатон» с «тесты»", "тесты" in (before.get("хакатон") or []),
      f"шаги: {before!r}")
code, output = write_step("хакатон", "тесты", "выключить")
after = yaml.safe_load(PULT.read_text(encoding="utf-8"))["шаги"]
check("БОЛЬНОЙ СЛУЧАЙ: write-step хакатон тесты выключить → код 0", code == 0, f"код {code} · {output[-200:]!r}")
check("хакатон: «тесты» сняты, остальное по порядку",
      after.get("хакатон") == [s for s in before["хакатон"] if s != "тесты"], f"стало {after.get('хакатон')!r}")
check("прочие уровни не тронуты", {k: v for k, v in after.items() if k != "хакатон"}
      == {k: v for k, v in before.items() if k != "хакатон"}, f"стало {after!r}")
code, output = write_step("хакатон", "тесты", "включить")
again = yaml.safe_load(PULT.read_text(encoding="utf-8"))["шаги"]
check("хакатон: «тесты» включаются обратно", code == 0 and "тесты" in (again.get("хакатон") or []),
      f"код {code} · {again.get('хакатон')!r}")

# 2. Уровень, известный только пульту, — тоже переключается; незнакомый пульту — отказ 2.
fresh_pult()
code, output = write_step(OWN_LEVEL, "план", "выключить")
own = yaml.safe_load(PULT.read_text(encoding="utf-8"))["шаги"].get(OWN_LEVEL)
check(f"уровень «{OWN_LEVEL}» из пульта стенда: выключить «план» → 0, осталось [тесты]",
      code == 0 and own == ["тесты"], f"код {code} · {own!r} · {output[-160:]!r}")
untouched = digest(PULT)
code, output = write_step("нет_такого", "план", "выключить")
check("уровня нет в пульте → отказ 2, пульт цел", code == 2 and digest(PULT) == untouched,
      f"код {code} · {output[-160:]!r}")

# 3. Окно панели перечисляет уровни из пульта (в своём процессе: окружение стенда).
levels = fresh_pult()
probe = ("import json, sys; sys.path.insert(0, sys.argv[1]); import state; "
         "print(json.dumps(state.шаги(), ensure_ascii=False))")
done = subprocess.run([sys.executable, "-c", probe, str(ROOT / "harness" / "panel")],
                      capture_output=True, text=True, env=ENV, timeout=60)
try:
    shown = json.loads(done.stdout.strip().splitlines()[-1]).get("уровни") or {}
except (ValueError, IndexError) as err:
    shown = {"не разобрано": f"{err}: {(done.stdout + done.stderr)[-200:]}"}
check("state.шаги(): уровни — ровно ключи «шаги» пульта (с «хакатон» и своим)",
      set(shown) == set(levels), f"панель {sorted(shown)}, пульт {sorted(levels)}")
check("state.шаги(): списки уровней — как в пульте", shown == levels, f"панель {shown!r}")

# 4. CR-7 (ревью кода 20.11): «тесты» выкл → панель показывает «мутации» выключенными.
# Контракт данных окна: рядом с сырым «уровни» (его держат кнопки) state.шаги() отдаёт
# «действующие» — уровень → список, который требуют судьи (pipeline.шаги_уровня).
# Поломка, что оставит зелёным: «действующие» — копия сырого списка (ловит «обычная»);
# отбор по зависимостям без пульта стенда (ловят уровни, которых нет в живом пульте).
fresh_pult()
code, output = write_step("обычная", "тесты", "выключить")
raw = yaml.safe_load(PULT.read_text(encoding="utf-8"))["шаги"]
check("стенд: у «обычная» сняты «тесты», «мутации» в пульте остались",
      code == 0 and "тесты" not in raw["обычная"] and "мутации" in raw["обычная"],
      f"код {code} · {raw.get('обычная')!r} · {output[-160:]!r}")
_spec = importlib.util.spec_from_file_location("pipeline_levels_probe", ROOT / "scripts" / "pipeline.py")
price = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(price)
stand_settings = price.конфиг(PULT)
expected_live = {level: price.шаги_уровня(stand_settings, level) for level in raw}
done = subprocess.run([sys.executable, "-c", probe, str(ROOT / "harness" / "panel")],
                      capture_output=True, text=True, env=ENV, timeout=60)
try:
    shown_all = json.loads(done.stdout.strip().splitlines()[-1])
except (ValueError, IndexError) as err:
    shown_all = {"не разобрано": f"{err}: {(done.stdout + done.stderr)[-200:]}"}
acting = shown_all.get("действующие")
check("CR-7: state.шаги()[«уровни»] — сырой пульт (кнопкам нужен он): «мутации» у «обычная» видны",
      (shown_all.get("уровни") or {}) == raw, f"панель {shown_all.get('уровни')!r}")
check("CR-7: БОЛЬНОЙ СЛУЧАЙ: state.шаги()[«действующие»][«обычная»] без «мутации» — опоры «тесты» нет",
      isinstance(acting, dict) and "мутации" not in (acting.get("обычная") or ["мутации"]),
      f"действующие {acting!r}")
check("CR-7: state.шаги()[«действующие»] — ровно pipeline.шаги_уровня пульта стенда по каждому уровню",
      acting == expected_live, f"панель {acting!r}, судьи {expected_live!r}")

# 5. SR-1 (ревью спеки 20.11): окно шагов РИСУЕТСЯ на данных state.шаги() с уровнями
# «хакатон» и своим — экран панели (vid._шаги) держал подписи трёх уровней и падал
# KeyError. Каталог панели — дверь PANEL_DIR_UNDER_TEST (мутации судят копию).
# Поломка, что оставит зелёным: подпись только для «хакатон» (ловит свой уровень стенда);
# пустая подпись вместо KeyError (ловит суд заголовка карточки каждого уровня).
PANEL = Path(os.environ.get("PANEL_DIR_UNDER_TEST") or ROOT / "harness" / "panel")
levels = fresh_pult()
render = ("import json, sys; sys.path.insert(0, sys.argv[1]); import state, vid; "
          "print(json.dumps(vid._шаги(state.шаги(), 'csrf-стенда'), ensure_ascii=False))")
done = subprocess.run([sys.executable, "-c", render, str(PANEL)],
                      capture_output=True, text=True, env=ENV, timeout=60)
try:
    page = json.loads(done.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    page = None
check("SR-1: БОЛЬНОЙ СЛУЧАЙ: vid._шаги(state.шаги()) с «хакатон» и своим уровнем рисуется без исключения",
      done.returncode == 0 and isinstance(page, str), f"код {done.returncode} · {(done.stdout + done.stderr)[-240:]!r}")
cards = (page or "").split('<div class="карта"><h2>')[1:]
for level in levels:
    marker = f'name="уровень" value="{level}"'
    own_cards = [card for card in cards if marker in card]
    title = own_cards[0].split("</h2>", 1)[0].strip() if len(own_cards) == 1 else ""
    check(f"SR-1: уровень «{level}» — ровно одна карточка с непустой подписью",
          len(own_cards) == 1 and bool(title), f"карточек {len(own_cards)}, подпись {title!r}")
check("SR-1: стенд: уровней в пульте больше трёх прежних (судить есть что)",
      {"хакатон", OWN_LEVEL} <= set(levels), f"уровни {sorted(levels)}")

# 6. SR2-1 / CR2-4 (ревью 20.11, итерация 2): слова для владельца. Шаг, который есть в
# пульте уровня, но снят опорой, экран помечает «выключен опорой: <опоры>» — с именами
# опор из зависимости_шагов, чтобы владелец знал, что включить. У уровня, где опора
# стоит, пометки нет. Слова — те же, что в спеке §3.1 п.9 и в живой проверке §7 п.1.
# Поломка, что оставит зелёным: пометка без имени опоры («выключен опорой») — ловит
# суд полной строки; пометка на каждом шаге из пульта — ловит уровень «крупная», где
# «тесты» стоят и «мутации» живы.
LABEL = "выключен опорой: тесты"
fresh_pult()
code, output = write_step("обычная", "тесты", "выключить")
done = subprocess.run([sys.executable, "-c", render, str(PANEL)],
                      capture_output=True, text=True, env=ENV, timeout=60)
try:
    page = json.loads(done.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    page = ""
buttons = {}
for card in page.split('<div class="карта"><h2>')[1:]:
    for form in card.split("<form ")[1:]:
        level = form.split('name="уровень" value="', 1)[-1].split('"', 1)[0]
        step = form.split('name="шаг" value="', 1)[-1].split('"', 1)[0]
        # Текст кнопки, без атрибутов: в title — пояснение шага, его слова не судим.
        buttons[(level, step)] = form.split("<button", 1)[-1].split(">", 1)[-1].split("</button>", 1)[0]
check("SR2-1: стенд: у «обычная» сняты «тесты», экран нарисован", code == 0 and bool(buttons),
      f"код {code} · кнопок {len(buttons)} · {(done.stdout + done.stderr)[-200:]!r}")
check(f"SR2-1: БОЛЬНОЙ СЛУЧАЙ: «обычная» · «мутации» помечены «{LABEL}»",
      LABEL in buttons.get(("обычная", "мутации"), ""), f"кнопка {buttons.get(('обычная', 'мутации'))!r}")
check("SR2-1: «крупная» · «мутации» (опора «тесты» стоит) — без пометки опоры",
      ("крупная", "мутации") in buttons and "опор" not in buttons[("крупная", "мутации")],
      f"кнопка {buttons.get(('крупная', 'мутации'))!r}")

check("живой pipeline.yaml пробой не тронут", digest(LIVE) == live_before)

shutil.rmtree(_YARD, ignore_errors=True)
print(f"УРОВНИ-ПУЛЬТА: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
