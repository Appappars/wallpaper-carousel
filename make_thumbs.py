#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Превью (thumbnail) для обоев Lively — кадр с живого окна Chrome.

Headless-Chrome здесь не годится: WebGL-обои в нём не рисуются, а «снимок после
загрузки» ловит интро-экран вместо анимации. Поэтому страница открывается в
отдельном окне Chrome (kiosk, поверх всех), UI-обвес страницы скрывается
инъекцией CSS, через N секунд снимается экран.

  python make_thumbs.py                 # только тем, у кого превью нет
  python make_thumbs.py corals moire    # переснять конкретные
  python make_thumbs.py --all           # переснять всё

Старое превью сохраняется рядом как thumbnail.orig.png (один раз).
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageGrab, ImageStat

LIB = os.environ.get("LIVELY_LIB") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Lively Wallpaper", "Library", "wallpapers")
HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 1280, 720


def find_browser():
    """Chrome, а если его нет — Edge (тот же движок, тот же kiosk-режим)."""
    cands = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        cands.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for cand in cands:
        if os.path.isfile(cand):
            return cand
    return cands[0] if cands else "chrome.exe"


CHROME = os.environ.get("BROWSER_EXE") or find_browser()

# сколько ждать, чтобы анимация успела набрать характерный кадр
WAIT = {
    "corals": 15.0,
    "moire": 5.0,
    "physarum": 6.0,
    "lattice-4d": 15.0,
    "matrixrain-cyrillic": 5.0,
}
DEFAULT_WAIT = 7.0

# прячем только обвес страницы; canvas/svg со сценой остаются
HIDE_UI = ("<style>"
           "header, aside, .frame, .legend, .cluster, .title-block, .panel,"
           ".readout, .hud, .bar, .hint-bar {visibility:hidden!important}"
           "</style>")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def windows_of(pid):
    found = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        pid_ = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_))
        if pid_.value == pid and user32.IsWindowVisible(hwnd):
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 600 and r.bottom - r.top > 400:
                found.append(hwnd)
        return True

    user32.EnumWindows(CB(cb), 0)
    return found


def rect_size(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.right - r.left, r.bottom - r.top)


def kill_profile(profile):
    """Гасит chrome, оставшийся на этом временном профиле (иначе новое окно не появится)."""
    marker = profile.lower()
    try:
        out = subprocess.run(["wmic", "process", "where", "name='chrome.exe'", "get",
                              "ProcessId,CommandLine"],
                             capture_output=True, text=True, errors="ignore",
                             timeout=25).stdout
    except Exception:
        return
    for line in out.splitlines():
        if marker and marker in line.lower():
            tail = line.strip().rsplit(None, 1)
            if tail and tail[-1].isdigit():
                subprocess.run(["taskkill", "/PID", tail[-1], "/F"], capture_output=True)


def entry_html(folder):
    with open(os.path.join(folder, "LivelyInfo.json"), encoding="utf-8-sig") as f:
        info = json.load(f)
    fn = info.get("FileName") or "index.html"
    page = fn if os.path.isabs(fn) else os.path.join(folder, fn)
    thumb = info.get("Thumbnail") or ""
    target = (thumb if os.path.isabs(thumb) else os.path.join(folder, thumb)) if thumb else None
    for name in ("thumbnail.png", "thumbnail.jpg"):
        if target is None and os.path.isfile(os.path.join(folder, name)):
            target = os.path.join(folder, name)
    return page, (target or os.path.join(folder, "thumbnail.png"))


def shoot(name, page):
    src = open(page, encoding="utf-8-sig").read()
    tmp_page = os.path.join(os.path.dirname(page), "__thumb_tmp.html")
    with open(tmp_page, "w", encoding="utf-8") as f:
        f.write(src.replace("</head>", HIDE_UI + "</head>", 1))

    profile = os.path.join(tempfile.gettempdir(), "chthumb-" + name)
    kill_profile(profile)
    shutil.rmtree(profile, ignore_errors=True)

    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    proc = subprocess.Popen([
        CHROME, "--kiosk", "--app=file:///" + tmp_page.replace("\\", "/"),
        "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=%d,%d" % (W, H), "--window-position=0,0",
    ])
    hwnd = None
    try:
        for _ in range(60):
            time.sleep(0.25)
            wins = [h for h in windows_of(proc.pid) if rect_size(h)[0] >= sw * 0.9]
            if wins:
                hwnd = wins[0]
                break
        if hwnd is None:
            return None, "окно не появилось"

        HWND_TOPMOST, SWP_SHOWWINDOW, SW_MAXIMIZE = -1, 0x0040, 3
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, sw, sh, SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)

        time.sleep(WAIT.get(name, DEFAULT_WAIT))
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, sw, sh, SWP_SHOWWINDOW)
        time.sleep(0.35)

        img = ImageGrab.grab(all_screens=True).convert("RGB").crop((0, 0, sw, sh))
        if img.size != (W, H):
            img = img.resize((W, H), Image.LANCZOS)
        if max(ImageStat.Stat(img).stddev) < 1.5:
            return None, "пустой кадр"
        return img, "ok"
    finally:
        try:
            proc.terminate()
            time.sleep(1.0)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        for _ in range(20):
            try:
                os.remove(tmp_page)
                break
            except OSError:
                time.sleep(0.2)
        shutil.rmtree(profile, ignore_errors=True)


def has_thumb(target):
    return os.path.isfile(target) and os.path.getsize(target) > 5000


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    all_flag = "--all" in sys.argv
    names = args or sorted(os.path.basename(p) for p in
                           [os.path.join(LIB, d) for d in os.listdir(LIB)]
                           if os.path.isdir(os.path.join(LIB, d)))

    made = 0
    for name in names:
        folder = os.path.join(LIB, name)
        if not os.path.isfile(os.path.join(folder, "LivelyInfo.json")):
            continue
        page, target = entry_html(folder)
        if not args and not all_flag and has_thumb(target):
            continue
        if not os.path.isfile(page):
            print("%-22s нет %s" % (name, os.path.basename(page)))
            continue

        img, status = shoot(name, page)
        if img is None:
            print("%-22s %s" % (name, status))
            continue

        if has_thumb(target):
            orig = os.path.join(os.path.dirname(target),
                                "thumbnail.orig" + os.path.splitext(target)[1])
            if not os.path.isfile(orig):
                shutil.copy2(target, orig)
        img.save(target)
        made += 1
        print("%-22s ok -> %s" % (name, os.path.basename(target)))

    print("готово, превью обновлено:", made)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()