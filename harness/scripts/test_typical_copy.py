#!/usr/bin/env python3
"""Проба замка «типовая чистая копия» (check-typical-copy.py): стенд видит то, чего не видит наша машина.

Стенд ставит новый выпуск так, как ставит копия: процессом `update.py --поставить` прежнего
выпуска (он передаёт постановку обновлятору нового), из самой старой версии, что стоит у копий
(страницы серверы/<копия>.md сети эволюции, поле «версия_харнеса»).

Три стенда:
  чистый      — сборка — git с историей выпусков: древнейший (v0.8.0) без раскладчика,
                старый (v0.9.1, «пакет abcdef1») и прошлый — с обновлятором до перезапуска
                (как 38c4ff1 у нескольких копий сети: нет HARNESS_UPDATE_RELAUNCHED, откатывает любая
                новая красная); у копий стоят все три. Стенд обязан назвать древнейший
                неставящимся и взять старый, не прошлый. Такой копии агент харнеса шлёт
                команду перехода — стенд ставит ею (КОМАНДА_ПЕРЕХОДА нового выпуска) и
                называет это; постановку и самопроверку ведёт обновлятор нового выпуска,
                данные копии не откатывают. Код 0, update.py назвал «поставлено»; свой код копии (коммит
                проекта, строка crontab, свой .sh) цел; данные копии (пароль в своём коде,
                указание владельца) названы строками; главные проверки отката
                (update-rollback.yaml нового выпуска) — в выборке проверок;
  мутация     — версий копий не узнано → старое — прошлый выпуск; в новом выпуске изоляция
                tmux судит копию как НАШУ машину (не главная: update.py с update-rollback.yaml
                её ставит с карантином, замок — отказывает) и сломана «маска секретов»
                (главная), обновлятор нового выпуска отличается — постановку ведёт он
                (перезапуск прежнего): update.py откатывает — код 1, «не поставил: самопроверка не
                прошла», новые красные «изоляция tmux» (свой .sh в причине) и «маска секретов»;
  карантин    — в новом выпуске красны «признаки», у нас под карантином: стенд судит их, код 1.
И вторая типовая копия — с продуктом-игрушкой: `deploy.sh --план` выходит нулём и называет
цепочку; `--проверка` идёт, когда docker и образ alpine:3.20 есть, иначе «пропущено».

Какая поломка оставит пробу зелёной: судья, что берёт `--старое` или версии не по старшинству,
— строка «старое — …» называет не «старая»; судья, что не ставит выпуск на деле, — древнейший
не назван неставящимся; судья, что раскладывает новый
выпуск сам, мимо update.py, — нет строк «[обновление] постановку ведёт…» и «поставлено»;
судья, что ставит старым обновлятором без перезапуска, — тот откатывает на данных копии
(«не поставил»), строки о команде перехода нет;
судья без строки crontab или коммита копии — «свой код копии» красный; судья, что
не смотрит итог update.py, — нет «не поставил: самопроверка не прошла».
Дверь живости: TYPICAL_COPY_JUDGE — путь судьи (мутированная КОПИЯ).

Прогон (только фоном, десятки минут: стенды обновления):
    python3 scripts/test_typical_copy.py
Последняя строка: `ТИПОВАЯ КОПИЯ: путей N, неудач F`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402

JUDGE = Path(os.environ.get("TYPICAL_COPY_JUDGE") or HERE / "check-typical-copy.py")
MUTATED = "harness/scripts/check-tmux-isolation.py"
SIGNALS = "harness/scripts/check-signals.py"
MASK = "harness/scripts/test_check_secrets_mask.sh"  # «маска секретов» — главная проверка отката
UPDATER = "harness/scripts/update.py"
LAYOUT = "harness/razlozhit-harnes.sh"
ROLLBACK = "harness/config/update-rollback.yaml"
BRIDGE = "типовая копия: обновлятор копии без перезапуска — ставит команда перехода нового выпуска"
# Обновлятор до перезапуска и до главных проверок (как 38c4ff1): сам ведёт постановку,
# откатывает любая новая красная.
PRE_RELAUNCH = (("        _перезапуститься_новой_версией(argv)\n", ""),
                ("HARNESS_UPDATE_RELAUNCHED", "HARNESS_UPDATE_NONE"),
                ("    главные = главные_проверки()\n    новые, прочие = {}, {}\n",
                 "    главные = None\n    новые, прочие = {}, {}\n"))
TOY = "типовая копия с продуктом-игрушкой: "
TOY_BASE = "alpine:3.20"
paths = failures = 0


def report(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += not ok
    print(("ок    · " if ok else "КРАСН · ") + text + ("" if ok else f": {detail}"), flush=True)


def judged(old: Path, new: Path, quarantine: Path, servers: Path, toy_image: str = "") -> tuple:
    env = {**os.environ, "TYPICAL_COPY_TOY_IMAGE": toy_image}
    done = subprocess.run([sys.executable, str(JUDGE), "--старое", str(old), "--новое", str(new),
                           "--карантин", str(quarantine), "--копии", str(servers)], env=env,
                          capture_output=True, text=True, timeout=7200, stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout + done.stderr).strip()


def docker_has(image: str) -> bool:
    """Есть ли docker и образ у этой машины — тот же вопрос, что задаёт судья."""
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                          timeout=60, stdin=subprocess.DEVNULL).returncode == 0


def mutate(tree: Path, rel: str, before: str, after: str) -> bool:
    target = tree / rel
    text = target.read_text(encoding="utf-8")
    if before not in text:
        report(False, f"мутация не встала: строки нет в {rel}", before.strip()[:80])
        return False
    target.write_text(text.replace(before, after, 1), encoding="utf-8")
    return True


def pre_relaunch(tree: Path) -> None:
    """Обновлятор дерева — до перезапуска и главных проверок; якоря нет — проба слепа."""
    target = tree / UPDATER
    text = target.read_text(encoding="utf-8")
    for before, after in PRE_RELAUNCH:
        if before not in text:
            raise RuntimeError(f"обновлятор до перезапуска не собран: якоря нет в {UPDATER}: {before.strip()[:60]}")
        text = text.replace(before, after)
    target.write_text(text, encoding="utf-8")


def git(where: Path, *args, date: str = "") -> str:
    env = {**os.environ, **({"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {})}
    return subprocess.run(["git", "-C", str(where), "-c", "user.name=proba", "-c", "user.email=proba@stand",
                           *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def fill(repo: Path, tree: Path) -> None:
    for item in repo.iterdir():
        if item.name != ".git":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    shutil.copytree(tree, repo, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))


def release_history(work: Path, clean: Path) -> tuple:
    """Сборка с историей выпусков → (сборка, прошлый выпуск деревом, серверы копий).

    Древнейший выпуск (v0.8.0) — без раскладчика: стенд его с нуля не ставит. Старый (v0.9.1,
    «пакет abcdef1») и прошлый — чистые. У копий стоят все три и одна неузнаваемая версия."""
    build = work / "сборка"
    build.mkdir()
    git(build, "init", "-q", "-b", "master")
    fill(build, clean)
    (build / LAYOUT).unlink()
    git(build, "add", "-A")
    git(build, "commit", "-q", "-m", "версия от 2026-10-20, пакет 0a0a0a0", date="2026-10-20T10:00:00")
    git(build, "tag", "v0.8.0")
    fill(build, clean)
    pre_relaunch(build)
    git(build, "add", "-A")
    git(build, "commit", "-q", "--allow-empty", "-m", "версия от 2026-11-01, пакет abcdef1",
        date="2026-11-01T10:00:00")
    git(build, "tag", "v0.9.1")
    (build / "ЧТО-НОВОГО-ПРОБЫ.md").write_text("прошлый выпуск\n", encoding="utf-8")
    git(build, "add", "-A")
    git(build, "commit", "-q", "-m", "версия от 2026-11-10, пакет 1234567", date="2026-11-10T10:00:00")
    last = git(build, "rev-parse", "--short=7", "HEAD")
    previous = work / "прошлый-выпуск"
    shutil.copytree(build, previous, ignore=shutil.ignore_patterns(".git"))
    fill(build, clean)
    updater = build / UPDATER
    updater.write_text(updater.read_text(encoding="utf-8") + "\n# проба: обновлятор нового выпуска\n",
                       encoding="utf-8")
    (build / ROLLBACK).write_text('главные:\n  - имя: "сторож команд"\n    почему: "проба"\n', encoding="utf-8")
    servers = work / "серверы"
    servers.mkdir()
    for name, version in (("древняя", "0.8.0"), ("старая", "0.9.1 (пакет abcdef1)"),
                          ("прошлая", f"пакет {last} (17.11.2026)"),
                          ("своя", "проект ad6abe5 (sha пакета не записан)")):
        (servers / f"{name}.md").write_text(f"---\nсервер: {name}\nверсия_харнеса: {version}\n---\n\nтекст\n",
                                            encoding="utf-8")
    (servers / "хозяин.md").write_text("---\nсервер: хозяин\nроль: хозяин\n---\n", encoding="utf-8")
    return build, previous, servers


def main() -> int:
    package = config.katalog_paketa()
    work = Path(tempfile.mkdtemp(prefix="proba-typical-copy-trees-"))
    try:
        clean = work / "выпуск"
        shutil.copytree(package, clean, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        no_quarantine = work / "карантин-пуст.yaml"
        no_quarantine.write_text("карантин: []\n", encoding="utf-8")
        no_servers = work / "серверов-нет"
        no_servers.mkdir()
        build, previous, servers = release_history(work, clean)
        rc, said = judged(previous, build, no_quarantine, servers)
        report(rc == 0 and "новых красных нет" in said, "чистый выпуск → новых красных нет, код 0",
               f"код {rc}: {said[-400:]}")
        report("с версии копии «древняя»" in said and "стенд не ставится" in said
               and "старое — самая старая из ставящихся версий у копий: старая" in said,
               "(а) старое — самая старая версия у копий (не ставящаяся названа, взята следующая), "
               "не прошлый выпуск", said[-400:])
        report("версия копии «своя» не узнана" in said,
               "(а) версия, которой нет в истории выпусков, названа, а не выдумана", said[-400:])
        report(BRIDGE in said and "[обновление] поставлено:" in said,
               "(б) обновлятор копии без перезапуска → ставит команда перехода, обновлятор нового — поставлено",
               said[-400:])
        report("свой код копии цел: коммит проекта" in said and "crontab" in said,
               "(в) свой код копии (коммит, crontab, свой .sh) на стенде и цел после обновления",
               said[-400:])
        report("сторож команд" in said and "изоляция tmux" in said,
               "(в) выборка: проверки предмета копии и главные из update-rollback.yaml", said[-400:])
        report("красная в данных копии «указания»" in said and "1794700000.3" in said,
               "данные копии: указание владельца за часы обновления названо, а не отказом", said[-400:])
        report("красная в данных копии «секреты»" in said and "app/settings.py" in said,
               "данные копии: пароль в своём коде продукта назван, а не отказом", said[-400:])
        report(f"{TOY}--план код 0, цепочка названа" in said,
               "игрушка: deploy.sh --план в стенде → код 0, цепочка названа", said[-400:])
        if docker_has(TOY_BASE):
            report(f"{TOY}--проверка: тесты success" in said,
                   f"игрушка: docker и {TOY_BASE} есть → --проверка прошла, тесты success", said[-400:])
        else:
            report(f"{TOY}--проверка пропущено: нет docker/образа" in said,
                   f"игрушка: docker или {TOY_BASE} нет → --проверка пропущена словами", said[-400:])
        mutated = work / "мутация"
        shutil.copytree(clean, mutated)
        (mutated / UPDATER).write_text((mutated / UPDATER).read_text(encoding="utf-8")
                                       + "\n# проба: обновлятор нового выпуска\n", encoding="utf-8")
        if not (mutated / ROLLBACK).is_file():   # выпуск без главных: откатывает любая новая красная
            (mutated / ROLLBACK).write_text('главные:\n  - имя: "маска секретов"\n', encoding="utf-8")
        if mutate(mutated, MUTATED,
                  '    return config.конфиг(config.путь_harness_conf()).get("HARNESS_DEV_GATES") == "1"\n',
                  "    return True\n") and mutate(mutated, MASK, "#!/usr/bin/env bash\n",
                                              '#!/usr/bin/env bash\necho "[маска] мутация пробы: маска снята"\nexit 1\n'):
            rc, said = judged(clean, mutated, no_quarantine, no_servers, toy_image="alpine:0-typical-absent")
            report("[обновление] постановку ведёт обновлятор новой версии" in said and BRIDGE not in said,
                   "(б) обновлятор копии с перезапуском → update.py --поставить прежнего передаёт новому",
                   said[-400:])
            report("старое — прошлый выпуск" in said,
                   "версий копий не узнано → старое — прошлый выпуск, путь тот же", said[-400:])
            report(f"{TOY}--проверка пропущено: нет docker/образа alpine:0-typical-absent" in said,
                   "игрушка без образа базы → --проверка «пропущено: нет docker/образа», не отказ",
                   said[-400:])
            report(rc == 1 and "новая красная «изоляция tmux»" in said and "typical-preview.sh" in said,
                   "мутация «судит как нашу машину» → код 1, «изоляция tmux» и свой файл копии в причине",
                   f"код {rc}: {said[-400:]}")
            report("update.py --поставить не поставил: самопроверка не прошла" in said
                   and "новая красная «маска секретов»" in said and "маска снята" in said,
                   "(г) откат update.py на стенде (главная «маска секретов») = отказ замка с причиной", said[-400:])
        held = work / "под-карантином"
        shutil.copytree(clean, held)
        quarantine = work / "карантин.yaml"
        quarantine.write_text('карантин:\n  - гейт: "признаки"\n    карточка: hr.проба\n', encoding="utf-8")
        if mutate(held, SIGNALS, "    sys.exit(главная(sys.argv[1:]))\n",
                  '    print("[признаки] мутация пробы: судят без источника")\n    sys.exit(1)\n'):
            rc, said = judged(clean, held, quarantine, no_servers)
            report(rc == 1 and "новая красная «признаки»" in said and "мутация пробы" in said,
                   "красная под нашим карантином → стенд судит её, код 1", f"код {rc}: {said[-400:]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"ТИПОВАЯ КОПИЯ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
