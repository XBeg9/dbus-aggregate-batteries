import os

import pytest

from functions import Functions

CVLS = [55.2, 54.0]


@pytest.mark.parametrize(
    "keep_max_cvl, charge_modes, expected",
    [
        (True, ["Bulk", "Absorption"], 55.2),
        (True, ["Bulk │ Linear Mode", "Absorption │ Linear Mode"], 55.2),
        # a pack that finished must not be charged back up at the other pack's absorption voltage (2026-10-23: the
        # bank CVL swung 54.0 <-> 55.2 every ~90 s and the other pack's absorption timer kept resetting)
        (True, ["Bulk", "Float"], 54.0),
        (True, ["Absorption │ Linear Mode", "Float Transition │ Linear Mode"], 54.0),
        (True, ["Absorption", "Float\nTransition"], 54.0),
        (True, ["Bulk", "Float (Cell OVP)"], 54.0),
        # a driver that is still starting publishes no charge mode yet; it could hide a Cell OVP
        (True, ["Bulk", None], 54.0),
        # a battery that has no /Info/ChargeMode at all
        (True, [None, None], 54.0),
        (False, ["Bulk", "Float"], 54.0),
        (False, ["Bulk", None], 54.0),
    ],
)
def test_max_charge_voltage(keep_max_cvl, charge_modes, expected):
    assert Functions()._max_charge_voltage(CVLS, charge_modes, keep_max_cvl) == expected


def test_update_chooses_the_charge_voltage_with_max_charge_voltage():
    # _update needs a live D-Bus to run, so check its source: the inline "Cell OVP" check it replaced raised
    # TypeError outside the try block on a None charge mode and froze the aggregate.
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dbus-aggregate-batteries.py")) as f:
        source = f.read()
    assert "self._fn._max_charge_voltage(MaxChargeVoltage_list, ChargeMode_list, settings.KEEP_MAX_CVL)" in source
    assert '"Cell OVP" in item' not in source
