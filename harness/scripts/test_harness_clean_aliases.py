#!/usr/bin/env python3
"""Проба: гейт чистоты харнеса судит прозвища продукта (PRODUCT_ALIASES), а не имя каталога.

Откуда взята. Копия Content-Factory (замер 10.11.2026): при `PRODUCT_DIR="html"`
гейт нашёл 166 вхождений в 13 файлах — «html» слово языка разметки, а не имя
продукта. Имя продукта — только `PRODUCT_ALIASES`; чтобы копии, где имя каталога
и есть имя продукта, проверку не потеряли, установщик кладёт имя каталога в
PRODUCT_ALIASES сам (его проба — test_installer_product_aliases.sh).

Гейт гоняется ЦЕЛИКОМ (main, режим --список) на своём стенде: копия гейта,
гейта сверки с пакетом (список «что харнес») и загрузчика конфига в свой
каталог, свой harness.conf через HARNESS_CONF. Живой конфиг установки и живое
дерево пробе не нужны.

Имена подставные (mojprodukt, МОЙПРОДУКТ): настоящее имя продукта в харнесе
запрещено самим этим гейтом.

    python3 scripts/test_harness_clean_aliases.py
    HARNESS_CLEAN_PY=<копия гейта> python3 scripts/test_harness_clean_aliases.py
Код возврата: 0 — все пути прошли, 1 — есть упавший.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

GATE = Path(os.environ.get("HARNESS_CLEAN_PY") or HERE / "check-harness-clean.py")
# Файл харнеса на стенде: путь обязан попадать в список «что харнес» сверки с пакетом.
HARNESS_FILE = "scripts/panel.py"


def build_stand(tmp: Path) -> Path:
    """Корень стенда: гейт видит его своим КОРНЕМ (parent.parent скрипта)."""
    (tmp / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(GATE, tmp / "scripts" / "check-harness-clean.py")
    shutil.copy(HERE / "compare-with-package.py", tmp / "scripts" / "compare-with-package.py")
    shutil.copy(HERE / "lib" / "config.py", tmp / "scripts" / "lib" / "config.py")
    (tmp / HARNESS_FILE).write_text(
        'PAGE = "<html><body></body></html>"\n'
        '# разметка: html-шаблон панели\n'
        'ROOTS = ("mojprodukt", ".")\n',
        encoding="utf-8")
    return tmp


def run_gate(stand: Path, product_dir: str, aliases: str) -> tuple[int, str]:
    conf = stand / "harness.conf"
    conf.write_text(f'PRODUCT_DIR="{product_dir}"\nPRODUCT_ALIASES="{aliases}"\n',
                    encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PRODUCT_DIR", "PRODUCT_ALIASES")}
    env["HARNESS_CONF"] = str(conf)
    out = subprocess.run(
        [sys.executable, str(stand / "scripts" / "check-harness-clean.py"), "--список"],
        input=HARNESS_FILE + "\n", capture_output=True, text=True, env=env, timeout=60)
    return out.returncode, out.stdout + out.stderr


def load_gate(stand: Path):
    path = stand / "scripts" / "check-harness-clean.py"
    spec = importlib.util.spec_from_file_location("harness_clean_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    cases = Cases()
    with tempfile.TemporaryDirectory(prefix="proba-harness-clean-") as tmp_name:
        stand = build_stand(Path(tmp_name))

        # Контроль стенда: файл харнеса правда в списке «что харнес» и правда
        # несёт оба слова — иначе зелёный случай 1 был бы зелёным на пустом месте.
        gate = load_gate(stand)
        cases.check("контроль: файл стенда — харнес по сверке с пакетом",
                    True, gate._место_в_пакете()(HARNESS_FILE) is not None)
        text = (stand / HARNESS_FILE).read_text(encoding="utf-8")
        cases.check("контроль: в файле стенда есть «html» и «mojprodukt»",
                    True, "html" in text and "mojprodukt" in text)

        rc, out = run_gate(stand, "html", "")
        cases.check("1 БОЛЬНОЙ СЛУЧАЙ: PRODUCT_DIR=html, прозвищ нет, «html» в харнесе — rc 0",
                    0, rc)
        cases.check("1 БОЛЬНОЙ СЛУЧАЙ: в выводе нет находки «html»",
                    False, "«html»" in out)
        cases.check("1 причина молчания названа ключом PRODUCT_ALIASES",
                    True, "PRODUCT_ALIASES пуст" in out)

        rc, out = run_gate(stand, "html", "mojproduktxyz МОЙПРОДУКТ")
        cases.check("1б PRODUCT_DIR=html, прозвища другие и в харнесе их нет — rc 0", 0, rc)

        rc, out = run_gate(stand, "html", "mojprodukt МОЙПРОДУКТ")
        cases.check("2 прозвище из PRODUCT_ALIASES в харнесе — rc 1", 1, rc)
        cases.check("2 находка названа файлом и строкой", True,
                    f"{HARNESS_FILE}:3" in out)
        cases.check("2 «html» при этом не находка (каталог не судится)",
                    False, "«html»" in out)

        names = gate.имена_продукта(str(stand / "harness.conf"))
        cases.check("3 имена продукта — ровно прозвища, PRODUCT_DIR не участвует",
                    ["mojprodukt", "МОЙПРОДУКТ"], names)

        rc, out = run_gate(stand, "mojprodukt", "")
        cases.check("4 имя каталога без прозвищ гейт само не судит (его кладёт установщик) — rc 0",
                    0, rc)

    print(f"ИТОГ: путей {cases.paths}, плохих {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
