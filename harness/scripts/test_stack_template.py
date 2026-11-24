#!/usr/bin/env python3
"""Проба: заготовка секции STACK_* из compose продукта (stack-template.py) и её вызов установщиком.

Откуда взята. Находка копии сети 10.11.2026: deploy.sh на первом выкате не знает
продукт, пока не заполнены STACK_* и PRODUCT_REPO, хотя PRODUCT_PATH уже задан
установщиком, — и секцию копия пишет руками с нуля, хотя всё нужное (службы со
сборкой, образы, порт наружу, база) уже лежит в compose продукта.

Заготовка — ПРЕДЛОЖЕНИЕ: каждая её строка закомментирована, и выкат она не
включает (сборка без сверенного health — не выкат). Проба судит это прямо:
конфиг после дописки отдаёт пустой STACK_BUILD_CMD.

Значения заготовки судятся ИСПОЛНЕНИЕМ: строки без «# » читает bash, как
прочтёт их harness.conf, — `$` дампа доезжает буквальным, а не раскрывается
при чтении конфига.

Установщик достаётся РАЗБОРОМ (строка вызова; пустое извлечение — провал) и
исполняется на своём стенде: живой /etc/harness проба не трогает.

    python3 scripts/test_stack_template.py
Двери: STACK_TEMPLATE_UNDER_TEST — судимый stack-template.py,
USTANOVIT_UNDER_TEST — судимый установщик.
Код возврата: 0 — все пути прошли, 1 — есть упавший.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

TOOL = Path(os.environ.get("STACK_TEMPLATE_UNDER_TEST") or HERE / "stack-template.py")
MARK = "# ── Заготовка STACK_*"
SECRET = "s3cr3t-proba-parol"  # не-секрет: пароль подставного compose стенда

COMPOSE = f"""\
services:
  backend:
    build: ./backend
    ports: ["127.0.0.1:8010:8000"]
  worker:
    build: ./backend
  web:
    build: {{context: ./front}}
    image: registry.local/shop-web:latest
    ports:
      - target: 80
        published: 8080
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: {SECRET}
  redis:
    image: redis:7
"""


def installer_path() -> Path:
    if os.environ.get("USTANOVIT_UNDER_TEST"):
        return Path(os.environ["USTANOVIT_UNDER_TEST"])
    beside = HERE.parent.parent / "USTANOVIT.sh"   # проба в пакете: harness/scripts
    if beside.is_file():
        return beside
    import config  # noqa: E402 — проба в проекте: пакет называет конфиг
    return config.katalog_paketa() / "USTANOVIT.sh"


def run_tool(*args: str) -> tuple[int, str, str]:
    out = subprocess.run([sys.executable, "-I", str(TOOL), *args],
                         capture_output=True, text=True, timeout=60)
    return out.returncode, out.stdout, out.stderr


def make_product(root: Path, name: str, compose: str, file: str = "docker-compose.yml") -> Path:
    product = root / name
    product.mkdir(parents=True)
    (product / file).write_text(compose, encoding="utf-8")
    subprocess.run(["git", "-C", str(product), "init", "-q"], check=True)
    return product


def proposal(text: str) -> dict[str, str]:
    """Значения заготовки так, как их прочтёт bash после снятия «# »."""
    lines = [l[2:] for l in text.splitlines() if re.match(r"^# [A-Z][A-Z0-9_]*=", l)]
    script = "\n".join(lines) + "\nfor k in $(compgen -v | grep -E '^(STACK_|PRODUCT_)'); do printf '%s\\t%s\\n' \"$k\" \"${!k}\"; done\n"
    out = subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                         capture_output=True, text=True, timeout=30)
    return dict(l.split("\t", 1) for l in out.stdout.splitlines() if "\t" in l)


def sourced(conf: Path, key: str) -> str:
    out = subprocess.run(["bash", "--noprofile", "--norc", "-c", f'source "$1"; printf %s "${{{key}-нет}}"', "_", str(conf)],
                         capture_output=True, text=True, timeout=30)
    return out.stdout


def block_of(text: str) -> str:
    start = text.find(MARK)
    return text[start:] if start >= 0 else ""


def tool_cases(cases: Cases, tmp: Path) -> None:
    product = make_product(tmp, "shopxyz", COMPOSE)
    compose = product / "docker-compose.yml"
    rc, out, err = run_tool(str(product))
    cases.check("1 БОЛЬНОЙ СЛУЧАЙ: compose есть — заготовка снята, rc 0", 0, rc)
    block = block_of(out)
    cases.check("1 заготовка помечена своей шапкой", True, bool(block))
    body = [l for l in block.splitlines() if l.strip()]
    cases.check("1 каждая строка заготовки закомментирована (ничего не включено)",
                [], [l for l in body if not l.startswith("#")])
    v = proposal(out)
    dc = f"docker compose -f {compose}"
    cases.check("2 сборка: только службы со build, в порядке compose",
                f"{dc} build backend worker web", v.get("STACK_BUILD_CMD"))
    cases.check("2 сборка --no-cache тех же служб",
                f"{dc} build --no-cache backend worker web", v.get("STACK_BUILD_NOCACHE_CMD"))
    cases.check("2 пересоздание служб после миграций",
                f"{dc} up -d --force-recreate backend worker web", v.get("STACK_RELEASE_CMD"))
    cases.check("2 подъём при откате без сборки", f"{dc} up -d --no-build", v.get("STACK_UP_CMD"))
    cases.check("3 образы: имя проекта-служба, а заданный image — без тега latest",
                "shopxyz-backend shopxyz-worker registry.local/shop-web", v.get("STACK_IMAGES"))
    cases.check("4 живость: первый порт наружу службы со сборкой (вид ip:хост:контейнер)",
                "http://127.0.0.1:8010/", v.get("STACK_HEALTH_URL"))
    cases.check("5 дамп postgres: переменные базы раскрывает контейнер, не чтение конфига",
                f"{dc} exec -T db sh -c 'pg_dump -U \"${{POSTGRES_USER:-postgres}}\" -Fc "
                f"\"${{POSTGRES_DB:-${{POSTGRES_USER:-postgres}}}}\"'",
                v.get("STACK_DB_DUMP_CMD"))
    cases.check("5 пароль базы из compose в заготовку не попал", False, SECRET in out + err)
    cases.check("6 PRODUCT_REPO — корень git продукта",
                str(product.resolve()), v.get("PRODUCT_REPO"))

    named = make_product(tmp, "dir-imya", "name: Other_Proj\nservices:\n  api:\n    build: .\n")
    rc, out, _ = run_tool(str(named))
    v = proposal(out)
    cases.check("7 имя проекта из name: compose, приведённое как у compose", "other_proj-api",
                v.get("STACK_IMAGES"))
    cases.check("7 порта наружу нет — живость пуста и названа словами",
                ("", True), (v.get("STACK_HEALTH_URL"), "STACK_HEALTH_" in block_of(out)))

    both = make_product(tmp, "oba", "services:\n  novyj:\n    build: .\n", file="compose.yaml")
    (both / "docker-compose.yml").write_text("services:\n  staryj:\n    build: .\n", encoding="utf-8")
    rc, out, _ = run_tool(str(both))
    cases.check("8 compose.yaml старше docker-compose.yml (порядок docker compose)",
                f"docker compose -f {both / 'compose.yaml'} build novyj", proposal(out).get("STACK_BUILD_CMD"))

    empty = tmp / "pusto"
    empty.mkdir()
    rc, out, err = run_tool(str(empty))
    cases.check("9 compose нет — rc 0, заготовки нет, причина словами",
                (0, "", True), (rc, block_of(out), "compose" in out + err))

    broken = make_product(tmp, "slomano", "services: [\n")
    rc, out, err = run_tool(str(broken))
    cases.check("10 compose не разбирается — rc 1, заготовки нет", (1, ""), (rc, block_of(out)))

    conf_cases(cases, tmp, product)


CONF = """\
# ── Стек продукта (deploy.sh)
STACK_BUILD_CMD=""
STACK_IMAGES=""
PRODUCT_PATH=""
"""


def conf_cases(cases: Cases, tmp: Path, product: Path) -> None:
    conf = tmp / "harness.conf"
    conf.write_text(CONF, encoding="utf-8")
    rc, out, _ = run_tool(str(product), "--в", str(conf))
    text = conf.read_text(encoding="utf-8")
    cases.check("11 --в: заготовка дописана в конец, прежний текст цел до байта",
                (0, True, True), (rc, text.startswith(CONF), bool(block_of(text))))
    cases.check("11 --в: живой ключ STACK_BUILD_CMD один и пуст (выкат не включён)",
                (1, ""), (len(re.findall(r"(?m)^STACK_BUILD_CMD=", text)), sourced(conf, "STACK_BUILD_CMD")))
    again = text
    run_tool(str(product), "--в", str(conf))
    cases.check("12 --в повторно: файл не меняется", again, conf.read_text(encoding="utf-8"))

    filled = tmp / "zapolneno.conf"
    filled.write_text(CONF.replace('STACK_BUILD_CMD=""', 'STACK_BUILD_CMD="make image"'), encoding="utf-8")
    before = filled.read_text(encoding="utf-8")
    rc, out, _ = run_tool(str(product), "--в", str(filled))
    cases.check("13 --в: секция уже заполнена — файл не меняется, rc 0",
                (0, before), (rc, filled.read_text(encoding="utf-8")))

    (tmp / "etc" / "agent").mkdir(parents=True)
    target = tmp / "etc" / "agent" / "harness.conf"
    target.write_text(CONF, encoding="utf-8")
    link = tmp / "etc" / "harness.conf"
    link.symlink_to("agent/harness.conf")
    run_tool(str(product), "--в", str(link))
    cases.check("14 --в по ссылке: ссылка осталась ссылкой, правится цель",
                (True, True), (link.is_symlink(), bool(block_of(target.read_text(encoding="utf-8")))))


def installer_cases(cases: Cases, tmp: Path) -> None:
    src = installer_path()
    if not src.is_file():
        cases.check(f"установщик {src} найден", True, False)
        return
    lines = src.read_text(encoding="utf-8").splitlines()
    calls = [(n, l) for n, l in enumerate(lines, 1)
             if "stack-template.py" in l and "--в /etc/harness/harness.conf" in l]
    path_set = [n for n, l in enumerate(lines, 1)
                if "s|^PRODUCT_PATH=" in l and "/etc/harness/harness.conf" in l]
    cases.check("15 установщик: вызов заготовки одной строкой, одно место", 1, len(calls))
    cases.check("15 контроль: шаг записи PRODUCT_PATH в harness.conf найден", 1, len(path_set))
    if len(calls) != 1 or len(path_set) != 1:
        return
    call_no, call = calls[0]
    cases.check(f"15 вызов после записи PRODUCT_PATH (строка {call_no} > {path_set[0]})",
                True, call_no > path_set[0])

    if not TOOL.is_file():
        return
    pkg = tmp / "paket"
    (pkg / "harness" / "scripts").mkdir(parents=True)
    shutil.copy(TOOL, pkg / "harness" / "scripts" / "stack-template.py")
    product = make_product(tmp, "inst-produkt", "services:\n  app:\n    build: .\n")

    def run_call(product_path: str) -> str:
        stand = tmp / "inst.conf"
        stand.write_text(CONF, encoding="utf-8")
        script = ('делай() { local c="${1//\\/etc\\/harness\\/harness.conf/$STAND}"; eval "$c"; }\n'
                  + call.strip() + "\n")
        env = dict(os.environ, STAND=str(stand), PRODUCT_PATH=product_path, pkg_dst=str(pkg))
        # Каталог запуска — с compose: вызов без проверки PRODUCT_PATH взял бы «.» за продукт.
        subprocess.run(["bash", "--noprofile", "--norc", "-c", script], env=env, cwd=product,
                       capture_output=True, text=True, timeout=60)
        return stand.read_text(encoding="utf-8")

    cases.check("16 вызов: PRODUCT_PATH задан — заготовка дописана в harness.conf",
                True, f"docker compose -f {product / 'docker-compose.yml'} build app" in run_call(str(product)))
    cases.check("16 вызов: PRODUCT_PATH пуст — harness.conf не меняется", CONF, run_call(""))


def main() -> int:
    cases = Cases()
    if not TOOL.is_file():
        cases.check(f"stack-template.py найден ({TOOL})", True, False)
    with tempfile.TemporaryDirectory(prefix="proba-stack-template-") as tmp_name:
        tmp = Path(tmp_name)
        if TOOL.is_file():
            tool_cases(cases, tmp)
        installer_cases(cases, tmp)
    print(f"ИТОГ: путей {cases.paths}, плохих {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
