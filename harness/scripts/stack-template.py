#!/usr/bin/env python3
"""Заготовка секции STACK_* для deploy.sh из compose продукта.

Откуда взят. Находка копии bkb 10.11.2026: на первом выкате deploy.sh не знает
продукт, пока секция STACK_* пуста, и копия писала её руками с нуля — хотя
службы со сборкой, имена образов, порт наружу и база уже названы в compose
продукта. Установщик зовёт заготовку сам, как только знает PRODUCT_PATH.

Заготовка — ПРЕДЛОЖЕНИЕ: каждая строка закомментирована. Выкат с несверенной
живостью и угаданными миграциями — не выкат, поэтому включает секцию тот, кто
сверил её с продуктом (перенос значений в ключи секции «Стек продукта»).

Пароли базы из compose в заготовку не попадают: переменные базы раскрывает
контейнер при дампе (`sh -c '…$POSTGRES_USER…'`), а не чтение конфига.

    python3 scripts/stack-template.py <каталог продукта>               # печать
    python3 scripts/stack-template.py <каталог продукта> --в <harness.conf>
Код: 0 — заготовка снята или предлагать нечего (причина словами); 1 — compose
не разбирается или неверный вызов.
"""
from __future__ import annotations

import argparse
import datetime
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

# Порядок поиска — как у самого `docker compose`.
COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
MARK = "# ── Заготовка STACK_*"
POSTGRES_DUMP = ("pg_dump -U \"${POSTGRES_USER:-postgres}\" -Fc "
                 "\"${POSTGRES_DB:-${POSTGRES_USER:-postgres}}\"")


def find_compose(product: Path) -> Path | None:
    return next((product / n for n in COMPOSE_NAMES if (product / n).is_file()), None)


def project_name(compose: Path, data: dict) -> str:
    """Имя проекта, как его приводит compose: строчные, только [a-z0-9_-]."""
    raw = str(data.get("name") or compose.parent.resolve().name).lower()
    return re.sub(r"^[^a-z0-9]+", "", re.sub(r"[^a-z0-9_-]", "", raw))


def image_of(project: str, service: str, spec: dict) -> str:
    image = str(spec.get("image") or f"{project}-{service}")
    return image[:-len(":latest")] if image.endswith(":latest") else image


def host_port(entry) -> str | None:
    if isinstance(entry, dict):
        published = entry.get("published")
        return str(published) if published not in (None, "") else None
    parts = str(entry).split("/")[0].split(":")
    return parts[-2] if len(parts) >= 2 and parts[-2].isdigit() else None


def conf_value(text: str) -> str:
    """Значение в двойных кавычках harness.conf: bash не раскрывает в нём ничего."""
    return re.sub(r'([\\"$`])', r"\\\1", text)


def line(key: str, value: str, note: str = "") -> str:
    return f'# {key}="{conf_value(value)}"' + (f"  # {note}" if note else "")


def git_root(product: Path) -> str:
    out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(product),
                          "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip() if out.returncode == 0 else ""


def template(product: Path, compose: Path, data: dict) -> str:
    services = data.get("services") or {}
    project = project_name(compose, data)
    built = [name for name, spec in services.items() if isinstance(spec, dict) and "build" in spec]
    dc = f"docker compose -f {shlex.quote(str(compose))}"
    names = " ".join(built)
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    out = [f"{MARK} из {compose} (stack-template.py, {stamp}) ──",
           "# Предложение, не включено: сверь с продуктом и перенеси значения в ключи",
           "# секции «Стек продукта» выше. Живость и миграции угадать нельзя — проверь их."]
    root = git_root(product)
    out.append(line("PRODUCT_REPO", root, "корень git продукта: тег и сверка секретов выката")
               if root else "# PRODUCT_REPO — у продукта нет git: выкат ставит тег в его репозиторий, заведи")
    if built:
        out += [line("STACK_BUILD_CMD", f"{dc} build {names}", "только образы: службы поднимет RELEASE"),
                line("STACK_BUILD_NOCACHE_CMD", f"{dc} build --no-cache {names}"),
                line("STACK_RELEASE_CMD", f"{dc} up -d --force-recreate {names}", "после миграций"),
                line("STACK_UP_CMD", f"{dc} up -d --no-build", "подъём при откате"),
                line("STACK_IMAGES", " ".join(image_of(project, n, services[n]) for n in built),
                     "снимок :previous")]
    else:
        out.append("# STACK_BUILD_CMD — в compose нет служб со сборкой (build:): собирать нечего, задай сам")
    port = next((p for n in built for p in map(host_port, services[n].get("ports") or []) if p), None)
    out.append(line("STACK_HEALTH_URL", f"http://127.0.0.1:{port}/", "сверь путь живости")
               if port else
               '# STACK_HEALTH_URL=""  # порта наружу у служб со сборкой нет — задай STACK_HEALTH_CMD или URL сам')
    db = next((n for n, s in services.items() if isinstance(s, dict)
               and re.match(r"(postgres|postgis)", str(s.get("image", "")).split("/")[-1])), None)
    out.append(line("STACK_DB_DUMP_CMD", f"{dc} exec -T {db} sh -c '{POSTGRES_DUMP}'", "дамп перед миграциями")
               if db else "# STACK_DB_DUMP_CMD — службы postgres в compose нет: дамп базы задай сам (И-1)")
    out += ["# STACK_MIGRATE_CMD — миграции из compose не видны: задай, если они есть",
            "# ── конец заготовки ──"]
    return "\n".join(out) + "\n"


def write_into(conf: Path, block: str) -> str:
    text = conf.read_text(encoding="utf-8")
    if MARK in text:
        return f"заготовка STACK_* уже есть в {conf} — не дописываю"
    if re.search(r'(?m)^STACK_BUILD_CMD="[^"]', text):
        return f"секция STACK_* в {conf} уже заполнена — заготовка не нужна"
    with conf.open("a", encoding="utf-8") as fh:
        fh.write(("" if text.endswith("\n") else "\n") + "\n" + block)
    return f"заготовка STACK_* дописана в {conf} закомментированной — сверь и перенеси значения"


def main() -> int:
    parser = argparse.ArgumentParser(description="Заготовка STACK_* из compose продукта")
    parser.add_argument("product", type=Path)
    parser.add_argument("--в", dest="conf", type=Path)
    args = parser.parse_args()
    compose = find_compose(args.product)
    if compose is None:
        print(f"stack-template: в {args.product} нет compose ({', '.join(COMPOSE_NAMES)}) — заготовки нет")
        return 0
    try:
        data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as trouble:
        print(f"stack-template: {compose} не разбирается — заготовки нет: {trouble}", file=sys.stderr)
        return 1
    if not isinstance(data, dict) or not isinstance(data.get("services") or {}, dict):
        print(f"stack-template: в {compose} нет словаря services — заготовки нет", file=sys.stderr)
        return 1
    block = template(args.product, compose, data)
    print(write_into(args.conf, block) if args.conf else block, end="" if not args.conf else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
