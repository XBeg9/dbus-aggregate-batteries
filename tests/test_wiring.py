"""Emergency mode wired into the aggregator (spec 2026-11-24), with D-Bus, GLib and velib stubbed."""

import ast
import importlib.util
import json
import logging
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dbus-aggregate-batteries.py"
PACKS = ("com.victronenergy.battery.ttyUSB0", "com.victronenergy.battery.ttyUSB1")
VEBUS = "com.victronenergy.vebus.ttyS4"
MULTI = "com.victronenergy.multi.socketcan_can1"
ESS = "com.victronenergy.settings"


class DBusException(Exception):
    pass


def pack(n):
    p = {
        "/Connected": 1,
        "/ProductName": "SerialBattery(Seplos)",
        "/Serial": "Pack-0%d" % n,
        "/CustomName": "Pack-0%d" % n,
        "/System/NrOfCellsPerBattery": 16,
        "/Dc/0/Voltage": 53.0,
        "/Dc/0/Current": -5.0,
        "/Dc/0/Power": -265.0,
        "/InstalledCapacity": 280.0,
        "/ConsumedAmphours": -47.6,
        "/Capacity": 232.4,
        "/Soc": 83.0,
        "/TimeToGo": 3600,
        "/Dc/0/Temperature": 20.0,
        "/System/MaxTemperatureCellId": "T1",
        "/System/MaxCellTemperature": 21.0,
        "/System/MinTemperatureCellId": "T2",
        "/System/MinCellTemperature": 19.0,
        "/System/MaxVoltageCellId": "C1",
        "/System/MaxCellVoltage": 3.32,
        "/System/MinVoltageCellId": "C2",
        "/System/MinCellVoltage": 3.30,
        "/Voltages/Sum": 53.0,
        "/System/NrOfModulesOnline": 1,
        "/System/NrOfModulesOffline": 0,
        "/System/NrOfModulesBlockingCharge": 0,
        "/System/NrOfModulesBlockingDischarge": 0,
        "/Info/MaxChargeCurrent": 70.0,
        "/Info/MaxDischargeCurrent": 140.0,
        "/Info/MaxChargeVoltage": 55.2,
        "/Info/ChargeMode": "Bulk",
        "/Io/AllowToCharge": 1,
        "/Io/AllowToDischarge": 1,
        "/Io/AllowToBalance": 1,
    }
    p.update({"/Voltages/Cell%d" % c: 3.31 for c in range(1, 17)})
    for a in ("LowVoltage", "HighVoltage", "LowCellVoltage", "HighCellVoltage", "LowSoc", "HighChargeCurrent", "HighDischargeCurrent"):
        p["/Alarms/" + a] = 0
    for a in ("CellImbalance", "InternalFailure", "HighChargeTemperature", "LowChargeTemperature", "HighTemperature", "LowTemperature", "BmsCable"):
        p["/Alarms/" + a] = 0
    return p


def inverters():
    return {
        VEBUS: {"/Connected": 1, "/Dc/0/Voltage": 53.0, "/Dc/0/Current": -30.0},
        MULTI: {"/Connected": 1, "/Dc/0/Voltage": 53.2, "/Dc/0/Current": 20.0, "/Settings/Ess/MinimumSocLimit": 100},
        "com.victronenergy.solarcharger.ttyS2": {"/Connected": 1, "/Dc/0/Current": 4.0},
        ESS: {"/Settings/CGwacs/BatteryLife/MinimumSocLimit": 95},
    }


class World:
    """One dict behind the dbus monitor, VeDbusItemImport and list_names(), so a test changes the bus in one place."""

    def __init__(self):
        self.bus = {}
        self.timers = []
        self.t = 0.0


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World()

    class FakeConn:
        def list_names(self):
            return list(w.bus)

    class FakeService:
        def __init__(self, *a, **k):
            self.d = self._dbusobjects = {}
            self.fail = None  # a path whose write raises

        def add_path(self, path, value, *a, **k):
            self.d[path] = value

        def register(self):
            pass

        def __contains__(self, path):
            return path in self.d

        def __getitem__(self, path):
            return self.d[path]

        def __setitem__(self, path, value):
            if path not in self.d:
                raise KeyError(path)  # like the real service
            if path == self.fail:
                raise RuntimeError("publish failed")
            self.d[path] = value

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def del_tree(self, root):
            for p in list(self.d):
                if p.startswith(root + "/"):
                    del self.d[p]  # like velib: ServiceContext.del_tree -> __del__ -> _item_deleted drops the path

    class FakeImport:
        def __init__(self, bus, service, path, eventCallback=None, createsignal=True):
            if service not in w.bus:
                raise DBusException("no owner for " + service)
            v = w.bus[service].get(path)
            if isinstance(v, Exception):
                raise v
            self.v = v

        def get_value(self):
            return self.v

    class FakeMon:
        def get_value(self, service, path, default=None):
            v = w.bus.get(service, {}).get(path, default)
            if isinstance(v, Exception):
                raise v
            return v

        def get_service_list(self, classfilter=None):  # like velib: {name: device instance}, by the name's first three parts
            return {n: 0 for n in w.bus if classfilter is None or ".".join(n.split(".")[:3]) == classfilter}

    class FakeThread:
        def __init__(self, target):
            self.target = target

        def start(self):
            self.target()

    dbus = types.ModuleType("dbus")
    dbus.SystemBus = dbus.SessionBus = FakeConn
    dbus.exceptions = types.SimpleNamespace(DBusException=DBusException)
    gi = types.ModuleType("gi")
    gi.repository = types.ModuleType("gi.repository")
    gi.repository.GLib = types.SimpleNamespace(timeout_add_seconds=lambda s, fn: w.timers.append(fn.__name__))
    vedbus = types.ModuleType("vedbus")
    vedbus.VeDbusService, vedbus.VeDbusItemImport = FakeService, FakeImport
    dbusmon = types.ModuleType("dbusmon")
    dbusmon.DbusMon = lambda: types.SimpleNamespace(dbusmon=FakeMon())
    for name, m in {"dbus": dbus, "gi": gi, "gi.repository": gi.repository, "vedbus": vedbus, "dbusmon": dbusmon}.items():
        monkeypatch.setitem(sys.modules, name, m)

    # settings.py reads config.ini next to itself and exits on the default's NR_OF_BATTERIES = -1: run it on a copy
    shutil.copy(ROOT / "config.default.ini", tmp_path)
    (tmp_path / "config.ini").write_text("[DEFAULT]\nNR_OF_BATTERIES = 2\nNR_OF_CELLS_PER_BATTERY = 16\n")
    settings = types.ModuleType("settings")
    settings.__file__ = str(tmp_path / "settings.py")
    exec(compile((ROOT / "settings.py").read_text(), str(ROOT / "settings.py"), "exec"), settings.__dict__)
    monkeypatch.setitem(sys.modules, "settings", settings)

    spec = importlib.util.spec_from_file_location("dab_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.Thread = FakeThread
    mod._STATE_FILE_CHARGE = str(tmp_path / "storedvalue_charge")
    mod._STATE_FILE_EMERGENCY = str(tmp_path / "storedvalue_emergency")
    w.mod = mod

    def make():
        svc = mod.DbusAggBatService()
        svc._now = lambda: w.t
        return svc

    w.make = make
    return w


def tick(w, svc, n=1):
    for _ in range(n):
        assert svc._update() is True
        w.t += 1


def started(w):
    w.bus.update({PACKS[0]: pack(1), PACKS[1]: pack(2)}, **inverters())
    svc = w.make()
    assert svc._find_batteries() is False
    return svc


def test_no_sys_exit_after_register():
    allowed = {"__init__", "_find_settings", "main"}
    found = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and isinstance(child.func.value, ast.Name):
                if (child.func.value.id, child.func.attr) in {("sys", "exit"), ("tt", "sleep")}:
                    found.append((func, child.func.value.id + "." + child.func.attr, child.lineno))
            visit(child, name)

    visit(ast.parse(SRC.read_text()), None)
    assert found, "parser found nothing: the test is broken"
    assert [f for f in found if f[0] not in allowed] == []


def test_registers_with_emergency_safe_limits(world):
    b = world.make()._dbusservice  # before any tick
    assert (b["/Info/MaxChargeCurrent"], b["/Info/MaxChargeVoltage"], b["/Info/MaxDischargeCurrent"]) == (0, 53.6, 140)
    assert (b["/Io/AllowToCharge"], b["/Io/AllowToDischarge"], b["/EmergencyMode"]) == (0, 1, 0)


def test_registers_without_discharge_when_emergency_mode_is_off(world):
    sys.modules["settings"].EMERGENCY_MODE = False
    b = world.make()._dbusservice
    assert (b["/Info/MaxChargeCurrent"], b["/Info/MaxChargeVoltage"], b["/Info/MaxDischargeCurrent"]) == (0, 53.6, 0)
    assert b["/Io/AllowToDischarge"] == 0


def test_update_enters_emergency_and_publishes_table(world):
    svc = started(world)
    tick(world, svc)  # a normal tick: real SoC 83
    assert svc._dbusservice["/EmergencyMode"] == 0
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 140
    for p in PACKS:
        world.bus[p]["/Connected"] = 0
    world.bus[MULTI]["/Dc/0/Voltage"] = None  # a reading the monitor does not have counts as missing
    tick(world, svc, 11)
    b = svc._dbusservice
    assert b["/EmergencyMode"] == 1
    assert b["/EmergencyReason"] == "pack data lost"
    assert b["/Info/MaxChargeCurrent"] == 0
    assert b["/Info/MaxChargeVoltage"] == 53.0
    assert b["/Info/MaxDischargeCurrent"] == 140
    assert (b["/Io/AllowToCharge"], b["/Io/AllowToDischarge"]) == (0, 1)
    assert b["/Soc"] == 83  # min(last real 83, ESS minimum 95 - 1)
    assert b["/Dc/0/Voltage"] == 53.0
    assert b["/Dc/0/Current"] == -30.0 + 20.0 + 4.0
    assert b["/Dc/0/Power"] == pytest.approx(53.0 * -6.0)
    assert b["/Connected"] == 1
    assert b["/Alarms/BmsCable"] == 2
    assert all(b[p] == 0 for p in b.d if p.startswith("/Alarms/") and p != "/Alarms/BmsCable")
    for p in ("/System/MinCellVoltage", "/System/MaxCellVoltage", "/System/MinVoltageCellId", "/Voltages/Sum", "/Voltages/Diff"):
        assert p in b and b[p] is None, p  # still on the bus, unknown
    for p in ("/ConsumedAmphours", "/TimeToGo", "/Dc/0/Temperature", "/System/MinCellTemperature"):
        assert b[p] is None, p
    assert (b["/System/NrOfModulesOnline"], b["/System/NrOfModulesOffline"]) == (0, 2)
    assert b["/InstalledCapacity"] == 560


def test_grace_keeps_last_good_values(world):
    svc = started(world)
    tick(world, svc)
    for p in PACKS:
        world.bus[p]["/Connected"] = 0
    for _ in range(5):
        tick(world, svc)
        b = svc._dbusservice
        assert (b["/Info/MaxChargeCurrent"], b["/Info/MaxDischargeCurrent"], b["/Info/MaxChargeVoltage"]) == (140, 280, 55.2)
        assert b["/EmergencyMode"] == 0


def test_exception_in_normal_tick_counts_as_bad_data(world, caplog):
    svc = started(world)
    tick(world, svc)
    world.bus[PACKS[1]]["/Dc/0/Temperature"] = None  # the normal read raises
    tick(world, svc, 10)
    assert svc._dbusservice["/EmergencyMode"] == 0
    tick(world, svc)
    assert svc._dbusservice["/EmergencyMode"] == 1
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 0
    assert caplog.text.count("Local variables at error") == 1  # once per run of failures, not every second
    world.bus[PACKS[1]]["/Dc/0/Temperature"] = 20.0
    caplog.set_level("INFO")
    tick(world, svc)
    assert caplog.text.count("Pack reads work again after 11 failed ticks") == 1


def test_exception_in_emergency_tick_returns_true(world):
    svc = started(world)
    for p in PACKS:
        world.bus[p]["/Connected"] = 0
    tick(world, svc, 11)
    assert svc._emergency.active
    world.bus[VEBUS]["/Dc/0/Voltage"] = RuntimeError("bug")  # an inverter read that raises counts as missing
    assert svc._update() is True
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 0
    assert svc._dbusservice["/Info/MaxChargeVoltage"] == 53.0
    assert svc._dbusservice["/Dc/0/Voltage"] == 53.2  # the Multi RS alone
    svc._dbusservice["/Info/MaxChargeCurrent"] = 99  # proves the except path writes them
    svc._dbusservice["/Io/AllowToCharge"] = 1
    svc._dbusservice["/Info/MaxChargeVoltage"] = 99
    world.bus[PACKS[0]]["/Connected"] = RuntimeError("bug")  # the pack monitor raises: the whole tick fails
    assert svc._update() is True
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 0
    assert svc._dbusservice["/Io/AllowToCharge"] == 0
    assert svc._dbusservice["/Info/MaxChargeVoltage"] == 53.0  # the episode's pinned entry voltage


def test_persistent_publish_exception_enters_emergency(world):
    svc = started(world)
    tick(world, svc)
    svc._dbusservice.fail = "/System/NrOfCellsPerBattery"  # only the normal publish writes it
    tick(world, svc, 10)
    assert svc._dbusservice["/EmergencyMode"] == 0
    tick(world, svc)
    assert svc._dbusservice["/EmergencyMode"] == 1
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 0


def test_publish_exception_traceback_logged_once_per_run(world, caplog):
    svc = started(world)
    tick(world, svc)
    svc._dbusservice.fail = "/System/NrOfCellsPerBattery"
    with caplog.at_level(logging.INFO):
        tick(world, svc, 150)  # in and out of emergency mode while the publish keeps raising
        svc._dbusservice.fail = None
        tick(world, svc, 40)
    tracebacks = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert len(tracebacks) == 1
    assert caplog.text.count("Update works again after") == 1


def test_failure_run_ends_only_with_a_normal_publish_and_a_new_kind_is_logged(world, caplog):
    svc = started(world)
    tick(world, svc)
    svc._dbusservice.fail = "/System/NrOfCellsPerBattery"
    with caplog.at_level(logging.INFO):
        tick(world, svc, 3)
        for p in PACKS:
            world.bus[p]["/Connected"] = 0
        tick(world, svc, 3)  # grace ticks publish nothing: they must not end the run
        for p in PACKS:
            world.bus[p]["/Connected"] = 1
        tick(world, svc, 3)
        assert "Update works again" not in caplog.text
        assert len([r for r in caplog.records if r.exc_info and r.levelno >= logging.ERROR]) == 1
        normal = svc._update_normal

        def other():
            raise ValueError("another fault")

        svc._update_normal = other
        tick(world, svc)
        svc._update_normal = normal
        svc._dbusservice.fail = None
        tick(world, svc, 40)
    tracebacks = [r for r in caplog.records if r.exc_info and r.levelno >= logging.ERROR]
    assert [r.exc_info[0] for r in tracebacks] == [RuntimeError, ValueError]
    assert caplog.text.count("Update works again after") == 1


def test_normal_values_return_after_emergency(world, caplog):
    svc = started(world)
    tick(world, svc)
    for p in PACKS:
        world.bus[p]["/Connected"] = 0
    tick(world, svc, 11)
    assert svc._emergency.active
    for p in PACKS:
        world.bus[p]["/Connected"] = 1
    caplog.clear()
    tick(world, svc, 200)
    b = svc._dbusservice
    assert (b["/EmergencyMode"], b["/Info/MaxChargeCurrent"], b["/Io/AllowToCharge"], b["/Voltages/Sum"]) == (0, 140, 1, 53.0)
    assert "Update failed" not in caplog.text


def test_rediscovery_after_driver_restart(world):
    world.bus.update(inverters())
    svc = world.make()
    for _ in range(9):
        assert svc._find_batteries() is True
    assert svc._find_batteries() is False  # SEARCH_TRIALS reached: emergency mode, no exit
    assert svc._emergency.active
    assert world.timers.count("_update") == 1
    tick(world, svc)
    assert svc._dbusservice["/EmergencyMode"] == 1
    assert svc._dbusservice["/EmergencyReason"] == "start-up without packs"
    assert svc._dbusservice["/Info/MaxChargeVoltage"] == 53.0

    world.bus.update({PACKS[0]: pack(1), PACKS[1]: pack(2)})  # the driver is back
    tick(world, svc, 30)
    assert svc._dbusservice["/EmergencyMode"] == 1  # found within 10 s, 30 s of good data not yet over
    tick(world, svc, 15)
    assert svc._dbusservice["/EmergencyMode"] == 0
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 140
    assert svc._dbusservice["/System/MinCellVoltage"] == 3.30
    assert world.timers.count("_update") == 1


def test_pack_back_under_its_name_within_the_grace(world):
    svc = started(world)
    tick(world, svc, 9)
    gone = world.bus.pop(PACKS[1])  # a driver restart, across the 10 s listing at t=10
    tick(world, svc, 6)
    assert svc._dbusservice["/Info/MaxChargeCurrent"] == 140  # grace: last good values
    world.bus[PACKS[1]] = gone
    for _ in range(30):
        tick(world, svc)
        assert svc._dbusservice["/EmergencyMode"] == 0


def test_pack_back_under_a_new_name(world):
    svc = started(world)
    tick(world, svc)
    gone = world.bus.pop(PACKS[1])
    tick(world, svc, 11)
    assert svc._dbusservice["/EmergencyMode"] == 1
    world.bus["com.victronenergy.battery.ttyUSB2"] = gone
    tick(world, svc, 38)  # found at the listing at t=20, then 30 s of good data
    assert svc._dbusservice["/EmergencyMode"] == 1
    tick(world, svc)
    assert svc._dbusservice["/EmergencyMode"] == 0
    assert sorted(svc._batteries_dict.values()) == [PACKS[0], "com.victronenergy.battery.ttyUSB2"]


def test_no_blocking_imports_in_the_update_path():
    found = {
        f.name
        for f in ast.walk(ast.parse(SRC.read_text()))
        if isinstance(f, ast.FunctionDef)
        for c in ast.walk(f)
        if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "VeDbusItemImport"
    }
    assert found == {"_find_settings", "_callback_changed_custom_name"}  # the emergency reads use the dbus monitor's cache


def test_persisted_latch_keeps_discharge_shut_from_registration(world, tmp_path):
    (tmp_path / "storedvalue_emergency").write_text(json.dumps({"latched": True, "last_soc": 40.0}))
    world.bus.update(inverters())
    svc = world.make()
    b = svc._dbusservice  # before any tick: a restart must not reopen the latched stop
    assert (b["/Info/MaxChargeCurrent"], b["/Info/MaxDischargeCurrent"], b["/Io/AllowToDischarge"]) == (0, 0, 0)
    while svc._find_batteries():
        pass
    for _ in range(5):
        tick(world, svc)
        assert (b["/Info/MaxDischargeCurrent"], b["/Io/AllowToDischarge"]) == (0, 0)
    assert b["/EmergencyReason"] == "floor latched"


def test_pack_search_only_when_the_pack_services_change(world):
    world.bus.update(inverters())
    svc = world.make()
    while svc._find_batteries():
        pass
    calls = []
    search = svc._find_batteries
    svc._find_batteries = lambda: calls.append(world.t) or search()
    tick(world, svc, 60)
    assert len(calls) <= 1  # packs absent, names unchanged: not every 10 s (~25 log lines each)
    n = len(calls)
    world.bus[PACKS[0]] = pack(1)
    tick(world, svc, 10)
    assert len(calls) == n + 1


def test_missing_current_means_no_load_correction(world):
    svc = started(world)
    tick(world, svc)
    for p in PACKS:
        world.bus[p]["/Connected"] = 0
    world.bus[VEBUS].update({"/Dc/0/Voltage": 50.3, "/Dc/0/Current": -100.0})
    world.bus[MULTI].update({"/Dc/0/Voltage": 50.3, "/Dc/0/Current": 0.0})
    tick(world, svc, 45)
    b = svc._dbusservice
    assert b["/EmergencyMode"] == 1
    assert b["/Dc/0/Current"] == -96.0
    assert b["/Info/MaxDischargeCurrent"] == 140  # 50.3 V + 96 A x 8.2 mOhm is above the 50.4 V floor
    world.bus["com.victronenergy.solarcharger.ttyS2"]["/Dc/0/Current"] = None  # connected, reading missing
    tick(world, svc)
    assert b["/Dc/0/Current"] is None and b["/Dc/0/Power"] is None
    tick(world, svc, 30)
    assert b["/Info/MaxDischargeCurrent"] == 0  # no correction: 50.3 V is below the floor
    assert b["/EmergencyReason"] == "floor latched"


def test_latch_survives_while_normal_publish_keeps_failing(world, tmp_path):
    (tmp_path / "storedvalue_emergency").write_text(json.dumps({"latched": True, "last_soc": 40.0}))
    svc = started(world)
    for v in (VEBUS, MULTI):
        world.bus[v].update({"/Dc/0/Voltage": 49.5, "/Dc/0/Current": 0.0})  # below the 50.4 V floor
    svc._dbusservice.fail = "/System/NrOfCellsPerBattery"  # only the normal publish writes it: it raises every tick
    b = svc._dbusservice
    opened = []
    for _ in range(150):
        tick(world, svc)
        if b["/Info/MaxDischargeCurrent"] > 0:
            opened.append((world.t, b["/EmergencyMode"]))
    assert not opened, "discharge reopened with no successful normal publish: %s" % opened[:3]
    assert json.loads((tmp_path / "storedvalue_emergency").read_text())["latched"] is True
