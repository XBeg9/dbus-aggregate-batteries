import pytest

from functions import Functions

CVLS = [55.2, 54.0]


@pytest.mark.parametrize(
    "keep_max_cvl, charge_modes, expected",
    [
        (True, ["Bulk", "Float"], 55.2),
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
