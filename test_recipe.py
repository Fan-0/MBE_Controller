import sys, types, time
sys.path.insert(0, ".")
import unittest.mock as mock
sys.modules['serial'] = mock.MagicMock()
sys.modules['minimalmodbus'] = mock.MagicMock()

from recipe_engine import RecipeEngine, RecipeStep
from devices.sim_devices import HEAT3Sim, BRAX3400Sim, ArduinoShutterSim
import openpyxl

wb = openpyxl.load_workbook("/mnt/user-data/uploads/recipe.xlsx")
ws = wb.active
steps = []
for i, row in enumerate(ws.iter_rows(values_only=True)):
    if i == 0: continue
    step_num, action, p1, p2, notes = row
    if action:
        steps.append(RecipeStep(step_num, action, p1, p2, notes or ""))

print("Steps loaded:")
for s in steps:
    print(f"  {s.step}: {s.action}  p1={s.param1!r}({type(s.param1).__name__})  p2={s.param2!r}({type(s.param2).__name__})")

heat3 = HEAT3Sim()
brax = BRAX3400Sim()
shutter = ArduinoShutterSim()

log_msgs = []
eng = RecipeEngine()
eng.heat3 = heat3; eng.brax = brax; eng.shutter = shutter
eng.steps = steps
eng.on_status_msg = lambda m: log_msgs.append(m)
eng.on_state_change = lambda s: print(f"  STATE: {s}")
eng.on_step_change = lambda i: print(f"  STEP idx={i}")
eng.start()

deadline = time.time() + 60
while eng.state not in ("DONE","ERROR") and time.time() < deadline:
    time.sleep(0.3)

print(f"\nFinal: {eng.state}")
for m in log_msgs: print(" ", m)
