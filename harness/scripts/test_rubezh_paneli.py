#!/usr/bin/env python3
"""Пробы (тесты ДО кода) гейта `scripts/check-rubezh-paneli.py`.

Гейта ещё НЕТ. Эти пробы обязаны быть КРАСНЫМИ сейчас и зелёными после
реализации — они и есть контракт: ниже выписано, что гейт должен вернуть и
какими СЛОВАМИ назвать причину.

Что гейт обязан иметь (подпись из pretask.yaml):

    проверить(конф: Path, nginx_dir: Path, nginx_bin: str, unit: Path) -> int
    шаблон_цел(путь: Path) -> list[str]        # список НЕДОСТАЮЩИХ опор

Стенд. Каждая проба строит своё дерево в tempfile: `sites-available`,
`sites-enabled` (симлинками, как на живой машине), свой `harness.conf` с
PANEL_URL, свой юнит с `ExecStart ... --порт N`, подставные `nginx`,
`sudo` и `systemctl` в каталоге стенда. Боевые `/etc/nginx` и `/etc/harness`
не читаются и не пишутся НИ ОДНОЙ пробой: проверка обязана создавать своё
условие, а не совпадать с состоянием машины.

Вывод подставного nginx снят с живого nginx 13.09.2026, а не написан по
памяти (`nginx -T` на этой машине):

    nginx: the configuration file /etc/nginx/nginx.conf syntax is ok   (stderr)
    nginx: configuration file /etc/nginx/nginx.conf test is successful (stderr)
    # configuration file /etc/nginx/sites-enabled/produkt-ssl:           (stdout)

Важно: имя файла в дампе — путь ИЗ include (`sites-enabled/…`), симлинк nginx
не разворачивает. Гейт называет в отказе именно его.

Ожидание «rc не ноль» здесь недостаточно: 127 «команды нет» зеленит такую
пробу впустую. Поэтому каждый красный путь требует ещё и ПРИЧИНУ — слово из
собственного сообщения гейта. Слова собраны в СЛОВА_ОТКАЗА: реализация обязана
их печатать, менять их можно только вместе с этим файлом.

Запуск:
    python3 scripts/test_rubezh_paneli.py

Код выхода: 0 — все пути зелёные, 1 — есть красные.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile

КОРЕНЬ = pathlib.Path(__file__).resolve().parent.parent
ФАЙЛ_ГЕЙТА = КОРЕНЬ / "scripts" / "check-rubezh-paneli.py"
ШАБЛОН_РУБЕЖА = КОРЕНЬ / "harness" / "panel" / "nginx-panel.conf.in"

# Слова причины: по ним проба отличает «отказал за дело» от «упал как попало».
СЛОВА_ОТКАЗА = {
    "чужой_host": "default_server",   # панель отвечает на любой Host
    "второй_proxy": "proxy_pass",     # порт панели проксируют дважды
    "vnutr": "/vnutr/",               # внутренние маршруты не закрыты
    "x_real_ip": "X-Real-IP",         # адрес гостя неоткуда взять
    "нет_рубежа": "рубеж",            # PANEL_URL непуст, а рубежа нет
    "порт": "порт",                   # порт разошёлся с юнитом
    "не_требуется": "не требуется",   # PANEL_URL пуст — это НЕ отказ
}
# Слова, которых в зелёном случае быть НЕ должно (S-02: признак «nginx есть»
# по PATH слеп — под агентом `command -v nginx` пусто, а служба активна).
СЛОВА_СЛЕПОТЫ = ("nginx нет", "nginx не установлен", "nginx не найден")

ХОСТ = "panel.stend.example"
ПОРТ_ЮНИТА = 8787


# ── стенд ──────────────────────────────────────────────────────────────────

def _блок_панели(*, порт: int, default_server: bool, vnutr: str,
                 x_real_ip: bool) -> str:
    """443-блок панели. Все опоры — параметрами, чтобы снять по одной."""
    строка_vnutr = {
        "живой": "    location /vnutr/ { return 404; }",
        "закомментирован": "    # location /vnutr/ { return 404; }",
        "нет": "",
    }[vnutr]
    return "\n".join([
        "server {",
        "    listen 443 ssl default_server;" if default_server
        else "    listen 443 ssl;",
        f"    server_name {ХОСТ};",
        строка_vnutr,
        "    location / {",
        f"        proxy_pass http://127.0.0.1:{порт};",
        "        proxy_set_header Host $host;",
        "        proxy_set_header X-Real-IP $remote_addr;" if x_real_ip else "",
        "    }",
        "}",
    ])


def _блок_заглушки() -> str:
    """Ответ по умолчанию для неизвестных имён — обрыв, а не панель."""
    return ("server {\n"
            "    listen 443 ssl default_server;\n"
            "    server_name _;\n"
            "    return 444;\n"
            "}")


def _блок_80(*, с_опорами: bool) -> str:
    """Редирект-блок. `с_опорами` — больной случай S-08: опоры лежат ЗДЕСЬ,
    а подстрокой по файлу это неотличимо от рубежа."""
    хвост = ("    location /vnutr/ { return 404; }\n"
             "    proxy_set_header X-Real-IP $remote_addr;\n") if с_опорами else ""
    return ("server {\n"
            "    listen 80;\n"
            f"    server_name {ХОСТ};\n"
            f"{хвост}"
            "    location / { return 301 https://$host$request_uri; }\n"
            "}")


def конфиг_рубежа(*, порт: int = ПОРТ_ЮНИТА, заглушка: bool = True,
                  панель_default: bool = False, vnutr: str = "живой",
                  x_real_ip: bool = True, опоры_в_80: bool = False) -> str:
    куски = []
    if заглушка:
        куски.append(_блок_заглушки())
    куски.append(_блок_панели(порт=порт, default_server=панель_default,
                              vnutr=vnutr, x_real_ip=x_real_ip))
    куски.append(_блок_80(с_опорами=опоры_в_80))
    return "\n\n".join(куски) + "\n"


ПОДСТАВНОЙ_NGINX = """#!/usr/bin/env bash
# Подставной nginx стенда: печатает эффективную конфигурацию по -T, собирая
# её из файлов стенда. Формат снят с живого nginx 13.09.2026.
set -u
koren="{koren}"
rc_t={rc_t}

if [ "${{1:-}}" = "-t" ] || [ "${{1:-}}" = "-T" ]; then
    if [ "$rc_t" != 0 ]; then
        echo "nginx: [emerg] unknown directive \\"breh\\" in $koren/sites-enabled/chuzhoj:1" >&2
        echo "nginx: configuration file $koren/nginx.conf test failed" >&2
        exit "$rc_t"
    fi
    echo "nginx: the configuration file $koren/nginx.conf syntax is ok" >&2
    echo "nginx: configuration file $koren/nginx.conf test is successful" >&2
fi
[ "${{1:-}}" = "-T" ] || exit 0

echo "# configuration file $koren/nginx.conf:"
cat "$koren/nginx.conf"
echo
for f in "$koren"/sites-enabled/*; do
    [ -e "$f" ] || continue
    # Путь печатается КАК В include (симлинк не разворачивается) — так делает
    # живой nginx, и по этому имени гейт называет виновный файл.
    echo "# configuration file $f:"
    cat "$f"
    echo
done
exit 0
"""

ПОДСТАВНОЙ_SUDO = """#!/usr/bin/env bash
# Подставной sudo: держит боевую машину в стороне. `-n true` — проверка прав.
set -u
[ "${1:-}" = "-n" ] && shift
[ "${1:-}" = "true" ] && exit 0
exec "$@"
"""

# Формат снят с живой машины: `systemctl show harness-panel -p ExecStart`.
ПОДСТАВНОЙ_SYSTEMCTL = """#!/usr/bin/env bash
set -u
case "${{1:-}} ${{2:-}}" in
  "is-active nginx") echo active; exit 0 ;;
esac
if [ "${{1:-}}" = "show" ]; then
    echo 'ExecStart={{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 \
{proekt}/harness/panel/server.py --порт {port} ; ignore_errors=no ; \
start_time=[Sun 2026-09-13 13:58:00 +05] ; stop_time=[n/a] ; pid=1494640 ; \
code=(null) ; status=0/0 }}'
    exit 0
fi
exit 0
"""


class Стенд:
    """Дерево nginx + конфиг + юнит + подставные бинари в одном каталоге."""

    def __init__(self, tmp: str, *, panel_url: str = f"https://{ХОСТ}",
                 рубеж: str | None = None, чужой: str | None = None,
                 порт_юнита: int = ПОРТ_ЮНИТА, nginx_rc: int = 0,
                 проект: str = "/srv/harness-stend",
                 panel_url_в_паспорт: bool = False):
        корень = pathlib.Path(tmp)
        self.корень = корень
        self.nginx_dir = корень / "nginx"
        (self.nginx_dir / "sites-available").mkdir(parents=True)
        (self.nginx_dir / "sites-enabled").mkdir(parents=True)
        (self.nginx_dir / "nginx.conf").write_text(
            "user www-data;\nhttp {\n    include "
            f"{self.nginx_dir}/sites-enabled/*;\n}}\n", encoding="utf-8")

        if рубеж is not None:
            self.включить("harness-panel", рубеж)
        if чужой is not None:
            self.включить("produkt", чужой)

        self.конф = корень / "harness.conf"
        self.паспорт = корень / "install.conf"
        # PANEL_URL кладётся туда, куда просит проба: установка может написать
        # ключ и в паспорт, и гейт обязан читать ОБА файла (ревью кода F-10).
        if panel_url_в_паспорт:
            self.конф.write_text('PANEL_URL=""\n', encoding="utf-8")
            self.паспорт.write_text(f'PANEL_URL="{panel_url}"\n', encoding="utf-8")
        else:
            self.конф.write_text(f'PANEL_URL="{panel_url}"\n', encoding="utf-8")
            self.паспорт.write_text("PROJECT_DIR=/srv/harness-stend\n", encoding="utf-8")

        self.unit = корень / "harness-panel.service"
        self.unit.write_text(
            "[Service]\n"
            f"WorkingDirectory={проект}\n"
            f"ExecStart=/usr/bin/python3 {проект}/harness/panel/server.py "
            f"--порт {порт_юнита}\n", encoding="utf-8")

        self.bin = корень / "bin"
        self.bin.mkdir()
        self.nginx_bin = self._бинарь(
            self.bin / "nginx",
            ПОДСТАВНОЙ_NGINX.format(koren=self.nginx_dir, rc_t=nginx_rc))
        self._бинарь(self.bin / "sudo", ПОДСТАВНОЙ_SUDO)
        self._бинарь(self.bin / "systemctl",
                     ПОДСТАВНОЙ_SYSTEMCTL.format(proekt=проект, port=порт_юнита))

    @staticmethod
    def _бинарь(путь: pathlib.Path, текст: str) -> str:
        путь.write_text(текст, encoding="utf-8")
        путь.chmod(0o755)
        return str(путь)

    def включить(self, имя: str, текст: str) -> pathlib.Path:
        """Файл в sites-available + симлинк в sites-enabled, как на живой."""
        файл = self.nginx_dir / "sites-available" / имя
        файл.write_text(текст, encoding="utf-8")
        ссылка = self.nginx_dir / "sites-enabled" / имя
        ссылка.symlink_to(файл)
        return ссылка


# ── прогон гейта ───────────────────────────────────────────────────────────

def загрузить_гейт():
    """Гейт грузится ТЕМ ЖЕ файлом, каким его зовут ворота."""
    if not ФАЙЛ_ГЕЙТА.exists():
        return None
    спец = importlib.util.spec_from_file_location("check_rubezh_paneli",
                                                  ФАЙЛ_ГЕЙТА)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def прогон(гейт, стенд: Стенд) -> tuple[int, str]:
    """rc гейта и всё, что он напечатал (stdout+stderr одной строкой).

    PATH подменяется на каталог стенда: подставные sudo и systemctl обязаны
    перехватить вызовы, иначе проба играла бы на боевой машине.
    """
    было = os.environ.get("PATH", "")
    буфер = io.StringIO()
    try:
        os.environ["PATH"] = f"{стенд.bin}:/usr/bin:/bin"
        os.environ["HARNESS_INSTALL_CONF"] = str(стенд.паспорт)
        with contextlib.redirect_stdout(буфер), contextlib.redirect_stderr(буфер):
            rc = гейт.проверить(стенд.конф, стенд.nginx_dir,
                                стенд.nginx_bin, стенд.unit)
    finally:
        # Возврат ИМЕННО в finally: после падения гейта боевой PATH иначе
        # остался бы подменённым до конца процесса.
        os.environ["PATH"] = было
        os.environ.pop("HARNESS_INSTALL_CONF", None)
    return rc, буфер.getvalue()


def есть(текст: str, слово: str) -> int:
    """1 — слово названо, 0 — нет. Абсолютное число, а не bool в отчёте."""
    return 1 if слово.lower() in текст.lower() else 0


def нет_слов(текст: str, слова) -> int:
    return 1 if all(с.lower() not in текст.lower() for с in слова) else 0


# ── сами пробы ─────────────────────────────────────────────────────────────

def самотест() -> int:
    ok = True
    путей = 0

    def проба(имя: str, ждём, факт):
        """Путь засчитан только при совпадении с АБСОЛЮТНЫМ ожиданием."""
        nonlocal ok, путей
        путей += 1
        if факт == ждём:
            print(f"  ок    {имя}")
        else:
            print(f"  ПЛОХО {имя}: ждали «{ждём}», вышло «{факт}»")
            ok = False

    гейт = загрузить_гейт()
    if гейт is None:
        print(f"  ПЛОХО реализации ещё нет: {ФАЙЛ_ГЕЙТА}")
        print("SELFTEST: КРАСНЫЙ (путей 0 — гейт не написан)")
        return 1
    if not (hasattr(гейт, "проверить") and hasattr(гейт, "шаблон_цел")):
        print("SELFTEST: КРАСНЫЙ (путей 0 — нет функций «проверить»/«шаблон_цел»)")
        return 1

    # ── 1. БОЛЬНОЙ (живой 13.09.2026) ──────────────────────────────────────
    # `curl -sk -H 'Host: bogus.example' https://127.0.0.1/` отдавал 200 и
    # страницу панели: блок панели шёл первым в sites-enabled и стал ответом
    # по умолчанию для 443. «Свой origin», на который опирается server.py,
    # не выполнялся. Дыра ЖИВАЯ, не будущая — потому проба первая.
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа(заглушка=False, панель_default=True))
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ (живой 13.09): панель отвечает на чужой Host — rc", 1, rc)
        проба("БОЛЬНОЙ: назван виновный файл", 1, есть(вывод, "sites-enabled/harness-panel"))
        проба("БОЛЬНОЙ: названа причина словом", 1,
              есть(вывод, СЛОВА_ОТКАЗА["чужой_host"]))

    # ── 1б. БОЛЬНОЙ (живой 13.09.2026, точный снимок): default_server не
    # объявлен НИКЕМ, и панель — первый блок по алфавиту. В её собственном
    # конфиге слова default_server при этом НЕТ: ровно так выглядел боевой
    # рубеж, и подстрокой такую дыру не поймать.
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа(заглушка=False, панель_default=False))
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: заглушки нет, панель не помечена — rc", 1, rc)
        проба("БОЛЬНОЙ: сказано, что default_server никем не объявлен", 1,
              есть(вывод, СЛОВА_ОТКАЗА["чужой_host"]))

    # ── 2. БОЛЬНОЙ: порт панели проксирует ЕЩЁ и конфиг продукта ───────────
    # Вывод панели через origin продукта — та самая беда C6: судить надо
    # эффективную конфигурацию целиком, а не один файл (S-04).
    with tempfile.TemporaryDirectory() as tmp:
        чужой = ("server {\n    listen 443 ssl;\n    server_name produkt.example;\n"
                 "    location /panel/ {\n"
                 f"        proxy_pass http://127.0.0.1:{ПОРТ_ЮНИТА};\n"
                 "    }\n}\n")
        с = Стенд(tmp, рубеж=конфиг_рубежа(), чужой=чужой)
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: второй proxy_pass на порт панели — rc", 1, rc)
        проба("БОЛЬНОЙ: названо имя ЧУЖОГО файла", 1,
              есть(вывод, "sites-enabled/produkt"))

    # ── 3. БОЛЬНОЙ: /vnutr/ закомментирован (S-08) ─────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа(vnutr="закомментирован"))
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: location /vnutr/ закомментирован — rc", 1, rc)
        проба("БОЛЬНОЙ: названа опора /vnutr/", 1, есть(вывод, СЛОВА_ОТКАЗА["vnutr"]))

    # ── 4. БОЛЬНОЙ: опоры лежат только в редирект-блоке :80 (S-08) ─────────
    # Подстрокой по файлу такой конфиг неотличим от целого рубежа.
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа(vnutr="нет", x_real_ip=False,
                                           опоры_в_80=True))
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: директивы только в блоке :80 — rc", 1, rc)
        проба("БОЛЬНОЙ: названа опора X-Real-IP", 1,
              есть(вывод, СЛОВА_ОТКАЗА["x_real_ip"]))

    # ── 5. БОЛЬНОЙ: порт в proxy_pass разошёлся с установленным юнитом ─────
    # Работает то, что СТОИТ, а не то, что лежит в шаблоне (S-07).
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа(порт=9999), порт_юнита=8787)
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: порт рубежа 9999 против юнита 8787 — rc", 1, rc)
        проба("БОЛЬНОЙ: назван порт рубежа", 1, есть(вывод, "9999"))
        проба("БОЛЬНОЙ: назван порт юнита", 1, есть(вывод, "8787"))

    # ── 6. БОЛЬНОЙ: ссылка владельцу выдана, а рубежа нет вовсе ────────────
    # Граница — по PANEL_URL, а не по наличию nginx (S-03).
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=None)
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: PANEL_URL непуст, рубежа нет — rc", 1, rc)
        проба("БОЛЬНОЙ: сказано про рубеж словом", 1,
              есть(вывод, СЛОВА_ОТКАЗА["нет_рубежа"]))

    # ── 7. PANEL_URL пуст — рубеж не требуется ─────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, panel_url="", рубеж=None)
        rc, вывод = прогон(гейт, с)
        проба("PANEL_URL пуст — rc", 0, rc)
        проба("PANEL_URL пуст — причина названа словом", 1,
              есть(вывод, СЛОВА_ОТКАЗА["не_требуется"]))
        # Живая проба в контейнере чистой установки 13.09.2026: гейт печатал
        # «рубеж на месте» там, где рубежа нет вовсе — отчёт врал словами при
        # верном коде возврата.
        проба("PANEL_URL пуст — гейт НЕ говорит «рубеж на месте»", 1,
              нет_слов(вывод, ("рубеж на месте",)))

    # ── 8. Здоровый рубеж: гейт не краснеет впустую ────────────────────────
    # Без этого пути все красные пробы зеленели бы от любой поломки гейта.
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа())
        rc, вывод = прогон(гейт, с)
        проба("здоровый рубеж — rc", 0, rc)
        проба("здоровый рубеж — гейт молчит про default_server", 1,
              нет_слов(вывод, ("default_server",)))

    # ── 9. nginx вне PATH, но активен (S-02) ───────────────────────────────
    # Под агентом `command -v nginx` пусто (бинарь в /usr/sbin), при этом
    # `systemctl is-active nginx` → active. Гейт с признаком по PATH на
    # боевой машине зелен всегда — и не видит ничего.
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=конфиг_рубежа())
        (с.bin / "nginx").rename(с.корень / "nginx-vne-path")
        с.nginx_bin = str(с.корень / "nginx-vne-path")
        rc, вывод = прогон(гейт, с)
        проба("nginx вне PATH, но активен — rc", 0, rc)
        проба("nginx вне PATH — гейт НЕ говорит «nginx нет»", 1,
              нет_слов(вывод, СЛОВА_СЛЕПОТЫ))

    # ── 10. Шаблон судится отдельно ────────────────────────────────────────
    проба("шаблон рубежа лежит в репозитории", True, ШАБЛОН_РУБЕЖА.exists())
    if ШАБЛОН_РУБЕЖА.exists():
        проба("целый шаблон — недостающих опор 0", 0,
              len(гейт.шаблон_цел(ШАБЛОН_РУБЕЖА)))
        with tempfile.TemporaryDirectory() as tmp:
            битый = pathlib.Path(tmp) / "nginx-panel.conf.in"
            текст = ШАБЛОН_РУБЕЖА.read_text(encoding="utf-8")
            битый.write_text(
                "\n".join(с for с in текст.splitlines()
                          if "X-Real-IP" not in с) + "\n", encoding="utf-8")
            пропало = гейт.шаблон_цел(битый)
            проба("шаблон без одной опоры — недостающих ровно 1", 1, len(пропало))
            проба("шаблон без одной опоры — опора НАЗВАНА", 1,
                  есть(" ".join(пропало), СЛОВА_ОТКАЗА["x_real_ip"]))

    # ── 11. Рубеж целиком на 8443: на 443 не слушает НИКТО ────────────────
    # «listen 8443» содержит подстроку «443», и суд по подстроке признавал
    # такой рубеж целым (ревью кода 13.09.2026, F-05).
    with tempfile.TemporaryDirectory() as tmp:
        рубеж = конфиг_рубежа().replace("listen 443 ssl", "listen 8443 ssl")
        с = Стенд(tmp, рубеж=рубеж)
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: весь рубеж на 8443 — rc", 1, rc)
        проба("БОЛЬНОЙ: 8443 не засчитан за 443", 1, есть(вывод, "443-блока"))

    # ── 12. Второй блок с тем же именем: старый оставили рядом с новым ────
    # Судился только первый (F-06), и открытый наружу /vnutr/ во втором блоке
    # гейт не видел.
    with tempfile.TemporaryDirectory() as tmp:
        второй = ("server {\n    listen 8443 ssl;\n"
                  f"    server_name {ХОСТ};\n    location / {{\n"
                  f"        proxy_pass http://127.0.0.1:{ПОРТ_ЮНИТА};\n"
                  "    }\n}\n")
        с = Стенд(tmp, рубеж=конфиг_рубежа() + "\n" + второй)
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: второй блок с тем же именем судится тоже — rc", 1, rc)
        проба("БОЛЬНОЙ: названа опора /vnutr/ второго блока", 1,
              есть(вывод, СЛОВА_ОТКАЗА["vnutr"]))

    # ── 13. PANEL_URL лежит в ПАСПОРТЕ установки, а не в harness.conf ─────
    # Шаг установки (карточка hr.установка-не-ставит-панель) пишет ключ туда;
    # гейт, читающий один файл, молчал бы при выведенной наружу панели (F-10).
    with tempfile.TemporaryDirectory() as tmp:
        с = Стенд(tmp, рубеж=None, panel_url_в_паспорт=True)
        rc, вывод = прогон(гейт, с)
        проба("БОЛЬНОЙ: PANEL_URL в паспорте, рубежа нет — rc", 1, rc)
        проба("БОЛЬНОЙ: не сказано «не требуется»", 1,
              нет_слов(вывод, (СЛОВА_ОТКАЗА["не_требуется"],)))

    итог = (f"SELFTEST: зелёный ({путей} путей; первым — живой больной случай "
            "13.09.2026: панель отвечала на чужой Host)"
            if ok else f"SELFTEST: КРАСНЫЙ (путей {путей})")
    print(итог)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(самотест())
