#!/usr/bin/env python3
"""Проба замка «типовая чистая копия» (check-typical-copy.py): стенд видит то, чего не видит наша машина.

Три стенда одного выпуска пакета:
  чистый      — прежний и новый выпуск одинаковы → новых красных харнеса нет, код 0; данные
                копии на стенде (пароль в своём коде, указание владельца за часы
                обновления) названы строками «красная в данных копии» — «секреты» и «указания»;
  мутация     — в новом выпуске изоляция tmux судит копию как НАШУ машину
                (dev_install() всегда «да», как при HARNESS_DEV_GATES=1): свой .sh
                копии краснит — код 1, в выводе «новая красная «изоляция tmux»» и причина;
  карантин    — в новом выпуске красны «признаки», у нас они под карантином: стенд судит
                их тоже — код 1 (0.13.0 ушёл к копиям с красными «признаками» под карантином).
И вторая типовая копия — с продуктом-игрушкой (эталонный выкат §6.1): Dockerfile на
alpine:3.20, файл проверок с одним набором, ручка здоровья командой. На чистом стенде её
`deploy.sh --план` выходит нулём и называет цепочку; `--проверка` идёт, когда docker и
образ alpine:3.20 есть (набор success), иначе — «пропущено: нет docker/образа». На стенде
мутации база игрушки — образ, которого нет (дверь TYPICAL_COPY_TOY_IMAGE): пропуск словами.
Какая поломка оставит пробу зелёной: стенд, который берёт наше окружение (HARNESS_DEV_GATES
из живого конфига), не заводит свой файл копии или данные проекта, не читает карантин, —
на чистом стенде мутация не видна; судья, который печатает строки игрушки, не собрав её, —
ловит пропуск при отсутствующем образе (строка --проверка обязана зависеть от среды).

Прогон (только фоном, минуты: четыре раскладки):
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

JUDGE = HERE / "check-typical-copy.py"
MUTATED = "harness/scripts/check-tmux-isolation.py"
SIGNALS = "harness/scripts/check-signals.py"
TOY = "типовая копия с продуктом-игрушкой: "
TOY_BASE = "alpine:3.20"
paths = failures = 0


def report(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += not ok
    print(("ок    · " if ok else "КРАСН · ") + text + ("" if ok else f": {detail}"))


def judged(old: Path, new: Path, quarantine: Path, toy_image: str = "") -> tuple:
    env = {**os.environ, "TYPICAL_COPY_TOY_IMAGE": toy_image}
    done = subprocess.run([sys.executable, str(JUDGE), "--старое", str(old), "--новое", str(new),
                           "--карантин", str(quarantine)], env=env,
                          capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
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


def main() -> int:
    package = config.katalog_paketa()
    work = Path(tempfile.mkdtemp(prefix="proba-typical-copy-trees-"))
    try:
        clean = work / "выпуск"
        shutil.copytree(package, clean, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        no_quarantine = work / "карантин-пуст.yaml"
        no_quarantine.write_text("карантин: []\n", encoding="utf-8")
        rc, said = judged(clean, clean, no_quarantine)
        report(rc == 0 and "новых красных нет" in said, "чистый выпуск → новых красных нет, код 0",
               f"код {rc}: {said[-300:]}")
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
        if mutate(mutated, MUTATED,
                  '    return config.конфиг(config.путь_harness_conf()).get("HARNESS_DEV_GATES") == "1"\n',
                  "    return True\n"):
            rc, said = judged(clean, mutated, no_quarantine, toy_image="alpine:0-typical-absent")
            report(f"{TOY}--проверка пропущено: нет docker/образа alpine:0-typical-absent" in said,
                   "игрушка без образа базы → --проверка «пропущено: нет docker/образа», не отказ",
                   said[-400:])
            report(rc == 1 and "новая красная «изоляция tmux»" in said and "typical-preview.sh" in said,
                   "мутация «судит как нашу машину» → код 1, «изоляция tmux» и свой файл копии в причине",
                   f"код {rc}: {said[-300:]}")
        held = work / "под-карантином"
        shutil.copytree(clean, held)
        quarantine = work / "карантин.yaml"
        quarantine.write_text('карантин:\n  - гейт: "признаки"\n    карточка: hr.проба\n', encoding="utf-8")
        if mutate(held, SIGNALS, "    sys.exit(главная(sys.argv[1:]))\n",
                  '    print("[признаки] мутация пробы: судят без источника")\n    sys.exit(1)\n'):
            rc, said = judged(clean, held, quarantine)
            report(rc == 1 and "новая красная «признаки»" in said and "мутация пробы" in said,
                   "красная под нашим карантином → стенд судит её, код 1", f"код {rc}: {said[-300:]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"ТИПОВАЯ КОПИЯ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
