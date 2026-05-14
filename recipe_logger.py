"""
RecipeLogger — collects live data during a recipe run and exports to Excel.

Workbook structure:
  Sheet 1 "Run Info"  : Sample name, BEP data, recipe table, + line chart of all vars
  Sheet 2 "Pressure"  : Timestamp, Elapsed(s), IG_Pressure_Torr
  Sheet 3 "Temperature": Timestamp, Elapsed(s), CH1_Se_C, CH2_Sub_C
  Sheet 4 "Shutters"  : Timestamp, Elapsed(s), In_Open (0/1), Se_Open (0/1)

Data is collected on a 1-second background thread during the recipe.
"""

import csv, os, threading, time
from datetime import datetime
from typing import Optional, List, Callable

try:
    import openpyxl
    from openpyxl.chart import LineChart, Reference
    from openpyxl.chart.series_factory import SeriesFactory
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True

    def _make_series(ws, col: int, hdr_row: int, data_end_row: int):
        """
        Build a chart Series using SeriesFactory (works openpyxl 3.0–3.1+).
        The Reference MUST include the header cell so title_from_data picks it up.
        """
        ref = Reference(ws, min_col=col, min_row=hdr_row, max_row=data_end_row)
        return SeriesFactory(ref, title_from_data=True)

except ImportError:
    HAS_OPENPYXL = False


class RecipeLogger:
    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self.sample_name   = "sample"
        self.bep_in        = None   # float Torr
        self.bep_se        = None
        self.bep_ratio     = None
        self.bep_trace_in  = None   # BEPTrace — annotated fast trace for Indium
        self.bep_trace_se  = None   # BEPTrace — annotated fast trace for Selenium
        self.recipe_steps  = []     # list of RecipeStep

        self.brax    = None
        self.heat3   = None
        self.shutter = None
        self.indium  = None   # EPC3004 — Indium source temperature

        self._rows_pressure: List[tuple]   = []  # (ts, elapsed, ig)
        self._rows_temp:     List[tuple]   = []  # (ts, elapsed, ch1, ch2, in_t)
        self._rows_shutter:  List[tuple]   = []  # (ts, elapsed, in_open, se_open)

        self._start_time: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False

    # ── Control ──────────────────────────────────────────────────────

    def start(self, sample_name: str = "sample"):
        self.sample_name = sample_name
        self._rows_pressure = []; self._rows_temp = []; self._rows_shutter = []
        self._start_time = time.time()
        self._stop.clear(); self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="RecLog")
        self._thread.start()

    def stop(self):
        self._stop.set(); self.running = False
        if self._thread: self._thread.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            ts = datetime.now().isoformat(timespec="seconds")
            el = round(t0 - self._start_time, 1)

            # Pressure
            ig = None
            try:
                if self.brax and getattr(self.brax,"connected",False):
                    ig = self.brax.read_ion_gauge()
            except: pass
            self._rows_pressure.append((ts, el, ig))

            # Temperature
            ch1 = ch2 = in_t = None
            try:
                if self.heat3 and getattr(self.heat3,"connected",False):
                    ch1 = self.heat3.ch1_read_process_value()
                    ch2 = self.heat3.ch2_read_process_value()
            except: pass
            try:
                if self.indium and getattr(self.indium,"connected",False):
                    in_t = self.indium.ch1_read_process_value()
            except: pass
            self._rows_temp.append((ts, el, ch1, ch2, in_t))

            # Shutter
            in_o = se_o = 0
            try:
                if self.shutter:
                    in_o = 1 if getattr(self.shutter,"in_is_open",False) else 0
                    se_o = 1 if getattr(self.shutter,"se_is_open",False) else 0
            except: pass
            self._rows_shutter.append((ts, el, in_o, se_o))

            self._stop.wait(max(0, self.poll_interval - (time.time()-t0)))

    # ── Export ────────────────────────────────────────────────────────

    def export_excel(self, out_dir: str = ".") -> Optional[str]:
        if not HAS_OPENPYXL:
            return None

        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.sample_name)
        filename = os.path.join(out_dir, f"{ts_str}_{safe_name}.xlsx")

        wb = openpyxl.Workbook()

        # ── Sheet 1: Run Info ────────────────────────────────────────
        ws1 = wb.active; ws1.title = "Run Info"
        hdr_fill = PatternFill("solid", fgColor="2a2a3e")
        hdr_font = Font(bold=True, color="5e9cf3", size=11)
        val_font = Font(color="cdd6f4", size=11)
        bold_font = Font(bold=True, color="cdd6f4", size=11)

        def _h(cell, text):
            cell.value = text; cell.font = hdr_font; cell.fill = hdr_fill
        def _v(cell, text):
            cell.value = text; cell.font = val_font

        ws1["A1"] = "Sample Name"; ws1["A1"].font = bold_font
        ws1["B1"] = self.sample_name; ws1["B1"].font = Font(bold=True, color="50fa7b", size=12)
        ws1["A2"] = "Run Date/Time"; _v(ws1["B2"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        ws1["A3"] = ""; ws1["A4"] = "— BEP Data —"; ws1["A4"].font = hdr_font
        _h(ws1["A5"],"Source"); _h(ws1["B5"],"BEP (Torr)"); _h(ws1["C5"],"Temp (°C)")
        # Show actual In temp from last rec_log row if available, else "N/A"
        _in_t_last = next((r[4] for r in reversed(self._rows_temp) if len(r) > 4 and r[4] is not None), None)
        _in_t_str  = f"{_in_t_last:.1f}" if _in_t_last is not None else "744 (fixed)"
        ws1["A6"] = "Indium"; _v(ws1["B6"], self.bep_in if self.bep_in else "N/A"); _v(ws1["C6"], _in_t_str)
        ws1["A7"] = "Selenium"; _v(ws1["B7"], self.bep_se if self.bep_se else "N/A")
        ws1["A8"] = "Se/In Ratio"; _v(ws1["B8"], f"{self.bep_ratio:.4f}" if self.bep_ratio else "N/A")
        ws1["A9"] = ""
        ws1["A10"] = "— Recipe —"; ws1["A10"].font = hdr_font
        _h(ws1["A11"],"Step"); _h(ws1["B11"],"Action"); _h(ws1["C11"],"Param1"); _h(ws1["D11"],"Param2"); _h(ws1["E11"],"Notes")
        for i, s in enumerate(self.recipe_steps):
            r = 12 + i
            for col, val in enumerate([s.step, s.action, s.param1, s.param2, s.notes], 1):
                ws1.cell(r, col).value = val; ws1.cell(r, col).font = val_font

        # Chart on sheet 1 (temps + optionally IG in second axis)
        recipe_rows = len(self.recipe_steps)
        chart_start_row = 12 + recipe_rows + 3

        # Build a small data table for the chart (elapsed, ch1, ch2, in_t)
        chart_data_start = chart_start_row
        ws1.cell(chart_data_start, 1).value = "Elapsed(s)"
        ws1.cell(chart_data_start, 2).value = "Se Temp (°C)"
        ws1.cell(chart_data_start, 3).value = "Sub Temp (°C)"
        ws1.cell(chart_data_start, 4).value = "In Temp (°C)"
        for i, row in enumerate(self._rows_temp):
            r = chart_data_start + 1 + i
            ws1.cell(r, 1).value = row[1]                             # elapsed
            ws1.cell(r, 2).value = row[2]                             # ch1 Se
            ws1.cell(r, 3).value = row[3]                             # ch2 Sub
            ws1.cell(r, 4).value = row[4] if len(row) > 4 else None  # In

        n_data = len(self._rows_temp)
        if n_data > 1:
            chart = LineChart()
            chart.title = f"{self.sample_name} — Temperature vs Time"
            chart.style = 10; chart.y_axis.title = "Temperature (°C)"; chart.x_axis.title = "Elapsed (s)"
            chart.width = 24; chart.height = 14

            cats = Reference(ws1, min_col=1, min_row=chart_data_start+1, max_row=chart_data_start+n_data)
            for col in [2, 3, 4]:
                chart.series.append(_make_series(ws1, col, chart_data_start, chart_data_start + n_data))
            chart.set_categories(cats)
            ws1.add_chart(chart, f"G{chart_data_start}")

        ws1.column_dimensions["A"].width = 18; ws1.column_dimensions["B"].width = 22
        ws1.column_dimensions["C"].width = 18; ws1.column_dimensions["D"].width = 12; ws1.column_dimensions["E"].width = 30

        # ── Sheet 2: Pressure ────────────────────────────────────────
        ws2 = wb.create_sheet("Pressure")
        for col, h in enumerate(["Timestamp","Elapsed(s)","IG_Pressure_Torr"],1):
            ws2.cell(1,col).value = h; ws2.cell(1,col).font = hdr_font; ws2.cell(1,col).fill = hdr_fill
        for i, row in enumerate(self._rows_pressure):
            for col, val in enumerate(row, 1):
                ws2.cell(i+2, col).value = val
        if len(self._rows_pressure) > 1:
            pc = LineChart(); pc.title = "Ion Gauge Pressure"; pc.style = 10
            pc.y_axis.title = "Pressure (Torr)"; pc.x_axis.title = "Elapsed (s)"; pc.width = 22; pc.height = 12
            pc.series.append(_make_series(ws2, 3, 1, len(self._rows_pressure)+1))
            cats_p = Reference(ws2, min_col=2, min_row=2, max_row=len(self._rows_pressure)+1)
            pc.set_categories(cats_p); ws2.add_chart(pc,"E2")

        # ── Sheet 3: Temperature ─────────────────────────────────────
        ws3 = wb.create_sheet("Temperature")
        for col, h in enumerate(["Timestamp","Elapsed(s)","CH1_Se_C","CH2_Sub_C","In_C"],1):
            ws3.cell(1,col).value = h; ws3.cell(1,col).font = hdr_font; ws3.cell(1,col).fill = hdr_fill
        for i, row in enumerate(self._rows_temp):
            # Pad to 5 cols in case older rows have 4 elements (backward compat)
            padded = tuple(row) + (None,) * max(0, 5 - len(row))
            for col, val in enumerate(padded[:5], 1):
                ws3.cell(i+2, col).value = val
        if len(self._rows_temp) > 1:
            tc = LineChart(); tc.title = "Temperature"; tc.style = 10
            tc.y_axis.title = "°C"; tc.x_axis.title = "Elapsed (s)"; tc.width = 22; tc.height = 12
            for col in [3, 4, 5]:
                tc.series.append(_make_series(ws3, col, 1, len(self._rows_temp)+1))
            cats_t = Reference(ws3, min_col=2, min_row=2, max_row=len(self._rows_temp)+1)
            tc.set_categories(cats_t); ws3.add_chart(tc,"F2")

        # ── Sheet 4: Shutters ────────────────────────────────────────
        ws4 = wb.create_sheet("Shutters")
        for col, h in enumerate(["Timestamp","Elapsed(s)","In_Open","Se_Open"],1):
            ws4.cell(1,col).value = h; ws4.cell(1,col).font = hdr_font; ws4.cell(1,col).fill = hdr_fill
        for i, row in enumerate(self._rows_shutter):
            for col, val in enumerate(row, 1):
                ws4.cell(i+2, col).value = val
        if len(self._rows_shutter) > 1:
            sc = LineChart(); sc.title = "Shutter States"; sc.style = 10
            sc.y_axis.title = "Open (1=open)"; sc.x_axis.title = "Elapsed (s)"; sc.y_axis.scaling.min = -0.1; sc.y_axis.scaling.max = 1.5
            sc.width = 22; sc.height = 12
            for col in [3, 4]:
                sc.series.append(_make_series(ws4, col, 1, len(self._rows_shutter)+1))
            cats_s = Reference(ws4, min_col=2, min_row=2, max_row=len(self._rows_shutter)+1)
            sc.set_categories(cats_s); ws4.add_chart(sc,"F2")

        # ── Sheet 5: BEP Fast Trace ─────────────────────────────────────
        ti = self.bep_trace_in
        ts_ = self.bep_trace_se
        if ti or ts_:
            ws5 = wb.create_sheet("BEP Fast Trace")
            ws5["A1"] = "BEP Fast Trace Data"
            ws5["A1"].font = Font(bold=True, color="ffb86c", size=13)

            # ── Summary block ────────────────────────────────────────
            def _fmt(v): return f"{v:.3e}" if v is not None else "N/A"
            if ti:
                _h(ws5["A3"], "Indium"); ws5["A3"].font = Font(bold=True, color="ffb86c", size=11)
                _v(ws5["A4"], "Baseline (mean)");   _v(ws5["B4"], _fmt(ti.baseline_val))
                _v(ws5["A5"], "Peak (top-50% mean)");_v(ws5["B5"], _fmt(ti.peak_val))
                _v(ws5["A6"], "BEP");               _v(ws5["B6"], _fmt(ti.bep_val))
                _v(ws5["A7"], "Open window (s)");   _v(ws5["B7"], f"{ti.open_start_t:.1f} – {ti.open_end_t:.1f}")
            if ts_:
                _h(ws5["D3"], "Selenium"); ws5["D3"].font = Font(bold=True, color="bd93f9", size=11)
                _v(ws5["D4"], "Baseline (mean)");   _v(ws5["E4"], _fmt(ts_.baseline_val))
                _v(ws5["D5"], "Peak (top-50% mean)");_v(ws5["E5"], _fmt(ts_.peak_val))
                _v(ws5["D6"], "BEP");               _v(ws5["E6"], _fmt(ts_.bep_val))
                _v(ws5["D7"], "Open window (s)");   _v(ws5["E7"], f"{ts_.open_start_t:.1f} – {ts_.open_end_t:.1f}")
            if self.bep_ratio:
                _v(ws5["A8"], "Se/In BEP Ratio"); _v(ws5["B8"], f"{self.bep_ratio:.4f}")

            # ── Data tables ──────────────────────────────────────────
            DATA_ROW = 10
            if ti:
                _h(ws5.cell(DATA_ROW, 1), "In: Time (s)")
                _h(ws5.cell(DATA_ROW, 2), "In: Pressure (Torr)")
            if ts_:
                _h(ws5.cell(DATA_ROW, 4), "Se: Time (s)")
                _h(ws5.cell(DATA_ROW, 5), "Se: Pressure (Torr)")

            in_samp = ti.samples  if ti  else []
            se_samp = ts_.samples if ts_ else []
            n_max = max(len(in_samp), len(se_samp))
            for i in range(n_max):
                row = DATA_ROW + 1 + i
                if i < len(in_samp):
                    ws5.cell(row,1).value = round(in_samp[i][0], 3)
                    ws5.cell(row,2).value = in_samp[i][1]
                if i < len(se_samp):
                    ws5.cell(row,4).value = round(se_samp[i][0], 3)
                    ws5.cell(row,5).value = se_samp[i][1]

            # ── Charts ───────────────────────────────────────────────
            if ti and len(in_samp) > 1:
                ic = LineChart(); ic.title = "Indium Fast Trace"; ic.style = 10
                ic.y_axis.title = "Pressure (Torr)"; ic.x_axis.title = "Time (s)"
                ic.width = 18; ic.height = 12
                ic.series.append(_make_series(ws5, 2, DATA_ROW, DATA_ROW + len(in_samp)))
                ic.set_categories(Reference(ws5, min_col=1, min_row=DATA_ROW+1, max_row=DATA_ROW+len(in_samp)))
                ws5.add_chart(ic, "G10")
            if ts_ and len(se_samp) > 1:
                sc = LineChart(); sc.title = "Selenium Fast Trace"; sc.style = 10
                sc.y_axis.title = "Pressure (Torr)"; sc.x_axis.title = "Time (s)"
                sc.width = 18; sc.height = 12
                sc.series.append(_make_series(ws5, 5, DATA_ROW, DATA_ROW + len(se_samp)))
                sc.set_categories(Reference(ws5, min_col=4, min_row=DATA_ROW+1, max_row=DATA_ROW+len(se_samp)))
                ws5.add_chart(sc, f"G{DATA_ROW + 20}")

            ws5.column_dimensions["A"].width = 22; ws5.column_dimensions["B"].width = 18
            ws5.column_dimensions["D"].width = 22; ws5.column_dimensions["E"].width = 18

        wb.save(filename)
        return filename
