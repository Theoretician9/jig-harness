#!/usr/bin/env python3
"""Пробы гейта «ключи образца harness.conf»: ключ, который читает код, есть в образце.

Откуда. Разбор правок соседней установки (18.10.2026, пункт 2.2): в живом
`/etc/harness/harness.conf` 18 ключей, которых нет в образце
`harness/config/harness.conf`, и код читает их почти все (`DEMON_MODEL`,
`PUBLIC_REMOTE`, `DEPLOY_CONSENT_STANDING`, `PULSE_OWNER_MIN` …). Ключ,
вписанный руками в живой конфиг, до новой установки не доезжает никогда:
установщик кладёт ОБРАЗЕЦ. Тот же разрыв у доустановки: `system-jobs.yaml`
(`ключи:`) доносит ключ при обновлении, а чистая установка его не получает;
`снять_ключи:` снимает ключ у всех, а образец продолжает его раздавать.

Почему не «все имена из кода»: замер 18.10 — синтаксический поиск `${X:-…}` и
`.get("X")` по коду даёт 202 имени вне образца (локальные переменные, двери
проб, ключи паспорта). Гейт, краснеющий на двухстах, выключат. Поэтому первое
правило судит только имена, которые ЖИВОЙ конфиг уже объявил ключами.

Гейт (его пишет шаг «код»): `scripts/check-sample-config-keys.py`
    --sample <образец> --live <живой> --jobs <system-jobs.yaml> --code-root <корень>
    rc 0 — чисто; rc 1 — находки, по строке: имя ключа и слово причины;
    rc 2 — названный явно файл не прочитан (молча зеленеть нельзя).
    Правила: (1) ключ живого конфига, который читает код (имя в строке кода,
    не в комментарии), есть в образце — строкой `KEY=` или закомментированной
    `# KEY=` (дверь: ключ описан, умолчание держит код) → иначе «нет в образце»;
    (2) каждый ключ из `ключи:` стоит в образце НЕзакомментированным →
    иначе «нет в образце»; (3) ключ из `снять_ключи:` в образце не стоит →
    иначе «снят».
Дверь пробы: SAMPLE_KEYS_GATE — путь копии гейта (мутации); HARNESS_STARTER —
каталог пакета, чей образец судят случаи раздела «образец пакета».

Прогон: python3 scripts/test_sample_config_keys.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

# Проба живёт в двух раскладках (пакет: harness/scripts/, проект: scripts/):
# корень ищется вверх по дереву, гейт — сосед этого файла.
ROOT = config.корень_дерева(__file__)
GATE = Path(os.environ.get("SAMPLE_KEYS_GATE") or Path(__file__).resolve().parent / "check-sample-config-keys.py")
SAMPLE = ROOT / "harness" / "config" / "harness.conf"
JOBS = ROOT / "harness" / "config" / "system-jobs.yaml"

paths = 0
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name}" + (f": {detail}" if detail else ""))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def stand(base: Path, sample: str, live: str, jobs: str, code: dict) -> list[str]:
    write(base / "sample.conf", sample)
    write(base / "live.conf", live)
    write(base / "jobs.yaml", jobs)
    for rel, text in code.items():
        write(base / "tree" / rel, text)
    return ["--sample", str(base / "sample.conf"), "--live", str(base / "live.conf"),
            "--jobs", str(base / "jobs.yaml"), "--code-root", str(base / "tree")]


HEALTHY_SAMPLE = 'LOG_KEEP_DAYS="14"\nPULSE_KEY="60"\n'
HEALTHY_LIVE = 'LOG_KEEP_DAYS="30"\nPULSE_KEY="60"\n'
HEALTHY_JOBS = 'ключи:\n  PULSE_KEY: "60"\nснять_ключи:\n  - GONE_KEY\n'
HEALTHY_CODE = {
    "harness/demons/warden.sh": '#!/usr/bin/env bash\n[ "$s" -ge "${PULSE_KEY:-60}" ]\n',
    "scripts/keep.py": 'keep = int(conf.get("LOG_KEEP_DAYS") or 14)\n',
}


def run_gate(args: list[str]) -> tuple[int, str]:
    done = subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True, timeout=300)
    return done.returncode, done.stdout + done.stderr


def finding(out: str, key: str, word: str) -> bool:
    return any(re.search(rf"(?<![A-Z0-9_]){key}(?![A-Z0-9_])", line) and word in line
               for line in out.splitlines())


def stand_cases() -> None:
    if not GATE.is_file():
        check("гейт ключей образца существует", False, f"механизма нет: {GATE} (его пишет шаг «код»)")
        return

    def case(sample=HEALTHY_SAMPLE, live=HEALTHY_LIVE, jobs=HEALTHY_JOBS, extra=None):
        code = dict(HEALTHY_CODE, **(extra or {}))
        with tempfile.TemporaryDirectory() as tmp:
            return run_gate(stand(Path(tmp), sample, live, jobs, code))

    rc, out = case()
    check("здоровый стенд — rc 0", rc == 0, f"rc {rc}: {out.strip()[:300]}")
    # Пустая строка на зелёном неотличима от «судить было нечем» (без живого
    # конфига гейт не судит ни одного ключа). Числа — из стенда.
    check("здоровый: приговор называет число судимого",
          out.strip() == "чисто: ключей живого конфига 2, ключей доустановки 1, снятых ключей 1",
          repr(out.strip()[:300]))

    # БОЛЬНОЙ 2.2, живой случай: ключ вписан руками в живой конфиг, демон
    # его читает, образец о нём не знает.
    rc, out = case(live=HEALTHY_LIVE + 'ALARM_WATCH_SEC="300"\n',
                   extra={"harness/demons/rotor.sh": '#!/usr/bin/env bash\nw="${ALARM_WATCH_SEC:-300}"\n'})
    check("БОЛЬНОЙ: ключ живого конфига читает shell-демон, в образце нет — rc 1", rc == 1, f"rc {rc}")
    check("БОЛЬНОЙ: назван ALARM_WATCH_SEC и «нет в образце»",
          finding(out, "ALARM_WATCH_SEC", "нет в образце"), out.strip()[:300])

    rc, out = case(live=HEALTHY_LIVE + 'CALLS_PER_DAY="50"\n',
                   extra={"scripts/ask.py": 'limit = int(conf.get("CALLS_PER_DAY") or 50)\n'})
    check("БОЛЬНОЙ: ключ живого конфига читает питон, в образце нет — назван",
          rc == 1 and finding(out, "CALLS_PER_DAY", "нет в образце"), f"rc {rc}: {out.strip()[:300]}")

    # Ключ живого конфига, имя которого стоит только в комментарии, — код его
    # не читает: остаток машины, а не дыра образца.
    rc, out = case(live=HEALTHY_LIVE + 'OLD_KNOB="1"\n',
                   extra={"scripts/note.sh": '#!/usr/bin/env bash\n# OLD_KNOB когда-то включал ночной прогон\necho\n'})
    check("здоровый: ключ живого конфига только в комментарии кода — не находка",
          rc == 0 and "OLD_KNOB" not in out, f"rc {rc}: {out.strip()[:300]}")

    # Дверь: ключ описан в образце закомментированной строкой — умолчание держит код.
    rc, out = case(sample=HEALTHY_SAMPLE + '# REMOTE_NAME=""   # имя удалённого; пусто — нет публикации\n',
                   live=HEALTHY_LIVE + 'REMOTE_NAME="jig"\n',
                   extra={"scripts/pub.py": 'remote = conf.get("REMOTE_NAME", "")\n'})
    check("здоровый: ключ описан в образце закомментированным — не находка", rc == 0, f"rc {rc}: {out.strip()[:300]}")

    # БОЛЬНОЙ: доустановка доносит ключ, образец — нет (MODEL_CALLS_PER_DAY,
    # PANEL_TUNNEL, PULSE_OWNER_MIN 18.10).
    rc, out = case(jobs=HEALTHY_JOBS.replace('ключи:\n', 'ключи:\n  TUNNEL_ON: "1"\n', 1))
    check("БОЛЬНОЙ: ключ из «ключи:» доустановки, которого нет в образце — назван",
          rc == 1 and finding(out, "TUNNEL_ON", "нет в образце"), f"rc {rc}: {out.strip()[:300]}")

    # Закомментированная строка для «ключи:» не годится: чистая установка
    # получит ключ без значения, а обновлённая — со значением.
    rc, out = case(sample=HEALTHY_SAMPLE + '# TUNNEL_ON="1"\n',
                   jobs=HEALTHY_JOBS.replace('ключи:\n', 'ключи:\n  TUNNEL_ON: "1"\n', 1))
    check("БОЛЬНОЙ: ключ «ключи:» в образце только комментарием — всё равно находка",
          rc == 1 and finding(out, "TUNNEL_ON", "нет в образце"), f"rc {rc}: {out.strip()[:300]}")

    # БОЛЬНОЙ: снятый ключ образец продолжает раздавать (HYGIENE_BACKLOG_FILES 18.10).
    rc, out = case(sample=HEALTHY_SAMPLE + 'GONE_KEY="x"\n')
    check("БОЛЬНОЙ: ключ из «снять_ключи:» стоит в образце — назван со словом «снят»",
          rc == 1 and finding(out, "GONE_KEY", "снят"), f"rc {rc}: {out.strip()[:300]}")

    # Названный явно живой конфиг не прочитан — это не «ключей нет».
    with tempfile.TemporaryDirectory() as tmp:
        args = stand(Path(tmp), HEALTHY_SAMPLE, HEALTHY_LIVE, HEALTHY_JOBS, HEALTHY_CODE)
        args[args.index("--live") + 1] = str(Path(tmp) / "nowhere.conf")
        rc, out = run_gate(args)
    check("БОЛЬНОЙ: явно названного живого конфига нет — rc 2, а не зелёный", rc == 2, f"rc {rc}: {out.strip()[:300]}")


def sample_keys(text: str) -> set[str]:
    """Действующие ключи образца — общим разборщиком паспорта (одна дверь на харнес)."""
    return set(config.из_текста(text))


def live_cases() -> None:
    import yaml
    sample = sample_keys(SAMPLE.read_text(encoding="utf-8"))
    check("ЖИВОЙ: в образце есть ключи (иначе случаи ниже пусты)", len(sample) > 50, f"{len(sample)}")
    jobs = yaml.safe_load(JOBS.read_text(encoding="utf-8")) or {}
    added = set((jobs.get("ключи") or {}).keys())
    removed = set(jobs.get("снять_ключи") or [])
    check("ЖИВОЙ: «ключи:» доустановки не пусты", bool(added))
    check("ЖИВОЙ 2.2: каждый ключ «ключи:» доустановки стоит в образце",
          added <= sample, " ".join(sorted(added - sample)))
    check("ЖИВОЙ 2.2: ни один ключ «снять_ключи:» не стоит в образце",
          not (removed & sample), " ".join(sorted(removed & sample)))
    # Стык с группой Г (3.4): stop_reminder.sh будет читать список путей,
    # которые пишут демоны. Ключ заводится ВМЕСТЕ с читателем — и в образце,
    # и в живом конфиге; без него чистая установка напоминает о чужих файлах.
    check("ЖИВОЙ 3.4: ключ DAEMON_OWNED_PATHS стоит в образце", "DAEMON_OWNED_PATHS" in sample)
    if GATE.is_file():
        rc, out = run_gate(["--sample", str(SAMPLE), "--jobs", str(JOBS), "--code-root", str(ROOT)])
        check("ЖИВОЙ: гейт на настоящих образце, живом конфиге и коде — rc 0", rc == 0,
              f"rc {rc}:\n{out.strip()[:1500]}")



# deploy.sh пакета читает три ключа стека, и образец обязан их описывать:
# потребитель получает образец, а не живой конфиг. Судится образец ПАКЕТА:
# образцы проекта и пакета у нас совпадают, и проба на проектном зеленела бы
# там, где они разошлись.
STACK_KEYS = ("STACK_RELEASE_CMD", "STACK_HEALTH_CMD", "STACK_DB_RESTORE_HINT")


def package_root() -> Path:
    """Пакет-источник (дверь HARNESS_STARTER); в самом пакете это ROOT."""
    named = config.katalog_paketa()
    return named if (named / "harness" / "config" / "harness.conf").is_file() else ROOT


def package_cases() -> None:
    pkg = package_root()
    sample_path = pkg / "harness" / "config" / "harness.conf"
    deploy = pkg / "harness" / "scripts" / "deploy.sh"
    check(f"в пакете есть образец и deploy.sh ({pkg})", sample_path.is_file() and deploy.is_file())
    if not (sample_path.is_file() and deploy.is_file()):
        return
    # Без читателя случаи ниже пусты: «ключ читает код» — их предпосылка.
    code_lines = [l for l in deploy.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]
    unread = [k for k in STACK_KEYS if not any(re.search(rf"(?<![A-Z0-9_]){k}(?![A-Z0-9_])", l) for l in code_lines)]
    check("deploy.sh пакета читает все три ключа стека (предпосылка)", not unread, " ".join(unread))

    sample = sample_path.read_text(encoding="utf-8")
    declared = set(config.из_текста(sample)) | set(re.findall(r"^\s*#\s*([A-Z][A-Z0-9_]*)=", sample, re.MULTILINE))
    missing = [k for k in STACK_KEYS if k not in declared]
    check("образец пакета описывает каждый ключ стека строкой KEY= или # KEY=",
          not missing, f"нет в {sample_path}: {' '.join(missing)}")

    if not GATE.is_file():
        return
    # Живой конфиг потребителя объявляет ключи стека (там deploy.sh ими
    # пользуются) — свой файл стенда: живой /etc у нас их не знает, и гейт
    # с ним молчал бы о любом образце.
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "live.conf"
        write(live, "".join(f'{k}="x"\n' for k in STACK_KEYS))
        stripped = Path(tmp) / "stripped.conf"
        write(stripped, "".join(l + "\n" for l in sample.splitlines()
                                if not any(re.search(rf"^\s*#?\s*{k}=", l) for k in STACK_KEYS)))
        base = ["--live", str(live), "--jobs", str(pkg / "harness" / "config" / "system-jobs.yaml"),
                "--code-root", str(pkg)]
        # Контроль стенда: образец без строк стека гейт обязан назвать — иначе
        # «не находит» значило бы «не видит deploy.sh», а не «образец полон».
        rc, out = run_gate(["--sample", str(stripped), *base])
        blind = [k for k in STACK_KEYS if not finding(out, k, "нет в образце")]
        check("контроль: образец без ключей стека — гейт называет все три", rc == 1 and not blind,
              f"rc {rc}, не назвал: {' '.join(blind)}; {out.strip()[:300]}")
        rc, out = run_gate(["--sample", str(sample_path), *base])
        named = [k for k in STACK_KEYS if finding(out, k, "нет в образце")]
        check("гейт на образце ПАКЕТА не называет ключи стека «нет в образце»",
              rc in (0, 1) and not named, f"rc {rc}, названы: {' '.join(named)}")


def main() -> int:
    print("[стенды]")
    stand_cases()
    print("[живые данные]")
    live_cases()
    print("[образец пакета]")
    package_cases()
    if failures:
        print(f"SELFTEST: КРАСНЫЙ ({failures} из {paths} путей)")
        return 1
    print(f"SELFTEST: зелёный ({paths} путей; первый больной — 18 ключей живого конфига вне образца, 18.10)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
