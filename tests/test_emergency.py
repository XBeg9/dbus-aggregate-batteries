import json

from emergency import Emergency, EmergencyConfig, Stored, load_state


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, content):
        self.calls.append(content)


def make(stored=None, write=None):
    return Emergency(EmergencyConfig(r_ohm=0.0056), stored or Stored(), write or Rec())


def tick(e, t, ok, v=(53.0, 53.1), i=-20.0, mins=(95, 95), soc=83.0):
    return e.step(t, ok, v, i, mins, soc)


def enter(e, t0=0):
    for t in range(t0, t0 + 10):
        assert tick(e, t, False) is None
    return tick(e, t0 + 10, False)


def test_entry_after_10s_not_before():
    e = make()
    lim = enter(e)
    assert lim.ccl == 0 and lim.dcl == 140 and lim.allow_charge == 0 and lim.allow_discharge == 1
    assert lim.reason == "pack data lost" and e.active


def test_good_tick_resets_entry():
    e = make()
    for t in range(0, 9):
        assert tick(e, t, False) is None
    assert tick(e, 9, True) is None
    for t in range(10, 19):
        assert tick(e, t, False) is None


def test_exit_after_30s_good():
    e = make()
    enter(e)
    for t in range(100, 130):
        assert tick(e, t, True) is not None
    assert tick(e, 130, True) is None
    assert not e.active


def test_flapping_never_toggles():
    e = make()
    for t in range(0, 120):
        assert tick(e, t, (t // 3) % 2 == 0) is None
    assert not e.active
    e = make()
    enter(e)
    for t in range(100, 220):
        assert tick(e, t, (t // 3) % 2 == 0) is not None
    assert e.active


def test_cvl_is_entry_voltage_capped():
    e = make()
    for t in range(10):
        tick(e, t, False, v=(52.9, 53.2))
    assert tick(e, 10, False, v=(52.9, 53.2)).cvl == 52.9
    assert tick(e, 11, False, v=(54.0, 54.0)).cvl == 52.9
    e = make()
    for t in range(10):
        tick(e, t, False, v=(55.0,))
    assert tick(e, 10, False, v=(55.0,)).cvl == 53.6


def test_floor_load_corrected_latch():
    e = make()
    enter(e)
    for t in range(100, 160):
        assert tick(e, t, False, v=(50.0,), i=-200.0).dcl == 140
    w = Rec()
    e = make(write=w)
    enter(e)
    for t in range(100, 130):
        assert tick(e, t, False, v=(50.2,), i=-10.0).dcl == 140  # 30 ticks: 0..29 s
    lim = tick(e, 130, False, v=(50.2,), i=-10.0)
    assert lim.dcl == 0 and lim.allow_discharge == 0 and lim.reason == "floor latched"
    assert json.loads(w.calls[-1])["latched"] is True


def test_latch_survives_restart():
    e = make(Stored(latched=True))
    e.start_in_emergency(0)
    lim = tick(e, 0, False, v=(53.0,))
    assert lim.dcl == 0 and lim.reason == "floor latched"


def test_boot_without_voltage_never_latches():
    w = Rec()
    e = make(write=w)
    e.start_in_emergency(0)
    for t in range(300):
        lim = tick(e, t, False, v=(None, None))
        assert lim.dcl == 0 and lim.reason == "no inverter voltage"
    assert not w.calls
    assert tick(e, 300, False, v=(53.0,)).dcl == 140


def test_out_of_range_voltage_is_missing():
    e = make()
    e.start_in_emergency(0)
    lim = tick(e, 0, False, v=(0.0, 53.0))
    assert lim.dcl == 140 and lim.cvl == 53.0
    e = make()
    e.start_in_emergency(0)
    for t in range(100):
        lim = tick(e, t, False, v=(70.0, None))
        assert lim.dcl == 0
    assert not e._latched


def test_soc_rule():
    def soc(mins, last):
        e = make(Stored(last_soc=last))
        e.start_in_emergency(0)
        return e.step(0, False, (53.0,), -1.0, mins, None).soc

    assert soc((95, 100), 83) == 83
    assert soc((95, 100), 97) == 94
    assert soc((None, None), 83) == 83
    assert soc((100, 100), None) == 99
    assert soc((None, None), None) is None
    assert soc((0, 95), 40) == 0  # an ESS minimum of 0 never gives -1
    assert soc((0, None), None) == 0


def test_last_soc_only_updates_in_normal_mode():
    w = Rec()
    e = make(write=w)
    tick(e, 0, True, soc=80.2)
    assert e.stored.last_soc == 80.2 and len(w.calls) == 1
    tick(e, 1, True, soc=80.4)
    assert len(w.calls) == 1  # same rounded integer
    tick(e, 2, False, soc=10.0)  # grace
    tick(e, 3, True, soc=80.4)  # good tick resets the grace
    enter(e, 4)
    tick(e, 100, True, soc=10.0)
    assert e.stored.last_soc == 80.4


def test_state_file_roundtrip_and_corrupt(tmp_path):
    assert load_state(str(tmp_path / "none")) == Stored()
    bad = tmp_path / "bad"
    bad.write_text("{garbage")
    assert load_state(str(bad)) == Stored()
    ok = tmp_path / "ok"
    ok.write_text(json.dumps({"latched": True, "last_soc": 71.5}))
    assert load_state(str(ok)) == Stored(latched=True, last_soc=71.5)


def test_write_failure_is_ignored():
    calls = []

    def boom(c):
        calls.append(c)
        raise OSError("disk")

    e = make(write=boom)
    assert tick(e, 0, True, soc=80.0) is None
    assert calls and e.stored.last_soc == 80.0
    enter(e, 1)
    n = len(calls)
    for t in range(100, 130):
        tick(e, t, False, v=(50.2,), i=-10.0)
    lim = tick(e, 130, False, v=(50.2,), i=-10.0)
    assert len(calls) > n and lim.dcl == 0


def test_exit_keeps_latch_until_30s_of_normal_mode():
    w = Rec()
    e = make(Stored(latched=True), w)
    enter(e)
    for t in range(100, 131):
        tick(e, t, True)
    assert not e.active and e.latched  # a failing first normal publish must re-enter latched
    lim = enter(e, 200)
    assert lim.dcl == 0 and lim.reason == "floor latched"


def test_persisted_latch_clears_in_normal_mode_after_exit_s():
    w = Rec()
    e = make(Stored(latched=True), w)
    for t in range(0, 30):  # 0..29 s: 29 s elapsed at the last tick
        assert tick(e, t, True, soc=None) is None
    assert e._latched and not w.calls
    assert tick(e, 30, True, soc=None) is None
    assert not e._latched and json.loads(w.calls[-1])["latched"] is False
    lim = enter(e, 31)
    assert lim.dcl == 140 and lim.reason == "pack data lost"


def test_start_in_emergency_when_active_keeps_cvl():
    e = make()
    for t in range(10):
        tick(e, t, False, v=(52.9,))
    assert tick(e, 10, False, v=(52.9,)).cvl == 52.9
    e.start_in_emergency(11)
    assert tick(e, 11, False, v=(54.0,)).cvl == 52.9


def test_bad_real_soc_is_ignored():
    w = Rec()
    e = make(write=w)
    assert tick(e, 0, True, soc=80.0) is None
    for t, bad in enumerate((float("nan"), float("inf"), 150.0, -5.0), 1):
        assert tick(e, t, True, soc=bad) is None
        assert e.stored.last_soc == 80.0
    assert all(json.loads(c)["last_soc"] == 80.0 for c in w.calls)


def test_bad_stored_soc_loads_as_none(tmp_path):
    p = tmp_path / "st"
    for bad in ("NaN", "Infinity", "150", "-5", '"abc"'):
        p.write_text('{"latched": true, "last_soc": %s}' % bad)
        assert load_state(str(p)) == Stored(latched=True, last_soc=None), bad
    p.write_text('{"latched": "false", "last_soc": 40}')
    assert load_state(str(p)) == Stored(latched=False, last_soc=40.0)


def test_constructs_from_bad_stored_soc():
    for bad in (float("nan"), float("inf"), 150.0, -5.0):
        e = make(Stored(latched=True, last_soc=bad))
        assert e.latched and e.stored.last_soc is None
