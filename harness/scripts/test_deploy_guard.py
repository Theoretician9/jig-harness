#!/usr/bin/env python3
"""Больной случай 04.10.2026: номер PID из старого лока переиспользован.

Лок выката лежал с 11.09 с номером 3037008, в этот день тот же номер достался
службе api — ворота сказали «занято: выкат начат 2026-09-11» и отменили push.
Проверка СОЗДАЁТ условие сама: берёт ЖИВОЙ процесс (свой собственный) и кладёт
лок, чей mtime СТАРШЕ старта этого процесса.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time

# Корень — рядом с самим файлом: проба обязана судить ТОТ прибор, с которым
# лежит, иначе раскладка пакета оставит её судить чужую копию.
КОРЕНЬ = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
ПРИБОР = КОРЕНЬ / "scripts" / "deploy_guard.py"

плохо = 0


def судить(зачем: str, вышло, ждём) -> None:
    global плохо
    if вышло == ждём:
        print(f"  ок    {зачем}")
    else:
        плохо += 1
        print(f"  ПЛОХО {зачем}: вышло {вышло!r}, ждали {ждём!r}")


def стендовый_конфиг(двор: pathlib.Path) -> pathlib.Path:
    """Живой harness.conf плюс PUSH_NEEDS_OWNER=0 последней строкой (последняя
    побеждает): ответ ворот «можно» иначе зависит от ручки согласия машины, и
    проба на установке с PUSH_NEEDS_OWNER=1 краснела бы не от своего правила."""
    sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
    import config as конф
    живой = pathlib.Path(конф.путь_harness_conf())
    текст = живой.read_text(encoding="utf-8") if живой.is_file() else ""
    свой = двор / "harness.conf"
    свой.write_text(текст + '\nPUSH_NEEDS_OWNER=0\nPRODUCT_PATH=""\nPRODUCT_DIR=""\n', encoding="utf-8")
    return свой


def стендовый_install(двор: pathlib.Path, проект: pathlib.Path) -> pathlib.Path:
    """Живой install.conf, но PROJECT_DIR — пустой каталог двора: иначе долгий
    прогон в живом дереве проекта ворота звали бы занятостью стенда."""
    import config as конф
    живой = pathlib.Path(конф.путь_install_conf())
    текст = живой.read_text(encoding="utf-8") if живой.is_file() else ""
    свой = двор / "install.conf"
    свой.write_text(текст + f'\nPROJECT_DIR="{проект}"\n', encoding="utf-8")
    return свой


def ничей_uid() -> int:
    """uid, которому не принадлежит ни один процесс машины."""
    занятые = set()
    for p in pathlib.Path("/proc").iterdir():
        try:
            if p.name.isdigit():
                занятые.add(p.stat().st_uid)
        except OSError:
            continue
    uid = 60000
    while uid in занятые:
        uid += 1
    return uid


def ворота(двор: pathlib.Path) -> tuple[int, str]:
    """(код, ответ) ворот. Код судится вместе со словом: незнакомый ключ
    отвечает rc 2, и проба, судившая одно слово, принимала отказ разбора за
    «не блокирует»."""
    # Случаи судят ЛОК, поэтому занятость процессами машины отрезана: «свой» uid —
    # ничей, дерево проекта — пустой каталог двора, /proc для подсчёта смен —
    # пустой. DEPLOY_BUSY_MIN_SEC=0: помеха ниже, созданная самой пробой, видна
    # сразу — без отреза она и краснит случаи о локе.
    пусто = двор / "пусто"
    пусто.mkdir(exist_ok=True)
    среда = dict(os.environ, HARNESS_LOG_DIR=str(двор), HARNESS_CONF=str(стендовый_конфиг(двор)),
                 HARNESS_INSTALL_CONF=str(стендовый_install(двор, пусто)),
                 HARNESS_OWN_UID=str(ничей_uid()), AGENT_PROC_ROOT=str(пусто),
                 DEPLOY_BUSY_MIN_SEC="0")
    готово = subprocess.run([sys.executable, str(ПРИБОР), "--действие", "push"],
                            capture_output=True, text=True, env=среда, check=False)
    return готово.returncode, готово.stdout + готово.stderr


with tempfile.TemporaryDirectory() as врем:
    двор = pathlib.Path(врем)
    локи = двор / "locks"
    локи.mkdir(parents=True)

    # ЧУЖОЙ живой процесс. Свой номер здесь не годится: ворота законно считают
    # лок своего предка «не блокирует» (иначе выкат блокировал бы сам себя), и
    # проба на своём PID судила бы другую ветку. `setsid` отвязывает процесс от
    # дерева смены — он живой и не предок.
    отвязанный = subprocess.Popen(["setsid", "sleep", "60"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    живой = отвязанный.pid

    # Помеха машины, созданная здесь: свой «claude» вне tmux агента, в дереве
    # проекта (как долгий прогон ночных ворот). Улика
    # 11.11.2026: забытая заглушка claude пробы переключения смены жила 4 часа,
    # и ворота стенда отвечали «занято: процесс …» — случаи о локе краснели
    # от машины, а не от своего правила.
    помеха = subprocess.Popen(["setsid", "bash", "-c", "exec -a claude sleep 60"], cwd=str(КОРЕНЬ),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)

    # БОЛЬНОЙ СЛУЧАЙ: лок СТАРШЕ старта этого процесса (номер переиспользован).
    старый = локи / "deploy.lock"
    старый.write_text(f"{живой}\nвыкат deploy.sh, начат 2026-09-11T18:18:08+05:00\n",
                      encoding="utf-8")
    час_назад = time.time() - 3600
    os.utime(старый, (час_назад, час_назад))
    код, ответ = ворота(двор)
    судить("БОЛЬНОЙ СЛУЧАЙ: номер переиспользован — лок не блокирует (rc 0)",
           (код, "переиспользован" in ответ, "занято: лок deploy.lock" in ответ), (0, True, False))

    # ОБРАТНАЯ СТОРОНА: лок МОЛОЖЕ старта процесса — настоящий держатель, блокирует.
    старый.write_text(f"{живой}\nвыкат deploy.sh, начат только что\n", encoding="utf-8")
    сейчас = time.time()
    os.utime(старый, (сейчас, сейчас))
    код, ответ = ворота(двор)
    судить("живой держатель моложе лока — ворота заняты (rc 1)",
           (код, "занято: лок deploy.lock" in ответ), (1, True))

    # Мёртвый номер по-прежнему протухший (прежнее поведение цело).
    мёртвый = 4194303
    старый.write_text(f"{мёртвый}\nвыкат deploy.sh\n", encoding="utf-8")
    код, ответ = ворота(двор)
    судить("мёртвый номер — протухший лок, как было (rc 0)",
           (код, "держатель мёртв" in ответ), (0, True))

    # БОЛЬНОЙ СЛУЧАЙ 30.10.2026 (LXC, соседний проект): lxcfs подменяет /proc/uptime
    # временем жизни КОНТЕЙНЕРА, а старт в /proc/<pid>/stat считается от загрузки
    # ХОСТА — живой держатель выходил «моложе лока», и любой лок звался
    # протухшим. Условие создаётся здесь: /proc/uptime отвечает как lxcfs.
    import importlib.util
    os.environ["HARNESS_LOG_DIR"] = str(двор)
    spec = importlib.util.spec_from_file_location("ворота_под_пробой", ПРИБОР)
    прибор = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(прибор)
    прежнее_чтение = pathlib.Path.read_text

    def как_lxcfs(путь, *a, **k):
        return "100.00 100.00\n" if str(путь) == "/proc/uptime" else прежнее_чтение(путь, *a, **k)

    # Та же подмена у долгих работ: возраст выходил отрицательным, и долгий
    # прогон под LXC занятостью не считался. Работа своя, с именем pytest.
    работа = subprocess.Popen(["bash", "-c", "exec -a pytest sleep 60"], cwd=str(КОРЕНЬ),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    прибор.ДОЛГО_СЕК = 0
    pathlib.Path.read_text = как_lxcfs
    try:
        моложе = прибор._процесс_моложе(живой, time.time())
        долгие = прибор.долгие_процессы()
    finally:
        pathlib.Path.read_text = прежнее_чтение
        работа.terminate()
    судить("LXC: /proc/uptime контейнера — живой держатель не «моложе лока»", моложе, False)
    судить("LXC: /proc/uptime контейнера — долгая работа видна занятостью",
           any(с.startswith(f"процесс {работа.pid}:") for с in долгие), True)

    отвязанный.terminate()
    помеха.terminate()

print(f"ИТОГ: плохих {плохо}")
sys.exit(1 if плохо else 0)
