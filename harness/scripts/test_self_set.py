#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба двери слоя человека: instance.yaml пишется только по кнопке «Да».

Откуда взята. Спека §5.1–§5.3, В-3 (а): слой 2 памяти
(проект, роль, цель) — слой владельца. Модель только ПРЕДЛАГАЕТ; записывает код
по нажатию «Да» на тот же предмет. Единственная дверь — scripts/self-set.py,
вид согласия «слой-человека» (owner_word.ВИДЫ_СОГЛАСИЯ, контракт K45). Правка
instance.yaml мимо двери видна сверкой sha256 при старте смены
(session_state.sh). Пустые обязательные поля — один вопрос не чаще раза в сутки.

Что судится (свой стенд sap-ch11-*: конфиг, каталог журналов, instance.yaml,
подставной ask-consent.py; живой кнопки нет, «нажатие» пишет проба в журнал
кнопок стенда так, как его пишет диспетчер):
  - без «Да»: файл побайтно цел, кнопка попрошена видом «слой-человека» на
    предмет «<поле>=<значение>», журнал self-set.jsonl — «состояние: задан»
    с кодом запроса;
  - «Да» владельца на этот предмет: поле записано, комментарии и прочие поля
    целы, значение с «:» и «#» читается yaml как есть; в журнале «да» и sha256
    файла после записи;
  - «Нет»: не записано, кнопка больше не просится на то же значение (второй
    вызов тоже); другое значение — просится;
  - «Да» не владельца и «Да» на другое значение — не записано;
  - правка instance.yaml мимо двери — session_state.sh печатает «слой 2 изменён
    мимо согласия: <поля>»; без правки — молчит;
  - пробелы обязательные_поля_человека — один вопрос за запуск и не чаще раза
    в сутки (по журналу self-set.jsonl, «источник: вопрос»).
  - запись поля зовёт пересборку карточек помощников: self-prompt.py --карточки
    --в <ROOT>/.claude/agents; с дверью SELF_INSTANCE — не зовёт (subprocess
    подменён в процессе двери, живые карточки не трогаются).
Живые instance.yaml, журналы и канал не трогаются: снимок до и после.

Интерфейс (его закрепляет проба):
  python3 scripts/self-set.py <поле> <значение>
    0 — записано (или то же значение уже стоит);
    3 — не записано, ждём «Да» (кнопка попрошена или уже ушла);
    4 — владелец нажал «Нет» на это значение: не записано, не просится.
  Журнал $LOG_DIR/self-set.jsonl: время, поле, значение, источник
  (вопрос|контекст), состояние (задан|да|нет), код_запроса; у «да» — sha256.
  Конверт events.emit (поля в data) или плоская строка — проба читает оба.

Двери:
  SELF_SET_PY          — путь self-set.py (мутационная проба подставляет копию);
  SESSION_STATE_SH     — путь session_state.sh (то же);
  SELF_INSTANCE        — instance.yaml, который пишет дверь и сверяет старт смены;
  MEMORY_LAYERS        — файл слоёв (обязательные_поля_человека);
  HARNESS_INSTALL_CONF, HARNESS_CONF, HARNESS_LOG_DIR, LOG_DIR — свои каталоги и
  паспорт ДВЕРИ. Судье кнопок стенд приходит носителем: дверь зовёт раннер пробы,
  который подменяет owner_word.носитель и owner_word.ЗАПРОС_КНОПКИ (подставной ask-consent
  с теми же доводами и тем же кодом в выводе) в процессе двери (окружение судья не слушает).

Прогон: python3 scripts/test_self_set.py
"""
from __future__ import annotations

import atexit
import datetime
import hashlib
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

ROOT = Path(__file__).resolve().parent.parent
SELF_SET = Path(os.environ.get("SELF_SET_PY") or ROOT / "scripts" / "self-set.py")
SESSION_STATE = Path(os.environ.get("SESSION_STATE_SH")
                     or ROOT / "scripts" / "hooks" / "session_state.sh")
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import config  # noqa: E402

KIND = "слой-человека"
OWNER = "777011"
STRANGER = "555022"
WRITTEN, WAITING, REFUSED = 0, 3, 4
BYPASS_LINE = "слой 2 изменён мимо согласия:"

# Живое — снимок до стенда: проба обязана оставить его как было.
# Журналы кнопок живой машины пишет диспетчер и во время пробы, их снимок ничего
# не доказал бы; судятся файлы, которые пишет только дверь.
LIVE_INSTANCE = ROOT / "harness" / "self" / "instance.yaml"
try:
    LIVE_TRACES = [LIVE_INSTANCE, Path(config.log_dir()) / "self-set.jsonl"]
except (KeyError, OSError):
    LIVE_TRACES = [LIVE_INSTANCE]

STAND = Path(tempfile.mkdtemp(prefix="sap-ch11-"))
atexit.register(shutil.rmtree, STAND, ignore_errors=True)

INSTANCE_TEXT = """\
# Личность ЭТОЙ копии харнеса — стенд пробы sap-ch11.
# Комментарий обязан пережить запись поля: yaml.dump стёр бы его.

копия: proba-ch11            # имя установки
роль: агент проекта
цель: >
  Проба слоя человека: держать
  цель в две строки.
человек: владелец
обратная_связь:
  получаю_от: человек проекта
"""
LAYERS_TEXT = """\
неизменяемое: [harness/self/core.json]
человека: [harness/self/instance.yaml]
без_потерь: [память/ORDERS.md]
архив_прополки: память/archive/
обязательные_поля_человека: [копия, роль, цель, человек, проект.описание]
"""
FAKE_ASK = '''\
#!/usr/bin/env python3
"""Подставной ask-consent: след доводов и просьба в журнал стенда, как у настоящего."""
import datetime, json, os, sys
from pathlib import Path
trace = Path(__file__).with_name("ask-consent-trace.jsonl")
args = sys.argv[1:]
count = len(trace.read_text(encoding="utf-8").splitlines()) if trace.exists() else 0
code = "sapch11code%011d" % (count + 1)
def value(flag):
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
subject = value("--предмет-двери") or value("--предмет")
with open(trace, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "code": code}, ensure_ascii=False) + "\\n")
log = Path(os.environ.get("HARNESS_LOG_DIR") or os.environ["LOG_DIR"])
row = {"v": 1, "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
       "src": "scripts/ask-consent.py", "event": "consent.request", "outcome": "ok",
       "data": {"nonce": code, "вид": value("--вид"), "цель": subject, "почему": value("--почему")}}
with open(log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False) + "\\n")
print("[согласие кнопкой] запрос записан; диспетчер пришлёт владельцу кнопки")
print(code)
'''

# Раннер двери: судья получает носитель стенда подменой owner_word.носитель в процессе двери.
CARRIER_RUNNER = r"""
import json, pathlib, runpy, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["lib"])
import owner_word
carrier = owner_word.Носитель(папка=pathlib.Path(spec["папка"]), uid=None, паспорт=spec["паспорт"],
                              граница=pathlib.Path(spec["граница"]), окно_мин=spec["окно_мин"],
                              срок_кнопки_мин=spec["срок_кнопки_мин"], ручки=spec["ручки"])
owner_word.носитель = lambda: carrier
owner_word.ЗАПРОС_КНОПКИ = pathlib.Path(spec["ask"])
sys.argv = spec["argv"]
runpy.run_path(spec["argv"][0], run_name="__main__")
"""

# Пересборка карточек после записи (Р4): дверь в своём
# процессе, subprocess.run и record подменены записью — живые карточки и журнал не трогаются.
CARDS_RUNNER = r"""
import importlib.util, json, pathlib, sys, types
given = json.loads(sys.argv[1])
spec = importlib.util.spec_from_file_location("self_set_under_probe", given["door"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
calls = []
def fake_run(argv, **_):
    calls.append([str(a) for a in argv])
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
module.subprocess = types.SimpleNamespace(run=fake_run)
module.rebuild_cards()
rebuilds = []
module.rebuild_cards = lambda: rebuilds.append(1)
module.record = lambda *a, **k: True
module.write_field(pathlib.Path(given["instance"]), "цель", "проба карточек", "контекст")
print(json.dumps({"rebuild": calls, "root": str(module.ROOT), "write_field": len(rebuilds)}, ensure_ascii=False))
"""

cases = 0
failures = 0


def check(name: str, ok: bool, details: str = "") -> None:
    """Счёт живёт здесь: итоговая строка — доказательство, врать ей нельзя."""
    global cases, failures
    cases += 1
    if ok:
        print(f"ок    · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name} — {details[:400]}")


def snapshot(paths: list[Path]) -> dict:
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in paths}


def now_iso(hours_ago: float = 0) -> str:
    moment = datetime.datetime.now().astimezone() - datetime.timedelta(hours=hours_ago)
    return moment.isoformat(timespec="seconds")


class Stand:
    """Один стенд: проект, каталог журналов, конфиги, подставная кнопка."""

    def __init__(self, name: str, instance_text: str = INSTANCE_TEXT):
        self.base = STAND / name
        self.project = self.base / "proj"
        self.log = self.base / "log"
        self.instance = self.project / "harness" / "self" / "instance.yaml"
        self.layers = self.project / "harness" / "config" / "memory-layers.yaml"
        self.fake_ask = self.base / "fake-ask-consent.py"
        self.trace = self.base / "ask-consent-trace.jsonl"
        for folder in (self.log, self.instance.parent, self.layers.parent,
                       self.project / "scripts"):
            folder.mkdir(parents=True, exist_ok=True)
        self.instance.write_text(instance_text, encoding="utf-8")
        self.layers.write_text(LAYERS_TEXT, encoding="utf-8")
        self.fake_ask.write_text(FAKE_ASK, encoding="utf-8")
        self.fake_ask.chmod(0o755)
        # Стенд-проект видит дверь там же, где её видит живой: $PROJECT_DIR/scripts.
        (self.project / "scripts" / "self-set.py").symlink_to(SELF_SET)
        # Токена канала у стенда нет: tg_send.sh стенда не отправит ничего.
        (self.base / "install.conf").write_text(
            f'PROJECT_NAME="proba-ch11"\nPROJECT_DIR="{self.project}"\nLOG_DIR="{self.log}"\n'
            f'TG_CHAT_ID="{OWNER}"\nTG_TOKEN_FILE="{self.base}/net-tokena"\nAUTONOMY="semi"\n',
            encoding="utf-8")
        (self.base / "harness.conf").write_text('CONSENT_BUTTON_TTL_MIN="15"\n', encoding="utf-8")
        for name in ("consent-requests.jsonl", "consent-buttons.jsonl", "inbox.jsonl"):
            (self.log / name).write_text("", encoding="utf-8")

    def env(self) -> dict:
        env = dict(os.environ,
                   HARNESS_INSTALL_CONF=str(self.base / "install.conf"),
                   HARNESS_CONF=str(self.base / "harness.conf"),
                   HARNESS_LOG_DIR=str(self.log), LOG_DIR=str(self.log),
                   PROJECT_DIR=str(self.project), SELF_INSTANCE=str(self.instance),
                   MEMORY_LAYERS=str(self.layers), SHIFT_SELF_MARK=str(self.log / "shift.mark"),
                   HARNESS_TASK="-")
        for door in ("SELF_SET_PY", "SESSION_STATE_SH", "HEARTBEAT_DIR", "STATE_DIR"):
            env.pop(door, None)
        return env

    def carrier(self, argv: list[str]) -> str:
        return json.dumps({"lib": str(SELF_SET.resolve().parent.parent / "scripts" / "lib"),
                           "папка": str(self.log), "граница": str(self.base), "окно_мин": 60,
                           "срок_кнопки_мин": 15, "argv": argv, "ask": str(self.fake_ask),
                           "паспорт": config.из_файла(str(self.base / "install.conf")),
                           "ручки": config.из_файла(str(self.base / "harness.conf"))}, ensure_ascii=False)

    def self_set(self, field: str, value: str) -> tuple[int, str]:
        runner = [sys.executable, "-c", CARRIER_RUNNER, self.carrier([str(SELF_SET), field, value])]
        done = subprocess.run(runner, capture_output=True,
                              text=True, timeout=60, cwd=str(self.project), env=self.env(),
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout + done.stderr

    def session_state(self) -> tuple[int, str]:
        done = subprocess.run(["bash", str(SESSION_STATE)], input='{"source":"startup"}',
                              capture_output=True, text=True, timeout=180,
                              cwd=str(self.project), env=self.env())
        return done.returncode, done.stdout + done.stderr

    def asks(self) -> list[dict]:
        if not self.trace.is_file():
            return []
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def journal(self) -> list[dict]:
        """Записи self-set.jsonl: конверт events.emit раскрывается в data."""
        path = self.log / "self-set.jsonl"
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("v") == 1 and isinstance(row.get("data"), dict):
                row = {**row["data"], "ts": row.get("ts"),
                       "время": row["data"].get("время") or row.get("ts")}
            rows.append(row)
        return rows

    def age_journal(self, hours: float) -> None:
        """Сдвинуть все записи журнала в прошлое: сутки стенда без ожидания суток."""
        path = self.log / "self-set.jsonl"
        if not path.is_file():
            return
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "ts" in row:
                row["ts"] = now_iso(hours)
            for holder in (row, row.get("data") if isinstance(row.get("data"), dict) else {}):
                if "время" in holder:
                    holder["время"] = now_iso(hours)
            out.append(json.dumps(row, ensure_ascii=False))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")

    def press(self, answer: str, subject: str, from_id: str = OWNER) -> None:
        """Нажатие так, как его пишет диспетчер: на последнюю просьбу этого предмета.
        Просьбы нет — пишется и она (как если бы ask-consent её записал)."""
        nonce = None
        for line in (self.log / "consent-requests.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("data", {}).get("вид") == KIND and row["data"].get("цель") == subject:
                    nonce = row["data"]["nonce"]
        if nonce is None:
            nonce = f"press{time.time_ns()}"
            with open(self.log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"v": 1, "ts": now_iso(), "src": "scripts/ask-consent.py",
                                     "event": "consent.request", "outcome": "ok",
                                     "data": {"nonce": nonce, "вид": KIND, "цель": subject,
                                              "почему": "проба"}}, ensure_ascii=False) + "\n")
        event = {"да": "consent.yes", "нет": "consent.no"}[answer]
        with open(self.log / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": now_iso(), "src": "проба", "event": event,
                                 "outcome": "ok",
                                 "data": {"nonce": nonce, "вид": KIND, "цель": subject,
                                          "message_id": 4100, "from_id": int(from_id)}},
                                ensure_ascii=False) + "\n")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def field_of(data: dict, field: str):
    for part in field.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def last(rows: list[dict], field: str, value: str) -> dict:
    found = [r for r in rows if r.get("поле") == field and r.get("значение") == value]
    return found[-1] if found else {}


def case_kind_registered() -> None:
    try:
        import owner_word
        kinds = owner_word.ВИДЫ_СОГЛАСИЯ
    except Exception as error:  # noqa: BLE001 — проба называет беду, а не падает
        kinds = f"не прочитан: {error}"
    check(f"вид согласия «{KIND}» в owner_word.ВИДЫ_СОГЛАСИЯ (K45)", KIND in kinds, f"{kinds}")


def case_yes_flow(stand: Stand) -> None:
    """Без «Да» — просьба и «задан»; «Да» — запись с комментариями и sha256."""
    field, value = "роль", "агент проекта: склад #2"
    subject = f"{field}={value}"
    before = stand.instance.read_bytes()
    rc, out = stand.self_set(field, value)
    check("без «Да»: код 3 (ждём кнопку)", rc == WAITING, f"rc={rc} {out.strip()[-200:]!r}")
    check("без «Да»: instance.yaml побайтно цел", stand.instance.read_bytes() == before)
    asks = stand.asks()
    argv = asks[-1]["argv"] if asks else []
    check("без «Да»: кнопка попрошена ровно один раз", len(asks) == 1, f"просьб {len(asks)}")
    check(f"просьба видом «{KIND}»", "--вид" in argv and argv[argv.index("--вид") + 1:][:1] == [KIND],
          f"argv={argv}")
    check(f"просьба на предмет «{subject}»", subject in argv, f"argv={argv}")
    record = last(stand.journal(), field, value)
    check("журнал self-set.jsonl: «состояние: задан»", record.get("состояние") == "задан",
          f"запись {record}")
    # Код кнопки печатают и целиком, и первыми восемью знаками (owner_word.попросить_кнопку):
    # засчитывается начало кода не короче восьми знаков.
    code = str(record.get("код_запроса") or "")
    check("журнал: код_запроса — код этой кнопки",
          bool(asks) and len(code) >= 8 and asks[-1]["code"].startswith(code),
          f"запись {record}, код {asks[-1]['code'] if asks else None}")
    check("журнал: «время» есть", bool(record.get("время")), f"запись {record}")

    stand.press("да", subject)
    rc, out = stand.self_set(field, value)
    after = stand.instance.read_text(encoding="utf-8")
    data = load(stand.instance)
    check("«Да»: код 0", rc == WRITTEN, f"rc={rc} {out.strip()[-200:]!r}")
    check("«Да»: поле записано как есть (yaml читает «:» и «#»)", data.get("роль") == value,
          f"роль={data.get('роль')!r}")
    check("«Да»: комментарии пережили запись",
          "# Комментарий обязан пережить запись поля" in after and "# имя установки" in after,
          after[:300])
    check("«Да»: прочие поля целы",
          data.get("копия") == "proba-ch11" and data.get("человек") == "владелец"
          and data.get("обратная_связь") == {"получаю_от": "человек проекта"}
          and str(data.get("цель", "")).startswith("Проба слоя человека"), f"{data}")
    record = last(stand.journal(), field, value)
    digest = hashlib.sha256(stand.instance.read_bytes()).hexdigest()
    check("журнал: «состояние: да» и sha256 файла после записи",
          record.get("состояние") == "да" and record.get("sha256") == digest,
          f"запись {record}, sha256 файла {digest}")


def case_nested_field(stand: Stand) -> None:
    """Поле раздела «проект:», которого в файле нет, — раздел заводится."""
    field, value = "проект.описание", "склад запчастей: учёт и заказы"
    rc, _ = stand.self_set(field, value)
    stand.press("да", f"{field}={value}")
    rc, out = stand.self_set(field, value)
    data = load(stand.instance)
    check("«Да» на проект.описание: раздел заведён, значение записано",
          rc == WRITTEN and field_of(data, field) == value,
          f"rc={rc} проект={data.get('проект')!r} {out.strip()[-200:]!r}")


def case_no_flow(stand: Stand) -> None:
    """«Нет» — то же значение больше не предлагается; другое — предлагается."""
    field, value = "цель", "Продавать склад целиком"
    subject = f"{field}={value}"
    stand.self_set(field, value)
    stand.press("нет", subject)
    before_file, before_asks = stand.instance.read_bytes(), len(stand.asks())
    rc, out = stand.self_set(field, value)
    check("«Нет»: код 4", rc == REFUSED, f"rc={rc} {out.strip()[-200:]!r}")
    check("«Нет»: файл цел", stand.instance.read_bytes() == before_file)
    check("«Нет»: кнопка не просится снова", len(stand.asks()) == before_asks,
          f"просьб было {before_asks}, стало {len(stand.asks())}")
    check("журнал: «состояние: нет»", last(stand.journal(), field, value).get("состояние") == "нет",
          f"{last(stand.journal(), field, value)}")
    rc, _ = stand.self_set(field, value)
    check("«Нет», второй вызов: код 4 и без кнопки",
          rc == REFUSED and len(stand.asks()) == before_asks, f"rc={rc}, просьб {len(stand.asks())}")

    other = "Вести склад и заказы"
    rc, _ = stand.self_set(field, other)
    check("после «Нет» другое значение — кнопка просится (код 3)",
          rc == WAITING and len(stand.asks()) == before_asks + 1,
          f"rc={rc}, просьб {len(stand.asks())}")
    stand.press("да", f"{field}={other}")
    rc, _ = stand.self_set(field, other)
    data = load(stand.instance)
    after = stand.instance.read_text(encoding="utf-8")
    check("«Да» на цель: многострочное значение заменено целиком",
          rc == WRITTEN and data.get("цель") == other and "цель в две строки" not in after,
          f"rc={rc}, цель={data.get('цель')!r}")


def case_wrong_yes(stand: Stand) -> None:
    """«Да» не владельца и «Да» на другое значение дверь не открывают."""
    field, value = "человек", "владелец склада"
    stand.self_set(field, value)
    before = stand.instance.read_bytes()
    stand.press("да", f"{field}={value}", from_id=STRANGER)
    rc, _ = stand.self_set(field, value)
    check("«Да» не владельца: не записано (код 3)",
          rc == WAITING and stand.instance.read_bytes() == before, f"rc={rc}")
    stand.press("да", f"{field}=другое значение")
    rc, _ = stand.self_set(field, value)
    check("«Да» на другое значение того же поля: не записано (код 3)",
          rc == WAITING and stand.instance.read_bytes() == before, f"rc={rc}")


def case_bypass(stand: Stand) -> None:
    """Сверка при старте смены: молчит после записи дверью, называет поле правки мимо."""
    rc, out = stand.session_state()
    check("старт смены после записи дверью: строки «мимо согласия» нет",
          BYPASS_LINE not in out, out[-300:])
    check("старт смены: код 0", rc == 0, f"rc={rc}")
    text = stand.instance.read_text(encoding="utf-8")
    edited = re.sub(r"(?m)^роль:.*$", "роль: агент вписан руками", text, count=1)
    assert edited != text
    stand.instance.write_text(edited, encoding="utf-8")
    rc, out = stand.session_state()
    line = next((l for l in out.splitlines() if BYPASS_LINE in l), "")
    check("правка мимо двери: «слой 2 изменён мимо согласия: роль»",
          "роль" in line.split(BYPASS_LINE, 1)[-1] if line else False, out[-400:])
    check("правка мимо двери: старт смены не падает (код 0)", rc == 0, f"rc={rc}")


def questions(stand: Stand) -> list[dict]:
    return [r for r in stand.journal() if r.get("источник") == "вопрос" and r.get("состояние") == "задан"]


def case_gaps() -> None:
    """Пробелы: один вопрос за старт, не чаще раза в сутки; полный файл — вопросов нет."""
    gaps = Stand("gaps", INSTANCE_TEXT.replace(
        "цель: >\n  Проба слоя человека: держать\n  цель в две строки.\n", "цель: \"\"\n"))
    assert "цель: \"\"" in gaps.instance.read_text(encoding="utf-8")
    gaps.session_state()
    asked = questions(gaps)
    check("пробелы (цель, проект.описание): ровно один вопрос за старт", len(asked) == 1,
          f"вопросов {len(asked)}: {asked}")
    check("вопрос — об одном из пробелов",
          bool(asked) and asked[0].get("поле") in ("цель", "проект.описание"), f"{asked}")
    gaps.session_state()
    check("второй старт в те же сутки: вопрос не повторён", len(questions(gaps)) == 1,
          f"вопросов {len(questions(gaps))}")
    gaps.age_journal(25)
    gaps.session_state()
    check("старт через сутки: вопрос задан снова (один)", len(questions(gaps)) == 2,
          f"вопросов {len(questions(gaps))}")

    full = Stand("full", INSTANCE_TEXT + "проект:\n  описание: склад запчастей\n")
    full.session_state()
    check("все обязательные поля заполнены: вопросов нет", len(questions(full)) == 0,
          f"вопросов {len(questions(full))}")


def case_cards_rebuild() -> None:
    """После записи поля карточки помощников пересобираются (они несут instance); дверь
    SELF_INSTANCE (проба с чужим файлом) живые карточки не трогает."""
    base = STAND / "cards"
    base.mkdir(parents=True, exist_ok=True)
    instance = base / "instance.yaml"
    for door_set in (False, True):
        instance.write_text(INSTANCE_TEXT, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("SELF_INSTANCE", "SELF_SET_PY")}
        env.update(HARNESS_LOG_DIR=str(base), LOG_DIR=str(base))
        if door_set:
            env["SELF_INSTANCE"] = str(instance)
        done = subprocess.run([sys.executable, "-c", CARDS_RUNNER,
                               json.dumps({"door": str(SELF_SET), "instance": str(instance)})],
                              capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
        try:
            seen = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            check(f"карточки (дверь {door_set}): раннер ответил", False,
                  f"код {done.returncode}; {done.stderr.strip()[-300:]}")
            continue
        root = Path(seen["root"])
        want = [] if door_set else [["--карточки", "--в", str(root / ".claude" / "agents")]]
        got = [c[2:] for c in seen["rebuild"] if len(c) > 1 and c[1] == str(root / "scripts" / "self-prompt.py")]
        got += [c for c in seen["rebuild"] if not (len(c) > 1 and c[1] == str(root / "scripts" / "self-prompt.py"))]
        label = "дверь SELF_INSTANCE: живые карточки не трогаются" if door_set else \
            "без двери: self-prompt.py --карточки --в <ROOT>/.claude/agents"
        check(label, got == want, f"зовы {seen['rebuild']}")
        check(f"запись поля зовёт пересборку карточек (дверь {door_set})", seen["write_field"] == 1,
              f"зовов {seen['write_field']}")


def main() -> int:
    print(f"дверь: {SELF_SET}\nстарт смены: {SESSION_STATE}")
    live_before = snapshot(LIVE_TRACES)
    if not SELF_SET.is_file():
        check("файл двери есть", False, f"нет {SELF_SET} — дверь слоя 2 ещё не написана")
    case_kind_registered()
    main_stand = Stand("main")
    case_yes_flow(main_stand)
    case_nested_field(main_stand)
    case_no_flow(main_stand)
    case_wrong_yes(main_stand)
    case_bypass(main_stand)
    case_gaps()
    case_cards_rebuild()
    live_after = snapshot(LIVE_TRACES)
    changed = [p for p in live_before if live_before[p] != live_after[p]]
    check("живые instance.yaml и self-set.jsonl не тронуты", not changed, f"изменены: {changed}")
    print(f"итог: случаев {cases}, неудач {failures}")
    return 1 if failures or not cases else 0


if __name__ == "__main__":
    sys.exit(main())
