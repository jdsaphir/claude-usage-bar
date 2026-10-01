"""
Claude Usage Bar - a small always-on-top readout that sits on the Windows
taskbar and shows current session (5h) and weekly (7d) limit usage.

Run with pythonw.exe so no console window appears:
    pythonw ClaudeUsageBar.pyw

Right-click the widget for refresh / autostart / demo / quit.
Left-drag it sideways to reposition; the offset is remembered.
"""

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_usage as cu  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
)
STARTUP_FILE = os.path.join(STARTUP_DIR, "ClaudeUsageBar.vbs")
LOG_PATH = os.path.join(APP_DIR, "claude-usage-bar.log")
MAX_LOG_BYTES = 256 * 1024


def _open_log():
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            os.remove(LOG_PATH)
        return open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


# Under pythonw.exe there is no console, so sys.stderr is None. Anything that
# writes to it - including tkinter's own exception reporter - then raises
# inside the error handler and can take the whole process down silently.
if sys.stderr is None or sys.stdout is None:
    _fallback_log = _open_log()
    if sys.stdout is None:
        sys.stdout = _fallback_log
    if sys.stderr is None:
        sys.stderr = _fallback_log


def log_error(context, exc_info=None):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n[%s] %s\n" % (stamp, context))
            traceback.print_exception(*(exc_info or sys.exc_info()), file=fh)
    except (OSError, TypeError):
        pass


DEFAULTS = {
    "poll_seconds": 60,
    "x_offset": 14,
    "y_nudge": 0,
    "scale": 1.0,
    "show_labels": True,
    "show_reset": False,
    "warn_at": 75,
    "critical_at": 90,
    "hide_on_fullscreen": True,
    "demo": False,
}

# ---- palette -------------------------------------------------------------
TRANSPARENT_KEY = "#010203"
PANEL_BG = "#16161A"
PANEL_EDGE = "#2E2E36"
TRACK = "#2C2C33"
LABEL_FG = "#7A7A88"
TEXT_FG = "#E9E9EE"
TEXT_DIM = "#8A8A96"
CLAUDE_ORANGE = "#D97757"
VIOLET = "#A78BFA"
AMBER = "#E0A458"
RED = "#EF4444"
GLYPH_IDLE = "#4A4A55"


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Win32 helpers
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FLAGS = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


# These prototypes are load-bearing, not decoration. Without argtypes, ctypes
# passes the special HWND_TOPMOST (-1) / HWND_NOTOPMOST (-2) values as 32-bit
# ints, so on 64-bit Windows they arrive as 0x00000000FFFFFFFF instead of a
# sign-extended pointer and SetWindowPos fails with ERROR_INVALID_WINDOW_HANDLE
# - silently, since nothing checks the return value. Declaring HWND params
# makes ctypes sign-extend them correctly.
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.FindWindowW.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE


def set_dpi_aware():
    """Per-monitor-v2 if available, else the older APIs."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def taskbar_rect():
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return None
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def foreground_is_fullscreen(our_hwnd=None):
    """True only if a fullscreen window covers the monitor our widget sits on.

    Scoping it to one monitor matters on multi-monitor setups: a fullscreen
    video on another screen should not blank the widget.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    if window_class(hwnd) in ("Shell_TrayWnd", "WorkerW", "Progman", "TaskListThumbnailWnd"):
        return False
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if our_hwnd:
        ours = user32.MonitorFromWindow(our_hwnd, MONITOR_DEFAULTTONEAREST)
        if ours and mon != ours:
            return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    m = info.rcMonitor
    return (rect.left <= m.left and rect.top <= m.top
            and rect.right >= m.right and rect.bottom >= m.bottom)


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

def rounded_points(x0, y0, x1, y1, r, steps=6):
    """Point list approximating a rounded rectangle."""
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r <= 0.5:
        return [x0, y0, x1, y0, x1, y1, x0, y1]
    pts = []
    corners = (
        (x1 - r, y1 - r, 0.0),          # bottom-right
        (x0 + r, y1 - r, math.pi / 2),  # bottom-left
        (x0 + r, y0 + r, math.pi),      # top-left
        (x1 - r, y0 + r, 3 * math.pi / 2),  # top-right
    )
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = start + (math.pi / 2) * (i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class UsageBar:
    # How often to re-check position and z-order. Short enough that being
    # covered by the taskbar is not perceptible; the checks are a few cheap
    # Win32 calls.
    TICK_MS = 350

    def __init__(self):
        set_dpi_aware()
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.withdraw()

        self.scale = self._resolve_scale()
        self.geom = self._layout()

        self.data = None
        self.error = None
        self.last_ok = None
        self.shown = {"session": 0.0, "weekly": 0.0}   # animated values
        self.target = {"session": 0.0, "weekly": 0.0}
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.dragging = None
        self.tip = None

        self.alive = True

        self._build_window()
        self._build_menu()
        self._start_worker()

        self.root.report_callback_exception = self._on_callback_error
        self.root.after(60, self._apply_styles)
        self._schedule(self._pump, 200)
        self._schedule(self._animate, 33)
        self._schedule(self._housekeeping, self.TICK_MS)

    def _on_callback_error(self, exc, val, tb):
        log_error("unhandled callback exception", (exc, val, tb))

    def _schedule(self, fn, delay):
        """Run fn every delay ms, surviving exceptions.

        Self-rescheduling callbacks stop forever if they ever raise before
        their own after() call, which would silently freeze the widget.
        """
        def run():
            try:
                fn()
            except Exception:
                log_error("in %s" % fn.__name__)
            finally:
                if self.alive:
                    try:
                        self.root.after(delay, run)
                    except tk.TclError:
                        pass

        self.root.after(delay, run)

    # -- geometry ----------------------------------------------------------

    def _resolve_scale(self):
        try:
            dpi = self.root.winfo_fpixels("1i")
        except tk.TclError:
            dpi = 96.0
        return (dpi / 96.0) * float(self.cfg.get("scale", 1.0))

    def _layout(self):
        s = self.scale

        def p(v):
            return int(round(v * s))

        # Room for "6d 12h" after the percentage when reset times are shown.
        extra = p(42) if self.cfg.get("show_reset") else 0
        g = {
            "w": p(214) + extra, "h": p(36), "pad": p(9),
            "glyph_cx": p(18), "glyph_r": p(8.5),
            "label_x": p(32),
            "bar_x0": p(52), "bar_x1": p(168), "bar_h": max(4, p(7)),
            "pct_x": p(205),
            "reset_x": p(205) + extra,
            "row_cy": (p(12.5), p(23.5)),
            "radius": p(9),
            "f_label": max(6, int(round(7 * s))),
            "f_pct": max(7, int(round(9 * s))),
        }
        return g

    def _build_window(self):
        r = self.root
        r.overrideredirect(True)
        r.configure(bg=TRANSPARENT_KEY)
        r.attributes("-topmost", True)
        try:
            r.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
        r.attributes("-alpha", 0.98)

        self.canvas = tk.Canvas(
            r, width=self.geom["w"], height=self.geom["h"],
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        self.canvas.bind("<Button-3>", self._popup_menu)
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Double-Button-1>", lambda e: self.refresh_now())
        self.canvas.bind("<Enter>", self._show_tip)
        self.canvas.bind("<Leave>", self._hide_tip)

        r.deiconify()
        self._reposition()
        self._draw()

    def _apply_styles(self):
        """Tool-window + no-activate so it stays out of Alt-Tab and never steals focus."""
        try:
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            self.hwnd = hwnd
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except Exception:
            self.hwnd = None

    def _reposition(self):
        rect = taskbar_rect()
        g = self.geom
        if rect is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x, y = int(self.cfg["x_offset"] * self.scale), sh - g["h"] - 8
        else:
            tb_h = rect.bottom - rect.top
            x = rect.left + int(self.cfg["x_offset"] * self.scale)
            y = rect.top + (tb_h - g["h"]) // 2 + int(self.cfg["y_nudge"] * self.scale)
            # Vertical taskbars: fall back to hugging the top of the bar.
            if tb_h > (rect.right - rect.left):
                y = rect.top + int(8 * self.scale)
        # Only touch geometry when it actually changed - this runs every second.
        if getattr(self, "pos", None) != (x, y):
            self.root.geometry("%dx%d+%d+%d" % (g["w"], g["h"], x, y))
            self.pos = (x, y)

    # -- data --------------------------------------------------------------

    def _start_worker(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            if self.cfg.get("demo"):
                self.q.put(("demo", None))
            else:
                try:
                    self.q.put(("ok", cu.fetch_usage()))
                except cu.UsageError as exc:
                    self.q.put(("err", exc))
                except Exception as exc:  # never let the poller die
                    log_error("in usage poller")
                    self.q.put(("err", cu.UsageError(str(exc)[:40])))
            self.wake.wait(max(10, int(self.cfg.get("poll_seconds", 60))))
            self.wake.clear()

    def _pump(self):
        changed = False
        try:
            while True:
                kind, payload = self.q.get_nowait()
                changed = True
                if kind == "ok":
                    self.data, self.error = payload, None
                    self.last_ok = payload["fetched_at"]
                elif kind == "demo":
                    self.data, self.error = self._demo_data(), None
                    self.last_ok = time.time()
                else:
                    self.error = payload
                self._sync_targets()
        except queue.Empty:
            pass
        if changed:
            # Repaint even when no bar is moving, so glyph/error state updates.
            self._draw()

    def _demo_data(self):
        phase = time.time() / 9.0
        return {
            "session": {"percent": 50 + 42 * math.sin(phase), "resets_at": None},
            "weekly": {"percent": 45 + 30 * math.sin(phase / 2.7), "resets_at": None},
            "weekly_opus": {"percent": None, "resets_at": None},
            "fetched_at": time.time(), "raw": {},
        }

    def _sync_targets(self):
        for key in ("session", "weekly"):
            pct = (self.data or {}).get(key, {}).get("percent") if self.data else None
            self.target[key] = 0.0 if pct is None else float(pct)

    def refresh_now(self):
        self.wake.set()

    # -- animation ---------------------------------------------------------

    def _animate(self):
        moved = False
        for key in ("session", "weekly"):
            cur, tgt = self.shown[key], self.target[key]
            if abs(tgt - cur) > 0.05:
                self.shown[key] = cur + (tgt - cur) * 0.18
                moved = True
            else:
                self.shown[key] = tgt
        if moved or self.cfg.get("demo"):
            self._draw()

    # -- painting ----------------------------------------------------------

    def _bar_color(self, pct, base):
        if pct >= self.cfg["critical_at"]:
            return RED
        if pct >= self.cfg["warn_at"]:
            return AMBER
        return base

    def _draw(self):
        c = self.canvas
        g = self.geom
        c.delete("all")

        healthy = self.data is not None and self.error is None

        # panel
        c.create_polygon(rounded_points(0, 0, g["w"] - 1, g["h"] - 1, g["radius"]),
                         fill=PANEL_BG, outline=PANEL_EDGE, width=1, smooth=False)

        self._draw_glyph(healthy)

        rows = (("session", "5H", CLAUDE_ORANGE), ("weekly", "7D", VIOLET))
        for idx, (key, label, base) in enumerate(rows):
            cy = g["row_cy"][idx]
            if self.cfg.get("show_labels"):
                c.create_text(g["label_x"], cy, text=label, anchor="w",
                              fill=LABEL_FG,
                              font=("Segoe UI", g["f_label"], "bold"))

            y0, y1 = cy - g["bar_h"] / 2.0, cy + g["bar_h"] / 2.0
            rad = g["bar_h"] / 2.0
            c.create_polygon(rounded_points(g["bar_x0"], y0, g["bar_x1"], y1, rad),
                             fill=TRACK, outline="", smooth=False)

            pct_raw = None
            if self.data:
                pct_raw = self.data.get(key, {}).get("percent")

            if pct_raw is not None:
                shown = self.shown[key]
                span = g["bar_x1"] - g["bar_x0"]
                fill_w = max(g["bar_h"], span * (shown / 100.0))
                colour = self._bar_color(shown, base)
                c.create_polygon(
                    rounded_points(g["bar_x0"], y0, g["bar_x0"] + fill_w, y1, rad),
                    fill=colour, outline="", smooth=False)
                c.create_text(g["pct_x"], cy, text="%d%%" % round(shown), anchor="e",
                              fill=TEXT_FG if shown < self.cfg["critical_at"] else RED,
                              font=("Segoe UI", g["f_pct"], "bold"))
                resets = self.data.get(key, {}).get("resets_at")
                if self.cfg.get("show_reset") and resets is not None:
                    c.create_text(g["reset_x"], cy, text=cu.humanise_reset(resets),
                                  anchor="e", fill=TEXT_DIM,
                                  font=("Segoe UI", g["f_label"] + 1))
            else:
                c.create_text(g["pct_x"], cy, text="--", anchor="e", fill=TEXT_DIM,
                              font=("Segoe UI", g["f_pct"], "bold"))

    def _draw_glyph(self, healthy):
        """Claude-style radial burst mark."""
        g = self.geom
        cx, cy = g["glyph_cx"], g["h"] / 2.0
        r_out = g["glyph_r"]
        r_in = r_out * 0.20
        if self.error is not None:
            colour = AMBER if self.error.kind == "network" else RED
        elif healthy:
            colour = CLAUDE_ORANGE
        else:
            colour = GLYPH_IDLE

        rays = 11
        width = max(2, int(round(2.1 * self.scale)))
        for i in range(rays):
            a = (2 * math.pi / rays) * i - math.pi / 2
            self.canvas.create_line(
                cx + r_in * math.cos(a), cy + r_in * math.sin(a),
                cx + r_out * math.cos(a), cy + r_out * math.sin(a),
                fill=colour, width=width, capstyle="round")

    # -- tooltip -----------------------------------------------------------

    def _tip_text(self):
        if self.error is not None and not self.data:
            if self.error.kind == "auth":
                return ("Claude usage - not signed in\n%s\n"
                        "Run  claude  in a terminal and sign in once." % self.error)
            return "Claude usage - %s" % self.error

        if not self.data:
            return "Claude usage - loading..."

        lines = []
        if self.error is not None:
            # Bars still show the last good numbers - say why they are frozen.
            lines.append("!! %s (showing last known)" % self.error)
            lines.append("")
        pairs = (("session", "Session (5h)"), ("weekly", "Week (7d)"),
                 ("weekly_opus", "Week (Opus)"))
        for key, title in pairs:
            win = self.data.get(key) or {}
            pct = win.get("percent")
            if pct is None:
                continue
            resets = win.get("resets_at")
            suffix = "  -  resets in %s" % cu.humanise_reset(resets) if resets else ""
            lines.append("%-14s %3d%%%s" % (title, round(pct), suffix))
        if self.last_ok:
            lines.append("")
            lines.append("updated %s" % datetime.fromtimestamp(self.last_ok).strftime("%H:%M:%S"))
        return "\n".join(lines) or "Claude usage - no data"

    def _show_tip(self, _event=None):
        self._hide_tip()
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=PANEL_EDGE)
        label = tk.Label(tip, text=self._tip_text(), justify="left",
                         bg=PANEL_BG, fg=TEXT_FG, padx=10, pady=7,
                         font=("Consolas", max(8, int(round(8 * self.scale)))))
        label.pack(padx=1, pady=1)
        tip.update_idletasks()
        x = self.pos[0]
        y = self.pos[1] - tip.winfo_height() - int(8 * self.scale)
        tip.geometry("+%d+%d" % (max(0, x), max(0, y)))
        self.tip = tip

    def _hide_tip(self, _event=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None

    # -- interaction -------------------------------------------------------

    def _drag_start(self, event):
        self.dragging = (event.x_root, self.cfg["x_offset"])
        self._hide_tip()

    def _drag_move(self, event):
        if not self.dragging:
            return
        start_x, start_off = self.dragging
        delta = (event.x_root - start_x) / self.scale
        self.cfg["x_offset"] = max(0, int(start_off + delta))
        self._reposition()

    def _drag_end(self, _event):
        if self.dragging:
            self.dragging = None
            save_config(self.cfg)

    def _build_menu(self):
        # Contents are rebuilt on each popup so the toggles show current state.
        self.menu = tk.Menu(self.root, tearoff=0)

    def _popup_menu(self, event):
        self._hide_tip()
        self.menu.delete(0, "end")
        self.menu.add_command(label="Refresh now", command=self.refresh_now)
        self.menu.add_separator()
        self.menu.add_command(
            label=("%s Start with Windows" % ("[x]" if self._autostart_on() else "[  ]")),
            command=self._toggle_autostart)
        self.menu.add_command(
            label=("%s Show time to reset" % ("[x]" if self.cfg.get("show_reset") else "[  ]")),
            command=self._toggle_show_reset)
        self.menu.add_command(
            label=("%s Demo mode" % ("[x]" if self.cfg.get("demo") else "[  ]")),
            command=self._toggle_demo)
        self.menu.add_command(label="Open config file", command=self._open_config)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self._quit)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _autostart_on(self):
        return os.path.exists(STARTUP_FILE)

    def _toggle_autostart(self):
        if self._autostart_on():
            try:
                os.remove(STARTUP_FILE)
            except OSError:
                pass
            return
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        script = os.path.join(APP_DIR, os.path.basename(__file__))
        vbs = (
            'Set s = CreateObject("WScript.Shell")\r\n'
            's.Run """%s"" ""%s""", 0, False\r\n' % (pythonw, script)
        )
        try:
            os.makedirs(STARTUP_DIR, exist_ok=True)
            with open(STARTUP_FILE, "w", encoding="utf-8") as fh:
                fh.write(vbs)
        except OSError:
            pass

    def _toggle_show_reset(self):
        self.cfg["show_reset"] = not self.cfg.get("show_reset")
        save_config(self.cfg)
        # The widget changes width, so rebuild the layout and resize in place.
        self.geom = self._layout()
        self.canvas.config(width=self.geom["w"], height=self.geom["h"])
        self.pos = None
        self._reposition()
        self._draw()

    def _toggle_demo(self):
        self.cfg["demo"] = not self.cfg.get("demo")
        save_config(self.cfg)
        self.refresh_now()

    def _open_config(self):
        if not os.path.exists(CONFIG_PATH):
            save_config(self.cfg)
        try:
            os.startfile(CONFIG_PATH)  # noqa: S606
        except OSError:
            subprocess.Popen(["notepad.exe", CONFIG_PATH])

    def _quit(self):
        self.alive = False  # stop the scheduled loops rescheduling onto a dead root
        self._hide_tip()
        self.root.destroy()

    # -- upkeep ------------------------------------------------------------

    def _covered_by_taskbar(self):
        """True if the taskbar specifically is painted over the widget.

        Clicking the taskbar raises Shell_TrayWnd above us, and it never drops
        back on its own. Only the taskbar is worth fighting: if a Start menu or
        another flyout is on top, punching through it would be worse than being
        briefly hidden.
        """
        if not getattr(self, "hwnd", None) or not getattr(self, "pos", None):
            return False
        x = self.pos[0] + self.geom["w"] // 2
        y = self.pos[1] + self.geom["h"] // 2
        top = user32.WindowFromPoint(POINT(x, y))
        if not top:
            return False
        root = user32.GetAncestor(top, GA_ROOT)
        if root == self.hwnd:
            return False
        return window_class(root) == "Shell_TrayWnd"

    def _force_topmost(self):
        """Re-enter the topmost band.

        A plain SetWindowPos(HWND_TOPMOST) on a window that is *already*
        topmost is a no-op, so it cannot climb back over the taskbar. Dropping
        to NOTOPMOST first makes it a real z-order change.
        """
        try:
            user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        except Exception:
            pass

    def _housekeeping(self):
        """Keep the widget pinned above the taskbar and out of fullscreen apps."""
        self._reposition()
        # Keep the reset countdown ticking between polls (it has minute resolution).
        minute = int(time.time() // 60)
        if self.cfg.get("show_reset") and minute != getattr(self, "drawn_minute", None):
            self.drawn_minute = minute
            self._draw()
        if (self.cfg.get("hide_on_fullscreen")
                and foreground_is_fullscreen(getattr(self, "hwnd", None))):
            self.root.withdraw()
        else:
            if not self.root.winfo_viewable():
                self.root.deiconify()
            if getattr(self, "hwnd", None) and self._covered_by_taskbar():
                self._force_topmost()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = UsageBar()
    if "--demo" in sys.argv:
        app.cfg["demo"] = True
        app.refresh_now()
    app.run()
