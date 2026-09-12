"""Прогон хука-сторожа по всем путям.

Отдельным файлом, а не строкой в оболочке: сторож разбирает текст команды,
и тестовые примеры внутри командной строки он видит как саму команду.
"""
import json
import pathlib
import subprocess
import sys

# Путь относительно самого теста: набор переносится целиком, тест работает из любого каталога.
GUARD = ["python3", str(pathlib.Path(__file__).resolve().with_name("guard_bash.py"))]

CASES = [
    # (ожидаемый код, описание, команда)
    (2, "проверка состава MCP", "claude mcp list"),
    (2, "фоновый запуск без строгого флага", 'claude -p "сделай X"'),
    (2, "он же в конвейере", 'echo hi | claude -p "x"'),
    (2, "он же в подстановке", 'out=$(claude -p "x")'),
    (2, "перезапуск контейнера без пересборки", "docker compose restart backend"),
    (0, "запуск с пустым набором MCP", "claude -p \"x\" --mcp-config '{\"mcpServers\":{}}' --strict-mcp-config"),
    (0, "пересборка", "docker compose up -d --build backend"),
    (0, "прогон тестов, когда другого нет", "docker compose exec -T backend python -m pytest tests/ -q"),
    (0, "обычная команда", "git status --short"),
    (0, "ТЕКСТ про запрет, а не команда", 'echo "никогда не запускай claude mcp list — оборвётся связь"'),
    (0, "текст в середине строки", 'grep -n "claude mcp" docs/*.md'),
    # Обратная кавычка размечает код в markdown. Пока она считалась позицией
    # команды, сторож не давал писать документацию про самого себя.
    (0, "разметка кода в markdown", "echo 'смотри `claude mcp list` в документации'"),
    (2, "команда после разделителя &&", "cd /tmp && claude mcp list"),
    (2, "команда после точки с запятой", "cd /tmp; claude mcp list"),
]


def run(cmd: str, tool: str = "Bash") -> int:
    p = subprocess.run(
        GUARD,
        input=json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}),
        capture_output=True, text=True,
    )
    return p.returncode


def main() -> int:
    bad = 0
    for want, name, cmd in CASES:
        got = run(cmd)
        ok = "ок " if got == want else "ПЛОХО"
        if got != want:
            bad += 1
        print(f"  {ok} ждали={want} получили={got}  {name}")
    # не-Bash и битый вход не должны мешать
    if run("что угодно", tool="Read") != 0:
        print("  ПЛОХО  не-Bash вызов должен проходить"); bad += 1
    p = subprocess.run(GUARD, input="не json", capture_output=True, text=True)
    if p.returncode != 0:
        print("  ПЛОХО  битый вход должен проходить"); bad += 1
    print(f"\nнеудач: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
