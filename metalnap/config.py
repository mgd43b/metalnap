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

    def validate(self):
        if self.mode not in ("off", "dry_run", "on"):
            raise ValueError("MODE must be off|dry_run|on, got %r" % self.mode)
        if self.wake_sustain_s >= self.sleep_sustain_s:
            # Not fatal, but almost always a mistake: it makes the controller
            # sleep as readily as it wakes, and every cold start costs real
            # latency to whatever was queued.
            pass
        return self
