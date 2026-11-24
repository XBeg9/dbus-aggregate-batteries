"""Emergency mode: pure state machine for lost pack data (spec 2026-11-24). No D-Bus, no GLib."""

import json
import logging
from dataclasses import asdict, dataclass
from typing import Callable, Optional, Sequence


@dataclass(frozen=True)
class EmergencyConfig:
    enabled: bool = True
    enter_s: float = 10
    exit_s: float = 30
    floor_v: float = 50.4
    floor_s: float = 30
    r_ohm: float = 0.0082
    cvl_cap_v: float = 53.6
    dcl_a: float = 140.0
    v_min: float = 40.0
    v_max: float = 62.0


@dataclass
class Stored:
    latched: bool = False
    last_soc: Optional[float] = None


@dataclass(frozen=True)
class Limits:
    ccl: float
    cvl: float
    dcl: float
    allow_charge: int
    allow_discharge: int
    soc: Optional[float]
    reason: str


def load_state(path: str) -> Stored:
    try:
        with open(path) as f:
            d = json.load(f)
        soc = d.get("last_soc")
        return Stored(latched=bool(d.get("latched", False)), last_soc=None if soc is None else float(soc))
    except Exception:  # missing, unreadable or corrupt: not latched, no last SoC
        return Stored()


class Emergency:
    def __init__(self, cfg: EmergencyConfig, stored: Stored, write: Callable[[str], None]):
        self.cfg = cfg
        self.stored = stored
        self._write = write
        self.active = False
        self._startup = False
        self._latched = stored.latched
        self._bad_since: Optional[float] = None
        self._good_since: Optional[float] = None
        self._below_since: Optional[float] = None
        self._cvl: Optional[float] = None
        self._saved_soc = None if stored.last_soc is None else round(stored.last_soc)

    @property
    def cvl(self) -> float:
        """The charge voltage emergency mode publishes: the pinned entry voltage, or the cap before one is known."""
        return self._cvl if self._cvl is not None else self.cfg.cvl_cap_v

    def _persist(self):
        self.stored.latched = self._latched
        try:
            self._write(json.dumps(asdict(self.stored)))
        except Exception as ex:
            logging.error("emergency: state file write failed: %s", ex)

    def _enter(self, startup: bool, bank_v: Optional[float]):
        self.active = True
        self._startup = startup
        self._good_since = self._below_since = self._cvl = None
        logging.warning("emergency mode ON (%s), bank %s V", "start-up without packs" if startup else "pack data lost", bank_v)

    def start_in_emergency(self, now: float) -> None:
        if self.active:  # must not re-pin the entry CVL
            return
        self._enter(True, None)

    def step(
        self,
        now: float,
        packs_ok: bool,
        voltages: Sequence[Optional[float]],
        current_a: Optional[float],
        min_socs: Sequence[Optional[float]],
        real_soc: Optional[float],
    ) -> Optional[Limits]:
        c = self.cfg
        valid = [v for v in voltages if v is not None and c.v_min <= v <= c.v_max]
        bank_v = min(valid) if valid else None

        if not self.active:
            if packs_ok:
                self._bad_since = None
                if self._good_since is None:
                    self._good_since = now
                if self._latched and now - self._good_since >= c.exit_s:  # a persisted latch ends with normal mode
                    self._latched = False
                    self._persist()
                if real_soc is not None:
                    self.stored.last_soc = real_soc
                    if round(real_soc) != self._saved_soc:
                        self._saved_soc = round(real_soc)
                        self._persist()
            else:
                self._good_since = None
                if self._bad_since is None:
                    self._bad_since = now
                if now - self._bad_since >= c.enter_s:
                    self._enter(False, bank_v)
            if not self.active:
                return None
        elif packs_ok:
            if self._good_since is None:
                self._good_since = now
            if now - self._good_since >= c.exit_s:
                logging.warning("emergency mode OFF, bank %s V", bank_v)
                self.active = self._startup = False
                self._bad_since = self._good_since = self._below_since = self._cvl = None
                if self._latched:
                    self._latched = False
                    self._persist()
                return None
        else:
            self._good_since = None

        if self._cvl is None and bank_v is not None:
            self._cvl = min(c.cvl_cap_v, bank_v)

        if bank_v is None:
            self._below_since = None
        else:
            corrected = bank_v + (max(0.0, -current_a) * c.r_ohm if current_a is not None else 0.0)
            if corrected >= c.floor_v:
                self._below_since = None
            else:
                if self._below_since is None:
                    self._below_since = now
                if not self._latched and now - self._below_since >= c.floor_s:
                    self._latched = True
                    logging.warning("emergency discharge stop (floor latched), bank %s V, corrected %.2f V", bank_v, corrected)
                    self._persist()

        dcl = 0.0 if self._latched or bank_v is None else c.dcl_a
        known = [s for s in min_socs if s is not None]
        last = self.stored.last_soc
        if known and last is not None:
            soc = min(last, min(known) - 1)
        elif known:
            soc = min(known) - 1
        else:
            soc = last

        if self._latched:
            reason = "floor latched"
        elif bank_v is None:
            reason = "no inverter voltage"
        elif self._startup:
            reason = "start-up without packs"
        else:
            reason = "pack data lost"
        return Limits(0.0, self.cvl, dcl, 0, 1 if dcl > 0 else 0, soc, reason)
