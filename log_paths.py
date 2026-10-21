"""
Where every log goes — one place, decided once at startup (user request 2026-10-21).

    <LOG_ROOT>/system/       continuous / instrument logs: pressure_log_*.csv (DataLogger),
                             ln2_history.csv (LN2Monitor), bep_log.csv (BEP history)
    <LOG_ROOT>/deposition/   one run log per deposition: <stamp>_<sample>.xlsx (RecipeLogger)
    <LOG_ROOT>/rheed/        RHEED exports / intensity traces (only when the user saves)

LOG_ROOT = ~/Box/logs  (C:\\Users\\<whoever is logged in>\\Box\\logs — Box Drive's mount point,
           so the same code works on every lab computer), used only when ~/Box EXISTS:
           we never create a fake "Box" folder that would not sync.
           Otherwise (no Box Drive, or it cannot be written) → <app folder>/logs, and
           describe() says why — the GUI logs that line at startup.
           Override: environment variable VAC_LOG_ROOT=<folder>.

Box Drive is a cloud-backed virtual drive: writes can stall or a file can be briefly
locked while it uploads. So anything written continuously must not be written from a
hardware thread — DataLogger writes through its own writer thread (data_logger.py).
"""

import os
import shutil
import sys

ENV_OVERRIDE = "VAC_LOG_ROOT"


def app_dir() -> str:
    """Folder of the program: the .exe's folder when frozen (NOT PyInstaller's temp
    _MEIPASS dir, where logs would vanish on exit), else this source folder."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


LOCAL_ROOT = os.path.join(app_dir(), "logs")


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _choose_root():
    env = os.environ.get(ENV_OVERRIDE, "").strip()
    if env:
        if _writable(env):
            return env, f"logs → {env} (from {ENV_OVERRIDE})"
        return LOCAL_ROOT, f"⚠ {ENV_OVERRIDE}={env} is not writable — logs → {LOCAL_ROOT}"
    box = os.path.join(os.path.expanduser("~"), "Box")
    if os.path.isdir(box):
        root = os.path.join(box, "logs")
        if _writable(os.path.join(root, "system")) and _writable(os.path.join(root, "deposition")):
            return root, f"logs → Box: {root}"
        return LOCAL_ROOT, f"⚠ Box found but {root} is not writable (Box Drive signed out?) — logs → {LOCAL_ROOT}"
    return LOCAL_ROOT, f"⚠ no Box folder at {box} — logs → {LOCAL_ROOT}"


LOG_ROOT, _REASON = _choose_root()
SYSTEM_DIR = os.path.join(LOG_ROOT, "system")
DEPOSITION_DIR = os.path.join(LOG_ROOT, "deposition")
RHEED_DIR = os.path.join(LOG_ROOT, "rheed")
LOCAL_SYSTEM_DIR = os.path.join(LOCAL_ROOT, "system")
LOCAL_DEPOSITION_DIR = os.path.join(LOCAL_ROOT, "deposition")


def describe() -> str:
    """One line for the GUI log: where logs go and, if not Box, why not."""
    return _REASON


def ensure(d: str, fallback: str) -> str:
    """d if it can be created/written right now, else fallback (created). For writes
    that happen later in the session — Box Drive can go offline mid-run."""
    if _writable(d):
        return d
    os.makedirs(fallback, exist_ok=True)
    return fallback


def system_file(name: str, legacy: str = "") -> str:
    """Path of a cumulative system file under SYSTEM_DIR. If it does not exist there yet
    but an older copy does (legacy, e.g. the pre-2026-10-21 location), the old file is
    COPIED once so histories (LN2, BEP) stay continuous; the original is left in place."""
    path = os.path.join(SYSTEM_DIR, name)
    if legacy and not os.path.exists(path) and os.path.isfile(legacy):
        try:
            os.makedirs(SYSTEM_DIR, exist_ok=True)
            shutil.copy2(legacy, path)
        except OSError:
            return legacy                      # keep using the old file rather than lose it
    return path
