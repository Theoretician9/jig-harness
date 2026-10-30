#!/usr/bin/env python3
"""Проба гейта отправителей ЦЕЛИКОМ: main() на стендовых данных.

Зачем. Храповик `harness/config/owner-senders.yaml` судится в обе стороны:
мёртвая заявка (файл заявлен прямым путём, а прямого пути в нём нет) молча
разрешила бы вернуть обход. Самотест гейта судит ФУНКЦИЮ `мёртвые_заявки`;
что main() её зовёт и краснеет, судит эта проба — мутация «main не зовёт
судью» оставила бы самотест зелёным.

Стенд — свой git-двор во временном каталоге; гейт грузится модулем, и его
корень и путь данных указываются на стенд (своей двери среды у гейта нет).

Дверь пробы: OWNER_SENDERS_GATE — путь копии гейта (мутации).
Прогон: python3 scripts/test_owner_senders.py
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile

GATE = pathlib.Path(os.environ.get("OWNER_SENDERS_GATE")
                    or pathlib.Path(__file__).resolve().parent / "check-owner-senders.py")
# Признаки гейта собраны из кусков: целиком они сделали бы саму пробу
# отправителем владельцу в глазах того же гейта на живом дереве.
DOOR = 'curl "https://api.' + 'telegram.org/bot$T/' + 'sendMessage" # --несмотря-на-' + 'лимит\n'
QUIET = 'bash scripts/tg_send.sh "весть"\n'

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


def load_gate():
    spec = importlib.util.spec_from_file_location("owner_senders_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_main(yard: pathlib.Path, data: str) -> tuple[int, str]:
    """main() гейта на стенде: корень и данные — стендовые, argv без ключей."""
    (yard / "data.yaml").write_text(data, encoding="utf-8")
    gate = load_gate()
    gate.КОРЕНЬ, gate.ДАННЫЕ = yard, yard / "data.yaml"
    out, err = io.StringIO(), io.StringIO()
    argv = sys.argv
    sys.argv = [str(GATE)]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gate.main()
    finally:
        sys.argv = argv
    return rc, out.getvalue() + err.getvalue()


def named(out: str, path: str, word: str) -> bool:
    """Находка — одна строка, где названы и файл, и причина."""
    return any(path in line and word in line for line in out.splitlines())


def yard_with(base: pathlib.Path) -> pathlib.Path:
    (base / "scripts").mkdir()
    (base / "scripts" / "tg_send.sh").write_text(DOOR, encoding="utf-8")
    (base / "scripts" / "tiho.sh").write_text(QUIET, encoding="utf-8")
    subprocess.run(["git", "-C", str(base), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(base), "add", "-A"], check=True)
    return base


# Раздел model_task (что за вестью каждого отправителя) гейт судит с 26.10:
# здоровый стенд обязан его нести, иначе красен по чужому правилу. Стоит
# ПЕРВЫМ: случаи ниже дописывают строки в хвост последнего раздела.
HEALTHY = ("model_task:\n  scripts/tiho.sh:\n    - {kind: owner-only, why: стенд}\n"
           "debt_max: 0\n"
           "pryamoj_api:\n  scripts/tg_send.sh: дверь\n"
           "nesmotrya_na_limit:\n  scripts/tg_send.sh: дверь\n")


def cases() -> None:
    if not GATE.is_file():
        check("гейт отправителей существует", False, f"нет {GATE}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        yard = yard_with(pathlib.Path(tmp))

        rc, out = run_main(yard, HEALTHY)
        check("здоровый стенд — main() rc 0 и «чисто»", rc == 0 and "чисто" in out,
              f"rc {rc}: {out.strip()[:300]}")

        rc, out = run_main(yard, HEALTHY.replace(
            "nesmotrya_na_limit:", "  scripts/tiho.sh: запасной путь\nnesmotrya_na_limit:"))
        check("БОЛЬНОЙ 3.15: мёртвая заявка прямого пути — main() rc 1", rc == 1,
              f"rc {rc}: {out.strip()[:300]}")
        check("БОЛЬНОЙ 3.15: мёртвая заявка названа файлом и словом «заявлен прямым путём»",
              named(out, "scripts/tiho.sh", "заявлен прямым путём"), out.strip()[:300])
        check("БОЛЬНОЙ 3.15: сводка считает мёртвую заявку", "мёртвых заявок 1" in out,
              out.strip()[-200:])

        rc, out = run_main(yard, HEALTHY + "  scripts/tiho.sh: право без ключа\n")
        check("БОЛЬНОЙ 3.15: мёртвое право говорить при лимите — main() rc 1 с именем",
              rc == 1 and named(out, "scripts/tiho.sh", "заявлено право"),
              f"rc {rc}: {out.strip()[:300]}")

        rc, out = run_main(yard, HEALTHY.replace(
            "nesmotrya_na_limit:", "  scripts/ushel.sh: файла больше нет\nnesmotrya_na_limit:"))
        check("БОЛЬНОЙ 3.15: заявка на файл вне гита — main() rc 1 с именем",
              rc == 1 and named(out, "scripts/ushel.sh", "заявлен прямым путём"),
              f"rc {rc}: {out.strip()[:300]}")


def main() -> int:
    print("[стенд: гейт целиком]")
    cases()
    if failures:
        print(f"SELFTEST: КРАСНЫЙ ({failures} из {paths} путей)")
        return 1
    print(f"SELFTEST: зелёный ({paths} путей; первый больной — мёртвая заявка sentinel.sh, 18.10)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
