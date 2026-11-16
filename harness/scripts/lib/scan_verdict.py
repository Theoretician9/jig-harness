#!/usr/bin/env python3
"""Вердикт по выводу сканера образа (этапы D/I выката): Trivy JSON на stdin.

Коды: 0 — блокирующих нет; 1 — есть CRITICAL с исправлением (FixedVersion не пуст);
2 — вывод не прочитан. «Не прочитано» не равно «чисто»: зелёное на непрочитанном
выводе выкатило бы образ без скана (И-4), а код 1 при падении самого разбора
остановил бы выкат без причины, поэтому коды 0/1/2 точные, а падение — тоже 2.

В stdout: строка «блокирует: …» на каждую CRITICAL с исправлением (id, пакет,
версия с исправлением) и строка «CRITICAL без исправления: …» на каждую без него:
исправления нет, выкат его не принесёт и не уберёт, но молчать о ней нельзя.
Объект с SchemaVersion без Results — чистый образ: Trivy опускает пустой Results.
"""
import json
import sys

OK, BLOCKING, UNREAD = 0, 1, 2


class Unreadable(Exception):
    pass


def vulnerabilities(report):
    if not isinstance(report, dict):
        raise Unreadable("не объект")
    if "Results" not in report:
        if "SchemaVersion" not in report:
            raise Unreadable("нет ни SchemaVersion, ни Results")
        return
    results = report["Results"]
    if results is None:
        return
    if not isinstance(results, list):
        raise Unreadable("Results не список")
    for result in results:
        if not isinstance(result, dict):
            raise Unreadable("элемент Results не объект")
        found = result.get("Vulnerabilities")
        if found is None:
            continue
        if not isinstance(found, list) or not all(isinstance(v, dict) for v in found):
            raise Unreadable("Vulnerabilities не список объектов")
        yield from found


def judge(report):
    blocking, unfixed = [], []
    for vuln in vulnerabilities(report):
        if vuln.get("Severity") != "CRITICAL":
            continue
        who = f'{vuln.get("VulnerabilityID", "?")} {vuln.get("PkgName", "?")}'
        fixed = vuln.get("FixedVersion")
        if fixed:
            blocking.append(f"блокирует: {who}, исправлено в {fixed}")
        else:
            unfixed.append(f"CRITICAL без исправления: {who}")
    return blocking, unfixed


def main():
    try:
        blocking, unfixed = judge(json.loads(sys.stdin.read()))
    # RecursionError — глубокая вложенность: json падает не ValueError, а это тоже «не прочитан».
    except (ValueError, RecursionError, Unreadable) as exc:
        print(f"скан не прочитан: {exc}")
        return UNREAD
    for line in blocking + unfixed:
        print(line)
    return BLOCKING if blocking else OK


if __name__ == "__main__":
    sys.exit(main())
