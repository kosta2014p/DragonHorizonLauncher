"""
Лаунчер модпака Minecraft.
Скачивает актуальную сборку с GitHub Releases, ставит нужный
Minecraft/modloader через minecraft-launcher-lib и запускает игру.
Интерфейс — HTML/CSS/JS (папка web/), отображается через pywebview.

Формат launcher.json, который должен лежать в релизе (как отдельный asset):
{
  "minecraft_version": "1.20.1",
  "loader": "forge",          // "forge" | "fabric" | "quilt" | "vanilla"
  "loader_version": "47.2.0", // версия лоадера (для vanilla можно пустую строку)
  "build_version": "1.0.3"    // версия сборки, чтобы понимать когда обновлять
}
"""

import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import time
import zipfile
import collections
import subprocess
import threading
import webbrowser
import concurrent.futures
try:
    import psutil
except ImportError:
    psutil = None
try:
    import pystray
    HAS_TRAY = True
except ImportError:
    pystray = None
    HAS_TRAY = False
Image = None  # PIL загружается лениво при создании трея
from pathlib import Path

# Qt-бэкенд: DLL QtWebEngine лежат в корне пакета PySide6 и не видны дочерним
# процессам — без этого QtWebEngineProcess.exe падает с "не найдена Qt6WebEngineCore.dll".
try:
    import PySide6
    os.environ["PATH"] = os.path.dirname(PySide6.__file__) + os.pathsep + os.environ.get("PATH", "")
    # Store-Python запускает процессы в урезанной песочнице, поэтому WebEngine без этого не стартует
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    # Программный рендеринг: в песочнице Store-Python и на ПК без рабочего GL
    # Chromium иначе сыплет "Failed to create GLES3 context" и окно чёрное
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QSG_RENDER_LOOP", "basic")
    # быстрый старт на слабых ПК: без попыток достучаться до GPU
    # (Chromium сам падает на софтверный рендер) + тихий лог
    # + без фоновой активности Chromium
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--disable-gpu --disable-gpu-sandbox --in-process-gpu "
        "--disable-logging --log-level=3 --noerrdialogs "
        "--disable-background-networking --disable-sync",
    )
except ImportError:
    pass

import requests
import webview

# ------------------------- НАСТРОЙКИ (поменяй под себя) -------------------------

GITHUB_OWNER = "kosta2014p"
GITHUB_REPO = "DragonHorizonLauncherModPack"   # пак: build.zip + launcher.json
LAUNCHER_REPO = "DragonHorizonLauncher"        # лаунчер: Setup exe (отдельно, чтобы
                                               # пак-релизы не маскировали обновления)
GITHUB_TOKEN = ""  # Репозиторий публичный, токен не нужен

# Версия лаунчера — держать в синхроне с MyAppVersion в installer.iss.
# Тег релиза GitHub = эта версия (v1.2.0), ассет — DragonHorizonLauncher-Setup-*.exe.
LAUNCHER_VERSION = "1.5.0"

APP_NAME = "DragonHorizonLauncher"
DISPLAY_NAME = "DragonHorizonLauncher"  # то, что видно в шапке окна

# Адрес сервера, к которому игра подключится сразу после запуска.
# SRV-запись _minecraft._tcp.mc.dragonhorizon.online отдаёт порт 25642,
# поэтому указываем его явно (без этого пинг шёл на 25565 и всегда был "офлайн").
SERVER_ADDRESS = "mc.dragonhorizon.online"
SERVER_PORT = 25642

# ВАЖНО: используем стандартную папку .minecraft в %APPDATA%,
# иначе установщик Fabric её не находит и падает с "No launcher directory found".
BASE_DIR = Path(os.getenv("APPDATA") or Path.home()) / ".minecraft"
MINECRAFT_DIR = BASE_DIR
INSTANCE_DIR = MINECRAFT_DIR
VERSION_FILE = BASE_DIR / "installed_version.json"
FILES_MANIFEST = BASE_DIR / "files_hashes.json"
UI_SETTINGS_FILE = BASE_DIR / "launcher_ui.json"
STATS_FILE = BASE_DIR / "launcher_stats.json"
ONLINE_HISTORY_FILE = BASE_DIR / "online_history.json"

# Вход в лаунчер: учётка та же, что на сайте (её выдаёт администратор).
# Токен сессии (та же HMAC-cookie dh_session, что у сайта) храним локально.
SITE_URL = "https://dragonhorizon.ru"
AUTH_FILE = BASE_DIR / "launcher_auth.json"

WINDOW_SIZE = (860, 560)
WINDOW_MIN_SIZE = (860, 560)

# Авто-выбор GUI: qt если PySide6+qtpy есть, иначе EdgeChromium (не требует Qt)
try:
    import PySide6
    import qtpy
    FORCE_GUI = "qt"
except ImportError:
    FORCE_GUI = None

DEBUG = False  # True — открывает окно с DevTools/консолью для диагностики

# Флаг отмены скачивания (4,6) — потокобезопасно
_cancel_event = threading.Event()

def request_cancel():
    _cancel_event.set()

def reset_cancel():
    _cancel_event.clear()

def is_cancelled():
    return _cancel_event.is_set()

class DownloadCancelled(Exception):
    pass

_session_lock = threading.Lock()

# ------------------------- консоль логов -------------------------
import collections as _coll
_CONSOLE_MAX = 2000
_CONSOLE_BUFFER = _coll.deque(maxlen=_CONSOLE_MAX)
_CONSOLE_LOCK = threading.Lock()
_CONSOLE_FILE = BASE_DIR / "launcher_console.log"
_console_api_ref = None  # заполняется в Api.__init__

def _console_log(msg, level="info"):
    """Пишет строку в буфер + файл + пушит в JS (если окно живо)."""
    try:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {str(msg)}"
        # файл (best-effort, ротация 512КБ) — под локом, чтобы не было interleave/гонки ротации
        with _CONSOLE_LOCK:
            try:
                _CONSOLE_FILE.parent.mkdir(parents=True, exist_ok=True)
                if _CONSOLE_FILE.exists() and _CONSOLE_FILE.stat().st_size > 512*1024:
                    try:
                        _CONSOLE_FILE.write_text("", encoding="utf-8")
                    except Exception:
                        pass
                with open(_CONSOLE_FILE, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line + "\n")
            except Exception:
                pass
            _CONSOLE_BUFFER.append(line)
        # пуш в UI
        ref = _console_api_ref
        if ref is not None and getattr(ref, "_window", None) is not None:
            try:
                ref._window.evaluate_js(f"window.appendConsole({json.dumps(line)})")
            except Exception:
                pass
    except Exception:
        pass

def _console_clear():
    with _CONSOLE_LOCK:
        _CONSOLE_BUFFER.clear()
    try:
        if _CONSOLE_FILE.exists():
            _CONSOLE_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass


def _build_stamp():
    """mtime exe (или launcher.py) — видно, какая версия реально запущена."""
    try:
        import datetime
        src = sys.executable if getattr(sys, "frozen", False) else __file__
        return datetime.datetime.fromtimestamp(Path(src).stat().st_mtime).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "?"


def _java_diag_tail(limit=6):
    """Хвост лога про поиск Java — встраивается в текст ошибки, чтобы
    клик-копия/скриншот ошибки сразу показывал сборку и шаг поиска."""
    keys = ("resolve_java", "Докачка Java", "ensure_java_installed",
            "javaPath", "Java-рантайм", "Компонент Java", "Java не резолвится")
    try:
        with _CONSOLE_LOCK:
            lines = [l for l in _CONSOLE_BUFFER if any(k in l for k in keys)]
        tail = " • ".join(lines[-limit:])
    except Exception:
        tail = ""
    out = f"сборка {_build_stamp()}"
    if tail:
        out += " • " + tail
    return out


# ---------------------------------------------------------------------------------


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            path.write_text(text, encoding="utf-8")
        except Exception:
            pass

def resource_path(rel_path):
    candidates=[]
    try:
        candidates.append(Path(getattr(sys, "_MEIPASS", Path(__file__).parent)))
    except Exception:
        pass
    try:
        candidates.append(Path(__file__).parent)
    except Exception:
        pass
    try:
        candidates.append(Path.cwd())
    except Exception:
        pass
    try:
        candidates.append(Path.home() / "OneDrive" / "Desktop" / "LAUNCHER")
        candidates.append(Path.home() / "Desktop" / "LAUNCHER")
    except Exception:
        pass
    for base in candidates:
        try:
            p=Path(base, rel_path)
            if p.exists():
                uri=p.resolve().as_uri()
                print(f"[resource] {rel_path} -> {uri}")
                return uri
        except Exception:
            continue
    base = getattr(sys, "_MEIPASS", Path(__file__).parent)
    uri=Path(base, rel_path).resolve().as_uri()
    print(f"[resource fallback] {rel_path} -> {uri} (exists={Path(base, rel_path).exists()})")
    return uri


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


# Общая HTTP-сессия: переиспользует TCP/TLS-соединения к api.github.com
# вместо нового handshake на каждый запрос (старт, проверка обновлений, скачивание).
_session = None


def _http():
    global _session
    with _session_lock:
        if _session is None:
            from requests.adapters import HTTPAdapter
            s = requests.Session()
            s.headers.update(gh_headers())
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            _session = s
        else:
            _session.headers.update(gh_headers())
        return _session


def get_latest_release(repo=GITHUB_REPO):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{repo}/releases/latest"
    r = _http().get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def find_asset(release, predicate):
    if not isinstance(release, dict):
        return None
    for asset in (release.get("assets") or []):
        if predicate((asset or {}).get("name", "")):
            return asset
    return None


def parse_version(tag):
    """'v1.2.0' -> (1, 2, 0); из мусора вроде '1.2.0-beta' вырезает цифры."""
    if not tag:
        return ()
    return tuple(int(n) for n in re.findall(r"\d+", str(tag)))


def is_newer_version(remote, local):
    """remote > local с выравниванием длины: (1,1,0) == (1,1), без циклов."""
    r, l = parse_version(remote), parse_version(local)
    if not r or not l:
        return False
    n = max(len(r), len(l))
    r += (0,) * (n - len(r))
    l += (0,) * (n - len(l))
    return r > l


def find_setup_asset(release):
    """Setup exe из ассетов релиза (сначала по имени, потом хоть какой-то .exe)."""
    asset = find_asset(release, lambda n: n.lower().endswith(".exe") and "setup" in n.lower())
    if asset:
        return asset
    return find_asset(release, lambda n: n.lower().endswith(".exe"))


def _setup_asset_version(name):
    """Версия из имени Setup exe: 'DragonHorizonLauncher-Setup-1.2.0.exe' -> (1, 2, 0)."""
    m = re.search(r"setup[-_ ]v?(\d+(?:\.\d+)*)", str(name or ""), re.I)
    return parse_version(m.group(1)) if m else ()


def _best_version(*versions):
    """Максимум из набора версий (строк или кортежей) с выравниванием длины."""
    best = ()
    for v in versions:
        v = v if isinstance(v, tuple) else parse_version(v)
        n = max(len(best), len(v))
        b = best + (0,) * (n - len(best))
        c = v + (0,) * (n - len(v))
        if c > b:
            best = c
    return best


def get_release_with_manifest():
    """Последний релиз, в котором лежит launcher.json (обновления пака).

    Релизы только с инсталлером лаунчера идут новее — их пропускаем,
    иначе проверка пака сломается после первого автообновления."""
    try:
        release = get_latest_release()
        if find_asset(release, lambda n: n == "launcher.json"):
            return release
    except Exception:
        return None
    try:
        r = _http().get(
            f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=20",
            timeout=15,
        )
        r.raise_for_status()
        for rel in (r.json() or []):
            if find_asset(rel, lambda n: n == "launcher.json"):
                return rel
    except Exception:
        return None
    return None


_UPDATE_CACHE_FILE = None  # лениво: tempdir/launcher_update_cache.json
_UPDATE_CACHE_TTL = 1800  # 30 минут

# Фактический путь скачиваемого Setup (для прогресса). Глобально, потому что
# update_launcher и update_progress — вызовы API из JS в разных потоках.
_update_setup_file = None


def _update_cache_path():
    global _UPDATE_CACHE_FILE
    if _UPDATE_CACHE_FILE is None:
        import tempfile
        _UPDATE_CACHE_FILE = Path(tempfile.gettempdir()) / "launcher_update_cache.json"
    return _UPDATE_CACHE_FILE


def _read_update_cache():
    try:
        data = json.loads(_update_cache_path().read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) < _UPDATE_CACHE_TTL:
            return data.get("result")
    except Exception:
        pass
    return None


def _write_update_cache(result):
    try:
        _update_cache_path().write_text(
            json.dumps({"ts": time.time(), "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def check_launcher_update():
    """Есть ли новая версия лаунчера — по данным сайта, а не GitHub.

    Версия живёт в server.json (её отдаёт /api/server-info), а сам Setup
    exe сайт раздаёт после входа. Релиз на GitHub пришлось бы публиковать
    вручную и запросто запаздывает — проверяем там же, где игрок и так
    качает лаунчер. Результат кэшируется на 30 минут — иначе каждый
    запуск дёргает сеть."""
    cached = _read_update_cache()
    if cached is not None:
        return cached
    token = load_auth().get("token")
    if not token:
        # exe отдаётся только после входа — без сессии бессмысленно.
        # В кэш НЕ пишем: игрок может войти через пару секунд, а обновление
        # после входа должно найтись сразу, а не через 30 минут.
        return {"needed": False, "error": "нет входа"}
    try:
        r = _http().get(SITE_URL + "/api/server-info", headers=_auth_cookie(token), timeout=15)
        info = r.json() if r.ok else {}
    except Exception as e:
        _console_log(f"Обновление лаунчера: сайт не ответил — {e}", level="error")
        result = {"needed": False, "error": str(e)}
        _write_update_cache(result)
        return result
    version = ".".join(str(x) for x in parse_version((info or {}).get("launcherVersion")))
    if not is_newer_version(version, LAUNCHER_VERSION):
        result = {"needed": False, "version": version}
        _write_update_cache(result)
        return result
    size = int((info or {}).get("launcherSetupSize") or 0)
    _console_log(f"Обновление лаунчера: {version} (сейчас {LAUNCHER_VERSION})"
                 + (f", {size // (1024 * 1024)} МБ" if size else ""))
    result = {
        "needed": True,
        "version": version,
        "name": "DragonHorizonLauncher-Setup-" + version + ".exe",
        "url": SITE_URL + "/download/DragonHorizonLauncher-Setup-" + version + ".exe",
        "size": size,
    }
    _write_update_cache(result)
    return result


def _build_update_bat(pid):
    """Батник обновления (только ASCII — путь инсталлера берётся из %DH_SETUP%).

    Ждёт, пока лаунчер (pid) закроется, ставит Setup молча (без UAC: установка
    в %LOCALAPPDATA%) и запускает уже новую версию. Путь к установке берём
    из переменных окружения: он может содержать кириллицу, а cmd читает
    .bat в кодовой странице OEM — писать туда юникод нельзя."""
    return "\r\n".join([
        "@echo off",
        "setlocal",
        f"set PID={int(pid)}",
        "set COUNT=0",
        ":wait",
        'tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul',
        "if errorlevel 1 goto installed",
        "timeout /t 1 /nobreak >nul",
        "set /a COUNT+=1",
        "if %COUNT% LSS 180 goto wait",
        ":installed",
        # лаунчер — onefile: после выхода python-процесса bootloader ещё
        # пару мгновений держит exe — даём 3 секунды на освобождение файла
        "timeout /t 3 /nobreak >nul",
        '"%DH_SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /LOG="%LOCALAPPDATA%\\dh_launcher_update.log"',
        'start "" "%LOCALAPPDATA%\\Programs\\DragonHorizonLauncher\\DragonHorizonLauncher.exe"',
        'del "%~f0"',
        "",
    ])


def format_eta(seconds):
    if seconds is None:
        return ""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"~{seconds} сек"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"~{minutes} мин {sec:02d} сек"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours} ч {minutes} мин"


def _get_speed_limit():
    try:
        return int(load_ui_settings().get("downloadSpeedLimit", 0) or 0)
    except Exception:
        return 0

def download_file(url, dest_path, headers=None, progress_cb=None, speed_cb=None, retries=3, speed_limit_kbps=None):
    """Скачивание с докачкой при обрыве (HTTP Range) и ретраями.

    Без предварительного HEAD: полный размер узнаём из Content-Range /
    Content-Length самого GET-ответа — на один roundtrip меньше.
    Готовый build.zip (лежит как dest без .part) считается полным.
    speed_limit_kbps: 0 = без лимита, иначе КБ/с (41)
    """
    headers = dict(headers or {})
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")

    # dest существует, а хвоста нет — проверяем что это валидный zip (иначе битый файл зальёт вечно)
    if dest_path.exists() and not part_path.exists():
        try:
            if dest_path.stat().st_size > 0 and zipfile.is_zipfile(dest_path):
                if progress_cb:
                    progress_cb(1)
                return
            else:
                # битый/пустой — перекачаем
                dest_path.unlink(missing_ok=True)
        except Exception:
            dest_path.unlink(missing_ok=True)

    def _total_from_response(resp, resume_from):
        # При докачке GitHub отдаёт 206 + Content-Range: bytes 100-999/1000
        cr = resp.headers.get("content-range", "")
        if "/" in cr:
            try:
                return int(cr.rsplit("/", 1)[1])
            except ValueError:
                pass
        chunk_total = int(resp.headers.get("content-length", 0) or 0)
        return (resume_from + chunk_total) if chunk_total else 0

    # лимит скорости (41)
    _limit_kbps = speed_limit_kbps if speed_limit_kbps is not None else _get_speed_limit()
    _limit_bps = _limit_kbps * 1024 if _limit_kbps else 0
    resume_from = part_path.stat().st_size if part_path.exists() else 0
    # Параллельная загрузка 4×Range для больших файлов (>5МБ) — в 2-3× быстрее на 100Мбит
    # при лимите скорости параллель отключаем — иначе троттлить сложно
    if _limit_bps and resume_from == 0 and not dest_path.exists():
        pass  # fallback к однопотоку с лимитом
    elif resume_from == 0 and not dest_path.exists():
        try:
            hr = _http().head(url, headers=headers, timeout=10, allow_redirects=True)
            total = int(hr.headers.get("Content-Length", 0) or 0) if hr.ok else 0
            # GitHub S3 отдаёт Accept-Ranges: bytes
            if total > 5 * 1024 * 1024 and hr.headers.get("Accept-Ranges") == "bytes":
                n = 4
                part_size = total // n
                tmp_parts = [part_path.with_suffix(f".part{i}") for i in range(n)]
                # чистим старые куски
                for p in tmp_parts:
                    if is_cancelled():
                        raise DownloadCancelled("Загрузка отменена")
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                # уже есть прогресс
                done_arr = [0]*n
                lock = threading.Lock()
                start = time.monotonic()
                last_report = [start]
                def _dl_range(idx, start_b, end_b, dst):
                    if is_cancelled():
                        raise DownloadCancelled("Загрузка отменена")
                    rh = dict(headers)
                    rh["Range"] = f"bytes={start_b}-{end_b}"
                    # отдельная сессия на поток: requests.Session не thread-safe
                    import requests as _rq
                    with _rq.get(url, headers=rh, stream=True, timeout=60) as rr:
                        # сервер проигнорировал Range и отдал 200 с полным телом —
                        # склейка дала бы файл x4, прерываемся в fallback
                        if rr.status_code != 206:
                            raise RuntimeError(f"Range not supported (HTTP {rr.status_code})")
                        rr.raise_for_status()
                        with open(dst, "wb") as f:
                            for chunk in rr.iter_content(chunk_size=1024 * 1024):
                                if is_cancelled():
                                    raise DownloadCancelled("Загрузка отменена")
                                if not chunk:
                                    continue
                                f.write(chunk)
                                with lock:
                                    done_arr[idx] += len(chunk)
                                    total_done = sum(done_arr)
                                    if progress_cb:
                                        progress_cb(total_done / total)
                                    if speed_cb:
                                        now = time.monotonic()
                                        if (now - last_report[0]) >= 0.25:
                                            elapsed = now - start
                                            if elapsed > 0:
                                                speed_bps = total_done / elapsed
                                                eta = (total - total_done) / speed_bps if speed_bps > 0 else None
                                                speed_cb(speed_bps, eta)
                                            last_report[0] = now
                    return dst
                with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
                    futs = []
                    for i in range(n):
                        s = i * part_size
                        e = (s + part_size - 1) if i < n - 1 else total - 1
                        futs.append(ex.submit(_dl_range, i, s, e, tmp_parts[i]))
                    for f in concurrent.futures.as_completed(futs):
                        f.result()
                # склейка
                with open(part_path, "wb") as out:
                    for p in tmp_parts:
                        with open(p, "rb") as inp:
                            while True:
                                ch = inp.read(1024 * 1024)
                                if not ch:
                                    break
                                out.write(ch)
                        p.unlink(missing_ok=True)
                part_path.replace(dest_path)
                if progress_cb:
                    progress_cb(1.0)
                if speed_cb and total:
                    elapsed = time.monotonic() - start
                    if elapsed > 0:
                        speed_cb(total / elapsed, 0)
                return
        except DownloadCancelled:
            # не оставляем мусор .part0..3 — иначе висят на диске навсегда
            for p in [part_path.with_suffix(f".part{i}") for i in range(4)]:
                try: p.unlink(missing_ok=True)
                except Exception: pass
            raise
        except Exception:
            # fallback на обычный однопоток
            for p in [part_path.with_suffix(f".part{i}") for i in range(4)]:
                try: p.unlink(missing_ok=True)
                except: pass
            pass

    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            req_headers = dict(headers)
            if resume_from > 0:
                req_headers["Range"] = f"bytes={resume_from}-"
            with _http().get(url, headers=req_headers, stream=True, timeout=60) as r:
                # 416 Range Not Satisfiable — битый .part, сбрасываем и ретраим
                if r.status_code == 416:
                    try:
                        part_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    resume_from = 0
                    raise requests.exceptions.ConnectionError("HTTP 416, part сброшен")
                r.raise_for_status()
                # Сервер проигнорировал Range и отдал всё с нуля — пересчитываем full_total
                if resume_from > 0 and r.status_code == 200:
                    resume_from = 0
                    part_path.unlink(missing_ok=True)
                    full_total = _total_from_response(r, 0)
                else:
                    full_total = _total_from_response(r, resume_from)
                # Хвост больше целого — битый .part, сбрасываем и ретраим новым запросом
                if full_total and resume_from > full_total:
                    try:
                        part_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    resume_from = 0
                    raise requests.exceptions.ConnectionError("part больше целого, начинаем заново")

                done = resume_from
                if progress_cb and full_total:
                    progress_cb(done / full_total)
                start = time.monotonic()
                session_done = 0
                last_report = start
                mode = "ab" if resume_from > 0 else "wb"
                with open(part_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if is_cancelled():
                            raise DownloadCancelled("Загрузка отменена пользователем")
                        if not chunk:
                            continue
                        f.write(chunk)
                        # троттлинг 41
                        if _limit_bps:
                            elapsed = time.monotonic() - start
                            if elapsed > 0:
                                expected = (session_done + len(chunk)) / _limit_bps if _limit_bps else 0
                                if expected > elapsed:
                                    time.sleep(expected - elapsed)
                        done += len(chunk)
                        session_done += len(chunk)
                        if progress_cb and full_total:
                            progress_cb(done / full_total)

                        now = time.monotonic()
                        if speed_cb and (now - last_report) >= 0.25:
                            elapsed = now - start
                            if elapsed > 0:
                                # скорость текущей сессии (честно при докачке)
                                speed_bps = session_done / elapsed
                                eta = (full_total - done) / speed_bps if full_total and speed_bps > 0 else None
                                speed_cb(speed_bps, eta)
                            last_report = now

                if speed_cb:
                    elapsed = time.monotonic() - start
                    if elapsed > 0 and session_done:
                        speed_cb(session_done / elapsed, 0)

            # Атомарно переименовываем .part в целевой файл
            part_path.replace(dest_path)
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            try:
                if part_path.exists():
                    resume_from = part_path.stat().st_size
            except Exception:
                pass
            if attempt + 1 >= max(1, retries):
                raise
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            raise
    if last_exc:
        raise last_exc


def load_local_manifest():
    if VERSION_FILE.exists():
        try:
            return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_local_manifest(manifest):
    _atomic_write(VERSION_FILE, json.dumps(manifest, ensure_ascii=False, indent=2))


DEFAULT_UI_SETTINGS = {
    "nickname": "",
    "skinNick": "",
    "ram": 4096,
    "javaPath": "",
    "jvmArgs": "",
    "closeOnLaunch": False,
    "perfMode": None,
    "launcherOnly": False,
    "theme": "dark",
    "sounds": True,
    "trayMinimize": False,
    "music": False,
    "customCursor": False,
    "language": "ru",
    "compactMode": False,
    "startMinimized": False,
    "autoCloseDelay": 0,
    "downloadSpeedLimit": 0,
    "recentNicks": [],
}

# --- BlackList анти-чит (по имени/хешу/содержимому jar) ---
CHEAT_KEYWORDS = [
    "wurst","wurstclient","impact","aristois","meteor","meteor-client",
    "baritone","xray","x-ray","killaura","kill aura","scaffold","autoclick",
    "cheat","hacked","ghost","liquidbounce","future","rusherhack","sigma",
    "weeping","inertia","bleachhack","minecraft-hack"
]
# сюда можно добавить sha256 известных читов: "ab12...": "Meteor 0.5.7"
CHEAT_HASHES = {
    # пример: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855": "test-cheat"
}
CHEAT_IN_JAR_PATHS = [
    "wurst","meteordevelopment","meteor/client","baritone","impact","aristois",
    "net/minecraft/client/mixin/cheat","autoclick","killaura","xray"
]

def scan_for_cheats(jvm_args=None):
    found=[]
    seen=set()
    def add(msg):
        if msg not in seen:
            seen.add(msg)
            found.append(msg)
    # 1. JVM-инжект
    if jvm_args and isinstance(jvm_args, str):
        low=jvm_args.lower()
        for bad in ["-javaagent","-xbootclasspath","-agentlib","-agentpath"]:
            if bad in low:
                add(f"JVM-инжект: {bad} в аргументах")
    # 2. сканируем mods
    mods_dir = MINECRAFT_DIR / "mods"
    if mods_dir.exists() and mods_dir.is_dir():
        for f in mods_dir.iterdir():
            if not f.is_file(): continue
            lname=f.name.lower()
            # по имени файла
            for kw in CHEAT_KEYWORDS:
                if kw.lower() in lname:
                    add(f"{f.name} — запрещённое слово '{kw}'")
                    break
            # по хешу
            try:
                h=_sha256_file(f)
                if h in CHEAT_HASHES:
                    add(f"{f.name} — известный чит {CHEAT_HASHES[h]} ({h[:8]}...)")
            except Exception:
                pass
            # по содержимому jar (zip namelist)
            if f.suffix.lower()==".jar":
                try:
                    with zipfile.ZipFile(f, "r") as z:
                        hit=False
                        for n in z.namelist():
                            nl=n.lower()
                            seg = "/" + nl
                            for cp in CHEAT_IN_JAR_PATHS:
                                if ("/" + cp + "/") in seg or seg.endswith("/" + cp):
                                    add(f"{f.name} — внутри jar: {n}")
                                    hit=True
                                    break
                            if hit:
                                break
                except Exception:
                    pass
    return found

# --- уведомления Windows (40) ---
def send_notification(title, message):
    # injection-safe: используем только plyer/win10toast/MessageBeep, без PowerShell интерполяции
    try:
        try:
            from plyer import notification as _notif
            _notif.notify(title=str(title)[:80], message=str(message)[:200], app_name=DISPLAY_NAME, timeout=4)
            return
        except Exception:
            pass
        try:
            from win10toast import ToastNotifier as _TN
            _TN().show_toast(str(title)[:80], str(message)[:200], duration=4, threaded=True)
            return
        except Exception:
            pass
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBeep(0x40)
        except Exception:
            pass
    except Exception:
        pass


def _normalize_ui_settings(data):
    data = data if isinstance(data, dict) else {}
    out = dict(DEFAULT_UI_SETTINGS)
    nick = data.get("nickname", "")
    out["nickname"] = str(nick)[:16] if isinstance(nick, str) else ""
    skin_nick = data.get("skinNick", "")
    out["skinNick"] = str(skin_nick)[:16] if isinstance(skin_nick, str) else ""
    try:
        ram = int(data.get("ram", out["ram"]))
    except (TypeError, ValueError):
        ram = out["ram"]
    out["ram"] = max(512, min(65536, ram))
    out["javaPath"] = str(data.get("javaPath", "") or "")
    out["jvmArgs"] = str(data.get("jvmArgs", "") or "")
    out["closeOnLaunch"] = bool(data.get("closeOnLaunch", False))
    pm = data.get("perfMode", None)
    out["perfMode"] = pm if isinstance(pm, bool) else None
    out["launcherOnly"] = bool(data.get("launcherOnly", False))
    _theme = data.get("theme", "dark")
    out["theme"] = _theme if _theme in ("dark", "light") else "dark"
    out["sounds"] = bool(data.get("sounds", True)) if "sounds" in data else True
    out["trayMinimize"] = bool(data.get("trayMinimize", False))
    out["music"] = bool(data.get("music", False))
    out["customCursor"] = bool(data.get("customCursor", False))
    out["compactMode"] = bool(data.get("compactMode", False))
    out["startMinimized"] = bool(data.get("startMinimized", False))
    try:
        acd = int(data.get("autoCloseDelay", 0))
    except Exception:
        acd = 0
    out["autoCloseDelay"] = acd if acd in (0, 3, 5, 10, 15, 30) else 0
    try:
        dsl = int(data.get("downloadSpeedLimit", 0))
    except Exception:
        dsl = 0
    out["downloadSpeedLimit"] = max(0, min(10240, dsl))
    # 13 recent nicks
    rn=data.get("recentNicks", [])
    if isinstance(rn, list):
        out["recentNicks"]=[str(x)[:16] for x in rn if isinstance(x, str) and re.match(r"^[A-Za-z0-9_]{1,16}$", x)][:5]
    else:
        out["recentNicks"]=[]
    lang = str(data.get("language", "ru") or "ru").lower()
    out["language"] = lang if lang in ("ru", "en") else "ru"
    return out


def load_ui_settings():
    if UI_SETTINGS_FILE.exists():
        try:
            # utf-8-sig: файл мог быть перезаписан с BOM (ручная правка/копирование) —
            # без него json.loads падает, лаунчер видит дефолты и затирает настройки
            return _normalize_ui_settings(json.loads(UI_SETTINGS_FILE.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    return dict(DEFAULT_UI_SETTINGS)


def _push_recent_nick(nick):
    try:
        nick=(nick or "").strip()
        if not nick or not re.match(r"^[A-Za-z0-9_]{3,16}$", nick):
            return
        cur=load_ui_settings()
        lst=list(cur.get("recentNicks") or [])
        if nick in lst:
            lst.remove(nick)
        lst.insert(0, nick)
        cur["recentNicks"]=lst[:5]
        # bypass _push loop: direct write
        _atomic_write(UI_SETTINGS_FILE, json.dumps(_normalize_ui_settings(cur), ensure_ascii=False, indent=2))
    except Exception:
        pass

def save_ui_settings(data):
    if not isinstance(data, dict):
        data = {}
    merged = _normalize_ui_settings({**load_ui_settings(), **data})
    _atomic_write(UI_SETTINGS_FILE, json.dumps(merged, ensure_ascii=False, indent=2))
    return merged


# ------------------------- вход через сайт -------------------------
# Лаунчер пускает только авторизованных: та же учётка, что и на
# dragonhorizon.ru. Сессия — та же HMAC-cookie, что у сайта, только
# хранится локально в launcher_auth.json вместо браузерной cookie.

def load_auth():
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data.get("token"):
                return data
        except Exception:
            pass
    return {}


def _save_auth(data):
    try:
        if isinstance(data, dict) and data.get("token"):
            _atomic_write(AUTH_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        elif AUTH_FILE.exists():
            AUTH_FILE.unlink()
    except Exception:
        pass


def clear_auth():
    _save_auth({})


def _auth_cookie(token):
    return {"Cookie": "dh_session=" + str(token)}


def site_login(username, password):
    """POST /api/login → {ok, username, role…} или {ok: False, error}."""
    try:
        r = _http().post(
            SITE_URL + "/api/login",
            json={"username": username, "password": password},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": friendly_error(e)}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": "Сайт ответил непонятно — попробуй позже"}
    if r.status_code != 200 or not data.get("ok"):
        return {"ok": False, "error": data.get("error") or "Неверный ник или пароль"}
    token = r.cookies.get("dh_session")
    if not token:
        return {"ok": False, "error": "Сайт не выдал сессию — попробуй ещё раз"}
    name = data.get("username") or (username or "").strip()
    _save_auth({"token": token, "username": name})
    out = {"ok": True, "username": name}
    # профиль подтягиваем сразу: роль нужна и для показа, и для истории
    prof = site_whoami(token)
    if prof.get("ok"):
        _save_auth({
            "token": token,
            "username": prof.get("username") or name,
            "role": prof.get("role"),
            "roleLabel": prof.get("roleLabel"),
        })
        out["role"] = prof.get("role")
        out["roleLabel"] = prof.get("roleLabel")
    return out


def site_whoami(token):
    """GET /api/whoami. offline=True — нет сети (не путать с отозванной сессией)."""
    try:
        r = _http().get(SITE_URL + "/api/whoami", headers=_auth_cookie(token), timeout=12)
    except requests.exceptions.RequestException as e:
        # сайт недоступен: токен не чистим, иначе игрок отваливается
        # из-за кратковременной сетевой проблемы
        return {"ok": False, "offline": True, "error": friendly_error(e)}
    if r.status_code == 401:
        return {"ok": False, "error": "Сессия истекла"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": "Плохой ответ сайта"}
    if r.status_code != 200 or not data.get("ok"):
        return {"ok": False, "error": "Сессия недействительна"}
    return data


def site_logout(token):
    """POST /api/logout. Ошибки сети не важны — токен сотрём локально в любом случае."""
    if not token:
        return
    try:
        _http().post(SITE_URL + "/api/logout", headers=_auth_cookie(token), timeout=10)
    except requests.exceptions.RequestException:
        pass


def site_note_download(token, what="win-setup"):
    """Отметка «скачал» для истории скачиваний в кабинете. Ошибка не важна."""
    if not token:
        return
    try:
        _http().post(
            SITE_URL + "/api/note-download",
            json={"what": what},
            headers=_auth_cookie(token),
            timeout=10,
        )
    except requests.exceptions.RequestException:
        pass


def _current_skin_bytes():
    """PNG текущего скина (skinNick) из кэша/поиска, best-effort. None — если скина нет."""
    try:
        nick = str(load_ui_settings().get("skinNick", "") or "").strip()
        if not nick:
            return None
        data = skin_lookup(nick)
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        uuid = data.get("uuid")
        if not uuid:
            return None
        p = SKIN_CACHE_DIR / (str(uuid) + ".png")
        if not (p.exists() and p.stat().st_size > 0):
            url = data.get("skin_url")
            if not url:
                return None
            p = _download_skin_png(url, uuid)
        if not p:
            return None
        raw = p.read_bytes()
        if len(raw) > 128 * 1024 or raw[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return raw
    except Exception:
        return None


def site_sync_stats(token):
    """Синк статистики/ачивок/каталога/скина на сайт — карточки кабинета.

    Best-effort: офлайн или ошибка сети не должны мешать лаунчеру.
    """
    if not token:
        return False
    stats = _load_stats()
    payload = {
        "launches": int(stats.get("launches", 0) or 0),
        "play_seconds": int(stats.get("play_seconds", 0) or 0),
        "first_launch": stats.get("first_launch"),
        "last_launch": stats.get("last_launch"),
        "achievements": dict(stats.get("achievements") or {}),
        "catalog": [
            {"id": a["id"], "icon": a["icon"], "name": a["name"], "desc": a["desc"]}
            for a in ACHIEVEMENTS
        ],
    }
    raw = _current_skin_bytes()
    if raw:
        payload["skin_b64"] = base64.b64encode(raw).decode("ascii")
    r = _http().post(
        SITE_URL + "/api/sync",
        json=payload,
        headers=_auth_cookie(token),
        timeout=20,
    )
    return bool(r.ok)


def _sync_quietly():
    try:
        site_sync_stats(load_auth().get("token"))
    except Exception:
        pass


# --- антивирус (40) ---
def check_antivirus():
    info={"defender_running": False, "excluded": None, "advice": ""}
    try:
        # Defender процесс
        try:
            import psutil as _ps
            if _ps:
                for p in _ps.process_iter(["name"]):
                    if (p.info.get("name") or "").lower()=="msmpeng.exe":
                        info["defender_running"]=True
                        break
        except Exception:
            pass
        # PowerShell проверка исключений (best-effort, без админки может вернуть пусто)
        try:
            out=subprocess.run(["powershell","-NoProfile","-Command","Get-MpPreference | Select-Object -ExpandProperty ExclusionPath"], capture_output=True, text=True, timeout=5)
            txt=(out.stdout or "") + (out.stderr or "")
            if str(BASE_DIR) in txt:
                info["excluded"]=True
            elif out.returncode==0:
                info["excluded"]=False
        except Exception:
            pass
        if info["defender_running"] and info["excluded"]==False:
            info["advice"]="Добавь папку .minecraft в исключения Защитника Windows: Параметры -> Защита от вирусов -> Исключения"
        elif info["defender_running"]:
            info["advice"]="Защитник активен. Если игра вылетает/лагает - добавь .minecraft в исключения."
        else:
            info["advice"]="Защитник не обнаружен или неактивен."
    except Exception as e:
        info["error"]=str(e)
    return info

# --- экспорт логов (39) ---
def export_logs():
    try:
        import zipfile as _zf, tempfile
        tmpdir=Path(tempfile.gettempdir())
        out = tmpdir / f"DragonHorizon_logs_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        with _zf.ZipFile(out, "w", _zf.ZIP_DEFLATED) as z:
            # лаунчер лог
            if _CONSOLE_FILE.exists():
                z.write(_CONSOLE_FILE, "launcher_console.log")
            # latest.log
            latest = MINECRAFT_DIR / "logs" / "latest.log"
            if latest.exists():
                z.write(latest, "logs/latest.log")
            # debug.log
            dbg = MINECRAFT_DIR / "logs" / "debug.log"
            if dbg.exists():
                # ограничим 2МБ
                try:
                    if dbg.stat().st_size > 2*1024*1024:
                        txt=dbg.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
                        z.writestr("logs/debug.log", "\n".join(txt))
                    else:
                        z.write(dbg, "logs/debug.log")
                except Exception:
                    pass
            # краши (до 5)
            crash_dir = MINECRAFT_DIR / "crash-reports"
            if crash_dir.exists():
                try:
                    _cr = list(crash_dir.iterdir())
                except OSError:
                    _cr = []
                _with_mtime = []
                for _p in _cr:
                    try:
                        _with_mtime.append((_p.stat().st_mtime, _p))
                    except (FileNotFoundError, OSError):
                        continue
                _with_mtime.sort(reverse=True)
                for _, p in _with_mtime[:5]:
                    if p.suffix == ".txt":
                        try:
                            z.write(p, f"crash-reports/{p.name}")
                        except (FileNotFoundError, OSError):
                            continue
            # launcher_ui + stats
            for name in ["launcher_ui.json","launcher_stats.json","installed_version.json"]:
                p=BASE_DIR / name
                if p.exists():
                    z.write(p, name)
        return {"ok": True, "path": str(out), "uri": out.as_uri()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def needs_update(remote_manifest):
    local = load_local_manifest()
    if not local:
        return True
    return local.get("build_version") != remote_manifest.get("build_version")


def ensure_java_installed(mc_version, progress_cb=None):
    """
    Устанавливает Java, которая нужна для указанной версии Minecraft.
    minecraft-launcher-lib умеет это делать через модуль runtime.
    Возвращает путь к java.exe/javaw.exe или None, если не удалось.
    """
    import minecraft_launcher_lib as mll

    def cb(kind, value):
        if progress_cb:
            progress_cb(kind, value)

    callback = {
        "setStatus": lambda text: cb("status", text),
        "setProgress": lambda val: cb("progress", val),
        "setMax": lambda val: cb("max", val),
    }

    try:
        # get_jvm_runtimes() возвращает list[str] (в старых версиях библиотеки
        # был get_available_jvm_runtimes() с dict — поддерживаем оба)
        _get_runtimes = getattr(mll.runtime, "get_jvm_runtimes", None) or getattr(
            mll.runtime, "get_available_jvm_runtimes", None)
        if _get_runtimes is None:
            _console_log("ensure_java_installed: в mll нет get_jvm_runtimes", level="error")
            return None
        raw_runtimes = _get_runtimes()
        if isinstance(raw_runtimes, dict):
            runtimes = list(raw_runtimes.keys())
        else:
            runtimes = list(raw_runtimes or [])
        if not runtimes:
            _console_log("ensure_java_installed: список рантаймов пуст (нет сети?)", level="error")
            return None
        _console_log(f"ensure_java_installed: доступно рантаймов={len(runtimes)}")

        # Пробуем найти подходящий рантайм: берём компонент из json версии.
        # В mll 8.x нет utils.get_version_json — используем
        # runtime.get_version_runtime_information (он поднимает inheritsFrom).
        java_version_component = None
        try:
            info = mll.runtime.get_version_runtime_information(mc_version, str(MINECRAFT_DIR))
            if info:
                java_version_component = info.get("name")
        except Exception as e:
            _console_log(f"Компонент Java для {mc_version}: {e}", level="error")
        _console_log(f"ensure_java_installed: компонент версии {mc_version!r} = {java_version_component!r}")

        candidates = []
        if java_version_component and java_version_component in runtimes:
            candidates.append(java_version_component)
        # fallback: пробуем популярные имена
        for name in ("java-runtime-delta", "java-runtime-gamma", "java-runtime-beta", "jre-legacy"):
            if name in runtimes and name not in candidates:
                candidates.append(name)
        # если ничего не подошло — берём первый доступный
        if not candidates and runtimes:
            candidates.append(next(iter(runtimes)))

        for name in candidates:
            try:
                # install_jvm_runtime принимает имя рантайма ("java-runtime-gamma"),
                # а не вложенную версию — ставит сам и возвращает имя
                mll.runtime.install_jvm_runtime(name, str(MINECRAFT_DIR), callback=callback)
                _console_log(f"Java-рантайм {name} установлен")
                return name
            except Exception as e:
                _console_log(f"Java-рантайм {name} не установился: {e}", level="error")
                continue
    except Exception as e:
        _console_log(f"ensure_java_installed: {e}", level="error")
    return None


def resolve_java_path(version_id=None, mc_version=None, progress_cb=None):
    """Возвращает рабочий путь к java.exe или None.

    Порядок: путь из настроек → установленный рантайм сборки
    (с докачкой при необходимости) → java в PATH.
    """
    import shutil
    import minecraft_launcher_lib as mll

    def _runtime_path(component):
        if not component:
            return None
        try:
            p = mll.runtime.get_executable_path(component, str(MINECRAFT_DIR))
        except Exception:
            return None
        return p if p and Path(p).is_file() else None

    def _component(ver):
        if not ver:
            return None
        try:
            info = mll.runtime.get_version_runtime_information(ver, str(MINECRAFT_DIR))
        except Exception:
            return None
        if not info:
            return None
        return info.get("name") or None

    # 1) путь, указанный в настройках
    _console_log(f"resolve_java: version_id={version_id!r} mc_version={mc_version!r}")
    custom = str(load_ui_settings().get("javaPath", "") or "").strip()
    if custom:
        if Path(custom).is_file():
            _console_log(f"Java из настроек: {custom}")
            return custom
        _console_log(f"javaPath из настроек не существует: {custom}", level="error")

    # 2) уже установленный рантайм сборки
    component = _component(mc_version) or _component(version_id)
    p = _runtime_path(component)
    _console_log(f"resolve_java: компонент={component!r}, рантайм установлен={bool(p)}")
    if p:
        _console_log(f"Java из рантайма сборки: {p}")
        return p

    # 3) докачать рантайм (свежая установка на чужом ПК)
    if mc_version or version_id:
        installed = ensure_java_installed(mc_version or version_id, progress_cb=progress_cb)
        _console_log(f"Докачка Java: {installed or 'не удалась'}")
        component = component or _component(mc_version) or _component(version_id)
        p = _runtime_path(component)
        if p:
            _console_log(f"Java после докачки: {p}")
            return p
        # рантайм мог встать под другим компонентом — пробуем известные имена
        for rt_name in ("java-runtime-delta", "java-runtime-gamma", "java-runtime-beta", "jre-legacy"):
            p = _runtime_path(rt_name)
            if p:
                _console_log(f"Java по имени {rt_name}: {p}")
                return p
        # и, наконец, любой уже установленный рантайм в runtime/
        try:
            rt_root = MINECRAFT_DIR / "runtime"
            if rt_root.is_dir():
                exe_name = "java.exe" if os.name == "nt" else "java"
                for exe in rt_root.rglob(exe_name):
                    if exe.is_file() and exe.parent.name == "bin":
                        _console_log(f"Java найдена сканом runtime/: {exe}")
                        return str(exe)
        except Exception:
            pass

    # 4) java в PATH
    w = shutil.which("java")
    if w:
        _console_log(f"Java из PATH: {w}")
        return w
    _console_log("resolve_java: рабочая Java не найдена ни в одном источнике", level="error")
    return None


def ensure_minecraft_installed(manifest, progress_cb=None):
    """Ставит vanilla + modloader, если их ещё нет локально."""
    import minecraft_launcher_lib as mll
    mc_version = manifest["minecraft_version"]
    loader = manifest.get("loader", "vanilla").lower()
    loader_version = manifest.get("loader_version", "")

    def cb(kind, value):
        if progress_cb:
            progress_cb(kind, value)

    callback = {
        "setStatus": lambda text: cb("status", text),
        "setProgress": lambda val: cb("progress", val),
        "setMax": lambda val: cb("max", val),
    }

    # Сначала ставим Java для этой версии Minecraft
    if progress_cb:
        progress_cb("status", "Устанавливаем Java\u2026")
    ensure_java_installed(mc_version, progress_cb=progress_cb)

    if loader == "vanilla":
        mll.install.install_minecraft_version(mc_version, str(MINECRAFT_DIR), callback=callback)
        return mc_version

    if loader == "forge":
        forge_full = (
            mll.forge.find_forge_version(mc_version)
            if not loader_version else f"{mc_version}-{loader_version}"
        )
        if not forge_full:
            raise RuntimeError(f"Не нашли версию Forge для Minecraft {mc_version}")
        mll.forge.install_forge_version(forge_full, str(MINECRAFT_DIR), callback=callback)
        try:
            versions = os.listdir(MINECRAFT_DIR / "versions")
        except FileNotFoundError:
            return mc_version
        # предпочитаем профиль с той же версией MC, иначе первый forge-профиль
        forge_profile = next((v for v in versions if "forge" in v.lower() and mc_version in v), None)
        if forge_profile is None:
            forge_profile = next((v for v in versions if "forge" in v.lower()), None)
        return forge_profile or mc_version

    if loader == "fabric":
        mll.fabric.install_fabric(mc_version, str(MINECRAFT_DIR), loader_version=loader_version or None, callback=callback)
        lv = loader_version or mll.fabric.get_latest_loader_version()
        return f"fabric-loader-{lv}-{mc_version}"

    if loader == "quilt":
        mll.quilt.install_quilt(mc_version, str(MINECRAFT_DIR), loader_version=loader_version or None, callback=callback)
        lv = loader_version or mll.quilt.get_latest_loader_version()
        return f"quilt-loader-{lv}-{mc_version}"

    raise ValueError(f"Неизвестный loader в launcher.json: {loader}")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path, chunk_size=1024 * 512):
    """Хэш файла чанками — без загрузки целиком в память."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _load_files_manifest():
    if FILES_MANIFEST.exists():
        try:
            return json.loads(FILES_MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_files_manifest(hashes):
    _atomic_write(FILES_MANIFEST, json.dumps(hashes, ensure_ascii=False, indent=2))


def update_build(build_asset, manifest, progress_cb=None, status_cb=None, speed_cb=None):
    zip_path = BASE_DIR / "build.zip"
    download_file(
        build_asset["browser_download_url"],
        zip_path,
        headers=gh_headers(),
        progress_cb=lambda p: progress_cb(p) if progress_cb else None,
        speed_cb=speed_cb,
    )
    # отметка «скачал сборку» — попадает в историю кабинета на сайте
    try:
        site_note_download(load_auth().get("token"), "build-zip")
    except Exception:
        pass

    if status_cb:
        status_cb("Сверяем файлы сборки\u2026")

    old_hashes = _load_files_manifest()
    new_hashes = {}
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

    # Параллельный хэш 4 потока + потоковая запись только изменённых (в 2-3× быстрее на SSD)
    def _safe_members():
        lst=[]
        with zipfile.ZipFile(zip_path, "r") as _zf:
            for m in _zf.infolist():
                if m.is_dir(): continue
                raw = m.filename.replace("\\", "/")
                if not raw or raw.startswith("/") or ".." in raw.split("/"): continue
                try:
                    (INSTANCE_DIR / raw).resolve().relative_to(INSTANCE_DIR.resolve())
                except ValueError:
                    continue
                lst.append(raw)
        return lst

    safe_list = _safe_members()
    total = len(safe_list) or 1

    def _hash_one(name):
        if is_cancelled():
            raise DownloadCancelled("Загрузка отменена")
        h = hashlib.sha256()
        # каждый поток открывает свой ZipFile — zipfile не потокобезопасен
        with zipfile.ZipFile(zip_path, "r") as _zf2:
            with _zf2.open(name, "r") as src:
                while True:
                    if is_cancelled():
                        raise DownloadCancelled("Загрузка отменена")
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
        return name, h.hexdigest()

    # 4 потока — оптимум для 2-8 ядер, не грузит диск
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_hash_one, n): n for n in safe_list}
        done_cnt = 0
        last_push = 0.0
        for fut in concurrent.futures.as_completed(futs):
            if is_cancelled():
                for f in futs: f.cancel()
                raise DownloadCancelled("Загрузка отменена пользователем")
            name, file_hash = fut.result()
            new_hashes[name] = file_hash
            done_cnt += 1
            if progress_cb:
                now = time.monotonic()
                if (now - last_push) >= 0.1 or done_cnt == total:
                    # прогресс 0-0.5 на хэше, 0.5-1 на записи
                    progress_cb((done_cnt / total) * 0.5)
                    last_push = now

    # запись только изменённых — последовательно (диск не любит параллельную запись)
    write_total = sum(1 for n, h in new_hashes.items() if old_hashes.get(n) != h) or 1
    write_done = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name, file_hash in new_hashes.items():
            if is_cancelled():
                raise DownloadCancelled("Загрузка отменена пользователем")
            if old_hashes.get(name) == file_hash:
                continue
            dest = INSTANCE_DIR / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name, "r") as src, open(dest, "wb") as out:
                while True:
                    if is_cancelled():
                        raise DownloadCancelled("Загрузка отменена пользователем")
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            write_done += 1
            if progress_cb:
                now = time.monotonic()
                if (now - last_push) >= 0.1 or write_done == write_total:
                    progress_cb(0.5 + (write_done / write_total) * 0.5)
                    last_push = now
    if progress_cb:
        progress_cb(1.0)

    for rel_path in set(old_hashes) - set(new_hashes):
        local_path = INSTANCE_DIR / rel_path
        try:
            if local_path.exists() and _sha256_file(local_path) == old_hashes[rel_path]:
                local_path.unlink()
        except Exception:
            pass

    zip_path.unlink(missing_ok=True)
    _save_files_manifest(new_hashes)
    save_local_manifest(manifest)


# ------------------------- пинг сервера -------------------------

def _pack_varint(value):
    out = b""
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out += bytes((byte | 0x80,))
        else:
            out += bytes((byte,))
            return out


def _pack_data(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _pack_varint(len(data)) + data


def _read_varint(sock):
    value = 0
    position = 0
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise IOError("Соединение закрылось при чтении ответа сервера")
        byte = chunk[0]
        value |= (byte & 0x7F) << position
        if not (byte & 0x80):
            return value
        position += 7
        if position >= 32:
            raise IOError("Некорректный ответ сервера (слишком длинный VarInt)")


def ping_server_status(host, port=25565, timeout=3, handshake_host=None):
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            handshake = (
                _pack_varint(0)
                + _pack_varint(0)
                + _pack_data(handshake_host or host)
                + struct.pack(">H", port)
                + _pack_varint(1)
            )
            sock.sendall(_pack_data(handshake))
            sock.sendall(_pack_data(_pack_varint(0)))

            _read_varint(sock)
            packet_id = _read_varint(sock)
            length = _read_varint(sock)
            data = b""
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk:
                    raise IOError("Соединение оборвалось во время чтения ответа")
                data += chunk

            payload = json.loads(data.decode("utf-8"))
            players = payload.get("players", {}) or {}
            sample = players.get("sample") or []
            # 15 ники sample (до 12)
            names=[]
            if isinstance(sample, list):
                for e in sample[:12]:
                    if isinstance(e, dict) and e.get("name"):
                        names.append(str(e["name"])[:16])
            return {
                "online": True,
                "players": int(players.get("online", 0)),
                "max": int(players.get("max", 0)),
                "sample": names,
            }
    except Exception:
        return {"online": False, "players": 0, "max": 0}


def _query_minecraft_srv(host, timeout=2):
    """SRV-запрос _minecraft._tcp.<host> чистым stdlib (UDP DNS).

    Возвращает (target, port) или None. Нужен, т.к. порт сервера может
    отличаться от 25565 и задаваться только через SRV (у нас — 25642).
    """
    import random

    qname = f"_minecraft._tcp.{host}".encode("utf-8")
    q = b"".join(bytes((c,)) + part for part in qname.split(b".") for c in (len(part),)) + b"\x00"
    txid = random.randint(0, 65535)
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    question = q + struct.pack(">HH", 33, 1)  # SRV, IN
    packet = header + question

    def _parse(resp):
        if len(resp) < 12:
            return None
        # сверяем ID транзакции — защита от чужих ответов/spoof
        if struct.unpack(">H", resp[0:2])[0] != txid:
            return None
        ancount = struct.unpack(">H", resp[4:6])[0]
        # pos уже за header+question: запрос и есть header+question, ответ
        # начинается с тех же байт — пропускать question повторно не нужно
        pos = len(packet)
        best = None
        for _ in range(ancount):
            if pos + 10 > len(resp):
                break
            # имя (обычно указатель)
            if resp[pos] & 0xC0:
                pos += 2
            else:
                while pos < len(resp) and resp[pos] != 0:
                    pos += 1 + resp[pos]
                pos += 1
            if pos + 10 > len(resp):
                break
            rtype, _, _, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
            pos += 10
            rdata = resp[pos:pos + rdlen]
            pos += rdlen
            if rtype == 33 and len(rdata) >= 6:
                priority, weight, port = struct.unpack(">HHH", rdata[:6])
                # декодируем target (с указателями)
                labels = []
                i = 6
                jumps = 0
                while i < len(rdata) and jumps < 10:
                    ln = rdata[i]
                    if ln == 0:
                        break
                    if ln & 0xC0:
                        ptr = struct.unpack(">H", rdata[i:i + 2])[0] & 0x3FFF
                        sub = []
                        j = ptr
                        while j < len(resp) and resp[j] != 0 and len(sub) < 10:
                            if resp[j] & 0xC0:
                                j = struct.unpack(">H", resp[j:j + 2])[0] & 0x3FFF
                                continue
                            l = resp[j]
                            sub.append(resp[j + 1:j + 1 + l].decode("utf-8", "ignore"))
                            j += 1 + l
                        labels.extend(sub)
                        break
                    labels.append(rdata[i + 1:i + 1 + ln].decode("utf-8", "ignore"))
                    i += 1 + ln
                target = ".".join(labels)
                cand = (target or host, port, priority, weight)
                if best is None or (priority, -weight) < (best[2], -best[3]):
                    best = cand
        return (best[0], best[1]) if best else None

    for dns in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(packet, (dns, 53))
                resp, _ = s.recvfrom(4096)
                # TC-бит (truncated): ответ обрезан, точек может не быть — пробуем следующий DNS
                if len(resp) >= 2 and (resp[2] & 0x02):
                    continue
            found = _parse(resp)
            if found:
                return found
        except Exception:
            continue
    return None


def resolve_server_address():
    """Возвращает (connect_host, handshake_host, port) с учётом SRV.

    Если SERVER_PORT задан явно — используем его. Иначе пробуем SRV,
    иначе 25565.
    """
    if SERVER_PORT:
        return SERVER_ADDRESS, SERVER_ADDRESS, SERVER_PORT
    try:
        found = _query_minecraft_srv(SERVER_ADDRESS)
    except Exception:
        found = None
    if found:
        target, port = found
        return target or SERVER_ADDRESS, SERVER_ADDRESS, port
    return SERVER_ADDRESS, SERVER_ADDRESS, 25565


def repair_build():
    """Сброс маркеров установки: следующий запуск перекачает сборку и игру."""
    removed = []
    for f in (VERSION_FILE, FILES_MANIFEST):
        try:
            if f.exists():
                f.unlink()
                removed.append(f.name)
        except Exception:
            pass
    # счётчик для ачивки «Мастер починки»
    try:
        stats = _load_stats()
        stats["repairs"] = int(stats.get("repairs", 0)) + 1
        _check_achievements(stats)
        _save_stats(stats)
    except Exception:
        pass
    return {"ok": True, "removed": removed}

def factory_reset():
    """Полный сброс как в начале: чистит все файлы лаунчера."""
    import shutil
    removed = []
    _local_appdata = os.getenv("LOCALAPPDATA") or ""
    _store_mc = (Path(_local_appdata) / "Packages" / "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0" / "LocalCache" / "Roaming" / ".minecraft") if _local_appdata else None
    _bases = {BASE_DIR, Path.home() / ".minecraft"}
    if _store_mc is not None:
        _bases.add(_store_mc)
    # чистим оба возможных BASE_DIR (APPDATA и home) — Store Python может виртуализировать
    for base in _bases:
        try:
            for p in [base / "launcher_ui.json", base / "launcher_stats.json", base / "online_history.json", base / "files_hashes.json", base / "installed_version.json", base / "launcher_auth.json"]:
                if p.exists():
                    p.unlink(missing_ok=True)
                    removed.append(str(p))
        except Exception:
            pass
        try:
            for d in [base / "launcher_skins", base / "build.zip", base / "build.zip.part"]:
                if d.exists():
                    if d.is_dir():
                        shutil.rmtree(d, ignore_errors=True)
                    else:
                        d.unlink(missing_ok=True)
                    removed.append(str(d))
        except Exception:
            pass
    # основной BASE_DIR
    for p in [UI_SETTINGS_FILE, STATS_FILE, ONLINE_HISTORY_FILE, FILES_MANIFEST, VERSION_FILE, AUTH_FILE]:
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except Exception:
            pass
    for d in [SKIN_CACHE_DIR, BASE_DIR / "build.zip", BASE_DIR / "build.zip.part"]:
        try:
            if d.exists():
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink(missing_ok=True)
                removed.append(d.name)
        except Exception:
            pass
    return {"ok": True, "removed": removed}


# ------------------------- проверка Java -------------------------

def check_java():
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output_lines = (result.stderr or result.stdout or "").splitlines()
        version_line = output_lines[0].strip() if output_lines else "неизвестная версия"
        return {"ok": True, "message": version_line}
    except FileNotFoundError:
        return {
            "ok": False,
            "message": (
                "Java не найдена в системе. Обычно это не проблема — лаунчер "
                "сам ставит нужную версию при первом запуске. Если ошибка "
                "повторится, поставь Java вручную с adoptium.net"
            ),
        }
    except Exception as e:
        return {"ok": False, "message": f"Не удалось проверить Java: {e}"}


# ------------------------- память системы -------------------------

def get_system_memory():
    """Возвращает {'total_mb': int, 'available_mb': int} без сторонних зависимостей."""
    # Windows — GlobalMemoryStatusEx
    if os.name == "nt":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                raise OSError("GlobalMemoryStatusEx failed")
            if not stat.ullTotalPhys:
                raise OSError("GlobalMemoryStatusEx returned zero")
            return {
                "total_mb": int(stat.ullTotalPhys // (1024 * 1024)),
                "available_mb": int(stat.ullAvailPhys // (1024 * 1024)),
            }
        except Exception:
            pass
    # Linux — /proc/meminfo
    try:
        if Path("/proc/meminfo").exists():
            total_kb = avail_kb = 0
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
            if total_kb:
                return {"total_mb": total_kb // 1024, "available_mb": avail_kb // 1024}
    except Exception:
        pass
    # macOS — sysctl hw.memsize
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        total_bytes = int((out.stdout or "").strip())
        if total_bytes > 0:
            return {"total_mb": total_bytes // (1024 * 1024), "available_mb": 0}
    except Exception:
        pass
    return {"total_mb": 0, "available_mb": 0}


# ------------------------- самодиагностика -------------------------

def run_diagnostics():
    """Проверки: интернет → GitHub → место на диске → Java → версия → сервер."""
    checks = []

    def add(cid, label, ok, detail=""):
        checks.append({"id": cid, "label": label, "ok": bool(ok), "detail": str(detail or "")})

    # 1. Интернет (DNS+TCP до 1.1.1.1:53 и 8.8.8.8:53)
    try:
        ok = False
        detail = ""
        for dns in ("1.1.1.1", "8.8.8.8"):
            try:
                with socket.create_connection((dns, 53), timeout=3):
                    ok = True
                    detail = f"соединение до {dns}:53 OK"
                    break
            except Exception as e:
                detail = str(e)
        add("internet", "Интернет", ok, detail if ok else f"нет доступа к DNS: {detail}")
    except Exception as e:
        add("internet", "Интернет", False, str(e))

    # 2. GitHub API (releases/latest)
    try:
        r = _http().get(
            f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest",
            timeout=10,
        )
        if r.ok:
            data = r.json()
            add("github", "GitHub", True,
                f"релиз {data.get('tag_name', '?')}, файлов: {len(data.get('assets', []))}")
        else:
            add("github", "GitHub", False, f"HTTP {r.status_code}")
    except Exception as e:
        add("github", "GitHub", False, friendly_error(e))

    # 3. Место на диске (нужно минимум 2 ГБ под сборку+игру)
    try:
        import shutil
        # BASE_DIR на свежей установке может ещё не существовать —
        # disk_usage тогда кидает FileNotFoundError и диагностика врёт.
        try:
            BASE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        _du_path = str(BASE_DIR)
        if not Path(_du_path).exists():
            _du_path = str(Path(_du_path).resolve().parent)
            while not Path(_du_path).exists() and Path(_du_path).parent != Path(_du_path):
                _du_path = str(Path(_du_path).parent)
        free = shutil.disk_usage(_du_path).free
        free_gb = free / (1024 ** 3)
        add("disk", "Место на диске", free > 2 * 1024 ** 3,
            f"свободно {free_gb:.1f} ГБ в {BASE_DIR}")
    except Exception as e:
        add("disk", "Место на диске", False, str(e))

    # 4. Java
    try:
        j = check_java()
        add("java", "Java", j.get("ok", False), j.get("message", ""))
    except Exception as e:
        add("java", "Java", False, str(e))

    # 5. Установленная версия сборки
    try:
        local = load_local_manifest()
        if local:
            add("version", "Версия сборки", True,
                f"сборка {local.get('build_version', '?')} · Minecraft {local.get('minecraft_version', '?')}")
        else:
            add("version", "Версия сборки", False, "сборка ещё не установлена — нажми «Играть»")
    except Exception as e:
        add("version", "Версия сборки", False, str(e))

    # 6. Игровой сервер
    try:
        if not SERVER_ADDRESS:
            add("server", "Сервер", False, "адрес сервера не задан")
        else:
            ch, hh, port = resolve_server_address()
            st = ping_server_status(ch, port, handshake_host=hh)
            if st.get("online"):
                add("server", "Сервер", True,
                    f"{SERVER_ADDRESS}:{port} — {st['players']}/{st['max']} онлайн")
            else:
                add("server", "Сервер", False, f"{SERVER_ADDRESS}:{port} недоступен")
    except Exception as e:
        add("server", "Сервер", False, str(e))

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ------------------------- статистика игры -------------------------

DEFAULT_STATS = {
    "launches": 0,
    "play_seconds": 0,
    "first_launch": None,
    "last_launch": None,
    "session_start": None,  # незакрытая сессия (лаунчер закрыли раньше игры)
    "achievements": {},  # id -> "YYYY-MM-DD HH:MM" разблокировки
    "night_launches": 0,  # запуски с 0 до 5 утра
    "launch_dates": [],  # уникальные "YYYY-MM-DD", последние 30
    "repairs": 0,  # сколько раз чинили сборку
}

VETERAN_LAUNCHES = 50
VETERAN_HOURS = 50

# id, иконка, название, описание, условие проверяется в _achievement_unlocked()
ACHIEVEMENTS = [
    {"id": "first", "icon": "\U0001f331", "name": "Первый шаг",
     "desc": "Первый запуск игры"},
    {"id": "regular", "icon": "⚔️", "name": "Завсегдатай",
     "desc": "10 запусков"},
    {"id": "veteran", "icon": "★", "name": "Ветеран",
     "desc": f"{VETERAN_LAUNCHES} запусков или {VETERAN_HOURS} ч в игре"},
    {"id": "centurion", "icon": "\U0001f4af", "name": "Сотня",
     "desc": "100 запусков"},
    {"id": "hours10", "icon": "⏱️", "name": "Марафонец",
     "desc": "10 ч в игре"},
    {"id": "hours100", "icon": "\U0001f3c6", "name": "Легенда",
     "desc": "100 ч в игре"},
    {"id": "night_owl", "icon": "\U0001f989", "name": "Ночная сова",
     "desc": "3 запуска с 0 до 5 утра"},
    {"id": "week", "icon": "\U0001f4c5", "name": "Неделя в игре",
     "desc": "Запуски в 7 разных дней"},
    {"id": "fixer", "icon": "\U0001f527", "name": "Мастер починки",
     "desc": "Починил сборку кнопкой восстановления"},
]


def _achievement_unlocked(ach_id, stats):
    launches = int(stats.get("launches", 0))
    seconds = int(stats.get("play_seconds", 0))
    hours = seconds / 3600
    if ach_id == "first":
        return launches >= 1
    if ach_id == "regular":
        return launches >= 10
    if ach_id == "veteran":
        return launches >= VETERAN_LAUNCHES or hours >= VETERAN_HOURS
    if ach_id == "centurion":
        return launches >= 100
    if ach_id == "hours10":
        return hours >= 10
    if ach_id == "hours100":
        return hours >= 100
    if ach_id == "night_owl":
        return int(stats.get("night_launches", 0)) >= 3
    if ach_id == "week":
        dates = stats.get("launch_dates") or []
        return len(set(dates)) >= 7
    if ach_id == "fixer":
        return int(stats.get("repairs", 0)) >= 1
    return False


def _check_achievements(stats):
    """Сверяет условия, дописывает новые. Возвращает список id новых."""
    new = []
    try:
        unlocked = stats.get("achievements")
        if not isinstance(unlocked, dict):
            unlocked = {}
            stats["achievements"] = unlocked
        for ach in ACHIEVEMENTS:
            aid = ach["id"]
            if aid not in unlocked and _achievement_unlocked(aid, stats):
                unlocked[aid] = time.strftime("%Y-%m-%d %H:%M")
                new.append(aid)
    except Exception:
        pass
    return new


def _achievements_view(stats):
    unlocked = stats.get("achievements") if isinstance(stats.get("achievements"), dict) else {}
    out = []
    for ach in ACHIEVEMENTS:
        aid = ach["id"]
        out.append({"id": aid, "icon": ach["icon"], "name": ach["name"],
                    "desc": ach["desc"], "unlocked": aid in unlocked,
                    "date": unlocked.get(aid)})
    return out


def _load_stats():
    if STATS_FILE.exists():
        try:
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = dict(DEFAULT_STATS)
                out.update({k: data.get(k, v) for k, v in DEFAULT_STATS.items()})
                # санитайз новых полей (старые файлы без них + битые типы)
                if not isinstance(out.get("achievements"), dict):
                    out["achievements"] = {}
                try:
                    out["night_launches"] = max(0, int(out.get("night_launches", 0)))
                except (TypeError, ValueError):
                    out["night_launches"] = 0
                try:
                    out["repairs"] = max(0, int(out.get("repairs", 0)))
                except (TypeError, ValueError):
                    out["repairs"] = 0
                dates = out.get("launch_dates")
                if not isinstance(dates, list):
                    dates = []
                out["launch_dates"] = [str(d)[:10] for d in dates if isinstance(d, str) and d][:30]
                # копии мутабельных дефолтов, чтобы не шарить между вызовами
                out["achievements"] = dict(out["achievements"])
                out["launch_dates"] = list(out["launch_dates"])
                return out
        except Exception:
            pass
    out = dict(DEFAULT_STATS)
    out["achievements"] = {}
    out["launch_dates"] = []
    return out


def _save_stats(stats):
    _atomic_write(STATS_FILE, json.dumps(stats, ensure_ascii=False, indent=2))


def get_play_stats():
    stats = _load_stats()
    # Если прошлый раз лаунчер закрыли до конца игры — закрываем сессию сейчас,
    # чтобы часы не терялись. Кап 12 ч против абсурда.
    # Важно: сессию НЕ обнуляем, а сдвигаем session_start вперёд — иначе просмотр
    # статистики во время игры обрежет сессию и _finish_play_session потеряет хвост.
    if stats.get("session_start"):
        try:
            start = float(stats["session_start"])
            now = time.time()
            dur = max(0, min(now - start, 12 * 3600))
            stats["play_seconds"] = int(stats.get("play_seconds", 0) + dur)
            stats["session_start"] = now
        except Exception:
            stats["session_start"] = None
        _save_stats(stats)
    launches = int(stats.get("launches", 0))
    seconds = int(stats.get("play_seconds", 0))
    veteran = launches >= VETERAN_LAUNCHES or (seconds / 3600) >= VETERAN_HOURS
    new_unlocks = _check_achievements(stats)
    if new_unlocks:
        _save_stats(stats)
    return {
        "launches": launches,
        "play_seconds": seconds,
        "play_hours": round(seconds / 3600, 1),
        "first_launch": stats.get("first_launch"),
        "last_launch": stats.get("last_launch"),
        "veteran": veteran,
        "veteran_launches": VETERAN_LAUNCHES,
        "veteran_hours": VETERAN_HOURS,
        "achievements": _achievements_view(stats),
        "new_unlocks": new_unlocks,
    }


def _begin_play_session():
    stats = _load_stats()
    now = time.time()
    # если висит незакрытая сессия — сначала дописываем её хвост
    if stats.get("session_start"):
        try:
            dur = max(0, min(now - float(stats["session_start"]), 12 * 3600))
            stats["play_seconds"] = int(stats.get("play_seconds", 0) + dur)
        except Exception:
            pass
    stats["launches"] = int(stats.get("launches", 0)) + 1
    if not stats.get("first_launch"):
        stats["first_launch"] = time.strftime("%Y-%m-%d", time.localtime(now))
    stats["last_launch"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    stats["session_start"] = now
    # ночные запуски (0–5 утра) для ачивки «Ночная сова»
    try:
        if time.localtime(now).tm_hour < 5:
            stats["night_launches"] = int(stats.get("night_launches", 0)) + 1
    except Exception:
        pass
    # уникальные дни запусков для ачивки «Неделя в игре»
    try:
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        dates = stats.get("launch_dates") or []
        if not isinstance(dates, list):
            dates = []
        if today not in dates:
            dates.append(today)
        stats["launch_dates"] = dates[-30:]
    except Exception:
        pass
    _check_achievements(stats)
    _save_stats(stats)
    return stats


def _finish_play_session(proc):
    """Фоновый вотчер: ждёт завершения игры и дописывает длительность."""
    try:
        proc.wait()
    except Exception:
        return
    try:
        stats = _load_stats()
        if stats.get("session_start"):
            dur = max(0, min(time.time() - float(stats["session_start"]), 12 * 3600))
            stats["play_seconds"] = int(stats.get("play_seconds", 0) + dur)
            stats["session_start"] = None
            _save_stats(stats)
    except Exception:
        pass
    # после завершения игры синкуем свежую статистику/ачивки на сайт
    threading.Thread(target=_sync_quietly, daemon=True).start()
    # 38 звук/вибро + 36 уведомление о краше
    try:
        code = proc.returncode
        if code is not None and code != 0:
            _console_log(f"Игра крашнулась (код {code}) — смотри logs/latest.log и crash-reports/", level="error")
            try:
                send_notification("Краш игры", f"Minecraft завершился с кодом {code}")
            except Exception:
                pass
            # пуш звука в UI
            try:
                ref=_console_api_ref
                if ref and getattr(ref,"_window",None):
                    ref._window.evaluate_js("try{sfx.error(); if(navigator.vibrate) navigator.vibrate([120,60,120]);}catch(e){}")
            except Exception:
                pass
    except Exception:
        pass


# ------------------------- история онлайна (24 ч) -------------------------

ONLINE_HISTORY_TTL = 24 * 3600
ONLINE_HISTORY_MIN_GAP = 60  # не чаще точки в минуту


def _load_online_history():
    if ONLINE_HISTORY_FILE.exists():
        try:
            data = json.loads(ONLINE_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [p for p in data if isinstance(p, dict) and "t" in p]
        except Exception:
            pass
    return []


def _save_online_history(points):
    _atomic_write(ONLINE_HISTORY_FILE, json.dumps(points, ensure_ascii=False))


def _append_online_sample(players, max_players, online):
    try:
        now = time.time()
        points = _load_online_history()
        # чистим старше 24 ч
        points = [p for p in points if now - float(p.get("t", 0)) <= ONLINE_HISTORY_TTL]
        if points and (now - float(points[-1].get("t", 0)) < ONLINE_HISTORY_MIN_GAP):
            return points
        points.append({"t": int(now), "players": int(players or 0),
                       "max": int(max_players or 0), "online": bool(online)})
        _save_online_history(points[-1500:])
        return points
    except Exception:
        return []


def get_online_history():
    now = time.time()
    points = [p for p in _load_online_history()
              if now - float(p.get("t", 0)) <= ONLINE_HISTORY_TTL]
    return {"points": points}


# ------------------------- скины по нику -------------------------
# Превью и выбор скина лицензионного ника для offline-режима:
# lookup идёт в Mojang API, картинки — с Minotar по нику, выбор
# сохраняется в launcher_ui.json + PNG пинится в launcher_skins/.

SKIN_CACHE_DIR = BASE_DIR / "launcher_skins"
SKIN_CACHE_TTL = 7 * 24 * 3600
_NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def _mojang_get(url, timeout=10):
    """GET без GitHub заголовков — Mojang/Ely иначе 406."""
    return requests.get(url, timeout=timeout, headers={"User-Agent": "DragonHorizonLauncher/1.0"})

def _ely_lookup(nick):
    """Пробует Ely.by если Mojang не нашёл (32)."""
    try:
        r = _mojang_get(f"https://skinsystem.ely.by/textures/{nick}", timeout=8)
        if r.status_code != 200 or not r.content:
            return None
        data = r.json()
        textures = data.get("textures") or {}
        skin = textures.get("SKIN") or {}
        skin_url = skin.get("url")
        if not skin_url:
            return None
        meta = skin.get("metadata") or {}
        slim = meta.get("model") == "slim"
        # uuid у Ely — свой, генерируем псевдо
        pseudo_uuid = hashlib.md5(nick.lower().encode()).hexdigest()
        out = {"ok": True, "nickname": nick, "uuid": pseudo_uuid, "skin_url": skin_url, "slim": slim, "source": "ely.by"}
        try:
            dl = _download_skin_png(skin_url, pseudo_uuid)
            if dl: out["local_png"] = dl.as_uri()
        except Exception:
            pass
        return out
    except Exception:
        return None

def _cape_urls(nick):
    # (33) Minotar + Ely.by
    return {
        "minotar": f"https://minotar.net/cape/{nick}/64.png",
        "ely": f"https://skinsystem.ely.by/capes/{nick}.png",
    }

def skin_lookup(nickname):
    """Ник -> UUID/URL скина через Mojang API (с кэшем на 7 дней), fallback Ely.by."""
    nick = (nickname or "").strip()
    if not _NICK_RE.match(nick):
        return {"ok": False, "error": "Ник 3–16 символов: латиница, цифры, _"}
    cache_file = None
    try:
        SKIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = SKIN_CACHE_DIR / (nick.lower() + ".json")
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and time.time() - float(cached.get("t", 0)) < SKIN_CACHE_TTL:
                data = dict(cached.get("data") or {})
                data["cached"] = True
                # дополняем cape даже для кэша
                data["cape_urls"] = _cape_urls(data.get("nickname", nick))
                # офлайн превью — отдаём локальный png если есть, иначе пробуем скачать
                try:
                    uuid = data.get("uuid")
                    if uuid:
                        lp = SKIN_CACHE_DIR / (uuid + ".png")
                        if lp.exists() and lp.stat().st_size > 0:
                            data["local_png"] = lp.as_uri()
                        elif data.get("skin_url"):
                            dl = _download_skin_png(data["skin_url"], uuid)
                            if dl: data["local_png"] = dl.as_uri()
                except Exception:
                    pass
                return data
    except Exception:
        pass
    try:
        r = _mojang_get(f"https://api.mojang.com/users/profiles/minecraft/{nick}", timeout=10)
        if r.status_code in (404, 204) or not r.content:
            # пробуем Ely.by
            ely = _ely_lookup(nick)
            if ely:
                ely["cape_urls"] = _cape_urls(nick)
                if cache_file is not None:
                    try:
                        cache_file.write_text(json.dumps({"t": time.time(), "data": ely}, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                return ely
            return {"ok": False, "error": f"Ник «{nick}» не найден на Mojang (и Ely.by тоже)"}
        if r.status_code in (406, 429):
            # 406 из-за заголовков — пробуем Ely, иначе понятная ошибка
            ely = _ely_lookup(nick)
            if ely:
                ely["cape_urls"] = _cape_urls(nick)
                if cache_file is not None:
                    try:
                        cache_file.write_text(json.dumps({"t": time.time(), "data": ely}, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                return ely
            if r.status_code == 429:
                return {"ok": False, "error": "Mojang ограничил запросы, попробуй позже"}
            return {"ok": False, "error": f"Mojang вернул ошибку ({r.status_code}) — попробуй импорт .png"}
        r.raise_for_status()
        prof = r.json()
        uuid = (prof.get("id") or "").replace("-", "")
        name = prof.get("name", nick)
        if not uuid:
            return {"ok": False, "error": "Mojang вернул пустой профиль"}
        r2 = _mojang_get(f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}", timeout=10)
        r2.raise_for_status()
        props = r2.json().get("properties") or []
        skin_url, slim = None, False
        cape_url = None
        for p in props:
            if p.get("name") == "textures":
                try:
                    tex = json.loads(base64.b64decode(p.get("value", "")).decode("utf-8"))
                    skin = (tex.get("textures") or {}).get("SKIN") or {}
                    skin_url = skin.get("url")
                    slim = (skin.get("metadata") or {}).get("model") == "slim"
                    cape = (tex.get("textures") or {}).get("CAPE") or {}
                    cape_url = cape.get("url")
                except Exception:
                    pass
        if not skin_url:
            ely = _ely_lookup(nick)
            if ely:
                ely["cape_urls"] = _cape_urls(nick)
                if cache_file is not None:
                    try:
                        cache_file.write_text(json.dumps({"t": time.time(), "data": ely}, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                return ely
            return {"ok": False, "error": "У ника нет скина — только Steve/Alex по умолчанию"}
        data = {"ok": True, "nickname": name, "uuid": uuid,
                "skin_url": skin_url, "slim": slim, "cape_url": cape_url, "cape_urls": _cape_urls(name)}
        # best-effort кешируем png для офлайн превью
        try:
            dl = _download_skin_png(skin_url, uuid)
            if dl: data["local_png"] = dl.as_uri()
        except Exception:
            pass
        try:
            if cache_file is not None:
                cache_file.write_text(json.dumps({"t": time.time(), "data": data}, ensure_ascii=False),
                                      encoding="utf-8")
        except Exception:
            pass
        return data
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return {"ok": False, "error": "Нет соединения с Mojang. Проверь интернет."}
    except requests.exceptions.HTTPError as e:
        return {"ok": False, "error": f"Mojang вернул ошибку ({getattr(e.response, 'status_code', '?')})"}
    except Exception as e:
        return {"ok": False, "error": f"Не получилось получить скин: {e}"}


def _download_skin_png(skin_url, uuid):
    """Пинит PNG скина в кэш (best-effort, для офлайн-превью). Атомарно, с валидацией."""
    try:
        dest = SKIN_CACHE_DIR / (uuid + ".png")
        if dest.exists() and dest.stat().st_size > 0:
            # проверяем что это правда PNG, а не оборванный мусор
            try:
                with open(dest, "rb") as _f:
                    if _f.read(8) == b"\x89PNG\r\n\x1a\n":
                        return dest
            except Exception:
                pass
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
        SKIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".png.tmp")
        with requests.get(skin_url, timeout=15, stream=True, headers={"User-Agent": "DragonHorizonLauncher/1.0"}) as r:
            r.raise_for_status()
            if int(r.headers.get("content-length", 0) or 0) > 2 * 1024 * 1024:
                return None
            total = 0
            first = True
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if first:
                        if chunk[:8] != b"\x89PNG\r\n\x1a\n":
                            f.close()
                            try:
                                tmp.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return None
                        first = False
                    total += len(chunk)
                    if total > 2 * 1024 * 1024:
                        f.close()
                        try:
                            tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return None
                    f.write(chunk)
            if first:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
        tmp.replace(dest)
        return dest
    except Exception:
        try:
            if "tmp" in locals() and tmp is not None:
                tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def friendly_error(exc):
    # ConnectTimeout — наследник и ConnectionError, и Timeout: проверяем первым,
    # иначе всегда выигрывает ветка ConnectionError
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "Не удалось соединиться с GitHub (таймаут подключения). Проверь интернет и попробуй снова."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Нет соединения с интернетом. Проверь сеть и попробуй снова."
    if isinstance(exc, requests.exceptions.Timeout):
        return "GitHub слишком долго не отвечает. Попробуй ещё раз чуть позже."
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            return "Не найден релиз сборки на GitHub. Сообщи администратору сервера."
        if status in (401, 403):
            return "Нет доступа к репозиторию сборки — проблема с токеном доступа. Сообщи администратору сервера."
        if status == 429:
            return "GitHub временно ограничил запросы (слишком много обновлений подряд). Подожди немного и попробуй снова."
        if status is None:
            return "GitHub вернул ошибку без кода. Попробуй позже."
        return f"GitHub вернул ошибку ({status}). Попробуй позже."
    if isinstance(exc, zipfile.BadZipFile):
        return "Файл сборки повреждён при скачивании. Попробуй запустить ещё раз."
    if isinstance(exc, FileNotFoundError):
        # FileNotFoundError с filename — это отсутствующий файл (jar/библиотека),
        # а не java.exe: subprocess поднимает его БЕЗ filename. Не виним настройки.
        fname = str(getattr(exc, "filename", "") or "")
        if fname:
            return (
                f"Не хватает файла игры: {Path(fname).name}. "
                "Нажми «Починить сборку» и попробуй снова."
            )
        custom = ""
        try:
            custom = str(load_ui_settings().get("javaPath", "") or "").strip()
        except Exception:
            pass
        if custom:
            msg = "Не найден указанный в настройках путь к Java. Проверь путь или очисти поле, чтобы использовать Java по умолчанию."
        else:
            msg = (
                "Java не найдена на компьютере и не удалось установить её автоматически. "
                "Проверь интернет и повтори запуск — либо установи Java с adoptium.net и укажи путь в настройках."
            )
        return msg + " • " + _java_diag_tail()
    if isinstance(exc, PermissionError):
        return "Нет прав на запись в папку игры. Попробуй запустить лаунчер от имени администратора."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "На диске закончилось место. Освободи немного и попробуй снова."
    if isinstance(exc, json.JSONDecodeError):
        return "Сервер прислал повреждённые данные. Попробуй ещё раз."
    return f"Что-то пошло не так: {exc}"


def _split_jvm_args(extra):
    """Делит строку JVM-аргументов, не разрывая пробелы внутри кавычек.

    shlex с posix=False (Windows) кавычки не группирует, а с posix=True ест
    бэкслэши в путях (C:\\...) — поэтому свой разбор: куски в двойных кавычках
    остаются одним токеном (кавычки снимаются, Popen сам экранирует при сборке
    командной строки), остальное — по пробелам.
    """
    import shlex as _shlex
    if os.name != "nt":
        try:
            return _shlex.split(extra, posix=True)
        except ValueError:
            return extra.split()
    # Несбалансированные кавычки (вставили -Dfoo="a b без закрытия) —
    # regex развалил бы пробел внутри на куски, проще честный split по пробелам
    if extra.count('"') % 2 == 1:
        return extra.split()
    parts = re.findall(r'(?:[^\s"]|"[^"]*")+', extra)
    out = []
    for p in parts:
        # снимаем кавычки вокруг групп: -Dfoo="a b" -> -Dfoo=a b
        if '"' in p:
            p = re.sub(r'"([^"]*)"', r'\1', p)
        out.append(p)
    return out


def _apply_auto_join(options):
    """Автовход на сервер сразу после загрузки игры.

    Клиент 26.3 принимает только --quickPlayMultiplayer (старые
    --server/--port он больше не читает), поэтому выставляем опции
    quick play — minecraft-launcher-lib сам вставит аргументы
    по правилам версии."""
    if not SERVER_ADDRESS:
        return options
    address = SERVER_ADDRESS if not SERVER_PORT or SERVER_PORT == 25565 else f"{SERVER_ADDRESS}:{SERVER_PORT}"
    options["quickPlayMultiplayer"] = address
    options["quickPlayPath"] = str(INSTANCE_DIR / "quickPlay" / "lastServer.json")
    return options


def build_launch_options(username, ram_mb, java_path=None, extra_jvm_args=None):
    import minecraft_launcher_lib as mll
    options = mll.utils.generate_test_options()
    options["gameDirectory"] = str(INSTANCE_DIR)

    try:
        ram_mb = int(ram_mb)
    except (TypeError, ValueError):
        ram_mb = 4096
    ram_mb = max(512, min(65536, ram_mb))
    jvm_args = [f"-Xmx{ram_mb}M", f"-Xms{min(1024, ram_mb)}M"]
    if extra_jvm_args:
        jvm_args += _split_jvm_args(extra_jvm_args)
    options["jvmArguments"] = jvm_args

    options["username"] = (username or "Player").strip() or "Player"
    options["uuid"] = ""
    options["token"] = ""

    # Защита от неверного пути к Java: только файл, директория не подходит
    if java_path and Path(java_path).is_file():
        options["executablePath"] = java_path

    _apply_auto_join(options)
    return options


_gpu_name_cache = None


def _detect_gpu_name():
    """Имя видеокарты через PowerShell — один раз за сессию (нужно для диагностики FPS)."""
    global _gpu_name_cache
    if _gpu_name_cache is not None:
        return _gpu_name_cache
    _gpu_name_cache = "неизвестна"
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name -join ' | '"],
                capture_output=True, text=True, timeout=8,
            )
            name = (r.stdout or "").strip().splitlines()
            if name and name[0].strip():
                _gpu_name_cache = name[0].strip()
        except Exception:
            pass
    return _gpu_name_cache


def _log_launch_diagnostics(command, ram_mb):
    """Всё, что нужно, чтобы разобраться со слабым ПК/2 FPS по одному логу."""
    _console_log(f"Видеокарта: {_detect_gpu_name()}")
    try:
        mem = get_system_memory()
        _console_log(
            f"Система: RAM всего {mem.get('total_mb')} МБ, свободно {mem.get('available_mb')} МБ, "
            f"игре выдано {ram_mb} МБ"
        )
    except Exception as e:
        _console_log(f"Система: RAM неизвестна ({e})", level="error")

    java_exe = command[0] if command else None
    if java_exe:
        try:
            r = subprocess.run([java_exe, "-version"], capture_output=True, text=True, timeout=5)
            line = ((r.stderr or "") + (r.stdout or "")).splitlines()
            _console_log(f"Java: {line[0].strip() if line else 'версия неизвестна'} ({java_exe})")
        except Exception as e:
            _console_log(f"Java: не удалось опросить версию — {e}", level="error")

    try:
        opt = INSTANCE_DIR / "options.txt"
        if opt.exists():
            kv = {}
            for raw in opt.read_text(encoding="utf-8", errors="replace").splitlines():
                if ":" in raw:
                    k, v = raw.split(":", 1)
                    kv[k.strip()] = v.strip()
            keys = ("renderDistance", "simulationDistance", "graphics", "maxFps",
                    "particles", "mipmapLevels", "clouds", "entityShadows")
            _console_log("Графика: " + ", ".join(f"{k}={kv[k]}" for k in keys if k in kv))
            want = _load_files_manifest().get("options.txt")
            if want and _sha256_file(opt) != want:
                _console_log(
                    "options.txt отличается от пака (настройки менялись) — "
                    "нажми «Починить сборку», чтобы вернуть профиль «Максимум FPS»",
                    level="error",
                )
        else:
            _console_log("Графика: options.txt отсутствует", level="error")
    except Exception as e:
        _console_log(f"Графика: {e}", level="error")


def _force_high_performance_gpu(java_exe):
    """Windows: просим систему запускать java на дискретной видеокарте
    (ноутбуки часто пускают игру на встройке — отсюда и 2 FPS).
    Уже сделанный пользовательский выбор не перезаписываем."""
    if os.name != "nt" or not java_exe or not Path(java_exe).is_file():
        return
    try:
        import winreg
        key_path = r"Software\Microsoft\DirectX\UserGpuPreferences"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                 winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        except FileNotFoundError:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        with key:
            try:
                cur, _ = winreg.QueryValueEx(key, java_exe)
            except FileNotFoundError:
                cur = None
            if cur:
                _console_log(f"GPU: предпочтение уже задано ({cur}) для {Path(java_exe).name}")
                return
            winreg.SetValueEx(key, java_exe, 0, winreg.REG_SZ, "GpuPreference=2;")
            _console_log(f"GPU: {Path(java_exe).name} → высокопроизводительная видеокарта")
    except Exception as e:
        _console_log(f"GPU: не удалось выставить предпочтение — {e}", level="error")


def launch_game(version_id, username, ram_mb, java_path=None, extra_jvm_args=None):
    import minecraft_launcher_lib as mll
    options = build_launch_options(username, ram_mb, java_path=java_path, extra_jvm_args=extra_jvm_args)
    command = mll.command.get_minecraft_command(version_id, str(MINECRAFT_DIR), options)
    _console_log(f"Запуск: {' '.join(command[:4])} ... (RAM {ram_mb}M, ник {username})")
    _log_launch_diagnostics(command, ram_mb)
    _force_high_performance_gpu(command[0] if command else None)
    _begin_play_session()
    try:
        proc = subprocess.Popen(command, cwd=str(MINECRAFT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True, encoding="utf-8", errors="replace")
    except Exception:
        proc = subprocess.Popen(command, cwd=str(MINECRAFT_DIR))
    # поток чтения вывода игры -> консоль + вотчер завершения (один поток вместо двух)
    def _pump_and_watch():
        try:
            if proc.stdout:
                try:
                    for line in iter(proc.stdout.readline, ""):
                        if not line:
                            break
                        line = line.rstrip("\r\n")
                        if line:
                            _console_log(line, level="game")
                except Exception:
                    pass
                finally:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
            code = proc.wait()
            _console_log(f"Игра завершена (код {code})")
        except Exception:
            try:
                proc.wait()
            except Exception:
                pass
    threading.Thread(target=_pump_and_watch, daemon=True).start()
    # Вотчер допишет длительность сессии, когда игра закроется
    threading.Thread(target=_finish_play_session, args=(proc,), daemon=True).start()
    return proc


# --------------------------------- мост к JS ---------------------------------

class Api:
    def __init__(self):
        global _console_api_ref
        self._window = None
        self._game_proc = None
        self._last_version_id = None
        # автообновление лаунчера: пока _busy — идёт запуск/починка/сброс,
        # окно закрывать нельзя, иначе процесс оборвётся посреди операции
        self._busy = False
        self._launcher_updating = False
        self._update_proc = None
        _console_api_ref = self
        _console_log(f"Лаунчер запущен (сборка {_build_stamp()})")

    def _js(self, code):
        if not self._window:
            return
        try:
            self._window.evaluate_js(code)
        except Exception:
            pass

    def _status(self, text):
        self._js(f"window.updateStatus({json.dumps(text)})")
        _console_log(text)

    def _progress(self, frac):
        self._js(f"window.updateProgress({frac})")

    def _speed(self, speed_bps, eta_seconds):
        mb_s = speed_bps / (1024 * 1024)
        self._js(f"window.updateDownloadStats({mb_s:.2f}, {json.dumps(format_eta(eta_seconds))})")

    def _clear_speed(self):
        self._js("window.clearDownloadStats()")

    # ---------- вход через сайт ----------
    def auth_state(self):
        """Состояние входа для экрана логина: сессия есть и ещё жива?"""
        auth = load_auth()
        token = auth.get("token")
        if not token:
            return {"logged_in": False}
        prof = site_whoami(token)
        if prof.get("ok"):
            state = {
                "logged_in": True,
                "username": prof.get("username") or auth.get("username"),
                "role": prof.get("role"),
                "roleLabel": prof.get("roleLabel"),
            }
            _save_auth({**auth, "username": state["username"],
                        "role": state.get("role"), "roleLabel": state.get("roleLabel")})
            return state
        if prof.get("offline"):
            # нет сети: токен выдавал сайт — пускаем, но имя берём из файла
            return {"logged_in": True, "username": auth.get("username"),
                    "role": auth.get("role"), "roleLabel": auth.get("roleLabel"),
                    "offline": True}
        clear_auth()
        return {"logged_in": False, "error": prof.get("error") or "Сессия истекла"}

    def login(self, username, password):
        res = site_login(username, password)
        if res.get("ok"):
            _console_log(f"Вход выполнен: {res.get('username')}")
            try:
                self._js("window.onAuthStateChanged && window.onAuthStateChanged("
                         + json.dumps(res, ensure_ascii=False) + ")")
            except Exception:
                pass
        else:
            _console_log(f"Вход не удался: {res.get('error')}")
        return res

    def logout(self):
        auth = load_auth()
        site_logout(auth.get("token"))
        clear_auth()
        _console_log("Выход из аккаунта")
        return {"ok": True}

    def open_site(self, path="/"):
        """Открыть сайт в браузере ОС: ссылка внутри окна лаунчера увела бы
        сам интерфейс с рабочего экрана на сайт."""
        try:
            webbrowser.open(SITE_URL + (path or "/"))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _require_login(self):
        """Барьер перед запуском игры: None — можно, строка — текст отказа."""
        token = load_auth().get("token")
        if not token:
            return "Войди в аккаунт, чтобы играть"
        prof = site_whoami(token)
        if prof.get("ok") or prof.get("offline"):
            return None
        clear_auth()
        return "Сессия истекла — войди заново"

    def check_for_update(self):
        try:
            release = get_release_with_manifest()
            if not release:
                return {"outdated": False}
            manifest_asset = find_asset(release, lambda n: n == "launcher.json")
            if not manifest_asset:
                return {"outdated": False}
            manifest_resp = _http().get(
                manifest_asset["browser_download_url"], timeout=15
            )
            manifest_resp.raise_for_status()
            remote_manifest = manifest_resp.json()
        except Exception:
            return {"outdated": False}

        local = load_local_manifest()
        if not local:
            return {"outdated": False}

        remote_mc = remote_manifest.get("minecraft_version")
        local_mc = local.get("minecraft_version")
        if remote_mc and local_mc and remote_mc != local_mc:
            return {"outdated": True, "current": local_mc, "available": remote_mc}
        # модпак обновился при том же MC — тоже outdated
        remote_build = remote_manifest.get("build_version")
        local_build = local.get("build_version")
        if remote_build and local_build and remote_build != local_build:
            return {"outdated": True, "current": str(local_build), "available": str(remote_build),
                    "minecraft_version": local_mc}
        if remote_build and not local_build:
            return {"outdated": True, "current": local_mc, "available": remote_mc}
        return {"outdated": False}

    def check_launcher_update(self):
        """Автообновление лаунчера: тег релиза = версия, ассет = Setup exe."""
        try:
            info = check_launcher_update()
        except Exception as e:
            return {"needed": False, "error": str(e)}
        # из исходников (не установленный exe) обновление не предлагаем
        if info.get("needed") and (os.name != "nt" or not getattr(sys, "frozen", False)):
            info = dict(info, needed=False)
        proc = getattr(self, "_game_proc", None)
        info["game_running"] = bool(proc and proc.poll() is None)
        info["busy"] = bool(getattr(self, "_busy", False))
        return info

    def update_launcher(self):
        """Скачивает Setup, тихо ставит и перезапускает лаунчер.

        Лаунчер сначала закрывается сам (bat ждёт его pid), дальше всё
        делает батник: /VERYSILENT без UAC + запуск новой версии."""
        if getattr(self, "_launcher_updating", False):
            return {"ok": False, "error": "обновление уже идёт"}
        proc = getattr(self, "_game_proc", None)
        if proc and proc.poll() is None:
            return {"ok": False, "error": "игра запущена", "game_running": True}
        if getattr(self, "_busy", False):
            return {"ok": False, "error": "идёт установка или запуск", "busy": True}
        if os.name != "nt" or not getattr(sys, "frozen", False):
            return {"ok": False, "error": "обновление работает только в установленной версии"}
        self._launcher_updating = True  # сразу — гасит параллельные двойные вызовы
        try:
            info = check_launcher_update()
            if not info.get("needed"):
                self._launcher_updating = False
                return {"ok": False, "error": "нет обновления"}
            import tempfile
            # остаток отмены прошлой скачки не должен ронять обновление
            reset_cancel()
            dest = Path(tempfile.gettempdir()) / (Path(info["name"]).name or "launcher_setup.exe")
            global _update_setup_file
            _update_setup_file = dest  # прогресс из JS должен смотреть именно сюда
            size = int(info.get("size") or 0)
            if not (dest.exists() and size and dest.stat().st_size == size):
                _console_log(f"Обновление лаунчера: качаю {info['name']} ({size // (1024 * 1024)} МБ)")
                # exe отдаётся после входа — шлём ту же сессию, что и лаунчер
                download_file(info["url"], dest,
                              headers=_auth_cookie(load_auth().get("token")), retries=2)
            if not dest.exists() or dest.stat().st_size == 0:
                self._launcher_updating = False
                return {"ok": False, "error": "скачивание не удалось"}
            bat = Path(tempfile.gettempdir()) / "dh_launcher_update.bat"
            bat.write_text(_build_update_bat(os.getpid()), encoding="ascii")
            env = dict(os.environ)
            env["DH_SETUP"] = str(dest)
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            self._update_proc = subprocess.Popen(
                ["cmd", "/c", str(bat)], env=env, cwd=str(bat.parent),
                creationflags=flags, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _console_log(f"Обновление до {info.get('version')} скачано — лаунчер закрывается")
            threading.Timer(0.7, self._close_for_update).start()
            try:
                _update_cache_path().unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": True, "version": info.get("version")}
        except Exception as e:
            self._launcher_updating = False
            _console_log(f"Обновление лаунчера не удалось: {e}", level="error")
            return {"ok": False, "error": friendly_error(e)}

    def update_progress(self):
        """Процент скачивания Setup (для тоста «качаю… N%» из JS)."""
        try:
            if not getattr(self, "_launcher_updating", False):
                return {"active": False}
            # путь запоминает update_launcher: глоб glob по маске мог поймать
            # старый Setup прошлой версии, который лежит в %TEMP%
            dest = _update_setup_file
            if dest is None:
                import tempfile
                dest = Path(tempfile.gettempdir()) / "DragonHorizonLauncher-Setup.exe"
            size = dest.stat().st_size if dest.exists() else 0
            return {"active": True, "size": size, "path": dest.name}
        except Exception:
            return {"active": False}

    def _close_for_update(self):
        # батник убиваем: обновление само предложится при следующем старте
        if getattr(self, "_busy", False):
            p = getattr(self, "_update_proc", None)
            if p is not None:
                try:
                    p.terminate()
                except Exception:
                    pass
            self._update_proc = None
            self._launcher_updating = False
            self._js('try{showToast("Обновление лаунчера отложено — идёт установка или запуск", "ok")}catch(e){}')
            _console_log("Обновление лаунчера отложено: идёт установка/запуск", level="error")
            return
        try:
            if self._window:
                self._window.destroy()
                return
        except Exception:
            pass
        os._exit(0)

    def server_status(self):
        if not SERVER_ADDRESS:
            return {"online": False, "players": 0, "max": 0}
        # Кэш 10 сек: опросы JS (15 сек) + обновление графика (60 сек) часто
        # накладываются — не дёргаем сокет дважды подряд, мост не блокируется.
        now = time.monotonic()
        cached = getattr(self, "_status_cache", None)
        if cached and (now - cached[0] < 10):
            return cached[1]
        connect_host, handshake_host, port = resolve_server_address()
        result = ping_server_status(connect_host, port, handshake_host=handshake_host)
        # запасной вариант: прямой хост без SRV-цели (на случай блокировки DNS)
        if not result.get("online") and connect_host != SERVER_ADDRESS:
            result = ping_server_status(SERVER_ADDRESS, port, handshake_host=SERVER_ADDRESS)
        try:
            _append_online_sample(result.get("players", 0), result.get("max", 0),
                                  result.get("online", False))
        except Exception:
            pass
        self._status_cache = (now, result)
        return result

    def run_diagnostics(self):
        try:
            return run_diagnostics()
        except Exception as e:
            return {"ok": False, "checks": [], "error": str(e)}

    def play_stats(self):
        try:
            return get_play_stats()
        except Exception as e:
            return {"launches": 0, "play_seconds": 0, "play_hours": 0,
                    "veteran": False, "achievements": [], "new_unlocks": [],
                    "error": str(e)}

    def online_history(self):
        try:
            return get_online_history()
        except Exception as e:
            return {"points": [], "error": str(e)}

    def skin_lookup(self, nickname):
        try:
            return skin_lookup(nickname)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def skin_use(self, nickname):
        try:
            data = skin_lookup(nickname)
            if not data.get("ok"):
                return data
            local = _download_skin_png(data["skin_url"], data["uuid"])
            try:
                save_ui_settings({"skinNick": data["nickname"]})
            except Exception:
                pass
            data["local_png"] = local.as_uri() if local else None
            return data
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def repair(self):
        if getattr(self, "_launcher_updating", False):
            return {"ok": False, "error": "Лаунчер обновляется — повтори через секунду"}
        self._busy = True
        try:
            return repair_build()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._busy = False

    def factory_reset(self):
        if getattr(self, "_launcher_updating", False):
            return {"ok": False, "error": "Лаунчер обновляется — повтори через секунду"}
        self._busy = True
        try:
            return factory_reset()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._busy = False

    def check_java(self):
        return check_java()

    def system_memory(self):
        return get_system_memory()

    def get_ui_settings(self):
        return load_ui_settings()

    def save_ui_settings(self, data):
        try:
            if not isinstance(data, dict):
                return {"ok": False, "error": "Некорректные настройки"}
            _console_log(f"[dbg] save_ui_settings ram={data.get('ram')!r} nick={data.get('nickname')!r} keys={sorted(map(str, data.keys()))}")
            return save_ui_settings(data or {})
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def has_ui_settings(self):
        # Нужен фронту, чтобы отличить первую установку (автовыбор RAM)
        # от сохранённых значений: load/get возвращают дефолты и в том,
        # и в другом случае.
        return UI_SETTINGS_FILE.exists()

    def play(self, nickname, ram_mb, settings=None):
        # вход обязателен: без живой сессии игры не будет (барьер и в UI,
        # и здесь — снять оверлей в консоли недостаточно)
        denied = self._require_login()
        if denied:
            try:
                self._js(f"window.launchFailed({json.dumps(denied)})")
            except Exception:
                pass
            return {"ok": False, "error": denied}
        # автообновление: в финальной фазе обновления окно закрывается —
        # новый запуск в этот момент не пускаем, иначе он оборвётся
        if getattr(self, "_launcher_updating", False):
            msg = "Лаунчер обновляется — нажми «Играть» ещё раз через секунду"
            try:
                self._js(f"window.launchFailed({json.dumps(msg)})")
            except Exception:
                pass
            return {"ok": False, "error": msg}
        self._busy = True
        try:
            return self._play_impl(nickname, ram_mb, settings)
        finally:
            self._busy = False

    def _play_impl(self, nickname, ram_mb, settings=None):
        if not isinstance(settings, dict):
            settings = {}
        # Валидация ДО сети: иначе плохой ник/RAM дают сырой трейсбек вместо понятной ошибки
        clean_nick = (nickname or "").strip() if isinstance(nickname, str) else ""
        if not re.match(r"^[A-Za-z0-9_]{3,16}$", clean_nick):
            msg = "Ник 3–16 символов: латиница, цифры, _ (без пробелов)"
            try:
                self._js(f"window.launchFailed({json.dumps(msg)})")
            except Exception:
                pass
            return {"ok": False, "error": msg}
        try:
            clean_ram = int(ram_mb)
        except (TypeError, ValueError):
            msg = "Некорректный объём памяти — выбери значение ползунком в настройках"
            try:
                self._js(f"window.launchFailed({json.dumps(msg)})")
            except Exception:
                pass
            return {"ok": False, "error": msg}
        clean_ram = max(512, min(65536, clean_ram))
        nickname = clean_nick
        ram_mb = clean_ram
        # JS шлёт camelCase (javaPath/jvmArgs/closeOnLaunch/launcherOnly) —
        # принимаем оба стиля, иначе путь к Java и JVM-аргументы теряются
        def _pick(*names, default=None):
            for n in names:
                if settings.get(n) not in (None, ""):
                    return settings.get(n)
            return default
        launcher_only = bool(_pick("launcher_only", "launcherOnly", default=False))
        _java_raw = _pick("java_path", "javaPath", default="")
        _jvm_raw = _pick("jvm_args", "jvmArgs", default="")
        _close_raw = bool(_pick("close_on_launch", "closeOnLaunch", default=False))
        # Бэкап-сохранение: даже если JS не смог сохранить, ник и настройки не потеряются
        try:
            save_ui_settings({
                "nickname": nickname,
                "ram": int(ram_mb),
                "javaPath": _java_raw or "",
                "jvmArgs": _jvm_raw or "",
                "closeOnLaunch": _close_raw,
                "launcherOnly": launcher_only,
            })
            _push_recent_nick(nickname)
        except Exception:
            pass
        java_path = (str(_java_raw or "").strip()) or None
        jvm_args = (str(_jvm_raw or "").strip()) or None
        close_on_launch = _close_raw
        reset_cancel()
        # сбрасываем прогресс установщика от прошлого запуска — иначе прыгает
        self._mc_val = 0
        self._mc_max = 0
        _console_log(f"Нажат Играть — ник {nickname}, RAM {ram_mb}M")
        try:
            self._status("Проверяем обновления\u2026")
            _console_log("Проверяем обновления на GitHub...")
            if is_cancelled():
                raise DownloadCancelled("Загрузка отменена пользователем")
            # последний релиз с launcher.json (свой пак-репозиторий);
            # если его почему-то нет — обычный latest даст настоящую ошибку сети
            release = get_release_with_manifest() or get_latest_release()
            if is_cancelled():
                raise DownloadCancelled("Загрузка отменена пользователем")

            manifest_asset = find_asset(release, lambda n: n == "launcher.json")
            build_asset = find_asset(release, lambda n: n.endswith(".zip"))
            if not manifest_asset or not build_asset:
                raise RuntimeError("В релизе нет launcher.json или zip-архива сборки")

            manifest_resp = _http().get(manifest_asset["browser_download_url"], timeout=15)
            manifest_resp.raise_for_status()
            manifest = manifest_resp.json()

            meta_text = "сборка " + str(manifest.get("build_version", "")) + " \u00b7 Minecraft " + str(manifest.get("minecraft_version", ""))
            self._js(f"window.setModpackInfo(null, {json.dumps(meta_text)})")

            if needs_update(manifest):
                self._status(f"Скачиваем сборку {manifest.get('build_version', '')}\u2026")
                update_build(
                    build_asset,
                    manifest,
                    progress_cb=self._progress,
                    status_cb=self._status,
                    speed_cb=self._speed,
                )
            else:
                self._status("Сборка уже актуальна")
                self._progress(1)

            self._clear_speed()
            self._status("Проверяем Minecraft и модлоадер\u2026")
            self._progress(0)

            def mc_progress(kind, val):
                if kind == "status":
                    self._status(val)
                elif kind == "progress":
                    self._mc_val = val
                elif kind == "max" and val:
                    self._mc_max = val
                if getattr(self, "_mc_max", 0):
                    self._progress(getattr(self, "_mc_val", 0) / self._mc_max)

            version_id = ensure_minecraft_installed(manifest, progress_cb=mc_progress)
            self._last_version_id = version_id

            if launcher_only:
                self._status("Лаунчер-режим: игра не запускается")
                self._progress(1)
                self._js("window.launchDone()")
                send_notification("Готово", "Сборка обновлена — игра не запускалась (режим лаунчера)")
                return {"ok": True, "launcher_only": True}

            # Java: ищем рабочий путь (настройка → рантайм сборки → PATH),
            # иначе mll подставит голый "java" и падает с FileNotFoundError
            java_resolved = resolve_java_path(
                version_id,
                mc_version=manifest.get("minecraft_version"),
                progress_cb=mc_progress,
            )
            if not java_resolved:
                _console_log("Java не резолвится", level="error")
                msg = (
                    "Java не найдена на компьютере, и автоматическая установка не удалась. "
                    "Проверь интернет и нажми «Играть» ещё раз — либо установи Java с adoptium.net "
                    "и укажи её путь в настройках лаунчера."
                )
                msg += " • " + _java_diag_tail()
                self._clear_speed()
                self._js(f"window.launchFailed({json.dumps(msg)})")
                return {"ok": False, "error": msg}

            # BlackList анти-чит
            cheats = scan_for_cheats(jvm_args)
            if cheats:
                details = "\\n".join(cheats[:5])
                msg = f"Читерство запрещено! Обнаружены читы:\\n{details}\\nУдали читы из mods и нажми Починить сборку."
                _console_log(f"Анти-чит блок: {cheats}", level="error")
                self._clear_speed()
                self._js(f"window.launchFailed({json.dumps(msg)})")
                send_notification("Читерство запрещено", "Удали читы из mods")
                try:
                    self._window.evaluate_js("try{sfx.error();}catch(e){}")
                except Exception:
                    pass
                return {"ok": False, "error": msg, "cheats": cheats}
            self._status("Запускаем игру\u2026")
            self._progress(1)
            proc = launch_game(version_id, nickname, int(ram_mb), java_path=java_resolved, extra_jvm_args=jvm_args)
            self._game_proc = proc

            self._js("window.launchDone()")
            send_notification("Игра запущена", f"{nickname} — Minecraft {manifest.get('minecraft_version','')}")

            if close_on_launch:
                # 13 таймер автозакрытия
                try:
                    delay=int(load_ui_settings().get("autoCloseDelay",0) or 0)
                except Exception:
                    delay=0
                if delay:
                    for i in range(delay,0,-1):
                        self._status(f"Закрываем через {i}с…")
                        _console_log(f"Автозакрытие через {i}с")
                        time.sleep(1)
                        if is_cancelled():
                            break
                else:
                    time.sleep(1.2)
                if self._window:
                    try:
                        self._window.destroy()
                    except Exception:
                        pass
            return {"ok": True, "version": version_id}
        except DownloadCancelled as e:
            self._clear_speed()
            reset_cancel()
            self._js(f"window.launchFailed({json.dumps(str(e))})")
            self._js("window.showIdle()")
            return {"ok": False, "cancelled": True, "error": str(e)}
        except Exception as e:
            self._clear_speed()
            msg = friendly_error(e)
            _console_log(f"Ошибка: {msg} ({e})", level="error")
            self._js(f"window.launchFailed({json.dumps(msg)})")
            send_notification("Ошибка", msg)
            return {"ok": False, "error": msg}

    def notify(self, title, message):
        send_notification(str(title)[:80], str(message)[:200])
        return {"ok": True}

    def cancel_download(self):
        request_cancel()
        return {"ok": True, "message": "Отмена запрошена"}

    def kill_game(self):
        try:
            proc = getattr(self, "_game_proc", None)
            if not proc or proc.poll() is not None:
                return {"ok": False, "error": "Игра не запущена"}
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            _console_log("Игра принудительно завершена (зависла)", level="error")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def restart_game(self):
        try:
            import minecraft_launcher_lib as mll
            proc = getattr(self, "_game_proc", None)
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                time.sleep(1)
            if not getattr(self, "_last_version_id", None):
                return {"ok": False, "error": "Игра ещё не запускалась — нажми «Играть»"}
            options = mll.utils.generate_test_options()
            options["gameDirectory"] = str(INSTANCE_DIR)
            settings = load_ui_settings()
            try:
                ram_mb = max(512, min(65536, int(settings.get("ram", 4096))))
            except (TypeError, ValueError):
                ram_mb = 4096
            java_path = resolve_java_path(self._last_version_id)
            if not java_path:
                _console_log("Рестарт: Java не резолвится", level="error")
                return {
                    "ok": False,
                    "error": "Java не найдена — нажми «Играть», чтобы лаунчер докачал её заново • " + _java_diag_tail(),
                }
            jvm_args = settings.get("jvmArgs", "") or None
            jvm_args_list = [f"-Xmx{ram_mb}M", f"-Xms{min(1024, ram_mb)}M"]
            if jvm_args:
                jvm_args_list += _split_jvm_args(jvm_args)
            options["jvmArguments"] = jvm_args_list
            options["username"] = settings.get("nickname", "Player").strip() or "Player"
            options["uuid"] = ""
            options["token"] = ""
            if java_path and Path(java_path).is_file():
                options["executablePath"] = java_path
            _apply_auto_join(options)
            command = mll.command.get_minecraft_command(self._last_version_id or "unknown", str(MINECRAFT_DIR), options)
            try:
                proc = subprocess.Popen(command, cwd=str(MINECRAFT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True, encoding="utf-8", errors="replace")
            except Exception:
                proc = subprocess.Popen(command, cwd=str(MINECRAFT_DIR))
            self._game_proc = proc
            _begin_play_session()
            # читаем вывод перезапущенной игры в консоль (как в launch_game),
            # иначе рестарт молча теряет логи + вотчер ниже допишет сессию
            def _pump_restarted():
                try:
                    if proc.stdout:
                        try:
                            for line in iter(proc.stdout.readline, ""):
                                if not line:
                                    break
                                line = line.rstrip("\r\n")
                                if line:
                                    _console_log(line, level="game")
                        except Exception:
                            pass
                        finally:
                            try:
                                proc.stdout.close()
                            except Exception:
                                pass
                    code = proc.wait()
                    _console_log(f"Игра завершена (код {code})")
                except Exception:
                    try:
                        proc.wait()
                    except Exception:
                        pass
            threading.Thread(target=_pump_restarted, daemon=True).start()
            threading.Thread(target=_finish_play_session, args=(proc,), daemon=True).start()
            return {"ok": True, "message": "Игра перезапущена"}
        except Exception as e:
            return {"ok": False, "error": friendly_error(e)}

    def resource_packs(self):
        try:
            rp_dir = MINECRAFT_DIR / "resourcepacks"
            rp_dir.mkdir(parents=True, exist_ok=True)
            packs = []
            for f in rp_dir.iterdir():
                if f.suffix.lower() in (".zip", ".mcpack"):
                    try:
                        h = _sha256_file(f)
                        size = f.stat().st_size
                        info = {"name": f.name, "path": str(f), "hash": h, "size": size}
                        if f.suffix.lower() == ".zip":
                            try:
                                with zipfile.ZipFile(f, "r") as zf:
                                    names = zf.namelist()
                                    info["files"] = len([n for n in names if not n.endswith("/")])
                                    pack_json = [n for n in names if "pack.mcmeta" in n]
                                    if pack_json:
                                        meta = json.loads(zf.read(pack_json[0]).decode("utf-8"))
                                        # спека: pack.pack_format (int), description может быть строкой или dict
                                        info["pack_format"] = meta.get("pack", {}).get("pack_format", "?")
                                        info["format_version"] = info["pack_format"]
                                        desc = meta.get("pack", {}).get("description", "")
                                        info["description"] = desc if isinstance(desc, str) else json.dumps(desc, ensure_ascii=False)
                            except Exception:
                                pass
                        packs.append(info)
                    except Exception:
                        pass
            return {"ok": True, "packs": packs}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def install_resource_pack(self, zip_url, pack_name):
        try:
            rp_dir = MINECRAFT_DIR / "resourcepacks"
            rp_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(pack_name).name)[:80] or "pack.zip"
            if not safe.lower().endswith((".zip",".mcpack")): safe += ".zip"
            dest = rp_dir / safe
            # защита от traversal: dest внутри rp_dir
            try:
                dest.resolve().relative_to(rp_dir.resolve())
            except ValueError:
                return {"ok": False, "error": "Недопустимое имя файла"}
            # повторная установка тем же именем должна обновлять, а не no-op:
            # download_file пропускает готовый zip — удаляем старый заранее
            try:
                dest.unlink(missing_ok=True)
                dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
            except Exception:
                pass
            download_file(zip_url, dest, progress_cb=None, retries=2)
            if not dest.exists() or dest.stat().st_size == 0 or not zipfile.is_zipfile(dest):
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                return {"ok": False, "error": "Скачанный файл не похож на ресурспак (битый zip)"}
            return {"ok": True, "name": safe}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_resource_pack(self, pack_name):
        try:
            rp_dir = MINECRAFT_DIR / "resourcepacks"
            safe = Path(pack_name).name
            f = rp_dir / safe
            try:
                f.resolve().relative_to(rp_dir.resolve())
            except ValueError:
                return {"ok": False, "error": "Недопустимое имя"}
            if f.exists():
                f.unlink()
                return {"ok": True, "removed": safe}
            return {"ok": False, "error": "Файл не найден"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def game_stats(self):
        try:
            proc = getattr(self, "_game_proc", None)
            if not proc or proc.poll() is not None:
                return {"ok": False, "running": False}
            if not psutil:
                return {"ok": False, "running": True, "cpu": 0, "memory_mb": 0}
            p = psutil.Process(proc.pid)
            # первый замер cpu_percent всегда 0 — прогреваем и меряем заново
            try:
                p.cpu_percent(interval=None)
                for _ch in p.children(recursive=True):
                    try:
                        _ch.cpu_percent(interval=None)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.2)
            try:
                cpu = p.cpu_percent(interval=None)
            except Exception:
                cpu = 0
            mem = 0
            try:
                mem = p.memory_info().rss / (1024 ** 2)
            except Exception:
                pass
            total_cpu = cpu
            total_mem = mem
            try:
                children = p.children(recursive=True)
            except Exception:
                children = []
            for child in children:
                try:
                    total_cpu += child.cpu_percent(interval=None)
                    total_mem += child.memory_info().rss / (1024 ** 2)
                except Exception:
                    pass
            server_online = None
            server_max = None
            if SERVER_ADDRESS:
                # переиспользуем ping_server_status вместо дублирования протокола:
                # дубль расходился с основным пингом и висел до 2с на мосту каждые 2с опроса
                try:
                    ch, hh, port = resolve_server_address()
                    st = ping_server_status(ch, port, timeout=1.5, handshake_host=hh)
                    if st.get("online"):
                        server_online = st.get("players", 0)
                        server_max = st.get("max", 0)
                except Exception:
                    pass
            return {
                "ok": True,
                "running": True,
                "cpu": round(total_cpu, 1),
                "memory_mb": round(mem, 1),
                "children_memory_mb": round(total_mem - mem, 1) if total_mem > mem else 0,
                "total_memory_mb": round(total_mem, 1),
                "server_online": server_online,
                "server_online_max": server_max,
            }
        except Exception as e:
            return {"ok": False, "running": False, "error": str(e)}

    def get_theme(self):
        # контракт с JS: объект {theme}, а не голая строка (иначе t.theme undefined)
        try:
            if UI_SETTINGS_FILE.exists():
                data = json.loads(UI_SETTINGS_FILE.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and data.get("theme") in ("dark", "light"):
                    return {"ok": True, "theme": data["theme"]}
            return {"ok": True, "theme": "dark"}
        except Exception:
            return {"ok": True, "theme": "dark"}

    def set_theme(self, theme):
        try:
            theme = theme if theme in ("dark", "light") else "dark"
            data = _normalize_ui_settings(load_ui_settings())
            data["theme"] = theme
            save_ui_settings(data)
            return {"ok": True, "theme": theme}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_launcher_settings(self):
        try:
            data = load_ui_settings()
            return {
                "launcherOnly": bool(data.get("launcherOnly", False)),
                "nickname": data.get("nickname", ""),
                "ram": data.get("ram", 4096),
                "javaPath": data.get("javaPath", ""),
                "jvmArgs": data.get("jvmArgs", ""),
                "closeOnLaunch": bool(data.get("closeOnLaunch", False)),
            }
        except Exception:
            return {}

    def copy_clipboard(self, text):
        try:
            import subprocess as _sp
            if os.name == "nt":
                _sp.run(["clip"], input=str(text), text=True, timeout=3, check=True)
            else:
                # linux: xclip/xsel, mac: pbcopy — пробуем по очереди до первого успеха
                done = False
                for cmd in [["xclip","-selection","clipboard"], ["xsel","--clipboard"], ["pbcopy"]]:
                    try:
                        _sp.run(cmd, input=str(text), text=True, timeout=3, check=True)
                        done = True
                        break
                    except FileNotFoundError:
                        continue
                    except _sp.CalledProcessError:
                        continue
                if not done:
                    return {"ok": False, "error": "буфер обмена недоступен"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def toggle_fullscreen(self):
        try:
            w = getattr(self, "_window", None)
            if not w:
                return {"ok": False, "error": "no window"}
            # pywebview: toggle_fullscreen, maximize, restore
            if hasattr(w, "toggle_fullscreen"):
                w.toggle_fullscreen()
            elif hasattr(w, "maximize"):
                # fallback: maximize/restore
                try:
                    w.maximize()
                except Exception:
                    w.restore()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_fullscreen(self, enable):
        try:
            w = getattr(self, "_window", None)
            if not w:
                return {"ok": False, "error": "no window"}
            # только одно действие: toggle_fullscreen приоритетнее, иначе maximize/restore
            if enable:
                if hasattr(w, "toggle_fullscreen"):
                    w.toggle_fullscreen()
                elif hasattr(w, "maximize"):
                    w.maximize()
            else:
                if hasattr(w, "restore"):
                    w.restore()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def window_state(self):
        try:
            w = getattr(self, "_window", None)
            if not w:
                return {"ok": False}
            # best-effort: pywebview не всегда отдаёт state
            return {"ok": True, "width": getattr(w, "width", None), "height": getattr(w, "height", None)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 18 импорт скина из файла .png
    def import_skin_file(self, data_b64, filename="imported.png"):
        try:
            import re as _re
            SKIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", (filename or "skin.png").strip()) or "skin.png"
            if not safe.lower().endswith(".png"):
                safe += ".png"
            dest = SKIN_CACHE_DIR / f"imported_{int(time.time())}_{safe}"
            raw_b64 = str(data_b64).split(",")[-1] if "," in str(data_b64) else str(data_b64)
            # грубая проверка ДО декодирования: base64 ~4/3 от бинаря, 2МБ -> ~2.8М символов
            if len(raw_b64) > 3 * 1024 * 1024:
                return {"ok": False, "error": "Файл слишком большой (>2 МБ)"}
            try:
                raw = base64.b64decode(raw_b64, validate=True)
            except Exception:
                return {"ok": False, "error": "Не получилось прочитать base64"}
            if len(raw) > 2*1024*1024:
                return {"ok": False, "error": "Файл слишком большой (>2 МБ)"}
            # базовая проверка PNG сигнатуры
            if raw[:8] != b"\x89PNG\r\n\x1a\n":
                return {"ok": False, "error": "Это не PNG"}
            dest.write_bytes(raw)
            # skinNick тут НЕ трогаем: имя файла — не ник для lookup, привязка
            # делается позже через skin_use с реальным ником из поля ввода
            return {"ok": True, "uri": dest.as_uri(), "path": str(dest), "name": dest.name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 19 галерея последних 5 скинов
    def get_recent_skins(self):
        try:
            SKIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            files = []
            for p in SKIN_CACHE_DIR.iterdir():
                if p.suffix.lower() in (".png", ".json"):
                    try:
                        files.append((p.stat().st_mtime, p))
                    except (FileNotFoundError, OSError):
                        continue
            files.sort(reverse=True)
            out = []
            for _, p in files[:10]:
                if p.suffix.lower() == ".png":
                    try:
                        out.append({"name": p.name, "uri": p.as_uri(), "type": "png", "mtime": int(p.stat().st_mtime)})
                    except (FileNotFoundError, OSError):
                        continue
                elif p.suffix.lower() == ".json":
                    try:
                        d = json.loads(p.read_text(encoding="utf-8"))
                        data = d.get("data") or {}
                        out.append({"name": p.name, "uri": data.get("local_png") or None, "type": "json", "nickname": data.get("nickname", p.stem), "uuid": data.get("uuid"), "mtime": int(p.stat().st_mtime)})
                    except Exception:
                        pass
                if len(out) >= 5:
                    break
            return {"ok": True, "skins": out}
        except Exception as e:
            return {"ok": False, "error": str(e), "skins": []}

    # 21 скриншоты
    def get_screenshots(self):
        try:
            scr_dir = MINECRAFT_DIR / "screenshots"
            if not scr_dir.exists():
                return {"ok": True, "shots": []}
            try:
                entries = list(scr_dir.iterdir())
            except OSError:
                return {"ok": True, "shots": []}
            with_mtime = []
            for p in entries:
                try:
                    with_mtime.append((p.stat().st_mtime, p))
                except (FileNotFoundError, OSError):
                    continue
            with_mtime.sort(reverse=True)
            shots = []
            for _, p in with_mtime:
                if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    try:
                        shots.append({"name": p.name, "uri": p.as_uri(), "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
                    except (FileNotFoundError, OSError):
                        continue
                    if len(shots) >= 20:
                        break
            return {"ok": True, "shots": shots}
        except Exception as e:
            return {"ok": False, "error": str(e), "shots": []}

    def open_screenshots_folder(self):
        try:
            scr_dir = MINECRAFT_DIR / "screenshots"
            scr_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(scr_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(scr_dir)])
            else:
                subprocess.Popen(["xdg-open", str(scr_dir)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- консоль ----------
    def get_console_log(self):
        try:
            with _CONSOLE_LOCK:
                lines = list(_CONSOLE_BUFFER)
            return {"ok": True, "lines": lines}
        except Exception as e:
            return {"ok": False, "error": str(e), "lines": []}

    def clear_console_log(self):
        try:
            _console_clear()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_latest_log(self, lines=200):
        try:
            try:
                lines = max(1, min(2000, int(lines)))
            except (TypeError, ValueError):
                lines = 200
            log = MINECRAFT_DIR / "logs" / "latest.log"
            if not log.exists():
                return {"ok": True, "text": "", "exists": False}
            # хвост без загрузки всего файла в память (лог бывает сотни МБ)
            try:
                size = log.stat().st_size
            except OSError:
                return {"ok": True, "text": "", "exists": False}
            parts = []
            with open(log, "rb") as f:
                # читаем с конца блоками по 64КБ пока не наберём lines строк (макс 2МБ)
                chunk_size = 65536
                pos = size
                buf = b""
                while pos > 0 and buf.count(b"\n") <= lines and len(buf) < 2 * 1024 * 1024:
                    step = min(chunk_size, pos)
                    pos -= step
                    f.seek(pos)
                    buf = f.read(step) + buf
            text = buf.decode("utf-8", errors="replace")
            out = text.splitlines()
            if len(out) > lines:
                out = out[-lines:]
            return {"ok": True, "text": "\n".join(out), "exists": True}
        except Exception as e:
            return {"ok": False, "error": str(e), "text": ""}

    def get_crash_reports(self):
        try:
            crash_dir = MINECRAFT_DIR / "crash-reports"
            if not crash_dir.exists():
                return {"ok": True, "reports": []}
            reports = []
            try:
                entries = list(crash_dir.iterdir())
            except OSError:
                return {"ok": True, "reports": []}
            with_mtime = []
            for p in entries:
                try:
                    with_mtime.append((p.stat().st_mtime, p))
                except (FileNotFoundError, OSError):
                    continue
            with_mtime.sort(reverse=True)
            for _, p in with_mtime:
                if p.suffix == ".txt":
                    try:
                        reports.append({"name": p.name, "mtime": int(p.stat().st_mtime), "size": p.stat().st_size})
                    except (FileNotFoundError, OSError):
                        continue
                    if len(reports) >= 10:
                        break
            return {"ok": True, "reports": reports}
        except Exception as e:
            return {"ok": False, "error": str(e), "reports": []}

    def open_logs_folder(self):
        try:
            d = MINECRAFT_DIR / "logs"
            d.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(d))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_game_folder(self):
        """Открыть папку игры (инстанс модов) — для саппорта и «где мои моды»."""
        try:
            MINECRAFT_DIR.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(MINECRAFT_DIR))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(MINECRAFT_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(MINECRAFT_DIR)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def versions(self):
        """Версии лаунчера и установленной сборки — для футера окна."""
        build = ""
        mc = ""
        m = load_local_manifest()
        if isinstance(m, dict):
            build = str(m.get("build_version") or "")
            mc = str(m.get("minecraft_version") or "")
        return {"launcher": LAUNCHER_VERSION, "build": build, "minecraft": mc}

    def open_crash_folder(self):
        try:
            d = MINECRAFT_DIR / "crash-reports"
            d.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(d))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def console_log(self, text):
        try:
            _console_log(str(text)[:2000])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 39 экспорт
    def export_logs(self):
        try:
            return export_logs()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_exported_logs(self):
        try:
            r=export_logs()
            if r.get("ok") and r.get("path"):
                p=Path(r["path"])
                if os.name=="nt":
                    os.startfile(str(p.parent))
                elif sys.platform=="darwin":
                    subprocess.Popen(["open", str(p.parent)])
                else:
                    subprocess.Popen(["xdg-open", str(p.parent)])
            return r
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # BlackList check
    def check_cheats(self, jvm_args=None):
        try:
            found=scan_for_cheats(jvm_args)
            return {"ok": True, "cheats": found, "clean": len(found)==0}
        except Exception as e:
            return {"ok": False, "error": str(e), "cheats": []}

    # 40 антивирус
    def check_antivirus(self):
        try:
            return check_antivirus()
        except Exception as e:
            return {"defender_running": False, "error": str(e)}

    # 21 шейдеры
    def shader_packs(self):
        try:
            d=MINECRAFT_DIR / "shaderpacks"
            d.mkdir(parents=True, exist_ok=True)
            out=[]
            for f in d.iterdir():
                if f.suffix.lower() in (".zip",):
                    try:
                        out.append({"name": f.name, "size": f.stat().st_size, "hash": _sha256_file(f)})
                    except Exception:
                        out.append({"name": f.name, "size": 0})
                elif f.is_dir():
                    out.append({"name": f.name+"/", "size": 0, "dir": True})
            return {"ok": True, "packs": out}
        except Exception as e:
            return {"ok": False, "error": str(e), "packs":[]}
    def install_shader_pack(self, zip_url, pack_name):
        try:
            d=MINECRAFT_DIR / "shaderpacks"
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(pack_name).name)[:80] or "pack.zip"
            if not safe.lower().endswith(".zip"): safe += ".zip"
            dest=d / safe
            try:
                dest.resolve().relative_to(d.resolve())
            except ValueError:
                return {"ok": False, "error": "Недопустимое имя"}
            try:
                dest.unlink(missing_ok=True)
                dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
            except Exception:
                pass
            download_file(zip_url, dest, progress_cb=None, retries=2)
            if not dest.exists() or dest.stat().st_size == 0 or not zipfile.is_zipfile(dest):
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                return {"ok": False, "error": "Скачанный файл не похож на шейдер (битый zip)"}
            return {"ok": True, "name": safe}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    def remove_shader_pack(self, pack_name):
        try:
            d=MINECRAFT_DIR / "shaderpacks"
            safe = Path(pack_name.rstrip("/")).name
            p=d / safe
            try:
                p.resolve().relative_to(d.resolve())
            except ValueError:
                return {"ok": False, "error": "Недопустимое имя"}
            if p.exists():
                if p.is_dir():
                    import shutil as _sh
                    _sh.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                return {"ok": True}
            return {"ok": False, "error": "Не найден"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_ui_settings_extended(self):
        try:
            return load_ui_settings()
        except Exception:
            return dict(DEFAULT_UI_SETTINGS)


def _setup_tray(window):
    if not HAS_TRAY:
        return None
    try:
        settings = load_ui_settings()
        if not settings.get("trayMinimize"):
            return None
        global Image
        if Image is None:
            from PIL import Image as _Image
            Image = _Image
        icon_path = Path(__file__).parent / "icon.ico"
        if icon_path.exists():
            try:
                image = Image.open(str(icon_path))
            except Exception:
                image = Image.new("RGB", (64,64), color=(124,58,237))
        else:
            image = Image.new("RGB", (64,64), color=(124,58,237))
        def _show(icon, item):
            try:
                window.show()
                window.restore()
            except Exception:
                pass
        def _quit(icon, item):
            try:
                icon.stop()
            except Exception:
                pass
            # сохраняем статистику/настройки естественным выходом:
            # window.destroy() возвращает webview.start(), процесс завершается сам.
            # os._exit() тут убран — он убивал процесс без flush и терял сессию/лог.
            try:
                window.destroy()
            except Exception:
                pass
        menu = pystray.Menu(
            pystray.MenuItem("Показать", _show, default=True),
            pystray.MenuItem("Выход", _quit),
        )
        tray = pystray.Icon(DISPLAY_NAME, image, DISPLAY_NAME, menu)
        # перехватываем закрытие — сворачиваем в трей, если включено
        def _on_closing():
            try:
                if load_ui_settings().get("trayMinimize"):
                    window.hide()
                    send_notification(DISPLAY_NAME, "Лаунчер свёрнут в трей")
                    return True  # отменяем закрытие
            except Exception:
                pass
            return False
        try:
            window.events.closing += _on_closing
        except Exception:
            pass
        import threading as _th
        _th.Thread(target=tray.run, daemon=True).start()
        return tray
    except Exception:
        return None

def main():
    api = Api()
    # первый синк статистики на сайт — сразу при старте, в фоне
    threading.Thread(target=_sync_quietly, daemon=True).start()
    # 36 запуск свернутым — уменьшаем окно и сразу минимизируем после старта
    # (один load_ui_settings вместо трёх — и без TOCTOU между чтениями)
    try:
        _ui = load_ui_settings()
    except Exception:
        _ui = dict(DEFAULT_UI_SETTINGS)
    start_min = bool(_ui.get("startMinimized"))
    _compact = bool(_ui.get("compactMode"))
    wsize = (560, 420) if _compact else WINDOW_SIZE
    window = webview.create_window(
        DISPLAY_NAME,
        url=resource_path("web/index.html"),
        js_api=api,
        width=wsize[0],
        height=wsize[1],
        resizable=True,
        min_size=(500, 380) if _compact else WINDOW_MIN_SIZE,
        background_color="#0d0a16",
        text_select=False,
    )
    api._window = window
    _setup_tray(window)
    if start_min:
        try:
            # hide сразу — иногда нужно подождать инициализации
            def _do_min():
                time.sleep(0.8)
                try:
                    window.minimize()
                except Exception:
                    try: window.hide()
                    except Exception: pass
            threading.Thread(target=_do_min, daemon=True).start()
        except Exception:
            pass
    try:
        webview.start(gui=FORCE_GUI, debug=DEBUG)
    except Exception as e:
        # Fallback: если Qt не завёлся (нет qtpy/PySide6) — пробуем EdgeChromium/MSHTML
        print(f"[webview] {FORCE_GUI} failed: {e}, fallback to edgechromium")
        try:
            webview.start(gui="edgechromium", debug=DEBUG)
        except Exception:
            webview.start(debug=DEBUG)


if __name__ == "__main__":
    main()