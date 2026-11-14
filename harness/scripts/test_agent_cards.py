#!/usr/bin/env python3
"""Пробы собранных карточек помощников (спека §4.4, Ф3).

Системный промпт помощника — тело его карточки .claude/agents/<тип>.md: добавку
смены помощник не наследует (спека §3). Поэтому карточки СОБИРАЮТСЯ тем же
сборщиком, что и промпт класса, а правка руками краснит гейт:

    self-prompt.py --карточки --в <каталог>     собрать три карточки в каталог
    self-prompt.py --карточки --проверить       сверить каталог карточек со сборкой

Карточка = шапка (frontmatter, как у нынешней карточки) + собранный текст класса
+ последняя строка «отпечаток: <метка сборки класса>». Тип → класс:
harness-worker → исполнитель, harness-lead → управляющий, harness-explorer → разведчик.

Двери (все — пути):
    SELF_PROMPT_PY   сборщик под пробой (по умолчанию — рядом с пробой)
    SELF_ETALON_DIR  эталон слоя 1
    SELF_TREE_DIR    корень дерева
    LAUNCH_POLICY    файл политики запуска
    SELF_CARDS_DIR   каталог карточек, который судит --проверить
                     (по умолчанию .claude/agents дерева)

    python3 scripts/test_agent_cards.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

COLLECTOR = Path(os.environ.get("SELF_PROMPT_PY") or HERE / "self-prompt.py")
LIVE_POLICY = ROOT / "harness" / "config" / "launch-policy.yaml"
LIVE_CARDS = ROOT / ".claude" / "agents"
PACKAGE_CARDS = Path("/home/user/starter/STARTER-PACKAGE/harness/agents")
DOORS = ("SELF_PROMPT_PY", "SELF_ETALON_DIR", "SELF_TREE_DIR", "LAUNCH_POLICY", "SELF_CARDS_DIR")
CLASS_OF = {"harness-worker": "исполнитель", "harness-lead": "управляющий",
            "harness-explorer": "разведчик"}
# Шапка исполнителя и управляющего переезжает в собранную карточку как есть:
# инструменты и omitClaudeMd держат цену старта помощника.
TOOLS = {"harness-worker": "Read, Write, Edit, Bash", "harness-lead": "Read, Write, Bash, Agent",
         "harness-explorer": "Read, Grep, Glob, Bash"}
FINGERPRINT = re.compile(r"^отпечаток: (\S+-[0-9a-f]{6})$")
LABEL = re.compile(r"^Метка сборки: (\S+-[0-9a-f]{6})$")

cases = Cases()


def env_of(etalon: Path, policy: Path = LIVE_POLICY, cards: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in DOORS}
    env.update(SELF_ETALON_DIR=str(etalon), SELF_TREE_DIR=str(ROOT), LAUNCH_POLICY=str(policy),
               PYTHONDONTWRITEBYTECODE="1")
    if cards is not None:
        env["SELF_CARDS_DIR"] = str(cards)
    return env


def run(args: list, env: dict):
    done = subprocess.run([sys.executable, str(COLLECTOR), *args], env=env,
                          capture_output=True, text=True, timeout=120)
    return done.returncode, done.stderr


def build_cards(target: Path, env: dict):
    target.mkdir(parents=True, exist_ok=True)
    return run(["--карточки", "--в", str(target)], env)


def verify_cards(cards: Path, env: dict):
    return run(["--карточки", "--проверить"], {**env, "SELF_CARDS_DIR": str(cards)})


def class_text(cls: str, out: Path, env: dict) -> str:
    """Собранный текст класса той же сборкой; пусто, если сборщик отказал."""
    out.parent.mkdir(parents=True, exist_ok=True)
    rc, _ = run(["--класс", cls, "--в", str(out)], env)
    return out.read_text(encoding="utf-8") if rc in (0, 3) and out.exists() else ""


def split_card(text: str):
    """(шапка, тело, последняя строка) или None, если шапки нет."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    rest = text[end + 5:].rstrip("\n").split("\n")
    return text[4:end], "\n".join(rest[:-1]).strip("\n"), rest[-1]


def foreign_fingerprint(text: str) -> str:
    """Та же карточка с чужим, но правильным по виду отпечатком: хвост сдвинут на единицу."""
    head, _, tail = text.rstrip("\n").rpartition("-")
    return f"{head}-{(int(tail, 16) + 1) % 0x1000000:06x}\n" if re.fullmatch(r"[0-9a-f]{6}", tail) else text + "\n#"


def snapshot(folder: Path) -> dict:
    if not folder.is_dir():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())
            if p.is_file()}


def tree_bytes(folder: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else {}


def check_build(root: Path, env: dict) -> Path:
    print("── сборка трёх карточек из эталона ──")
    cards = root / "карточки"
    rc, err = build_cards(cards, env)
    cases.check("--карточки --в <каталог>: код 0", (0, ""), (rc, "" if rc == 0 else err.strip()[:200]))
    cases.check("в каталоге ровно три карточки, временных файлов нет",
                sorted(f"{name}.md" for name in CLASS_OF), sorted(snapshot(cards)))
    for name, cls in CLASS_OF.items():
        path = cards / f"{name}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        parts = split_card(text)
        cases.check(f"{name}: шапка frontmatter в начале", True, parts is not None)
        if parts is None:
            continue
        head, body, last = parts
        meta = yaml.safe_load(head) or {}
        cases.check(f"{name}: name, tools, omitClaudeMd в шапке",
                    (name, TOOLS[name], True),
                    (meta.get("name"), meta.get("tools"), meta.get("omitClaudeMd")))
        cases.check(f"{name}: description в шапке не пуст", True, bool(str(meta.get("description") or "").strip()))
        live = LIVE_CARDS / f"{name}.md"
        if name != "harness-explorer":
            live_parts = split_card(live.read_text(encoding="utf-8")) if live.exists() else None
            cases.check(f"{name}: шапка — из нынешней карточки без изменений",
                        live_parts[0] if live_parts else "нет живой карточки", head)
        built = class_text(cls, root / "класс" / f"{cls}.md", env)
        first, _, rest = built.partition("\n")
        label = LABEL.match(first)
        cases.check(f"{cls}: классовая сборка дала метку (условие пробы)", True, label is not None)
        mark = FINGERPRINT.match(last)
        cases.check(f"{name}: последняя строка «отпечаток: <метка>» вида \\S+-[0-9a-f]{{6}}",
                    True, mark is not None)
        cases.check(f"{name}: отпечаток равен метке сборки класса {cls}",
                    label[1] if label else "нет метки класса", mark[1] if mark else last[:80])
        # Строка метки в теле допустима: отпечаток её уже несёт.
        cases.check(f"{name}: тело = собранный текст класса {cls}", True,
                    bool(built) and body in (built.strip("\n"), rest.strip("\n")))
    return cards


def check_repeat(root: Path, env: dict, cards: Path) -> None:
    print("── повтор без правки — те же байты ──")
    before = tree_bytes(cards)
    again = root / "повтор"
    build_cards(again, env)
    cases.check("сборка в другой каталог — те же байты", (True, before), (bool(before), tree_bytes(again)))
    build_cards(cards, env)
    cases.check("пересборка поверх — те же байты", (True, before), (bool(before), tree_bytes(cards)))


def check_verify(root: Path, env: dict, cards: Path) -> None:
    print("── --карточки --проверить ──")
    rc, err = verify_cards(cards, env)
    cases.check("собранные карточки: --проверить код 0", (0, ""), (rc, "" if rc == 0 else err.strip()[:200]))

    cases_of_break = (
        ("harness-lead", "тело правлено руками",
         lambda t: t.replace("\nотпечаток: ", "\nправка руками\n\nотпечаток: ")),
        ("harness-explorer", "отпечаток подменён", foreign_fingerprint),
        ("harness-worker", "карточки нет", None),
    )
    for name, what, spoil in cases_of_break:
        bench = root / f"проверить-{name}"
        shutil.copytree(cards, bench)
        path = bench / f"{name}.md"
        if not path.exists():
            cases.check(f"{name}: {what} — условие: карточка собрана", True, False)
            continue
        if spoil is None:
            path.unlink()
        else:
            path.write_text(spoil(path.read_text(encoding="utf-8")), encoding="utf-8")
        spoiled = tree_bytes(bench)
        rc, err = verify_cards(bench, env)
        cases.check(f"{name}: {what} — --проверить код 1 и имя карточки в stderr",
                    (1, True), (rc, name in err))
        cases.check(f"{name}: {what} — --проверить каталог не чинит", spoiled, tree_bytes(bench))

    # Источник роли изменился, карточки старые: отпечаток ≠ текущей сборке.
    etalon = root / "эталон-правлен"
    shutil.copytree(Path(env["SELF_ETALON_DIR"]), etalon)
    role = etalon / "roles" / "worker.md"
    role.write_text(role.read_text(encoding="utf-8") + "\nновое правило роли\n", encoding="utf-8")
    rc, err = verify_cards(cards, {**env, "SELF_ETALON_DIR": str(etalon)})
    cases.check("роль исполнителя изменилась, карточки не пересобраны — код 1 и harness-worker",
                (1, True), (rc, "harness-worker" in err))


def inodes(folder: Path) -> dict:
    return {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in sorted(folder.glob("*.md"))}


def check_only_stale(root: Path, env: dict) -> None:
    """Ревью #5, Н1: сборка сама создаёт каталог и пишет только карточки, не равные сборке.

    Повтор раскладки не трогает файлы; правленая руками карточка переписывается одна."""
    print("── пишутся только устаревшие карточки ──")
    fresh = root / "нет-каталога" / "agents"
    rc, err = run(["--карточки", "--в", str(fresh)], env)
    cases.check("каталога нет — сборщик создаёт его сам: код 0 и три карточки",
                (0, sorted(f"{name}.md" for name in CLASS_OF)),
                (rc, sorted(snapshot(fresh)) if rc == 0 else err.strip()[:200]))
    before = inodes(fresh)
    time.sleep(0.05)   # mtime_ns переписанного файла обязан отличаться
    run(["--карточки", "--в", str(fresh)], env)
    cases.check("повтор без правки: inode и mtime каждой карточки прежние", (True, before),
                (bool(before), inodes(fresh)))
    edited = fresh / "harness-lead.md"
    if not edited.exists():
        cases.check("правка руками — условие: harness-lead собран", True, False)
        return
    good = edited.read_bytes()
    edited.write_bytes(good + "\nправка руками\n".encode("utf-8"))
    before = inodes(fresh)
    time.sleep(0.05)
    run(["--карточки", "--в", str(fresh)], env)
    after = inodes(fresh)
    cases.check("правка руками: переписана только harness-lead, её байты — сборка",
                (["harness-lead.md"], good),
                (sorted(n for n in before if before[n] != after.get(n)), edited.read_bytes()))


def check_degraded_and_refusal(root: Path, env: dict, cards: Path) -> None:
    print("── без эталона и сверх потолка — как в классовой сборке ──")
    no_etalon = {**env, "SELF_ETALON_DIR": str(root / "нет-эталона")}
    target = root / "без-эталона"
    rc, err = build_cards(target, no_etalon)
    cases.check("эталона нет — код 3", (3, ""), (rc, "" if rc == 3 else err.strip()[:200]))
    cases.check("эталона нет — три карточки собраны",
                sorted(f"{name}.md" for name in CLASS_OF), sorted(snapshot(target)))
    for name, cls in CLASS_OF.items():
        path = target / f"{name}.md"
        parts = split_card(path.read_text(encoding="utf-8")) if path.exists() else None
        built = class_text(cls, root / "класс-без-эталона" / f"{cls}.md", no_etalon)
        label = LABEL.match(built.partition("\n")[0])
        cases.check(f"{name}: без эталона отпечаток = метка деградированной сборки {cls}",
                    f"отпечаток: {label[1]}" if label else "нет метки класса",
                    parts[2] if parts else "нет карточки")

    policy = root / "политика-потолок.yaml"
    policy.write_text(re.sub(r"потолок: \d+", "потолок: 1", LIVE_POLICY.read_text(encoding="utf-8")),
                      encoding="utf-8")
    tight = {**env, "LAUNCH_POLICY": str(policy)}
    empty = root / "потолок-пусто"
    rc, err = build_cards(empty, tight)
    cases.check("сверх потолка — код 1 и «потолок» в stderr", (1, True), (rc, "потолк" in err))
    cases.check("сверх потолка — в пустом каталоге карточек не появилось", {}, snapshot(empty))
    before = tree_bytes(cards)
    build_cards(cards, tight)
    cases.check("сверх потолка — прежние карточки не тронуты", (True, before),
                (bool(before), tree_bytes(cards)))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="sap-ch9-"))
    live_before = {"проект": snapshot(LIVE_CARDS), "пакет": snapshot(PACKAGE_CARDS)}
    try:
        print("── сборщик под пробой ──")
        cases.check(f"сборщик есть: {COLLECTOR}", True, COLLECTOR.is_file())
        etalon = root / "эталон"
        shutil.copytree(ROOT / "harness" / "self", etalon)
        env = env_of(etalon)
        cards = check_build(root, env)
        check_repeat(root, env, cards)
        check_only_stale(root, env)
        check_verify(root, env, cards)
        check_degraded_and_refusal(root, env, cards)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    cases.check("живые карточки проекта и пакета стендом не тронуты", live_before,
                {"проект": snapshot(LIVE_CARDS), "пакет": snapshot(PACKAGE_CARDS)})
    print(f"итог test_agent_cards: случаев {cases.paths}, неудач {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
