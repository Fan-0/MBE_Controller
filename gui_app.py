"""
Vacuum Deposition Control GUI

Devices
-------
BRAX3400    Ion gauge controller (Modbus RTU, RS-485, COM11 default)
HEAT3       Dual-loop PID heater (Modbus RTU, RS-485, COM12 default)
              CH1 = Selenium source   CH2 = Substrate heater
              poll_all() → 3 batch FC 0x03 reads (faster than 8 individual)
              apply_setpoint() → 2 FC 0x06 writes + readback + log
              debug_callback → prints raw TX/RX hex to System Log when Debug ON
EPC3004     Eurotherm single-loop PID for Indium evaporation source
              Modbus RTU, RS-485, 19200 baud 8N1, COM13 default
              poll_all() → returns in_pv, in_sp, in_op, in_out
              apply_setpoint() / go_operate() / go_standby() / write_ramp_rate()
Arduino     Dual-shutter controller (serial, COM7 default)
              in_open/in_close/se_open/se_close/both_open/both_close

Test mode
---------
Toggling "🧪 Test" uses sim objects (BRAX3400Sim, HEAT3Sim, ArduinoShutterSim,
EPC3004Sim) in place of real hardware.  All sim devices expose an identical API
and use a _ChannelSim ramp engine for realistic temperature responses.

Snap / Poller
-------------
Poller thread (~0.25 s) reads all hardware → writes Snap dict with keys:
  ig, ig_on                         (BRAX)
  ch1_pv/sp/out/op, ch2_pv/sp/out/op (HEAT3)
  in_pv, in_sp, in_op, in_out       (EPC3004)
Main thread reads Snap every 100 ms in _vals() — zero serial I/O on GUI thread.
Poller also calls DataLogger.feed() on every cycle (single read path).

DataLogger / PlotWorker
-----------------------
DataLogger buffers pressure, CH1 Se, CH2 Sub, and In temperatures (deque,
20 000 pts each).  PlotWorker QThread pre-builds numpy arrays every 100 ms and
emits data_ready → _on_plot_data() updates three temperature curves (Or/PU/cyan)
and the pressure plot on the main thread.  Rolling window: 1 min … 1 hr.

Timers
------
_vals():        100 ms — reads Snap, updates all widgets + safety check
_sync_rec_log():100 ms — pushes RecipeLogger rows into DataLogger plot buffers
PlotWorker:     100 ms — numpy prep in background QThread

Safety
------
_check_safety() called every 100 ms.  Monitors CH1, CH2, and Indium (EPC3004):
  PV ≥ max_temp  → safety trip: go_standby + SP → standby_temp at standby_ramp
  PV ≥ warn_t    → warning log (throttled to once per 30 s)
  output > max   → output warning
Defaults: CH1 max=1000°C stby=50°C | CH2 max=700°C stby=50°C | In max=900°C stby=200°C
STANDBY button: Se→50°C @4°/min (operating) | Sub→standby | In→200°C @15°/min
E-STOP button:  all → standby, ramp→0, SP→0.  Latches; second click → reset dialog.

Recipe engine
-------------
Editable table; actions routed by param1 source string:
  SET_TEMP / RAMP_TEMP / WAIT_TEMP / HEATER_ON / HEATER_OFF:
    "Indium (EPC)" → EPC3004    "Selenium (CH1)" / "Substrate (CH2)" → HEAT3
  OPEN/CLOSE_SHUTTER param1 = In / Se / Both
  WAIT_TIME / WAIT_PRESSURE / GOTO / END
On Run: asks sample name, starts RecipeLogger → saves Excel on finish.
Preview plot shows estimated timeline with shutter regions + ramp markers.

BEP
---
Sequence: baseline (8 s IG average) → open shutter (2 s, 20 Hz sampling) →
BEP = mean(top 50% of open samples) − baseline.  In then Se, 30 s settle between.
In temperature: live from EPC3004.ch1_read_process_value() if connected, else
fixed fallback 744°C.  Fast Trace plot shows annotated baseline + open window.

RecipeLogger
------------
Logs pressure (0.5 s) and temperature (1 s) rows during a recipe run.
Temperature rows: (ts, elapsed, CH1_Se, CH2_Sub, In_C).
Exports to Excel: Run Info, Pressure, Temperature (with In_C column), BEP sheets.
"""

import sys, os, math, time, threading, types
import numpy as np
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QGroupBox, QComboBox, QFileDialog,
    QTableWidget, QTableWidgetItem, QTextEdit, QSplitter, QFrame,
    QSpinBox, QDoubleSpinBox, QSizePolicy, QProgressBar, QHeaderView,
    QTabWidget, QMessageBox, QDialog, QFormLayout, QDialogButtonBox,
    QInputDialog, QLineEdit, QCheckBox, QAbstractItemView,
)
from PyQt5.QtCore  import Qt, QTimer, QThread, QObject, pyqtSignal
from PyQt5.QtGui   import QFont, QColor, QPalette, QPainter, QPen, QBrush

try:
    import pyqtgraph as pg
    HAS_PG = True
except ImportError:
    HAS_PG = False

try:
    import serial.tools.list_ports as _lp
    def list_ports(): return sorted(p.device for p in _lp.comports())
except Exception:
    def list_ports(): return [f"COM{i}" for i in range(1,13)]

from devices        import BRAX3400, HEAT3, ArduinoShutter, EPC3004
from devices        import BRAX3400Sim, HEAT3Sim, ArduinoShutterSim, EPC3004Sim
from recipe_engine  import RecipeEngine
from recipe_logger  import RecipeLogger
from data_logger    import DataLogger
from bep_manager    import BEPManager, INDIUM_TEMP_C

# ── Paths ─────────────────────────────────────────────────────────────
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# ── Palette ───────────────────────────────────────────────────────────
BG="#1e1e2e"; PBG="#2a2a3e"; BDR="#44446a"
BL="#5e9cf3"; GR="#50fa7b"; RD="#ff5555"; OR="#ffb86c"; PU="#bd93f9"
FG="#cdd6f4"; DIM="#6c7086"; SIM_C="#ff9f1c"; IN_C="#8be9fd"  # cyan — Indium EPC3004

# Actions that get a param1 source dropdown
ACTION_CH_OPTS     = {
    "SET_TEMP":   ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "RAMP_TEMP":  ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "WAIT_TEMP":  ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "HEATER_ON":  ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
    "HEATER_OFF": ["Selenium (CH1)", "Substrate (CH2)", "Indium (EPC)"],
}
ACTION_SHUTTER_OPTS= {"OPEN_SHUTTER":["In","Se","Both"],"CLOSE_SHUTTER":["In","Se","Both"]}

ALL_ACTIONS = [
    "SET_TEMP","RAMP_TEMP","WAIT_TEMP","WAIT_PRESSURE","WAIT_TIME",
    "OPEN_SHUTTER","CLOSE_SHUTTER",
    "IG_ON","IG_OFF","HEATER_ON","HEATER_OFF","GOTO","END",
]

def apply_dark_theme(app):
    app.setStyle("Fusion"); p = QPalette()
    for role, c in [
        (QPalette.Window,BG),(QPalette.WindowText,FG),(QPalette.Base,PBG),
        (QPalette.AlternateBase,"#31314a"),(QPalette.Text,FG),(QPalette.Button,PBG),
        (QPalette.ButtonText,FG),(QPalette.Highlight,BL),(QPalette.HighlightedText,"#fff")]:
        p.setColor(role, QColor(c))
    app.setPalette(p)


# ── Small widgets ─────────────────────────────────────────────────────
class LED(QLabel):
    def __init__(self, on_c=GR, off_c=DIM, sz=14, parent=None):
        super().__init__(parent); self._on=False; self._oc=QColor(on_c); self._xc=QColor(off_c); self._sz=sz; self.setFixedSize(sz,sz)
    def set_on(self,v):
        if self._on!=v: self._on=v; self.update()
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); c=self._oc if self._on else self._xc
        if self._on:
            g=QColor(c); g.setAlpha(45); p.setBrush(QBrush(g)); p.setPen(Qt.NoPen); p.drawEllipse(0,0,self._sz,self._sz)
        r=3; p.setBrush(QBrush(c)); p.setPen(QPen(c.darker(160),1)); p.drawEllipse(r,r,self._sz-2*r,self._sz-2*r)

class PressBox(QFrame):
    def __init__(self,label,parent=None):
        super().__init__(parent); self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(f"QFrame{{background:{BG};border:1px solid {BDR};border-radius:5px;}}")
        v=QVBoxLayout(self); v.setContentsMargins(6,3,6,3); v.setSpacing(0)
        lbl=QLabel(label); lbl.setFont(QFont("Arial",8)); lbl.setStyleSheet(f"color:{DIM};border:none;"); lbl.setAlignment(Qt.AlignCenter)
        self._v=QLabel("---"); self._v.setFont(QFont("Courier New",14,QFont.Bold)); self._v.setStyleSheet(f"color:{BL};border:none;"); self._v.setAlignment(Qt.AlignCenter)
        self._e=QLabel(""); self._e.setFont(QFont("Arial",8)); self._e.setStyleSheet(f"color:{DIM};border:none;"); self._e.setAlignment(Qt.AlignCenter)
        v.addWidget(lbl); v.addWidget(self._v); v.addWidget(self._e)
    def set_value(self,val):
        if val is None: self._v.setText("N/A"); self._v.setStyleSheet(f"color:{DIM};border:none;"); self._e.setText(""); return
        if val>=1e3:    self._v.setText("OVER RNG"); self._v.setStyleSheet(f"color:{RD};border:none;"); self._e.setText(""); return
        exp=math.floor(math.log10(abs(val))) if val>0 else 0; m=val/(10**exp) if val>0 else 0
        self._v.setText(f"{m:.2f}×10"); self._v.setStyleSheet(f"color:{BL};border:none;")
        self._e.setText(f"{'⁺' if exp>=0 else '⁻'}{abs(exp)} Torr")

class ShBtn(QPushButton):
    def __init__(self,lbl,parent=None):
        super().__init__(lbl,parent); self._open=False; self.setFixedHeight(30); self._upd()
    def set_open(self,s):
        if self._open!=s: self._open=s; self._upd()
    def _upd(self):
        if self._open: self.setStyleSheet(f"QPushButton{{background:{GR};color:#000;border-radius:4px;font-weight:bold;}}QPushButton:hover{{background:#70ff9b;}}")
        else:          self.setStyleSheet(f"QPushButton{{background:{PBG};color:{FG};border:1px solid {BDR};border-radius:4px;}}QPushButton:hover{{background:#3a3a5e;}}")


class TempCh(QFrame):
    """
    Full channel panel: Current PV, SP readout, output %, state LED,
    NEW SP spinbox + Apply button, OPERATE / STANDBY buttons.
    """
    def __init__(self, name, ac, parent=None):
        super().__init__(parent); self._ac=ac; self.setFrameShape(QFrame.StyledPanel)
        self._pv_color = None   # tracks last applied pv color; None forces first paint
        self._op_state = None   # tracks last operating bool; None forces first paint
        self.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {ac};border-radius:7px;}}")
        root=QVBoxLayout(self); root.setContentsMargins(10,8,10,8); root.setSpacing(5)

        # Header
        hdr=QHBoxLayout()
        self.led=LED(on_c=ac,off_c=DIM,sz=14); self.st=QLabel("STANDBY"); self.st.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:bold;")
        nm=QLabel(name); nm.setStyleSheet(f"color:{ac};font-size:12px;font-weight:bold;")
        self.sim=QLabel("SIM"); self.sim.setStyleSheet(f"color:{SIM_C};font-size:9px;border:1px solid {SIM_C};border-radius:2px;padding:0 3px;"); self.sim.setVisible(False)
        hdr.addWidget(nm); hdr.addStretch(1); hdr.addWidget(self.sim); hdr.addWidget(self.led); hdr.addWidget(self.st)
        root.addLayout(hdr)

        # Value boxes
        vr=QHBoxLayout(); vr.setSpacing(6)
        def _box(t,c):
            b=QFrame(); b.setStyleSheet(f"QFrame{{background:{BG};border:1px solid {BDR};border-radius:4px;}}")
            bv=QVBoxLayout(b); bv.setContentsMargins(5,3,5,3); bv.setSpacing(0)
            tl=QLabel(t); tl.setFont(QFont("Arial",8)); tl.setStyleSheet(f"color:{DIM};border:none;"); tl.setAlignment(Qt.AlignCenter)
            vl=QLabel("---°C"); vl.setFont(QFont("Courier New",15,QFont.Bold)); vl.setStyleSheet(f"color:{c};border:none;"); vl.setAlignment(Qt.AlignCenter)
            bv.addWidget(tl); bv.addWidget(vl); return b,vl
        pb,self.pv=_box("Current",ac); sb,self.sp=_box("Setpoint",DIM)
        vr.addWidget(pb); vr.addWidget(sb); root.addLayout(vr)

        # Output
        or_=QHBoxLayout(); ol=QLabel("Output:"); ol.setStyleSheet(f"color:{DIM};font-size:10px;")
        self.ov=QLabel("0.0 %"); self.ov.setStyleSheet(f"color:{ac};font-size:10px;font-weight:bold;")
        or_.addWidget(ol); or_.addWidget(self.ov); or_.addStretch(1); root.addLayout(or_)

        div=QFrame(); div.setFrameShape(QFrame.HLine); div.setStyleSheet(f"color:{BDR};"); root.addWidget(div)

        # New SP + Apply & Dev Ramp — QGridLayout: col 1 (spinbox) expands to fill space
        # This guarantees buttons never overflow the frame regardless of panel width
        ctrl=QGridLayout(); ctrl.setSpacing(4); ctrl.setColumnStretch(1,1)

        sp_lbl=QLabel("New SP:"); sp_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
        self.sp_spin=QDoubleSpinBox(); self.sp_spin.setRange(0,1500); self.sp_spin.setSuffix(" °C")
        self.sp_spin.setDecimals(1); self.sp_spin.setValue(100.0); self.sp_spin.setMinimumWidth(55)
        self.ap_btn=QPushButton("Apply"); self.ap_btn.setFixedWidth(46)
        self.ap_btn.setStyleSheet(f"QPushButton{{background:{BL};color:#000;font-weight:bold;border-radius:4px;}}QPushButton:hover{{background:#7ab5ff;}}")
        self.ap_msg=QLabel(""); self.ap_msg.setStyleSheet(f"color:{DIM};font-size:9px;font-weight:bold;")
        self.ap_msg.setFixedWidth(14); self.ap_msg.setAlignment(Qt.AlignCenter)
        ctrl.addWidget(sp_lbl,0,0); ctrl.addWidget(self.sp_spin,0,1)
        ctrl.addWidget(self.ap_btn,0,2); ctrl.addWidget(self.ap_msg,0,3)

        rt_lbl=QLabel("Dev Ramp:"); rt_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
        self.rate_spin=QDoubleSpinBox(); self.rate_spin.setRange(0,1000); self.rate_spin.setSuffix(" °C/min")
        self.rate_spin.setDecimals(1); self.rate_spin.setValue(0.0); self.rate_spin.setMinimumWidth(55)
        self.rate_spin.setSpecialValueText("Off (instant)")
        self.rate_spin.setToolTip(
            "HEAT3 device-internal SP ramp rate (°C/min).\n"
            "Controls how fast the device transitions to any new setpoint.\n"
            "0 (Off) = device jumps to new SP instantly.\n"
            "Factory defaults: CH1(Se)=4, CH2(Sub)=100.\n"
            "Click 'Set' to write to device.")
        self.rr_btn=QPushButton("Set"); self.rr_btn.setFixedWidth(32)
        self.rr_btn.setStyleSheet(f"QPushButton{{background:{BL};color:#000;font-weight:bold;border-radius:4px;font-size:9px;}}QPushButton:hover{{background:#7ab5ff;}}")
        self.rr_msg=QLabel(""); self.rr_msg.setStyleSheet(f"color:{DIM};font-size:9px;font-weight:bold;")
        self.rr_msg.setFixedWidth(14); self.rr_msg.setAlignment(Qt.AlignCenter)
        ctrl.addWidget(rt_lbl,1,0); ctrl.addWidget(self.rate_spin,1,1)
        ctrl.addWidget(self.rr_btn,1,2); ctrl.addWidget(self.rr_msg,1,3)
        root.addLayout(ctrl)

        # Operate / Standby
        br=QHBoxLayout(); br.setSpacing(6)
        self.ob=QPushButton("OPERATE"); self.sb_=QPushButton("STANDBY")
        for b in [self.ob,self.sb_]: b.setFixedHeight(28); b.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self._set_standby_style()
        br.addWidget(self.ob); br.addWidget(self.sb_); root.addLayout(br)

    def _set_standby_style(self):
        self.ob.setStyleSheet(f"QPushButton{{background:{PBG};color:{DIM};border:1px solid {BDR};border-radius:4px;font-size:11px;}}QPushButton:hover{{background:#3a3a5e;}}")
        self.sb_.setStyleSheet(f"QPushButton{{background:{OR};color:#000;font-weight:bold;border-radius:4px;font-size:11px;}}QPushButton:hover{{background:#ffc966;}}")

    def update_vals(self, pv, sp, out, operating, simulated=False):
        # PV label — only repaint when color threshold changes (None/normal/hot)
        new_pv_c = DIM if pv is None else (RD if pv > 450 else self._ac)
        self.pv.setText("N/A" if pv is None else f"{pv:.1f}°C")
        if new_pv_c != self._pv_color:
            self.pv.setStyleSheet(f"color:{new_pv_c};border:none;")
            self._pv_color = new_pv_c

        # SP label — color is always DIM (set at init), just update text
        self.sp.setText("N/A" if sp is None else f"{sp:.1f}°C")

        if out is not None: self.ov.setText(f"{out:.1f} %")

        # Operating state — only repaint buttons/label when state actually changes
        self.led.set_on(bool(operating))  # LED already caches internally
        if operating != self._op_state:
            self._op_state = operating
            if operating:
                self.st.setText("OPERATE"); self.st.setStyleSheet(f"color:{self._ac};font-size:10px;font-weight:bold;")
                self.ob.setStyleSheet(f"QPushButton{{background:{self._ac};color:#000;border-radius:4px;font-weight:bold;font-size:11px;}}")
                self.sb_.setStyleSheet(f"QPushButton{{background:{PBG};color:{DIM};border:1px solid {BDR};border-radius:4px;font-size:11px;}}QPushButton:hover{{background:#3a3a5e;}}")
            else:
                self.st.setText("STANDBY"); self.st.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:bold;")
                self._set_standby_style()

        self.sim.setVisible(bool(simulated))

    def set_safety_state(self, state: str):
        """Update frame border to reflect safety status: 'ok', 'warn', or 'trip'."""
        if state == "trip":
            border = f"2px solid {RD}"
        elif state == "warn":
            border = f"2px solid {OR}"
        else:
            border = f"1px solid {self._ac}"
        self.setStyleSheet(f"QFrame{{background:{PBG};border:{border};border-radius:7px;}}")


# ── Settings dialog ───────────────────────────────────────────────────
class SettingsDlg(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent); self.setWindowTitle("Hardware Settings"); self.setMinimumWidth(400)
        v=QVBoxLayout(self); ps=list_ports() or [f"COM{i}" for i in range(1,9)]
        rfr=QPushButton("🔄 Refresh COM Ports"); rfr.clicked.connect(self._refresh); v.addWidget(rfr)
        f=QFormLayout()
        def _cb(cur):
            cb=QComboBox(); cb.setEditable(True); cb.addItems(ps)
            i=cb.findText(cur); cb.setCurrentIndex(i) if i>=0 else cb.setCurrentText(cur); return cb
        self.bp=_cb(cfg.get("brax_port","COM11")); self.ba=QSpinBox(); self.ba.setRange(0,255); self.ba.setValue(cfg.get("brax_addr",1))
        self.bb=QComboBox(); self.bb.addItems(["9600","19200","38400"]); self.bb.setCurrentText(str(cfg.get("brax_baud",19200)))
        self.hp=_cb(cfg.get("heat3_port","COM12")); self.hi=QSpinBox(); self.hi.setRange(1,247); self.hi.setValue(cfg.get("heat3_id",1))
        self.hb=QComboBox(); self.hb.addItems(["9600","19200","57600","115200"]); self.hb.setCurrentText(str(cfg.get("heat3_baud",19200)))
        self.ap=_cb(cfg.get("arduino_port","COM7"))
        self.ep=_cb(cfg.get("epc3004_port","COM13")); self.ei=QSpinBox(); self.ei.setRange(1,247); self.ei.setValue(cfg.get("epc3004_id",1))
        self.eb=QComboBox(); self.eb.addItems(["9600","19200","38400","57600"]); self.eb.setCurrentText(str(cfg.get("epc3004_baud",19200)))
        self.pi=QDoubleSpinBox(); self.pi.setRange(0.1,30); self.pi.setDecimals(2); self.pi.setSuffix(" s"); self.pi.setValue(cfg.get("poll_interval",0.25))
        self._cbs=[self.bp,self.hp,self.ap,self.ep]
        for lbl,w in [("BRAX Port:",self.bp),("BRAX Addr:",self.ba),("BRAX Baud:",self.bb),
                       ("HEAT3 Port:",self.hp),("HEAT3 Slave ID:",self.hi),("HEAT3 Baud:",self.hb),
                       ("Arduino Port:",self.ap),
                       ("EPC3004 Port:",self.ep),("EPC3004 Slave ID:",self.ei),("EPC3004 Baud:",self.eb),
                       ("Poll interval:",self.pi)]: f.addRow(lbl,w)
        v.addLayout(f); btn=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        btn.accepted.connect(self.accept); btn.rejected.connect(self.reject); v.addWidget(btn)
    def _refresh(self):
        ps=list_ports()
        for cb in self._cbs:
            cur=cb.currentText(); cb.clear(); cb.addItems(ps)
            i=cb.findText(cur); cb.setCurrentIndex(i) if i>=0 else cb.setCurrentText(cur)
    def get(self):
        return {"brax_port":self.bp.currentText(),"brax_addr":self.ba.value(),"brax_baud":int(self.bb.currentText()),
                "heat3_port":self.hp.currentText(),"heat3_id":self.hi.value(),"heat3_baud":int(self.hb.currentText()),
                "arduino_port":self.ap.currentText(),
                "epc3004_port":self.ep.currentText(),"epc3004_id":self.ei.value(),"epc3004_baud":int(self.eb.currentText()),
                "poll_interval":self.pi.value()}


# ── HW snapshot + poller ──────────────────────────────────────────────
import threading as _th
class Snap:
    def __init__(self): self._l=_th.Lock(); self._d={}
    def write(self,**kw):
        with self._l: self._d.update(kw)
    def read(self):
        with self._l: return dict(self._d)

class Poller:
    def __init__(self,snap,interval=1.0):
        self.snap=snap; self.interval=interval; self.brax=None; self.heat3=None; self.indium=None
        self.logger=None   # set by _resolve(); receives feed() calls each cycle
        self._stop=_th.Event(); self._t=None
    def start(self):
        if self._t and self._t.is_alive(): return
        self._stop.clear(); self._t=_th.Thread(target=self._loop,daemon=True,name="Poller"); self._t.start()
    def stop(self):
        self._stop.set()
        if self._t: self._t.join(timeout=3)
    def _loop(self):
        while not self._stop.is_set():
            t0=time.time(); d={}
            b=self.brax
            if b and getattr(b,"connected",False):
                try:    d["ig"]=b.read_ion_gauge()
                except: d["ig"]=None
                try:    d["ig_on"]=b.ig_status()
                except: d["ig_on"]=None
            else: d["ig"]=None; d["ig_on"]=None
            h=self.heat3
            if h and getattr(h,"connected",False):
                if hasattr(h,"poll_all"):
                    # Batch: 3 FC 0x03 reads instead of 8 individual ones (~4× faster)
                    try:    d.update(h.poll_all())
                    except:
                        for k in ["ch1_pv","ch1_sp","ch1_out","ch1_op","ch2_pv","ch2_sp","ch2_out","ch2_op"]: d[k]=None
                else:
                    for k,fn in [("ch1_pv",h.ch1_read_process_value),("ch1_sp",h.ch1_read_setpoint),
                                  ("ch1_out",h.ch1_read_output_pct),  ("ch1_op",h.ch1_is_operating),
                                  ("ch2_pv",h.ch2_read_process_value),("ch2_sp",h.ch2_read_setpoint),
                                  ("ch2_out",h.ch2_read_output_pct),  ("ch2_op",h.ch2_is_operating)]:
                        try:    d[k]=fn()
                        except: d[k]=None
            else:
                for k in ["ch1_pv","ch1_sp","ch1_out","ch1_op","ch2_pv","ch2_sp","ch2_out","ch2_op"]: d[k]=None
            # EPC3004 — Indium source temperature controller
            epc=self.indium
            if epc and getattr(epc,"connected",False):
                if hasattr(epc,"poll_all"):
                    try:    d.update(epc.poll_all())
                    except:
                        for k in ["in_pv","in_sp","in_op","in_out"]: d[k]=None
                else:
                    for k,fn in [("in_pv",epc.ch1_read_process_value),("in_sp",epc.ch1_read_setpoint),
                                  ("in_out",epc.ch1_read_output_pct),  ("in_op",epc.ch1_is_operating)]:
                        try:    d[k]=fn()
                        except: d[k]=None
            else:
                for k in ["in_pv","in_sp","in_op","in_out"]: d[k]=None
            self.snap.write(**d)
            # Feed DataLogger buffers (and CSV) with the data we just read.
            # This replaces DataLogger's old internal poll thread — one serial
            # read path, plot updates at Poller rate instead of 1 Hz.
            lg = self.logger
            if lg:
                lg.feed(d.get("ig"), d.get("ch1_pv"), d.get("ch2_pv"), d.get("in_pv"))
            self._stop.wait(max(0.0,self.interval-(time.time()-t0)))


# ── Plot worker ───────────────────────────────────────────────────────
class PlotWorker(QThread):
    """Background thread: reads DataLogger buffers, builds numpy arrays,
    emits data_ready so the main thread only has to call setData() — no
    heavy list comprehensions or deque iteration on the GUI thread.
    Polls every 100 ms; skips emission when data hasn't changed.
    Applies rolling window filter (window_s) before building arrays.
    """
    data_ready = pyqtSignal(dict)

    def __init__(self, logger):
        super().__init__()
        self._logger = logger
        self.window_s = 600   # seconds; set by MainWindow._set_plot_window (GIL-safe)
        self._last_p = None; self._last_t1 = None; self._last_t2 = None; self._last_ti = None

    def run(self):
        while not self.isInterruptionRequested():
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                pass
            elapsed = time.perf_counter() - t0
            rem_ms = max(0, int((0.10 - elapsed) * 1000))
            self.msleep(rem_ms)

    def _tick(self):
        # Always compute the rolling window bounds first — setXRange must advance
        # every tick even when no new data has arrived, otherwise the plot view
        # freezes and data appears to start "off-screen" until new points push it in.
        now    = time.time()
        cutoff = now - self.window_s
        d = {'x_min': cutoff, 'x_max': now}

        buf_p  = self._logger.pressure_buf
        buf_t1 = self._logger.ch1_temp_buf
        buf_t2 = self._logger.ch2_temp_buf
        buf_ti = self._logger.in_temp_buf
        last_p  = buf_p[-1]  if buf_p  else None
        last_t1 = buf_t1[-1] if buf_t1 else None
        last_t2 = buf_t2[-1] if buf_t2 else None
        last_ti = buf_ti[-1] if buf_ti else None
        if last_p == self._last_p and last_t1 == self._last_t1 and last_t2 == self._last_t2 and last_ti == self._last_ti:
            # No new data — emit only the x-range update so the rolling window advances
            self.data_ready.emit(d)
            return
        self._last_p = last_p; self._last_t1 = last_t1; self._last_t2 = last_t2; self._last_ti = last_ti

        # Pressure (log-scale: filter None/≤0, apply rolling window)
        tx, pv = self._logger.get_pressure_history()
        if tx:
            pairs = [(t, v) for t, v in zip(tx, pv) if t >= cutoff and v is not None and v > 0]
            if pairs:
                d['px'], d['py'] = (np.array(a, dtype=np.float64) for a in zip(*pairs))
            else:
                d['px'] = d['py'] = np.array([], dtype=np.float64)
        else:
            d['px'] = d['py'] = np.array([], dtype=np.float64)

        # Temperature CH1 (apply rolling window)
        t1x, t1v = self._logger.get_temp_history()
        if t1x:
            pairs = [(t, v) for t, v in zip(t1x, t1v) if t >= cutoff and v is not None]
            if pairs:
                d['t1x'], d['t1y'] = (np.array(a, dtype=np.float64) for a in zip(*pairs))
            else:
                d['t1x'] = d['t1y'] = np.array([], dtype=np.float64)
        else:
            d['t1x'] = d['t1y'] = np.array([], dtype=np.float64)

        # Temperature CH2 (apply rolling window)
        t2x, t2v = self._logger.get_ch2_temp_history()
        if t2x:
            pairs = [(t, v) for t, v in zip(t2x, t2v) if t >= cutoff and v is not None]
            if pairs:
                d['t2x'], d['t2y'] = (np.array(a, dtype=np.float64) for a in zip(*pairs))
            else:
                d['t2x'] = d['t2y'] = np.array([], dtype=np.float64)
        else:
            d['t2x'] = d['t2y'] = np.array([], dtype=np.float64)

        # Temperature Indium (apply rolling window)
        tix, tiv = self._logger.get_in_temp_history()
        if tix:
            pairs = [(t, v) for t, v in zip(tix, tiv) if t >= cutoff and v is not None]
            if pairs:
                d['tix'], d['tiy'] = (np.array(a, dtype=np.float64) for a in zip(*pairs))
            else:
                d['tix'] = d['tiy'] = np.array([], dtype=np.float64)
        else:
            d['tix'] = d['tiy'] = np.array([], dtype=np.float64)

        self.data_ready.emit(d)

    def stop(self):
        self.requestInterruption()
        self.wait(3000)


# ── Drag-and-drop recipe table ────────────────────────────────────────
class RecipeTable(QTableWidget):
    """QTableWidget that supports drag-and-drop row reordering.

    Qt's built-in InternalMove only moves QTableWidgetItems, not cell widgets
    (QComboBox etc.).  We intercept the drop at the lowest level, emit a
    row_moved signal, and let MainWindow rebuild the row through the normal
    _rtab_populate path so every ComboBox is recreated with correct row-index
    lambdas and no stale signal connections remain.
    """
    row_moved = pyqtSignal(int, int)   # (source_row, raw_dest before adjustment)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event):
        # Only accept drags that originated from this table
        if event.source() is not self:
            event.ignore()
            return

        src_rows = sorted({idx.row() for idx in self.selectedIndexes()})
        if len(src_rows) != 1:
            event.ignore()
            return
        src = src_rows[0]

        target = self.indexAt(event.pos())
        if target.isValid():
            dst = target.row()
            if self.dropIndicatorPosition() == QAbstractItemView.BelowItem:
                dst += 1
        else:
            dst = self.rowCount()   # dropped below all rows

        # Suppress Qt's built-in move (it would corrupt cell widgets)
        event.setDropAction(Qt.IgnoreAction)
        event.accept()

        if dst != src and dst != src + 1:
            self.row_moved.emit(src, dst)


# ── Recipe signal bridge ──────────────────────────────────────────────
class _RecipeBridge(QObject):
    """Thread-safe bridge from the recipe background thread to the GUI thread.
    QTimer.singleShot called from a plain threading.Thread (non-QThread) is
    unreliable on Windows PyQt5 because the background thread has no Qt event
    loop.  pyqtSignal queued-connections are guaranteed to deliver to the main
    thread regardless of which thread emits them. (fixes BUG 7 from context)"""
    step_change  = pyqtSignal(int)
    status_msg   = pyqtSignal(str)
    state_change = pyqtSignal(str)


# ── Main window ───────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    DCFG={"brax_port":"COM11","brax_addr":1,"brax_baud":19200,
          "heat3_port":"COM12","heat3_id":1,"heat3_baud":19200,
          "arduino_port":"COM7","poll_interval":0.25,
          "epc3004_port":"COM13","epc3004_id":1,"epc3004_baud":19200}

    def __init__(self):
        super().__init__(); self.cfg=dict(self.DCFG)
        self._br=None; self._h3=None; self._sh=None; self._in=None
        self._brs=BRAX3400Sim(); self._h3s=HEAT3Sim(); self._shs=ArduinoShutterSim(); self._ins=EPC3004Sim()
        self.brax=self._brs; self.heat3=self._h3s; self.shutter=self._shs; self.indium=self._ins
        self._test=False; self._debug=False
        self.safety = {
            "enabled":        True,
            "ch1_max_temp":   1000.0,  # °C — CH1 (Se) hard limit; trip → standby
            "ch1_warn_pct":   10.0,    # warn when PV within this % of max
            "ch1_max_output": 100.0,   # % output warning threshold
            "ch1_max_ramp":   100.0,   # °C/min — informational max ramp
            "ch1_standby_temp": 50.0,  # SP set when standby is triggered
            "ch1_standby_ramp": 10.0,  # °C/min ramp rate used during standby descent
            "ch2_max_temp":   700.0,
            "ch2_warn_pct":   10.0,
            "ch2_max_output": 100.0,
            "ch2_max_ramp":   100.0,
            "ch2_standby_temp": 50.0,
            "ch2_standby_ramp": 10.0,
            # Indium (EPC3004) safety limits
            "in_max_temp":    900.0,   # °C — EPC3004 Indium hard limit
            "in_warn_pct":    10.0,
            "in_max_output":  100.0,
            "in_max_ramp":    100.0,
            "in_standby_temp":  200.0, # hold at 200°C (above solidification point ~157°C)
            "in_standby_ramp":  15.0,  # °C/min
        }
        self._safety_warn_t  = {}    # key → last warning time (time.monotonic)
        self._safety_tripped = set() # channels currently safety-tripped
        self._safety_latch   = None  # None | "standby" | "estop" — buttons stay latched until reset
        self.recipe=RecipeEngine(); self.logger=DataLogger()
        self.rec_log=RecipeLogger()
        self._log_was_running=False
        self._active_recipe_row = -1   # tracks which row is visually highlighted
        self._sync_p_idx = 0           # last rec_log pressure row synced to plot buffers
        self._sync_t_idx = 0           # last rec_log temp row synced to plot buffers
        self.snap=Snap(); self.poller=Poller(self.snap, interval=self.cfg.get("poll_interval", 0.25))
        self._plot_worker = PlotWorker(self.logger) if HAS_PG else None
        if self._plot_worker: self._plot_worker.window_s = self._WIN_DEFAULT
        self.bep=BEPManager(); self.bep.load_history_from_csv()
        self._bep_ok=False
        self._build(); self._wire()
        self.poller.start(); self._timers()

    def _resolve(self):
        def _p(r,s): return r if (r and r.connected) else (s if self._test else None)
        self.brax=_p(self._br,self._brs); self.heat3=_p(self._h3,self._h3s)
        self.shutter=_p(self._sh,self._shs); self.indium=_p(self._in,self._ins)
        for obj in [self.poller,self.logger,self.recipe,self.bep,self.rec_log]:
            obj.brax=self.brax; obj.heat3=self.heat3
        for obj in [self.poller,self.recipe,self.bep,self.rec_log]:
            obj.indium=self.indium
        self.poller.logger = self.logger   # Poller feeds DataLogger on every cycle
        self.recipe.shutter=self.shutter; self.bep.shutter=self.shutter; self.rec_log.shutter=self.shutter
        # hook debug callback
        if self._h3:
            self._h3.debug_callback = (lambda m: QTimer.singleShot(0,lambda:self._log(f"<span style='color:#888'>DBG {m}</span>"))) if self._debug else None
        if self._h3s:
            self._h3s.debug_callback = None

    def _gs(self):
        return (f"QGroupBox{{background:{PBG};border:1px solid {BDR};border-radius:6px;"
                f"margin-top:12px;font-weight:bold;color:{FG};padding:4px;}}"
                f"QGroupBox::title{{subcontrol-origin:margin;left:10px;color:{BL};}}")

    # ── Build UI ──────────────────────────────────────────────────────
    def _build(self):
        self.setWindowTitle("Vacuum Deposition Control"); self.setMinimumSize(1380,880)
        mb=self.menuBar()
        fm=mb.addMenu("File"); fm.addAction("Settings…",self._settings); fm.addAction("Load Recipe…",self._load_recipe)
        fm.addAction("Export Firmware…",self._export_fw); fm.addSeparator(); fm.addAction("Quit",self.close)
        hm=mb.addMenu("Hardware"); hm.addAction("Connect All",self._conn_all); hm.addAction("Disconnect All",self._disc_all)
        hm.addSeparator(); hm.addAction("Connect BRAX",self._conn_brax); hm.addAction("Connect HEAT3",self._conn_h3); hm.addAction("Connect Arduino",self._conn_ard); hm.addAction("Connect EPC3004",self._conn_epc)
        c=QWidget(); self.setCentralWidget(c)
        root=QVBoxLayout(c); root.setContentsMargins(8,4,8,4); root.setSpacing(6)
        root.addWidget(self._topbar())
        spl=QSplitter(Qt.Horizontal); spl.addWidget(self._left()); spl.addWidget(self._right())
        spl.setSizes([460,940]); root.addWidget(spl); self.statusBar().showMessage("Ready")

    def _topbar(self):
        bar=QFrame(); bar.setStyleSheet(f"background:{PBG};border-radius:6px;border:1px solid {BDR};"); bar.setFixedHeight(46)
        h=QHBoxLayout(bar); h.setContentsMargins(12,4,12,4)
        def _ind(t):
            w=QWidget(); hl=QHBoxLayout(w); hl.setContentsMargins(0,0,0,0); hl.setSpacing(4)
            led=LED(sz=13); l=QLabel(t); l.setStyleSheet(f"color:{DIM};font-size:10px;")
            hl.addWidget(led); hl.addWidget(l); return w,led
        self.bl_w,self.bl=_ind("BRAX"); self.hl_w,self.hl=_ind("HEAT3"); self.sl_w,self.sl=_ind("Shutter"); self.il_w,self.il=_ind("EPC3004")
        self.sim_b=QLabel("⚠  TEST MODE"); self.sim_b.setStyleSheet(f"background:{SIM_C};color:#000;font-weight:bold;font-size:11px;border-radius:4px;padding:2px 8px;"); self.sim_b.setVisible(False)
        title=QLabel("⚗  Vacuum Deposition Control"); title.setStyleSheet(f"color:{FG};font-size:14px;font-weight:bold;")
        self.tb=QPushButton("🧪 Test: OFF"); self.tb.setCheckable(True); self.tb.setFixedWidth(115); self.tb.clicked.connect(self._toggle_test); self._style_tb(False)
        self.db=QPushButton("🐛 Debug: OFF"); self.db.setCheckable(True); self.db.setFixedWidth(120); self.db.clicked.connect(self._toggle_debug)
        self.db.setStyleSheet(f"QPushButton{{background:{PBG};color:{DIM};border:1px solid {BDR};border-radius:4px;font-size:10px;}}"); 
        ps=list_ports() or ["COM1","COM2","COM3","COM4","COM5"]; self._cbs={}
        for k in ["brax_port","heat3_port","arduino_port","epc3004_port"]:
            cb=QComboBox(); cb.setEditable(True); cb.setFixedWidth(78); cb.addItems(ps)
            i=cb.findText(self.cfg.get(k,"")); cb.setCurrentIndex(i) if i>=0 else cb.setCurrentText(self.cfg.get(k,""))
            cb.currentTextChanged.connect(lambda t,k2=k: self.cfg.__setitem__(k2,t)); self._cbs[k]=cb
        ca=QPushButton("Connect All"); ca.setFixedWidth(92); ca.clicked.connect(self._conn_all)
        ca.setStyleSheet(f"QPushButton{{background:{BL};color:#000;border-radius:4px;font-weight:bold;}}QPushButton:hover{{background:#7ab5ff;}}")
        sg=QPushButton("⚙"); sg.setFixedWidth(30); sg.setToolTip("Settings"); sg.clicked.connect(self._settings)
        # ── Safety buttons — centered in topbar ──────────────────────
        self.standby_btn = QPushButton("⏸  STANDBY")
        self.standby_btn.setFixedHeight(34); self.standby_btn.setMinimumWidth(118)
        self.standby_btn.setStyleSheet(
            f"QPushButton{{background:#b8860b;color:#fff;font-weight:bold;font-size:11px;"
            f"border-radius:5px;border:2px solid #ffd700;letter-spacing:0.5px;}}"
            f"QPushButton:hover{{background:#daa520;border-color:#ffe066;}}"
            f"QPushButton:disabled{{background:#443300;color:#666;border-color:#443300;}}"
        )
        self.standby_btn.setToolTip(
            "Soft standby preset:\n"
            "• Close all shutters\n"
            "• Se (CH1): ramp 4 °C/min → SP 50 °C  (stays OPERATING)\n"
            "• Sub (CH2): go STANDBY, ramp 100 °C/min\n"
            "• Indium (EPC3004): ramp 15 °C/min → SP 200 °C  (stays OPERATING)"
        )
        self.standby_btn.clicked.connect(self._standby_all_click)

        self.estop_btn = QPushButton("🛑  E-STOP")
        self.estop_btn.setFixedHeight(34); self.estop_btn.setMinimumWidth(118)
        self.estop_btn.setStyleSheet(
            f"QPushButton{{background:{RD};color:#fff;font-weight:bold;font-size:11px;"
            f"border-radius:5px;border:2px solid #ff8888;letter-spacing:0.5px;}}"
            f"QPushButton:hover{{background:#ff2222;border-color:#ffaaaa;}}"
            f"QPushButton:disabled{{background:#553333;color:#666;border-color:#553333;}}"
        )
        self.estop_btn.setToolTip(
            "EMERGENCY STOP — close shutters + immediate Standby + SP → 0°C (no ramp)\n"
            "Click again to clear latch and return to normal"
        )
        self.estop_btn.clicked.connect(self._estop_click)

        h.addWidget(title); h.addStretch(1)
        h.addWidget(self.standby_btn); h.addSpacing(6); h.addWidget(self.estop_btn)
        h.addStretch(1)
        h.addWidget(self.sim_b); h.addSpacing(4)
        for k in ["brax_port","heat3_port","arduino_port","epc3004_port"]: h.addWidget(self._cbs[k])
        h.addSpacing(4); h.addWidget(self.bl_w); h.addWidget(self.hl_w); h.addWidget(self.sl_w); h.addWidget(self.il_w)
        h.addSpacing(6); h.addWidget(self.db); h.addWidget(self.tb); h.addWidget(ca); h.addWidget(sg)
        return bar

    def _style_tb(self, on):
        if on: self.tb.setStyleSheet(f"QPushButton{{background:{SIM_C};color:#000;border-radius:4px;font-weight:bold;}}"); self.tb.setText("🧪 Test: ON")
        else:  self.tb.setStyleSheet(f"QPushButton{{background:{PBG};color:{DIM};border:1px solid {BDR};border-radius:4px;}}"); self.tb.setText("🧪 Test: OFF")

    def _left(self):
        w=QWidget(); w.setMinimumWidth(445); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(8)
        v.addWidget(self._pres_grp()); v.addWidget(self._heat3_grp()); v.addWidget(self._indium_grp()); v.addWidget(self._shutter_grp()); v.addStretch(1)
        return w

    def _pres_grp(self):
        g=QGroupBox("Ion Gauge Pressure"); g.setStyleSheet(self._gs()); gl=QHBoxLayout(g)
        self.ig_box=PressBox("IG"); self.ig_led=LED(on_c=BL,sz=13)
        self.ig_sim=QLabel("SIM"); self.ig_sim.setStyleSheet(f"color:{SIM_C};font-size:9px;"); self.ig_sim.setVisible(False)
        self.ig_on=QPushButton("IG ON"); self.ig_off=QPushButton("IG OFF")
        for b in [self.ig_on,self.ig_off]: b.setFixedHeight(24); b.setStyleSheet(f"QPushButton{{background:{PBG};color:{FG};border:1px solid {BDR};border-radius:3px;}}QPushButton:hover{{background:#3a3a5e;}}")
        self.ig_on.clicked.connect(lambda: threading.Thread(target=self.brax.ig_on,daemon=True).start() if self.brax else None)
        self.ig_off.clicked.connect(lambda: threading.Thread(target=self.brax.ig_off,daemon=True).start() if self.brax else None)
        gl.addWidget(self.ig_box); cr=QVBoxLayout(); cr.addWidget(self.ig_led); cr.addWidget(self.ig_sim); cr.addWidget(self.ig_on); cr.addWidget(self.ig_off); cr.addStretch(1); gl.addLayout(cr)
        return g

    def _heat3_grp(self):
        g=QGroupBox("HEAT3 Controllers"); g.setStyleSheet(self._gs()); h=QHBoxLayout(g); h.setSpacing(8)
        self.ch1=TempCh("CH1 — Selenium",OR); self.ch2=TempCh("CH2 — Substrate",PU)
        self.ch1.ob.clicked.connect(lambda: self._operate(1)); self.ch1.sb_.clicked.connect(lambda: self._standby(1))
        self.ch2.ob.clicked.connect(lambda: self._operate(2)); self.ch2.sb_.clicked.connect(lambda: self._standby(2))
        self.ch1.ap_btn.clicked.connect(lambda: self._apply_sp(1,self.ch1.sp_spin.value(),self.ch1.ap_msg))
        self.ch2.ap_btn.clicked.connect(lambda: self._apply_sp(2,self.ch2.sp_spin.value(),self.ch2.ap_msg))
        self.ch1.rr_btn.clicked.connect(lambda: self._apply_ramp(1,self.ch1.rate_spin.value(),self.ch1.rr_msg))
        self.ch2.rr_btn.clicked.connect(lambda: self._apply_ramp(2,self.ch2.rate_spin.value(),self.ch2.rr_msg))
        h.addWidget(self.ch1); h.addWidget(self.ch2)
        return g

    def _indium_grp(self):
        g=QGroupBox("EPC3004 — Indium Controller"); g.setStyleSheet(self._gs()); h=QHBoxLayout(g); h.setSpacing(8)
        self.ch_in=TempCh("Indium Source",IN_C)
        self.ch_in.ob.clicked.connect(self._operate_in)
        self.ch_in.sb_.clicked.connect(self._standby_in)
        self.ch_in.ap_btn.clicked.connect(lambda: self._apply_sp_in(self.ch_in.sp_spin.value(),self.ch_in.ap_msg))
        self.ch_in.rr_btn.clicked.connect(lambda: self._apply_ramp_in(self.ch_in.rate_spin.value(),self.ch_in.rr_msg))
        # Override tooltip for Dev Ramp to mention EPC3004
        self.ch_in.rate_spin.setToolTip(
            "EPC3004 device-internal SP ramp rate (°C/min).\n"
            "Controls how fast the controller transitions to any new setpoint.\n"
            "0 (Off) = jump to new SP instantly.\n"
            "Factory default: 4 °C/min.\n"
            "Click 'Set' to write to device.  Register address marked VERIFY —\n"
            "confirm with iTools before live hardware use.")
        h.addWidget(self.ch_in)
        return g

    def _shutter_grp(self):
        from PyQt5.QtWidgets import QGridLayout
        g=QGroupBox("Shutter Controls"); g.setStyleSheet(self._gs()); gl=QGridLayout(g); gl.setSpacing(6)
        self.in_b=ShBtn("IN  ●"); self.se_b=ShBtn("SE  ●")
        self.bo_b=QPushButton("BOTH OPEN"); self.bc_b=QPushButton("BOTH CLOSE")
        self.in_l=LED(on_c=GR); self.se_l=LED(on_c=PU)
        self.sh_sim=QLabel("SIM"); self.sh_sim.setStyleSheet(f"color:{SIM_C};font-size:9px;"); self.sh_sim.setVisible(False)
        self.in_b.clicked.connect(self._tog_in); self.se_b.clicked.connect(self._tog_se)
        self.bo_b.clicked.connect(lambda: threading.Thread(target=self.shutter.both_open,daemon=True).start() if self.shutter else None)
        self.bc_b.clicked.connect(lambda: threading.Thread(target=self.shutter.both_close,daemon=True).start() if self.shutter else None)
        self.bo_b.setStyleSheet(f"background:{GR};color:#000;font-weight:bold;border-radius:4px;")
        self.bc_b.setStyleSheet(f"background:{RD};color:#fff;font-weight:bold;border-radius:4px;")
        gl.addWidget(QLabel("In:"),0,0,Qt.AlignRight); gl.addWidget(self.in_l,0,1); gl.addWidget(self.in_b,0,2)
        gl.addWidget(QLabel("Se:"),1,0,Qt.AlignRight); gl.addWidget(self.se_l,1,1); gl.addWidget(self.se_b,1,2); gl.addWidget(self.sh_sim,1,3)
        gl.addWidget(self.bo_b,2,0,1,2); gl.addWidget(self.bc_b,2,2,1,2)
        return g

    def _right(self):
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(0,0,0,0); v.setSpacing(6)
        ts=f"QTabWidget::pane{{border:1px solid {BDR};background:{PBG};}} QTabBar::tab{{background:{BG};color:{DIM};padding:6px 14px;border:1px solid {BDR};border-bottom:none;border-radius:4px 4px 0 0;}} QTabBar::tab:selected{{background:{PBG};color:{FG};}}"
        tabs=QTabWidget(); tabs.setStyleSheet(ts)
        tabs.addTab(self._plot_tab(),"📈 Live Plot"); tabs.addTab(self._recipe_tab(),"📋 Recipe"); tabs.addTab(self._bep_tab(),"🔬 BEP"); tabs.addTab(self._safety_tab(),"⚠ Safety")
        v.addWidget(tabs)
        lg=QGroupBox("System Log"); lg.setStyleSheet(self._gs()); lg.setMaximumHeight(148); lv=QVBoxLayout(lg)
        self.log_w=QTextEdit(); self.log_w.setReadOnly(True)
        self.log_w.document().setMaximumBlockCount(800)
        self.log_w.setStyleSheet(f"QTextEdit{{background:{BG};color:{FG};font-family:'Courier New';font-size:11px;border:none;}}")
        lv.addWidget(self.log_w); v.addWidget(lg)
        return w

    # ── Plot tab ──────────────────────────────────────────────────────
    _WIN_OPTS = [("1 min", 60), ("10 min", 600), ("30 min", 1800), ("1 hr", 3600)]
    _WIN_DEFAULT = 600   # 10 min

    def _plot_tab(self):
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(4,4,4,4); v.setSpacing(4)

        # Top control row: logging + clear
        ctrl=QHBoxLayout()
        self.log_btn=QPushButton("▶ Start Logging"); self.log_btn.setStyleSheet(f"background:{GR};color:#000;font-weight:bold;border-radius:4px;padding:4px 10px;"); self.log_btn.clicked.connect(self._tog_log)
        self.log_lbl=QLabel("No log"); self.log_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
        clr=QPushButton("Clear"); clr.setStyleSheet(f"QPushButton{{background:{PBG};color:{FG};border:1px solid {BDR};border-radius:4px;padding:3px 8px;}}"); clr.clicked.connect(self._clear_plot)
        ctrl.addWidget(self.log_btn); ctrl.addWidget(self.log_lbl); ctrl.addStretch(1); ctrl.addWidget(clr)
        v.addLayout(ctrl)

        # Window selector row
        win_row=QHBoxLayout(); win_row.setSpacing(4)
        win_lbl=QLabel("Window:"); win_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
        win_row.addWidget(win_lbl)
        _btn_style=(
            f"QPushButton{{background:{PBG};color:{DIM};border:1px solid {BDR};"
            f"border-radius:4px;font-size:10px;padding:2px 8px;}}"
            f"QPushButton:hover{{background:#3a3a5e;}}"
            f"QPushButton:checked{{background:{BL};color:#000;font-weight:bold;border:none;}}"
        )
        self._win_btns=[]
        for lbl,secs in self._WIN_OPTS:
            b=QPushButton(lbl); b.setCheckable(True); b.setChecked(secs==self._WIN_DEFAULT)
            b.setStyleSheet(_btn_style)
            b.clicked.connect(lambda _,s=secs: self._set_plot_window(s))
            self._win_btns.append((b,secs)); win_row.addWidget(b)
        win_row.addStretch(1)
        v.addLayout(win_row)

        if HAS_PG:
            pg.setConfigOption("background",BG); pg.setConfigOption("foreground",FG)
            # Use DateAxisItem so x-axis shows real wall-clock time (HH:MM:SS)
            self.pp=pg.PlotWidget(title="Ion Gauge Pressure",
                                   axisItems={'bottom': pg.DateAxisItem()})
            self.pp.setLogMode(y=True); self.pp.showGrid(x=True,y=True,alpha=0.3)
            self.pp.setLabel("left","Pressure",units="Torr")
            self.pc=self.pp.plot(pen=pg.mkPen(BL,width=2))

            self.tp=pg.PlotWidget(title="Temperature",
                                   axisItems={'bottom': pg.DateAxisItem()})
            self.tp.showGrid(x=True,y=True,alpha=0.3)
            self.tp.setLabel("left","Temp",units="°C")
            self.tc1=self.tp.plot(pen=pg.mkPen(OR,width=2),name="Se")
            self.tc2=self.tp.plot(pen=pg.mkPen(PU,width=2),name="Sub")
            self.tc3=self.tp.plot(pen=pg.mkPen(IN_C,width=2),name="In")
            v.addWidget(self.pp,stretch=3); v.addWidget(self.tp,stretch=2)
        else:
            v.addWidget(QLabel("pip install pyqtgraph"))
        return w

    def _set_plot_window(self, secs):
        """Switch the rolling window length and update button styles."""
        for b,s in self._win_btns:
            b.setChecked(s==secs)
        if self._plot_worker:
            self._plot_worker.window_s = secs

    # ── BEP tab ───────────────────────────────────────────────────────
    def _bep_tab(self):
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(8,8,8,8); v.setSpacing(8)
        ctrl=QHBoxLayout()
        self.bep_btn=QPushButton("🔬 Measure BEP"); self.bep_btn.setStyleSheet(f"QPushButton{{background:{BL};color:#000;font-weight:bold;border-radius:5px;padding:6px 18px;font-size:13px;}}QPushButton:hover{{background:#7ab5ff;}}"); self.bep_btn.clicked.connect(self._bep_start)
        self.bep_ab=QPushButton("⏹ Abort"); self.bep_ab.setStyleSheet(f"QPushButton{{background:{RD};color:#fff;font-weight:bold;border-radius:5px;padding:6px 12px;}}"); self.bep_ab.setEnabled(False); self.bep_ab.clicked.connect(self.bep.abort)
        ctrl.addWidget(self.bep_btn); ctrl.addWidget(self.bep_ab); ctrl.addStretch(1); v.addLayout(ctrl)
        sr=QHBoxLayout(); self.bep_st=QLabel("IDLE"); self.bep_st.setStyleSheet(f"color:{BL};font-weight:bold;")
        self.bep_pr=QProgressBar(); self.bep_pr.setRange(0,100); self.bep_pr.setValue(0); self.bep_pr.setFixedHeight(13)
        self.bep_pr.setStyleSheet(f"QProgressBar{{background:{BG};border:1px solid {BDR};border-radius:3px;}} QProgressBar::chunk{{background:{BL};border-radius:3px;}}")
        sr.addWidget(self.bep_st); sr.addWidget(self.bep_pr,1); v.addLayout(sr)
        res=QFrame(); res.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:6px;padding:4px;}}"); rv=QHBoxLayout(res); rv.setSpacing(16)
        def _big(t,c): vb=QVBoxLayout(); tl=QLabel(t); tl.setStyleSheet(f"color:{DIM};font-size:10px;"); vl=QLabel("---"); vl.setStyleSheet(f"color:{c};font-size:18px;font-weight:bold;"); vl.setAlignment(Qt.AlignCenter); vb.addWidget(tl); vb.addWidget(vl); return vb,vl
        iv,self.bep_iv=_big("In BEP (Torr)",OR); sv,self.bep_sv=_big("Se BEP (Torr)",PU); rv2,self.bep_rat=_big("Se/In Ratio",GR)
        for vbox in [iv,sv,rv2]: rv.addLayout(vbox); v.addWidget(res)
        tl=QHBoxLayout(); self.bep_itl=QLabel(f"In: {INDIUM_TEMP_C:.0f}°C (fixed)"); self.bep_itl.setStyleSheet(f"color:{DIM};font-size:10px;"); self.bep_stl=QLabel("Se: ---°C"); self.bep_stl.setStyleSheet(f"color:{DIM};font-size:10px;")
        tl.addWidget(self.bep_itl); tl.addWidget(self.bep_stl); tl.addStretch(1); v.addLayout(tl)
        if HAS_PG:
            ht=QTabWidget()
            ht.setStyleSheet(f"QTabWidget::pane{{border:1px solid {BDR};background:{PBG};}} QTabBar::tab{{background:{BG};color:{DIM};padding:4px 10px;border:1px solid {BDR};border-bottom:none;border-radius:3px 3px 0 0;}} QTabBar::tab:selected{{background:{PBG};color:{FG};}}")
            self.bep_ip=pg.PlotWidget(title="Indium BEP History"); self.bep_ip.setLogMode(y=True); self.bep_ip.showGrid(x=True,y=True,alpha=0.3); self.bep_ip.setLabel("left","BEP",units="Torr"); self.bep_ip.setLabel("bottom","Temp",units="°C"); self.bep_ic=self.bep_ip.plot(pen=pg.mkPen(OR,width=1),symbol="o",symbolBrush=OR,symbolSize=8)
            self.bep_sp_=pg.PlotWidget(title="Selenium BEP History"); self.bep_sp_.setLogMode(y=True); self.bep_sp_.showGrid(x=True,y=True,alpha=0.3); self.bep_sp_.setLabel("left","BEP",units="Torr"); self.bep_sp_.setLabel("bottom","Temp",units="°C"); self.bep_sc=self.bep_sp_.plot(pen=pg.mkPen(PU,width=1),symbol="s",symbolBrush=PU,symbolSize=8)
            self.bep_fp=pg.PlotWidget(title="Fast Trace — Baseline + Open Window"); self.bep_fp.setLogMode(y=True); self.bep_fp.showGrid(x=True,y=True,alpha=0.3); self.bep_fp.setLabel("left","Pressure",units="Torr"); self.bep_fp.setLabel("bottom","Time from baseline start",units="s")
            leg=self.bep_fp.addLegend(offset=(10,10))
            # Main trace lines (all samples: baseline + open window)
            self._bft_in  =self.bep_fp.plot(pen=pg.mkPen(OR,width=2),name="In samples")
            self._bft_se  =self.bep_fp.plot(pen=pg.mkPen(PU,width=2),name="Se samples")
            # Top-fraction scatter (samples used in BEP mean — highlighted)
            self._bft_in_top=self.bep_fp.plot(pen=None,symbol="o",symbolSize=7,symbolBrush=pg.mkColor(OR),symbolPen=pg.mkPen("w",width=1),name="In top range")
            self._bft_se_top=self.bep_fp.plot(pen=None,symbol="s",symbolSize=7,symbolBrush=pg.mkColor(PU),symbolPen=pg.mkPen("w",width=1),name="Se top range")
            # Dashed baseline reference lines
            self._bft_in_bl =self.bep_fp.plot(pen=pg.mkPen(OR,width=1,style=Qt.DashLine))
            self._bft_se_bl =self.bep_fp.plot(pen=pg.mkPen(PU,width=1,style=Qt.DashLine))
            # Solid BEP-level lines (baseline + BEP = accepted flux level)
            self._bft_in_bep=self.bep_fp.plot(pen=pg.mkPen(OR,width=2))
            self._bft_se_bep=self.bep_fp.plot(pen=pg.mkPen(PU,width=2))
            # Region items for open windows — created/replaced in _bep_plots
            self._bft_in_rgn=None; self._bft_se_rgn=None
            ht.addTab(self.bep_ip,"📊 In History"); ht.addTab(self.bep_sp_,"📊 Se History"); ht.addTab(self.bep_fp,"⚡ Fast Trace")
            v.addWidget(ht,stretch=3)
        return w

    # ── Recipe tab ────────────────────────────────────────────────────
    def _recipe_tab(self):
        w=QWidget(); v=QVBoxLayout(w); v.setContentsMargins(4,4,4,4); v.setSpacing(5)
        tb=QHBoxLayout()
        self.rl=QPushButton("📂 Load"); self.rs=QPushButton("💾 Save"); self.ra=QPushButton("+ Row"); self.rd=QPushButton("✕ Del")
        for b in [self.rl,self.rs,self.ra,self.rd]: b.setStyleSheet(f"QPushButton{{background:{PBG};color:{FG};border:1px solid {BDR};border-radius:4px;padding:3px 8px;}}QPushButton:hover{{background:#3a3a5e;}}")
        self.rl.clicked.connect(self._load_recipe); self.rs.clicked.connect(self._save_recipe)
        self.ra.clicked.connect(lambda: self._rtab_add()); self.rd.clicked.connect(self._rtab_del)
        self.rf=QLabel("No file"); self.rf.setStyleSheet(f"color:{DIM};font-size:10px;")
        self.rrun=QPushButton("▶ Run"); self.rpau=QPushButton("⏸"); self.rsto=QPushButton("⏹")
        for b,c in [(self.rrun,GR),(self.rpau,OR),(self.rsto,RD)]: b.setFixedWidth(52); b.setStyleSheet(f"background:{c};color:#000;font-weight:bold;border-radius:4px;"); b.setEnabled(False)
        self.rrun.clicked.connect(self._r_run); self.rpau.clicked.connect(self.recipe.pause); self.rsto.clicked.connect(self._r_stop)
        sl=QHBoxLayout(); self.rst=QLabel("IDLE"); self.rst.setStyleSheet(f"color:{BL};font-weight:bold;"); self.rsk=QLabel("—"); self.rsk.setStyleSheet(f"color:{FG};font-weight:bold;")
        sl.addWidget(QLabel("State:")); sl.addWidget(self.rst); sl.addStretch(1); sl.addWidget(QLabel("Step:")); sl.addWidget(self.rsk)
        tb.addWidget(self.rl); tb.addWidget(self.rs); tb.addWidget(self.rf); tb.addStretch(1)
        tb.addWidget(self.ra); tb.addWidget(self.rd); tb.addSpacing(8); tb.addWidget(self.rrun); tb.addWidget(self.rpau); tb.addWidget(self.rsto)
        v.addLayout(tb); v.addLayout(sl)

        # ── Collapsible column guide ──────────────────────────────────
        self._help_visible = False
        help_toggle = QPushButton("▶  Column Guide")
        help_toggle.setCheckable(False)
        help_toggle.setStyleSheet(
            f"QPushButton{{background:transparent;color:{DIM};border:none;"
            f"font-size:10px;text-align:left;padding:1px 4px;}}"
            f"QPushButton:hover{{color:{FG};}}"
        )
        self._help_frame = QFrame()
        self._help_frame.setVisible(False)
        self._help_frame.setStyleSheet(
            f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:4px;}}"
        )
        hfl = QVBoxLayout(self._help_frame); hfl.setContentsMargins(8,6,8,6)
        help_html = (
            "<table style='font-size:11px;color:#cdd6f4;border-collapse:collapse;'>"
            "<tr style='color:#bd93f9;font-weight:bold;'>"
            "<td style='padding:0 12px 4px 0'>Column</td>"
            "<td style='padding:0 12px 4px 0'>Role</td>"
            "<td style='padding:0 0 4px 0'>Examples / notes</td></tr>"
            "<tr><td><b>Step</b></td><td>Row number (auto-renumbered on reorder)</td><td>1, 2, 3 …</td></tr>"
            "<tr style='background:#1e1e2e;'><td><b>Action</b></td><td>Command to execute</td>"
            "<td>SET_TEMP, WAIT_TEMP, RAMP_TEMP, OPEN_SHUTTER, CLOSE_SHUTTER,<br>"
            "HEATER_ON/OFF, IG_ON/OFF, WAIT_TIME, WAIT_PRESSURE, GOTO, END</td></tr>"
            "<tr><td><b>Param 1</b></td><td>Primary argument (source / channel / value)</td>"
            "<td>SET_TEMP → <i>Se</i> or <i>Sub</i>&nbsp;&nbsp;OPEN_SHUTTER → <i>In / Se / Both</i><br>"
            "WAIT_TIME → seconds&nbsp;&nbsp;GOTO → target step number</td></tr>"
            "<tr style='background:#1e1e2e;'><td><b>Param 2</b></td><td>Secondary value (temperature / rate / loop count)</td>"
            "<td>SET_TEMP → setpoint °C&nbsp;&nbsp;WAIT_TEMP → tolerance °C (default ±2.5)<br>"
            "WAIT_PRESSURE → Torr threshold&nbsp;&nbsp;GOTO → extra loops&nbsp;&nbsp;"
            "<b>RAMP_TEMP → °C/min</b> (sets device internal ramp rate)</td></tr>"
            "<tr style='background:#1e1e2e;'><td><b>Notes</b></td><td>Free text — logged during execution</td><td></td></tr>"
            "</table>"
        )
        help_lbl = QLabel(help_html); help_lbl.setTextFormat(Qt.RichText)
        help_lbl.setWordWrap(True)
        hfl.addWidget(help_lbl)
        self._help_toggle_btn = help_toggle

        def _toggle_help():
            self._help_visible = not self._help_visible
            self._help_frame.setVisible(self._help_visible)
            help_toggle.setText("▼  Column Guide" if self._help_visible else "▶  Column Guide")

        help_toggle.clicked.connect(_toggle_help)
        v.addWidget(help_toggle)
        v.addWidget(self._help_frame)

        self.rtab=RecipeTable(); self.rtab.setColumnCount(5)
        self.rtab.setHorizontalHeaderLabels(["Step","Action","Param 1  (source/value)","Param 2  (°C / rate / loops)","Notes"])
        self.rtab.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.rtab.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.rtab.horizontalHeader().setSectionResizeMode(2,QHeaderView.Interactive)
        self.rtab.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch)
        self.rtab.setColumnWidth(2,180); self.rtab.setColumnWidth(3,110)
        self.rtab.setSelectionBehavior(QTableWidget.SelectRows)
        # Vertical header acts as the drag handle — show it slim with a grip icon hint
        vh = self.rtab.verticalHeader()
        vh.setDefaultSectionSize(24)
        vh.setFixedWidth(18)
        vh.setStyleSheet(
            f"QHeaderView::section{{background:{PBG};color:{DIM};border:none;"
            f"border-right:1px solid {BDR};font-size:9px;padding:0;}}"
        )
        self.rtab.setStyleSheet(
            f"QTableWidget{{background:{BG};color:{FG};gridline-color:{BDR};font-size:12px;}}"
            f" QTableWidget::item{{background:{BG};color:{FG};}}"
            f" QTableWidget::item:selected{{background:#3d2e00;color:#f1fa8c;}}"
            f" QHeaderView::section{{background:{PBG};color:{DIM};border:1px solid {BDR};padding:4px;}}"
        )
        self.rtab.row_moved.connect(self._on_row_moved)
        v.addWidget(self.rtab, stretch=3)

        # ── Recipe Preview Plot ───────────────────────────────────────
        self._rp_plot = None; self._rp_items = []
        if HAS_PG:
            rp_frame = QFrame()
            rp_frame.setStyleSheet(f"QFrame{{background:{PBG};border:1px solid {BDR};border-radius:4px;}}")
            rp_fl = QVBoxLayout(rp_frame); rp_fl.setContentsMargins(4,2,4,4); rp_fl.setSpacing(2)
            rp_lbl = QLabel("Recipe Preview  (estimated timeline — RAMP_TEMP sets device rate, SET_TEMP moves to target)")
            rp_lbl.setStyleSheet(f"color:{DIM};font-size:10px;")
            rp_fl.addWidget(rp_lbl)
            self._rp_plot = pg.PlotWidget(background=BG)
            self._rp_plot.showGrid(x=True, y=True, alpha=0.2)
            self._rp_plot.setLabel("left","Temp",units="°C")
            self._rp_plot.setLabel("bottom","Est. Time",units="s")
            self._rp_plot.setMaximumHeight(210)
            self._rp_plot.addLegend(offset=(10,10))
            self._rp_se  = self._rp_plot.plot(pen=pg.mkPen(OR,width=2),name="Se (CH1)")
            self._rp_sub = self._rp_plot.plot(pen=pg.mkPen(PU,width=2),name="Sub (CH2)")
            rp_fl.addWidget(self._rp_plot)
            v.addWidget(rp_frame, stretch=2)

        # Debounce timer: table edits → preview refresh after 400 ms silence
        self._rp_timer = QTimer(); self._rp_timer.setSingleShot(True); self._rp_timer.setInterval(400)
        self._rp_timer.timeout.connect(self._refresh_recipe_preview)
        self.rtab.itemChanged.connect(lambda _=None: self._rp_timer.start())
        return w

    # ── Recipe table helpers ──────────────────────────────────────────
    def _action_cb_style(self):
        return f"QComboBox{{background:{PBG};color:{FG};border:1px solid {BDR};border-radius:3px;padding:1px 4px;}}"

    def _make_action_cb(self, action="WAIT_TIME"):
        cb=QComboBox(); cb.addItems(ALL_ACTIONS)
        i=cb.findText(action); cb.setCurrentIndex(i if i>=0 else 0)
        cb.setStyleSheet(self._action_cb_style())
        return cb

    def _make_param1_widget(self, action: str, value=""):
        """Return a QComboBox for actions with source options, else None (use text item)."""
        if action in ACTION_CH_OPTS:
            cb=QComboBox(); cb.addItems(ACTION_CH_OPTS[action]); cb.setStyleSheet(self._action_cb_style())
            i=cb.findText(str(value)); cb.setCurrentIndex(i if i>=0 else 0)
            return cb
        if action in ACTION_SHUTTER_OPTS:
            cb=QComboBox(); cb.addItems(ACTION_SHUTTER_OPTS[action]); cb.setStyleSheet(self._action_cb_style())
            i=cb.findText(str(value)); cb.setCurrentIndex(i if i>=0 else 0)
            return cb
        return None  # use plain text item

    def _rtab_add(self, step=None, action="WAIT_TIME", p1="", p2="", notes=""):
        r=self.rtab.rowCount(); self.rtab.insertRow(r)
        self.rtab.setItem(r,0,QTableWidgetItem(str(step or r+1)))
        # Action combobox
        acb=self._make_action_cb(action)
        acb.currentTextChanged.connect(lambda t,row=r: self._on_action_changed(row,t))
        acb.currentTextChanged.connect(lambda _=None: self._rp_timer.start())
        self.rtab.setCellWidget(r,1,acb)
        # Param1: widget or text
        w1=self._make_param1_widget(action,p1)
        if w1:
            self.rtab.setCellWidget(r,2,w1)
            w1.currentTextChanged.connect(lambda _=None: self._rp_timer.start())
        else:  self.rtab.setItem(r,2,QTableWidgetItem("" if p1 is None else str(p1)))
        self.rtab.setItem(r,3,QTableWidgetItem("" if p2 is None else str(p2)))
        self.rtab.setItem(r,4,QTableWidgetItem(str(notes) if notes else ""))
        self.rrun.setEnabled(True)

    def _on_action_changed(self, row, action):
        # Rebuild param1 cell widget for the new action
        old_val=""
        old_w=self.rtab.cellWidget(row,2)
        if old_w and hasattr(old_w,"currentText"): old_val=old_w.currentText()
        elif self.rtab.item(row,2): old_val=self.rtab.item(row,2).text()
        w1=self._make_param1_widget(action,old_val)
        if w1:
            self.rtab.setCellWidget(row,2,w1); self.rtab.setItem(row,2,None)
            w1.currentTextChanged.connect(lambda _=None: self._rp_timer.start())
        else:
            self.rtab.removeCellWidget(row,2); self.rtab.setItem(row,2,QTableWidgetItem(old_val if not any(c.isalpha() for c in old_val) else ""))
        self._rp_timer.start()

    def _rtab_del(self):
        rows=sorted({i.row() for i in self.rtab.selectedItems()},reverse=True)
        for r in rows: self.rtab.removeRow(r)
        for r in range(self.rtab.rowCount()): self.rtab.setItem(r,0,QTableWidgetItem(str(r+1)))

    def _on_row_moved(self, src: int, dst: int):
        """Handle drag-and-drop row reorder.

        Strategy: extract all step data → reorder the Python list → repopulate
        the table from scratch.  This is simpler and more reliable than trying
        to move individual items and cell widgets, and it automatically rewires
        all action-ComboBox signal lambdas with correct row indices.
        """
        steps = self._rtab_to_steps()
        if src < 0 or src >= len(steps):
            return

        # After removing src the list shrinks by 1, so insertion index adjusts
        # only when dst > src (because all indices ≥ src shift down by 1).
        insert_at = dst if src >= dst else dst - 1
        insert_at = max(0, min(insert_at, len(steps) - 1))

        step = steps.pop(src)
        steps.insert(insert_at, step)

        # Renumber steps sequentially so Step column stays tidy
        for i, s in enumerate(steps):
            s.step = i + 1

        self._rtab_populate(steps)
        self.rtab.selectRow(insert_at)   # keep visual focus on the moved row

    def _rtab_populate(self, steps):
        self.rtab.setRowCount(0)
        for s in steps:
            self._rtab_add(s.step, s.action,
                           s.param1 if s.param1 is not None else "",
                           s.param2 if s.param2 is not None else "",
                           s.notes)
        self._rp_timer.start()

    def _rtab_to_steps(self):
        out=[]
        for r in range(self.rtab.rowCount()):
            acb=self.rtab.cellWidget(r,1); act=acb.currentText() if acb else ""
            def _g(c): w=self.rtab.cellWidget(r,c); return w.currentText() if (w and hasattr(w,"currentText")) else (self.rtab.item(r,c).text().strip() if self.rtab.item(r,c) else "")
            p1s=_g(2); p2s=_g(3)
            if p1s == "None": p1s = ""
            if p2s == "None": p2s = ""
            p1=p1s if (p1s and any(c.isalpha() for c in p1s)) else (float(p1s) if p1s else None)
            try: p2=float(p2s) if p2s else None
            except: p2=p2s if p2s else None
            out.append(types.SimpleNamespace(step=r+1,action=act,param1=p1,param2=p2,param3=None,notes=_g(4)))
        return out

    def _save_recipe(self):
        try: import openpyxl
        except ImportError: QMessageBox.critical(self,"Error","pip install openpyxl"); return
        path,_=QFileDialog.getSaveFileName(self,"Save Recipe","recipe.xlsx","Excel Files (*.xlsx)")
        if not path: return
        wb=openpyxl.Workbook(); ws=wb.active; ws.title="Recipe"; ws.append(["Step","Action","Param1","Param2","Notes"])
        for s in self._rtab_to_steps(): ws.append([s.step,s.action,s.param1,s.param2,s.notes])
        wb.save(path); self.rf.setText(os.path.basename(path)); self._log(f"Recipe saved → {path}")

    # ── Wire callbacks ────────────────────────────────────────────────
    def _wire(self):
        self.bep.on_status  =lambda m: QTimer.singleShot(0,lambda:self._log(m))
        self.bep.on_state   =lambda s: QTimer.singleShot(0,lambda:self._bep_state(s))
        self.bep.on_progress=lambda p: QTimer.singleShot(0,lambda:self.bep_pr.setValue(int(p*100)))
        self.bep.on_complete=lambda: QTimer.singleShot(0,self._bep_done)
        # Recipe: use pyqtSignal bridge so callbacks are guaranteed to execute on the
        # main thread regardless of which thread emits them.  QTimer.singleShot from
        # a plain threading.Thread (no Qt event loop) is unreliable on Windows PyQt5.
        self._rb = _RecipeBridge()
        self._rb.step_change.connect(self._r_step)
        self._rb.status_msg.connect(self._log)
        self._rb.state_change.connect(self._r_state)
        self.recipe.on_status_msg   = lambda m: self._rb.status_msg.emit(m)
        self.recipe.on_step_change  = lambda i: self._rb.step_change.emit(i)
        self.recipe.on_state_change = lambda s: self._rb.state_change.emit(s)
        self.logger.on_data=lambda p,t: None
        if self._plot_worker:
            self._plot_worker.data_ready.connect(self._on_plot_data)
            self._plot_worker.start()

    # ── Timers ────────────────────────────────────────────────────────
    def _timers(self):
        self._vt=QTimer(); self._vt.setInterval(100); self._vt.timeout.connect(self._vals); self._vt.start()
        if HAS_PG:
            # Syncs rec_log rows into DataLogger buffers (fast, main thread only)
            self._pt=QTimer(); self._pt.setInterval(100); self._pt.timeout.connect(self._sync_rec_log); self._pt.start()

    def _vals(self):
        bo=self._br is not None and self._br.connected; ho=self._h3 is not None and self._h3.connected; ao=self._sh is not None and self._sh.connected
        io=self._in is not None and self._in.connected
        self.bl.set_on(bo); self.hl.set_on(ho); self.sl.set_on(ao); self.il.set_on(io)
        self.sim_b.setVisible(self._test)
        self.ig_sim.setVisible(self._test and not bo); self.sh_sim.setVisible(self._test and not ao)
        sh_h=self._test and not ho; self.ch1.sim.setVisible(sh_h); self.ch2.sim.setVisible(sh_h)
        sh_in=self._test and not io; self.ch_in.sim.setVisible(sh_in)
        sh=self.shutter
        if sh:
            self.in_l.set_on(sh.in_is_open); self.se_l.set_on(sh.se_is_open)
            self.in_b.set_open(sh.in_is_open); self.se_b.set_open(sh.se_is_open)
        d=self.snap.read()
        self.ig_box.set_value(d.get("ig")); self.ig_led.set_on(bool(d.get("ig_on",False)))
        self.ch1.update_vals(d.get("ch1_pv"),d.get("ch1_sp"),d.get("ch1_out"),d.get("ch1_op"),sh_h)
        self.ch2.update_vals(d.get("ch2_pv"),d.get("ch2_sp"),d.get("ch2_out"),d.get("ch2_op"),sh_h)
        self.ch_in.update_vals(d.get("in_pv"),d.get("in_sp"),d.get("in_out"),d.get("in_op"),sh_in)
        st=d.get("ch1_pv"); self.bep_stl.setText(f"Se: {st:.1f}°C" if st else "Se: ---°C")
        in_pv=d.get("in_pv")
        if in_pv is not None:
            self.bep_itl.setText(f"In: {in_pv:.1f}°C (live)")
        else:
            self.bep_itl.setText(f"In: {INDIUM_TEMP_C:.0f}°C (fixed)")
        self._check_safety(d)

    def _sync_rec_log(self):
        """Fast main-thread timer (100 ms): push NEW rec_log rows into DataLogger
        buffers so the PlotWorker picks them up on its next tick.
        Uses _sync_p_idx / _sync_t_idx to track how far we've synced, so each
        row is appended exactly once (previously re-appended every 100 ms → 10× duplication)."""
        if not self.rec_log.running:
            # Reset indices when run ends so next run starts clean
            self._sync_p_idx = 0; self._sync_t_idx = 0
            return
        rows_p = self.rec_log._rows_pressure
        rows_t = self.rec_log._rows_temp
        now = time.time()

        # Pressure: append only rows we haven't synced yet
        new_p = rows_p[self._sync_p_idx:]
        if new_p:
            # base maps the most recent recorded elapsed time to wall-clock "now"
            last_elapsed = rows_p[-1][1] or 0
            base = now - last_elapsed
            for row in new_p:
                if row[2] is not None:
                    self.logger.pressure_buf.append((base + row[1], row[2]))
            self._sync_p_idx += len(new_p)

        # Temperature: append only rows we haven't synced yet
        new_t = rows_t[self._sync_t_idx:]
        if new_t:
            last_elapsed = rows_t[-1][1] or 0
            base = now - last_elapsed
            for row in new_t:
                t = base + row[1]
                if row[2] is not None: self.logger.ch1_temp_buf.append((t, row[2]))
                if row[3] is not None: self.logger.ch2_temp_buf.append((t, row[3]))
                if len(row) > 4 and row[4] is not None: self.logger.in_temp_buf.append((t, row[4]))
            self._sync_t_idx += len(new_t)

    def _on_plot_data(self, d):
        """Slot called on the main thread with pre-built numpy arrays from PlotWorker."""
        if not HAS_PG: return
        if len(d.get('px', ())): self.pc.setData(d['px'], d['py'])
        if len(d.get('t1x', ())): self.tc1.setData(d['t1x'], d['t1y'])
        if len(d.get('t2x', ())): self.tc2.setData(d['t2x'], d['t2y'])
        if len(d.get('tix', ())): self.tc3.setData(d['tix'], d['tiy'])
        # Lock the X axis to the rolling window so the view always shows now-window..now
        x_min, x_max = d.get('x_min'), d.get('x_max')
        if x_min and x_max:
            self.pp.setXRange(x_min, x_max, padding=0)
            self.tp.setXRange(x_min, x_max, padding=0)

    # ── Log ───────────────────────────────────────────────────────────
    def _log(self, msg):
        ts=time.strftime("%H:%M:%S")
        self.log_w.append(f"<span style='color:{DIM}'>[{ts}]</span> {msg}")
        self.log_w.verticalScrollBar().setValue(self.log_w.verticalScrollBar().maximum())
        self.statusBar().showMessage(msg, 5000)

    # ── Mode toggles ──────────────────────────────────────────────────
    def _toggle_test(self):
        self._test=self.tb.isChecked(); self._style_tb(self._test); self._resolve()
        self._log("🧪 TEST MODE ON" if self._test else "TEST MODE OFF")
        if self._test:
            if not self.logger.running: self._tog_log()
            self._read_device_ramp_rates()

    def _toggle_debug(self):
        self._debug=self.db.isChecked()
        if self._debug:
            self.db.setText("🐛 Debug: ON"); self.db.setStyleSheet(f"QPushButton{{background:{OR};color:#000;border-radius:4px;font-size:10px;font-weight:bold;}}")
        else:
            self.db.setText("🐛 Debug: OFF"); self.db.setStyleSheet(f"QPushButton{{background:{PBG};color:{DIM};border:1px solid {BDR};border-radius:4px;font-size:10px;}}")
        self._resolve()  # re-applies debug_callback to heat3
        self._log(f"Debug mode {'ON — HEAT3 TX/RX will be shown below' if self._debug else 'OFF'}")

    # ── Connections ───────────────────────────────────────────────────
    def _conn_all(self):
        for k,cb in self._cbs.items(): self.cfg[k]=cb.currentText()
        self._conn_brax(); self._conn_h3(); self._conn_ard(); self._conn_epc()
    def _disc_all(self):
        for d in [self._br,self._h3,self._sh,self._in]:
            try:
                if d: d.disconnect()
            except: pass
        self._resolve(); self._log("All disconnected")
    def _conn_brax(self):
        p=self._cbs["brax_port"].currentText()
        try:
            if self._br and self._br.connected: self._br.disconnect()
            self._br=BRAX3400(p,self.cfg["brax_addr"],self.cfg["brax_baud"]); self._br.connect(); self._resolve(); self._log(f"✓ BRAX on {p}")
        except Exception as e: self._log(f"✗ BRAX: {e}"); self._resolve()
    def _conn_h3(self):
        p=self._cbs["heat3_port"].currentText()
        try:
            if self._h3 and self._h3.connected: self._h3.disconnect()
            self._h3=HEAT3(p,self.cfg["heat3_id"],self.cfg["heat3_baud"]); self._h3.connect(); self._resolve(); self._log(f"✓ HEAT3 on {p}")
            self._read_device_ramp_rates()
        except Exception as e: self._log(f"✗ HEAT3: {e}"); self._resolve()

    def _read_device_ramp_rates(self):
        """Read HEAT3 and EPC3004 internal ramp rates and populate the Dev Ramp spinboxes."""
        h3  = self.heat3;  epc = self.indium
        has_h3  = h3  and hasattr(h3,  "read_ramp_rate")
        has_epc = epc and hasattr(epc, "read_ramp_rate")
        if not has_h3 and not has_epc: return
        def _do():
            r1 = h3.read_ramp_rate(1)  if has_h3  else None
            r2 = h3.read_ramp_rate(2)  if has_h3  else None
            ri = epc.read_ramp_rate(1) if has_epc else None
            def _ui():
                if r1 is not None: self.ch1.rate_spin.setValue(r1)
                if r2 is not None: self.ch2.rate_spin.setValue(r2)
                if ri is not None: self.ch_in.rate_spin.setValue(ri)
            QTimer.singleShot(0, _ui)
        threading.Thread(target=_do, daemon=True).start()
    def _conn_ard(self):
        p=self._cbs["arduino_port"].currentText()
        try:
            if self._sh and self._sh.connected: self._sh.disconnect()
            self._sh=ArduinoShutter(p); self._sh.connect(); self._resolve(); self._log(f"✓ Arduino on {p}")
        except Exception as e: self._log(f"✗ Arduino: {e}"); self._resolve()

    def _conn_epc(self):
        p=self._cbs["epc3004_port"].currentText()
        try:
            if self._in and self._in.connected: self._in.disconnect()
            self._in=EPC3004(p,self.cfg["epc3004_id"],self.cfg["epc3004_baud"],j_proxy=True); self._in.connect()
            self._resolve(); self._log(f"✓ EPC3004 on {p}")
            self._read_device_ramp_rates()
        except Exception as e: self._log(f"✗ EPC3004: {e}"); self._resolve()

    # ── HEAT3 controls ────────────────────────────────────────────────
    def _apply_sp(self, ch: int, val: float, msg_lbl: QLabel):
        """Apply setpoint instantly to HEAT3 channel ch.
        The device will ramp to the new SP at its internal ramp rate
        (configured via the 'Dev Ramp' / 'Set' controls).
        """
        if not self.heat3: self._log("HEAT3 not connected"); return
        msg_lbl.setText("…"); msg_lbl.setStyleSheet(f"color:{DIM};font-size:9px;")

        def _do():
            res = self.heat3.apply_setpoint(ch, val)
            def _ui():
                col = GR if res["ok"] else RD
                msg_lbl.setText("✓" if res["ok"] else "✗")
                msg_lbl.setStyleSheet(f"color:{col};font-size:9px;font-weight:bold;")
                self._log(res["message"])
            QTimer.singleShot(0, _ui)

        threading.Thread(target=_do, daemon=True).start()

    def _apply_ramp(self, ch: int, rate: float, msg_lbl: QLabel):
        """Write the HEAT3 device-internal SP ramp rate for channel ch."""
        if not self.heat3: self._log("HEAT3 not connected"); return
        if not hasattr(self.heat3, "write_ramp_rate"):
            self._log("Device does not support ramp rate write"); return
        msg_lbl.setText("…"); msg_lbl.setStyleSheet(f"color:{DIM};font-size:9px;")
        ch_name = "CH1(Se)" if ch == 1 else "CH2(Sub)"

        def _do():
            ok = self.heat3.write_ramp_rate(ch, rate)
            def _ui():
                col = GR if ok else RD
                msg_lbl.setText("✓" if ok else "✗")
                msg_lbl.setStyleSheet(f"color:{col};font-size:9px;font-weight:bold;")
                rate_str = f"{rate:.1f}°C/min" if rate > 0 else "Off (instant)"
                self._log(f"{ch_name} dev ramp → {rate_str} {'✓' if ok else '✗'}")
            QTimer.singleShot(0, _ui)

        threading.Thread(target=_do, daemon=True).start()

    def _operate(self, ch):
        if not self.heat3: self._log("HEAT3 not connected"); return
        def _do():
            res=self.heat3.go_operate(ch); QTimer.singleShot(0,lambda:self._log(res["message"]))
        threading.Thread(target=_do,daemon=True).start()

    def _standby(self, ch):
        if not self.heat3: self._log("HEAT3 not connected"); return
        def _do():
            res=self.heat3.go_standby(ch); QTimer.singleShot(0,lambda:self._log(res["message"]))
        threading.Thread(target=_do,daemon=True).start()

    # ── EPC3004 (Indium) controls ─────────────────────────────────────
    def _operate_in(self):
        if not self.indium: self._log("EPC3004 not connected"); return
        def _do():
            res=self.indium.go_operate(1); QTimer.singleShot(0,lambda:self._log(res["message"]))
        threading.Thread(target=_do,daemon=True).start()

    def _standby_in(self):
        if not self.indium: self._log("EPC3004 not connected"); return
        def _do():
            res=self.indium.go_standby(1); QTimer.singleShot(0,lambda:self._log(res["message"]))
        threading.Thread(target=_do,daemon=True).start()

    def _apply_sp_in(self, val: float, msg_lbl: QLabel):
        if not self.indium: self._log("EPC3004 not connected"); return
        msg_lbl.setText("…"); msg_lbl.setStyleSheet(f"color:{DIM};font-size:9px;")
        def _do():
            res=self.indium.apply_setpoint(1,val)
            def _ui():
                col=GR if res["ok"] else RD
                msg_lbl.setText("✓" if res["ok"] else "✗")
                msg_lbl.setStyleSheet(f"color:{col};font-size:9px;font-weight:bold;")
                self._log(res["message"])
            QTimer.singleShot(0,_ui)
        threading.Thread(target=_do,daemon=True).start()

    def _apply_ramp_in(self, rate: float, msg_lbl: QLabel):
        if not self.indium: self._log("EPC3004 not connected"); return
        if not hasattr(self.indium,"write_ramp_rate"):
            self._log("EPC3004 does not support ramp rate write"); return
        msg_lbl.setText("…"); msg_lbl.setStyleSheet(f"color:{DIM};font-size:9px;")
        def _do():
            ok=self.indium.write_ramp_rate(1,rate)
            def _ui():
                col=GR if ok else RD
                msg_lbl.setText("✓" if ok else "✗")
                msg_lbl.setStyleSheet(f"color:{col};font-size:9px;font-weight:bold;")
                rate_str=f"{rate:.1f}°C/min" if rate>0 else "Off (instant)"
                self._log(f"In dev ramp → {rate_str} {'✓' if ok else '✗'}")
            QTimer.singleShot(0,_ui)
        threading.Thread(target=_do,daemon=True).start()

    def _safe_standby_in(self):
        """Safety-triggered standby for Indium: go_standby + set standby SP/ramp."""
        def _do():
            epc=self.indium
            if not epc: return
            sbt=self.safety.get("in_standby_temp",200.0)
            sbr=self.safety.get("in_standby_ramp",15.0)
            try: epc.go_standby(1)
            except: pass
            try:
                if hasattr(epc,"write_ramp_rate"): epc.write_ramp_rate(1,sbr)
                epc.apply_setpoint(1,sbt)
            except: pass
        threading.Thread(target=_do,daemon=True).start()

    def _standby_all_click(self):
        """Soft standby preset — no latch, no reset required.
        • Close shutters
        • Se (CH1): ramp 4 °C/min, SP → 50 °C, stays OPERATING
        • Sub (CH2): go STANDBY, ramp 100 °C/min
        """
        self.standby_btn.setEnabled(False)
        self._log(
            f"<span style='color:#ffd700;font-weight:bold'>⏸ STANDBY — "
            f"shutters closed | Se→50°C @4°/min (operating) | Sub→standby @100°/min | In→200°C @15°/min</span>"
        )
        def _do():
            sh = self.shutter
            if sh:
                try: sh.both_close()
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"Shutter close error: {e}"))
            h3 = self.heat3
            if h3:
                # CH1 Se — stays OPERATING, ramp down gracefully
                try:
                    if hasattr(h3, "write_ramp_rate"): h3.write_ramp_rate(1, 4.0)
                    h3.apply_setpoint(1, 50.0)
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"Se standby SP error: {e}"))
                # CH2 Sub — go to STANDBY state, set ramp for next operation
                try:
                    h3.go_standby(2)
                    if hasattr(h3, "write_ramp_rate"): h3.write_ramp_rate(2, 100.0)
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"Sub standby error: {e}"))
            epc = self.indium
            if epc:
                # Indium — stays OPERATING, ramp to 200°C hold point (above solidification ~157°C)
                try:
                    if hasattr(epc, "write_ramp_rate"): epc.write_ramp_rate(1, 15.0)
                    epc.apply_setpoint(1, 200.0)
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"In standby SP error: {e}"))
            QTimer.singleShot(0, lambda: self.standby_btn.setEnabled(True))
        threading.Thread(target=_do, daemon=True).start()

    def _estop_click(self):
        """E-STOP button — first click: immediate stop + latch.
        Second click: show reset confirmation dialog."""
        if self._safety_latch == "estop":
            self._safety_reset_prompt(); return
        self._safety_latch = "estop"
        self._update_safety_btn_states()
        self._log(f"<span style='color:{RD};font-weight:bold'>🛑 EMERGENCY STOP — closing shutters, immediate standby, SP → 0°C</span>")
        def _do():
            sh = self.shutter
            if sh:
                try: sh.both_close()
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"Shutter close error: {e}"))
            h3 = self.heat3
            if h3:
                for ch in [1, 2]:
                    try: h3.go_standby(ch)
                    except Exception as ex:
                        QTimer.singleShot(0, lambda e=ex, c=ch: self._log(f"Standby CH{c} error: {e}"))
                    try:
                        if hasattr(h3, "write_ramp_rate"): h3.write_ramp_rate(ch, 0.0)
                        h3.apply_setpoint(ch, 0.0)
                    except Exception as ex:
                        QTimer.singleShot(0, lambda e=ex, c=ch: self._log(f"SP CH{c} error: {e}"))
            epc = self.indium
            if epc:
                try: epc.go_standby(1)
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"Standby In error: {e}"))
                try:
                    if hasattr(epc, "write_ramp_rate"): epc.write_ramp_rate(1, 0.0)
                    epc.apply_setpoint(1, 0.0)
                except Exception as ex:
                    QTimer.singleShot(0, lambda e=ex: self._log(f"SP In error: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _update_safety_btn_states(self):
        """Sync E-STOP button text/style/enabled to the current _safety_latch state.
        Only E-STOP latches; STANDBY button is unaffected."""
        _latch_style = (
            f"QPushButton{{background:{OR};color:#000;font-weight:bold;font-size:11px;"
            f"border-radius:5px;border:2px solid #ffcc44;letter-spacing:0.5px;}}"
            f"QPushButton:hover{{background:#ffcc44;}}"
        )
        _estop_normal = (
            f"QPushButton{{background:{RD};color:#fff;font-weight:bold;font-size:11px;"
            f"border-radius:5px;border:2px solid #ff8888;}}"
            f"QPushButton:hover{{background:#ff2222;border-color:#ffaaaa;}}"
            f"QPushButton:disabled{{background:#553333;color:#666;border-color:#553333;}}"
        )
        _stby_normal = (
            f"QPushButton{{background:#b8860b;color:#fff;font-weight:bold;font-size:11px;"
            f"border-radius:5px;border:2px solid #ffd700;}}"
            f"QPushButton:hover{{background:#daa520;border-color:#ffe066;}}"
            f"QPushButton:disabled{{background:#443300;color:#666;border-color:#443300;}}"
        )
        if self._safety_latch == "estop":
            self.estop_btn.setText("↺  CLICK TO RESET")
            self.estop_btn.setStyleSheet(_latch_style);   self.estop_btn.setEnabled(True)
            self.standby_btn.setStyleSheet(_stby_normal); self.standby_btn.setEnabled(False)
        else:
            self.estop_btn.setText("🛑  E-STOP")
            self.estop_btn.setStyleSheet(_estop_normal);  self.estop_btn.setEnabled(True)
            self.standby_btn.setStyleSheet(_stby_normal); self.standby_btn.setEnabled(True)

    def _safety_reset_prompt(self):
        """Confirmation dialog when user clicks the latched E-STOP button."""
        ret = QMessageBox.question(
            self, "Clear Emergency Stop",
            "Emergency Stop has been activated.\n\n"
            "Heaters are in Standby. Clicking Yes will clear the E-STOP latch "
            "and restore both buttons to normal.\n\n"
            "Note: heaters remain in Standby until you manually press OPERATE.\n\n"
            "Clear all errors and return to normal operations?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            self._safety_latch = None
            self._safety_tripped.clear()
            self.ch1.set_safety_state("ok"); self.ch2.set_safety_state("ok"); self.ch_in.set_safety_state("ok")
            self._update_safety_btn_states()
            self._log(f"<span style='color:{GR}'>✓ E-STOP cleared — heaters remain in Standby until manually operated</span>")

    def _safe_standby_ch(self, ch: int):
        """Safety-triggered standby for one channel: go_standby + set standby SP/ramp."""
        def _do():
            h3 = self.heat3
            if not h3: return
            pfx = f"ch{ch}_"
            sbt = self.safety.get(f"{pfx}standby_temp", 50.0)
            sbr = self.safety.get(f"{pfx}standby_ramp", 10.0)
            try: h3.go_standby(ch)
            except: pass
            try:
                if hasattr(h3, "write_ramp_rate"): h3.write_ramp_rate(ch, sbr)
                h3.apply_setpoint(ch, sbt)
            except: pass
        threading.Thread(target=_do, daemon=True).start()

    def _check_safety(self, d: dict):
        """Called every 100 ms from _vals(). Checks temps/outputs against safety limits."""
        if not self.safety.get("enabled", True):
            if hasattr(self, "_sf_status"):
                self._sf_status.setText("DISABLED")
                self._sf_status.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:bold;")
            return
        now = time.monotonic(); any_warn = False; any_trip = False
        for ch, ch_widget in [(1, self.ch1), (2, self.ch2)]:
            pfx      = f"ch{ch}_"
            pv       = d.get(f"ch{ch}_pv")
            out      = d.get(f"ch{ch}_out")
            max_t    = self.safety.get(f"{pfx}max_temp",   1e6)
            warn_pct = self.safety.get(f"{pfx}warn_pct",   10.0)
            warn_t   = max_t * (1.0 - warn_pct / 100.0)
            max_out  = self.safety.get(f"{pfx}max_output", 100.0)
            trip_key = f"ch{ch}_temp"

            if pv is not None:
                if pv >= max_t:
                    any_trip = True; ch_widget.set_safety_state("trip")
                    if trip_key not in self._safety_tripped:
                        self._safety_tripped.add(trip_key)
                        self._log(f"<span style='color:{RD};font-weight:bold'>"
                                  f"⚠ SAFETY TRIP CH{ch}: PV={pv:.1f}°C ≥ MAX {max_t:.0f}°C — going to standby</span>")
                        self._safe_standby_ch(ch)
                elif pv >= warn_t:
                    any_warn = True; ch_widget.set_safety_state("warn")
                    wk = f"{trip_key}_warn"
                    if now - self._safety_warn_t.get(wk, 0) > 30.0:
                        self._safety_warn_t[wk] = now
                        self._log(f"<span style='color:{OR}'>⚠ WARNING CH{ch}: "
                                  f"PV={pv:.1f}°C approaching limit {max_t:.0f}°C "
                                  f"(warn ≥ {warn_t:.0f}°C)</span>")
                else:
                    self._safety_tripped.discard(trip_key)
                    ch_widget.set_safety_state("ok")

            if out is not None and out > max_out:
                any_warn = True
                wk = f"ch{ch}_out_warn"
                if now - self._safety_warn_t.get(wk, 0) > 30.0:
                    self._safety_warn_t[wk] = now
                    self._log(f"<span style='color:{OR}'>⚠ WARNING CH{ch}: "
                              f"output {out:.1f}% > max {max_out:.0f}%</span>")

        # ── Indium (EPC3004) safety check ─────────────────────────────
        in_pv  = d.get("in_pv")
        in_out = d.get("in_out")
        in_max    = self.safety.get("in_max_temp",    900.0)
        in_wpct   = self.safety.get("in_warn_pct",    10.0)
        in_warn_t = in_max * (1.0 - in_wpct / 100.0)
        in_max_out= self.safety.get("in_max_output",  100.0)
        if in_pv is not None:
            if in_pv >= in_max:
                any_trip = True; self.ch_in.set_safety_state("trip")
                if "in_temp" not in self._safety_tripped:
                    self._safety_tripped.add("in_temp")
                    self._log(f"<span style='color:{RD};font-weight:bold'>"
                              f"⚠ SAFETY TRIP In: PV={in_pv:.1f}°C ≥ MAX {in_max:.0f}°C — going to standby</span>")
                    self._safe_standby_in()
            elif in_pv >= in_warn_t:
                any_warn = True; self.ch_in.set_safety_state("warn")
                wk = "in_temp_warn"
                if now - self._safety_warn_t.get(wk, 0) > 30.0:
                    self._safety_warn_t[wk] = now
                    self._log(f"<span style='color:{OR}'>⚠ WARNING In: "
                              f"PV={in_pv:.1f}°C approaching limit {in_max:.0f}°C "
                              f"(warn ≥ {in_warn_t:.0f}°C)</span>")
            else:
                self._safety_tripped.discard("in_temp")
                self.ch_in.set_safety_state("ok")
        if in_out is not None and in_out > in_max_out:
            any_warn = True
            wk = "in_out_warn"
            if now - self._safety_warn_t.get(wk, 0) > 30.0:
                self._safety_warn_t[wk] = now
                self._log(f"<span style='color:{OR}'>⚠ WARNING In: "
                          f"output {in_out:.1f}% > max {in_max_out:.0f}%</span>")

        if hasattr(self, "_sf_status"):
            if any_trip:
                self._sf_status.setText("⛔ TRIPPED")
                self._sf_status.setStyleSheet(f"color:{RD};font-size:11px;font-weight:bold;")
            elif any_warn:
                self._sf_status.setText("⚠ WARNING")
                self._sf_status.setStyleSheet(f"color:{OR};font-size:11px;font-weight:bold;")
            else:
                self._sf_status.setText("✓ OK")
                self._sf_status.setStyleSheet(f"color:{GR};font-size:11px;font-weight:bold;")

    def _tog_in(self):
        if not self.shutter: return
        fn=self.shutter.in_close if self.shutter.in_is_open else self.shutter.in_open
        threading.Thread(target=fn,daemon=True).start()
    def _tog_se(self):
        if not self.shutter: return
        fn=self.shutter.se_close if self.shutter.se_is_open else self.shutter.se_open
        threading.Thread(target=fn,daemon=True).start()

    # ── Logging ───────────────────────────────────────────────────────
    def _tog_log(self):
        if self.logger.running:
            self.logger.stop_logging(); self.log_btn.setText("▶ Start Logging")
            self.log_btn.setStyleSheet(f"background:{GR};color:#000;font-weight:bold;border-radius:4px;padding:4px 10px;"); self._log("Logging stopped")
        else:
            self.logger.brax=self.brax; self.logger.heat3=self.heat3; self.logger.start_logging()
            self.log_btn.setText("⏹ Stop"); self.log_btn.setStyleSheet(f"background:{RD};color:#fff;font-weight:bold;border-radius:4px;padding:4px 10px;")
            self.log_lbl.setText(os.path.basename(self.logger.log_file_path or "")); self._log(f"Logging → {self.logger.log_file_path}")

    def _clear_plot(self):
        if HAS_PG:
            for c in [self.pc,self.tc1,self.tc2,self.tc3]: c.setData([],[])
        self.logger.pressure_buf.clear(); self.logger.ch1_temp_buf.clear(); self.logger.ch2_temp_buf.clear(); self.logger.in_temp_buf.clear()

    # ── BEP ───────────────────────────────────────────────────────────
    def _bep_state(self, s):
        self.bep_st.setText(s); run=s not in("IDLE","DONE","ABORTED")
        self.bep_btn.setEnabled(not run); self.bep_ab.setEnabled(run)
    def _bep_done(self):
        if self.bep.last_in_bep: self.bep_iv.setText(f"{self.bep.last_in_bep:.3e}")
        if self.bep.last_se_bep: self.bep_sv.setText(f"{self.bep.last_se_bep:.3e}")
        r=self.bep.se_in_ratio
        if r: self.bep_rat.setText(f"{r:.4f}")
        self.bep_pr.setValue(100)
        if HAS_PG: self._bep_plots()
    def _bep_plots(self):
        if self.bep.history_in:
            pts=[(r.temp_c,r.bep) for r in self.bep.history_in if r.bep>0]
            if pts: x,y=zip(*pts); self.bep_ic.setData(list(x),list(y))
        if self.bep.history_se:
            pts=[(r.temp_c,r.bep) for r in self.bep.history_se if r.bep>0]
            if pts: x,y=zip(*pts); self.bep_sc.setData(list(x),list(y))
        if HAS_PG: self._update_fast_trace()
    def _update_fast_trace(self):
        # Remove old open-window region items before redrawing
        for attr in ('_bft_in_rgn','_bft_se_rgn'):
            rgn=getattr(self,attr,None)
            if rgn is not None:
                self.bep_fp.removeItem(rgn)
                setattr(self,attr,None)
        def _draw(trace, line, top, bl, bep_l, rgn_attr, color):
            if trace is None:
                for item in (line,top,bl,bep_l): item.setData([],[])
                return
            ts=[t for t,p in trace.samples]; ps=[p for t,p in trace.samples]
            line.setData(ts,ps)
            if trace.top_samples:
                tt=[t for t,p in trace.top_samples]; tp=[p for t,p in trace.top_samples]
                top.setData(tt,tp)
            else: top.setData([],[])
            x_max=max(ts) if ts else 0
            if trace.baseline_val and x_max:
                bl.setData([0,x_max],[trace.baseline_val,trace.baseline_val])
            else: bl.setData([],[])
            if trace.bep_val is not None and trace.baseline_val and x_max:
                lvl=trace.baseline_val+trace.bep_val
                bep_l.setData([0,x_max],[lvl,lvl])
            else: bep_l.setData([],[])
            # Highlight open-window region with a semi-transparent band
            c=pg.mkColor(color); c.setAlpha(50)
            rgn=pg.LinearRegionItem(values=(trace.open_start_t,trace.open_end_t),movable=False,brush=pg.mkBrush(c))
            ec=pg.mkColor(color); ec.setAlpha(180)
            ep=pg.mkPen(ec,width=1)
            rgn.lines[0].setPen(ep); rgn.lines[1].setPen(ep)
            self.bep_fp.addItem(rgn)
            setattr(self,rgn_attr,rgn)
        _draw(self.bep.trace_in,self._bft_in,self._bft_in_top,self._bft_in_bl,self._bft_in_bep,'_bft_in_rgn',OR)
        _draw(self.bep.trace_se,self._bft_se,self._bft_se_top,self._bft_se_bl,self._bft_se_bep,'_bft_se_rgn',PU)
    def _bep_start(self):
        if not self._bep_ok:
            m=QMessageBox(self); m.setWindowTitle("BEP Setup"); m.setIcon(QMessageBox.Question)
            m.setText("Is the Ion Gauge facing the evaporation sources?")
            yes=m.addButton("Yes",QMessageBox.YesRole); m.addButton("Cancel",QMessageBox.NoRole)
            m.exec_()
            if m.clickedButton()!=yes: return
            self._bep_ok=True
        self._resolve(); self.bep.brax=self.brax; self.bep.heat3=self.heat3; self.bep.shutter=self.shutter
        if not self.brax: QMessageBox.warning(self,"No BRAX","BRAX 3400 not connected."); return
        if not self.shutter: QMessageBox.warning(self,"No Shutter","Arduino not connected."); return
        self.bep.start_measurement()

    # ── Recipe Preview ────────────────────────────────────────────────
    @staticmethod
    def _simulate_recipe_timeline(steps):
        """Walk recipe steps and build a proportional timeline for the preview plot.

        Returns:
          pts_t, pts_se, pts_sub  : temperature trace arrays
          shutter_segs            : List[(t0,t1,in_open,se_open)]
          rate_markers            : List[(t, rate_°C/min, ch)]  — RAMP_TEMP events
          goto_regions            : List[(t0, t1, label_str)]
          unk_regions             : List[(t0, t1)]  — WAIT_TEMP / WAIT_PRESSURE
        """
        t = 0.0
        se_t, sub_t = 25.0, 25.0
        se_rate, sub_rate = 4.0, 100.0   # °C/min defaults (HEAT3 factory)
        cur_in = cur_se = False

        pts_t=[0.0]; pts_se=[se_t]; pts_sub=[sub_t]
        shutter_segs=[]; cur_sh_t=0.0
        rate_markers=[]; goto_regions=[]; unk_regions=[]
        step_times={}   # step number → t at entry
        UNKNOWN_DT=120.0

        def snap():
            pts_t.append(t); pts_se.append(se_t); pts_sub.append(sub_t)
        def push(dt):
            nonlocal t
            t += max(0.0, float(dt))
            pts_t.append(t); pts_se.append(se_t); pts_sub.append(sub_t)
        def flush_sh():
            if t > cur_sh_t:
                shutter_segs.append((cur_sh_t, t, cur_in, cur_se))

        for s in steps:
            step_times[s.step] = t
            a = s.action

            if a == "RAMP_TEMP":
                try:
                    ch = RecipeEngine._src_to_ch(str(s.param1 or ""))
                    rate = float(s.param2) if s.param2 else 4.0
                    if ch == 1: se_rate = rate
                    else:       sub_rate = rate
                    rate_markers.append((t, rate, ch))
                except: pass
                snap()

            elif a == "SET_TEMP":
                try:
                    p1 = s.param1
                    if isinstance(p1, str) and any(c.isalpha() for c in p1):
                        ch = RecipeEngine._src_to_ch(p1); target = float(s.param2)
                    else:
                        ch = 1; target = float(p1)
                    if ch == 1:
                        push(abs(target - se_t) / max(se_rate, 0.01) * 60.0); se_t = target
                        pts_se[-1] = se_t
                    else:
                        push(abs(target - sub_t) / max(sub_rate, 0.01) * 60.0); sub_t = target
                        pts_sub[-1] = sub_t
                except: snap()

            elif a == "WAIT_TIME":
                try: push(float(s.param1))
                except: push(10)

            elif a in ("WAIT_TEMP","WAIT_PRESSURE"):
                t0u=t; push(UNKNOWN_DT); unk_regions.append((t0u, t))

            elif a in ("OPEN_SHUTTER","CLOSE_SHUTTER"):
                flush_sh(); cur_sh_t=t
                src=str(s.param1 or "").lower()
                if a == "OPEN_SHUTTER":
                    if "both" in src: cur_in=True;  cur_se=True
                    elif "se" in src: cur_se=True
                    else:             cur_in=True
                else:
                    if "both" in src: cur_in=False; cur_se=False
                    elif "se" in src: cur_se=False
                    else:             cur_in=False
                snap()

            elif a == "GOTO":
                try:
                    tgt   = int(float(s.param1))
                    loops = int(float(s.param2)) if s.param2 is not None else 1
                    if tgt in step_times:
                        goto_regions.append((step_times[tgt], t, f"?×{loops+1}"))
                except: pass
                snap()

            else:
                snap()   # HEATER_ON/OFF, IG_ON/OFF, END — no time advance

        flush_sh()
        return pts_t, pts_se, pts_sub, shutter_segs, rate_markers, goto_regions, unk_regions

    def _refresh_recipe_preview(self):
        if not HAS_PG or self._rp_plot is None: return
        steps = self._rtab_to_steps()
        # Remove old dynamic items
        for item in self._rp_items:
            try: self._rp_plot.removeItem(item)
            except: pass
        self._rp_items.clear()

        if not steps:
            self._rp_se.setData([],[]); self._rp_sub.setData([],[])
            return

        pts_t, pts_se, pts_sub, shutters, rmarks, gotos, unks = \
            self._simulate_recipe_timeline(steps)

        self._rp_se.setData(pts_t, pts_se)
        self._rp_sub.setData(pts_t, pts_sub)

        y_top = max(max(pts_se, default=0), max(pts_sub, default=0)) or 100.0

        # ── Shutter backgrounds ──────────────────────────────────────
        for t0, t1, in_s, se_s in shutters:
            if not (in_s or se_s) or t1 <= t0: continue
            if in_s and se_s: col="#22cc55"    # both: green
            elif in_s:        col="#22ee44"    # In only: bright green
            else:             col="#ee6622"    # Se only: orange-red
            bc=pg.mkColor(col); bc.setAlpha(50)
            rgn=pg.LinearRegionItem((t0,t1),movable=False,brush=pg.mkBrush(bc))
            ec=pg.mkColor(col); ec.setAlpha(160)
            for ln in rgn.lines: ln.setPen(pg.mkPen(ec,width=1))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)

        # ── Unknown-duration regions (WAIT_TEMP / WAIT_PRESSURE) ────
        for t0, t1 in unks:
            if t1 <= t0: continue
            bc=pg.mkColor("#888888"); bc.setAlpha(22)
            rgn=pg.LinearRegionItem((t0,t1),movable=False,brush=pg.mkBrush(bc))
            for ln in rgn.lines: ln.setPen(pg.mkPen("#888888",width=1,style=Qt.DotLine))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)

        # ── GOTO loop regions ────────────────────────────────────────
        for t0, t1, label in gotos:
            if t1 <= t0: continue
            bc=pg.mkColor("#ffffff"); bc.setAlpha(18)
            rgn=pg.LinearRegionItem((t0,t1),movable=False,brush=pg.mkBrush(bc))
            for ln in rgn.lines: ln.setPen(pg.mkPen("#cccccc",width=1.5,style=Qt.DashLine))
            self._rp_plot.addItem(rgn); self._rp_items.append(rgn)
            txt=pg.TextItem(label,color="#cccccc",anchor=(0.5,0.5))
            txt.setFont(QFont("Arial",11,QFont.Bold))
            txt.setPos((t0+t1)/2, y_top*0.85)
            self._rp_plot.addItem(txt); self._rp_items.append(txt)

        # ── RAMP_TEMP markers (vertical dashed line + rate label) ───
        for t_m, rate, ch in rmarks:
            col=OR if ch==1 else PU
            ln=pg.InfiniteLine(t_m,angle=90,pen=pg.mkPen(col,width=1,style=Qt.DashLine))
            self._rp_plot.addItem(ln); self._rp_items.append(ln)
            txt=pg.TextItem(f" {rate:.0f}°/m",color=col,anchor=(0,1))
            txt.setFont(QFont("Arial",8))
            txt.setPos(t_m, y_top)
            self._rp_plot.addItem(txt); self._rp_items.append(txt)

        self._rp_plot.autoRange()

    # ── Recipe ────────────────────────────────────────────────────────
    def _load_recipe(self):
        path,_=QFileDialog.getOpenFileName(self,"Open Recipe","","Excel Files (*.xlsx *.xls);;All Files (*)")
        if not path: return
        try:
            steps=self.recipe.load_excel(path); self._rtab_populate(steps)
            self.rf.setText(os.path.basename(path)); self.rrun.setEnabled(True); self._log(f"Recipe: {os.path.basename(path)} ({len(steps)} steps)")
        except Exception as e: QMessageBox.critical(self,"Load Error",str(e))

    def _r_run(self):
        self._resolve()
        # Log device state so user can see what recipe engine will use
        h3_desc = type(self.recipe.heat3).__name__ if self.recipe.heat3 else "None ⚠"
        self._log(f"▶ Run: heat3={h3_desc}  test={'ON' if self._test else 'OFF'}")
        if self.recipe.state==RecipeEngine.STATE_PAUSED:
            self.recipe.resume(); return
        # Ask for sample name
        name,ok=QInputDialog.getText(self,"Sample Name","Enter sample name for the run log:",QLineEdit.Normal,"")
        if not ok: return
        if not name.strip(): name="sample"
        steps=self._rtab_to_steps()
        if steps: self.recipe.steps=steps
        # If live logging is running, pause it so rec_log takes over; remember to restore
        self._log_was_running = self.logger.running
        if self._log_was_running:
            self.logger.stop_logging()
            self.log_btn.setText("▶ Start Logging")
            self.log_btn.setStyleSheet(f"background:{GR};color:#000;font-weight:bold;border-radius:4px;padding:4px 10px;")
            self._log("Pausing live log during run…")
        # Start recipe logger (feeds live plots + export)
        self.rec_log.brax=self.brax; self.rec_log.heat3=self.heat3; self.rec_log.shutter=self.shutter
        self.rec_log.recipe_steps=steps
        self.rec_log.bep_in=self.bep.last_in_bep; self.rec_log.bep_se=self.bep.last_se_bep; self.rec_log.bep_ratio=self.bep.se_in_ratio
        self.rec_log.bep_trace_in=self.bep.trace_in; self.rec_log.bep_trace_se=self.bep.trace_se
        self._sync_p_idx = 0; self._sync_t_idx = 0  # reset sync indices for fresh run
        self.rec_log.start(name.strip())
        self._log(f"🟢 Run started: <b>{name.strip()}</b>  ({len(steps)} steps)")
        self.recipe.start()

    def _r_stop(self):
        self.recipe.stop()
        if self.rec_log.running:
            self.rec_log.stop()
            def _export():
                os.makedirs(_LOG_DIR, exist_ok=True); path=self.rec_log.export_excel(_LOG_DIR)
                if path: QTimer.singleShot(0,lambda:self._log(f"📊 Run log saved → {path}"))
            threading.Thread(target=_export,daemon=True).start()

    # ── Recipe row highlighting ────────────────────────────────────────
    _HL_ROW_BG = "#3d2e00"    # active row background — must match ::item:selected CSS

    def _set_recipe_row_active(self, row, active):
        """Highlight or clear the active recipe row.

        Item cells (Step#, Param2, Notes): controlled via selectRow() /
        clearSelection() — the ::item:selected CSS rule does the painting, which
        is reliable regardless of QTableWidget stylesheet interactions.

        Cell-widget cells (Action col-1, Param1 col-2): QComboBoxes sit ON TOP of
        the item cell so ::item:selected has no effect on them.  We directly call
        setStyleSheet() on the widget itself.

        viewport().update() forces an immediate repaint so the highlight is visible
        on the very first call from the signal bridge.
        """
        if row < 0 or row >= self.rtab.rowCount():
            return

        hl_cb   = (f"QComboBox{{background:{self._HL_ROW_BG};color:#f1fa8c;"
                   f"border:1px solid {OR};border-radius:3px;padding:1px 4px;}}")
        base_cb = self._action_cb_style()

        # Style the ComboBox cell widgets
        for col in range(self.rtab.columnCount()):
            w = self.rtab.cellWidget(row, col)
            if w:
                w.setStyleSheet(hl_cb if active else base_cb)

        # Item cells: use Qt selection state (::item:selected CSS handles colour)
        if active:
            self.rtab.selectRow(row)
        else:
            # Only clear if this row is currently selected (avoids clearing user
            # selections on other rows when a stale -1 clear fires)
            if self.rtab.currentRow() == row:
                self.rtab.clearSelection()

        self.rtab.viewport().update()   # force immediate repaint

    def _r_step(self, i):
        # Clear the previous highlighted row, apply to new one
        self._set_recipe_row_active(self._active_recipe_row, False)
        self._active_recipe_row = i
        if 0 <= i < self.rtab.rowCount():
            self.rtab.scrollTo(self.rtab.model().index(i, 0))
            self._set_recipe_row_active(i, True)
        if 0<=i<self.rtab.rowCount():
            cb=self.rtab.cellWidget(i,1)
            action = cb.currentText() if cb else "?"
            sn_item = self.rtab.item(i, 0)
            step_num = sn_item.text() if sn_item else str(i+1)
            w1=self.rtab.cellWidget(i,2); p1_item=self.rtab.item(i,2)
            p1 = w1.currentText() if w1 else (p1_item.text() if p1_item else "")
            p2_item=self.rtab.item(i,3); p2 = p2_item.text() if p2_item else ""
            detail = f" {p1}" if p1 else ""
            detail += f" → {p2}" if p2 else ""
            # GOTO loop counter: [current_pass/total_executions]
            if action == "GOTO":
                try:
                    total_loops = int(float(p2)) if p2 else 0
                except (ValueError, TypeError):
                    total_loops = 0
                total_execs = total_loops + 1
                if i in self.recipe._loop_counters:
                    # _loop_counters holds remaining jumps from the previous execution
                    remaining = self.recipe._loop_counters[i]
                    current_pass = total_loops - remaining + 1
                else:
                    current_pass = 1   # first time reaching this GOTO
                detail += f"  [{current_pass}/{total_execs}]"
            self.rsk.setText(f"Step {step_num}: {action}{detail}")
            self._log(f"<span style='color:#50fa7b;font-weight:bold'>▶ Step {step_num}: {action}{detail}</span>")
    def _r_state(self, s):
        icon = {"RUNNING":"🟢","PAUSED":"🟡","DONE":"✅","ERROR":"❌","IDLE":"⬜"}.get(s,"")
        self.rst.setText(f"{icon} {s}")
        run=s=="RUNNING"; paused=s=="PAUSED"; idle=s in("IDLE","DONE","ERROR")
        self.rrun.setEnabled(idle or paused); self.rpau.setEnabled(run); self.rsto.setEnabled(run or paused)
        if idle:
            self.rsk.setText("—")
            self._set_recipe_row_active(self._active_recipe_row, False)
            self._active_recipe_row = -1
        # Auto-save log when recipe completes or errors
        if s in ("DONE","ERROR") and self.rec_log.running:
            self.rec_log.stop()
            def _export():
                os.makedirs(_LOG_DIR, exist_ok=True); path=self.rec_log.export_excel(_LOG_DIR)
                if path: QTimer.singleShot(0,lambda:self._log(f"📊 Run log saved → {path}"))
            threading.Thread(target=_export,daemon=True).start()
        # Resume live logging if it was running before the recipe
        if idle and getattr(self,"_log_was_running",False):
            self._log_was_running = False
            self.logger.brax=self.brax; self.logger.heat3=self.heat3
            self.logger.start_logging()
            self.log_btn.setText("⏹ Stop")
            self.log_btn.setStyleSheet(f"background:{RD};color:#fff;font-weight:bold;border-radius:4px;padding:4px 10px;")
            self.log_lbl.setText(os.path.basename(self.logger.log_file_path or ""))
            self._log("Live logging resumed")

    # ── Safety tab ────────────────────────────────────────────────────
    def _safety_tab(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(10, 10, 10, 10); v.setSpacing(10)

        # Enable row + live status
        top = QHBoxLayout()
        self._sf_enabled = QCheckBox("Enable Safety Monitoring")
        self._sf_enabled.setChecked(self.safety.get("enabled", True))
        self._sf_enabled.setStyleSheet(f"color:{FG};font-size:12px;font-weight:bold;")
        self._sf_status = QLabel("✓ OK")
        self._sf_status.setStyleSheet(f"color:{GR};font-size:11px;font-weight:bold;")
        stat_lbl = QLabel("Status:"); stat_lbl.setStyleSheet(f"color:{DIM};font-size:11px;")
        top.addWidget(self._sf_enabled); top.addStretch(1)
        top.addWidget(stat_lbl); top.addSpacing(4); top.addWidget(self._sf_status)
        v.addLayout(top)

        # Info banner
        info = QLabel(
            "Limits are checked every 100 ms against the hardware snapshot.  "
            "When PV ≥ Max Temp the heater is automatically sent to Standby and the SP "
            "is set to Standby Temp at Standby Ramp rate.  "
            "The 🛑 ALL STANDBY toolbar button triggers the same action on both channels immediately."
        )
        info.setStyleSheet(
            f"color:{DIM};font-size:10px;background:{BG};"
            f"border:1px solid {BDR};border-radius:4px;padding:6px;"
        )
        info.setWordWrap(True); v.addWidget(info)

        # Per-channel group boxes
        ch_row = QHBoxLayout(); ch_row.setSpacing(14)
        for ch, name, def_max in [(1, "Selenium", 1000.0), (2, "Substrate", 700.0), ("in", "Indium (EPC3004)", 900.0)]:
            pfx = f"ch{ch}_" if ch != "in" else "in_"
            g_title = f"CH{ch} — {name}" if ch != "in" else f"In — {name}"
            g = QGroupBox(g_title); g.setStyleSheet(self._gs())
            gv = QVBoxLayout(g); gv.setSpacing(4)

            def _dspin(lo, hi, val, suf="", dec=1):
                s = QDoubleSpinBox(); s.setRange(lo, hi); s.setDecimals(dec); s.setValue(val)
                if suf: s.setSuffix(f" {suf}")
                s.setStyleSheet(
                    f"background:{BG};color:{FG};border:1px solid {BDR};"
                    f"border-radius:3px;padding:1px 2px;"
                )
                return s

            def _lbl(t):
                l = QLabel(t); l.setStyleSheet(f"color:{FG};font-size:11px;"); return l

            # Limits section
            lim_lbl = QLabel("─── Limits ───")
            lim_lbl.setStyleSheet(f"color:{DIM};font-size:9px;"); lim_lbl.setAlignment(Qt.AlignCenter)
            gv.addWidget(lim_lbl)
            fl = QFormLayout(); fl.setSpacing(5); fl.setLabelAlignment(Qt.AlignRight)

            max_t    = _dspin(0, 1500, self.safety.get(f"{pfx}max_temp",   def_max),  "°C")
            warn_pct = _dspin(1,   50, self.safety.get(f"{pfx}warn_pct",   10.0),     "%",      0)
            max_out  = _dspin(0,  100, self.safety.get(f"{pfx}max_output", 100.0),    "%",      0)
            max_ramp = _dspin(0, 1000, self.safety.get(f"{pfx}max_ramp",   100.0),    "°C/min", 0)

            fl.addRow(_lbl("Max Temp:"),      max_t)
            fl.addRow(_lbl("Warn within:"),   warn_pct)
            fl.addRow(_lbl("Max Output:"),    max_out)
            fl.addRow(_lbl("Max Ramp Rate:"), max_ramp)
            gv.addLayout(fl)

            # Divider
            dv = QFrame(); dv.setFrameShape(QFrame.HLine)
            dv.setStyleSheet(f"color:{BDR};margin:4px 0;"); gv.addWidget(dv)

            # Standby section
            stby_lbl = QLabel("─── Standby Settings ───")
            stby_lbl.setStyleSheet(f"color:{DIM};font-size:9px;"); stby_lbl.setAlignment(Qt.AlignCenter)
            gv.addWidget(stby_lbl)
            fl2 = QFormLayout(); fl2.setSpacing(5); fl2.setLabelAlignment(Qt.AlignRight)

            stby_temp = _dspin(0, 500, self.safety.get(f"{pfx}standby_temp", 50.0),  "°C")
            stby_ramp = _dspin(0, 200, self.safety.get(f"{pfx}standby_ramp", 10.0),  "°C/min", 0)

            stby_temp.setToolTip("Setpoint written when standby is triggered (E-stop or safety trip)")
            stby_ramp.setToolTip("Device internal ramp rate used when descending to Standby Temp")

            fl2.addRow(_lbl("Standby Temp:"), stby_temp)
            fl2.addRow(_lbl("Standby Ramp:"), stby_ramp)
            gv.addLayout(fl2)

            setattr(self, f"_sf_{pfx}max_temp",  max_t)
            setattr(self, f"_sf_{pfx}warn_pct",   warn_pct)
            setattr(self, f"_sf_{pfx}max_out",    max_out)
            setattr(self, f"_sf_{pfx}max_ramp",   max_ramp)
            setattr(self, f"_sf_{pfx}stby_temp",  stby_temp)
            setattr(self, f"_sf_{pfx}stby_ramp",  stby_ramp)
            ch_row.addWidget(g)
        v.addLayout(ch_row)

        # Buttons row
        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        apply_btn = QPushButton("✓  Apply Safety Settings")
        apply_btn.setStyleSheet(
            f"QPushButton{{background:{GR};color:#000;font-weight:bold;border-radius:5px;"
            f"padding:7px 22px;font-size:12px;}}QPushButton:hover{{background:#70ff9b;}}"
        )
        apply_btn.clicked.connect(self._safety_apply)

        rst_btn = QPushButton("↺  Reset Trips")
        rst_btn.setStyleSheet(
            f"QPushButton{{background:{PBG};color:{FG};border:1px solid {BDR};"
            f"border-radius:5px;padding:7px 14px;}}QPushButton:hover{{background:#3a3a5e;}}"
        )
        rst_btn.setToolTip("Clear active safety trips. Does NOT re-enable heaters — use OPERATE button for that.")
        def _reset_trips():
            self._safety_tripped.clear()
            self.ch1.set_safety_state("ok"); self.ch2.set_safety_state("ok"); self.ch_in.set_safety_state("ok")
            self._log("Safety trips cleared — heaters remain in Standby until manually operated")
        rst_btn.clicked.connect(_reset_trips)

        btn_row.addStretch(1); btn_row.addWidget(rst_btn); btn_row.addWidget(apply_btn); btn_row.addStretch(1)
        v.addLayout(btn_row)
        v.addStretch(1)
        return w

    def _safety_apply(self):
        self.safety["enabled"] = self._sf_enabled.isChecked()
        for pfx in ["ch1_", "ch2_", "in_"]:
            self.safety[f"{pfx}max_temp"]    = getattr(self, f"_sf_{pfx}max_temp").value()
            self.safety[f"{pfx}warn_pct"]    = getattr(self, f"_sf_{pfx}warn_pct").value()
            self.safety[f"{pfx}max_output"]  = getattr(self, f"_sf_{pfx}max_out").value()
            self.safety[f"{pfx}max_ramp"]    = getattr(self, f"_sf_{pfx}max_ramp").value()
            self.safety[f"{pfx}standby_temp"]= getattr(self, f"_sf_{pfx}stby_temp").value()
            self.safety[f"{pfx}standby_ramp"]= getattr(self, f"_sf_{pfx}stby_ramp").value()
        self._safety_tripped.clear()
        en = "enabled" if self.safety["enabled"] else "disabled"
        self._log(
            f"✓ Safety settings applied — monitoring {en}  "
            f"| CH1 max={self.safety['ch1_max_temp']:.0f}°C  stby={self.safety['ch1_standby_temp']:.0f}°C  "
            f"| CH2 max={self.safety['ch2_max_temp']:.0f}°C  stby={self.safety['ch2_standby_temp']:.0f}°C  "
            f"| In max={self.safety['in_max_temp']:.0f}°C  stby={self.safety['in_standby_temp']:.0f}°C"
        )

    # ── Settings / misc ───────────────────────────────────────────────
    def _settings(self):
        dlg=SettingsDlg(self.cfg,self)
        if dlg.exec_()==QDialog.Accepted:
            self.cfg=dlg.get()
            for k,cb in self._cbs.items(): cb.setCurrentText(self.cfg.get(k,""))
            self.poller.interval = self.cfg.get("poll_interval", 0.25)
            self._log("Settings updated")
    def _export_fw(self):
        from devices.arduino_shutter import ARDUINO_FIRMWARE
        path,_=QFileDialog.getSaveFileName(self,"Export Firmware","CovapDuty_v2.ino","Arduino Sketch (*.ino)")
        if path:
            with open(path,"w") as f: f.write(ARDUINO_FIRMWARE.strip()); self._log(f"Firmware → {path}")

    def closeEvent(self, e):
        self.poller.stop()
        if self._plot_worker: self._plot_worker.stop()
        self.logger.stop_logging(); self.recipe.stop()
        self.rec_log.stop(); self.bep.abort()
        for d in [self._br, self._h3, self._sh, self._in]:
            try:
                if d: d.disconnect()
            except: pass
        self._resolve(); e.accept()
