"""
Recipe Engine — updated to support:
  SET_TEMP   param1 = "Se" | "Selenium" | "Substrate" | "In"  (or legacy float for CH1)
  OPEN_SHUTTER  param1 = "In" | "Se" | "Both"
  CLOSE_SHUTTER param1 = "In" | "Se" | "Both"
  (all original actions still work)
"""

import time, threading
from typing import List, Optional, Callable, Dict
from dataclasses import dataclass, field
import openpyxl


@dataclass
class RecipeStep:
    step: int
    action: str
    param1: Optional[object] = None   # float OR str  (channel name / source / value)
    param2: Optional[float]  = None   # °C target / loop count / pressure / tolerance
    param3: Optional[float]  = None   # ramp rate °C/min (RAMP_TEMP); unused by other actions
    notes: str = ""


class RecipeEngine:
    STATE_IDLE    = "IDLE"
    STATE_RUNNING = "RUNNING"
    STATE_PAUSED  = "PAUSED"
    STATE_DONE    = "DONE"
    STATE_ERROR   = "ERROR"

    def __init__(self):
        self.steps: List[RecipeStep] = []
        self.state = self.STATE_IDLE
        self.current_step_idx = 0
        self.brax = None; self.heat3 = None; self.shutter = None; self.indium = None
        self.on_step_change:  Optional[Callable[[int], None]] = None
        self.on_status_msg:   Optional[Callable[[str], None]] = None
        self.on_state_change: Optional[Callable[[str], None]] = None
        self._loop_counters: Dict[int, int] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop  = threading.Event()
        self._pause = threading.Event(); self._pause.set()

    # ── Loading ───────────────────────────────────────────────────────

    def load_excel(self, path: str) -> List[RecipeStep]:
        """Load recipe from Excel.

        Supports two column formats:
          Old (5-col): Step | Action | Param1 | Param2 | Notes
          New (6-col): Step | Action | Param1 | Param2 | Param3 (°C/min) | Notes
        Old files are detected automatically (no Param3 column → param3=None).
        """
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active; steps = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None: continue
            try:
                step_num = int(row[0])
                action   = str(row[1]).strip().upper() if row[1] else ""
                # param1: keep as string if it contains letters, else float
                raw_p1 = row[2]
                if raw_p1 is None or raw_p1 == "":
                    param1 = None
                elif isinstance(raw_p1, str) and any(c.isalpha() for c in raw_p1):
                    param1 = raw_p1.strip()
                else:
                    try: param1 = float(raw_p1)
                    except: param1 = str(raw_p1).strip()
                param2 = float(row[3]) if row[3] not in (None, "") else None
                # Detect 6-col (new) vs 5-col (old) format:
                #   New: row[4]=Param3 (rate), row[5]=Notes
                #   Old: row[4]=Notes, no Param3
                if len(row) > 5 and row[5] is not None:
                    # New 6-column format
                    try: param3 = float(row[4]) if row[4] not in (None, "") else None
                    except: param3 = None
                    notes = str(row[5]).strip() if row[5] else ""
                else:
                    # Old 5-column format — no ramp rate
                    param3 = None
                    notes  = str(row[4]).strip() if len(row) > 4 and row[4] else ""
                steps.append(RecipeStep(step_num, action, param1, param2, param3, notes))
            except (TypeError, ValueError): continue
        wb.close(); self.steps = steps; return steps

    # ── Control ───────────────────────────────────────────────────────

    def start(self):
        if self.state == self.STATE_RUNNING: return
        self._stop.clear(); self._pause.set(); self._loop_counters.clear()
        self.current_step_idx = 0; self._set_state(self.STATE_RUNNING)
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()

    def pause(self):
        if self.state == self.STATE_RUNNING:
            self._pause.clear(); self._set_state(self.STATE_PAUSED)

    def resume(self):
        if self.state == self.STATE_PAUSED:
            self._pause.set(); self._set_state(self.STATE_RUNNING)

    def stop(self):
        self._stop.set(); self._pause.set()
        if self._thread: self._thread.join(timeout=3)
        self._set_state(self.STATE_IDLE); self.current_step_idx = 0

    # ── Internals ─────────────────────────────────────────────────────

    def _set_state(self, s):
        self.state = s
        if self.on_state_change: self.on_state_change(s)

    def _log(self, m):
        if self.on_status_msg: self.on_status_msg(m)

    def _notify(self, i):
        self.current_step_idx = i
        if self.on_step_change: self.on_step_change(i)

    def _wait(self, secs: float) -> bool:
        end = time.time() + secs
        while time.time() < end:
            if self._stop.is_set(): return False
            self._pause.wait()
            if self._stop.is_set(): return False
            time.sleep(0.25)
        return True

    _goto_idx: int = 0

    def _run(self):
        idx = 0; n = len(self.steps)
        try:
            while idx < n:
                if self._stop.is_set(): break
                self._pause.wait()
                if self._stop.is_set(): break
                step = self.steps[idx]; self._notify(idx)
                self._log(f"Step {step.step}: {step.action}  {step.notes}")
                result = self._exec(step, idx)
                if result is None:   self._set_state(self.STATE_ERROR); return
                elif result == -1:   
                    idx = self._goto_idx
                elif result == 2:    
                    break  # END - exit loop cleanly → DONE below
                else:                
                    idx += 1
        except Exception as e:
            self._log(f"ERROR: {e}"); self._set_state(self.STATE_ERROR); return
        if not self._stop.is_set():
            self._log("Recipe complete."); self._set_state(self.STATE_DONE)

    @staticmethod
    def _is_indium_src(param) -> bool:
        """Return True when param refers to the Indium (EPC3004) source."""
        s = str(param or "").lower()
        return "indium" in s or "epc" in s

    @staticmethod
    def _src_to_ch(param) -> int:
        """Map source name to HEAT3 channel number. Se→1, Substrate/Sub→2."""
        s = str(param).lower()
        if any(x in s for x in ["se","selen","1"]): return 1
        if any(x in s for x in ["sub","substrate","2"]): return 2
        return 1  # default

    def _exec(self, step: RecipeStep, idx: int):
        a = step.action; p1 = step.param1; p2 = step.param2; p3 = step.param3

        # ── Shutter (legacy explicit) ────────────────────────────────
        if   a == "OPEN_IN":    self.shutter and self.shutter.in_open()
        elif a == "CLOSE_IN":   self.shutter and self.shutter.in_close()
        elif a == "OPEN_SE":    self.shutter and self.shutter.se_open()
        elif a == "CLOSE_SE":   self.shutter and self.shutter.se_close()
        elif a == "OPEN_BOTH":  self.shutter and self.shutter.both_open()
        elif a == "CLOSE_BOTH": self.shutter and self.shutter.both_close()

        # ── Unified shutter with source param ────────────────────────
        elif a == "OPEN_SHUTTER":
            src = str(p1).lower() if p1 else "in"
            if "both" in src:  self.shutter and self.shutter.both_open()
            elif "se" in src:  self.shutter and self.shutter.se_open()
            else:              self.shutter and self.shutter.in_open()
            self._log(f"  🔓 Shutter OPEN: {p1 or 'In'}")
        elif a == "CLOSE_SHUTTER":
            src = str(p1).lower() if p1 else "in"
            if "both" in src:  self.shutter and self.shutter.both_close()
            elif "se" in src:  self.shutter and self.shutter.se_close()
            else:              self.shutter and self.shutter.in_close()
            self._log(f"  🔒 Shutter CLOSE: {p1 or 'In'}")

        # ── Temperature ──────────────────────────────────────────────
        elif a == "SET_TEMP":
            if p1 is None: self._log("SET_TEMP: missing param"); return None
            # p1 can be a source name string or a legacy float (→ CH1)
            if isinstance(p1, str) and any(c.isalpha() for c in p1):
                if self._is_indium_src(p1):
                    # Indium source — uses EPC3004
                    temp = float(p2) if p2 is not None else None
                    if temp is None: self._log("SET_TEMP: need temp in Param2"); return None
                    if not self.indium:
                        self._log("  ⚠ SET_TEMP Indium: no EPC3004 device — skipping"); return 1
                    ok = self.indium.apply_setpoint(1, temp)["ok"]
                    self._log(f"  → Indium SP {'✓' if ok else '✗'} {temp}°C")
                    return 1
                ch = self._src_to_ch(p1)
                temp = float(p2) if p2 is not None else None
                if temp is None: self._log("SET_TEMP: need temp in Param2"); return None
            else:
                ch = 1; temp = float(p1)
            if self.heat3:
                ok = self.heat3.apply_setpoint(ch, temp)["ok"]
                self._log(f"  → CH{ch} setpoint {'✓' if ok else '✗'} {temp}°C")

        elif a == "WAIT_TEMP":
            # p1 = channel name (e.g. "Selenium (CH1)") OR legacy float tolerance
            # p2 = tolerance °C (default 2.5)
            # Condition: PV must reach SP then stay within ±tol for STABLE_SECS continuous seconds.
            STABLE_SECS = 15.0
            POLL_SECS   = 2.0

            if isinstance(p1, str) and any(c.isalpha() for c in p1) and self._is_indium_src(p1):
                # ── Indium source (EPC3004) ──────────────────────────────
                tol     = float(p2) if p2 is not None else 2.5
                ch_name = "Indium(EPC)"
                if not self.indium:
                    self._log(f"  ⚠ WAIT_TEMP: no EPC3004 device assigned — skipping step")
                    return 1
                sp_fn = self.indium.ch1_read_setpoint
                pv_fn = self.indium.ch1_read_process_value
            elif isinstance(p1, str) and any(c.isalpha() for c in p1):
                ch      = self._src_to_ch(p1)
                tol     = float(p2) if p2 is not None else 2.5
                ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"
                if not self.heat3:
                    self._log(f"  ⚠ WAIT_TEMP: no HEAT3 device assigned "
                              f"(enable Test Mode or connect HEAT3) — skipping step")
                    return 1
                sp_fn = self.heat3.ch1_read_setpoint      if ch == 1 else self.heat3.ch2_read_setpoint
                pv_fn = self.heat3.ch1_read_process_value if ch == 1 else self.heat3.ch2_read_process_value
            else:
                ch      = 1
                tol     = float(p1) if p1 is not None else 2.5
                ch_name = "CH1(Se)"
                if not self.heat3:
                    self._log(f"  ⚠ WAIT_TEMP: no HEAT3 device assigned — skipping step")
                    return 1
                sp_fn = self.heat3.ch1_read_setpoint
                pv_fn = self.heat3.ch1_read_process_value

            # Read initial state for diagnostics
            _sp0 = sp_fn(); _pv0 = pv_fn()
            if _sp0 is None: _sp0 = 0.0
            self._log(f"  WAIT_TEMP {ch_name}: SP={_sp0:.1f}°C  PV={_pv0:.1f}°C  "
                      f"tol=±{tol}°C  hold={int(STABLE_SECS)}s")
            if _pv0 is not None and abs(_pv0 - _sp0) > tol:
                self._log(f"  Waiting for {ch_name} to reach {_sp0:.1f}°C…")

            stable_since = None
            while True:
                if not self._wait(POLL_SECS): return None
                sp = sp_fn()
                pv = pv_fn()
                if sp is None: sp = 0.0
                if pv is None:
                    self._log(f"  {ch_name} PV read failed — retrying…"); continue
                in_range = abs(pv - sp) <= tol
                if in_range:
                    if stable_since is None:
                        stable_since = time.monotonic()
                        self._log(f"  {ch_name} reached {pv:.1f}°C "
                                  f"(SP={sp:.1f}°C ±{tol}) — holding for {int(STABLE_SECS)}s…")
                    elapsed = time.monotonic() - stable_since
                    remaining = STABLE_SECS - elapsed
                    if elapsed >= STABLE_SECS:
                        self._log(f"  ✓ {ch_name} stable at {pv:.1f}°C "
                                  f"(SP={sp:.1f}°C) for {int(elapsed)}s"); break
                    # Progress update every 6 seconds
                    if int(elapsed) % 6 == 0 and int(elapsed) > 0:
                        self._log(f"  {ch_name} holding… {pv:.1f}°C "
                                  f"({int(remaining)}s remaining)")
                else:
                    if stable_since is not None:
                        self._log(f"  {ch_name} left range: {pv:.1f}°C "
                                  f"(SP={sp:.1f}°C ±{tol}) — stability reset")
                    stable_since = None

        # ── Temperature ramp rate ────────────────────────────────────────
        elif a == "RAMP_TEMP":
            # Sets the device-internal ramp rate for the channel.
            # p1 = channel name (e.g. "Selenium (CH1)" or "Indium (EPC)"), p2 = rate °C/min
            if p1 is None: self._log("RAMP_TEMP: missing channel in Param1"); return None
            if p2 is None: self._log("RAMP_TEMP: missing rate °C/min in Param2"); return None
            rate = float(p2)
            if self._is_indium_src(p1):
                if not self.indium:
                    self._log("  ⚠ RAMP_TEMP Indium: no EPC3004 device — skipping"); return 1
                ok = self.indium.write_ramp_rate(1, rate)
                self._log(f"  RAMP_TEMP Indium(EPC): internal rate → {rate:.1f}°C/min {'✓' if ok else '✗'}")
            else:
                ch      = self._src_to_ch(p1)
                ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"
                if not self.heat3:
                    self._log(f"  ⚠ RAMP_TEMP: no HEAT3 device — skipping"); return 1
                ok = self.heat3.write_ramp_rate(ch, rate)
                self._log(f"  RAMP_TEMP {ch_name}: internal rate → {rate:.1f}°C/min {'✓' if ok else '✗'}")

        # ── Heater ───────────────────────────────────────────────────
        elif a == "HEATER_ON":
            if p1 and self._is_indium_src(p1):
                if self.indium:
                    res = self.indium.go_operate(1)
                    self._log(f"  {'✓' if res.get('ok') else '✗'} {res.get('message','In Heater ON')}")
                else:
                    self._log("  ⚠ HEATER_ON Indium: no EPC3004 device — skipping")
            else:
                ch = self._src_to_ch(p1) if p1 else 1
                if self.heat3:
                    res = self.heat3.go_operate(ch)
                    self._log(f"  {'✓' if res.get('ok') else '✗'} {res.get('message','Heater ON')}")
        elif a == "HEATER_OFF":
            if p1 and self._is_indium_src(p1):
                if self.indium:
                    res = self.indium.go_standby(1)
                    self._log(f"  {'✓' if res.get('ok') else '✗'} {res.get('message','In Heater OFF')}")
                else:
                    self._log("  ⚠ HEATER_OFF Indium: no EPC3004 device — skipping")
            else:
                ch = self._src_to_ch(p1) if p1 else 1
                if self.heat3:
                    res = self.heat3.go_standby(ch)
                    self._log(f"  {'✓' if res.get('ok') else '✗'} {res.get('message','Heater OFF')}")

        # ── IG ───────────────────────────────────────────────────────
        elif a == "IG_ON":
            if self.brax: self.brax.ig_on(); self._log("  IG → ON")
        elif a == "IG_OFF":
            if self.brax: self.brax.ig_off(); self._log("  IG → OFF")

        # ── Pressure wait ────────────────────────────────────────────
        elif a == "WAIT_PRESSURE":
            if p1 is None: self._log("WAIT_PRESSURE: missing param1"); return None
            self._log(f"  Waiting pressure ≤ {float(p1):.2e} Torr…")
            while True:
                if not self._wait(2.0): return None
                if self.brax:
                    p = self.brax.read_ion_gauge()
                    if p is not None and p <= float(p1):
                        self._log(f"  Pressure: {p:.2e} Torr"); break
                else: break

        # ── Time wait ────────────────────────────────────────────────
        elif a == "WAIT_TIME":
            if p1 is None: self._log("WAIT_TIME: missing param1"); return None
            self._log(f"  Waiting {float(p1):.1f} s…")
            if not self._wait(float(p1)): return None

        # ── GOTO ─────────────────────────────────────────────────────
        elif a == "GOTO":
            target = int(float(p1)) if p1 is not None else 1
            loops  = int(float(p2)) if p2 is not None else 1
            tidx = next((i for i,s in enumerate(self.steps) if s.step==target), None)
            if tidx is None: self._log(f"GOTO: step {target} not found"); return None
            if idx not in self._loop_counters: self._loop_counters[idx] = loops
            if self._loop_counters[idx] > 0:
                self._loop_counters[idx] -= 1
                self._log(f"  GOTO {target} ({self._loop_counters[idx]} left)")
                self._goto_idx = tidx; return -1
            else:
                del self._loop_counters[idx]

        elif a == "END":
            #self._set_state(self.STATE_IDLE)
            return 2  # clean end - let _run set DONE state
        else:
            self._log(f"  Unknown action: {a}, skipping")

        return 1
