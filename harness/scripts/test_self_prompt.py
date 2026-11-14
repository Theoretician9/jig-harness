#!/usr/bin/env python3
"""Пробы сборщика личности scripts/self-prompt.py (спека §4.3).

Сборщик кладёт в файл системный промпт класса запуска: наборы текста по
harness/config/launch-policy.yaml. Сбой здесь опасен тихо: модель уходит в
работу без личности и стандартов, и никто этого не видит (Б-1 ревью спеки).
Поэтому пробы держат коды 0/3/1, метку сборки, потолки, отсутствие кэша и
целость файла при параллельных сборках.

Двери (все — пути):
    SELF_PROMPT_PY   сборщик под пробой (по умолчанию — рядом с пробой)
    SELF_ETALON_DIR  эталон слоя 1: harness/self/<x> читается как <эталон>/<x>
    SELF_TREE_DIR    корень дерева: harness/self/<x> читается как <дерево>/harness/self/<x>
    LAUNCH_POLICY    файл политики запуска
    CONTEXT_HYGIENE_PY  гейт гигиены под пробой (потолок файла личности при коммите)

Метка — первая строка файла «Метка сборки: <слово>-<6 hex>»; хвост — первые
6 знаков sha256 (utf-8) всего, что в файле ПОСЛЕ первой строки.

    python3 scripts/test_self_prompt.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402
from token_estimate import tokens  # noqa: E402

COLLECTOR = Path(os.environ.get("SELF_PROMPT_PY") or HERE / "self-prompt.py")
HYGIENE = Path(os.environ.get("CONTEXT_HYGIENE_PY") or HERE / "check-context-hygiene.py")
LIVE_POLICY = ROOT / "harness" / "config" / "launch-policy.yaml"
LABEL = re.compile(r"^Метка сборки: \S+-([0-9a-f]{6})$")
DOORS = ("SELF_PROMPT_PY", "SELF_ETALON_DIR", "SELF_TREE_DIR", "LAUNCH_POLICY")

cases = Cases()


def run(cls: str, out: Path, etalon: Path, tree: Path, policy: Path):
    env = {k: v for k, v in os.environ.items() if k not in DOORS}
    env.update(SELF_ETALON_DIR=str(etalon), SELF_TREE_DIR=str(tree), LAUNCH_POLICY=str(policy))
    # Каталог цели готовит вызывающий (носитель): сборщик пишет в данный путь.
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([sys.executable, str(COLLECTOR), "--класс", cls, "--в", str(out)],
                          env=env, capture_output=True, text=True, timeout=60)
    return done.returncode, done.stderr


def label_of(path: Path) -> str:
    """Хвост метки, если файл целый (метка есть и сходится с текстом), иначе причина."""
    if not path.exists():
        return "файла нет"
    text = path.read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    m = LABEL.match(first)
    if not m:
        return f"первая строка не метка: {first[:60]!r}"
    if hashlib.sha256(body.encode("utf-8")).hexdigest()[:6] != m.group(1):
        return "хвост метки не sha256 текста"
    return m.group(1)


def is_label(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{6}", value) is not None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# Подставное дерево: у каждого источника своё слово-маркер, по нему видно,
# откуда взят кусок и в каком порядке он стоит.
SYNTH_POLICY = """\
наборы:
  первый:  {из: harness/self/one.md, слой: 1, потолок: 200}
  второй:  {из: harness/self/two.md, слой: 1}
  копия:   {из: harness/self/instance.yaml, слой: 2, потолок: 200}
  поля:    {из: harness/self/core.json, поля: [сущность, принципы], слой: 1, потолок: 200}
  раздел:  {из: harness/self/standards.md, раздел: "Инварианты", слой: 1, потолок: 200}
  большой: {из: harness/self/big.md, слой: 1, потолок: 50}
классы:
  прямой:   [первый, второй, копия]
  обратный: [копия, второй, первый]
  выборка:  [поля, раздел]
  жадный:   [первый, большой]
"""


def synth_layer1(base: Path, who: str) -> None:
    write(base / "one.md", f"ПЕРВЫЙ-{who}\n")
    write(base / "two.md", f"ВТОРОЙ-{who}\n")
    write(base / "instance.yaml", f"копия: КОПИЯ-{who}\n")
    write(base / "core.json", json.dumps({"сущность": f"СУЩНОСТЬ-{who}", "принципы": [f"ПРИНЦИП-{who}"],
                                          "смысл": f"ЛИШНЕЕ-ПОЛЕ-{who}"}, ensure_ascii=False))
    write(base / "standards.md", f"# Стандарты\n\n## Инварианты\nИНВАРИАНТ-{who}\n\n"
                                 f"## Планка кода\nЛИШНИЙ-РАЗДЕЛ-{who}\n")
    write(base / "big.md", " ".join(f"слово{i}" for i in range(2000)))


def make_stand(root: Path):
    tree = root / "tree"
    etalon = root / "etalon"
    synth_layer1(tree / "harness" / "self", "ДЕРЕВО")
    synth_layer1(etalon, "ЭТАЛОН")
    policy = root / "policy.yaml"
    write(policy, SYNTH_POLICY)
    return etalon, tree, policy


def check_collector_exists() -> None:
    print("── сборщик под пробой ──")
    cases.check(f"сборщик есть: {COLLECTOR}", True, COLLECTOR.is_file())


def check_live_classes(root: Path) -> None:
    print("── живая политика: все классы собираются ──")
    classes = list((yaml.safe_load(LIVE_POLICY.read_text(encoding="utf-8")) or {}).get("классы") or {})
    cases.check("список классов разобран из launch-policy.yaml и не пуст", True, len(classes) > 0)
    etalon_copy = root / "live-etalon"
    shutil.copytree(ROOT / "harness" / "self", etalon_copy)
    for cls in classes:
        out = root / "live" / f"{cls}.md"
        rc, err = run(cls, out, root / "нет-эталона", ROOT, LIVE_POLICY)
        cases.check(f"{cls}: без эталона — код 3", (3, ""), (rc, "" if rc == 3 else err.strip()[:200]))
        cases.check(f"{cls}: без эталона — файл с меткой", True, is_label(label_of(out)))
        out.unlink(missing_ok=True)
        rc, err = run(cls, out, etalon_copy, ROOT, LIVE_POLICY)
        cases.check(f"{cls}: эталон есть — код 0", (0, ""), (rc, "" if rc == 0 else err.strip()[:200]))
        cases.check(f"{cls}: эталон есть — файл с меткой", True, is_label(label_of(out)))


def live_piece(spec: dict) -> str:
    """Кусок набора так, как его режет сборщик: файл целиком, поля JSON или раздел."""
    text = (ROOT / spec["из"]).read_text(encoding="utf-8")
    if "поля" in spec:
        data = json.loads(text)
        return json.dumps({k: data[k] for k in spec["поля"]}, ensure_ascii=False, indent=2)
    if "раздел" in spec:
        m = re.search(rf"^## {re.escape(spec['раздел'])}\n.*?(?=^## |\Z)", text, re.S | re.M)
        return m.group(0) if m else ""
    return text


def check_live_ceilings() -> None:
    print("── живая политика: каждый набор не выше потолка ──")
    sets = (yaml.safe_load(LIVE_POLICY.read_text(encoding="utf-8")) or {}).get("наборы") or {}
    capped = {name: spec for name, spec in sets.items() if "потолок" in spec}
    cases.check("наборы с потолком разобраны и не пусты", True, len(capped) > 0)
    for name, spec in capped.items():
        piece = live_piece(spec)
        cases.check(f"набор {name}: кусок не пуст", True, bool(piece.strip()))
        cost = tokens(piece)
        cases.check(f"набор {name}: {cost} ≤ {spec['потолок']}", True, cost <= spec["потолок"])


HYGIENE_POLICY = """\
наборы:
  большой: {из: harness/self/big.md, слой: 1, потолок: 50}
  малый:   {из: harness/self/one.md, слой: 1, потолок: 200}
  выборка: {из: harness/self/core.json, поля: [сущность], слой: 1, потолок: 1}
"""


def check_hygiene_ceiling(root: Path) -> None:
    """Потолок набора судят двое: сборщик при сборке и гейт гигиены при коммите.

    Гейт судит файл целиком; кусок (поля, раздел) — дело сборщика, файл выборки
    гейт не называет, даже если весь файл выше потолка."""
    print("── гейт гигиены: файл личности выше потолка политики ──")
    synth_layer1(root / "harness" / "self", "ГИГИЕНА")
    write(root / "harness" / "self" / "core.json",
          json.dumps({"сущность": " ".join(f"с{i}" for i in range(2000))}, ensure_ascii=False))
    write(root / "harness" / "config" / "launch-policy.yaml", HYGIENE_POLICY)
    done = subprocess.run([sys.executable, str(HYGIENE), "--repo", str(root)],
                          capture_output=True, text=True, timeout=120)
    said = done.stdout + done.stderr
    cases.check("гигиена: файл набора выше потолка — блок «big.md разросся»", True,
                "harness/self/big.md разросся" in said)
    cases.check("гигиена: код 1 при разросшемся файле", 1, done.returncode)
    cases.check("гигиена: файл в пределах потолка не назван", False, "one.md разросся" in said)
    cases.check("гигиена: набор-выборка (поля) гейтом не судится", False, "core.json разросся" in said)


def check_layers(root: Path) -> None:
    print("── слои: эталон для слоя 1, дерево для слоя 2 ──")
    etalon, tree, policy = make_stand(root)
    out = root / "out" / "прямой.md"
    rc, _ = run("прямой", out, etalon, tree, policy)
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    cases.check("эталон есть — код 0", 0, rc)
    cases.check("слой 1 взят из эталона", (True, False), ("ПЕРВЫЙ-ЭТАЛОН" in text, "ПЕРВЫЙ-ДЕРЕВО" in text))
    cases.check("слой 2 взят из дерева, не из эталона", (True, False), ("КОПИЯ-ДЕРЕВО" in text, "КОПИЯ-ЭТАЛОН" in text))

    out = root / "out" / "деградация.md"
    rc, _ = run("прямой", out, root / "нет-эталона", tree, policy)
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    cases.check("эталона нет — код 3", 3, rc)
    cases.check("эталона нет — файл собран с меткой", True, is_label(label_of(out)))
    cases.check("эталона нет — слой 1 из дерева", True, "ПЕРВЫЙ-ДЕРЕВО" in text)

    out = root / "out" / "пусто.md"
    rc, err = run("прямой", out, root / "нет-эталона", root / "нет-дерева", policy)
    cases.check("нет ни эталона, ни дерева — код 1", 1, rc)
    cases.check("нет ни эталона, ни дерева — файл не создан", False, out.exists())
    cases.check("нет ни эталона, ни дерева — причина в stderr", True, bool(err.strip()))


def check_cut(root: Path) -> None:
    print("── поля и раздел: берётся только названное ──")
    etalon, tree, policy = make_stand(root)
    out = root / "out" / "выборка.md"
    rc, _ = run("выборка", out, etalon, tree, policy)
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    cases.check("выборка собрана — код 0", 0, rc)
    cases.check("поля: названные поля есть", (True, True), ("СУЩНОСТЬ-ЭТАЛОН" in text, "ПРИНЦИП-ЭТАЛОН" in text))
    cases.check("поля: чужого поля нет", False, "ЛИШНЕЕ-ПОЛЕ-ЭТАЛОН" in text)
    cases.check("раздел: названный раздел есть", True, "ИНВАРИАНТ-ЭТАЛОН" in text)
    cases.check("раздел: соседнего раздела нет", False, "ЛИШНИЙ-РАЗДЕЛ-ЭТАЛОН" in text)


def check_refusals(root: Path) -> None:
    print("── отказы: потолок и неизвестный класс ──")
    etalon, tree, policy = make_stand(root)
    out = root / "out" / "жадный.md"
    rc, err = run("жадный", out, etalon, tree, policy)
    cases.check("набор сверх потолка — код 1", 1, rc)
    cases.check("набор сверх потолка — файл не создан", False, out.exists())
    cases.check("набор сверх потолка — в stderr имя набора", True, "большой" in err)
    numbers = [int(n) for n in re.findall(r"\d+", err)]
    cases.check("набор сверх потолка — в stderr число токенов выше потолка", True, any(n > 50 for n in numbers))

    out = root / "out" / "чужой.md"
    rc, err = run("нет-такого-класса", out, etalon, tree, policy)
    cases.check("неизвестный класс — код 1", 1, rc)
    cases.check("неизвестный класс — файл не создан", False, out.exists())
    known = list(yaml.safe_load(policy.read_text(encoding="utf-8"))["классы"])
    cases.check("неизвестный класс — в stderr список классов", [], [c for c in known if c not in err])


def check_label_and_no_cache(root: Path) -> None:
    print("── метка: та же без правки, другая после правки; кэша нет ──")
    etalon, tree, policy = make_stand(root)
    out = root / "out" / "метка.md"
    run("прямой", out, etalon, tree, policy)
    first = label_of(out)
    cases.check("метка сходится с текстом", True, is_label(first))
    run("прямой", out, etalon, tree, policy)
    again = label_of(out)
    cases.check("повтор без правки — метка та же", (True, True), (is_label(again), again == first))

    source = etalon / "two.md"
    old = source.stat()
    write(source, "ВТОРОЙ-ЭТАЛОН-ПРАВЛЕН\n")
    run("прямой", out, etalon, tree, policy)
    second = label_of(out)
    cases.check("правка источника меняет метку", (True, True), (is_label(second), second != first))

    # Правка со старым mtime: сборщик, сверяющий время, её бы не увидел.
    write(source, "ВТОРОЙ-ЭТАЛОН-ТИХО\n")
    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    run("прямой", out, etalon, tree, policy)
    third = label_of(out)
    cases.check("правка со старым mtime тоже меняет метку", (True, True), (is_label(third), third != second))

    # Собранный файл свежее источников и испорчен: модель может его переписать.
    write(out, "чужой текст без метки\n")
    run("прямой", out, etalon, tree, policy)
    rebuilt = label_of(out)
    cases.check("подменённый файл перезаписан следующей сборкой", (True, True), (is_label(rebuilt), rebuilt == third))


PASSPORT_KEYS = ("PROJECT_DIR", "AGENT_USER", "PROJECT_NAME")


def run_with_passport(cls: str, out: Path, etalon: Path, tree: Path, policy: Path, conf: Path):
    """Сборка на стенде с install.conf стенда: ключи паспорта из окружения убраны,
    иначе они старше файла (config.конфиг) и проба судила бы паспорт машины."""
    env = {k: v for k, v in os.environ.items() if k not in DOORS + PASSPORT_KEYS}
    env.update(SELF_ETALON_DIR=str(etalon), SELF_TREE_DIR=str(tree), LAUNCH_POLICY=str(policy),
               HARNESS_INSTALL_CONF=str(conf))
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([sys.executable, str(COLLECTOR), "--класс", cls, "--в", str(out)],
                          env=env, capture_output=True, text=True, timeout=60)
    return done.returncode, out.read_text(encoding="utf-8") if out.exists() else ""


def stand_passport(root: Path) -> tuple:
    """(install.conf стенда, его PROJECT_DIR, его домашний каталог агента)."""
    project, user = root / "proj-стенд", "жилец-стенд"
    conf = root / "install.conf"
    # Пробел в кавычках у имени: значение паспорта судится очищенным от краёв.
    write(conf, f'PROJECT_DIR="{project}"\nAGENT_USER="{user}"\nPROJECT_NAME="Стенд "\n')
    return conf, str(project), f"/home/{user}"


def check_passport_expanded(root: Path) -> None:
    """Р2/Р3: эталон обезличен (${PROJECT_DIR}),
    и модель не должна получать подстановку нераскрытой."""
    print("── паспорт: ${PROJECT_DIR} и ${AGENT_USER} раскрыты значениями install.conf ──")
    etalon, tree, policy = make_stand(root)
    conf, project, home = stand_passport(root)
    # ${PROJECT_DIR} дважды: раскрыта каждая подстановка, не только первая.
    role = ("Проект `${PROJECT_DIR}`; пакет `/home/${AGENT_USER}/starter`; копия «${PROJECT_NAME}»;\n"
            "журналы ${PROJECT_DIR}/.tasks.\n")
    write(etalon / "one.md", role)
    write(tree / "harness" / "self" / "one.md", role)
    for mode, source in (("эталон есть", etalon), ("эталона нет", root / "нет-эталона")):
        rc, text = run_with_passport("прямой", root / "out" / f"паспорт-{mode}.md", source, tree, policy, conf)
        cases.check(f"{mode}: собрано (код 0 или 3)", True, rc in (0, 3))
        cases.check(f"{mode}: в собранном нет «${{»", [], re.findall(r"\$\{\w*\}?", text))
        cases.check(f"{mode}: PROJECT_DIR стенда подставлен", True, project in text)
        cases.check(f"{mode}: дом AGENT_USER стенда подставлен", True, f"{home}/starter" in text)
        cases.check(f"{mode}: PROJECT_NAME подставлен без пробелов края", True, "копия «Стенд»" in text)
    # Паспорт без ключа (старая установка, ключ не записан): сборка не падает, модель
    # получает личность; нераскрытая подстановка — меньшее зло, чем модель без личности.
    bare = root / "install-без-имени.conf"
    write(bare, f'PROJECT_DIR="{project}"\n')
    rc, text = run_with_passport("прямой", root / "out" / "паспорт-без-ключа.md", etalon, tree, policy, bare)
    cases.check("паспорт без AGENT_USER и PROJECT_NAME: собрано (код 0 или 3)", True, rc in (0, 3))
    cases.check("паспорт без ключей: PROJECT_DIR всё равно подставлен", True, project in text)


def live_sources(policy: dict, cls: str) -> list:
    sets = policy.get("наборы") or {}
    return [ROOT / sets[name]["из"] for name in policy["классы"][cls] if name in sets]


def check_live_passport(root: Path) -> None:
    """Живая политика на стенде с чужим паспортом: каждый класс без «${», и класс,
    чья роль называет проект (подстановкой или литералом паспорта этой машины),
    называет его значением стенда."""
    print("── живая политика: каждый класс собран с паспортом стенда ──")
    sys.path.insert(0, str(HERE / "lib"))
    import config
    machine_dir = (config.из_файла(config.путь_install_conf()).get("PROJECT_DIR") or "").strip()
    conf, project, _ = stand_passport(root)
    policy = yaml.safe_load(LIVE_POLICY.read_text(encoding="utf-8")) or {}
    classes = list(policy.get("классы") or {})
    cases.check("классы живой политики разобраны", True, len(classes) > 0)
    for cls in classes:
        rc, text = run_with_passport(cls, root / "live" / f"{cls}.md", root / "нет-эталона",
                                     ROOT, LIVE_POLICY, conf)
        cases.check(f"{cls}: собран", True, rc in (0, 3))
        cases.check(f"{cls}: нет «${{»", [], re.findall(r"\$\{\w*\}?", text))
        names_project = any("${PROJECT_DIR}" in s.read_text(encoding="utf-8")
                            or (machine_dir and machine_dir in s.read_text(encoding="utf-8"))
                            for s in live_sources(policy, cls) if s.is_file())
        if names_project:
            cases.check(f"{cls}: роль называет проект — значением стенда, не этой машины",
                        (True, False), (project in text, bool(machine_dir) and machine_dir in text))


def check_order(root: Path) -> None:
    print("── порядок наборов = порядок класса ──")
    etalon, tree, policy = make_stand(root)
    for cls, order in (("прямой", ["ПЕРВЫЙ", "ВТОРОЙ", "КОПИЯ"]), ("обратный", ["КОПИЯ", "ВТОРОЙ", "ПЕРВЫЙ"])):
        out = root / "out" / f"порядок-{cls}.md"
        run(cls, out, etalon, tree, policy)
        text = out.read_text(encoding="utf-8") if out.exists() else ""
        where = [text.find(f"{word}-") for word in order]
        cases.check(f"{cls}: наборы в порядке {order}", True, -1 not in where and where == sorted(where))


def check_parallel(root: Path) -> None:
    print("── два сборщика в фоне: файл всегда целый ──")
    etalon, tree, policy = make_stand(root)
    target_dir = root / "parallel"
    out = target_dir / "прямой.md"
    rounds = 15
    codes: list = []

    def builder() -> None:
        for _ in range(rounds):
            codes.append(run("прямой", out, etalon, tree, policy)[0])

    workers = [threading.Thread(target=builder) for _ in range(2)]
    for w in workers:
        w.start()
    reads, broken = 0, []
    while any(w.is_alive() for w in workers):
        if out.exists():
            try:
                verdict = label_of(out)
            except FileNotFoundError:
                verdict = "файл пропал между проверкой и чтением"
            except UnicodeDecodeError:
                verdict = "прочитан оборванный текст"
            reads += 1
            if not is_label(verdict):
                broken.append(verdict)
        time.sleep(0.002)
    for w in workers:
        w.join()
    cases.check("все сборки отработали с кодом 0", [0] * (2 * rounds), codes)
    cases.check("файл прочитан хотя бы раз во время сборок", True, reads > 0)
    cases.check("ни одного рваного чтения", [], broken[:3])
    cases.check("в каталоге цели нет брошенных временных файлов", ["прямой.md"],
                sorted(p.name for p in target_dir.iterdir()) if target_dir.exists() else [])


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="sap-self-prompt-"))
    try:
        check_collector_exists()
        check_live_classes(root / "live")
        check_live_ceilings()
        for check in (check_layers, check_cut, check_refusals, check_label_and_no_cache,
                      check_order, check_parallel, check_hygiene_ceiling,
                      check_passport_expanded, check_live_passport):
            check(root / check.__name__)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"итог test_self_prompt: случаев {cases.paths}, неудач {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
