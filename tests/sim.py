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
  * a node woken for a SCHEDULED maintenance visit sometimes reboots into the
    update it just installed, going NotReady in the middle of its own window.
    Cutting power there is how a routine update becomes an unbootable
    machine, so it is modelled and asserted rather than assumed.
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

MEASURED DETECTION, by reintroducing each real bug and counting failing seeds
(60 seeds x 900 ticks):

    busy work ignored when draining        60/60
    controller never sleeps anything       34/60
    no pre-release re-check                21/60
    cordon timestamp re-stamped             7/60
    idle units never released               4/60
    saturation signal ignored               3/60
    wake timer reset by a flickering signal  2/60
    in-flight operation ignores a cordon     1/60

and for scheduled maintenance, on the 30 of those 60 seeds that run it:

    power cut to a node rebooting mid-visit 30/30
    a maintenance window that never closes  30/30
    the schedule silently stops firing      28/30
    visits run in parallel                   0/30
    visits ignore unmet demand               0/30
    a failed visit is retried every tick     0/30
    a visit leaves the node schedulable      0/30

The four zeroes are not gaps in cover, they are gaps in THIS harness: each is
caught deterministically in test_controller.py, and each describes a
priority or scheduling mistake rather than a safety one -- the sim would need a
model of what the fleet ought to be doing, not just what it is doing, to see
them. The two 30/30 rows are the ones that destroy hardware, and they are the
ones this harness is good at.

The low-rate rows are BACKSTOPS, not primary cover. Anything guarding running
work or an operator's cordon also has a deterministic test in
test_controller.py, because a 1-in-60 chance is not a safety guarantee. Use
this harness for emergent, sequence-dependent failures; use the unit tests for
precise scenarios you can already name.

WHAT IT CANNOT REACH: any interleaving this generator does not produce. The
signal model is shaped from real incidents -- phased demand, a capped-queue
mode where shortfall parks just below one node's worth while saturation
toggles, hung work that outlives the drain timeout, operator maintenance,
injected restarts -- but it is a model, and a bug outside its shape will not be
found here.

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
        # Unknown to begin with, on purpose: a node that was already dark when
        # the controller started has no recoverable transition time, and the
        # fallback that covers it is worth exercising rather than assuming.
        self.down_since = None
        #: Set while the node is rebooting into an update it just installed.
        #: Powering a node off in this state is how a scheduled update turns
        #: into an unbootable machine.
        self.updating = None


class Sim:
    """The fake world, and the four seams the controller plugs into."""

    def __init__(self, seed, ticks, cfg=None):
        self.rnd = random.Random(seed)
        self.seed, self.ticks = seed, ticks
        self.t = 1_000_000.0
        self.cfg = cfg or self.default_cfg(seed)
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
        self.interrupted_update = []
        self.blocked = None
        self.zombie_since = {}
        self.sleepable_since = {}
        self.unvisited_since = {}
        self.visit_since = {}
        self.notified = {}
        self.warming = {}
        #: Set once run() builds it. The reboot model reads the controller's
        #: own phase, which is the only honest way to tell a maintenance visit
        #: apart from a drain that happens to look identical from outside.
        self.controller = None

    @staticmethod
    def default_cfg(seed):
        """Half the seeds run scheduled maintenance, half do not.

        Both halves are worth having. The maintenance half exercises a node
        that is powered, Ready and cordoned for minutes at a time on purpose,
        which is the exact shape the zombie invariant hunts for -- so it has to
        be reachable, and it has to stay bounded. The other half keeps the
        tighter budgets honest: a feature that widened every budget for every
        seed would quietly disarm the checks it was not supposed to touch.

        The interval is half an hour rather than a day so a 900-tick run spans
        thirty of them; real deployments set days. It is short for a reason
        beyond speed: the liveness budget below is dominated by the things a
        visit YIELDS to, so a long interval buries the schedule's own
        contribution under them and the check stops being able to see it.
        """
        if seed % 2 == 0:
            return Config(mode="on")
        return Config(mode="on", maintenance_interval_s=1800,
                      maintenance_window_s=300, maintenance_stagger_s=600,
                      maintenance_timeout_s=1200)

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
                    n.updating = None            # it came back on its own
                elif was and not n.ready:
                    n.down_since = self.t
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
                         ours_since=n.ours,
                         down_since=None if n.ready else n.down_since)

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
        if self.nodes[name].updating:
            self.interrupted_update.append((self.t, name))
            self.nodes[name].updating = None
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

    def fits_node(self, capacity):
        # Sometimes the waiting work genuinely cannot run here -- a selector,
        # a taint, an unbound volume. A controller that ignores this powers on
        # hardware for nothing.
        return self.rnd.random() > 0.1

    # -- Notifier --------------------------------------------------------
    def going_down(self, node):
        self.notified[node] = "down"

    def back_up(self, node):
        self.notified[node] = "up"

    # -- Warmup ----------------------------------------------------------
    def start(self, node):
        self.warming[node] = self.t

    def done(self, node):
        # Takes a few ticks, so the phase is genuinely observed rather than
        # completing instantly and never being exercised.
        return self.t - self.warming.get(node, self.t) > 120

    def cleanup(self, node):
        self.warming.pop(node, None)

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

        # Decide the phase BEFORE creating work. Creating work during a quiet
        # phase meant a node never stayed sleepable for a full settle window,
        # so the sleep-liveness check could never fire -- a controller that
        # never slept anything passed cleanly.
        self.phase_left -= 1
        if self.phase_left <= 0:
            self.busy_phase = not self.busy_phase
            self.big = self.rnd.random() < 0.4
            self.capped = (not self.big) and self.rnd.random() < 0.5
            # Quiet phases must outlast the liveness budget, or that invariant
            # is unreachable.
            self.phase_left = (self.rnd.randint(230, 320) if not self.busy_phase
                               else self.rnd.randint(45, 90))

        schedulable = [n for n in self.nodes.values()
                       if n.ready and not n.cordoned]
        if self.busy_phase and schedulable and self.rnd.random() < 0.5:
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
                    # Hung work outlives the drain timeout by a wide margin
                    # -- enough to exercise that branch -- but NOT forever.
                    # Real hung jobs hit a scheduler timeout and die. Modelling
                    # them as immortal let them accumulate until every node was
                    # permanently non-drainable, which silently exempted every
                    # node from the liveness checks and left them toothless.
                    ticks_left=(self.rnd.randint(150, 400) if stuck
                                else self.rnd.randint(1, 6)) if busy else 0))

        # A node that just installed a kernel reboots into it, and is NotReady
        # for several ticks in the middle of its own maintenance window. This
        # is the state the whole feature has to survive, and without modelling
        # it the harness ran 181 visits across 20 seeds and never once produced
        # it.
        #
        # Injected ONLY while the controller believes it is mid-visit, read
        # from its own phase rather than guessed from the cluster. The first
        # version guessed -- powered, Ready, cordoned and ours -- which is also
        # exactly what a node halfway through an ORDINARY drain looks like, so
        # it injected reboots into demand-driven sleeps and then reported the
        # controller for finishing them. A model that cannot tell those two
        # apart cannot assert anything about either.
        for n in self.nodes.values():
            if n.updating is not None:
                continue                         # advance() brings it back
            if (self.cfg.maintenance_interval_s and n.powered and n.ready
                    and self.controller is not None
                    and self.controller.st.get(n.name, {}).get("phase")
                            == "maintaining"
                    and self.rnd.random() < 0.15):
                n.ready = False
                n.down_since = self.t
                n.updating = self.t
                # Down for a couple of ticks, then a full POST -- comfortably
                # inside the visit's bound, so anything that cuts it short is
                # the controller deciding to, not the clock running out.
                n.change_at = self.t + 2 * self.cfg.interval_s + BOOT_S

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
        # A powered-off node must be announced as down; a live one must NOT
        # still be announced. A stale notification on a healthy node swallows
        # its next real failure, which is worse than the noise it suppressed.
        for n in self.nodes.values():
            ann = self.notified.get(n.name)
            # `powered AND ready` -- not ready alone. A node mid-shutdown is
            # briefly still Ready while the OS goes down, and announcing it as
            # down is exactly right there.
            if n.powered and n.ready and ann == "down":
                fail("%s is Ready but still announced as down -- a real "
                     "failure of it would be silenced" % n.name)
            if not n.powered and ann != "down":
                fail("%s was powered off without being announced as down "
                     "(announced=%r)" % (n.name, ann))

        if self.powered_off_busy:
            fail("powered off a node running work: %s"
                 % (self.powered_off_busy[0],))
        if self.killed_work:
            fail("released a unit that was executing work: %s"
                 % (self.killed_work[0],))
        if self.stomped:
            fail("overrode an operator's maintenance cordon: %s"
                 % (self.stomped[0],))
        if self.interrupted_update:
            fail("powered off a node that was rebooting into an update it had "
                 "just installed: %s" % (self.interrupted_update[0],))
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
                if self.cfg.maintenance_interval_s:
                    # A maintenance visit is powered, Ready and cordoned ON
                    # PURPOSE -- that is precisely its shape -- so the budget
                    # has to cover a whole window plus the drain that ends it.
                    # Added only when the feature is on: widening a budget for
                    # every seed would quietly disarm the check everywhere else.
                    budget += self.cfg.maintenance_window_s
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
        # Worst-case convergence, and every term is reachable together:
        #   min_uptime      a node that just woke may not sleep yet
        #   sleep_sustain   demand must stay low for a full window first
        #   sleep_cooldown  an abandoned sleep backs off before retrying
        #   N x drain       one corrective action per tick, so nodes drain
        #                   sequentially and the last waits behind the rest
        # Omitting the cooldown made this fail on correct behaviour roughly
        # 1 seed in 100 -- rare enough to look like a real bug, which is
        # exactly the kind of flake that erodes trust in a suite.
        # Measured PER NODE, against how long THAT node has been continuously
        # sleepable -- not against a global quiet counter.
        #
        # The first version compared a global counter to an instantaneous
        # drainable set, so a node that kept flipping in and out of drainable
        # still accrued the global clock and failed on correct behaviour. That
        # is a modelling error in kind, not degree, and the symptom was needing
        # to nudge the budget upward every time the soak widened -- which is
        # how you end up with a suite nobody trusts.
        settle = ((self.cfg.min_uptime_s + self.cfg.sleep_sustain_s
                   + self.cfg.sleep_cooldown_s
                   + len(self.nodes) * self.cfg.drain_timeout_s
                   # a node powered on for a visit is legitimately awake for a
                   # window before it may start going down again
                   + (self.cfg.maintenance_window_s
                      if self.cfg.maintenance_interval_s else 0))
                  / self.cfg.interval_s) + 25
        for n in self.nodes.values():
            sleepable = (self.busy_phase is False
                         and n.powered
                         and n.name not in self.human_held
                         and not any(w.node == n.name and w.work
                                     for w in self.workers))
            if not sleepable:
                self.sleepable_since.pop(n.name, None)
                continue
            self.sleepable_since.setdefault(n.name, self.t)
            held_for = (self.t - self.sleepable_since[n.name]) / self.cfg.interval_s
            if held_for > settle:
                fail("%s has been continuously sleepable for %d ticks "
                     "(budget %d) and is still powered"
                     % (n.name, held_for, settle))

        # A visit must not hold a node past the window it was promised. The
        # bound in maintenance_timeout_s is a backstop for a node that keeps
        # flapping, not a licence to sit on a healthy one -- and a budget built
        # from the backstop would accept a window that never closes at all,
        # which is a whole broken feature passing quietly.
        #
        # Measured against CONTINUOUS Ready time inside the visit, read from
        # the controller's own phase: a node that reboots resets the clock,
        # because waiting for it to come back is the correct behaviour and must
        # not read as overstaying.
        for n in self.nodes.values():
            visiting = (self.controller is not None
                        and (self.controller.st.get(n.name) or {}).get("phase")
                            == "maintaining")
            if not (visiting and n.ready):
                self.visit_since.pop(n.name, None)
                continue
            self.visit_since.setdefault(n.name, self.t)
            held = self.t - self.visit_since[n.name]
            # Two ticks of slack: the window is set on the tick the controller
            # first observes Ready and closed on the first tick past it, so a
            # correct visit lands on the window itself plus measurement grain.
            budget = self.cfg.maintenance_window_s + 2 * self.cfg.interval_s
            if held > budget:
                fail("%s has been up and cordoned inside its maintenance "
                     "window for %.0fs (window %.0fs, budget %.0fs)"
                     % (n.name, held, self.cfg.maintenance_window_s, budget))

        # A node metalnap put to sleep must eventually be visited. This is the
        # ONLY check that fails if the schedule silently stops firing, and a
        # schedule that never fires is indistinguishable from the feature being
        # switched off -- which is the failure mode a config knob is most
        # likely to produce.
        #
        # Measured only while a node is a legitimate candidate: dark, carrying
        # OUR cordon, and not in an operator's hands. A node an operator pulled
        # is one metalnap must never power on, so counting it here would demand
        # exactly the behaviour the safety rules forbid.
        if self.cfg.maintenance_interval_s:
            # Every term is reachable together, and each is a thing maintenance
            # yields to -- it is the lowest-priority work in the loop:
            #   interval + stagger   the schedule itself
            #   busy phase           visits wait for demand to be met, and a
            #                        busy phase runs up to 90 ticks
            #   N x (window+drain)   visits are serialised, so the last node
            #                        waits behind every other one
            #   timeout              one visit may burn its whole bound first
            #
            # sleep_cooldown is deliberately NOT here. It backs off a sleep
            # that was abandoned; it sets no phase, so it delays nothing about
            # a visit -- and a term that cannot fire only makes the budget
            # looser than the thing it is meant to bound.
            budget = (self.cfg.maintenance_interval_s
                      + self.cfg.maintenance_stagger_s
                      + self.cfg.maintenance_timeout_s
                      + 90 * self.cfg.interval_s
                      + len(self.nodes) * (self.cfg.maintenance_window_s
                                           + self.cfg.drain_timeout_s))
            for n in self.nodes.values():
                candidate = (not n.powered and n.cordoned
                             and n.ours is not None
                             and n.name not in self.human_held)
                if not candidate:
                    self.unvisited_since.pop(n.name, None)
                    continue
                self.unvisited_since.setdefault(n.name, self.t)
                dark = self.t - self.unvisited_since[n.name]
                if dark > budget:
                    fail("%s has been asleep and due for %.0fs (budget %.0fs) "
                         "without a maintenance visit -- the schedule has "
                         "stopped firing" % (n.name, dark, budget))

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
                       notifier=self, warmup=self,
                       log=lambda lvl, msg, **kv: self.log.append(
                           (self.t, lvl, msg)),
                       clock=self.now)
        self.controller = c
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
