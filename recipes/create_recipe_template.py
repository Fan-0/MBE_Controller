"""
create_recipe_template.py
Run this script once to generate a sample recipe Excel file.
"""

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Side, Border


def create_template(path="recipe_template.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Recipe"

    # Header row
    headers = ["Step", "Action", "Param1", "Param2", "Notes"]
    header_fill  = PatternFill("solid", fgColor="2a2a3e")
    header_font  = Font(bold=True, color="5e9cf3")
    thin = Side(style="thin", color="44446a")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border    = border

    # Column widths
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 40

    # Sample recipe rows
    rows = [
        (1,  "IG_ON",          "",      "",  "Turn on ion gauge"),
        (2,  "WAIT_PRESSURE",  "1E-5",  "",  "Wait until pressure <= 1e-5 Torr"),
        (3,  "HEATER_ON",      "",      "",  "Go to OPERATE"),
        (4,  "SET_TEMP",       "200",   "",  "Set temperature to 200°C"),
        (5,  "WAIT_TEMP",      "5",     "",  "Wait until within ±5°C of setpoint"),
        (6,  "OPEN_BOTH",      "",      "",  "Open In and Se shutters"),
        (7,  "WAIT_TIME",      "300",   "",  "Deposit for 300 seconds"),
        (8,  "CLOSE_BOTH",     "",      "",  "Close both shutters"),
        (9,  "GOTO",           "6",     "3", "Loop back to step 6, 3 total iterations"),
        (10, "SET_TEMP",       "25",    "",  "Cool down to 25°C"),
        (11, "WAIT_TEMP",      "5",     "",  "Wait for cool-down"),
        (12, "HEATER_OFF",     "",      "",  "Go to STANDBY"),
        (13, "IG_OFF",         "",      "",  "Turn off ion gauge"),
        (14, "END",            "",      "",  "Recipe complete"),
    ]

    data_fill_even = PatternFill("solid", fgColor="1e1e2e")
    data_fill_odd  = PatternFill("solid", fgColor="252535")
    data_font      = Font(color="cdd6f4")

    for r_idx, row in enumerate(rows, 2):
        fill = data_fill_even if r_idx % 2 == 0 else data_fill_odd
        for c_idx, val in enumerate(row, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.font   = data_font
            cell.fill   = fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center" if c_idx < 5 else "left")

    wb.save(path)
    print(f"Recipe template written to: {path}")
    return path


if __name__ == "__main__":
    create_template()
