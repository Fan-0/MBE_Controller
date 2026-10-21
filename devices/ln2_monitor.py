"""
LN2 Dewar Level Monitor — Air Products APDirect tank reading tracker.

Tracks liquid nitrogen level for the deposition system's LN2 dewar by
periodically logging into the Air Products APDirect portal
(https://apdirect.airproducts.com/Tanks/Readings/<tank_id>) and reading the
tank level. Runs independently of the main hardware Poller — this is a web
data source, not a serial device, and is checked on its own schedule
(twice daily by default) rather than continuously.

CREDENTIALS
-----------
Never stored in source, never passed on the command line, never logged.
Encrypted at rest with Windows DPAPI (CryptProtectData), which ties the
ciphertext to the current Windows user account + machine — it cannot be
decrypted by copying the file elsewhere or by another Windows account.

One-time setup (run this yourself, interactively, in your own terminal —
NOT through an assistant/automation, since the password prompt is hidden
input that requires a real interactive terminal):

    python tools/setup_ln2_credentials.py

This writes secrets/ln2_creds.dat. That file (and the secrets/ folder) is
gitignored. Nothing in this module ever logs, prints, or returns the
decrypted username/password — fetch() only uses them in-memory for the
duration of one login request.

STATUS OF THE SCRAPER ITSELF
-----------------------------
apdirect.airproducts.com is an Angular single-page app — the readings are
not in static HTML, they come from a JSON API called after login. Confirmed
via a browser DevTools HAR capture (2026-08-12):
  1. GET  https://account.airproducts.com/login?ReturnUrl=<...>/Tanks/
     -> scrape the __RequestVerificationToken hidden input.
  2. POST same URL, form-encoded: username, Password, password (both cased
     variants sent by the real form), __RequestVerificationToken. Success
     is a 302 redirect off the login page; session auth is cookie-based
     (no bearer token to manage — requests.Session() handles it).
  3. GET  https://apdirect.airproducts.com/Tanks/API/v1/ShipToList
     -> JSON list of tanks; each entry's logicalAssets[] has logicalAssetId,
        lastReading{value,dateTime,uom}, fullValue, fullUOM, logicalChannels[].
        This alone gives the current reading + true full-scale value.
  4. GET  https://apdirect.airproducts.com/Tanks/API/v1/GetEquipmentReadings
        ?logicalChannelId=<id>&from=<iso>&to=<iso>&companyKey=<..>&
         countryCode=<..>&shipToType=<..>
     -> JSON history array for that tank's channel; used to backfill the
        trend plot instead of waiting on twice-daily polling alone.
fetch() still fails safely on any error at any step (bad credentials, page
layout change, network failure, tank not found) — the GUI shows the dewar
in its stale ("no recent reading") state rather than crashing or fabricating
a value. This is the intended fallback behavior, not a bug.
"""
import ctypes
import csv
import json
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Optional

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

log = logging.getLogger("ln2_monitor")

# ── Paths ────────────────────────────────────────────────────────────────
_BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS_PATH   = os.path.join(_BASE_DIR, "secrets", "ln2_creds.dat")
HISTORY_PATH = os.path.join(_BASE_DIR, "logs", "ln2_history.csv")
TANK_ID      = "52228"
TANK_URL     = f"https://apdirect.airproducts.com/Tanks/Readings/{TANK_ID}"

# Times of day (local) the scheduler fires an automatic check. Manual
# refresh (GUI button / fetch()) works at any time regardless of this list.
CHECK_TIMES = [dtime(8, 0), dtime(20, 0)]

STALE_HOURS   = 18.0    # dewar shown yellow if no successful reading within this window
LOW_LEVEL_IN  = 20.0    # dewar shown red if level_in falls at/below this
FULL_SCALE_IN = 46.0    # fallback fill-bar 100% reference if the API's per-tank fullValue
                        # is unavailable; 46 is tank 52228's confirmed fullValue (2026-08-12).
                        # LN2Monitor.full_scale_for() prefers the live value when present.

# ── APDirect endpoints (confirmed via HAR capture, 2026-08-12) ────────────
LOGIN_URL      = "https://account.airproducts.com/login"
RETURN_URL     = "https://apdirect.airproducts.com/Tanks/"
SHIPTO_LIST_URL = "https://apdirect.airproducts.com/Tanks/API/v1/ShipToList"
READINGS_URL   = "https://apdirect.airproducts.com/Tanks/API/v1/GetEquipmentReadings"
BACKFILL_DAYS  = 30.0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_TOKEN_INPUT_RE = re.compile(
    r'<input[^>]*name=["\']__RequestVerificationToken["\'][^>]*>', re.I)
_VALUE_ATTR_RE = re.compile(r'value=["\']([^"\']+)["\']')


def _extract_verification_token(html: str) -> Optional[str]:
    """Pull the __RequestVerificationToken hidden-input value out of the
    APDirect login page HTML. Pure string function — no I/O, easy to unit
    test against a synthetic HTML snippet."""
    m = _TOKEN_INPUT_RE.search(html or "")
    if not m:
        return None
    vm = _VALUE_ATTR_RE.search(m.group(0))
    return vm.group(1) if vm else None


def _find_tank_asset(ship_to_list: list, tank_id: str):
    """Search a parsed ShipToList JSON ('d' array) for the logicalAsset
    matching tank_id. Returns (asset_dict, ship_to_dict) or (None, None).
    Pure function — no I/O."""
    for ship_to in ship_to_list or []:
        for asset in ship_to.get("logicalAssets", []) or []:
            if str(asset.get("logicalAssetId")) == str(tank_id):
                return asset, ship_to
    return None, None


def _parse_readings(readings_json_list) -> list:
    """Convert a GetEquipmentReadings 'd.readings' array into LN2Reading
    objects, skipping any row missing value/dateTime. Pure function."""
    out = []
    for row in readings_json_list or []:
        val, dt_str = row.get("value"), row.get("dateTime")
        if val is None or dt_str is None:
            continue
        try:
            ts = datetime.fromisoformat(dt_str).timestamp()
        except ValueError:
            continue
        out.append(LN2Reading(timestamp=ts, level_in=float(val)))
    return out


# ── Windows DPAPI (ctypes only — no extra dependency) ──────────────────────
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(plaintext: bytes, entropy: bytes = b"vac_control.ln2_monitor.v1") -> bytes:
    """Encrypt bytes with Windows DPAPI, bound to the current user + machine."""
    in_blob = _to_blob(plaintext)
    entropy_blob = _to_blob(entropy)
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob),
        None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def dpapi_unprotect(ciphertext: bytes, entropy: bytes = b"vac_control.ln2_monitor.v1") -> bytes:
    """Decrypt bytes produced by dpapi_protect(). Only works for the same
    Windows user account on the same machine that encrypted it."""
    in_blob = _to_blob(ciphertext)
    entropy_blob = _to_blob(entropy)
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob),
        None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise OSError("CryptUnprotectData failed (wrong user/machine, or corrupt file)")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


@dataclass
class LN2Reading:
    timestamp: float           # time.time() when reading was taken
    level_in: float            # tank level, inches
    raw: Optional[dict] = None # full parsed API payload, for future fields


class LN2Monitor:
    """
    Background LN2 dewar level tracker.

    GUI usage:
        self.ln2 = LN2Monitor()
        self.ln2.on_reading = lambda reading, ok: <bridge to Qt signal>
        self.ln2.start_scheduler()     # begins twice-daily automatic checks
        ...
        threading.Thread(target=self.ln2.fetch, daemon=True).start()  # manual refresh

    All network I/O happens on background threads (scheduler thread, or
    whatever thread calls fetch()) — never call fetch() from the Qt main
    thread. on_reading is invoked from that same background thread; the GUI
    must bridge it to the main thread with a pyqtSignal (see BUG 7 / BUG 20
    in the project context file — QTimer.singleShot from a plain thread is
    unreliable on Windows PyQt5).
    """

    def __init__(self, tank_id: str = TANK_ID, creds_path: str = CREDS_PATH,
                 history_path: str = HISTORY_PATH):
        self.tank_id = tank_id
        self._creds_path = creds_path
        self._history_path = history_path

        self.last_reading: Optional[LN2Reading] = None   # most recent SUCCESSFUL reading
        self.last_attempt_time: Optional[float] = None   # time of most recent attempt (ok or not)
        self.last_attempt_ok: bool = False
        self.last_error: Optional[str] = None
        self.in_progress: bool = False

        # Called (from whatever thread performed the fetch) as on_reading(reading|None, ok: bool)
        self.on_reading = None

        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()   # prevents overlapping fetch() calls
        self._stop_sched = threading.Event()
        self._sched_thread: Optional[threading.Thread] = None

        os.makedirs(os.path.dirname(self._history_path), exist_ok=True)
        self._load_last_from_history()

    # ── Credentials ─────────────────────────────────────────────────────
    def credentials_configured(self) -> bool:
        return os.path.isfile(self._creds_path)

    def _load_credentials(self):
        with open(self._creds_path, "rb") as f:
            blob = f.read()
        plaintext = dpapi_unprotect(blob)
        try:
            data = json.loads(plaintext.decode("utf-8"))
            return data["username"], data["password"]
        finally:
            plaintext = None  # best-effort: drop reference to decrypted bytes promptly

    # ── History (persists across app restarts) ─────────────────────────
    def _load_last_from_history(self):
        if not os.path.isfile(self._history_path):
            return
        try:
            with open(self._history_path, "r", newline="") as f:
                rows = list(csv.reader(f))
            if len(rows) > 1:
                ts_s, level_s = rows[-1][0], rows[-1][1]
                self.last_reading = LN2Reading(timestamp=float(ts_s), level_in=float(level_s))
                self.last_attempt_time = self.last_reading.timestamp
                self.last_attempt_ok = True
        except Exception as e:
            log.warning(f"Could not load LN2 history: {e}")

    def _append_history(self, reading: LN2Reading):
        self._merge_history([reading])

    def _merge_history(self, readings: list) -> int:
        """Append only the readings whose timestamp isn't already present in
        the history CSV (rounded to milliseconds). Needed because _do_fetch()
        backfills up to BACKFILL_DAYS of history on every call — without
        dedup, the same weeks of readings would be re-appended twice a day
        forever. Returns the number of rows actually written."""
        if not readings:
            return 0
        existing = set()
        if os.path.isfile(self._history_path):
            try:
                with open(self._history_path, "r", newline="") as f:
                    for row in csv.reader(f):
                        if row and row[0] != "Unix_Timestamp":
                            existing.add(round(float(row[0]), 3))
            except Exception as e:
                log.warning(f"Could not read LN2 history for dedup: {e}")
        new_rows = [r for r in readings if round(r.timestamp, 3) not in existing]
        if not new_rows:
            return 0
        is_new_file = not os.path.isfile(self._history_path)
        try:
            with open(self._history_path, "a", newline="") as f:
                w = csv.writer(f)
                if is_new_file:
                    w.writerow(["Unix_Timestamp", "Level_In", "Timestamp_Str"])
                for r in sorted(new_rows, key=lambda x: x.timestamp):
                    w.writerow([r.timestamp, r.level_in,
                                datetime.fromtimestamp(r.timestamp).strftime("%Y-%m-%d %H:%M:%S")])
        except Exception as e:
            log.warning(f"Could not write LN2 history: {e}")
            return 0
        return len(new_rows)

    def history(self, hours: Optional[float] = None):
        """Return (t_list, level_list) of past readings, absolute Unix timestamps,
        newest last. hours=None returns full history."""
        t, lv = [], []
        if not os.path.isfile(self._history_path):
            return t, lv
        cutoff = time.time() - hours * 3600 if hours else None
        try:
            with open(self._history_path, "r", newline="") as f:
                for row in csv.reader(f):
                    if not row or row[0] == "Unix_Timestamp":
                        continue
                    ts = float(row[0])
                    if cutoff is not None and ts < cutoff:
                        continue
                    t.append(ts); lv.append(float(row[1]))
        except Exception as e:
            log.warning(f"Could not read LN2 history: {e}")
        return t, lv

    # ── Status helpers (safe to call from the Qt main thread — no I/O) ──
    def is_stale(self, stale_hours: float = STALE_HOURS) -> bool:
        if self.last_reading is None:
            return True
        return (time.time() - self.last_reading.timestamp) > stale_hours * 3600.0

    def is_low(self, low_in: float = LOW_LEVEL_IN) -> bool:
        return self.last_reading is not None and self.last_reading.level_in <= low_in

    def last_read_str(self) -> str:
        if self.last_reading is None:
            return "never"
        return datetime.fromtimestamp(self.last_reading.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def full_scale_for(self, reading: Optional[LN2Reading] = None,
                        default: float = FULL_SCALE_IN) -> float:
        """Prefer the tank's real full-scale value from the API (reading.raw
        ['fullValue'], confirmed in inches via fullUOM) over the FULL_SCALE_IN
        fallback constant. Falls back to `default` if unavailable."""
        reading = reading or self.last_reading
        if reading and reading.raw:
            fv = reading.raw.get("fullValue")
            uom = reading.raw.get("fullUOM")
            if fv and (not uom or str(uom).upper() == "IN"):
                try:
                    return float(fv)
                except (TypeError, ValueError):
                    pass
        return default

    # ── Fetch (blocking network I/O — call from a background thread) ───
    def fetch(self) -> Optional[LN2Reading]:
        """
        Perform one login + read cycle against APDirect. Returns an
        LN2Reading on success, None on any failure (network, login, parse).
        Never raises to the caller. Updates last_reading / last_attempt_ok /
        last_error, appends successful readings to the history CSV, and
        invokes on_reading(reading_or_None, ok) if set.
        """
        if not self._fetch_lock.acquire(blocking=False):
            log.info("LN2 fetch already in progress — skipping overlapping call")
            return None
        self.in_progress = True
        try:
            return self._fetch_locked()
        finally:
            self.in_progress = False
            self._fetch_lock.release()

    def _fetch_locked(self) -> Optional[LN2Reading]:
        self.last_attempt_time = time.time()

        if not self.credentials_configured():
            self.last_attempt_ok = False
            self.last_error = "No credentials configured — run tools/setup_ln2_credentials.py"
            log.warning(self.last_error)
            self._notify(None, False)
            return None

        try:
            username, password = self._load_credentials()
        except Exception as e:
            self.last_attempt_ok = False
            self.last_error = f"Credential decrypt failed: {e}"
            log.error(self.last_error)
            self._notify(None, False)
            return None

        try:
            reading = self._do_fetch(username, password)
        except Exception as e:
            self.last_attempt_ok = False
            self.last_error = str(e)
            log.warning(f"LN2 fetch failed: {e}")
            self._notify(None, False)
            return None
        finally:
            username = password = None  # best-effort: drop plaintext refs immediately

        self.last_attempt_ok = True
        self.last_error = None
        with self._lock:
            self.last_reading = reading
        self._append_history(reading)
        self._notify(reading, True)
        return reading

    def _notify(self, reading: Optional[LN2Reading], ok: bool):
        if self.on_reading:
            try:
                self.on_reading(reading, ok)
            except Exception as e:
                log.error(f"LN2 on_reading callback raised: {e}")

    def _do_fetch(self, username: str, password: str) -> LN2Reading:
        """
        Logs into APDirect and reads the current level for self.tank_id.
        See the module docstring ("STATUS OF THE SCRAPER ITSELF") for the
        confirmed request flow this replicates. Raises on any failure —
        fetch() is the only caller and always catches exceptions from here.
        """
        if not HAS_REQUESTS:
            raise RuntimeError("The 'requests' package is not installed (pip install requests)")

        sess = requests.Session()
        sess.headers.update({"User-Agent": _UA})

        # 1. GET the login page — establishes the anti-forgery cookie and gives us
        #    the matching __RequestVerificationToken hidden-input value.
        r = sess.get(LOGIN_URL, params={"ReturnUrl": RETURN_URL}, timeout=15)
        r.raise_for_status()
        token = _extract_verification_token(r.text)
        if not token:
            raise RuntimeError(
                "Could not find __RequestVerificationToken on the APDirect login "
                "page — its layout may have changed; re-capture a HAR to update this."
            )

        # 2. POST credentials. The real form posts both "Password" and "password"
        #    (confirmed in the HAR) — send both with the same value to match it exactly.
        form = {
            "username": username,
            "Password": password,
            "password": password,
            "__RequestVerificationToken": token,
        }
        r2 = sess.post(LOGIN_URL, params={"ReturnUrl": RETURN_URL}, data=form, timeout=15)
        r2.raise_for_status()
        if "account.airproducts.com/login" in r2.url.lower():
            raise RuntimeError("APDirect login failed — still on the login page after POST "
                                "(check credentials, or the site may have added a new login step)")

        # 3. ShipToList gives the current reading + true full-scale value directly.
        r3 = sess.get(SHIPTO_LIST_URL, timeout=15)
        r3.raise_for_status()
        ship_to_list = r3.json().get("d", [])
        asset, ship_to = _find_tank_asset(ship_to_list, self.tank_id)
        if asset is None:
            raise RuntimeError(f"Tank {self.tank_id} not found in APDirect ShipToList response "
                                f"({len(ship_to_list)} ship-tos returned) — check TANK_ID / account access")

        last = asset.get("lastReading") or {}
        level_in, dt_str = last.get("value"), last.get("dateTime")
        if level_in is None or dt_str is None:
            raise RuntimeError(f"Tank {self.tank_id} has no lastReading in the ShipToList response")
        reading = LN2Reading(
            timestamp=datetime.fromisoformat(dt_str).timestamp(),
            level_in=float(level_in),
            raw={"fullValue": asset.get("fullValue"), "fullUOM": asset.get("fullUOM")},
        )

        # 4. Best-effort history backfill (non-fatal — the current reading above is
        #    already good enough on its own if this step fails for any reason).
        try:
            channels = asset.get("logicalChannels") or []
            channel_id = channels[0].get("logicalChannelId") if channels else None
            if channel_id is not None:
                now = datetime.now()
                params = {
                    "logicalChannelId": channel_id,
                    "from": (now - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                    "to": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                    "companyKey": ship_to.get("companyKey", ""),
                    "countryCode": ship_to.get("countryCode", ""),
                    "shipToType": (ship_to.get("shipToType") or "").strip(),
                }
                r4 = sess.get(READINGS_URL, params=params, timeout=15)
                r4.raise_for_status()
                backfill = _parse_readings(r4.json().get("d", {}).get("readings", []))
                n = self._merge_history(backfill)
                if n:
                    log.info(f"LN2 history backfill: added {n} new row(s) from GetEquipmentReadings")
        except Exception as e:
            log.info(f"LN2 history backfill skipped (non-fatal): {e}")

        return reading

    # ── Scheduler (twice-daily automatic checks) ────────────────────────
    def start_scheduler(self):
        if self._sched_thread and self._sched_thread.is_alive():
            return
        self._stop_sched.clear()
        self._sched_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._sched_thread.start()

    def stop_scheduler(self):
        self._stop_sched.set()

    def _scheduler_loop(self):
        fired_today = set()   # (date, slot_index) already fired
        # Catch-up check on startup if we have nothing, or what we have is already stale —
        # otherwise a user opening the app mid-afternoon would stare at a stale reading
        # until the next scheduled slot.
        if self.last_reading is None or self.is_stale():
            self.fetch()
        while not self._stop_sched.is_set():
            now = datetime.now()
            for i, t in enumerate(CHECK_TIMES):
                slot_key = (now.date(), i)
                if now.time() >= t and slot_key not in fired_today:
                    fired_today.add(slot_key)
                    self.fetch()
            self._stop_sched.wait(60)
