from pymodbus.client import ModbusSerialClient

client = ModbusSerialClient(
    port='COM13',        # check Device Manager for your USB-RS485 port
    baudrate=19200,
    bytesize=8,
    parity='N',
    stopbits=1,
    timeout=1
)
client.connect()

# Read PV (process temperature) — Modbus address 0x0001
result = client.read_holding_registers(address=0x0001, count=1, slave=1)
pv = result.registers[0] / 10.0  # EPC3004 scales by 10
print(f"Temperature: {pv}°C")

# Write setpoint — Modbus address 0x0018
client.write_register(address=0x0018, value=1700, slave=1)  # 170.0°C