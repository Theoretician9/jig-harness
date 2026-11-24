#!/usr/bin/env python3
"""Проба ручек панели «выкат» и «фоновые процессы»: обе в реестре и пишутся.

Приказ владельца 17.11.2026 (память/owner-orders-17-11-verbatim.md): выключатель
в панели для выката и фоновых. Панель рисует и пишет только то, что описано в
harness/config/switches.yaml: ключа BACKGROUND_PROCESSES там нет, и
scripts/write-key.py отказывает ему кодом 2; DEPLOY_ENABLED нет нигде.

Судим ЖИВОЙ реестр рабочего дерева (только чтение), пишем — в свой harness.conf
двора (двери HARNESS_CONF, HARNESS_INSTALL_CONF, LOG_DIR, PANEL_NO_SUDO).
Сверка гейтом check-switches — его же функцией `проверить` на конфигах двора.
"""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "harness" / "config" / "switches.yaml"
SAMPLE_CONF = ROOT / "harness" / "config" / "harness.conf"
WRITER = ROOT / "scripts" / "write-key.py"
_YARD = Path(tempfile.mkdtemp(prefix="proba-ruchki-vykat-fon-"))
CONF = _YARD / "harness.conf"
INSTALL = _YARD / "install.conf"
LOGS = _YARD / "log"
LOGS.mkdir()
INSTALL.write_text('AUTONOMY="semi"\n', encoding="utf-8")
ENV = dict(os.environ, HARNESS_CONF=str(CONF), HARNESS_INSTALL_CONF=str(INSTALL),
           HARNESS_PERECLUCHATELI=str(REGISTRY), LOG_DIR=str(LOGS), PANEL_NO_SUDO="1")

# ключ → (значения перечня, умолчание, читатель)
EXPECTED = {
    "BACKGROUND_PROCESSES": ({"on", "off"}, "on", "scripts/run-background.sh"),
    "DEPLOY_ENABLED": ({"1", "0"}, "1", "scripts/deploy_guard.py"),
}

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


def stand_conf():
    CONF.write_text("# конфиг двора\nBACKGROUND_PROCESSES=\"on\"\nDEPLOY_ENABLED=\"1\"\n", encoding="utf-8")


def write_key(key, value):
    done = subprocess.run([sys.executable, str(WRITER), key, value], capture_output=True, text=True,
                          env=ENV, timeout=60)
    return done.returncode, (done.stdout + done.stderr).strip()


registry_before = hashlib.sha256(REGISTRY.read_bytes()).hexdigest()
registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
rows = {row.get("ключ"): row for row in registry.get("переключатели") or []}

for key, (values, default, reader) in EXPECTED.items():
    row = rows.get(key)
    check(f"{key}: строка в реестре переключателей есть", row is not None, "строки нет")
    row = row or {}
    listed = {str(v.get("значение")) for v in row.get("значения") or []}
    defaults = [str(v.get("значение")) for v in row.get("значения") or [] if v.get("по_умолчанию")]
    check(f"{key}: файл harness.conf, вид выбор, значения {sorted(values)}",
          row.get("файл") == "harness.conf" and row.get("вид") == "выбор" and listed == values,
          f"файл {row.get('файл')!r}, вид {row.get('вид')!r}, значения {sorted(listed)}")
    check(f"{key}: по умолчанию «{default}»", defaults == [default], f"по_умолчанию {defaults}")
    check(f"{key}: читатель {reader}, при_вызове, сразу",
          (row.get("читатель"), row.get("способ_чтения"), row.get("применяется"))
          == (reader, "при_вызове", "сразу"),
          f"{row.get('читатель')!r}, {row.get('способ_чтения')!r}, {row.get('применяется')!r}")
    words = str(row.get("простыми словами") or "")
    labels = [str(v.get("подпись") or "") for v in row.get("значения") or []]
    # По-человечески: подпись и пояснение без имени ключа и без голых on/off/0/1.
    check(f"{key}: подписи и «простыми словами» человеческим языком",
          len(words) >= 60 and key not in words and row.get("имя") and key not in str(row.get("имя"))
          and len(labels) == len(values) and all(l and l not in values for l in labels),
          f"имя {row.get('имя')!r}, подписи {labels}, слов {len(words)}")

    stand_conf()
    for value in sorted(values):
        code, output = write_key(key, value)
        written = dict(line.split("=", 1) for line in CONF.read_text(encoding="utf-8").splitlines()
                       if "=" in line and not line.startswith("#"))
        check(f"БОЛЬНОЙ СЛУЧАЙ: write-key.py {key} {value} → 0 и значение в конфиге двора",
              code == 0 and written.get(key, "").strip('"') == value,
              f"код {code} · {output[-200:]!r}")
    code, output = write_key(key, "maybe")
    check(f"{key}: значение не из перечня → отказ 3", code == 3, f"код {code} · {output[-160:]!r}")

# Гейт check-switches на конфиге двора: по этим двум ключам расхождений нет.
spec = importlib.util.spec_from_file_location("check_switches_under_test", ROOT / "scripts" / "check-switches.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
stand_conf()
troubles = [t for t in gate.проверить(registry, {"harness.conf": str(CONF), "install.conf": str(INSTALL)})
            if t.split(":", 1)[0] in EXPECTED]
seen = [k for k in EXPECTED if k in rows]
check("check-switches: обе ручки описаны и сходятся с читателем и конфигом",
      len(seen) == len(EXPECTED) and not troubles, f"описаны {seen}, беды {troubles}")

# Образец конфига пакета везёт новый ключ на новую машину (фоновые там уже есть).
sample = SAMPLE_CONF.read_text(encoding="utf-8")
for key, (_values, default, _reader) in EXPECTED.items():
    check(f"образец harness/config/harness.conf: {key}=\"{default}\"", f'\n{key}="{default}"' in sample,
          "строки нет или значение другое")

check("живой реестр пробой не тронут",
      hashlib.sha256(REGISTRY.read_bytes()).hexdigest() == registry_before)

shutil.rmtree(_YARD, ignore_errors=True)
print(f"РУЧКИ-ВЫКАТ-ФОН: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
