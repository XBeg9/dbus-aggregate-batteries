"""Replay of 2026-11-23 (pack RS485 loss, 18:36:45 UTC) through the emergency state machine.

Data: tests/data/2026-11-23.json, VictoriaMetrics query_range 18:30-21:30 UTC, step 15s:
vebus_v/i, multi_v/i = victron_{vebus,multi}_Dc_0_{Voltage,Current}; solar_i = sum(victron_solarcharger_Dc_0_Current);
conn1/conn2 = victron_battery_Connected{instance="1"|"2"}; soc99 = victron_battery_Soc{instance="99"};
min_soc_settings/min_soc_multi = victron_settings_Settings_CGwacs_BatteryLife_MinimumSocLimit / victron_multi_Settings_Ess_MinimumSocLimit.
Missing samples (Cerbo hard reset 19:08:41) are None.
"""

import json
from pathlib import Path

from emergency import Emergency, EmergencyConfig, Stored

ENTRY = 18 * 3600 + 36 * 60 + 55  # seconds into the UTC day; replay uses epoch % 86400
STATE = ("conn1", "conn2", "soc99", "min_soc_settings", "min_soc_multi")


def replay():
    d = {k: dict(map(tuple, v)) for k, v in json.loads((Path(__file__).parent / "data" / "2026-11-23.json").read_text()).items()}
    e = Emergency(EmergencyConfig(), Stored(), lambda c: None)
    out = []
    held = {}  # state signals (Connected, SoC, minimum SoC) keep their last value between samples
    for t in sorted(d["vebus_i"]):
        held.update({k: d[k][t] for k in STATE if t in d[k]})
        g = lambda k: held.get(k) if k in STATE else d[k].get(t)
        cur = [g("vebus_i"), g("multi_i"), g("solar_i")]
        cur = None if g("vebus_i") is None else sum(x or 0 for x in cur)
        packs_ok = g("conn1") == 1 and g("conn2") == 1
        volts = [g("vebus_v"), g("multi_v")]
        lim = e.step(t, packs_ok, volts, cur, [g("min_soc_settings"), g("min_soc_multi")], g("soc99") if packs_ok else None)
        out.append((t, lim, volts, e.active, e._latched))
    return out


def test_replay_1123():
    out = replay()
    first = next(t for t, lim, *_ in out if lim is not None)
    assert abs(first % 86400 - ENTRY) <= 15
    limits = [(lim, volts) for t, lim, volts, *_ in out if lim is not None]
    assert limits
    for lim, volts in limits:
        assert lim.ccl == 0 and lim.cvl <= 53.6
        if any(v is not None and 40 <= v <= 62 for v in volts):
            assert lim.dcl == 140, lim
            assert lim.soc is not None and lim.soc < 95
        else:
            assert lim.dcl == 0 and lim.reason == "no inverter voltage"
    assert not any(latched for *_, latched in out)
