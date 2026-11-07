#!/usr/bin/env python3
"""test_spec_human_block.py — приёмка hr.spec-human-summary-block: спека уходит владельцу блоком «## Для человека».

Владелец 07.11.2026: в каждой спеке первым разделом блок «## Для человека» (суть
простыми словами), и при отправке спеки владельцу текстом уходит ИМЕННО этот блок.

Контракт spec_approval.py, который судит проба:
  ДЛЯ_ЧЕЛОВЕКА_ЗАГОЛОВОК = "## Для человека"
  ДЛЯ_ЧЕЛОВЕКА_ПОТОЛОК = 2500        — знаков тела блока
  блок_для_человека(текст) -> str | None
      тело от строки «## Для человека» до следующего заголовка «## » или конца
      файла, без строки заголовка, .strip(); раздела нет — None; пустое тело —
      тоже «нет» (None или "").
  отправить(id_задачи) — без описания; ключ --описание уходит, его заменяет блок.
      нет блока / пустой блок      → код 1, tg_send не зван, «отправлена» нет,
                                     в выводе «Для человека»;
      тело > ДЛЯ_ЧЕЛОВЕКА_ПОТОЛОК  → код 1, tg_send не зван, «отправлена» нет,
                                     в выводе «потолок» или 2500;
      тело ≤ лимита канала         → первый вызов tg_send ТЕКСТОМ (без --file),
                                     текст содержит тело целиком и сам влезает в
                                     лимит канала; затем --file с полной спекой;
                                     «отправлена» в журнале;
      лимит канала < тело ≤ потолок → блок ВЛОЖЕНИЕМ (--file + непустой
                                     --caption), полная спека отдельным
                                     вложением; «отправлена» в журнале.
  Лимит канала — ЖЁСТКИЙ потолок из потолки(корень) в check-channel-messages.py,
  того же читателя, которым tg_send.sh (проверить_правила) судит текст. Число
  стенда нарочно НЕ 1400: зашитое в код 1400 вместо читателя проба ловит.

Подмена tg_send — та же, что у test_consent_button_only.py: свой проект стенда во
временном каталоге, в его scripts/ — ссылки на настоящие скрипты (scripts/lib — копией), а tg_send.sh и
ask-consent.py — заглушки (пишут argv построчно JSON в свой файл, код 0; у
--file заглушка кладёт рядом и содержимое файла). spec_approval.py — КОПИЯ, не
ссылка: КОРЕНЬ = Path(__file__).resolve().parent.parent, и ссылка увела бы его в
боевой проект. Правки кода под подмену не нужно: КОРЕНЬ стенда уже ведёт
`bash КОРЕНЬ/scripts/tg_send.sh` и ask-consent.py на заглушки. Журналы — в
HARNESS_LOG_DIR стенда, карта dev-map.yaml и спека — в корне стенда; паспорт
(HARNESS_INSTALL_CONF, HARNESS_CONF) — свои файлы стенда, без PRODUCT_REPO.

Дверь мутации: SPEC_APPROVAL_UNDER_TEST=<путь> — какой файл копировать в стенд
вместо scripts/spec_approval.py рядом с пробой (мутируется копия, не боевой).

    python3 scripts/test_spec_human_block.py
Итог — последняя строка «ИТОГО: путей N, неудач K»; код 1 при K>0.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
SUBJECT = Path(os.environ.get("SPEC_APPROVAL_UNDER_TEST") or SCRIPTS / "spec_approval.py")
CHANNEL_GUARD = SCRIPTS / "check-channel-messages.py"
TASK = "hr.human-probe"
SPEC_REL = "docs/specs/human-probe.md"
HEADER = "## Для человека"
BODY_CEILING = 2500          # из контракта; константу модуля проба сверяет отдельно
# Потолки тона стенда: жёсткий ниже боевых 1400, чтобы зашитое число отличалось от читателя.
STAND_TONE = "<!-- потолки: мягкий=600 жёсткий=1000 -->"

STUB_PY = r'''import json, sys
from pathlib import Path
args = sys.argv[2:]
record = {"argv": args}
if "--file" in args:
    i = args.index("--file")
    try:
        record["file_text"] = Path(args[i + 1]).read_text(encoding="utf-8")
    except (IndexError, OSError) as e:
        record["file_text"] = None
        record["file_error"] = str(e)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
# Метка fail-text рядом с журналом вызовов — сторож канала отклонил текст.
if "--file" not in args and Path(sys.argv[1]).with_name("fail-text").exists():
    sys.exit(1)
'''

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok or not detail else f" — {detail}"))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exact(length: int, tag: str) -> str:
    """Тело блока ровно length знаков, строками; tag отличает тело от остальной спеки."""
    line = f"Суть {tag}: что меняем и как владелец это увидит.\n"
    body = (line * (length // len(line) + 1))[:length]
    return body[:-1] + "ж" if body.endswith(("\n", " ")) else body


class Stand:
    """Свой проект на случай: карта, спека, тон, паспорт, журналы, заглушки канала."""

    def __init__(self, spec_text: str):
        self.root = Path(tempfile.mkdtemp(prefix="human-stand-"))
        self.logs = self.root / "logs"
        self.logs.mkdir()
        scripts = self.root / "scripts"
        scripts.mkdir()
        # lib — копия, не ссылка: door_subjects.spec_file берёт spec_approval по
        # resolve() своего файла, и ссылка увела бы хэш кнопки на боевую карту.
        own = {"spec_approval.py", "tg_send.sh", "ask-consent.py", "lib"}
        for f in SCRIPTS.iterdir():
            if f.name not in own and not f.name.startswith(".") and f.name != "__pycache__":
                (scripts / f.name).symlink_to(f)
        shutil.copytree(SCRIPTS / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(SUBJECT, scripts / "spec_approval.py")
        self.tg_calls = self.root / "tg-calls.jsonl"
        self.ask_calls = self.root / "ask-calls.jsonl"
        (self.root / "stub.py").write_text(STUB_PY, encoding="utf-8")
        (scripts / "tg_send.sh").write_text(
            f'#!/bin/sh\nexec python3 "{self.root}/stub.py" "{self.tg_calls}" "$@"\n', encoding="utf-8")
        (scripts / "ask-consent.py").write_text(
            "import runpy, sys\n"
            f"sys.argv = ['stub', {str(self.ask_calls)!r}] + sys.argv[1:]\n"
            f"runpy.run_path({str(self.root / 'stub.py')!r}, run_name='__main__')\n",
            encoding="utf-8")
        (self.root / "память").mkdir()
        (self.root / "память" / "TONE.md").write_text(f"# Тон\n\n{STAND_TONE}\n", encoding="utf-8")
        (self.root / "docs" / "specs").mkdir(parents=True)
        self.spec = self.root / SPEC_REL
        self.spec.write_text(spec_text, encoding="utf-8")
        (self.root / "dev-map.yaml").write_text(
            "lines:\n"
            f"  - id: {TASK}\n"
            "    status: plan\n"
            "    title: Проба блока для человека\n"
            f"    spec: {SPEC_REL}\n", encoding="utf-8")
        self.install_conf = self.root / "install.conf"
        self.install_conf.write_text(
            f'PROJECT_DIR="{self.root}"\nLOG_DIR="{self.logs}"\n', encoding="utf-8")
        self.harness_conf = self.root / "harness.conf"
        self.harness_conf.write_text('AUTOMATION_LEVEL="partial"\n', encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE", "HARNESS_", "SPEC_APPROVAL"))}
        self.env.update(HARNESS_HOME=str(self.root), HARNESS_INSTALL_CONF=str(self.install_conf),
                        HARNESS_CONF=str(self.harness_conf), HARNESS_LOG_DIR=str(self.logs),
                        LOG_DIR=str(self.logs), PROJECT_DIR=str(self.root),
                        PYTHONDONTWRITEBYTECODE="1")

    def send(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / "spec_approval.py"), "--отправить", TASK],
            capture_output=True, text=True, errors="replace", env=self.env, timeout=120,
            cwd=str(self.root))

    def calls(self, path: Path) -> list:
        if not path.exists():
            return []
        return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]

    def sent_records(self) -> list:
        journal = self.logs / "спеки-владельцу.jsonl"
        if not journal.exists():
            return []
        out = []
        for s in journal.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(s)
            except ValueError:
                continue
            if r.get("событие") == "отправлена" and r.get("задача") == TASK:
                out.append(r)
        return out

    def drop(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def spec_with(body: str | None, tail: str = "") -> str:
    head = "# Спека: проба блока для человека\n\n"
    rest = ("## Цель\n\nТехническая цель для исполнителя, владельцу не уходит.\n\n"
            "## Приёмка\n\nПроба test_spec_human_block.py зелёная.\n" + tail)
    if body is None:
        return head + rest
    return head + f"{HEADER}\n\n{body}\n\n" + rest


def channel_limit(root: Path) -> int:
    """Лимит канала тем же читателем, что у сторожа tg_send.sh: жёсткий потолок тона."""
    guard = load(CHANNEL_GUARD, "human_channel_guard")
    return guard.потолки(str(root))[1]


def is_file_call(call: dict) -> bool:
    return "--file" in call["argv"]


def caption_of(call: dict) -> str:
    a = call["argv"]
    return a[a.index("--caption") + 1] if "--caption" in a and a.index("--caption") + 1 < len(a) else ""


def unit_checks() -> None:
    print("— 1. константы и блок_для_человека()")
    stand = Stand(spec_with("x"))
    try:
        try:
            module = load(stand.root / "scripts" / "spec_approval.py", "human_subject")
        except Exception as e:  # noqa: BLE001 — стенд обязан назвать причину, а не упасть
            check("модуль spec_approval.py импортируется", False, repr(e))
            return
        check("ДЛЯ_ЧЕЛОВЕКА_ЗАГОЛОВОК == «## Для человека»",
              getattr(module, "ДЛЯ_ЧЕЛОВЕКА_ЗАГОЛОВОК", None) == HEADER,
              f"есть {getattr(module, 'ДЛЯ_ЧЕЛОВЕКА_ЗАГОЛОВОК', 'НЕТ КОНСТАНТЫ')!r}")
        check("ДЛЯ_ЧЕЛОВЕКА_ПОТОЛОК == 2500",
              getattr(module, "ДЛЯ_ЧЕЛОВЕКА_ПОТОЛОК", None) == BODY_CEILING,
              f"есть {getattr(module, 'ДЛЯ_ЧЕЛОВЕКА_ПОТОЛОК', 'НЕТ КОНСТАНТЫ')!r}")
        block = getattr(module, "блок_для_человека", None)
        if not callable(block):
            check("функция блок_для_человека есть", False, "НЕТ ФУНКЦИИ")
            return
        check("функция блок_для_человека есть", True)
        cases = [
            ("блок до следующего «## », без заголовка, strip",
             "# Т\n\n## Для человека\n\n  Суть раз.\nСуть два.  \n\n## Цель\n\nне это\n",
             "Суть раз.\nСуть два."),
            ("«### » внутри блока — часть тела",
             "## Для человека\nСуть.\n### Подробнее\nещё\n## Цель\nне это\n",
             "Суть.\n### Подробнее\nещё"),
            ("блок последним разделом — до конца файла",
             "# Т\n\n## Цель\nне это\n\n## Для человека\nСуть в конце.\n",
             "Суть в конце."),
        ]
        for name, text, want in cases:
            got = block(text)
            check(name, got == want, f"вышло {got!r}")
        got = block("# Т\n\n## Цель\nтолько техника\n")
        check("БОЛЬНОЙ СЛУЧАЙ: раздела нет → None", got is None, f"вышло {got!r}")
        got = block("# Т\n\n## Для человека\n\n   \n## Цель\nтехника\n")
        check("пустое тело → «нет» (None или пустая строка)", not got, f"вышло {got!r}")
        got = block("# Т\n\n## Для человекаа\nне тот\n## Цель\nx\n")
        check("чужой заголовок «## Для человекаа» блоком не считается", not got, f"вышло {got!r}")
    finally:
        stand.drop()


def refusal(name: str, spec_text: str, words: tuple[str, ...]) -> None:
    stand = Stand(spec_text)
    try:
        done = stand.send()
        out = (done.stdout or "") + (done.stderr or "")
        check(f"{name}: код 1", done.returncode == 1, f"код {done.returncode}")
        check(f"{name}: tg_send не зван ни разу", not stand.calls(stand.tg_calls),
              f"вызовов {len(stand.calls(stand.tg_calls))}")
        check(f"{name}: кнопка не запрошена", not stand.calls(stand.ask_calls))
        check(f"{name}: записи «отправлена» нет", not stand.sent_records())
        check(f"{name}: в выводе причина ({' / '.join(words)})",
              any(w in out for w in words), f"вывод: {out.strip()[-300:]!r}")
    finally:
        stand.drop()


def refusal_checks() -> None:
    print("— 2. отказ без отправки")
    refusal("БОЛЬНОЙ СЛУЧАЙ: спека без блока", spec_with(None), ("Для человека",))
    refusal("блок с пустым телом", spec_with("   "), ("Для человека",))
    refusal("тело на знак длиннее потолка", spec_with(exact(BODY_CEILING + 1, "сверх")),
            ("потолок", "2500"))


def text_send_checks() -> None:
    print("— 3. блок короче лимита канала уходит ТЕКСТОМ")
    stand = Stand("")
    try:
        limit = channel_limit(stand.root)
        check("стенд: читатель тона дал жёсткий потолок стенда (1000)", limit == 1000, f"дал {limit}")
        body = exact(limit // 2, "текстом")
        full = spec_with(body)
        stand.spec.write_text(full, encoding="utf-8")
        done = stand.send()
        out = (done.stdout or "") + (done.stderr or "")
        calls = stand.calls(stand.tg_calls)
        check("код 0", done.returncode == 0, f"код {done.returncode}: {out.strip()[-300:]!r}")
        check("tg_send зван дважды: текст и файл", len(calls) == 2, f"вызовов {len(calls)}")
        if not calls:
            return
        first = calls[0]
        check("первый вызов — ТЕКСТОМ, без --file", not is_file_call(first), f"argv {first['argv'][:3]!r}")
        message = next((a for a in first["argv"] if body in a), None)
        check("текст содержит тело блока целиком", message is not None)
        if message is not None:
            check("текст влезает в лимит канала (читатель тона)", len(message) <= limit,
                  f"{len(message)} > {limit}")
        files = [c for c in calls[1:] if is_file_call(c)]
        check("второй вызов — --file с полной спекой",
              len(calls) > 1 and is_file_call(calls[1]) and calls[1].get("file_text") == full)
        check("запись «отправлена» в журнале", len(stand.sent_records()) == 1,
              f"записей {len(stand.sent_records())}; файлов {len(files)}")
    finally:
        stand.drop()


def text_refused_checks() -> None:
    print("— 3б. сторож канала отклонил текст блока — отказ без следа")
    # БОЛЬНОЙ СЛУЧАЙ: без проверки кода владелец получил бы голый файл без сути,
    # а журнал — «отправлена».
    stand = Stand(spec_with(exact(300, "отклонён")))
    try:
        (stand.root / "fail-text").write_text("", encoding="utf-8")
        done = stand.send()
        calls = stand.calls(stand.tg_calls)
        check("текст отклонён: код 1", done.returncode == 1, f"код {done.returncode}")
        check("текст отклонён: полная спека файлом не ушла", not any(map(is_file_call, calls)),
              f"вызовов с --file {sum(map(is_file_call, calls))}")
        check("текст отклонён: записи «отправлена» нет", not stand.sent_records())
        check("текст отклонён: кнопка не запрошена", not stand.calls(stand.ask_calls))
    finally:
        stand.drop()


def attachment(name: str, length_of) -> None:
    stand = Stand("")
    try:
        limit = channel_limit(stand.root)
        body = exact(length_of(limit), "вложением")
        full = spec_with(body)
        stand.spec.write_text(full, encoding="utf-8")
        done = stand.send()
        out = (done.stdout or "") + (done.stderr or "")
        calls = stand.calls(stand.tg_calls)
        check(f"{name} ({len(body)} зн., лимит {limit}): код 0", done.returncode == 0,
              f"код {done.returncode}: {out.strip()[-300:]!r}")
        texts = [c for c in calls if not is_file_call(c)]
        long_text = [c for c in texts if any(len(a) > limit for a in c["argv"])]
        check(f"{name}: текстом сверх лимита канала ничего не ушло", not long_text,
              f"текстов сверх лимита {len(long_text)}")
        blocks = [c for c in calls if is_file_call(c) and c.get("file_text") is not None
                  and body in c["file_text"] and c["file_text"] != full]
        check(f"{name}: блок ушёл ВЛОЖЕНИЕМ (--file, не полная спека)", len(blocks) == 1,
              f"таких вложений {len(blocks)} из {len(calls)} вызовов")
        if blocks:
            check(f"{name}: у вложения блока непустая подпись --caption",
                  bool(caption_of(blocks[0]).strip()), f"argv {blocks[0]['argv']!r}")
        fulls = [c for c in calls if is_file_call(c) and c.get("file_text") == full]
        check(f"{name}: полная спека — отдельным вложением", len(fulls) == 1, f"таких {len(fulls)}")
        check(f"{name}: запись «отправлена» в журнале", len(stand.sent_records()) == 1)
    finally:
        stand.drop()


def attachment_checks() -> None:
    print("— 4. блок длиннее лимита канала, но не выше потолка уходит ВЛОЖЕНИЕМ")
    # Между лимитом стенда (1000) и боевым 1400: зашитое 1400 отправило бы текстом.
    attachment("тело чуть длиннее лимита канала", lambda limit: limit + 200)
    attachment("тело ровно в потолок", lambda limit: BODY_CEILING)


def main() -> int:
    print(f"проба блока «Для человека»; предмет {SUBJECT}")
    if not SUBJECT.is_file() or not CHANNEL_GUARD.is_file():
        print(f"ИТОГО: путей 0, неудач 1 (нет предмета {SUBJECT} или читателя {CHANNEL_GUARD})")
        return 1
    unit_checks()
    refusal_checks()
    text_send_checks()
    text_refused_checks()
    attachment_checks()
    bad = sum(1 for _, ok in RESULTS if not ok)
    if not RESULTS:
        print("ИТОГО: путей 0, неудач 1 (пустой список проверок)")
        return 1
    print(f"ИТОГО: путей {len(RESULTS)}, неудач {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
