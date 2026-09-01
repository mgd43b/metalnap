"""
Simulation harness: a fake cluster, fake BMCs, randomised load, and invariants
checked after every tick.

Why this exists. The controller this was extracted from shipped eight bugs to
production. Its unit suite caught ZERO of them: three were found in production,
two by external review, two by re-reading the code, one by an operator noticing
the numbers did not add up. Every one lived in a SEQUENCE -- a restart then a
sleep, a warm-pool worker landing then a sleep, demand oscillating across three
ticks. A test that calls tick() once and asserts cannot see any of them.

Seeded, so a failure reproduces exactly.

The cluster model is faithful on the points a controller gets wrong:

  * an idle worker NEVER exits by itself. Only finishing work, or an explicit
    release, removes one.
  * power transitions take time, so the controller observes NotReady-but-
    powered and Ready-but-cordoned intermediate states.
  * an operator periodically takes a node for maintenance, cordoning it
    WITHOUT the controller's annotation.
  * demand is phased and, in one mode, deliberately shaped to flicker: parked
    just below one node's worth while saturation toggles around a ceiling.

Four things this harness got WRONG before it got them right, each of which made
it report OK while testing nothing:

  * per-tick random demand never held below capacity for the consecutive ticks
    a sleep needs, so the sleep path never ran at all.
  * the controller catches its own exceptions and logs them, so defects arrive
    as log lines, not tracebacks. It reads the log stream.
  * a seam left unstubbed made every release fail, so no sleep ever completed
    -- and that was reported as a controller bug.
  * liveness demanded a sleep from nodes holding hung work, i.e. demanded the
    controller destroy it.

Run: python3 -B tests/sim.py [--seeds N] [--ticks N]
"""
import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalnap import Config, Controller           # noqa: E402
from metalnap.types import NodeState              # noqa: E402

BOOT_S = 150.0
SHUTDOWN_S = 60.0
CAPACITY = 125.7


class InvariantError(AssertionError):
    pass


class Worker:
    __slots__ = ("name", "node", "work", "ticks_left")

    def __init__(self, name, node, work=None, ticks_left=0):
        self.name, self.node, self.work = name, node, work
        self.ticks_left = ticks_left


class Node:
    def __init__(self, name, powered=True, ready=True):
        self.name, self.powered, self.ready = name, powered, ready
        self.cordoned, self.ours, self.change_at = False, None, None
        self.ready_since = 0.0


class Sim:
    """The fake world, and the four seams the controller plugs into."""

    def __init__(self, seed, ticks, cfg=None):
        self.rnd = random.Random(seed)
        self.seed, self.ticks = seed, ticks
        self.t = 1_000_000.0
        self.cfg = cfg or Config(mode="on")
        self.nodes = {"a": Node("a"), "b": Node("b", powered=False, ready=False)}
        self.workers, self.next_id = [], 0
        self.demand, self.saturated = 0.0, 0
        self.busy_phase, self.big, self.capped = True, True, False
        self.phase_left, self.quiet_ticks, self.busy_ticks = 30, 0, 0
        self.human_held, self.need_window = set(), []
        self.log = []
        # invariant violations, collected rather than raised so the tick that
        # caused them finishes and the report shows full context
        self.killed_work, self.powered_off_busy, self.stomped = [], [], []
        self.blocked = None
        self.zombie_since = {}

    # -- clock -----------------------------------------------------------
    def now(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        for n in self.nodes.values():
            if n.change_at is not None and self.t >= n.change_at:
                was = n.ready
                n.ready = n.powered
                if n.ready and not was:
                    n.ready_since = self.t
                n.change_at = None

    # -- NodeSource ------------------------------------------------------
    def state(self, name):
        n = self.nodes.get(name)
        if n is None:
            return None
        return NodeState(ready=n.ready, cordoned=n.cordoned,
                         ours=n.ours is not None,
                         ready_since=n.ready_since if n.ready else None,
                         capacity=CAPACITY,
                         # durable: survives the controller restarts injected
                         # below, which is the whole point of it
                         ours_since=n.ours)

    def set_cordon(self, name, cordoned):
        n = self.nodes[name]
        if name in self.human_held and not cordoned:
            self.stomped.append((self.t, name, "uncordoned an operator"))
        n.cordoned = cordoned
        n.ours = self.t if cordoned else None

    # -- PowerBackend ----------------------------------------------------
    def power_state(self, name):
        return "on" if self.nodes[name].powered else "off"

    def power_on(self, name):
        n = self.nodes[name]
        if not n.powered:
            n.powered = True
            n.change_at = self.t + BOOT_S

    def soft_off(self, name):
        if name in self.human_held:
            self.stomped.append((self.t, name, "powered off while held"))
        for w in self.workers:
            if w.node == name and w.work:
                self.powered_off_busy.append((self.t, name, w.name, w.work))
        n = self.nodes[name]
        if n.powered:
            n.powered = False
            n.change_at = self.t + SHUTDOWN_S
        self.workers = [w for w in self.workers if w.node != name]

    # -- DemandSignal ----------------------------------------------------
    def shortfall(self):
        return self.demand

    def saturated_units(self):
        return self.saturated

    # -- DrainPolicy -----------------------------------------------------
    def _on(self, node):
        return [w for w in self.workers if w.node == node]

    def busy(self, node):
        return [w.name for w in self._on(node) if w.work]

    def idle(self, node):
        listed = [w for w in self._on(node) if not w.work]
        # THE RACE, injected at listing time: work lands in some units between
        # this listing and the release that follows. It happens here, not
        # inside holds_work(), so that a controller which never calls
        # holds_work() still meets the race -- and destroys work, and is
        # caught. Injecting it inside the check made the missing check
        # invisible, which is a fine way to ship a harness that proves nothing.
        for w in listed:
            if self.rnd.random() < 0.15:
                w.work = "late-" + w.name
                w.ticks_left = self.rnd.randint(2, 6)
        return [w.name for w in listed]

    def residual(self, node):
        return [w.name for w in self._on(node)]

    def holds_work(self, unit):
        """Truthful current read -- the race is injected in idle()."""
        for w in self.workers:
            if w.name == unit:
                return bool(w.work)
        return False

    def release(self, unit):
        for w in self.workers:
            if w.name == unit and w.work:
                self.killed_work.append((self.t, unit, w.work))
        self.workers = [w for w in self.workers if w.name != unit]

    # -- workload --------------------------------------------------------
    def step_workload(self):
        for w in list(self.workers):
            if w.work:
                w.ticks_left -= 1
                if w.ticks_left <= 0:
                    self.workers.remove(w)   # finishing work removes the unit

        schedulable = [n for n in self.nodes.values()
                       if n.ready and not n.cordoned]
        if schedulable and self.rnd.random() < 0.5:
            for _ in range(self.rnd.randint(0, 3)):
                n = self.rnd.choice(schedulable)
                self.next_id += 1
                busy = self.rnd.random() < 0.7
                # 1 in 25 hangs. Without work that outlives the drain timeout
                # that branch never executes and a bug in it cannot be found.
                stuck = busy and self.rnd.random() < 0.04
                self.workers.append(Worker(
                    "w%d" % self.next_id, n.name,
                    work="work-%d" % self.next_id if busy else None,
                    ticks_left=(10 ** 6 if stuck
                                else self.rnd.randint(1, 6)) if busy else 0))

        # An operator takes a node for maintenance, and later releases it.
        for n in self.nodes.values():
            if n.name in self.human_held:
                if self.rnd.random() < 0.03:
                    self.human_held.discard(n.name)
                    n.cordoned, n.ours = False, None
            elif not n.cordoned and n.ready and self.rnd.random() < 0.01:
                self.human_held.add(n.name)
                n.cordoned, n.ours = True, None      # NOT ours: a human did it

        # PHASED demand. Rerolling every tick never held demand below capacity
        # for the consecutive ticks a sleep needs, so the sleep path never ran.
        self.phase_left -= 1
        if self.phase_left <= 0:
            self.busy_phase = not self.busy_phase
            self.big = self.rnd.random() < 0.4
            self.capped = (not self.big) and self.rnd.random() < 0.5
            # Quiet phases must be able to OUTLAST the liveness budget
            # (~120 ticks with production timers) or that invariant can never
            # fire. Capping phases at 90 made it unreachable.
            self.phase_left = (self.rnd.randint(130, 220) if not self.busy_phase
                               else self.rnd.randint(45, 90))
        if not self.busy_phase:
            self.demand, self.saturated = 0.0, 0
            self.quiet_ticks += 1
            self.busy_ticks = 0
            return
        self.quiet_ticks = 0
        self.busy_ticks += 1
        if self.capped:
            # The flicker shape. Demand parks just BELOW one node's worth, so
            # it alone asks for one node and only saturation pushes it to two
            # -- and saturation toggles, because a queue hovering at its
            # ceiling crosses the threshold back and forth. That keeps every
            # want=2 run to one or two ticks, which is what starves a timer
            # needing three. Independent randomness produces long runs instead,
            # and a broken controller wakes during them.
            self.demand = self.rnd.uniform(0.55, 0.95) * CAPACITY
            if self.rnd.random() < 0.75:
                self.saturated = 0 if self.saturated else 1
        elif self.big:
            self.demand = self.rnd.choice([260.0, 300.0, 280.0])
            self.saturated = self.rnd.choice([0, 1, 1, 2])
        else:
            self.demand = self.rnd.choice([8.0, 40.0, 100.0, 130.0])
            self.saturated = self.rnd.choice([0, 1, 1, 2])

    # -- invariants ------------------------------------------------------
    def check(self, tick):
        def fail(msg):
            raise InvariantError(
                "seed=%d tick=%d t=%.0f: %s\n  nodes=%s\n  workers=%s"
                % (self.seed, tick, self.t, msg,
                   {k: (v.powered, v.ready, v.cordoned, v.ours is not None)
                    for k, v in self.nodes.items()},
                   [(w.name, w.node, w.work) for w in self.workers]))

        # ---- SAFETY ----
        if self.powered_off_busy:
            fail("powered off a node running work: %s"
                 % (self.powered_off_busy[0],))
        if self.killed_work:
            fail("released a unit that was executing work: %s"
                 % (self.killed_work[0],))
        if self.stomped:
            fail("overrode an operator's maintenance cordon: %s"
                 % (self.stomped[0],))
        if self.blocked is not None:
            fail("tick() called time.sleep(%s) -- the reconcile loop must "
                 "never block" % self.blocked)

        # The controller catches its own exceptions and logs them, so a defect
        # arrives here as a log line and never as a traceback.
        for _t, lvl, msg in self.log[-60:]:
            if "tick failed" in msg or "phase step failed" in msg:
                fail("controller swallowed an internal error: %r" % msg)

        for n in self.nodes.values():
            # Powered, Ready and cordoned is NEITHER desired state: full power,
            # zero service. Brief is fine (mid-sleep); indefinite is a livelock.
            # A node an operator holds is exempt -- that is what maintenance
            # looks like, and the controller is right to leave it.
            zombie = (n.powered and n.ready and n.cordoned
                      and n.name not in self.human_held)
            if zombie:
                self.zombie_since.setdefault(n.name, self.t)
                held = self.t - self.zombie_since[n.name]
                budget = (self.cfg.drain_timeout_s
                          + self.cfg.sleep_cooldown_s + 600)
                if held > budget:
                    fail("%s cordoned+powered+Ready for %.0fs (budget %.0fs)"
                         % (n.name, held, budget))
            else:
                self.zombie_since.pop(n.name, None)
            if not n.powered and n.ready and not n.cordoned:
                fail("%s is powered off but Ready and schedulable" % n.name)

        # ---- LIVENESS ----
        # Safety alone is satisfied by a controller that does nothing, and the
        # first version of this harness reported OK across 250 ticks while
        # never sleeping a node once.
        # The controller takes ONE corrective action per tick, so nodes sleep
        # sequentially: the last one waits behind every drain before it. Budget
        # accordingly, or this invariant fails on correct behaviour whenever
        # more than one node needs to go down.
        settle = ((self.cfg.min_uptime_s + self.cfg.sleep_sustain_s
                   + len(self.nodes) * self.cfg.drain_timeout_s)
                  / self.cfg.interval_s) + 25
        if self.quiet_ticks > settle:
            drainable = [n for n in self.nodes.values()
                         if n.name not in self.human_held
                         and not any(w.node == n.name and w.work
                                     for w in self.workers)]
            if drainable and all(n.powered for n in drainable):
                fail("no drainable node powered off after %d quiet ticks "
                     "(budget %d)" % (self.quiet_ticks, settle))

        # Sustained demand must actually produce a node. A WINDOWED MAJORITY,
        # not a run length: the signal flickers by nature, so a counter that
        # resets on any dip measures the flicker -- which is the mistake the
        # controller itself once made -- and a +1/-1 decay cancels exactly
        # under a 50/50 oscillation and never fires either.
        need = min(len(self.nodes),
                   math.ceil((self.demand + self.saturated * CAPACITY)
                             / CAPACITY))
        powered = sum(1 for n in self.nodes.values() if n.powered)
        self.need_window.append(need > powered)
        if len(self.need_window) > 40:
            self.need_window.pop(0)
        if (len(self.need_window) == 40 and sum(self.need_window) >= 16
                and any(not n.powered and n.name not in self.human_held
                        for n in self.nodes.values())):
            fail("demand exceeded powered capacity in %d of the last 40 ticks "
                 "but a node is still off" % sum(self.need_window))

    # -- driver ----------------------------------------------------------
    def run(self, restart_prob=0.02):
        c = Controller(nodes=["a", "b"], node_source=self, power=_Power(self),
                       signal=self, drain=self, config=self.cfg,
                       log=lambda lvl, msg, **kv: self.log.append(
                           (self.t, lvl, msg)),
                       clock=self.now)
        import time as _time
        real_sleep = _time.sleep
        # Recording rather than raising: the controller wraps its phase steps
        # in try/except, so an exception thrown from in here is swallowed.
        _time.sleep = lambda s: setattr(self, "blocked", s)
        try:
            for i in range(self.ticks):
                self.step_workload()
                if self.rnd.random() < restart_prob:
                    c.st = {}          # a restart loses in-memory state
                self.blocked = None
                c.tick()
                self.check(i)
                self.advance(self.cfg.interval_s)
        finally:
            _time.sleep = real_sleep
        return self


class _Power:
    def __init__(self, sim):
        self.sim = sim

    def state(self, name):
        return self.sim.power_state(name)

    def on(self, name):
        self.sim.power_on(name)

    def soft_off(self, name):
        self.sim.soft_off(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=500)
    ap.add_argument("--seeds", type=int, default=60)
    a = ap.parse_args()
    failures = []
    for seed in range(a.seeds):
        try:
            Sim(seed, a.ticks).run()
        except InvariantError as e:
            failures.append(str(e))
        except Exception as e:                        # noqa: BLE001
            failures.append("seed=%d unhandled %s: %s"
                            % (seed, type(e).__name__, e))
    total = a.seeds * a.ticks
    if failures:
        print("FAILED  %d/%d seeds  (%d ticks simulated)"
              % (len(failures), a.seeds, total))
        for f in failures[:5]:
            print("\n" + f)
        return 1
    print("OK      %d seeds x %d ticks = %d ticks, all invariants held"
          % (a.seeds, a.ticks, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
