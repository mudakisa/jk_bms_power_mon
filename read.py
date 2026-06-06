import asyncio
from jkbms import read_cell_info

try:
    from config import BANKS
except ImportError:
    raise SystemExit("PowerMon: no config.py found.\n"
                     "    cp config.example.py config.py   # then edit it")

def fmt(name, d):
    cells = " ".join(f"{v:.3f}" for v in d["cells_v"])
    print(f"\n=== {name} ===")
    print(f"  Pack:    {d['pack_v']:.2f} V   {d['current_a']:+.2f} A   {d['power_w']:+.1f} W")
    print(f"  SoC:     {d['soc_pct']} %   ({d['remain_ah']:.1f} / {d['nominal_ah']:.0f} Ah)")
    print(f"  Cells:   [{cells}] V")
    print(f"  Balance: avg {d['cell_avg_v']:.3f} V, delta {d['cell_delta_v']*1000:.0f} mV")
    print(f"  Temps:   MOS {d['mos_temp_c']:.1f}  T1 {d['temp1_c']:.1f}  T2 {d['temp2_c']:.1f} C")
    print(f"  Cycles:  {d['cycles']}  ({d['cycle_ah']:.0f} Ah lifetime)")

async def main():
    for name, addr in BANKS:
        try:
            fmt(name, await read_cell_info(addr))
        except Exception as e:
            print(f"\n=== {name} ===\n  ERROR: {e!r}")
        await asyncio.sleep(1.0)

asyncio.run(main())
