"""
jiggler.py - A lightweight, low-cost mouse jiggler with a small Tkinter GUI.

Click "Start" and it waits 5 seconds, then keeps the machine from locking /
sleeping until you touch the mouse yourself, at which point it stops and
resets back to the idle state so you can hit Start again whenever you like.

Two things needed to actually stop it from locking (a plain SetCursorPos()
loop is not enough on many systems):

1. SendInput() - injects a real synthetic mouse-move event into the input
   queue, the same path physical hardware uses. This resets the "last
   input" idle timer that drives screensaver-triggered lock and the GPO
   "Interactive logon: Machine inactivity limit" policy.
2. SetThreadExecutionState() - explicitly tells Windows' power manager
   the system/display is in use, which prevents the power-plan sleep /
   display-timeout path (a separate mechanism from #1) from kicking in.

Uses only the Python standard library (tkinter + ctypes) - no pip
installs. Everything is driven by Tkinter's own event-driven `after()`
scheduler, so it sits at effectively 0% CPU between ticks.

Windows only.

Run:
    python jiggler.py
"""

import ctypes
import tkinter as tk

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---- Win32 SendInput plumbing -----------------------------------------

PUL = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]


INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


def send_relative_move(dx, dy):
    """Inject a genuine synthetic mouse-move event (relative, in pixels)."""
    extra = ctypes.c_ulong(0)
    mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, ctypes.pointer(extra))
    inp = INPUT(INPUT_MOUSE, _INPUTunion(mi=mi))
    user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# ---- Power/idle state -----------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake(enabled: bool):
    if enabled:
        kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    else:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)


# ---- Config -------------------------------------------------------------

START_DELAY_S      = 5      # seconds to wait after clicking Start
JIGGLE_EVERY_MS     = 10000  # how often to nudge the mouse while jiggling
WATCH_INTERVAL_MS   = 250    # how often to check for user-initiated movement
NUDGE_PX            = 2      # how far to nudge (and immediately nudge back)
MOVE_THRESHOLD_PX   = 40     # user must move the cursor at least this far to stop


class Jiggler(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mouse Jiggler")
        self.geometry("300x150")
        self.resizable(False, False)

        self.state = "idle"          # idle -> countdown -> jiggling
        self.countdown = 0
        self.expected_pos = None
        self._after_ids = []

        self.status_var = tk.StringVar(value="Idle")
        tk.Label(self, textvariable=self.status_var, font=("Segoe UI", 12)).pack(pady=(22, 12))

        self.start_btn = tk.Button(self, text="Start", width=14, command=self.start)
        self.start_btn.pack(pady=4)

        self.stop_btn = tk.Button(self, text="Stop", width=14, command=self.reset, state="disabled")
        self.stop_btn.pack(pady=4)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- scheduling helpers, so everything can be cleanly cancelled ----
    def _schedule(self, delay_ms, fn):
        aid = self.after(delay_ms, fn)
        self._after_ids.append(aid)
        return aid

    def _cancel_all(self):
        for aid in self._after_ids:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._after_ids.clear()

    # ---- control flow ----
    def start(self):
        if self.state != "idle":
            return
        self.state = "countdown"
        self.countdown = START_DELAY_S
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._tick_countdown()

    def _tick_countdown(self):
        if self.state != "countdown":
            return
        if self.countdown <= 0:
            self._begin_jiggling()
            return
        self.status_var.set(f"Starting in {self.countdown}...")
        self.countdown -= 1
        self._schedule(1000, self._tick_countdown)

    def _begin_jiggling(self):
        self.state = "jiggling"
        self.expected_pos = get_cursor_pos()
        keep_awake(True)
        self.status_var.set("Jiggling - move mouse to stop")
        self._schedule(JIGGLE_EVERY_MS, self._do_jiggle)
        self._schedule(WATCH_INTERVAL_MS, self._watch)

    def _do_jiggle(self):
        if self.state != "jiggling":
            return
        # Real synthetic input, not a teleport - resets the idle/lock timer
        # the same way wiggling a physical mouse would, then nudges back so
        # the cursor doesn't visibly wander off.
        send_relative_move(NUDGE_PX, 0)
        send_relative_move(-NUDGE_PX, 0)
        self.expected_pos = get_cursor_pos()
        self._schedule(JIGGLE_EVERY_MS, self._do_jiggle)

    def _watch(self):
        if self.state != "jiggling":
            return
        x, y = get_cursor_pos()
        ex, ey = self.expected_pos
        dist = ((x - ex) ** 2 + (y - ey) ** 2) ** 0.5
        if dist >= MOVE_THRESHOLD_PX:
            self.status_var.set("Mouse moved - stopped")
            self.reset(keep_message=True)
            return
        self._schedule(WATCH_INTERVAL_MS, self._watch)

    def reset(self, keep_message=False):
        self._cancel_all()
        keep_awake(False)
        self.state = "idle"
        self.expected_pos = None
        self.countdown = 0
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if keep_message:
            # briefly show why it stopped, then fall back to Idle
            self.after(1500, lambda: self.status_var.set("Idle"))
        else:
            self.status_var.set("Idle")

    def on_close(self):
        self._cancel_all()
        keep_awake(False)
        self.destroy()


if __name__ == "__main__":
    Jiggler().mainloop()
