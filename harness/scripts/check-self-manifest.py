#!/usr/bin/env python3
"""Манифест эталона в пакете сходится с файлами слоя 1 — те же правила, что у self-seal.sh.

Раскладка нашей установки печатает эталон личности из пакета (sudo self-seal <пакет>),
а манифест пакета пишет только дверь согласия на публикацию. Правка роли без
пересчёта манифеста всплывала бы отказом печати на раскладке; здесь — на коммите.

  check-self-manifest.py [--пакет DIR]   (без довода — config.katalog_paketa())
Код: 0 — сходится, 1 — расходится или манифеста нет.
"""
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

MANIFEST = "MANIFEST.sha256"
OUTSIDE = {MANIFEST, "instance.yaml"}   # instance.yaml — слой 2, у каждой установки свой
LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


def problems(self_dir: Path) -> list[str]:
    manifest = self_dir / MANIFEST
    if not manifest.is_file():
        return [f"нет манифеста {manifest}"]
    listed: dict[str, str] = {}
    bad: list[str] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        # Путь вне self/ («..», корень, instance.yaml) не найдётся среди файлов ниже — тоже красный.
        match = LINE.match(line)
        if not match:
            bad.append(f"строка манифеста негодна: {line}")
            continue
        listed[match.group(2)] = match.group(1)
    found = {p.relative_to(self_dir).as_posix() for p in self_dir.rglob("*") if not p.is_dir()} - OUTSIDE
    bad += [f"файла нет, а в манифесте есть: {rel}" for rel in sorted(listed.keys() - found)]
    bad += [f"файл не в манифесте: {rel}" for rel in sorted(found - listed.keys())]
    bad += [f"sha256 не сошёлся: {rel}" for rel in sorted(listed.keys() & found)
            if hashlib.sha256((self_dir / rel).read_bytes()).hexdigest() != listed[rel]]
    return bad


def main(argv: list[str]) -> int:
    package = Path(argv[argv.index("--пакет") + 1]) if "--пакет" in argv else config.katalog_paketa()
    self_dir = package / "harness" / "self"
    bad = problems(self_dir)
    if not bad:
        print(f"[манифест эталона] сходится: {self_dir / MANIFEST}")
        return 0
    print(f"[манифест эталона] РАСХОДИТСЯ в {self_dir} — печать эталона (self-seal) на раскладке откажет:")
    print("\n".join(f"  {b}" for b in bad))
    # Подсказка — тот же счёт, что у publish.sh --манифест-слоя1, но без publish.sh: его зовёт
    # только оркестратор, а слой 1 правят и помощники. Манифест пишется через файл вне self/.
    print(f"  Пересчитать (по файлам на диске — сперва закоммить слой 1): cd {self_dir} && "
          "find . -type f ! -path ./MANIFEST.sha256 ! -path ./instance.yaml -printf '%P\\n' | LC_ALL=C sort "
          "| xargs -d '\\n' -r sha256sum > ../.manifest.new && mv -f ../.manifest.new MANIFEST.sha256")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
