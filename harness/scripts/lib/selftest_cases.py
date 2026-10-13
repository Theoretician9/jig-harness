"""Общий помощник самотестов: случай «ждём/вышло → ок/ПЛОХО» и его счёт.

Один порядок доводов (имя, ждём, вышло) на все самотесты, и счёт путей ведёт
помощник: итоговая строка не держит чисел, вписанных руками. Пара —
selftest_cases.sh.
"""


class Cases:
    def __init__(self) -> None:
        self.paths = 0
        self.failures = 0

    def check(self, name: str, expected, actual) -> None:
        self.paths += 1
        if actual == expected:
            print(f"  ок    {name}")
        else:
            self.failures += 1
            print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")

    @property
    def ok(self) -> bool:
        return self.failures == 0
