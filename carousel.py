#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Карусель обоев — прозрачный оверлей с превью и лентой миниатюр.

  Win+Alt+S   открыть/закрыть панель (меняется: --hotkey win+alt+k)
  Win+Alt+Q   выйти из приложения (или: python carousel.py --stop)

В панели:
  <- / -> / колесо   листать
  1..7               прыгнуть к миниатюре
  Enter / клик       применить
  Esc                закрыть

Источники: библиотека Lively Wallpaper (%LOCALAPPDATA%) и папка с картинками
(%USERPROFILE%\\Pictures\\Wallpapers). Пути переопределяются переменными
LIVELY_EXE, LIVELY_LIB, PHOTO_DIR.

Служебные команды: --help, --paths, --snapshot FILE, --hotkey COMBO,
--show-hotkey, --install-autostart, --uninstall-autostart, --stop.
"""

import ctypes
import ctypes.wintypes as wt
import glob
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# ---------- пути (переносимо: ничего не привязано к имени пользователя) ----------

HERE = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def _data_dir():
    """Скрипт — файлы рядом с собой; .exe — %LOCALAPPDATA%\\WallpaperCarousel.

    Если писать туда нельзя (чужая учётка, защищённая папка, политика), отступаем
    к папке рядом с .exe, затем к %TEMP%: приложение должно работать, а не падать
    на первом же файле.
    """
    if not getattr(sys, "frozen", False):
        return HERE
    base = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "WallpaperCarousel")
    for cand in (base,
                 os.path.join(HERE, "data"),
                 os.path.join(os.environ.get("TEMP", HERE), "WallpaperCarousel")):
        if _writable(cand):
            return cand
    return HERE


DATA_DIR = _data_dir()
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PID_FILE = os.path.join(DATA_DIR, "carousel.pid")
HOTKEY_FILE = os.path.join(DATA_DIR, "hotkey.json")
LOG_FILE = os.path.join(DATA_DIR, "carousel.log")
FONT_DIR = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"


def log(msg):
    """У pythonw нет консоли — пишем в carousel.log рядом со скриптом."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _registry_lively():
    try:
        import winreg
    except Exception:
        return ""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for path in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                root = winreg.OpenKey(hive, path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                try:
                    key = winreg.OpenKey(root, sub)
                    name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                except OSError:
                    continue
                if "lively" not in name.lower():
                    continue
                try:
                    loc = str(winreg.QueryValueEx(key, "InstallLocation")[0])
                except OSError:
                    continue
                for rel in ("Lively.exe", os.path.join("Lively Wallpaper", "Lively.exe")):
                    cand = os.path.join(loc, rel)
                    if os.path.isfile(cand):
                        return cand
    return ""


def detect_lively_exe():
    env = os.environ.get("LIVELY_EXE")
    if env:
        return env
    cands = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            cands.append(os.path.join(base, "Lively Wallpaper", "Lively.exe"))
            cands.append(os.path.join(base, "Programs", "Lively Wallpaper", "Lively.exe"))
    for cand in cands:
        if os.path.isfile(cand):
            return cand
    found = _registry_lively()
    return found or (cands[0] if cands else "Lively.exe")


def detect_lively_lib():
    return os.environ.get("LIVELY_LIB") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Lively Wallpaper", "Library", "wallpapers")


def detect_photo_dir():
    return os.environ.get("PHOTO_DIR") or os.path.join(
        os.path.expanduser("~"), "Pictures", "Wallpapers")


LIVELY_EXE = detect_lively_exe()
LIVELY_LIB = detect_lively_lib()
PHOTO_DIR = detect_photo_dir()

# ---------- вид (значения для экрана 2560x1440; дальше масштабируются) ----------

CARD_TOP = (23, 23, 29)
CARD_BOT = (16, 16, 20)
FG = (245, 244, 242)
MUTED = (156, 156, 170)
FAINT = (110, 110, 124)
BASE_W, BASE_H = 2560, 1440

STRIP_SPAN = 7

HOTKEY_ID = 0xB00B
MOD_ALT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0008, 0x4000
VK_SPACE, VK_Q = 0x20, 0x51
WM_HOTKEY = 0x0312

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
KEYS = {0x25: "left", 0x27: "right", 0x0D: "apply", 0x1B: "close"}
for _i in range(1, STRIP_SPAN + 1):
    KEYS[0x31 + _i] = "pick%d" % _i

MODS = {"win": MOD_WIN, "alt": MOD_ALT, "ctrl": 0x0002, "shift": 0x0004}
VK_NAMES = {"space": VK_SPACE, "enter": 0x0D, "tab": 0x09,
            "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28}
DEFAULT_HOTKEY = {"mods": ["win", "alt"], "key": "S"}

# ---------- хоткей ----------


def hotkey_spec():
    """Что в hotkey.json; при любой проблеме — комбинация по умолчанию."""
    try:
        with open(HOTKEY_FILE, encoding="utf-8-sig") as f:
            spec = json.load(f)
        mods = [str(m).lower() for m in spec.get("mods", [])]
        key = str(spec.get("key", "S"))
        if not mods or any(m not in MODS for m in mods):
            return list(DEFAULT_HOTKEY["mods"]), DEFAULT_HOTKEY["key"]
        return mods, key
    except Exception:
        return list(DEFAULT_HOTKEY["mods"]), DEFAULT_HOTKEY["key"]


def hotkey_mods():
    mods, _ = hotkey_spec()
    m = 0
    for name in mods:
        m |= MODS.get(str(name).lower(), 0)
    return m


def vk_for(key):
    k = str(key).lower()
    if k in VK_NAMES:
        return VK_NAMES[k]
    if len(k) == 1:
        return ord(k.upper())
    if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 12:
        return 0x70 + int(k[1:]) - 1
    return VK_SPACE


def hotkey_vk():
    _, key = hotkey_spec()
    return vk_for(key)


def hotkey_text(spec=None):
    if spec:
        if isinstance(spec, dict):
            mods = [str(m).lower() for m in spec.get("mods", [])]
            key = str(spec.get("key", "S"))
        else:
            mods, key = spec
    else:
        mods, key = hotkey_spec()
    labels = {"win": "Win", "alt": "Alt", "ctrl": "Ctrl", "shift": "Shift"}
    key_label = {"space": "Space", "enter": "Enter", "tab": "Tab",
                 "left": "Left", "right": "Right", "up": "Up", "down": "Down"}
    low = str(key).lower()
    shown = key_label.get(low) or (key.upper() if len(key) == 1 else key.upper())
    return "+".join([labels.get(m, m.title()) for m in mods] + [shown])


def parse_hotkey(text):
    """'win+alt+k' -> ({"mods": [...], "key": "K"}, None) либо (None, текст ошибки)."""
    parts = [p.strip() for p in str(text or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None, "пустая комбинация"
    key = parts[-1]
    mods = [m.lower() for m in parts[:-1]]
    bad = [m for m in mods if m not in MODS]
    if bad:
        return None, "неизвестный модификатор: %s (можно win, alt, ctrl, shift)" % ", ".join(bad)
    if not mods:
        return None, "нужен хотя бы один модификатор: win, alt, ctrl или shift"
    low = key.lower()
    if low in VK_NAMES:
        key = low.title()
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        key = key.upper()
    elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 12:
        key = low.upper()
    else:
        return None, "клавиша должна быть буквой, цифрой, F1-F12 или одной из: %s" % ", ".join(VK_NAMES)
    return {"mods": mods, "key": key}, None


def save_hotkey(spec):
    with open(HOTKEY_FILE, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)


def same_hotkey(spec, mods, key):
    try:
        cur_mods, cur_key = mods, key
        return ([str(m).lower() for m in spec["mods"]] == [str(m).lower() for m in cur_mods]
                and str(spec["key"]).lower() == str(cur_key).lower())
    except Exception:
        return False


def hotkey_free(mods, key):
    """Свободна ли комбинация: пробуем зарегистрировать и сразу отпускаем."""
    u = ctypes.windll.user32
    m = 0
    for name in mods:
        m |= MODS.get(name, 0)
    tmp = 0x7211
    ok = u.RegisterHotKey(None, tmp, m | MOD_NOREPEAT, vk_for(key))
    if ok:
        u.UnregisterHotKey(None, tmp)
    return bool(ok)

# ---------- утилиты ----------

def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def screen_size():
    """Физический размер экрана без tkinter (нужен и для --snapshot)."""
    dpi_aware()
    u = ctypes.windll.user32
    try:
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return BASE_W, BASE_H

_FONTS = {}

def font(name, size):
    size = max(8, int(round(size)))
    key = (name, size)
    if key not in _FONTS:
        try:
            _FONTS[key] = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
        except Exception:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]

_MASKS = {}

def rmask(w, h, r):
    """Сглаженная маска скругления: рисуем в 3x и уменьшаем."""
    w, h, r = max(1, int(w)), max(1, int(h)), max(0, int(r))
    key = (w, h, r)
    if key in _MASKS:
        return _MASKS[key]
    ss = 3 if min(w, h) <= 900 else 2
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1],
                                        radius=r * ss, fill=255)
    m = m.resize((w, h), Image.LANCZOS)
    _MASKS[key] = m
    return m

def rounded(img, w, h, r):
    if img.size != (int(w), int(h)):
        img = img.resize((int(w), int(h)), Image.LANCZOS)
    out = img.convert("RGBA")
    out.putalpha(rmask(w, h, r))
    return out

def cover(src, w, h):
    w, h = max(1, int(w)), max(1, int(h))
    k = max(w / src.width, h / src.height)
    im = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))

def framed(src, w, h, radius, crop_ok=True):
    """Кадр превью.

    crop_ok=True  — можно кропать под нужный формат (миниатюры в ленте).
    crop_ok=False — для большого превью держим исходный формата, если он не 16:9.
    Мелкий исходник не растягиваем: блюр-подложка + аккуратно вписанный кадр.
    """
    w, h = max(1, int(w)), max(1, int(h))
    ar_src, ar_t = src.width / src.height, w / h
    cover_scale = max(w / src.width, h / src.height)
    if cover_scale <= 1.25 and (crop_ok or abs(ar_src - ar_t) < 0.05):
        return rounded(cover(src, w, h), w, h, radius)

    blurred = cover(src, w, h).filter(ImageFilter.GaussianBlur(max(6, w // 60)))
    out = ImageEnhance.Brightness(blurred).enhance(0.42).convert("RGBA")
    k = min(w * 0.94 / src.width, h * 0.94 / src.height, 2.0)
    iw, ih = max(1, int(src.width * k)), max(1, int(src.height * k))
    inner = rounded(src, iw, ih, max(4, radius * 0.5))
    ox, oy = (w - iw) // 2, (h - ih) // 2
    out.alpha_composite(inner, (ox, oy))
    out.putalpha(rmask(w, h, radius))
    return out

_GRADS = {}

def vgrad(w, h, top, bot, cache=True):
    key = ("grad", int(w), int(h), top, bot)
    if cache and key in _GRADS:
        return _GRADS[key]
    col = Image.new("RGB", (1, int(h)))
    px = col.load()
    for y in range(int(h)):
        k = y / max(1, int(h) - 1)
        px[0, y] = tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3))
    img = col.resize((int(w), int(h)))
    if cache:
        _GRADS[key] = img
    return img

_SCRIMS = {}

def scrim(w, h, start=0.36, max_a=242):
    key = (int(w), int(h), start, max_a)
    if key in _SCRIMS:
        return _SCRIMS[key]
    h = int(h)
    col = Image.new("L", (1, h), 0)
    px = col.load()
    y0 = int(h * start)
    for y in range(h):
        if y >= y0:
            px[0, y] = int(max_a * ((y - y0) / max(1, h - y0 - 1)) ** 1.45)
    mask = col.resize((int(w), h))
    _SCRIMS[key] = mask
    return mask

def tracked_width(d, text, f, tr=0.0):
    return sum(d.textlength(c, font=f) for c in text) + tr * max(0, len(text) - 1)

def tracked(d, xy, text, f, fill, tr=0.0):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + tr
    return x

def shadowed(d, xy, text, f, fill):
    """Текст поверх картинки: тень на 1 px, чтобы читался на любом узоре."""
    d.text((xy[0] + 1, xy[1] + 1), text, font=f, fill=(0, 0, 0, 110))
    d.text(xy, text, font=f, fill=fill)

def shadowed_tracked(d, xy, text, f, fill, tr=0.0):
    tracked(d, (xy[0] + 1, xy[1] + 1), text, f, (0, 0, 0, 110), tr)
    return tracked(d, xy, text, f, fill, tr)

def centered_text(d, box, text, f, fill):
    """Текст ровно по центру прямоугольника (и по горизонтали, и по вертикали)."""
    x0, y0, x1, y1 = box
    d.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=f, fill=fill, anchor="mm")

def centered_tracked(d, box, text, f, fill, tr=0.0):
    x0, y0, x1, y1 = box
    w = tracked_width(d, text, f, tr)
    x = (x0 + x1) / 2 - w / 2
    cy = (y0 + y1) / 2
    for ch in text:
        d.text((x, cy), ch, font=f, fill=fill, anchor="lm")
        x += d.textlength(ch, font=f) + tr

def _hls(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, l, 0.0
    delta = mx - mn
    s = delta / (2 - mx - mn) if l > 0.5 else delta / (mx + mn)
    if mx == r:
        h = ((g - b) / delta) % 6
    elif mx == g:
        h = (b - r) / delta + 2
    else:
        h = (r - g) / delta + 4
    return h / 6.0, l, s

def _from_hls(h, l, s):
    if s == 0:
        v = int(l * 255)
        return v, v, v
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def ch(t):
        t %= 1.0
        if t < 1 / 6:
            v = p + (q - p) * 6 * t
        elif t < 0.5:
            v = q
        elif t < 2 / 3:
            v = p + (q - p) * (2 / 3 - t) * 6
        else:
            v = p
        return v

    return ch(h + 1 / 3), ch(h), ch(h - 1 / 3)

def accent_of(img):
    """Доминирующий насыщенный цвет превью → цвет акцента интерфейса."""
    small = img.convert("RGB").resize((32, 18))
    px = small.load()
    best = None
    for y in range(small.height):
        for x in range(small.width):
            h, l, s = _hls(*px[x, y])
            if s < 0.20 or not (0.12 < l < 0.92):
                continue
            score = s * (1 - abs(l - 0.55))
            if best is None or score > best[0]:
                best = (score, h, s)
    if best is None:
        return (140, 132, 255)
    r, g, b = _from_hls(best[1], 0.66, max(0.55, best[2]))
    return (int(r * 255), int(g * 255), int(b * 255))

def mix(c1, c2, k):
    return tuple(int(c1[i] * (1 - k) + c2[i] * k) for i in range(3))

def with_alpha(c, a):
    return (c[0], c[1], c[2], a)

def lively_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Lively.exe"],
                         capture_output=True, text=True, errors="ignore").stdout
    return "Lively.exe" in out

def pid_alive(pid):
    """os.kill(pid, 0) на Windows не проверяет процесс, а убивает его."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)

# ---------- источники ----------

def ensure_photo_dir():
    """Без Lively единственный источник — папка с картинками: создаём, чтобы было куда класть."""
    if os.path.isdir(PHOTO_DIR):
        return True
    try:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        log("создана папка для картинок: %s" % PHOTO_DIR)
        return True
    except Exception as e:
        log("не смог создать %s: %s" % (PHOTO_DIR, e))
        return False


def scan_wallpapers():
    items = []
    for folder in sorted(glob.glob(os.path.join(LIVELY_LIB, "*"))):
        info_path = os.path.join(folder, "LivelyInfo.json")
        if not os.path.isfile(info_path):
            continue
        try:
            with open(info_path, encoding="utf-8-sig") as f:
                info = json.load(f)
        except Exception:
            continue
        title = (info.get("Title") or os.path.basename(folder)).strip()
        thumb = None
        t = info.get("Thumbnail") or ""
        if t:
            cand = t if os.path.isabs(t) else os.path.join(folder, t)
            if os.path.isfile(cand):
                thumb = cand
        if thumb is None:
            for name in ("thumbnail.png", "thumbnail.jpg", "preview.png"):
                cand = os.path.join(folder, name)
                if os.path.isfile(cand):
                    thumb = cand
                    break
        items.append({"title": title, "sub": (info.get("Author") or "Lively").strip()[:38],
                      "kind": "lively", "path": folder, "thumb": thumb})

    if os.path.isdir(PHOTO_DIR):
        for f in sorted(glob.glob(os.path.join(PHOTO_DIR, "*"))):
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp")):
                items.append({"title": os.path.splitext(os.path.basename(f))[0],
                              "sub": "картинка", "kind": "photo", "path": f, "thumb": f})
    return items

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(d):
    st = load_state()
    st.update(d)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def current_path():
    """Что реально стоит сейчас: Lively пишет это в WallpaperLayout.json."""
    layout = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          r"Lively Wallpaper\WallpaperLayout.json")
    try:
        with open(layout, encoding="utf-8-sig") as f:
            data = json.load(f)
        if data:
            return os.path.normcase(data[0].get("LivelyInfoPath", ""))
    except Exception:
        pass
    return os.path.normcase(load_state().get("path", ""))

def apply_wallpaper(item):
    if item["kind"] == "lively":
        if not LIVELY_EXE or not os.path.isfile(LIVELY_EXE):
            log("Lively.exe не найден (%s) — задай переменную LIVELY_EXE" % LIVELY_EXE)
            return
        if not lively_running():
            subprocess.Popen([LIVELY_EXE], close_fds=True)
            for _ in range(40):
                time.sleep(0.25)
                if lively_running():
                    break
            time.sleep(2)
        subprocess.run([LIVELY_EXE, "setwp", "--file", item["path"]],
                       close_fds=True, timeout=40)
    else:
        if lively_running():  # Lively рисуется поверх стола, поэтому гасим его
            subprocess.run(["taskkill", "/IM", "Lively.exe", "/F"],
                           capture_output=True, close_fds=True)
            time.sleep(1)
        ctypes.windll.user32.SystemParametersInfoW(0x0014, 0, item["path"], 0x01 | 0x02)
    save_state({"current": item["title"], "path": item["path"]})

# ---------- панель ----------

class Panel:
    """Витрина обоев: только изображения. Фон окна прозрачный, без анимаций."""

    KEY_HEX = "#ff00fe"          # цвет-ключ прозрачности окна
    KEY_RGB = (255, 0, 254)

    def __init__(self, root, sw=None, sh=None):
        self.root = root
        self.sw, self.sh = (sw, sh) if sw else screen_size()
        self.s = min(self.sw / BASE_W, self.sh / BASE_H)
        self.items = scan_wallpapers()
        self.idx = 0
        self.current = None
        self.win = None
        self.preview_label = None
        self.strip_label = None
        self.events = queue.Queue()
        self.state = {"open": False}

        self._photo = None
        self._strip_photo = None
        self._hits = []
        self._hits_strip = []
        self._hk_win = None
        self._hotkey_warned = False

        self.sources = OrderedDict()   # LRU: полный кадр нужен только на время сборки
        self.cells = OrderedDict()     # LRU: миниатюры
        self.previews = OrderedDict()  # LRU: большие превью

        self._layout()

    # ---------- геометрия ----------

    def _layout(self):
        s = self.s
        self.pw = min(int(round(1292 * s)), self.sw - int(round(80 * s)))
        self.ph = int(round(self.pw * 9 / 16))
        self.cell_w = int(round(174 * s))
        self.cell_h = int(round(96 * s))
        self.cell_gap = int(round(12 * s))
        self.lift = int(round(6 * s))
        self.strip_w = STRIP_SPAN * self.cell_w + (STRIP_SPAN - 1) * self.cell_gap
        self.strip_h = self.cell_h + self.lift
        self.gap = int(round(24 * s))

        self.x = (self.sw - self.pw) // 2
        self.y = (self.sh - (self.ph + self.gap + self.strip_h)) // 2
        self.sx = (self.sw - self.strip_w) // 2
        self.sy = self.y + self.ph + self.gap

    # ---------- источники и картинки ----------

    def _placeholder(self, item, w, h):
        seed = sum(ord(c) for c in item["title"]) or 7
        hue = (seed % 360) / 360.0
        c1 = tuple(int(v * 255) for v in _from_hls(hue, 0.20, 0.45))
        c2 = tuple(int(v * 255) for v in _from_hls((hue + 0.08) % 1.0, 0.09, 0.55))
        img = vgrad(w, h, c1, c2, cache=False)
        d = ImageDraw.Draw(img)
        title = item["title"][:28]
        f = font("seguisb.ttf", max(12, h * 0.13))
        tw = d.textlength(title, font=f)
        d.text(((w - tw) / 2, h / 2 - h * 0.09), title, font=f, fill=(230, 230, 230))
        return img

    def source(self, item):
        key = item["path"]
        if key in self.sources:
            self.sources.move_to_end(key)
            return self.sources[key]
        img = None
        if item.get("thumb"):
            try:
                img = Image.open(item["thumb"]).convert("RGB")
            except Exception:
                img = None
        if img is None:
            img = self._placeholder(item, 1280, 720)
        self.sources[key] = img
        while len(self.sources) > 2:
            self.sources.popitem(last=False)
        return img

    def preview_img(self, item):
        key = (item["path"], self.pw, self.ph)
        if key in self.previews:
            self.previews.move_to_end(key)
            return self.previews[key]
        img = framed(self.source(item), self.pw, self.ph, 0, crop_ok=False)
        self.previews[key] = img
        while len(self.previews) > 4:
            self.previews.popitem(last=False)
        return img

    def cell_img(self, item, active):
        key = (item["path"], bool(active))
        if key in self.cells:
            self.cells.move_to_end(key)
            return self.cells[key]
        img = framed(self.source(item), self.cell_w, self.cell_h, 0)
        if not active:      # неактивные чуть приглушены — вместо рамки
            img = ImageEnhance.Brightness(img.convert("RGB")).enhance(0.72).convert("RGBA")
            img.putalpha(rmask(self.cell_w, self.cell_h, 0))
        self.cells[key] = img
        while len(self.cells) > 120:
            self.cells.popitem(last=False)
        return img

    # ---------- кадры ----------

    def _strip_frame(self, transparent=False):
        w, h = self.strip_w, self.strip_h
        bg = (0, 0, 0, 0) if transparent else self.KEY_RGB + (255,)
        canvas = Image.new("RGBA", (w, h), bg)
        n = len(self.items)
        step = self.cell_w + self.cell_gap
        hits = []
        if not n:
            self._hits_strip = hits
            return canvas

        for k in range(STRIP_SPAN):
            i = (self.idx - STRIP_SPAN // 2 + k) % n
            active = i == self.idx
            x = k * step
            y = 0 if active else self.lift
            canvas.alpha_composite(self.cell_img(self.items[i], active), (x, y))
            hits.append(("cell", (self.sx + x, self.sy + y, self.cell_w, self.cell_h), i))
        self._hits_strip = hits
        return canvas

    def paint(self):
        if self.win is None or not self.items:
            return
        item = self.items[self.idx]

        self._photo = tk_image(self.preview_img(item))
        if self.preview_label is not None:
            self.preview_label.configure(image=self._photo)

        self._strip_photo = tk_image(self._strip_frame())
        if self.strip_label is not None:
            self.strip_label.configure(image=self._strip_photo)

        self._hits = [("apply", (self.x, self.y, self.pw, self.ph))]

    # ---------- ввод ----------

    def move(self, d):
        if len(self.items) < 2:
            return
        self.idx = (self.idx + d) % len(self.items)
        self.paint()

    def pick(self, n):
        """1..7 — слот в ленте, а лента центрирована на текущих обоях."""
        if len(self.items) < 2:
            return
        self.jump((self.idx - STRIP_SPAN // 2 + (n - 1)) % len(self.items))

    def jump(self, target):
        if len(self.items) < 2:
            return
        target %= len(self.items)
        if target == self.idx:
            return
        self.idx = target
        self.paint()

    def apply(self):
        if not self.items:
            return
        item = self.items[self.idx]
        self.close()
        threading.Thread(target=apply_wallpaper, args=(item,), daemon=True).start()

    def _hit_test(self, x, y):
        for hits in (self._hits_strip, self._hits):
            for hit in reversed(hits):
                if hit[0] == "cell":
                    _, (rx, ry, rw, rh), idx = hit
                    if rx <= x < rx + rw and ry <= y < ry + rh:
                        return ("cell", idx)
                else:
                    _, (rx, ry, rw, rh) = hit
                    if rx <= x < rx + rw and ry <= y < ry + rh:
                        return (hit[0], None)
        return (None, None)

    def _on_click(self, x, y):
        kind, idx = self._hit_test(x, y)
        if kind == "apply":
            self.apply()
        elif kind == "cell":
            if idx == self.idx:
                self.apply()
            else:
                self.jump(idx)

    # ---------- окно ----------

    def sync_current(self):
        cur = current_path()
        self.current = None
        for i, it in enumerate(self.items):
            if os.path.normcase(it["path"]) == cur:
                self.idx = i
                self.current = it["title"]
                break

    def _context_menu(self, x, y):
        """Правый клик по панели: хоткей, автозапуск, закрыть."""
        import tkinter as tk

        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="Сменить хоткей…", command=self.hotkey_dialog)
        auto = os.path.isfile(autostart_link())
        menu.add_command(
            label=("Автозапуск: выключить" if auto else "Автозапуск: включить"),
            command=(lambda: self._toggle_autostart(auto)))
        menu.add_separator()
        menu.add_command(label="Закрыть", command=self.close)
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _toggle_autostart(self, enabled):
        if enabled:
            uninstall_autostart()
            log("автозапуск выключен")
        else:
            ok, lnk = install_autostart()
            log("автозапуск %s: %s" % ("включён" if ok else "не удался", lnk))

    def _hk_close(self):
        if getattr(self, "_hk_win", None) is not None:
            try:
                self._hk_win.destroy()
            except Exception:
                pass
        self._hk_win = None

    def hotkey_dialog(self):
        """Смена комбинации мышью: чекбоксы модификаторов + выпадающий список клавиш."""
        import tkinter as tk
        from tkinter import ttk

        if getattr(self, "_hk_win", None) is not None:
            try:
                self._hk_win.lift()
                self._hk_win.focus_force()
                return
            except Exception:
                self._hk_win = None

        mods, key = hotkey_spec()
        top = tk.Toplevel(self.root)
        self._hk_win = top
        top.title("Хоткей панели")
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.configure(padx=16, pady=14)
        top.protocol("WM_DELETE_WINDOW", self._hk_close)

        tk.Label(top, text="Модификаторы").grid(row=0, column=0, sticky="w")
        box = tk.Frame(top)
        box.grid(row=0, column=1, sticky="w", pady=(0, 8))
        flags = {}
        for i, name in enumerate(("win", "alt", "ctrl", "shift")):
            var = tk.BooleanVar(value=name in [m.lower() for m in mods])
            flags[name] = var
            tk.Checkbutton(box, text=name.capitalize(), variable=var).grid(
                row=0, column=i, padx=(0, 6))

        tk.Label(top, text="Клавиша").grid(row=1, column=0, sticky="w")
        keys = ([chr(c) for c in range(ord("A"), ord("Z") + 1)]
                + [str(d) for d in range(10)]
                + ["F%d" % i for i in range(1, 13)]
                + ["Space"])
        key_var = tk.StringVar(value=key.upper() if len(key) == 1 else key.title())
        combo = ttk.Combobox(top, textvariable=key_var, values=keys, width=8,
                             state="readonly")
        combo.grid(row=1, column=1, sticky="w", pady=(0, 6))

        hint = tk.Label(top, text="", fg="#b04040", wraplength=280, justify="left")
        hint.grid(row=2, column=0, columnspan=2, sticky="w")
        current = tk.Label(top, text="Сейчас: %s" % hotkey_text(), fg="#666")
        current.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def save():
            chosen = [name for name in ("win", "alt", "ctrl", "shift") if flags[name].get()]
            parsed, err = parse_hotkey("+".join(chosen + [key_var.get()]))
            if err:
                hint.configure(text=err)
                return
            mods_now, key_now = hotkey_spec()
            unchanged = same_hotkey(parsed, mods_now, key_now)
            # текущую комбинацию держит наш же процесс — «занято» для неё не проверяем
            if not unchanged and not hotkey_free(parsed["mods"], parsed["key"]):
                hint.configure(text="Комбинация %s занята другой программой"
                                    % hotkey_text(parsed))
                return
            if unchanged:
                self._hk_close()
                return
            save_hotkey(parsed)
            log("хоткей изменён: %s" % hotkey_text(parsed))
            self._hk_close()
            self.restart_self()

        new_row = tk.Frame(top)
        new_row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        save_btn = ttk.Button(new_row, text="Сохранить", command=save)
        save_btn.pack(side="left", padx=(0, 8))
        cancel_btn = ttk.Button(new_row, text="Отмена", command=self._hk_close)
        cancel_btn.pack(side="left")
        for w in (top, combo, save_btn):
            w.bind("<Return>", lambda e: save())
            w.bind("<KP_Enter>", lambda e: save())
        for w in (top, combo, cancel_btn):
            w.bind("<Escape>", lambda e: self._hk_close())

        top.update_idletasks()
        x = (top.winfo_screenwidth() - top.winfo_width()) // 2
        y = (top.winfo_screenheight() - top.winfo_height()) // 2
        top.geometry("+%d+%d" % (x, y))
        top.lift()
        top.focus_force()
        combo.focus_set()

    def restart_self(self):
        """Перезапуск на месте: новый процесс стартует, старый выходит."""
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        start_detached()
        self.close()
        try:
            self.root.quit()
        except Exception:
            pass

    def show(self):
        if self.win is not None:
            self.close()
            return
        if not self.items:
            # молча ничего не показывать — худший вариант: объясняем, что искать
            log("нет обоев: lib=%s (есть: %s), photos=%s (есть: %s)"
                % (LIVELY_LIB, os.path.isdir(LIVELY_LIB),
                   PHOTO_DIR, os.path.isdir(PHOTO_DIR)))
            lively_ok = os.path.isfile(LIVELY_EXE)
            try:
                from tkinter import messagebox
                lines = ["Не найдено ни одной картинки.", ""]
                if lively_ok:
                    lines.append("Библиотека Lively пуста: %s" % LIVELY_LIB)
                else:
                    lines.append("Lively Wallpaper не установлен.")
                lines += ["Папка с картинками: %s" % PHOTO_DIR, "",
                          "Положи туда .jpg / .png / .bmp / .webp — они появятся в панели.",
                          "Или установи Lively Wallpaper: https://www.rocksdanister.com/lively/"]
                messagebox.showinfo("Карусель обоев", "\n".join(lines))
            except Exception:
                pass
            return

        import tkinter as tk

        self.sync_current()

        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-transparentcolor", self.KEY_HEX)
        except Exception:
            pass
        w.geometry("%dx%d+0+0" % (self.sw, self.sh))
        w.configure(bg=self.KEY_HEX)
        self.win = w
        self.state["open"] = True

        self.preview_label = tk.Label(w, bd=0, bg=self.KEY_HEX, cursor="hand2",
                                      highlightthickness=0)
        self.preview_label.place(x=self.x, y=self.y, width=self.pw, height=self.ph)
        self.strip_label = tk.Label(w, bd=0, bg=self.KEY_HEX, cursor="hand2",
                                    highlightthickness=0)
        self.strip_label.place(x=self.sx, y=self.sy,
                               width=self.strip_w, height=self.strip_h)

        def on_click(e):
            self._on_click(e.x_root, e.y_root)
            return "break"

        for label in (self.preview_label, self.strip_label):
            label.bind("<Button-1>", on_click)
            label.bind("<Button-3>", lambda e: self._context_menu(e.x_root, e.y_root))
        w.bind("<Button-1>", lambda e: self.close())
        w.bind("<Button-3>", lambda e: self._context_menu(e.x_root, e.y_root))

        w.bind("<Left>", lambda e: self.move(-1))
        w.bind("<Right>", lambda e: self.move(1))
        w.bind("<Return>", lambda e: self.apply())
        w.bind("<KP_Enter>", lambda e: self.apply())
        w.bind("<Escape>", lambda e: self.close())
        w.bind("<MouseWheel>", lambda e: self.move(-1 if e.delta > 0 else 1))
        w.bind("<Button-4>", lambda e: self.move(-1))
        w.bind("<Button-5>", lambda e: self.move(1))
        for i in range(1, STRIP_SPAN + 1):
            w.bind(str(i), lambda e, n=i: self.pick(n - 1))

        w.deiconify()
        w.lift()
        w.focus_force()
        self.paint()          # окно прозрачное: картинки просто появляются

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self.preview_label = None
            self.strip_label = None
        self.state["open"] = False
        self._hits = []
        self._hits_strip = []

    # ---------- цикл событий ----------

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "toggle":
                    self.show()
                elif ev == "quit":
                    self.root.quit()
                elif self.win is None:
                    continue
                elif ev == "left":
                    self.move(-1)
                elif ev == "right":
                    self.move(1)
                elif ev == "apply":
                    self.apply()
                elif ev == "close":
                    self.close()
                elif ev == "no_hotkey":
                    if not self._hotkey_warned:
                        self._hotkey_warned = True
                        self.root.after(300, self.hotkey_dialog)
                elif ev.startswith("pick"):
                    self.pick(int(ev[4:]) - 1)
        except queue.Empty:
            pass
        self.root.after(16 if self.win is not None else 60, self.poll)


def tk_image(img):
    from PIL import ImageTk
    return ImageTk.PhotoImage(img)

# ---------- глобальный keyboard-хук ----------

class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

_hook_refs = []

def keyboard_hook_thread(ev_queue, state):
    user32 = ctypes.windll.user32
    CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                 ctypes.c_size_t, ctypes.c_ssize_t)
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_size_t, ctypes.c_ssize_t]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, CMPFUNC, ctypes.c_void_p,
                                         ctypes.c_uint32]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p

    def callback(code, wparam, lparam):
        if code == 0 and state.get("open") and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(ctypes.c_void_p(lparam),
                             ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            ev = KEYS.get(kb.vkCode)
            if ev:
                ev_queue.put(ev)
                return 1  # съедаем клавишу, пока панель открыта
        return user32.CallNextHookEx(None, code, wparam, lparam)

    cb = CMPFUNC(callback)
    _hook_refs.append(cb)
    if not user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, None, 0):
        print("keyboard hook не установился", file=sys.stderr)
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

# ---------- глобальный хоткей ----------

def hotkey_thread(ev_queue):
    user32 = ctypes.windll.user32
    ok = user32.RegisterHotKey(None, HOTKEY_ID, hotkey_mods() | MOD_NOREPEAT, hotkey_vk())
    if not ok:
        ev_queue.put("no_hotkey")
        log("хоткей %s занят другой программой — задай другой: правый клик по панели → «Сменить хоткей»"
            % hotkey_text())
    else:
        log("хоткей %s зарегистрирован" % hotkey_text())
    user32.RegisterHotKey(None, HOTKEY_ID + 1, MOD_WIN | MOD_ALT | MOD_NOREPEAT, VK_Q)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            ev_queue.put("toggle" if msg.wParam == HOTKEY_ID else "quit")

# ---------- автозапуск и перезапуск ----------

def startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def pythonw_path():
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            return cand
    return exe


def app_command():
    """Как запускать приложение: (exe, аргументы) — скрипт или собранный .exe."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return pythonw_path(), [os.path.abspath(__file__)]


def say(text):
    """Печать в консоль; в windowed-.exe консоли нет — показываем окно."""
    try:
        if sys.stdout is not None:
            print(text)
            return
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        r.attributes("-topmost", True)
        messagebox.showinfo("Карусель обоев", text)
        r.destroy()
    except Exception:
        pass


def autostart_link():
    return os.path.join(startup_dir(), "WallpaperCarousel.lnk")


def install_autostart():
    lnk = autostart_link()
    target, args = app_command()
    target = target.replace("'", "''")
    script = os.path.abspath(__file__).replace("'", "''")
    workdir = HERE.replace("'", "''")
    command = ('"%s"' % script) if args else ""
    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          "$s = $ws.CreateShortcut('%s'); "
          "$s.TargetPath = '%s'; $s.Arguments = '%s'; "
          "$s.WorkingDirectory = '%s'; $s.WindowStyle = 7; "
          "$s.Description = 'Wallpaper carousel'; $s.Save()"
          % (lnk, target, command, workdir))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True)
    return os.path.isfile(lnk), lnk


def uninstall_autostart():
    lnk = autostart_link()
    try:
        os.remove(lnk)
    except OSError:
        pass
    return not os.path.isfile(lnk), lnk


def running_pid():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        return pid if pid_alive(pid) else 0
    except Exception:
        return 0


def stop_running():
    pid = running_pid()
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(0.6)


def start_detached():
    exe, args = app_command()
    subprocess.Popen([exe] + args, close_fds=True,
                     creationflags=0x00000008)   # DETACHED_PROCESS


HELP = """Карусель обоев.

  python carousel.py                     запустить панель (в автозагрузке — pythonw)
  python carousel.py --show              запустить и сразу открыть панель
  python carousel.py --stop              остановить запущенный экземпляр
  python carousel.py --snapshot FILE     сохранить прозрачный кадр в PNG (--index N)
  python carousel.py --hotkey win+alt+k  задать хоткей и перезапустить приложение
  python carousel.py --show-hotkey       показать текущий хоткей
  python carousel.py --hotkey-dialog     открыть окно смены хоткея (то же, что правый клик)
  python carousel.py --install-autostart добавить в автозагрузку
  python carousel.py --uninstall-autostart убрать из автозагрузки
  python carousel.py --paths             показать найденные пути

Модификаторы: win, alt, ctrl, shift. Клавиша: буква, цифра, F1-F12, space.
Переопределить пути можно переменными LIVELY_EXE, LIVELY_LIB, PHOTO_DIR.
"""


def print_paths():
    lines = [
        "Lively.exe : %s %s" % (LIVELY_EXE, "(найден)" if os.path.isfile(LIVELY_EXE) else "(НЕ найден)"),
        "Библиотека : %s %s" % (LIVELY_LIB, "(есть)" if os.path.isdir(LIVELY_LIB) else "(НЕТ)"),
        "Картинки   : %s %s" % (PHOTO_DIR, "(есть)" if os.path.isdir(PHOTO_DIR) else "(НЕТ)"),
        "Хоткей     : %s" % hotkey_text(),
        "Автозапуск : %s %s" % (autostart_link(),
                                "(есть)" if os.path.isfile(autostart_link()) else "(нет)"),
        "Обоев найдено: %d" % len(scan_wallpapers()),
    ]
    text = "\n".join(lines)
    return text

# ---------- запуск ----------

def snapshot(path, index=0):
    sw, sh = screen_size()
    p = Panel(None, sw, sh)
    if not p.items:
        print("нет обоев")
        return
    p.sync_current()
    p.idx = max(0, min(int(index), len(p.items) - 1))
    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    canvas.alpha_composite(p.preview_img(p.items[p.idx]), (p.x, p.y))
    canvas.alpha_composite(p._strip_frame(transparent=True), (p.sx, p.sy))
    canvas.save(path)
    print("saved", path, canvas.size, "| idx", p.idx, "|", p.items[p.idx]["title"])


def main():
    import tkinter as tk

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    dpi_aware()   # строго до tk.Tk(): после создания окна Windows уже не даст
    args = sys.argv[1:]

    if "--help" in args or "-h" in args or "/?" in args:
        say(HELP)
        return

    if "--paths" in args:
        say(print_paths())
        return

    if "--show-hotkey" in args:
        say(hotkey_text())
        return

    flag = "--hotkey" if "--hotkey" in args else (
        "--set-hotkey" if "--set-hotkey" in args else None)
    if flag:
        i = args.index(flag)
        text = args[i + 1] if len(args) > i + 1 else ""
        spec, err = parse_hotkey(text)
        if err:
            say("Ошибка: " + err)
            say("Пример: python carousel.py --hotkey win+alt+k")
            return
        if not hotkey_free(spec["mods"], spec["key"]):
            mods_now, key_now = hotkey_spec()
            if not same_hotkey(spec, mods_now, key_now):
                say("Комбинация %s занята другой программой — выбери другую."
                    % hotkey_text(spec))
                return
        save_hotkey(spec)
        say("Хоткей сохранён: " + hotkey_text(spec))
        if running_pid():
            stop_running()
            start_detached()
            say("Приложение перезапущено с новым хоткеем.")
        return

    if "--install-autostart" in args:
        ok, lnk = install_autostart()
        say(("Автозапуск включён: %s" % lnk) if ok else
            ("Не удалось создать ярлык: %s" % lnk))
        return

    if "--uninstall-autostart" in args:
        ok, lnk = uninstall_autostart()
        say(("Ярлык удалён: %s" % lnk) if ok else ("Не удалось удалить: %s" % lnk))
        return

    if "--snapshot" in args:
        i = args.index("--snapshot")
        path = args[i + 1] if len(args) > i + 1 else "snapshot.png"
        idx = int(args[args.index("--index") + 1]) if "--index" in args else 0
        snapshot(path, idx)
        return

    if "--stop" in args:
        pid = running_pid()
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            say("остановлено: %s" % pid)
        else:
            say("не запущено")
        return

    pid = running_pid()
    if pid:
        say("уже запущено, pid %s" % pid)
        return

    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        log("не смог записать %s: %s (работаю дальше, --stop может не найти процесс)"
            % (PID_FILE, e))

    log("старт: python %s | lively=%s | lib=%s | photos=%s | хоткей=%s"
        % (sys.version.split()[0], LIVELY_EXE, LIVELY_LIB, PHOTO_DIR, hotkey_text()))
    if not os.path.isfile(LIVELY_EXE):
        log("Lively не установлен — работаем только с папкой картинок")
    ensure_photo_dir()

    root = tk.Tk()
    root.withdraw()
    app = Panel(root)
    threading.Thread(target=hotkey_thread, args=(app.events,), daemon=True).start()
    threading.Thread(target=keyboard_hook_thread, args=(app.events, app.state),
                     daemon=True).start()
    root.after(60, app.poll)
    if "--show" in args:
        root.after(300, app.show)
    if "--hotkey-dialog" in args:
        root.after(300, app.hotkey_dialog)
    try:
        root.mainloop()
    finally:
        # удаляем pid-файл только если он наш: при restart_self там уже чужой
        try:
            with open(PID_FILE) as f:
                mine = int(f.read().strip())
            if mine == os.getpid():
                os.remove(PID_FILE)
        except Exception:
            pass

if __name__ == "__main__":
    main()