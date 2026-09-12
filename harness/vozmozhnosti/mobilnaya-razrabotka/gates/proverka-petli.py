#!/usr/bin/env python3
"""Гейт коммита: правил код приложения — значит петля была, и была недавно.

Зачем гейт, если есть правило и есть скил. Правило «после правки экрана прогони
петлю и посмотри снимок» живёт на внимании модели, то есть до первого забывшего.
След петли (`$LOG_DIR/mobile/последняя-петля.json`) пишет `mobile-loop`, и этот
гейт проверяет его наличие и свежесть — так правило получает носителя уровнем
ниже, чем внимание.

Что гейт проверяет и чего НЕ проверяет — названо честно:
  проверяет: свежий след успешной петли есть, когда в коммите есть код приложения;
  НЕ проверяет: что снимок именно этой правки и что на него действительно
  посмотрели. Свежесть — оценка сверху, а не доказательство. Гейт закрывает
  «забыл прогнать вовсе», а не «прогнал и не глянул»; второе держится записью
  [[feedback-мобильная-петля]] и честностью отчёта (И-4).

Запуск (из pre-commit; список staged-файлов приходит на stdin, по одному в строке):
    python3 гейты/proverka-petli.py < список
    python3 гейты/proverka-petli.py --selftest

Код возврата: 0 — можно коммитить (или проверка неприменима, причина названа),
1 — петли не было или она устарела (адрес назван).
"""
import os
import pathlib
import re
import sys
import time

# Расширения кода приложения. XML и plist сюда НЕ входят: их правят генераторы
# сборки, и требовать на них петлю — ложное срабатывание, а ложный гейт
# снимают вместе со всей защитой.
CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".swift", ".dart", ".vue"}
DEFAULT_MAX_AGE_MIN = 120


def read_conf(path: pathlib.Path) -> dict:
    """Парсер KEY="value" — без source: гейту нельзя исполнять чужой файл."""
    conf = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*([A-Z][A-Z0-9_]*)=("[^"]*"|[^#\s]*)', line)
            if m:
                raw = m.group(2)
                conf[m.group(1)] = raw[1:-1] if raw[:1] == '"' else raw
    except OSError:
        pass
    return conf


def verdict(staged: list[str], conf: dict, repo: pathlib.Path, now: float) -> tuple[int, str]:
    app_dir = conf.get("APP_DIR", "").strip()
    if not app_dir:
        return 0, ("[петля] APP_DIR не задан в конфиге петли — приложения ещё нет, "
                   "проверка неприменима.\n"
                   "        адрес: заполнить APP_DIR/BUILD_CMD/APP_PACKAGE в "
                   "~/.config/harness-mobile.conf первой задачей приложения")
    app_dir = os.path.expandvars(app_dir.replace("$HOME", str(pathlib.Path.home())))
    try:
        rel = pathlib.Path(app_dir).resolve().relative_to(repo.resolve())
    except ValueError:
        return 0, (f"[петля] APP_DIR ({app_dir}) вне этого репозитория — "
                   "код приложения здесь не коммитится, проверка неприменима")
    prefix = str(rel) + os.sep if str(rel) not in (".", "") else ""
    touched = [f for f in staged
               if f.startswith(prefix) and pathlib.Path(f).suffix in CODE_SUFFIXES]
    if not touched:
        return 0, "[петля] кода приложения в коммите нет — проверка неприменима"

    log_dir = conf.get("LOG_DIR") or "/var/log/harness"
    trace = pathlib.Path(log_dir, "mobile", "последняя-петля.json")
    max_age = int(conf.get("LOOP_MAX_AGE_MIN") or DEFAULT_MAX_AGE_MIN)
    files = ", ".join(touched[:3]) + ("…" if len(touched) > 3 else "")
    address = ("        адрес: mobile-loop  (и ПОСМОТРЕТЬ снимок, путь он напечатает)")
    if not trace.is_file():
        return 1, (f"[петля] в коммите код приложения ({files}), а следа петли нет: {trace}\n"
                   + address)
    body = trace.read_text(encoding="utf-8")
    if '"итог": "ок"' not in body and '"итог":"ок"' not in body:
        return 1, (f"[петля] последняя петля НЕ была успешной (след {trace})\n" + address)
    age_min = (now - trace.stat().st_mtime) / 60
    if age_min > max_age:
        return 1, (f"[петля] след петли устарел: {int(age_min)} мин при потолке {max_age} "
                   f"(в коммите {files})\n" + address)
    return 0, f"[петля] след свежий ({int(age_min)} мин назад), код приложения проверен петлёй"


def selftest() -> int:
    import json
    import tempfile
    bad = 0

    def case(name: str, want: int, staged, conf_extra: dict, trace_age_min=None,
             trace_ok=True):
        nonlocal bad
        with tempfile.TemporaryDirectory() as t:
            repo = pathlib.Path(t, "repo", "app")
            repo.mkdir(parents=True)
            conf = {"LOG_DIR": str(pathlib.Path(t, "log")), "APP_DIR": str(repo)}
            conf.update(conf_extra)
            if trace_age_min is not None:
                trace = pathlib.Path(conf["LOG_DIR"], "mobile", "последняя-петля.json")
                trace.parent.mkdir(parents=True, exist_ok=True)
                trace.write_text(json.dumps({"итог": "ок" if trace_ok else "встала"},
                                            ensure_ascii=False), encoding="utf-8")
                stamp = time.time() - trace_age_min * 60
                os.utime(trace, (stamp, stamp))
            got, msg = verdict(staged, conf, pathlib.Path(t, "repo"), time.time())
            mark = "ок " if got == want else "ПЛОХО"
            if got != want:
                bad += 1
            print(f"  {mark} ждали={want} получили={got}  {name}")

    # больные случаи
    case("нет следа вовсе → красный", 1, ["app/Экран.tsx"], {})
    case("след устарел (5 часов) → красный", 1, ["app/Экран.tsx"], {}, trace_age_min=300)
    case("петля встала → красный", 1, ["app/Экран.tsx"], {}, trace_age_min=5, trace_ok=False)
    # здоровые случаи
    case("свежий след → зелёный", 0, ["app/Экран.tsx"], {}, trace_age_min=5)
    case("кода приложения нет → зелёный", 0, ["README.md", "dev-map.yaml"], {},
         trace_age_min=None)
    case("правки вне кода (xml) → зелёный", 0, ["app/AndroidManifest.xml"], {})
    case("APP_DIR пуст → зелёный с честной строкой", 0, ["app/Экран.tsx"], {"APP_DIR": ""})
    case("потолок из конфига (240) переживает 300 мин? нет → красный", 1,
         ["app/Экран.tsx"], {"LOOP_MAX_AGE_MIN": "240"}, trace_age_min=300)
    case("потолок из конфига (480) → зелёный", 0,
         ["app/Экран.tsx"], {"LOOP_MAX_AGE_MIN": "480"}, trace_age_min=300)
    print("SELFTEST: зелёный (9 путей)" if not bad else f"SELFTEST: неудач {bad}")
    return 1 if bad else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    conf_path = pathlib.Path(os.environ.get(
        "HARNESS_MOBILE_CONF", pathlib.Path.home() / ".config" / "harness-mobile.conf"))
    if not conf_path.is_file():
        print(f"[петля] нет {conf_path} — возможность «мобильная-разработка» "
              "не установлена, проверка неприменима")
        return 0
    staged = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    code, message = verdict(staged, read_conf(conf_path), pathlib.Path.cwd(), time.time())
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
