#!/usr/bin/env python3
"""Сборка ~/.config/harness-mobile.conf из паспорта возможности.

Отдельным файлом, а не heredoc-ом внутри установщика: значения паспорта бывают
многострочными (FTL_DEVICES — список моделей), и подстановка их в текст
программы рвала программу пополам («unterminated string literal»). Значения
приходят АРГУМЕНТАМИ — тогда содержимое значения не может испортить код.

Живые ключи приложения (APP_DIR / BUILD_CMD / APP_PACKAGE / MAESTRO_FLOWS_DIR /
SHOTS_DIR) заполняются первой задачей продукта и переустановкой НЕ затираются:
переустановка обновляет пороги, а не чужие данные.

Запуск: sobrat-konfig.py <конфиг> <ANDROID_HOME> <AVD> <МБ> <Xmx> <таймаут>
                          <устройства> <LOG_DIR> <SECRETS_DIR> <лок>
"""
import pathlib
import re
import sys

LIVE_KEYS = ("APP_DIR", "BUILD_CMD", "APP_PACKAGE", "MAESTRO_FLOWS_DIR", "SHOTS_DIR")
LIVE_DEFAULTS = {"SHOTS_DIR": '"/tmp/shots"'}


def quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def main() -> None:
    if len(sys.argv) != 11:
        sys.exit("sobrat-konfig.py: ждал 10 аргументов, получил %d" % (len(sys.argv) - 1))
    (conf, android, avd, mem, gradle_mem, ftl_timeout, ftl_devices,
     log_dir, secrets_dir, service_lock) = sys.argv[1:11]
    path = pathlib.Path(conf)
    live = {}
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        for key in LIVE_KEYS:
            found = re.search(r"^%s=(.*)$" % key, text, re.M)
            if found:
                live[key] = found.group(1)
    lines = [
        "# harness-mobile.conf — данные петли мобильной разработки. ДАННЫЕ, НЕ КОД.",
        "# Собран sobrat-konfig.py из МАНИФЕСТ.conf возможности. Порог менять ТАМ и",
        "# переустанавливать возможность; ключи APP_* — живые данные проекта.",
        "ANDROID_HOME=" + quote(android),
        "AVD_NAME=" + quote(avd),
        "EMULATOR_MEM_MB=" + mem,
        "EMU_BOOT_TIMEOUT_SEC=300",
        'EMU_TMUX_SESSION="emulator"',
        "GRADLE_MAX_MEM=" + quote(gradle_mem),
        "FTL_TIMEOUT=" + quote(ftl_timeout),
        "FTL_DEVICES=" + quote(ftl_devices),
        "LOG_DIR=" + quote(log_dir),
        "SECRETS_DIR=" + quote(secrets_dir),
        "SERVICE_LOCK=" + quote(service_lock),
        'SERVICE_LOCKS_SUBDIR="services"',
        'MAESTRO_BIN="$HOME/.maestro/bin/maestro"',
        "",
        "# ── Приложение: пусто до первой задачи (пустой ключ = честный отказ петли) ──",
    ]
    for key in LIVE_KEYS:
        lines.append(key + "=" + live.get(key, LIVE_DEFAULTS.get(key, '""')))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  конфиг собран; сохранённых живых ключей: %d" % len(live))


if __name__ == "__main__":
    main()
