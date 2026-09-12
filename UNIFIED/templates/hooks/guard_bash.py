#!/usr/bin/env python3
"""Запрет на команды, которые уже ломали работу.

Хук PreToolUse на Bash. Читает вызов из stdin, при совпадении с известной бедой
возвращает код 2 — харнес отменяет вызов и показывает причину модели.

Здесь только то, что реально случалось. Защита от воображаемых бед прячет ту,
которая бывает, поэтому список короткий и каждый пункт назван своей историей.
Ложный запрет хуже пропуска: от него избавляются обходом всей защиты.
"""
import json
import os
import re
import subprocess
import sys

# Команда узнаётся ТОЛЬКО в позиции команды: в начале строки, после разделителя
# или внутри подстановки. Иначе хук срабатывает на тексте, который про эту команду
# лишь рассказывает, — так он заблокировал запись собственной документации в первый
# же день. Запрет, мешающий писать о запрете, снимают вместе со всей защитой.
# Обратная кавычка в позицию команды НЕ входит: как подстановка она устарела и в
# наших командах не встречается, зато ею размечают код в markdown — и сторож ловил
# `команду` в тексте документации, мешая эту документацию писать.
_CMD_POS = r"(?:^|[;&|(]\s*|\$\(\s*)"
MCP_CHECK = re.compile(_CMD_POS + r"claude\s+mcp\b", re.MULTILINE)
CLAUDE_RUN = re.compile(_CMD_POS + r"claude\s+(?!mcp\b)", re.MULTILINE)
STRICT_MCP = re.compile(r"--strict-mcp-config")
COMPOSE_RESTART = re.compile(r"docker\s+compose\s+restart\b")
PYTEST_CALL = re.compile(r"\bpytest\b")


def _ancestors() -> set[int]:
    """Свои процессы вверх по дереву.

    Без этого проверка «идёт ли уже прогон тестов» ловит саму себя: команда,
    которую хук разбирает, лежит в его собственной командной строке, и поиск по
    строке находит слово pytest у родительской оболочки. Поймано тестом хука.
    """
    seen, pid = set(), os.getpid()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().split(") ", 1)[1].split()[1])
        except Exception:
            break
    return seen


def running_pytest() -> list[str]:
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    mine = _ancestors()
    found = []
    for line in out.stdout.splitlines():
        pid_s, _, args = line.strip().partition(" ")
        if not pid_s.isdigit() or int(pid_s) in mine:
            continue
        if re.search(r"(^|/)(python[\d.]*\s+-m\s+pytest|pytest)\b", args):
            found.append(pid_s)
    return found


def deny(reason: str) -> int:
    print(f"Команда остановлена хуком.\n\n{reason}", file=sys.stderr)
    return 2


def check(cmd: str) -> int:
    if MCP_CHECK.search(cmd):
        return deny(
            "«claude mcp» поднимает telegram-плагин, а Telegram допускает одного\n"
            "потребителя обновлений на токен: новый держатель убивает живой сеанс,\n"
            "и связь с владельцем обрывается.\n"
            "Состав MCP смотреть чтением ~/.claude/plugins/installed_plugins.json."
        )

    if CLAUDE_RUN.search(cmd) and not STRICT_MCP.search(cmd):
        return deny(
            "Запуск claude из сеанса разрешён ТОЛЬКО с пустым набором MCP — иначе\n"
            "поднимется telegram-плагин и оборвёт связь с владельцем:\n"
            "  claude -p \"...\" --mcp-config '{\"mcpServers\":{}}' --strict-mcp-config"
        )

    if COMPOSE_RESTART.search(cmd):
        return deny(
            "«docker compose restart» НЕ пересобирает образ — правка не доедет до\n"
            "прода, и проверка покажет старое поведение. Нужно:\n"
            "  docker compose up -d --build <сервис>\n"
            "Деплой бэкенда целиком — ./scripts/deploy.sh"
        )

    if PYTEST_CALL.search(cmd):
        pids = running_pytest()
        if pids:
            return deny(
                "Прогон тестов уже идёт (процессы: " + ", ".join(pids) + ").\n\n"
                "Тестовая база одна, фикстуры чистят таблицы перед каждым тестом —\n"
                "второй одновременный прогон даёт десятки ложных падений в обоих.\n"
                "Дождись завершения первого."
            )

    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # не разобрали вызов — не мешаем работать
    if payload.get("tool_name") != "Bash":
        return 0
    return check((payload.get("tool_input") or {}).get("command") or "")


if __name__ == "__main__":
    sys.exit(main())
