"""Tunables. Every one is an operational knob, so every one is settable."""
import os
from dataclasses import dataclass


def _f(name, default):
    return float(os.environ.get(name, default))


def _i(name, default):
    return int(os.environ.get(name, default))


@dataclass
class Config:
    #: off (observe nothing, do nothing) | dry_run (observe, log, never act) | on
    mode: str = os.environ.get("MODE", "dry_run").strip()
    #: Seconds between reconciles.
    interval_s: int = _i("INTERVAL_S", 60)
    #: Demand must exceed capacity for this long before a node is woken.
    wake_sustain_s: int = _i("WAKE_SUSTAIN_S", 120)
    #: ...and must stay below it this long before one is slept. Deliberately
    #: asymmetric: waking is cheap and reversible, sleeping costs a cold start.
    sleep_sustain_s: int = _i("SLEEP_SUSTAIN_S", 1200)
    #: A node may not sleep within this long of becoming Ready. Stops a node
    #: that woke for a short burst thrashing straight back down.
    min_uptime_s: int = _i("MIN_UPTIME_S", 2700)
    #: Give up waiting for a node to become Ready after a wake.
    wake_timeout_s: int = _i("WAKE_TIMEOUT_S", 900)
    #: Give up waiting for work to finish, and hand the node back.
    drain_timeout_s: int = _i("DRAIN_TIMEOUT_S", 1800)
    #: Consecutive failed sleep RESTARTS before abandoning and backing off.
    max_sleep_attempts: int = _i("MAX_SLEEP_ATTEMPTS", 5)
    #: How long to refuse re-entering a sleep after abandoning one.
    sleep_cooldown_s: int = _i("SLEEP_COOLDOWN_S", 1800)
    #: Give up waiting for the post-wake warmup. The node is already in
    #: service by then, so this is a warning, not a failure.
    warmup_timeout_s: int = _i("WARMUP_TIMEOUT_S", 900)
    #: Fallback capacity when a node reports none, in the signal's unit.
    default_capacity: float = _f("DEFAULT_CAPACITY", 125.7)

    # -- scheduled maintenance wakeups -----------------------------------
    # A node that sleeps for weeks never sees a package update, a config
    # management run, or new firmware -- and the longer it stays dark the more
    # it has to catch up on when demand finally wakes it, which is precisely
    # when you least want a 40-minute upgrade run. These bring it up on a
    # schedule instead, briefly, on purpose.
    #: Wake a node that has been asleep this long. 0 disables the feature
    #: entirely, which is the default: it powers hardware on when nothing asked
    #: for it, and that should be a decision somebody made.
    maintenance_interval_s: int = _i("MAINTENANCE_INTERVAL_S", 0)
    #: How long a node stays up once it is Ready, measured from Ready rather
    #: than from power-on, so a slow boot does not eat the window.
    maintenance_window_s: int = _i("MAINTENANCE_WINDOW_S", 300)
    #: Spread of the per-node offset added to the interval. Nodes tend to fall
    #: asleep together when a cluster goes quiet, so without a stagger they
    #: also come due together -- and a rack that powers on in unison is a
    #: current spike your PSUs did not agree to.
    maintenance_stagger_s: int = _i("MAINTENANCE_STAGGER_S", 3600)
    #: Hard bound on how long one visit may HOLD a node, measured from
    #: power-on. Must cover a full boot and the window, or no visit can ever
    #: complete. Note what it does and does not promise: it bounds the visit,
    #: not the node. A node still NotReady when this fires is released rather
    #: than powered off -- see maintain() for why cutting power to a machine
    #: that might be mid-update is the one outcome worth burning watts to
    #: avoid.
    maintenance_timeout_s: int = _i("MAINTENANCE_TIMEOUT_S", 3600)

    def validate(self):
        if self.mode not in ("off", "dry_run", "on"):
            raise ValueError("MODE must be off|dry_run|on, got %r" % self.mode)
        self._validate_maintenance()
        if self.wake_sustain_s >= self.sleep_sustain_s:
            # Not fatal, but almost always a mistake: it makes the controller
            # sleep as readily as it wakes, and every cold start costs real
            # latency to whatever was queued.
            pass
        return self

    def _validate_maintenance(self):
        """Reject schedules that cannot work, rather than half-working.

        Every check here describes a combination that produces no visits at
        all, or a visit that never ends -- and both of those look exactly like
        "the feature is not enabled" from the outside, which is the worst way
        for a misconfiguration to present.
        """
        if self.maintenance_interval_s < 0:
            raise ValueError("MAINTENANCE_INTERVAL_S must be >= 0 (0 disables)")
        if not self.maintenance_interval_s:
            return                       # disabled; nothing else can be wrong
        if self.maintenance_window_s <= 0:
            raise ValueError("MAINTENANCE_WINDOW_S must be > 0 when "
                             "MAINTENANCE_INTERVAL_S is set")
        if self.maintenance_stagger_s < 0:
            raise ValueError("MAINTENANCE_STAGGER_S must be >= 0")
        if self.maintenance_interval_s <= self.maintenance_window_s:
            raise ValueError(
                "MAINTENANCE_INTERVAL_S (%d) must exceed MAINTENANCE_WINDOW_S "
                "(%d), or a node would never be asleep long enough to come due"
                % (self.maintenance_interval_s, self.maintenance_window_s))
        floor = self.maintenance_window_s + self.wake_timeout_s
        if self.maintenance_timeout_s < floor:
            raise ValueError(
                "MAINTENANCE_TIMEOUT_S (%d) must be at least "
                "MAINTENANCE_WINDOW_S + WAKE_TIMEOUT_S (%d), or the bound "
                "fires before a node that booted slowly has had its window"
                % (self.maintenance_timeout_s, floor))
