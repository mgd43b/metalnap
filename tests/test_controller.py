"""
Deterministic tests for the scenarios the simulation harness catches only
probabilistically.

The division is deliberate. sim.py is for EMERGENT failures across long
sequences; these are for known, precise timing scenarios where a 1-in-40
backstop is not good enough -- particularly anything guarding running work or
an operator's cordon.
"""
import dataclasses
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalnap import Config, Controller          # noqa: E402
from metalnap.ceiling import PrometheusCeiling, StaticCeiling   # noqa: E402
from metalnap.controller import _hash_fraction   # noqa: E402
from metalnap.types import NodeState, NullCeiling   # noqa: E402


def node(ready=True, cordoned=False, ours=False, ready_since=0.0,
         capacity=100.0, ours_since=None, protected=False, down_since=None,
         **notes):
    return NodeState(ready=ready, cordoned=cordoned, ours=ours,
                     ready_since=ready_since, capacity=capacity,
                     protected=protected, ours_since=ours_since,
                     down_since=down_since, **notes)


#: NodeSource.note() keys, as the NodeState fields they come back as.
NOTE_FIELDS = {"power-cycled": "power_cycled_at", "visited": "visited_at",
               "shutdown": "shutdown_at", "trouble": "trouble",
               "maintenance-started": "maintenance_started_at",
               "shed": "shed_at"}


def crashed(**kw):
    """A node that went down in service: dark, and nobody's cordon on it."""
    kw.setdefault("down_since", 500.0)
    return node(ready=False, ready_since=None, **kw)


#: A realistic wall clock. The maintenance schedule is measured in hours
#: against unix timestamps, so a harness clock of 1000.0 would put "dark for a
#: day" before the epoch -- which is not a shape the controller will ever meet.
T0 = 1_700_000_000.0


def asleep(dark_for=100_000.0, at=T0, **kw):
    """A node metalnap itself put to sleep, dark for a while.

    Cordoned AND ours is what "we slept it" looks like from the cluster, and it
    is the only shape a maintenance visit will ever touch.
    """
    kw.setdefault("cordoned", True)
    kw.setdefault("ours", True)
    kw.setdefault("down_since", at - dark_for)
    return node(ready=False, ready_since=None, **kw)


class Harness:
    """A minimal stub world. Records what the controller tried to do."""

    def __init__(self, states, shortfall=0.0, saturated=0, busy=(), idle=(),
                 holds=False, fits=True, chassis=None, ceiling=None):
        self.states = states
        self._shortfall, self._saturated, self._fits = shortfall, saturated, fits
        #: What the capacity ceiling reads, tick by tick: an int, None (no
        #: ceiling) or an exception to raise (the signal is unavailable).
        self._ceiling = ceiling
        self.ceiling_reads = 0
        self._busy = busy if isinstance(busy, (dict, BaseException)) \
            else list(busy)
        self._idle, self._holds = list(idle), holds
        self.acted = {"cordon": [], "on": [], "off": [], "released": [],
                      "cycle": [], "record": [], "note": [], "disown": []}
        #: Every power and record action, in order -- for asserting that the
        #: durable record of a power cycle comes BEFORE the cycle.
        self.sequence = []
        #: What each BMC reports, where it differs from "on iff Ready". A
        #: wedged node is the case that needs it: powered, and not Ready.
        self.chassis = dict(chassis or {})
        self.record_fails = False
        #: Overrides for the SECOND and later state() reads in a tick -- the
        #: fresh read an operation makes where it finishes, which is where an
        #: operator's cordon has to be caught.
        self.fresh = {}
        self.logs = []
        #: The same lines with their level, which `logs` drops.
        self.records = []
        self.t = 1000.0

    def logged(self, needle):
        return [kv for msg, kv in self.logs if needle in msg]

    # NodeSource
    def state(self, n):
        if n in self.fresh:
            self._reads = getattr(self, "_reads", {})
            self._reads[n] = self._reads.get(n, 0) + 1
            if self._reads[n] > 1:
                if isinstance(self.fresh[n], BaseException):
                    raise self.fresh[n]
                return self.fresh[n]
        return self.states.get(n)

    def set_cordon(self, n, v):
        self.acted["cordon"].append((n, v))

    def note(self, n, key, value):
        """Durable, like the annotations it stands for: survives c.st = {}."""
        if key == "power-cycled":
            if self.record_fails:
                raise RuntimeError("apiserver said no")
            self.acted["record"].append(n)
            self.sequence.append(("record", n))
        self.acted["note"].append((n, key, value))
        self.states[n] = dataclasses.replace(self.states[n],
                                             **{NOTE_FIELDS[key]: value})

    def disown(self, n):
        self.acted["disown"].append(n)

    # PowerBackend lives on its own object: NodeSource.state() and
    # PowerBackend.state() share a name, so one class implementing both hands
    # the power check a NodeState instead of "on"/"off" -- silently, because
    # NodeState == "off" is simply False.
    @property
    def power(self):
        return _Power(self)

    # CapacityCeiling
    def limit(self):
        self.ceiling_reads += 1
        if isinstance(self._ceiling, BaseException):
            raise self._ceiling
        return self._ceiling

    # DemandSignal
    def shortfall(self):
        if isinstance(self._shortfall, BaseException):
            raise self._shortfall
        return self._shortfall

    def saturated_units(self):
        return self._saturated

    def fits_node(self, capacity):
        if isinstance(self._fits, BaseException):
            raise self._fits
        return self._fits

    # DrainPolicy
    def busy(self, n):
        if isinstance(self._busy, BaseException):
            raise self._busy
        if isinstance(self._busy, dict):          # per node
            return list(self._busy.get(n, []))
        return list(self._busy)

    def idle(self, n):
        return list(self._idle)

    def holds_work(self, u):
        return self._holds

    def release(self, u):
        self.acted["released"].append(u)

    def residual(self, n):
        return []

    def controller(self, nodes=("a", "b"), power=None, notifier=None,
                   status=None, metrics=None, warmup=None, **cfg):
        defaults = dict(mode="on", wake_sustain_s=0, sleep_sustain_s=0,
                        min_uptime_s=0)
        defaults.update(cfg)          # let a test override any of them
        c = Config(**defaults)
        return Controller(nodes=list(nodes), node_source=self,
                          power=power or self.power, signal=self, drain=self,
                          config=c, clock=lambda: self.t, notifier=notifier,
                          warmup=warmup, ceiling=self, status=status,
                          metrics=metrics,
                          log=self._record)

    def _record(self, lvl, msg, **kv):
        self.logs.append((msg, kv))
        self.records.append((lvl, msg, kv))

    def at_level(self, lvl, needle=""):
        """Log lines of one level -- for the cases where HOW LOUD is the rule."""
        return [kv for level, msg, kv in self.records
                if level == lvl and needle in msg]


class _Power:
    def __init__(self, h):
        self.h = h

    def state(self, n):
        if n in self.h.chassis:
            if isinstance(self.h.chassis[n], BaseException):
                raise self.h.chassis[n]
            return self.h.chassis[n]
        st = self.h.states.get(n)
        return "on" if (st and st.ready) else "off"

    def on(self, n):
        self.h.acted["on"].append(n)
        if getattr(self.h, "power_on_takes", True):
            self.h.chassis[n] = "on"

    def soft_off(self, n):
        self.h.acted["off"].append(n)

    def cycle(self, n):
        self.h.acted["cycle"].append(n)
        self.h.sequence.append(("cycle", n))


class _PowerWithoutCycle:
    """A backend that cannot power-cycle -- Wake-on-LAN, say."""

    def __init__(self, h):
        self.h = h

    def state(self, n):
        return _Power(self.h).state(n)

    def on(self, n):
        _Power(self.h).on(n)

    def soft_off(self, n):
        _Power(self.h).soft_off(n)


class TestOperatorCordon(unittest.TestCase):
    """An operator's cordon outranks every controller decision."""

    def test_held_node_is_not_woken(self):
        h = Harness({"a": node(cordoned=True, ours=False), "b": None},
                    shortfall=400.0)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["cordon"], [], "stomped an operator's cordon")
        self.assertEqual(h.acted["on"], [], "powered on a held node")
        # The stronger invariant: a held node must never enter an operation at
        # all. Without this the in-flight guard masks a broken candidate list
        # -- defence in depth is good, but it should not hide a missing guard.
        self.assertIsNone(c.st.get("a", {}).get("phase"),
                          "started an operation on a node an operator holds")

    def test_in_flight_wake_is_abandoned_when_an_operator_cordons(self):
        """The guarantee must hold where operations FINISH, not only start.

        A wake begun before the cordon completed and uncordoned the operator --
        in the same tick that logged the node as held.
        """
        h = Harness({"a": node(ready=True, cordoned=True, ours=False),
                     "b": None}, shortfall=400.0)
        c = h.controller()
        c.st["a"] = {"phase": "waking", "phase_since": h.t}
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"],
                         "completed a wake over an operator's cordon")
        self.assertIsNone(c.st["a"]["phase"], "left the operation in flight")

    def test_controller_owned_cordon_is_repaired(self):
        h = Harness({"a": node(cordoned=True, ours=True), "b": None},
                    shortfall=400.0)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertIn(("a", False), h.acted["cordon"],
                      "did not repair its own stranded cordon")


class TestRunningWork(unittest.TestCase):
    """Never interrupt work. The rule with the worst failure mode."""

    def test_busy_node_is_not_powered_off(self):
        h = Harness({"a": node(), "b": None}, busy=["u1"])
        c = h.controller()
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(h.acted["off"], [], "powered off a node running work")
        self.assertEqual(h.acted["released"], [], "released a busy unit")

    def test_unit_that_gained_work_in_the_race_is_not_released(self):
        h = Harness({"a": node(), "b": None}, idle=["u1"], holds=True)
        c = h.controller()
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(h.acted["released"], [],
                         "released a unit that had just been given work")
        self.assertEqual(h.acted["off"], [])

    def test_idle_units_are_released_so_the_drain_can_finish(self):
        h = Harness({"a": node(), "b": None}, idle=["u1"], holds=False)
        c = h.controller()
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(h.acted["released"], ["u1"],
                         "idle unit not released -- this is the livelock")
        self.assertEqual(h.acted["off"], [], "powered off without re-observing")

    def test_a_restarted_sleep_keeps_the_cordon_it_already_holds(self):
        """The cordon's timestamp is the drain deadline's durable anchor.
        Re-stamping it when a restarted process begins the sleep again resets
        the one clock meant to survive the restart -- and a node with hung
        work is held for ever, one restart at a time."""
        h = Harness({"a": node(), "b": None}, busy=["u1"])
        h.states["a"] = node(cordoned=True, ours=True, ours_since=h.t - 100)
        c = h.controller()                    # a fresh process: no phase
        c.sleep("a", h.states["a"])
        self.assertEqual(c.st["a"]["phase"], "sleeping")
        self.assertNotIn(("a", True), h.acted["cordon"],
                         "re-stamped the cordon, resetting the drain deadline")
        h.states["b"] = node()
        c.sleep("b", h.states["b"])
        self.assertIn(("b", True), h.acted["cordon"],
                      "a node not yet ours was never cordoned")

    def test_clean_node_powers_off(self):
        h = Harness({"a": node(), "b": None})
        c = h.controller()
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(h.acted["off"], ["a"])


class TestDemandSignal(unittest.TestCase):
    def test_saturated_queue_wakes_a_node_with_zero_shortfall(self):
        """A queue at its ceiling admits nothing, so its demand is invisible."""
        h = Harness({"a": node(ready=False), "b": None},
                    shortfall=0.0, saturated=1)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], ["a"],
                         "capped queue did not wake a node")

    def test_no_demand_wakes_nothing(self):
        h = Harness({"a": node(ready=False), "b": None})
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], [])


class TestProtectedNodes(unittest.TestCase):
    """Configuration is the weakest link. A typo must not cost a control plane."""

    def test_a_protected_node_is_never_powered_off(self):
        h = Harness({"a": node(protected=True), "b": None})
        c = h.controller()
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.st["want_low_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["off"], [], "powered off a protected node")
        self.assertEqual(h.acted["cordon"], [], "cordoned a protected node")

    def test_a_protected_node_is_never_woken(self):
        h = Harness({"a": node(ready=False, protected=True), "b": None},
                    shortfall=400.0)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], [], "powered on a protected node")

    def test_its_healthy_peer_is_still_managed(self):
        """Refusing one node must not disable the controller."""
        h = Harness({"a": node(protected=True), "b": node(ready=False)},
                    shortfall=400.0)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], ["b"],
                         "a protected node stopped its peer being managed")


class TestDryRunTouchesNothing(unittest.TestCase):
    """dry_run must not mutate ANY external state.

    Both of these were live bugs found by shadowing metalnap next to the
    controller it is replacing. The second is the dangerous one: `stranded` is
    read from the CLUSTER, not from our own state, so a dry_run shadow sharing
    the cordon annotation would uncordon a node the live controller was
    mid-drain on. It had not fired only because no sleep happened to occur
    while the shadow was up.
    """

    def test_stranded_repair_does_not_cordon_in_dry_run(self):
        h = Harness({"a": node(ready=True, cordoned=True, ours=True),
                     "b": None}, shortfall=400.0)
        c = h.controller(mode="dry_run")
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["cordon"], [],
                         "dry_run uncordoned a node -- it would fight the "
                         "controller it is shadowing")

    def test_mid_sleep_abort_does_not_cordon_in_dry_run(self):
        h = Harness({"a": node(ready=True, cordoned=True, ours=True),
                     "b": None}, shortfall=400.0)
        c = h.controller(mode="dry_run")
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["cordon"], [], "dry_run changed a cordon")

    def test_dry_run_powers_nothing(self):
        h = Harness({"a": node(ready=False), "b": None}, shortfall=400.0)
        c = h.controller(mode="dry_run")
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], [])
        self.assertEqual(h.acted["off"], [])


class TestFitGuard(unittest.TestCase):
    """shortfall() is a sum; it cannot say WHY work is waiting."""

    def test_unplaceable_demand_does_not_wake_a_node(self):
        h = Harness({"a": node(ready=False), "b": None},
                    shortfall=400.0, fits=False)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], [],
                         "powered on a node for work that cannot run there")

    def test_saturation_bypasses_the_fit_guard(self):
        """A saturated queue has nothing pending to inspect -- that IS the
        problem -- so the guard must not veto a saturation-driven wake."""
        h = Harness({"a": node(ready=False), "b": None},
                    shortfall=0.0, saturated=1, fits=False)
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], ["a"],
                         "fit guard vetoed a saturation-driven wake")


class TestNotifier(unittest.TestCase):
    """A node powering off looks exactly like a node dying."""

    class Spy:
        def __init__(self, fail=False):
            self.down, self.up, self.fail = [], [], fail
            self.alerts, self.cleared = {}, []

        def going_down(self, n):
            if self.fail:
                raise RuntimeError("alertmanager unreachable")
            self.down.append(n)

        def back_up(self, n):
            self.up.append(n)

        def alert(self, n, reason):
            self.alerts[n] = reason

        def clear_alert(self, n):
            self.cleared.append(n)
            self.alerts.pop(n, None)

    def test_announced_before_power_off(self):
        h = Harness({"a": node(), "b": None})
        spy = self.Spy()
        c = h.controller()
        c.notifier = spy
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(spy.down, ["a"], "powered off without announcing")
        self.assertEqual(h.acted["off"], ["a"])

    def test_not_powered_off_if_the_announcement_fails(self):
        """Otherwise the alert fires and nobody knows it was us."""
        h = Harness({"a": node(), "b": None})
        c = h.controller()
        c.notifier = self.Spy(fail=True)
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(h.acted["off"], [],
                         "powered off a node it could not announce")

    def test_dry_run_never_touches_the_notifier(self):
        """dry_run must not mutate anything outside the process.

        A shadow deployment that silences alerts is not observing, it is
        participating -- and it fights the controller it was meant to be
        compared against. Found in production: a dry_run metalnap created a
        live Alertmanager silence beside the incumbent's.
        """
        # asleep(), so the down path is genuinely live: an uncordoned dark
        # node is never announced down in ANY mode, and a test built on one
        # would pass with the dry_run guard deleted.
        h = Harness({"a": asleep(), "b": crashed()}, shortfall=400.0)
        h.t = T0
        spy = self.Spy()
        c = h.controller(mode="dry_run", power_cycle_cooldown_s=0)
        c.notifier = spy
        c.st["b"] = {"trouble": "anything"}
        c.tick()
        self.assertEqual(spy.down, [], "dry_run announced a node as down")
        self.assertEqual(spy.up, [], "dry_run cleared a notification")
        self.assertEqual(spy.alerts, {}, "dry_run raised an alert")
        self.assertEqual(spy.cleared, [], "dry_run cleared an alert")

    def test_a_node_that_is_up_gets_its_notification_cleared(self):
        """A leftover silence on a live node swallows a real failure."""
        h = Harness({"a": node(ready=True), "b": None})
        spy = self.Spy()
        c = h.controller()
        c.notifier = spy
        c.tick()
        self.assertIn("a", spy.up, "never cleared the notification")


class TestFlickeringDemand(unittest.TestCase):
    """`want` oscillates when a backlog sits right at a node's worth.

    a carries work, so it is one node wanted on its own; a backlog either side
    of the boundary makes that one or two, tick by tick.
    """

    def test_dip_to_equality_does_not_reset_the_wake_timer(self):
        h = Harness({"a": node(), "b": node(ready=False)},
                    shortfall=60.0, busy={"a": ["job-1"]})
        c = h.controller(wake_sustain_s=120)
        # t, backlog: 2 ticks wanting more, a dip, then wanting more again.
        woken = []
        for dt, short in ((0, 60.0), (60, 60.0), (90, 0.0), (200, 60.0)):
            h.t = 1000.0 + dt
            h._shortfall = short
            c.tick()
            woken += h.acted["on"]
            h.acted["on"] = []
        self.assertIn("b", woken,
                      "flickering demand never accumulated the sustain window")

    def test_a_one_off_spike_does_not_leave_a_primed_timer(self):
        h = Harness({"a": node(), "b": node(ready=False)},
                    shortfall=60.0, busy={"a": ["job-1"]})
        c = h.controller(wake_sustain_s=120)
        woken = []
        for dt, short in ((0, 60.0), (60, 0.0), (400, 0.0), (460, 60.0)):
            h.t = 1000.0 + dt
            h._shortfall = short
            c.tick()
            woken += h.acted["on"]
            h.acted["on"] = []
        self.assertEqual(woken, [], "a stale timer fired on a transient spike")


class TestScheduledMaintenance(unittest.TestCase):
    """A node nobody wants still has to be maintained.

    The hazard this feature introduces is new in kind: every other power-off in
    this controller happens to a node the cluster has finished with, whereas
    these happen to a node that was woken specifically to change itself. Most
    of what follows is about not cutting power to a machine in the middle of
    doing that.
    """

    MAINT = dict(maintenance_interval_s=3600, maintenance_window_s=300,
                 maintenance_stagger_s=0, maintenance_timeout_s=3600)

    @staticmethod
    def harness(*a, **kw):
        h = Harness(*a, **kw)
        h.t = T0
        return h

    def test_a_node_dark_past_the_interval_is_woken(self):
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], ["a"], "an overdue node never woke")
        self.assertEqual(c.st["a"]["phase"], "maintaining")

    def test_a_node_not_yet_due_is_left_alone(self):
        h = self.harness({"a": asleep(dark_for=60.0), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], [], "woke a node that was not due")

    def test_disabled_by_default(self):
        """The feature powers hardware on when nothing asked for it."""
        h = self.harness({"a": asleep(dark_for=10_000_000.0), "b": None})
        c = h.controller()                      # no maintenance config at all
        c.tick()
        self.assertEqual(h.acted["on"], [])

    def test_the_node_is_never_put_into_service_by_a_visit(self):
        """A visit holds the node OUT of service, deliberately.

        Uncordoning would advertise capacity that is about to be taken away
        again, so every visit would end by draining real work under a
        five-minute deadline -- and that drain would then be the thing keeping
        the node up.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()                                            # begin
        h.states["a"] = node(ready=True, cordoned=True, ours=True)
        c.tick()                                            # observed Ready
        self.assertNotIn(("a", False), h.acted["cordon"],
                         "a maintenance visit made the node schedulable")

    def test_the_window_ends_in_an_ordinary_sleep(self):
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()                                            # begin
        h.states["a"] = node(ready=True, cordoned=True, ours=True)
        c.tick()                                            # window opens
        self.assertEqual(h.acted["off"], [], "cut the visit short")
        h.t += 301
        c.tick()                                            # window closes
        self.assertEqual(c.st["a"]["phase"], "sleeping")
        c.tick()                                            # drain, power off
        self.assertEqual(h.acted["off"], ["a"], "node never went back down")

    def test_min_uptime_does_not_extend_the_window(self):
        """min_uptime exists to stop DEMAND thrashing a node up and down.

        A visit is not demand: the whole point is a short stay, and a node held
        up for the 45 minutes min_uptime defaults to would cost more power than
        the updates it collected are worth.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(min_uptime_s=100_000, **self.MAINT)
        c.tick()
        h.states["a"] = node(ready=True, cordoned=True, ours=True,
                             ready_since=h.t)
        c.tick()
        h.t += 301
        c.tick()
        c.tick()
        self.assertEqual(h.acted["off"], ["a"],
                         "min uptime held a maintenance visit open")

    def test_a_node_that_reboots_mid_window_is_not_powered_off(self):
        """THE failure this feature could introduce.

        A node that goes NotReady inside its own maintenance window is, far
        more often than not, a node rebooting into the kernel it just
        installed. Cutting power to it is how a scheduled update turns into an
        unbootable machine.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        h.states["a"] = node(ready=True, cordoned=True, ours=True)
        c.tick()                                            # window opens
        h.t += 301                                          # window elapses...
        h.states["a"] = node(ready=False, cordoned=True, ours=True)  # rebooting
        c.tick()
        self.assertEqual(h.acted["off"], [],
                         "powered off a node that was rebooting into an update")
        self.assertEqual(c.st["a"]["phase"], "maintaining", "abandoned it")
        h.states["a"] = node(ready=True, cordoned=True, ours=True)   # back
        c.tick()
        c.tick()
        self.assertEqual(h.acted["off"], ["a"],
                         "never finished the visit after the node came back")

    def test_the_visit_is_bounded(self):
        """A visit may not hold a node forever, whatever the node does."""
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        h.t += 3601                                  # never became Ready
        c.tick()
        self.assertIsNone(c.st["a"]["phase"], "the bound never fired")

    def test_a_node_still_notready_at_the_bound_is_not_powered_off(self):
        """The bound releases the VISIT. It does not power off the NODE.

        A node still NotReady when the bound fires is either partway through
        the updates it was woken to collect or it is broken, and nothing the
        controller can observe tells it which. Leaving a machine powered costs
        watts; cutting power to one writing its own firmware costs the machine.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        h.t += 3601
        c.tick()
        self.assertEqual(h.acted["off"], [],
                         "cut power to a node that might be mid-update")
        # ...and it is not abandoned: the ordinary stranded repair finishes the
        # job the moment the node comes back.
        h.states["a"] = node(ready=True, cordoned=True, ours=True)
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "sleeping",
                         "nothing picked the node back up when it returned")

    def test_a_node_that_is_up_at_the_bound_is_slept_normally(self):
        """Flapped in and out of Ready long enough to burn the bound, but
        observable and healthy right now -- so end the visit the usual way."""
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        h.states["a"] = node(ready=True, cordoned=True, ours=True)
        h.t += 3601
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "sleeping")
        c.tick()
        self.assertEqual(h.acted["off"], ["a"])

    def test_a_failed_visit_is_not_retried_every_tick(self):
        """`down_since` never moves for a node that will not come back Ready.

        Scheduling on that alone reads as due on every tick forever, which is a
        power cycle every reconcile interval.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.st["a"] = {"maintenance_at": h.t - 100}   # attempted a moment ago
        c.tick()
        self.assertEqual(h.acted["on"], [], "retried a failed visit at once")

    def test_a_node_an_operator_powered_off_is_never_woken(self):
        """Dark, but not ours: somebody has it open on the bench.

        An operator's cordon outranks the controller; so does an operator's
        screwdriver. The cordon we placed when we slept a node is the only
        evidence that powering it on is ours to do.
        """
        for shape in (asleep(cordoned=False, ours=False),   # simply pulled
                      asleep(cordoned=True, ours=False)):   # operator cordon
            h = self.harness({"a": shape, "b": None})
            c = h.controller(**self.MAINT)
            c.tick()
            self.assertEqual(h.acted["on"], [],
                             "powered on a node an operator had taken")

    def test_a_protected_node_is_never_visited(self):
        h = self.harness({"a": asleep(protected=True), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], [], "visited a protected node")

    def test_dry_run_powers_nothing_on(self):
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(mode="dry_run", **self.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], [])
        self.assertIsNone(c.st.get("a", {}).get("phase"))

    def test_dry_run_still_shows_the_whole_schedule(self):
        """A shadow has to show you what it WOULD do, all of it.

        dry_run advances the schedule in its own memory even though it touches
        nothing outside the process. Without that it re-picks the same overdue
        node on every tick, forever, and never once names the second machine.
        """
        h = self.harness({"a": asleep(dark_for=200_000.0),
                          "b": asleep(dark_for=150_000.0)})
        c = h.controller(mode="dry_run", **self.MAINT)
        seen = []
        c._log = lambda lvl, msg, **kv: (seen.append(kv.get("node"))
                                         if "MAINTENANCE begin" in msg else None)
        c.tick()
        c.tick()
        self.assertEqual(seen, ["a", "b"],
                         "the shadow named one node twice instead of both once")
        self.assertEqual(h.acted["on"], [], "dry_run powered a node on")

    def test_a_restart_mid_visit_leaves_the_node_recoverable(self):
        """In-memory state does not survive a restart, and must not need to.

        A visit interrupted that way ends early rather than resuming -- the
        node is Ready, carries our cordon and is in no operation, which is the
        stranded shape the controller already knows how to resolve. What must
        never happen is a machine left powered with nobody owning it.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        h.states["a"] = node(ready=True, cordoned=True, ours=True)
        c.tick()
        c.st = {}                                   # the process restarted
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "sleeping",
                         "nothing picked up a node left powered and cordoned")

    def test_only_one_node_is_visited_at_a_time(self):
        """A rack that powers on in unison is a current spike."""
        h = self.harness({"a": asleep(), "b": asleep()})
        c = h.controller(**self.MAINT)
        c.tick()
        self.assertEqual(len(h.acted["on"]), 1, "woke a herd")
        c.tick()
        self.assertEqual(len(h.acted["on"]), 1,
                         "started a second visit while one was in flight")

    def test_demand_outranks_the_schedule(self):
        h = self.harness({"a": asleep(), "b": None}, shortfall=400.0)
        c = h.controller(**self.MAINT)
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "waking",
                         "a maintenance visit pre-empted a demand wake")

    def test_a_visit_does_not_start_while_demand_is_unmet(self):
        """Including the gap before a wake's sustain window has elapsed.

        The guard cannot lean on "something else already took this tick": the
        whole shape of a sustain window is a run of ticks where demand is unmet
        and nothing has been started about it yet, and a visit slipping into
        one of those spends the node the wake was about to want.
        """
        h = self.harness({"a": asleep(), "b": asleep()}, shortfall=400.0)
        c = h.controller(wake_sustain_s=600, **self.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], [],
                         "started a maintenance visit while demand was unmet")

    def test_demand_during_a_visit_puts_the_node_into_service(self):
        """It is booted and cordoned: the cheapest capacity anywhere."""
        h = self.harness({"a": node(ready=True, cordoned=True, ours=True),
                          "b": None}, shortfall=400.0)
        c = h.controller(**self.MAINT)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_until": h.t + 300}
        c.tick()
        self.assertIn(("a", False), h.acted["cordon"],
                      "left a booted node cordoned while demand went unmet")
        self.assertIsNone(c.st["a"]["phase"])
        self.assertNotIn("maintenance_until", c.st["a"])

    def test_an_operator_cordon_abandons_a_visit(self):
        h = self.harness({"a": node(ready=True, cordoned=True, ours=False),
                          "b": None})
        c = h.controller(**self.MAINT)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_until": h.t - 1}
        c.tick()
        self.assertIsNone(c.st["a"]["phase"])
        self.assertEqual(h.acted["off"], [], "slept a node an operator holds")
        self.assertNotIn("maintenance_until", c.st["a"],
                         "a stale deadline would end the next visit instantly")

    def test_being_uncordoned_mid_visit_hands_the_node_over(self):
        """An operator putting the node into service outranks the schedule."""
        h = self.harness({"a": node(ready=True, cordoned=False, ours=False),
                          "b": None})
        c = h.controller(**self.MAINT)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_until": h.t - 1}
        c.tick()
        self.assertIsNone(c.st["a"]["phase"])
        self.assertEqual(h.acted["off"], [],
                         "powered off a node an operator had just put back")

    def test_the_drain_deadline_is_refreshed_before_the_drain(self):
        """The cordon is weeks old; the drain it now anchors is seconds old.

        sleep() measures its drain deadline from the cordon timestamp, so a
        visit that ended without re-stamping would abandon its own drain on the
        first busy unit -- against a deadline that expired before the drain
        existed.
        """
        h = self.harness({"a": asleep(), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        h.states["a"] = node(ready=True, cordoned=True, ours=True,
                             ours_since=h.t - 500_000)
        c.tick()
        h.t += 301
        c.tick()
        self.assertIn(("a", True), h.acted["cordon"],
                      "handed a fresh drain a deadline that expired weeks ago")

    def test_the_stagger_spreads_nodes_and_survives_a_restart(self):
        """Random-looking across a fleet, identical across a redeploy.

        A freshly seeded RNG would re-roll every offset on every restart, so a
        controller that redeploys often would keep re-bunching the very nodes
        the stagger exists to spread apart.
        """
        h = self.harness({"a": asleep(), "b": asleep()})
        cfg = dict(self.MAINT, maintenance_stagger_s=3600)
        c = h.controller(nodes=("a", "b", "c", "d"), **cfg)
        offsets = [c._maintenance_offset(n) for n in ("a", "b", "c", "d")]
        self.assertEqual(len(set(offsets)), 4, "every node got the same slot")
        self.assertTrue(all(0 <= o < 3600 for o in offsets), offsets)
        fresh = h.controller(nodes=("a", "b", "c", "d"), **cfg)
        self.assertEqual([fresh._maintenance_offset(n)
                          for n in ("a", "b", "c", "d")], offsets,
                         "a restart re-rolled the schedule")

    def test_the_offset_cannot_reach_a_whole_stagger(self):
        """The half-open end of [0, stagger) is load-bearing, and the way it
        breaks is ARITHMETIC, not statistical.

        A 64-bit numerator does not fit a double's mantissa, so the widest
        digests round the ratio up to exactly 1.0 and the node is pushed a
        whole stagger late. That is about one name in 2**54 -- no sweep over
        plausible node names finds it, which is why the boundary is tested
        directly instead of hunted for.
        """
        self.assertLess(_hash_fraction(b"\xff" * 32), 1.0,
                        "the widest possible digest rounded up to a full slot")
        self.assertEqual(_hash_fraction(b"\x00" * 32), 0.0)

    def test_a_source_without_down_since_still_schedules(self):
        """Falls back to the controller's own start time: late, never early."""
        h = self.harness({"a": asleep(down_since=None), "b": None})
        c = h.controller(**self.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], [],
                         "treated an unknown dark time as infinitely overdue")
        h.t += 3601
        c.tick()
        self.assertEqual(h.acted["on"], ["a"], "never came due at all")


class TestMaintenanceConfig(unittest.TestCase):
    """A schedule that cannot fire looks exactly like a disabled one."""

    def test_a_window_longer_than_the_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(mode="on", maintenance_interval_s=300,
                   maintenance_window_s=600).validate()

    def test_a_bound_that_cannot_cover_a_boot_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(mode="on", maintenance_interval_s=86400,
                   maintenance_window_s=300, wake_timeout_s=900,
                   maintenance_timeout_s=600).validate()

    def test_the_disabled_default_validates(self):
        self.assertIsNotNone(Config(mode="on").validate())


class TestConfigFromEnvironment(unittest.TestCase):
    """Read when a Config is built, not when the module is imported.

    As plain default expressions every knob was read once, at import, and an
    environment set any later was silently ignored -- by this suite, and by
    anything embedding the controller.
    """

    def test_the_environment_is_read_at_construction(self):
        saved = {k: os.environ.get(k) for k in ("INTERVAL_S", "MODE")}
        os.environ.update(INTERVAL_S="7", MODE=" on ")
        try:
            cfg = Config()
            self.assertEqual((cfg.interval_s, cfg.mode), (7, "on"))
            self.assertEqual(Config(interval_s=3).interval_s, 3,
                             "an explicit value lost to the environment")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


#: A short name for the notifier spy, for the classes below that are not
#: about notification but still have to see what was muted and alerted.
class Spy(TestNotifier.Spy):
    pass


def wedged(h, c, name="a", t=None):
    """Drive a wedged node to its first wake timeout. Returns the controller."""
    c.tick()                                     # WAKE begin, chassis found on
    h.t = (t or h.t) + 901
    c.tick()                                     # the timeout
    return c


class TestWakeEscalation(unittest.TestCase):
    """#13: a node that is powered but wedged must not be waited on forever.

    2026-09-17: a node's kernel locked up, and it read "on" to its BMC for 21
    hours. Every wake found it powered, sent nothing, timed out, and was
    chosen again. One `ipmitool chassis power cycle` fixed it in five minutes.
    """

    def harness(self, **kw):
        return Harness({"a": crashed()}, shortfall=400.0,
                       chassis={"a": "on"}, **kw)

    def test_a_wedged_node_is_power_cycled_at_the_wake_timeout(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",)))
        self.assertEqual(h.acted["on"], [], "sent power-on to a powered node")
        self.assertEqual(h.acted["cycle"], ["a"], "never escalated")
        self.assertEqual(c.st["a"]["phase"], "waking",
                         "a cycled node gets one more wake timeout")

    def test_the_cycle_is_recorded_before_it_happens(self):
        h = self.harness()
        wedged(h, h.controller(nodes=("a",)))
        self.assertEqual(h.sequence, [("record", "a"), ("cycle", "a")])

    def test_no_record_means_no_cycle(self):
        """A cycle the bound cannot see is how a bound becomes a loop."""
        h = self.harness()
        h.record_fails = True
        c = wedged(h, h.controller(nodes=("a",)))
        self.assertEqual(h.acted["cycle"], [], "cycled without a record")
        self.assertIn("recorded", c.st["a"]["trouble"])

    def test_a_second_timeout_gives_up_alerts_and_unmutes(self):
        h = self.harness()
        spy = Spy()
        c = wedged(h, h.controller(nodes=("a",), notifier=spy))
        h.t += 901
        c.tick()
        self.assertEqual(h.acted["cycle"], ["a"], "cycled a second time")
        self.assertIsNone(c.st["a"]["phase"])
        c.tick()
        self.assertIn("a", spy.alerts, "gave up without telling anyone")
        self.assertNotIn("a", spy.down, "muted a node it gave up on")
        self.assertEqual(len(h.logged("WAKE begin")), 1,
                         "retried the wake straight away")

    def test_a_node_we_slept_that_will_not_come_back_is_unmuted(self):
        """Cordoned and ours reads as asleep -- until we stop being able to
        account for it. Muted then, KubeNodeUnreachable looks like any other
        sleep and KubeNodeNotReady never fires (it skips cordoned nodes)."""
        h = Harness({"a": asleep(at=1000.0)}, shortfall=400.0,
                    chassis={"a": "on"})
        spy = Spy()
        c = wedged(h, h.controller(nodes=("a",), notifier=spy))
        h.t += 901
        c.tick()
        c.tick()
        self.assertIn("a", spy.alerts)
        self.assertEqual(spy.up[-1], "a", "still muted after giving up")

    def test_at_most_one_cycle_per_cooldown_across_a_restart(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",)))
        c.st = {}                                # a restart forgets everything
        wedged(h, c)
        self.assertEqual(h.acted["cycle"], ["a"],
                         "a restart re-armed the power cycle")
        self.assertIn("already power-cycled", c.st["a"]["trouble"])

    def test_one_cycle_per_wake_even_when_the_cooldown_allows_another(self):
        """At the shortest legal cooldown the record alone would permit a
        second cycle at the second timeout. One wake gets one cycle."""
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",), power_cycle_cooldown_s=900))
        h.t += 901
        c.tick()
        self.assertEqual(h.acted["cycle"], ["a"], "cycled twice in one wake")

    def test_giving_up_is_written_on_the_node(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",), power_cycle_cooldown_s=0))
        self.assertIn(("a", "trouble", c.st["a"]["trouble"]), h.acted["note"])

    def test_a_restart_does_not_forget_a_node_it_gave_up_on(self):
        """Unmuted and alerted on the first tick after a restart, before any
        new wake has to rediscover it -- and never re-muted meanwhile."""
        h = Harness({"a": asleep(at=T0, trouble="not Ready after a wake: x")},
                    chassis={"a": "on"})
        h.t = T0
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertEqual(spy.alerts, {"a": "not Ready after a wake: x"})
        self.assertNotIn("a", spy.down, "re-muted a node it had given up on")

    def test_recovery_clears_the_note_and_the_alert_on_the_same_tick(self):
        """The observation still carries the note it just deleted."""
        h = Harness({"a": node(trouble="x")})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertIn(("a", "trouble", None), h.acted["note"])
        self.assertEqual(spy.alerts, {}, "alerted on a node that recovered")

    def test_a_cycled_node_that_recovered_is_not_in_trouble(self):
        """Cycled, came back, and was later slept the ordinary way: asleep,
        not broken."""
        h = Harness({"a": asleep(at=T0, dark_for=100.0,
                                 power_cycled_at=T0 - 5000.0)})
        h.t = T0
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertEqual(spy.alerts, {})
        self.assertIn("a", spy.down)

    def test_deleting_the_record_re_arms_the_cycle_at_once(self):
        """The documented re-arm. A copy of the record in memory, or a backoff
        waiting it out, would quietly overrule the operator for a day."""
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",)))
        h.t += 901
        c.tick()                                  # gives up; backs off a day
        self.assertEqual(h.acted["cycle"], ["a"])
        h.states["a"] = dataclasses.replace(h.states["a"],
                                            power_cycled_at=None)
        h.t += 60
        wedged(h, c)
        self.assertEqual(h.acted["cycle"], ["a", "a"], "re-arm ignored")

    def test_deleting_the_record_while_it_boots_re_arms_it_too(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",)))
        h.states["a"] = dataclasses.replace(h.states["a"],
                                            power_cycled_at=None)
        h.t += 901
        c.tick()                                  # post-cycle timeout
        self.assertLessEqual(c.st["a"]["wake_backoff_until"] - h.t, 901,
                             "held off a day by a record that is gone")

    def test_a_failed_cycle_does_not_lift_its_own_backoff(self):
        """The record it just wrote is not an operator re-arming it."""
        h = self.harness()

        class Breaks(_Power):
            def cycle(self, n):
                raise RuntimeError("BMC timeout")
        c = wedged(h, h.controller(nodes=("a",), power=Breaks(h)))
        self.assertEqual(c.st["a"]["trouble"],
                         "not Ready after a wake: the power cycle failed")
        c.tick()
        h.t += 60
        c.tick()
        self.assertEqual(len(h.logged("WAKE begin")), 1,
                         "retried a node inside its own backoff")

    def test_no_exception_text_is_published(self):
        """Trouble goes to Alertmanager and onto the node. An exception can
        carry anything -- ipmitool's once carried the BMC password."""
        h = self.harness()
        h.record_fails = True
        c = wedged(h, h.controller(nodes=("a",)))
        self.assertNotIn("apiserver said no", c.st["a"]["trouble"])

    def test_a_failed_trouble_note_is_retried(self):
        h = self.harness()
        real = h.note
        fails = {"n": 1}

        def flaky(n, key, value):
            if key == "trouble" and fails["n"]:
                fails["n"] -= 1
                raise RuntimeError("503")
            real(n, key, value)
        h.note = flaky
        c = wedged(h, h.controller(nodes=("a",), power_cycle_cooldown_s=0))
        self.assertIsNone(h.states["a"].trouble)
        c.tick()
        self.assertIsNotNone(h.states["a"].trouble, "never retried")

    def test_the_cooldown_expires(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",)))
        h.t += 86400
        c.st = {}
        wedged(h, c)
        self.assertEqual(h.acted["cycle"], ["a", "a"])

    def test_an_operator_cordon_at_the_timeout_blocks_the_cycle(self):
        """Checked on the FRESH read where the operation finishes: the cordon
        landed after tick() had already observed the node as unheld."""
        h = self.harness()
        c = h.controller(nodes=("a",))
        c.tick()
        h.t += 901
        h.fresh["a"] = crashed(cordoned=True, ours=False)
        c.tick()
        self.assertEqual(h.acted["cycle"], [], "cycled a node an operator holds")
        self.assertIsNone(c.st["a"]["phase"])
        self.assertNotIn("trouble", c.st["a"], "alerted over an operator")

    def test_running_work_blocks_the_cycle(self):
        """NotReady is not dead. busy() reads the work queue's own records."""
        h = self.harness(busy=["runner-1"])
        c = wedged(h, h.controller(nodes=("a",)))
        self.assertEqual(h.acted["cycle"], [], "power-cycled running work")
        self.assertIn("runner-1", c.st["a"]["trouble"])

    def test_a_busy_check_that_fails_reads_as_busy(self):
        h = self.harness()
        h._busy = RuntimeError("arc api down")
        c = wedged(h, h.controller(nodes=("a",)))
        self.assertEqual(h.acted["cycle"], [])
        self.assertIn("reads as busy", c.st["a"]["trouble"])

    def test_disabled_escalation_alerts_instead(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",), power_cycle_cooldown_s=0))
        self.assertEqual(h.acted["cycle"], [])
        self.assertIn("disabled", c.st["a"]["trouble"])
        h.t += 10 * 86400
        c.tick()
        self.assertEqual(len(h.logged("WAKE begin")), 1,
                         "kept waking a node it cannot do anything about")

    def test_a_backend_that_cannot_cycle_alerts_instead(self):
        h = self.harness()
        c = wedged(h, h.controller(nodes=("a",),
                                   power=_PowerWithoutCycle(h)))
        self.assertEqual(h.acted["record"], [],
                         "recorded a cycle it could not perform")
        self.assertIn("cannot power-cycle", c.st["a"]["trouble"])

    def test_a_chassis_that_is_off_at_the_timeout_is_not_cycled(self):
        """Distinguished in the log, and retried by the next wake as before."""
        h = self.harness()
        c = h.controller(nodes=("a",))
        c.tick()
        h.chassis["a"] = "off"
        h.t += 901
        c.tick()
        self.assertEqual(h.acted["cycle"], [])
        self.assertEqual(h.logged("WAKE TIMEOUT")[-1]["power"], "off")
        self.assertNotIn("trouble", c.st["a"])
        c.tick()
        self.assertEqual(len(h.logged("WAKE begin")), 2, "never retried")

    def test_a_readiness_read_that_fails_is_not_a_verdict(self):
        h = self.harness()
        c = h.controller(nodes=("a",))
        c.tick()
        h.t += 901
        h.fresh["a"] = RuntimeError("apiserver hiccup")
        c.tick()
        self.assertEqual(h.acted["cycle"], [], "cycled on no data")
        self.assertEqual(c.st["a"]["phase"], "waking", "gave up on no data")

    def test_a_node_dark_since_a_maintenance_visit_is_not_cycled(self):
        """It may be rebooting into firmware it just installed."""
        h = self.harness()
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "waking", "phase_since": h.t - 901,
                     "dark_after_visit": h.t - 600}
        c.tick()
        self.assertEqual(h.acted["cycle"], [], "cut power mid-update")
        self.assertNotIn("trouble", c.st["a"])
        self.assertGreater(c.st["a"]["wake_backoff_until"], h.t)

    def test_the_visit_guard_survives_a_restart(self):
        """The visit is written on the node before its power-on, so a restart
        mid-update cannot hand the node to a wake that would cut its power."""
        h = self.harness()
        h.states["a"] = crashed(cordoned=True, ours=True,
                                visited_at=h.t - 600)
        wedged(h, h.controller(nodes=("a",), maintenance_timeout_s=3600))
        self.assertEqual(h.acted["cycle"], [], "cut power mid-update")

    def test_a_visit_to_a_powered_node_does_not_renew_the_grace(self):
        """Only a visit that powers the node on can have started an update.
        Stamping every visit kept a wedged node inside the grace for ever."""
        h = Harness({"a": asleep(at=T0)}, chassis={"a": "on"})
        h.t = T0
        c = h.controller(nodes=("a",), maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.tick()
        self.assertTrue(h.logged("MAINTENANCE begin"))
        self.assertNotIn("visited", [k for _n, k, _v in h.acted["note"]])

    def test_a_cold_boot_is_not_mid_update(self):
        """Found OFF and powered on by this wake: whatever the visit was, the
        machine is not rebooting into it now."""
        h = self.harness()
        h.states["a"] = crashed(cordoned=True, ours=True,
                                visited_at=h.t - 600)
        h.chassis["a"] = "off"
        c = h.controller(nodes=("a",), maintenance_timeout_s=3600)
        c.tick()                                     # powers it on
        h.t += 901
        c.tick()
        self.assertEqual(h.acted["cycle"], ["a"])

    def test_a_stale_cycle_flag_does_not_outlive_its_wake(self):
        h = Harness({"a": asleep(at=T0)}, shortfall=400.0,
                    chassis={"a": "on"})
        h.t = T0
        c = h.controller(nodes=("a",), maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_at": h.t, "cycled": True,
                     "power_confirmed": True}
        c.tick()
        self.assertNotIn("cycled", c.st["a"])
        self.assertNotIn("power_confirmed", c.st["a"])

    def test_demand_taking_over_a_dark_visit_marks_it(self):
        h = Harness({"a": asleep(at=T0)}, shortfall=400.0,
                    chassis={"a": "on"})
        h.t = T0
        c = h.controller(nodes=("a",), maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_at": h.t, "visit_powered_on": True}
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "waking")
        self.assertIn("dark_after_visit", c.st["a"])

    def test_a_visit_that_found_the_node_powered_grants_no_grace(self):
        """Wedged since long before this visit, which powered nothing on:
        taken over by demand, it must still be cycled at the wake timeout.
        Renewing the grace here -- found by the long soak -- kept a wedged
        node from ever being cycled, one visit at a time."""
        h = Harness({"a": asleep(at=T0)}, shortfall=400.0,
                    chassis={"a": "on"})
        h.t = T0
        c = h.controller(nodes=("a",), maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_at": h.t, "visit_powered_on": False}
        c.tick()                                  # demand takes the visit
        h.t += 901
        c.tick()
        self.assertEqual(h.acted["cycle"], ["a"], "a no-op visit renewed "
                                                  "the mid-update grace")

    def test_dry_run_never_cycles(self):
        h = self.harness()
        c = h.controller(nodes=("a",), mode="dry_run")
        for _ in range(4):
            c.tick()
            h.t += 901
        self.assertEqual(h.acted["cycle"], [])
        self.assertEqual(h.acted["record"], [])

    def test_recovery_clears_the_alert_and_the_backoff(self):
        h = self.harness()
        spy = Spy()
        c = wedged(h, h.controller(nodes=("a",), notifier=spy,
                                   power_cycle_cooldown_s=0))
        c.tick()
        self.assertIn("a", spy.alerts)
        h.states["a"] = node(ready=True)
        c.tick()
        self.assertNotIn("a", spy.alerts, "alert outlived the recovery")
        self.assertNotIn("wake_backoff_until", c.st["a"])

    def test_a_chassis_that_does_not_power_on_is_passed_over(self):
        """A cold boot holds back other wakes, so it had better be real."""
        h = Harness({"a": asleep(at=1000.0), "b": asleep(at=1000.0)},
                    shortfall=50.0)
        h.power_on_takes = False
        c = h.controller()
        c.tick()
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a", "b"],
                         "demand stalled behind a node that never powered on")
        self.assertIn("did not power on", c.st["a"]["trouble"])


class TestOneNodeOfDemandWakesOneNode(unittest.TestCase):
    """A cold boot takes minutes and tick() runs every one of them.

    Measured in production: every demand episode for want=1 powered on three
    or four nodes, one a minute, until the first came up -- and min_uptime
    then held each of them up for 45 minutes.
    """

    def test_a_booting_node_counts_as_capacity_on_its_way(self):
        h = Harness({n: asleep(at=1000.0) for n in "abcd"}, shortfall=50.0)
        c = h.controller(nodes="abcd")
        for _ in range(5):
            c.tick()
            h.t += 60
        self.assertEqual(h.acted["on"], ["a"])

    def test_a_node_found_powered_does_not_hold_demand_back(self):
        """It may be wedged, and never arrive."""
        h = Harness({"a": crashed(), "b": asleep(at=1000.0)}, shortfall=50.0,
                    chassis={"a": "on"})
        c = h.controller()
        c.tick()
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["b"])

    def test_a_wake_that_completes_counts_on_the_same_tick(self):
        """Observed cordoned at the start of the tick, uncordoned by the wake
        partway through it. Not counted, the tick wakes another node for the
        demand this one has just met."""
        h = Harness({"a": node(cordoned=True, ours=True),
                     "b": asleep(at=1000.0)}, shortfall=50.0)
        c = h.controller()
        c.st["a"] = {"phase": "waking", "phase_since": h.t, "booting": True,
                     "power_confirmed": True}
        c.tick()
        self.assertIn(("a", False), h.acted["cordon"])
        self.assertEqual(h.acted["on"], [], "woke a second node for one's worth")

    def test_a_bmc_that_fails_at_wake_begin_does_not_stall_the_tick(self):
        """It is passed over, not first in line forever."""
        h = Harness({"a": asleep(at=1000.0), "b": asleep(at=1000.0)},
                    shortfall=50.0,
                    chassis={"a": RuntimeError("BMC unreachable")})
        c = h.controller()
        c.tick()                                  # must not raise
        self.assertGreater(c.st["a"]["wake_backoff_until"], h.t)
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["b"])

    def test_more_demand_still_wakes_more_nodes(self):
        h = Harness({n: asleep(at=1000.0) for n in "abcd"}, shortfall=250.0)
        c = h.controller(nodes="abcd")
        for _ in range(5):
            c.tick()
            h.t += 60
        self.assertEqual(h.acted["on"], ["a", "b", "c"])


class TestMuteOnlyWhatWePutDown(unittest.TestCase):
    """#14: a crashed node was muted exactly like a slept one, for 21 hours."""

    def tick(self, states, chassis=None, st=None, **cfg):
        h = Harness(dict(states), chassis=chassis)
        spy = Spy()
        c = h.controller(nodes=tuple(states), notifier=spy, **cfg)
        c.st.update(st or {})
        c.tick()
        return h, c, spy

    def test_a_node_that_crashed_in_service_is_not_muted(self):
        _h, _c, spy = self.tick({"a": crashed()}, chassis={"a": "on"})
        self.assertNotIn("a", spy.down, "muted a crash")
        self.assertIn("a", spy.up)

    def test_a_node_an_operator_holds_is_not_muted(self):
        _h, _c, spy = self.tick({"a": crashed(cordoned=True)})
        self.assertNotIn("a", spy.down)

    def test_a_node_we_slept_is_muted(self):
        _h, _c, spy = self.tick({"a": asleep(at=1000.0)})
        self.assertIn("a", spy.down)

    def test_a_node_we_are_waking_stays_muted(self):
        _h, _c, spy = self.tick({"a": asleep(at=1000.0)}, chassis={"a": "on"},
                                st={"a": {"phase": "waking",
                                          "phase_since": 1000.0,
                                          "booting": True}})
        self.assertIn("a", spy.down)

    def test_a_node_that_went_dark_mid_drain_is_not_muted(self):
        """The sleep announces itself immediately before cutting power, and
        leaves the phase in the same step. Dark while still draining is a
        crash."""
        h = Harness({"a": crashed(cordoned=True, ours=True)},
                    busy=["runner-1"])      # so the drain goes no further
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.tick()
        self.assertNotIn("a", spy.down)

    def test_a_node_in_trouble_is_unmuted_and_alerted_in_the_same_tick(self):
        """Its own alerts are what should arrive now, alongside ours."""
        _h, _c, spy = self.tick({"a": asleep(at=1000.0)},
                                st={"a": {"trouble": "wedged"}})
        self.assertIn("a", spy.alerts)
        self.assertIn("a", spy.up)
        self.assertNotIn("a", spy.down)

    def test_a_sleeping_node_found_powered_is_unmuted_after_a_grace(self):
        """An OS wedged mid-shutdown, or a wake a restart forgot."""
        h = Harness({"a": asleep(at=1000.0)}, chassis={"a": "on"})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertEqual(spy.down, ["a"], "no grace for a node still booting")
        h.t += 901
        c.tick()
        self.assertIn("a", spy.alerts)
        self.assertEqual(spy.up, ["a"])

    def test_a_sleeping_node_whose_bmc_cannot_be_read_is_unmuted(self):
        """Bounded like everything else: nobody can vouch for it."""
        h = Harness({"a": asleep(at=1000.0)},
                    chassis={"a": RuntimeError("BMC unreachable")})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertEqual(spy.down, ["a"], "no grace for a flaky BMC")
        h.t += 901
        c.tick()
        self.assertIn("a", spy.alerts)

    def test_a_bmc_that_answers_again_takes_its_trouble_back(self):
        h = Harness({"a": asleep(at=1000.0)},
                    chassis={"a": RuntimeError("BMC unreachable")})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        h.t += 901
        c.tick()
        self.assertIn("a", spy.alerts)
        h.chassis["a"] = "off"
        c.tick()
        self.assertEqual(spy.alerts, {}, "alerted on a node that reads off")
        self.assertEqual(spy.down[-1:], ["a"], "left a sleeping node loud")

    def test_a_slow_shutdown_that_finishes_is_muted_again(self):
        h = Harness({"a": asleep(at=1000.0, trouble=
                                 "did not power off within 600s of a soft "
                                 "shutdown")}, chassis={"a": "off"})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertEqual(spy.alerts, {})
        self.assertIn(("a", "trouble", None), h.acted["note"])

    def test_a_failed_wake_is_not_taken_back_by_a_power_reading(self):
        """Reading off says nothing about a node that would not power on."""
        h = Harness({"a": asleep(at=1000.0, trouble=
                                 "not Ready after a wake: the chassis did not "
                                 "power on")})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertIn("a", spy.alerts)

    def test_a_node_dark_after_a_visit_is_not_called_wedged(self):
        h = Harness({"a": asleep(at=1000.0, visited_at=1000.0)},
                    chassis={"a": "on"})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy,
                         maintenance_timeout_s=3600)
        c.tick()
        h.t += 901
        c.tick()
        self.assertEqual(spy.alerts, {}, "alerted inside the visit's bound")

    def test_a_demand_signal_outage_does_not_let_alerts_lapse(self):
        """Alerts carry a TTL. The reconcile needs only node state."""
        h = Harness({"a": asleep(at=1000.0, trouble="wedged")})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        h.shortfall = lambda: (_ for _ in ()).throw(RuntimeError("prom down"))
        with self.assertRaises(RuntimeError):
            c.tick()
        self.assertIn("a", spy.alerts)

    def test_a_node_confirmed_off_is_not_asked_again(self):
        h = Harness({"a": asleep(at=1000.0)})
        c = h.controller(nodes=("a",))
        reads = []
        real = h.power

        class Counting(type(real)):
            def state(self, n):
                reads.append(n)
                return super().state(n)
        c.power = Counting(h)
        for _ in range(3):
            c.tick()
        self.assertEqual(reads, ["a"], "polled a sleeping node's BMC every tick")

    def test_a_notifier_from_before_alerts_still_works(self):
        class Old:
            def __init__(self):
                self.down = []

            def going_down(self, n):
                self.down.append(n)

            def back_up(self, n):
                pass
        h = Harness({"a": asleep(at=1000.0)})
        old = Old()
        c = h.controller(nodes=("a",), notifier=old)
        c.st["a"] = {"trouble": "wedged"}
        c.tick()
        self.assertEqual(h.logged("reconcile failed"), [])


class TestShutdownIsConfirmed(unittest.TestCase):
    """A soft shutdown is a request."""

    def slept(self):
        h = Harness({"a": node(cordoned=True, ours=True)})
        spy = Spy()
        c = h.controller(nodes=("a",), notifier=spy)
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.sleep("a", h.states["a"])
        self.assertEqual(h.acted["off"], ["a"])
        return h, c, spy

    def test_a_node_still_ready_after_soft_off_is_not_slept_again(self):
        """The kubelet reports Ready for most of a minute after the OS starts
        going down. Ready + cordoned + ours with nothing in flight reads as
        stranded, and the repair used to send a second soft-off into the
        shutdown -- five times a week in production."""
        h, c, spy = self.slept()
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["off"], ["a"], "slept a node mid-shutdown")
        self.assertEqual(spy.down[-1:], ["a"], "unmuted mid-shutdown")

    def test_power_off_is_confirmed(self):
        h, c, _spy = self.slept()
        h.chassis["a"] = "off"
        h.states["a"] = asleep(at=h.t)
        h.t += 60
        c.tick()
        self.assertIsNone(c.st["a"]["phase"])
        self.assertTrue(h.logged("SLEEP complete"))

    def test_a_node_that_ignores_the_request_is_alerted_while_ready(self):
        """The common way a node will not power off: the OS ignores the
        request and stays Ready. Ready must not clear it."""
        h, c, spy = self.slept()
        for _ in range(12):
            h.t += 60
            c.tick()
        self.assertIn("a", spy.alerts, "a node that will not power off, unsaid")
        self.assertTrue(h.logged("SLEEP FAILED"))

    def test_a_retried_shutdown_stays_muted_and_alerted(self):
        """It is going down now, and it failed to before: both are true."""
        h, c, spy = self.slept()
        while not h.logged("SLEEP FAILED"):
            h.t += 60
            c.tick()                        # ignored
        h.t += 60
        c.tick()                            # raised by the next reconcile
        self.assertIn("a", spy.alerts)
        c.st["a"].update(phase="powering_off", phase_since=h.t)
        h.chassis["a"] = "off"              # the retry is heard; still Ready
        h.t += 30
        spy.down.clear()
        c.tick()
        self.assertEqual(spy.down, ["a"], "unmuted a node going down")
        self.assertIn("a", spy.alerts)

    def test_a_node_returned_to_service_is_no_longer_alerted(self):
        """Raised while metalnap keeps asking; giving up ends it."""
        h, c, spy = self.slept()
        while not h.logged("SLEEP FAILED"):
            h.t += 60
            c.tick()
        h.t += 60
        c.tick()
        self.assertIn("a", spy.alerts)
        h.states["a"] = node()                    # abandoned: uncordoned
        h.t += 60
        c.tick()
        self.assertEqual(spy.alerts, {})

    def test_the_bmc_reading_off_is_not_enough_while_the_node_reads_ready(self):
        """Ending the phase there handed the node to the stranded repair,
        which sent a second soft-off into a chassis already off."""
        h, c, _spy = self.slept()
        h.chassis["a"] = "off"                 # the kubelet still says Ready
        for _ in range(3):
            h.t += 15
            c.tick()
        self.assertEqual(h.acted["off"], ["a"], "second soft-off")
        self.assertEqual(c.st["a"]["phase"], "powering_off")

    def test_a_restart_mid_shutdown_resumes_waiting(self):
        """The shutdown is written on the node before the request. Without it
        the restarted controller reads Ready + cordoned + ours as stranded,
        unmutes it, and sleeps it again -- a second soft-off."""
        h, c, spy = self.slept()
        self.assertIsNotNone(h.states["a"].shutdown_at)
        h.chassis["a"] = "off"                 # the kubelet still says Ready
        c.st = {}
        spy.up.clear()
        for _ in range(2):
            h.t += 20
            c.tick()
        self.assertEqual(h.acted["off"], ["a"], "asked a second time")
        self.assertNotIn("a", spy.up, "unmuted mid-shutdown")
        h.chassis["a"] = "off"
        h.states["a"] = asleep(at=h.t, shutdown_at=h.states["a"].shutdown_at)
        h.t += 60
        c.tick()
        self.assertIn(("a", "shutdown", None), h.acted["note"])

    def test_a_shutdown_that_never_finishes_is_reported_not_forced(self):
        h, c, spy = self.slept()
        h.chassis["a"] = "on"
        h.states["a"] = crashed(cordoned=True, ours=True)
        for _ in range(12):
            h.t += 60
            c.tick()
        self.assertIn("a", spy.alerts, "a wedged shutdown stayed muted")
        self.assertEqual((h.acted["off"], h.acted["cycle"]), (["a"], []),
                         "forced a shutdown")


class TestOwnershipMark(unittest.TestCase):
    """An operator's cordon outranks the controller -- even one that lands
    on a node whose mark we forgot to take back."""

    def test_a_mark_without_a_cordon_is_cleared(self):
        """kubectl uncordon leaves our annotation behind. The operator's next
        cordon would then read as ours, and stop deferring to them."""
        h = Harness({"a": node(ours=True)}, shortfall=50.0)  # keep it awake
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["disown"], ["a"])
        self.assertEqual(h.acted["cordon"], [],
                         "wrote the cordon, and could undo an operator's")

    def test_an_uncordon_mid_drain_keeps_the_node(self):
        h = Harness({"a": node()})
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        c.tick()
        self.assertEqual(h.acted["off"], [],
                         "powered off a node an operator put back")
        self.assertIsNone(c.st["a"]["phase"])
        h.t += 60
        c.tick()
        self.assertNotIn(("a", True), h.acted["cordon"],
                         "re-cordoned the node they just put back")

    def test_a_node_handed_to_a_human_is_never_visited(self):
        """Handing it over is removing our mark; until then, a visit must not
        power on a node metalnap has said needs a person."""
        h = Harness({"a": asleep(trouble="wedged")})
        h.t = T0
        c = h.controller(nodes=("a",), maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.tick()
        self.assertEqual(h.acted["on"], [])


class TestEscalationConfig(unittest.TestCase):
    def test_a_negative_cooldown_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(mode="on", power_cycle_cooldown_s=-1).validate()

    def test_a_cooldown_shorter_than_a_wake_is_rejected(self):
        """The next cycle would land before the last had a chance to work."""
        with self.assertRaises(ValueError):
            Config(mode="on", power_cycle_cooldown_s=600,
                   wake_timeout_s=900).validate()

    def test_zero_disables_and_validates(self):
        self.assertIsNotNone(
            Config(mode="on", power_cycle_cooldown_s=0).validate())

    def test_a_zero_shutdown_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(mode="on", shutdown_timeout_s=0).validate()

    def test_a_suspicious_combination_is_a_warning_not_an_error(self):
        cfg = Config(mode="on", wake_sustain_s=1200, sleep_sustain_s=120)
        self.assertIsNotNone(cfg.validate())
        self.assertTrue(cfg.warnings())


class FakeAlertmanager:
    """Just enough of the v2 API to hold silences and receive alerts."""

    class R:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP %d" % self.status_code)

    def __init__(self, plural_delete=False):
        self.silences, self.alerts, self.deletes = [], [], []
        self.plural_delete = plural_delete
        self.next_id = 0

    def get(self, url, timeout=None):
        return self.R(200, [dict(s) for s in self.silences])

    def post(self, url, json=None, timeout=None):
        if url.endswith("/api/v2/alerts"):
            self.alerts.extend(json)
            return self.R(200)
        self.next_id += 1
        self.silences.append(dict(json, id="s%d" % self.next_id,
                                  status={"state": "active"}))
        return self.R(200)

    def delete(self, url, timeout=None):
        self.deletes.append(url)
        sid = url.rsplit("/", 1)[1]
        plural = "/api/v2/silences/" in url
        if plural != self.plural_delete:
            return self.R(404)
        self.silences = [s for s in self.silences if s["id"] != sid]
        return self.R(200)


class TestAlertmanagerNotifier(unittest.TestCase):
    def setUp(self):
        from metalnap.notify import alertmanager
        self.mod = alertmanager
        self.real = alertmanager.requests
        self.am = FakeAlertmanager()
        alertmanager.requests = self.am

    def tearDown(self):
        self.mod.requests = self.real

    def notifier(self, **kw):
        return self.mod.AlertmanagerNotifier("http://am", **kw)

    def test_one_silence_per_label(self):
        """kube-state-metrics alerts name the node in `node`; node-exporter
        alerts, in `instance`. One silence cannot match either."""
        self.notifier().going_down("node1")
        self.assertEqual(
            sorted(m["name"] for s in self.am.silences for m in s["matchers"]
                   if m["value"] == "node1"), ["instance", "node"])

    def test_our_own_alert_is_never_silenced_by_our_own_silence(self):
        """It carries node= and instance=, so it routes like the node's other
        alerts -- and would be muted on exactly the node it is about."""
        self.notifier().going_down("node1")
        own = {"name": "alertname", "value": "MetalnapNodeNeedsAttention",
               "isRegex": False, "isEqual": False}
        for s in self.am.silences:
            self.assertIn(own, s["matchers"])

    def test_going_down_is_idempotent(self):
        n = self.notifier()
        n.going_down("node1")
        n.going_down("node1")
        self.assertEqual(len(self.am.silences), 2)

    def test_a_silence_from_before_this_change_is_replaced(self):
        """Its comment is its identity, so it is found -- and its shape is not
        what we create now, so it is replaced, new ones first."""
        self.am.silences.append({
            "id": "old", "status": {"state": "active"},
            "comment": "metalnap: node1 is deliberately powered down",
            "matchers": [{"name": "instance", "value": "node1",
                          "isRegex": False, "isEqual": True}]})
        self.notifier().going_down("node1")
        self.assertNotIn("old", [s["id"] for s in self.am.silences])
        self.assertEqual(len(self.am.silences), 2)

    def test_extra_matchers_narrow_every_silence(self):
        self.notifier(matchers=['alertname=~"KubeNode.*"']).going_down("node1")
        for s in self.am.silences:
            self.assertIn({"name": "alertname", "value": "KubeNode.*",
                           "isRegex": True, "isEqual": True}, s["matchers"])

    def test_a_reconfigured_silence_is_replaced(self):
        """Left in place it silences what the new matchers were written to
        let through."""
        self.notifier().going_down("node1")
        self.notifier(matchers=['alertname="KubeNodeUnreachable"']
                      ).going_down("node1")
        self.assertEqual(len(self.am.silences), 2)
        for s in self.am.silences:
            self.assertEqual(len(s["matchers"]), 3)

    def test_a_failed_replacement_leaves_the_old_silence(self):
        """New first, then old: a POST Alertmanager rejects must leave the
        node over-silenced, not bare."""
        self.am.silences.append({
            "id": "old", "status": {"state": "active"},
            "comment": "metalnap: node1 is deliberately powered down",
            "matchers": [{"name": "instance", "value": "node1",
                          "isRegex": False, "isEqual": True}]})
        real_post = self.am.post
        self.am.post = lambda *a, **k: FakeAlertmanager.R(400)
        with self.assertRaises(RuntimeError):
            self.notifier().going_down("node1")
        self.am.post = real_post
        self.assertEqual([s["id"] for s in self.am.silences], ["old"])

    def test_back_up_deletes_on_the_singular_path(self):
        n = self.notifier()
        n.going_down("node1")
        n.back_up("node1")
        self.assertEqual(self.am.silences, [])
        self.assertTrue(all("/api/v2/silence/" in u for u in self.am.deletes))

    def test_back_up_falls_back_to_the_plural_path(self):
        self.am.plural_delete = True
        n = self.notifier()
        n.going_down("node1")
        n.back_up("node1")
        self.assertEqual(self.am.silences, [])

    def test_another_nodes_silence_is_untouched(self):
        n = self.notifier()
        n.going_down("node1")
        n.going_down("node2")
        n.back_up("node1")
        self.assertEqual(len(self.am.silences), 2)

    def test_an_alert_keeps_its_start(self):
        """Omitted, Alertmanager sets startsAt to endsAt -- in the future."""
        n = self.notifier()
        n.alert("node2", "wedged")
        n.alert("node2", "wedged")
        first, second = self.am.alerts
        self.assertEqual(first["startsAt"], second["startsAt"])
        self.assertLess(first["startsAt"], first["endsAt"])

    def test_alert_and_clear(self):
        n = self.notifier()
        n.alert("node2", "wedged")
        a = self.am.alerts[-1]
        self.assertEqual(a["labels"]["node"], "node2")
        self.assertEqual(a["annotations"]["description"], "wedged")
        n.clear_alert("node2")
        self.assertEqual(len(self.am.alerts), 2, "never resolved")
        n.clear_alert("node2")
        self.assertEqual(len(self.am.alerts), 2, "one POST per tick for nothing")

    def test_matcher_syntax(self):
        p = self.mod.parse_matcher
        self.assertEqual(p('a="b"'), {"name": "a", "value": "b",
                                      "isRegex": False, "isEqual": True})
        self.assertEqual(p('a!="b"')["isEqual"], False)
        self.assertEqual(p('a=~"x|y"')["isRegex"], True)
        self.assertEqual(p('a!~"x"'), {"name": "a", "value": "x",
                                       "isRegex": True, "isEqual": False})
        self.assertEqual(p('a="say \\"hi\\""')["value"], 'say "hi"')
        self.assertEqual(p('a=~"k8s\\d+"')["value"], "k8s\\d+",
                         "ate a regex escape")
        self.assertEqual(p('a="c:\\\\x"')["value"], "c:\\x")
        for re2 in ('a=~"(?!Watchdog).*"', 'a=~"(a)\\1"', 'a=~"("',
                    'a=~"(?>K).*"', 'a=~"K.*\\Z"', 'a=~"K.*+"',
                    'a=~"(?x)K .*"', 'a=~"(?#c)K"', 'a=~"(?a)K"'):
            with self.assertRaises(ValueError, msg=re2):
                p(re2)
        self.assertEqual(p("severity=warning")["value"], "warning")
        for bad in ("", "a", "=b", 'a=="b"', 'a="b" c="d"'):
            with self.assertRaises(ValueError, msg=bad):
                p(bad)

    def test_no_labels_is_rejected(self):
        with self.assertRaises(ValueError):
            self.notifier(labels=())


class TestIpmiPower(unittest.TestCase):
    """The BMC password once rode in ipmitool's argv -- and so in every
    exception, log line, alert and node annotation that quoted one."""

    def run_with(self, returncode=0, stdout="", timeout=False):
        import subprocess
        from metalnap.power import ipmi
        seen = {}

        def fake(cmd, **kw):
            seen.update(cmd=cmd, env=kw.get("env"))
            if timeout:
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, returncode, stdout,
                                               "Error: unable to establish")
        real, ipmi.subprocess.run = ipmi.subprocess.run, fake
        try:
            p = ipmi.IpmiPower(lambda n: n + "-ipmi.", "admin", "hunter2")
            try:
                return seen, p.state("node2"), None
            except Exception as e:                # noqa: BLE001
                return seen, None, e
        finally:
            ipmi.subprocess.run = real

    def test_the_password_is_not_in_argv(self):
        seen, power, _ = self.run_with(stdout="Chassis Power is on")
        self.assertEqual(power, "on")
        # The whole command line, not element by element: "-Phunter2" or
        # "IPMI_PASSWORD=hunter2" in one argument would pass an element check.
        self.assertNotIn("hunter2", " ".join(seen["cmd"]))
        self.assertIn("-E", seen["cmd"])
        self.assertEqual(seen["env"]["IPMI_PASSWORD"], "hunter2")

    def test_a_failure_does_not_quote_the_password(self):
        for kw in ({"returncode": 1}, {"timeout": True}):
            _seen, _power, err = self.run_with(**kw)
            self.assertIsNotNone(err)
            self.assertNotIn("hunter2", str(err))
            self.assertNotIn("hunter2", repr(err.__cause__ or ""))
            self.assertTrue(err.__suppress_context__ or err.__context__
                            is None)


def held(state=None, reason="kernel 6.8", **kw):
    """A node an operator has asked for maintenance, in whatever state.

    `kw` applies either way: to asleep() when no state is given, and over the
    state when one is. Dropped in the second case, `held(crashed(),
    maintenance_started_at=...)` quietly built a request never taken up.
    """
    if state is None:
        return dataclasses.replace(asleep(**kw), maintenance=reason)
    return dataclasses.replace(state, maintenance=reason, **kw)


class TestMaintenanceMode(unittest.TestCase):
    """An operator asks for a node: powered on once, then left alone.

    Every remedy this controller has for a node behaving oddly -- sleep it,
    drain it, cycle it, mute it, alert on it -- is wrong for a node somebody
    is upgrading, because that node reboots and powers off on purpose.
    """

    def harness(self, states, **kw):
        h = Harness(states, **kw)
        h.t = T0
        return h

    def test_an_asleep_node_is_powered_on_once(self):
        h = self.harness({"a": held()})
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["on"], ["a"], "a request never woke the node")
        h.t += 60
        c.tick()                          # still dark: it is booting
        self.assertEqual(h.acted["on"], ["a"], "powered on a second time")

    def test_the_request_is_on_record_before_the_power_on(self):
        """A power-on this process cannot see is one it makes again -- after
        the operator has switched the machine off to work on it."""
        h = self.harness({"a": held()})

        class Checked(_Power):
            def on(self, n):
                assert self.h.states[n].maintenance_started_at is not None, \
                    "powered on before the request was on record"
                super().on(n)
        c = h.controller(nodes=("a",), power=Checked(h))
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])

    def test_no_record_means_no_power_on(self):
        h = self.harness({"a": held()})
        real = h.note

        def failing(n, key, value):
            if key == "maintenance-started":
                raise RuntimeError("apiserver said no")
            real(n, key, value)
        h.note = failing
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["on"], [], "powered on without a record")
        self.assertTrue(h.logged("could not record the maintenance request"))

    def test_a_failed_power_on_comes_off_the_record_and_is_retried(self):
        h = self.harness({"a": held()})

        class Refusing(_Power):
            fail = True

            def on(self, n):
                if Refusing.fail:
                    raise RuntimeError("BMC unreachable")
                super().on(n)
        c = h.controller(nodes=("a",), power=Refusing(h))
        c.tick()
        self.assertIsNone(h.states["a"].maintenance_started_at,
                          "a request that powered nothing on stayed on record, "
                          "so it would never be tried again")
        Refusing.fail = False
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])

    def test_an_operator_who_powers_it_off_is_not_overruled(self):
        """The screwdriver rule. Mid-maintenance, OFF is on purpose."""
        h = self.harness({"a": held()})
        c = h.controller(nodes=("a",))
        c.tick()                                        # powered on, recorded
        h.states["a"] = dataclasses.replace(h.states["a"], ready=True,
                                            ready_since=h.t, down_since=None)
        c.tick()                                        # up
        h.states["a"] = dataclasses.replace(h.states["a"], ready=False,
                                            ready_since=None, down_since=h.t)
        h.chassis["a"] = "off"                          # operator: poweroff
        for _ in range(5):
            h.t += 600
            c.tick()
        self.assertEqual(h.acted["on"], ["a"],
                         "powered back on a machine the operator switched off")

    def test_removing_the_record_asks_again(self):
        h = self.harness({"a": held(maintenance_started_at=T0 - 600)})
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["on"], [], "a request on record was re-run")
        h.states["a"] = dataclasses.replace(h.states["a"],
                                            maintenance_started_at=None)
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])

    def test_a_source_that_cannot_record_never_powers_on(self):
        """No record, no power-on -- including where there is nowhere to
        keep one. A NodeSource without note() would otherwise power the node
        on every tick it is dark, operator's screwdriver or not."""
        h = self.harness({"a": held()})

        class NoNotes:
            def state(self, n):
                return h.state(n)

            def set_cordon(self, n, v):
                h.set_cordon(n, v)
        c = Controller(nodes=["a"], node_source=NoNotes(), power=h.power,
                       signal=h, drain=h, config=Config(mode="on"),
                       clock=lambda: h.t,
                       log=lambda lvl, msg, **kv: h.logs.append((msg, kv)))
        c.tick()
        self.assertEqual(h.acted["on"], [], "powered on with no record")
        self.assertTrue(h.logged("could not record the maintenance request"))

    def test_a_power_on_that_keeps_failing_is_given_up_on(self):
        """Bound every retry, and say so when the bound is hit."""
        h = self.harness({"a": held()})

        class Dead(_Power):
            def on(self, n):
                self.h.acted["on"].append(n)
                raise RuntimeError("BMC unreachable")
        c = h.controller(nodes=("a",), power=Dead(h))
        for _ in range(30):
            c.tick()
            h.t += 60
        self.assertEqual(len(h.acted["on"]), 16,
                         "not bounded by the wake timeout")
        self.assertIsNotNone(h.states["a"].maintenance_started_at,
                             "gave up but left nothing to stop a retry")
        self.assertEqual(len(h.logged("giving up")), 1)

    def test_a_visit_waits_a_tick_behind_a_maintenance_power_on(self):
        """Serialised against each other, so one does not share a tick with
        the other's power-on either."""
        h = self.harness({"a": held(), "b": asleep(dark_for=90_000.0)})
        c = h.controller(maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a", "b"])

    def test_one_power_on_per_tick(self):
        """Someone asking for the whole fleet is not asking for a rack to power
        on in unison."""
        h = self.harness({n: held() for n in "abc"})
        c = h.controller(nodes=("a", "b", "c"))
        for want in (["a"], ["a", "b"], ["a", "b", "c"]):
            c.tick()
            self.assertEqual(h.acted["on"], want)
            h.t += 60

    def test_a_node_already_up_is_recorded_and_does_not_queue(self):
        h = self.harness({"a": held(), "b": held(node(ready=True)),
                          "c": held()})
        c = h.controller(nodes=("a", "b", "c"))
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])
        self.assertIsNotNone(h.states["b"].maintenance_started_at,
                             "a node already up waited its turn to be recorded")
        self.assertIsNone(h.states["c"].maintenance_started_at)

    def test_a_node_already_up_is_never_powered_on_later(self):
        """Recorded even though there was nothing to power on, so an operator
        who later switches it off is not overruled either."""
        h = self.harness({"a": held(node(ready=True))})
        c = h.controller(nodes=("a",))
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], ready=False,
                                            ready_since=None, down_since=h.t)
        h.chassis["a"] = "off"
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], [])

    def test_a_woken_node_stays_cordoned_and_is_never_slept(self):
        """Up, carrying our cordon, doing nothing: the exact shape of a
        stranded node, which the repair would put straight back to sleep."""
        h = self.harness({"a": held()})
        c = h.controller(nodes=("a",))
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], ready=True,
                                            ready_since=h.t, down_since=None)
        for _ in range(10):
            h.t += 3600
            c.tick()
        self.assertEqual(h.acted["cordon"], [], "changed the cordon")
        self.assertEqual(h.acted["off"], [], "powered off a held node")
        self.assertIsNone(c.st["a"].get("phase"))

    def test_a_node_in_service_is_never_slept(self):
        h = self.harness({"a": held(node(ready=True)), "b": asleep()})
        c = h.controller()
        for _ in range(10):
            h.t += 3600
            c.tick()
        self.assertEqual(h.acted["cordon"], [], "took a held node out of "
                                                "service")
        self.assertEqual(h.acted["off"], [])

    def test_a_reboot_is_neither_cycled_nor_muted_nor_alerted_on(self):
        """Dark, powered and ours for longer than a wake timeout is a wedge
        to every other part of this controller. Here it is a reboot."""
        spy = Spy()
        h = self.harness({"a": held(maintenance_started_at=T0 - 60)},
                         shortfall=400.0, chassis={"a": "on"})
        c = h.controller(nodes=("a",), notifier=spy)
        c.st["want_high_since"] = 0.0
        for _ in range(6):
            h.t += 900
            c.tick()
        self.assertEqual(h.acted["cycle"], [], "power-cycled a held node")
        self.assertEqual(h.acted["on"], [], "demand woke a held node")
        self.assertEqual(spy.down, [], "muted a node an operator is using")
        self.assertIn("a", spy.up)
        self.assertEqual(spy.alerts, {}, "alerted on a node an operator has")
        self.assertIsNone(c.st["a"].get("phase"))

    def test_trouble_is_handed_over_with_the_node(self):
        """A human has it; they know."""
        spy = Spy()
        h = self.harness({"a": held(trouble="wedged",
                                    maintenance_started_at=T0 - 60)})
        c = h.controller(nodes=("a",), notifier=spy)
        c.tick()
        self.assertIsNone(h.states["a"].trouble)
        self.assertEqual(spy.alerts, {})

    def test_a_held_node_is_not_reported_as_crashed(self):
        h = self.harness({"a": held(crashed(), maintenance_started_at=T0)})
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertFalse(h.logged("metalnap did not put them down"))

    def test_a_node_warming_up_is_recorded_straight_away(self):
        """Up and in service is up and in service, warmup or not. Waiting for
        the warmup to finish left a window -- up to WARMUP_TIMEOUT_S -- in
        which an operator's power-off was undone the moment it ended.
        Found by the simulation harness with a slow warmup."""
        h = self.harness({"a": held(node(ready=True))})
        c = h.controller(nodes=("a",))

        class Slow:
            def start(self, n):
                pass

            def done(self, n):
                return False

            def cleanup(self, n):
                pass
        c.warmup = Slow()
        c.st["a"] = {"phase": "warming", "phase_since": h.t}
        c.tick()
        self.assertIsNotNone(h.states["a"].maintenance_started_at,
                             "a node already up waited for its warmup to be "
                             "recorded")
        h.states["a"] = dataclasses.replace(h.states["a"], ready=False,
                                            ready_since=None, down_since=h.t)
        h.chassis["a"] = "off"                          # operator: poweroff
        for _ in range(20):
            h.t += 60
            c.tick()
        self.assertEqual(h.acted["on"], [],
                         "powered back on a machine the operator switched off")

    def test_giving_up_on_a_wake_is_forgotten_by_the_hold(self):
        """metalnap's verdict on a node it could not wake predates whatever
        the operator then did to it. Kept, a node fixed and given back dark
        was passed over by demand for up to a day, with nothing alerting.
        Found by the simulation harness (seed 4766, 1800 ticks)."""
        h = self.harness({"a": held(maintenance_started_at=T0 - 600)},
                         shortfall=150.0)
        c = h.controller(nodes=("a",))
        c.st["a"] = {"wake_backoff_until": h.t + 86400,
                     "backoff_record": None,
                     "trouble": "not Ready after a wake: wedged"}
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], maintenance=None)
        c.st["want_high_since"] = 0.0
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a"],
                         "demand passed over a node the operator fixed")

    def test_a_request_abandons_a_wake(self):
        """Well inside the wake timeout, so it is the abandon that is tested
        and not the timeout's own check: a cold boot left counted as capacity
        on its way would hold demand back behind a node it can never have."""
        h = self.harness({"a": held(), "b": asleep()}, shortfall=150.0,
                         chassis={"a": "on"})
        c = h.controller()
        c.st["a"] = {"phase": "waking", "phase_since": h.t, "booting": True,
                     "power_confirmed": True}
        c.st["want_high_since"] = 0.0
        h.t += 60
        c.tick()
        self.assertIsNone(c.st["a"]["phase"], "kept waking a held node")
        self.assertFalse(c.st["a"]["booting"])
        self.assertEqual(h.acted["on"], ["b"],
                         "demand waited on a held node's boot")
        self.assertEqual(h.acted["cordon"], [])

    def test_a_request_abandons_a_sleep_without_backing_off(self):
        h = self.harness({"a": held(node(ready=True, cordoned=True, ours=True,
                                         ours_since=T0))},
                         idle=["runner-1"])
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "sleeping", "phase_since": h.t}
        for _ in range(3):
            h.t += 60
            c.tick()
        self.assertEqual(h.acted["released"], [], "kept draining a held node")
        self.assertEqual(h.acted["off"], [])
        self.assertEqual(h.acted["cordon"], [], "changed the cordon")
        self.assertNotIn("cooldown_until", c.st["a"])

    def test_a_request_ends_a_visit(self):
        h = self.harness({"a": held(node(ready=True, cordoned=True,
                                         ours=True))})
        c = h.controller(nodes=("a",), maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_timeout_s=3600)
        c.st["a"] = {"phase": "maintaining", "phase_since": h.t,
                     "maintenance_until": h.t + 300}
        c.tick()
        h.t += 3600
        c.tick()
        self.assertIsNone(c.st["a"]["phase"])
        self.assertEqual(c.st["a"]["maintenance_at"], T0,
                         "the schedule was not advanced past the visit")
        self.assertEqual(h.acted["off"], [], "the visit's window put a held "
                                             "node to sleep")

    def test_a_shutdown_already_requested_finishes_then_powers_on(self):
        """A soft-off cannot be recalled. Seen through to OFF, then undone --
        and not recorded on the stale Ready this tick began with."""
        h = self.harness({"a": held(node(ready=True, cordoned=True,
                                         ours=True))}, chassis={"a": "off"})
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "powering_off", "phase_since": h.t - 700}
        c.tick()                                 # confirmed off at the bound
        self.assertIsNone(c.st["a"]["phase"])
        self.assertIsNone(h.states["a"].maintenance_started_at,
                          "recorded as taken up on a Ready from before the "
                          "shutdown finished")
        h.states["a"] = dataclasses.replace(h.states["a"], ready=False,
                                            ready_since=None, down_since=h.t)
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])

    def test_a_fresh_request_at_the_wake_timeout_stops_the_cycle(self):
        """Checked where the operation finishes, on the read it finishes on."""
        h = self.harness({"a": crashed()}, shortfall=400.0,
                         chassis={"a": "on"})
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "waking", "phase_since": h.t}
        h.fresh["a"] = held(crashed())
        h.t += 901
        c.tick()
        self.assertEqual(h.acted["cycle"], [], "cycled a node just asked for")
        self.assertIsNone(c.st["a"]["phase"])

    def test_a_fresh_request_at_wake_completion_keeps_the_cordon(self):
        h = self.harness({"a": asleep()}, shortfall=400.0)
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "waking", "phase_since": h.t}
        h.fresh["a"] = held(node(ready=True, cordoned=True, ours=True))
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"],
                         "put a node an operator had just asked for into "
                         "service")

    def test_demand_neither_wakes_nor_counts_a_held_node(self):
        h = self.harness({"a": held(maintenance_started_at=T0 - 60),
                          "b": asleep()}, shortfall=150.0,
                         chassis={"a": "off"})
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], ["b"])

    def test_a_busy_held_node_does_not_cap_the_pool(self):
        """Counted as awake while it could not be slept, a busy held node
        filled the only slot `want` had, and its peer never woke."""
        h = self.harness({"a": held(node(ready=True),
                                    maintenance_started_at=T0 - 60),
                          "b": asleep()},
                         shortfall=90.0, busy={"a": ["job-1"]})
        c = h.controller()
        c.st["want_high_since"] = 0.0
        c.tick()
        self.assertEqual(h.acted["on"], ["b"],
                         "a held node's work kept demand from waking a peer")

    def test_held_nodes_are_never_visited_and_block_no_one_else(self):
        h = self.harness({"a": held(maintenance_started_at=T0 - 60),
                          "b": asleep(dark_for=90_000.0)},
                         chassis={"a": "off"})
        c = h.controller(maintenance_interval_s=3600,
                         maintenance_window_s=300, maintenance_stagger_s=0,
                         maintenance_timeout_s=3600)
        c.tick()
        self.assertEqual(h.acted["on"], ["b"])
        self.assertEqual(c.st["b"]["phase"], "maintaining")
        self.assertNotIn("phase", c.st["a"])

    def test_giving_it_back_clears_the_record_and_hands_it_on(self):
        h = self.harness({"a": held(node(ready=True, cordoned=True, ours=True,
                                         ours_since=T0),
                                    maintenance_started_at=T0 - 600)})
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["off"], [])
        h.states["a"] = dataclasses.replace(h.states["a"], maintenance=None)
        c.tick()
        self.assertIn(("a", "maintenance-started", None), h.acted["note"],
                      "left a record that would stop the next request "
                      "powering the node on")
        self.assertTrue(h.logged("maintenance request withdrawn"))
        # Up, ours, unwanted: the ordinary stranded repair puts it to sleep.
        self.assertEqual(c.st["a"]["phase"], "sleeping")

    def test_given_back_when_needed_it_goes_into_service(self):
        h = self.harness({"a": node(ready=True, cordoned=True, ours=True,
                                    maintenance_started_at=T0 - 600)},
                         shortfall=400.0)
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertIn(("a", False), h.acted["cordon"])

    def test_given_back_in_service_it_gets_a_whole_sleep_window(self):
        """Held, it was idle for hours without the idle clock running.
        Otherwise the tick it is given back is the tick it is slept."""
        h = self.harness({"a": held(node(ready=True),
                                    maintenance_started_at=T0 - 600)})
        c = h.controller(nodes=("a",), sleep_sustain_s=600)
        for _ in range(5):
            h.t += 3600
            c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], maintenance=None)
        c.tick()
        h.t += 300
        c.tick()
        self.assertEqual(h.acted["cordon"], [], "slept the moment it was "
                                                "given back")
        h.t += 301
        c.tick()
        self.assertEqual(h.acted["cordon"], [("a", True)])

    def test_given_back_dark_its_power_is_checked_afresh(self):
        """metalnap last knew it as OFF, from before the request. A person
        may have switched it on since, so that is not taken on trust: it gets
        the grace any dark node of ours does, and then is called what it is."""
        spy = Spy()
        h = self.harness({"a": held(maintenance_started_at=T0 - 600)},
                         chassis={"a": "on"})
        c = h.controller(nodes=("a",), notifier=spy)
        c.st["a"] = {"off": True}
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], maintenance=None)
        c.tick()
        self.assertIn("dark_on_since", c.st["a"],
                      "muted a node given back dark without checking its "
                      "power")

    def test_dry_run_touches_nothing_and_says_so_once(self):
        h = self.harness({"a": held()})
        c = h.controller(nodes=("a",), mode="dry_run")
        for _ in range(3):
            c.tick()
            h.t += 60
        self.assertEqual((h.acted["on"], h.acted["note"], h.acted["cordon"]),
                         ([], [], []))
        self.assertEqual(len(h.logged("dry_run: would take up")), 1)
        # Once per REQUEST: the next one is said again.
        h.states["a"] = dataclasses.replace(h.states["a"], maintenance=None)
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], maintenance="bios")
        c.tick()
        self.assertEqual(len(h.logged("dry_run: would take up")), 2)

    def test_a_restart_mid_request_neither_powers_on_nor_sleeps(self):
        h = self.harness({"a": held()})
        c = h.controller(nodes=("a",))
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], ready=True,
                                            ready_since=h.t, down_since=None)
        c.st = {}                                       # a restart
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])
        self.assertEqual(h.acted["cordon"], [])
        self.assertIsNone(c.st["a"].get("phase"))


class TestWakeFinishesOnAFreshRead(unittest.TestCase):
    """An operator's cordon outranks a wake already in flight -- checked on
    the read the wake finishes on, as the power cycle is."""

    def test_an_operator_cordon_at_wake_completion_is_kept(self):
        h = Harness({"a": asleep()}, shortfall=400.0)
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "waking", "phase_since": h.t}
        h.fresh["a"] = node(ready=True, cordoned=True, ours=False)
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"],
                         "uncordoned a node an operator had just cordoned")
        self.assertIsNone(c.st["a"]["phase"])


class TestKubeNodeSource(unittest.TestCase):
    class Kube:
        def __init__(self, anns):
            self.anns, self.patches = anns, []

        def request(self, method, path, body=None):
            if method == "PATCH":
                self.patches.append(body)
                return {}
            return {"metadata": {"annotations": self.anns},
                    "spec": {"unschedulable": True},
                    "status": {"conditions": [
                        {"type": "Ready", "status": "Unknown",
                         "lastTransitionTime": "2026-09-17T23:10:14Z"}]}}

    def source(self, anns):
        from metalnap.kube import KubeNodeSource
        k = self.Kube(anns)
        return k, KubeNodeSource(k, annotation="metalnap.io/cordoned")

    def test_the_power_cycle_is_read_back(self):
        _k, src = self.source({"metalnap.io/power-cycled":
                               "2026-09-18T01:17:34+00:00"})
        st = src.state("node2")
        self.assertEqual(st.power_cycled_at, 1789694254.0)
        self.assertEqual(st.down_since, 1789686614.0)

    def test_an_unparseable_record_reads_as_none(self):
        _k, src = self.source({"metalnap.io/power-cycled": "yesterday"})
        self.assertIsNone(src.state("node2").power_cycled_at)

    def test_notes_touch_metadata_only(self):
        """The node noted is as likely an uncordoned crash as one we slept;
        the cordon is not this call's to change."""
        k, src = self.source({})
        src.note("node2", "power-cycled", 1789694254.0)
        src.note("node2", "trouble", None)
        src.disown("node2")
        self.assertTrue(all(list(p) == ["metadata"] for p in k.patches))
        self.assertEqual(k.patches[0]["metadata"]["annotations"],
                         {"metalnap.io/power-cycled":
                          "2026-09-18T01:17:34+00:00"})
        self.assertEqual(k.patches[1]["metadata"]["annotations"],
                         {"metalnap.io/trouble": None})
        self.assertEqual(k.patches[2]["metadata"]["annotations"],
                         {"metalnap.io/cordoned": None})

    def test_a_maintenance_request_is_read_back(self):
        _k, src = self.source({"metalnap.io/maintenance": " kernel 6.8 ",
                               "metalnap.io/maintenance-started":
                               "2026-09-18T01:17:34Z"})
        st = src.state("node2")
        self.assertEqual((st.maintenance, st.maintenance_started_at),
                         ("kernel 6.8", 1789694254.0))
        self.assertIsNone(src.state("node2").trouble)

    def test_a_request_without_a_reason_is_still_a_request(self):
        """`kubectl annotate node x metalnap.io/maintenance=` asks, too."""
        _k, src = self.source({"metalnap.io/maintenance": ""})
        self.assertEqual(src.state("node2").maintenance, "no reason given")
        _k, src = self.source({})
        self.assertIsNone(src.state("node2").maintenance)

    def test_parse_reads_a_listed_node_as_state_reads_a_fetched_one(self):
        k, src = self.source({"metalnap.io/cordoned": "2026-09-18T01:17:34Z",
                              "metalnap.io/maintenance": "firmware"})
        self.assertEqual(src.parse(k.request("GET", "/api/v1/nodes/node2")),
                         src.state("node2"))

    def test_every_note_is_read_back(self):
        _k, src = self.source({"metalnap.io/visited": "2026-09-18T01:17:34Z",
                               "metalnap.io/shutdown": "2026-09-18T01:17:34Z",
                               "metalnap.io/trouble": "wedged"})
        st = src.state("node2")
        self.assertEqual((st.visited_at, st.shutdown_at, st.trouble),
                         (1789694254.0, 1789694254.0, "wedged"))


class TestSizing(unittest.TestCase):
    """#16: the pool is sized on the work it holds, on whichever resource
    runs out first.

    The shortfall counts only work the scheduler cannot place. Compared with
    every awake node, it read a backlog the pool had just absorbed as no
    demand at all -- and drained every busy node in it, one a tick -- while a
    full node plus a new backlog read as demand met, and woke nothing.
    """

    def cordoned(self, h, n):
        return (n, True) in h.acted["cordon"]

    def test_work_already_running_is_demand(self):
        h = Harness({"a": node(), "b": node()}, busy={"a": ["job-1"],
                                                      "b": ["job-2"]})
        c = h.controller()
        for _ in range(3):
            c.tick()
            h.t += 60
        self.assertFalse(self.cordoned(h, "a") or self.cordoned(h, "b"),
                         "drained a busy pool on an empty backlog")

    def test_a_full_node_and_a_backlog_wake_another(self):
        h = Harness({"a": node(), "b": asleep(at=1000.0)}, shortfall=72.0,
                    busy={"a": ["job-1"]})
        c = h.controller()
        c.tick()
        self.assertEqual(h.acted["on"], ["b"],
                         "a full node plus a backlog read as demand met")

    def test_only_an_idle_node_is_put_to_sleep(self):
        """reversed(wakeable) would have picked b, which is busy."""
        h = Harness({"a": node(), "b": node()}, busy={"b": ["job-1"]})
        c = h.controller()
        c.tick()
        self.assertTrue(self.cordoned(h, "a"))
        self.assertFalse(self.cordoned(h, "b"), "cordoned a busy node")

    def test_a_node_is_idle_for_a_whole_window_before_it_sleeps(self):
        """a only just finished. (Here the pool's own sleep timer starts at the
        same moment; test_the_longest_idle_node_goes_first_not_the_one_just_
        done pins the per-node window where it does not.)"""
        h = Harness({"a": node(), "b": node()},
                    busy={"a": ["job-1"], "b": ["job-2"]})
        c = h.controller(sleep_sustain_s=600)
        c.tick()
        h.t += 500
        h._busy = {"b": ["job-2"]}                # a's job ends
        c.tick()
        h.t += 200
        c.tick()
        self.assertFalse(self.cordoned(h, "a"), "slept a node just idle")
        h.t += 420
        c.tick()
        self.assertTrue(self.cordoned(h, "a"))

    def test_the_longest_idle_node_goes_first_not_the_one_just_done(self):
        """The pool has had too many nodes for a while, so the demand timer is
        long satisfied. c finished its job a moment ago -- the likeliest to be
        handed the next one -- and a has been idle all along."""
        h = Harness({n: node() for n in "abc"},
                    busy={"b": ["job-1"], "c": ["job-2"]})
        c = h.controller(nodes="abc", sleep_sustain_s=600)
        c.tick()
        h.t += 500
        h._busy = {"b": ["job-1"]}                # c's job ends
        c.tick()
        h.t += 150
        c.tick()
        self.assertFalse(self.cordoned(h, "c"), "slept a node idle for 150s")
        self.assertTrue(self.cordoned(h, "a"))

    def test_a_node_whose_work_cannot_be_read_is_in_use(self):
        h = Harness({"a": node(), "b": node()},
                    busy=RuntimeError("arc api down"))
        c = h.controller()
        c.tick()
        self.assertEqual(h.acted["cordon"], [], "slept a node it could not read")

    def test_the_pool_is_sized_on_the_resource_that_runs_out(self):
        """Memory alone says one node; CPU says two."""
        cap = {"memory": 110.0, "cpu": 40.0}
        h = Harness({n: asleep(at=1000.0, capacity=cap) for n in "abc"},
                    shortfall={"memory": 30.0, "cpu": 60.0})
        c = h.controller(nodes="abc")
        for _ in range(3):
            c.tick()
            h.t += 60
        self.assertEqual(h.acted["on"], ["a", "b"])

    def test_a_signal_and_source_that_disagree_change_nothing(self):
        """Dividing CPU by GiB is not sizing; the tick fails toward
        changing nothing."""
        h = Harness({"a": asleep(at=1000.0,
                                 capacity={"memory": 110.0, "cpu": 40.0})},
                    shortfall=300.0)
        c = h.controller(nodes=("a",))
        with self.assertRaises(TypeError):
            c.tick()
        self.assertEqual(h.acted["on"], [])

    def test_saturation_is_a_floor_not_another_node(self):
        """A capped queue's runners are on the busy nodes already. A node
        woken on top is one the cap will never let it use."""
        h = Harness({"a": node(), "b": node(), "c": node(),
                     "d": asleep(at=1000.0)}, saturated=1,
                    busy={n: ["job"] for n in "abc"})
        c = h.controller(nodes="abcd")
        for _ in range(3):
            c.tick()
            h.t += 60
        self.assertEqual(h.acted["on"], [], "woke a node a capped queue "
                                            "cannot use")

    def test_saturation_still_keeps_a_node(self):
        h = Harness({"a": node()}, saturated=1)
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["cordon"], [], "slept the last node of a "
                                                "saturated pool")

    def test_work_that_cannot_land_holds_no_idle_node(self):
        h = Harness({"a": node(), "b": node()}, shortfall=60.0, fits=False,
                    busy={"a": ["job-1"]})
        c = h.controller()
        c.tick()
        self.assertIn(("b", True), h.acted["cordon"],
                      "an idle node held awake for work it cannot run")

    def test_work_that_cannot_land_rescues_no_drain(self):
        h = Harness({"a": node(), "b": node(cordoned=True, ours=True)},
                    shortfall=60.0, fits=False, busy={"a": ["job-1"]})
        c = h.controller()
        c.st["b"] = {"phase": "sleeping", "phase_since": h.t}
        c.tick()
        self.assertNotIn(("b", False), h.acted["cordon"],
                         "pulled a drain back for work it cannot run")

    def test_a_fit_check_that_fails_holds_the_pool(self):
        h = Harness({"a": node(), "b": asleep(at=1000.0)}, shortfall=60.0,
                    busy={"a": ["job-1"]})
        h.fits_node = lambda cap: (_ for _ in ()).throw(RuntimeError("api"))
        c = h.controller()
        c.tick()
        self.assertEqual((h.acted["on"], h.acted["cordon"]), ([], []))

    def test_a_stranded_node_is_still_returned_on_unfit_demand(self):
        """The one deliberate exception, pinned by a differential test
        against the controller this replaced."""
        h = Harness({"a": node(cordoned=True, ours=True), "b": None},
                    shortfall=400.0, fits=False)
        c = h.controller()
        c.tick()
        self.assertIn(("a", False), h.acted["cordon"])

    def test_no_new_wake_the_tick_after_a_node_joins(self):
        """A scraped shortfall can still count the pods that just landed on
        it, which busy() now counts too."""
        booting = {"phase": "waking", "phase_since": 1000.0,
                   "booting": True, "power_confirmed": True}
        h = Harness({"a": node(), "b": node(cordoned=True, ours=True),
                     "c": asleep(at=1000.0),
                     "d": asleep(at=1000.0)}, shortfall=200.0,
                    busy={"a": ["job-1"]})
        h.chassis["c"] = "on"
        c = h.controller(nodes="abcd")
        c.st["b"], c.st["c"] = dict(booting), dict(booting)
        c.tick()          # b joins: 1 in use + 2 waiting = 2 awake + c coming
        self.assertEqual(h.acted["on"], [])
        h.states["b"] = node()
        h._busy = {"a": ["job-1"], "b": ["job-2"]}
        h.t += 60
        c.tick()          # b's pods are busy AND still in a stale shortfall
        self.assertEqual(h.acted["on"], [], "double-counted a joining node")
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["d"], "held the wake too long")

    def test_a_resource_no_node_reports_is_not_sized_on(self):
        h = Harness({"a": asleep(at=1000.0, capacity={"memory": 110.0}),
                     "b": asleep(at=1000.0, capacity={"memory": 110.0})},
                    shortfall={"memory": 10.0, "gpu": 5.0})
        c = h.controller()
        c.tick()
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])
        self.assertTrue(h.logged("no node reports capacity"))

    def test_saturation_is_a_node_each_whatever_the_resources(self):
        cap = {"memory": 110.0, "cpu": 40.0}
        h = Harness({"a": asleep(at=1000.0, capacity=cap)},
                    shortfall={"memory": 0.0, "cpu": 0.0}, saturated=1)
        c = h.controller(nodes=("a",))
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])

    def test_the_default_shortfall_is_read_off_the_pods(self):
        """PromQL cannot tell a sidecar from an init container for a pod that
        was never scheduled; the pod spec can."""
        from metalnap.kube import PendingPodShortfall
        runner = {"containers": [{"resources": {"requests": {
            "cpu": "2", "memory": "4Gi"}}}],
            "initContainers": [{"restartPolicy": "Always", "resources": {
                "requests": {"cpu": "2", "memory": "4Gi"}}}]}
        unsched = {"conditions": [{"type": "PodScheduled", "status": "False",
                                   "reason": "Unschedulable"}]}

        class K:
            def request(self, *a, **k):
                return {"items": [
                    {"spec": runner, "status": unsched},
                    {"spec": runner, "status": {}},         # not tried yet
                    {"spec": dict(runner, nodeName="n1"),   # already bound
                     "status": unsched}]}
        pending = PendingPodShortfall(K(), "ns")
        self.assertEqual(pending.of("memory")(), 8.0,
                         "missed the sidecar, or counted pods not failing")
        self.assertEqual(pending.of("cpu")(), 4.0)


BURST_TAINT = {"key": "ci-burst", "value": "true", "effect": "NoSchedule"}


class TestTolerates(unittest.TestCase):
    """The scheduler's rule, clause for clause: a pod that the scheduler
    would keep off a burst node is no demand for one."""

    def tolerates(self, *tolerations, taint=BURST_TAINT):
        from metalnap.kube import tolerates
        return tolerates({"spec": {"tolerations": list(tolerations)}}, taint)

    def test_no_taint_lets_everything_land(self):
        self.assertTrue(self.tolerates(taint=None))
        from metalnap.kube import tolerates
        self.assertTrue(tolerates({"spec": {}}, None))

    def test_equal_needs_the_same_value(self):
        self.assertTrue(self.tolerates(dict(BURST_TAINT, operator="Equal")))
        self.assertFalse(self.tolerates(
            {"key": "ci-burst", "operator": "Equal", "value": "false"}))
        # No operator is Equal, and no value is "", not "anything".
        self.assertTrue(self.tolerates({"key": "ci-burst", "value": "true"}))
        self.assertFalse(self.tolerates({"key": "ci-burst"}))

    def test_exists_takes_any_value_of_its_key(self):
        self.assertTrue(self.tolerates({"key": "ci-burst",
                                        "operator": "Exists"}))
        self.assertFalse(self.tolerates({"key": "gpu", "operator": "Exists"}))

    def test_an_empty_key_with_exists_tolerates_every_taint(self):
        self.assertTrue(self.tolerates({"operator": "Exists"}))

    def test_the_effect_must_match_when_named(self):
        self.assertFalse(self.tolerates({"key": "ci-burst",
                                         "operator": "Exists",
                                         "effect": "NoExecute"}))
        self.assertFalse(self.tolerates({"operator": "Exists",
                                         "effect": "NoExecute"}))
        self.assertTrue(self.tolerates({"key": "ci-burst",
                                        "operator": "Exists",
                                        "effect": "NoSchedule"}))
        self.assertTrue(self.tolerates(
            {"key": "ci-burst", "operator": "Exists", "effect": "NoExecute"},
            taint=dict(BURST_TAINT, effect="NoExecute")))

    def test_an_unknown_operator_tolerates_nothing(self):
        self.assertFalse(self.tolerates({"key": "ci-burst", "operator": "Lt",
                                         "value": "true"}))

    def test_one_matching_toleration_is_enough(self):
        self.assertTrue(self.tolerates({"key": "gpu", "operator": "Exists"},
                                       dict(BURST_TAINT, operator="Equal")))
        self.assertFalse(self.tolerates())

    def test_a_preferred_taint_turns_nothing_away(self):
        self.assertTrue(self.tolerates(
            taint=dict(BURST_TAINT, effect="PreferNoSchedule")))

    def test_an_effect_left_out_is_no_schedule(self):
        taint = {"key": "ci-burst", "value": "true"}
        self.assertFalse(self.tolerates(taint=taint))
        self.assertTrue(self.tolerates(dict(BURST_TAINT, operator="Equal"),
                                       taint=taint))

    def test_a_bare_key_still_matches_on_the_key_alone(self):
        """The older wiring, PendingPodFit(toleration_key=...), keeps its
        meaning."""
        self.assertTrue(self.tolerates({"key": "ci-burst"}, taint="ci-burst"))
        self.assertTrue(self.tolerates({"key": "ci-burst", "value": "no"},
                                       taint="ci-burst"))
        self.assertTrue(self.tolerates({"operator": "Exists"},
                                       taint="ci-burst"))
        self.assertFalse(self.tolerates({"key": "gpu"}, taint="ci-burst"))

    def test_the_fit_check_uses_the_whole_taint(self):
        from metalnap.kube import PendingPodFit
        wrong = {"spec": {"tolerations": [{"key": "ci-burst",
                                           "value": "false"}],
                          "containers": [{"resources": {"requests": {
                              "memory": "1Gi"}}}]}}

        class K:
            def request(self, *a, **k):
                return {"items": [wrong]}
        self.assertFalse(PendingPodFit(K(), "ns", taint=BURST_TAINT)(100.0),
                         "a pod tolerating ci-burst=false fits a "
                         "ci-burst=true node?")
        self.assertTrue(PendingPodFit(K(), "ns",
                                      toleration_key="ci-burst")(100.0),
                        "the key-only form changed its meaning")


class TestPerResourceSeams(unittest.TestCase):
    def pod(self, cpu, mem, sidecar_cpu="0", init_cpu="1"):
        return {"spec": {
            "tolerations": [dict(BURST_TAINT, operator="Equal")],
            "containers": [{"resources": {"requests": {"cpu": cpu,
                                                       "memory": mem}}}],
            "initContainers": [
                {"restartPolicy": "Always", "resources": {"requests": {
                    "cpu": sidecar_cpu, "memory": "0"}}},
                {"resources": {"requests": {"cpu": init_cpu,
                                            "memory": "0"}}}]}}

    def fit(self, pods, capacity):
        from metalnap.kube import PendingPodFit

        class K:
            def request(self, *a, **k):
                return {"items": pods}
        return PendingPodFit(K(), "ns", taint=BURST_TAINT)(capacity)

    def test_a_pod_fits_only_if_every_resource_does(self):
        cap = {"memory": 110.0, "cpu": 40.0}
        self.assertTrue(self.fit([self.pod("4", "8Gi")], cap))
        self.assertFalse(self.fit([self.pod("48", "8Gi")], cap),
                         "a pod needing 48 cores fits a 40-core node?")

    def test_a_sidecar_counts_toward_the_steady_state(self):
        cap = {"memory": 110.0, "cpu": 40.0}
        self.assertFalse(self.fit([self.pod("30", "8Gi", "12")], cap))
        self.assertTrue(self.fit([self.pod("30", "8Gi", "4")], cap))

    def test_an_init_container_is_a_floor_not_a_sum(self):
        """It runs before the app containers, beside the sidecars started
        ahead of it: the pod needs the larger of the two phases."""
        cap = {"memory": 110.0, "cpu": 40.0}
        self.assertFalse(self.fit([self.pod("4", "8Gi", "4", "64")], cap),
                         "a 64-core init phase fits a 40-core node?")
        self.assertTrue(self.fit([self.pod("30", "8Gi", "4", "20")], cap),
                        "an init container was summed with the app")

    def test_the_effective_request_is_the_schedulers(self):
        from metalnap.kube import effective_requests

        def c(cpu, always=False):
            d = {"resources": {"requests": {"cpu": cpu}}}
            if always:
                d["restartPolicy"] = "Always"
            return d
        spec = {"containers": [c("2")],
                "initContainers": [c("5"), c("1", True), c("3"), c("2", True)],
                "overhead": {"cpu": "250m"}}
        # init phase: 5; 1 (sidecar); 1+3; sidecars 3. steady: 2 + 3 = 5.
        self.assertEqual(effective_requests(spec, ("cpu",)), {"cpu": 5.25})
        spec["initContainers"][2] = c("6")         # 1 + 6 beats the steady 5
        self.assertEqual(effective_requests(spec, ("cpu",)), {"cpu": 7.25})

    def test_a_bare_number_is_memory_as_before(self):
        self.assertTrue(self.fit([self.pod("400", "8Gi")], 110.0))

    def test_allocatable_reads_each_resource(self):
        from metalnap.kube import allocatable
        got = allocatable()({"status": {"allocatable": {
            "cpu": "39500m", "memory": "126976Mi"}}})
        self.assertEqual(got, {"memory": 124.0, "cpu": 39.5})

    def test_every_kind_of_quantity_is_read(self):
        """"1G" once raised, and on the default demand path that failed the
        whole tick."""
        from metalnap.kube import mem_to_gib, cpu_to_cores
        self.assertEqual(mem_to_gib("1Gi"), 1.0)
        self.assertEqual(mem_to_gib("1G"), 1e9 / 2 ** 30)
        self.assertEqual(mem_to_gib("1073741824"), 1.0)
        self.assertEqual(mem_to_gib("1.073741824e9"), 1.0)
        self.assertEqual(mem_to_gib("512Mi"), 0.5)
        self.assertEqual(mem_to_gib("1Ti"), 1024.0)
        self.assertEqual(cpu_to_cores("500m"), 0.5)
        self.assertEqual(cpu_to_cores("2"), 2.0)
        self.assertEqual(cpu_to_cores(3), 3.0)
        self.assertEqual(cpu_to_cores("1e3m"), 1.0)
        with self.assertRaises(ValueError):
            mem_to_gib("lots")

    def test_demand_that_cannot_land_here_is_not_counted(self):
        """Work that does not tolerate the burst taint never runs on a burst
        node; counted, it wakes one for nothing."""
        from metalnap.kube import PendingPodShortfall
        unsched = {"conditions": [{"type": "PodScheduled", "status": "False",
                                   "reason": "Unschedulable"}]}

        def pod(tolerations):
            return {"spec": {"tolerations": tolerations, "containers": [
                {"resources": {"requests": {"memory": "4Gi"}}}]},
                "status": unsched}

        class K:
            def request(self, *a, **k):
                return {"items": [
                    pod([dict(BURST_TAINT, operator="Equal")]), pod([]),
                    pod([{"operator": "Exists"}]),
                    # The right key, the wrong value: it cannot land either.
                    pod([{"key": "ci-burst", "value": "false"}])]}
        self.assertEqual(
            PendingPodShortfall(K(), "ns", BURST_TAINT).of("memory")(), 8.0)
        self.assertEqual(PendingPodShortfall(K(), "ns").of("memory")(), 16.0)

    def test_the_reference_wiring_builds(self):
        """The whole of main(), short of the loop: a name used before it is
        defined there only ever showed up at the first start."""
        import os
        from metalnap import __main__ as entry
        env = {"NODES": "a,b", "BMC_HOST_FMT": "{node}-bmc.", "BMC_USER": "u",
               "BMC_PASS": "p", "PROM_URL": "http://prom",
               "ALERTMANAGER_URL": "http://am", "MODE": "dry_run"}

        def controller(**extra):
            built = {}
            saved = dict(os.environ)
            real = entry.Controller.run_forever
            entry.Controller.run_forever = lambda c: built.update(c=c)
            try:
                os.environ.update(env, **extra)
                self.assertEqual(entry.main([]), 0)
            finally:
                entry.Controller.run_forever = real
                os.environ.clear()
                os.environ.update(saved)
            return built["c"]

        def build(**extra):
            return controller(**extra).signal.shortfall_query

        live = "PendingPodShortfall.of.<locals>.shortfall"
        q = build()
        self.assertEqual(set(q), {"memory", "cpu"})
        self.assertEqual(q["cpu"].__qualname__, live)
        self.assertEqual(set(build(CPU_SHORTFALL_QUERY="")), {"memory"},
                         '"" did not size on memory alone')
        q = build(CPU_SHORTFALL_QUERY="sum(cpu)", SHORTFALL_QUERY="sum(mem)")
        self.assertEqual(q, {"memory": "sum(mem)", "cpu": "sum(cpu)"})

        def taints(c):
            pending = next(cell.cell_contents for cell
                           in c.signal.shortfall_query["memory"].__closure__
                           if hasattr(cell.cell_contents, "taint"))
            return (pending.taint, c.signal.fit_check.taint,
                    c.warmup.tolerations)
        self.assertEqual(taints(controller(WARMUP_IMAGE="img")),
                         (BURST_TAINT, BURST_TAINT,
                          [dict(BURST_TAINT, operator="Equal")]),
                         "the warmup, the shortfall and the fit check do not "
                         "agree on the taint")
        mine = {"key": "burst", "value": "yes", "effect": "NoExecute"}
        self.assertEqual(taints(controller(
            WARMUP_IMAGE="img", BURST_TAINT_KEY="burst",
            BURST_TAINT_VALUE="yes", BURST_TAINT_EFFECT="NoExecute"))[:2],
            (mine, mine))
        self.assertEqual(taints(controller(WARMUP_IMAGE="img",
                                           BURST_TAINT_KEY="")),
                         (None, None, []), "an empty key is no taint")
        with self.assertRaises(SystemExit):
            controller(BURST_TAINT_EFFECT="NoScheduel")

    def test_a_source_may_be_a_callable(self):
        from metalnap.signal import prometheus
        sig = prometheus.PrometheusSignal("http://p", {"cpu": lambda: 3})
        self.assertEqual(sig.shortfall(), {"cpu": 3.0})

    def test_a_signal_with_a_query_per_resource_returns_each(self):
        from metalnap.signal import prometheus
        answers = {"qm": "30", "qc": "60"}

        class R:
            def __init__(self, q):
                self.q = q

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"result": [{"value": [0, answers[self.q]]}]}}
        real = prometheus.requests.get
        prometheus.requests.get = lambda url, params, timeout: R(
            params["query"])
        try:
            sig = prometheus.PrometheusSignal(
                "http://p", {"memory": "qm", "cpu": "qc"})
            self.assertEqual(sig.shortfall(), {"memory": 30.0, "cpu": 60.0})
        finally:
            prometheus.requests.get = real

# ---------------------------------------------------------------------------
# Capacity ceiling: an external signal that caps how many managed nodes may be
# awake, down to none (#27).
#
# README rule it touches: "Never interrupt running work". A forced shed after
# CEILING_DRAIN_DEADLINE_S is the first deliberate relaxation of it, so most of
# what follows is about what bounds that: nothing is interrupted before the
# deadline, the deadline is durable, and it is enforced only while the CURRENT
# reading still demands it.
# ---------------------------------------------------------------------------

class _Resp:
    """Just enough of a requests.Response for Prometheus's query API."""

    def __init__(self, body, code=200):
        self.body, self.code = body, code

    def raise_for_status(self):
        if self.code >= 400:
            raise RuntimeError("HTTP %d" % self.code)

    def json(self):
        return self.body


def prom(result, result_type="vector", status="success"):
    return _Resp({"status": status,
                  "data": {"resultType": result_type, "result": result}})


def vec(*values):
    """An instant vector with one series per value, as Prometheus returns it:
    the sample's value is a STRING."""
    return [{"metric": {"ups": str(i)}, "value": [1.7e9, str(v)]}
            for i, v in enumerate(values)]


class TestPrometheusCeiling(unittest.TestCase):
    """Three outcomes -- a value, none, and unavailable -- and the two that are
    not a value must never be confused with 0, which sheds every node."""

    def read(self, response):
        with mock.patch("metalnap.ceiling.prometheus.requests.get",
                        return_value=response):
            return PrometheusCeiling("http://prom:9090/", "q").limit()

    def test_no_series_is_no_ceiling_and_not_zero(self):
        """THE trap. PrometheusSignal._scalar() reads an empty result as 0.0,
        which is right for demand -- nothing waiting -- and, for a ceiling, an
        order to power every node off. A gate like `vector(0) and on() (...)`
        yields no series on every ordinary day."""
        self.assertIsNone(self.read(prom([])))

    def test_zero_is_a_ceiling_of_zero(self):
        got = self.read(prom(vec(0)))
        self.assertEqual(got, 0)
        self.assertIsNotNone(got)

    def test_a_fraction_floors(self):
        self.assertEqual(self.read(prom(vec(2.9))), 2)
        self.assertEqual(self.read(prom(vec(0.99))), 0)

    def test_several_series_take_the_minimum_whatever_their_order(self):
        """res[0] has no defined order. Most restrictive wins, so an
        unaggregated per-UPS expression just works."""
        self.assertEqual(self.read(prom(vec(3, 1, 2))), 1)
        self.assertEqual(self.read(prom(vec(1, 3, 2))), 1)
        self.assertEqual(self.read(prom(vec(2, 3, 1))), 1)

    def test_a_scalar_result_is_read_too(self):
        """`query: "2"` is a scalar, not a vector."""
        self.assertEqual(self.read(prom([1.7e9, "2"], "scalar")), 2)

    def test_a_value_that_is_not_a_node_count_is_unavailable(self):
        """Never 0, never a clamp: a reading that cannot be trusted must not
        power anything off."""
        for bad in ("NaN", "+Inf", "-Inf", "Inf", "-1", "-0.5", "banana", ""):
            with self.subTest(value=bad):
                with self.assertRaises(Exception):
                    self.read(prom(vec(bad)))

    def test_one_bad_series_makes_the_whole_reading_unavailable(self):
        """Better a loud fail-open than a minimum taken over what could be
        read: a half-readable signal is not one to act on."""
        with self.assertRaises(Exception):
            self.read(prom(vec(0, "NaN")))

    def test_an_error_is_unavailable(self):
        for resp in (_Resp({}, code=500),
                     prom([], status="error"),
                     prom([], result_type="matrix"),
                     prom([], result_type="string"),
                     _Resp({"status": "success"}),
                     _Resp(None)):
            with self.subTest(resp=resp.body):
                with self.assertRaises(Exception):
                    self.read(resp)

    def test_a_connection_error_propagates(self):
        with mock.patch("metalnap.ceiling.prometheus.requests.get",
                        side_effect=OSError("unreachable")):
            with self.assertRaises(OSError):
                PrometheusCeiling("http://prom", "q").limit()

    def test_the_query_goes_to_the_query_api(self):
        with mock.patch("metalnap.ceiling.prometheus.requests.get",
                        return_value=prom([])) as get:
            PrometheusCeiling("http://prom:9090/", "up == 0",
                              timeout=7).limit()
        get.assert_called_once_with("http://prom:9090/api/v1/query",
                                    params={"query": "up == 0"}, timeout=7)


class TestStaticCeiling(unittest.TestCase):
    def test_zero_is_a_ceiling(self):
        self.assertEqual(StaticCeiling(0).limit(), 0)
        self.assertEqual(StaticCeiling(3).limit(), 3)

    def test_anything_that_is_not_a_node_count_is_refused(self):
        for bad in (-1, True, 1.5, None, "2"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    StaticCeiling(bad)


class TestCeilingConfig(unittest.TestCase):
    def build(self, **env):
        saved = dict(os.environ)
        for k in [k for k in os.environ if k.startswith("CEILING_")]:
            del os.environ[k]
        os.environ.update(env)
        try:
            return Config()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_off_by_default_with_the_documented_timers(self):
        cfg = self.build()
        self.assertEqual((cfg.ceiling_query, cfg.ceiling_static), ("", None))
        self.assertEqual((cfg.ceiling_release_hold_s,
                          cfg.ceiling_drain_deadline_s), (900, 600))

    def test_static_zero_is_a_value_and_empty_is_not(self):
        """`static: 0` is a ceiling of zero nodes. An env that tested
        truthiness would read it as unset -- no ceiling, in the one case it
        matters most."""
        self.assertEqual(self.build(CEILING_STATIC="0").ceiling_static, 0)
        self.assertIsNone(self.build(CEILING_STATIC="").ceiling_static)
        self.assertEqual(self.build(CEILING_STATIC=" 2 ").ceiling_static, 2)

    def test_the_environment_is_parsed(self):
        cfg = self.build(CEILING_QUERY=" up == 0 ", CEILING_RELEASE_HOLD_S="30",
                         CEILING_DRAIN_DEADLINE_S="0")
        self.assertEqual((cfg.ceiling_query, cfg.ceiling_release_hold_s,
                          cfg.ceiling_drain_deadline_s), ("up == 0", 30, 0))

    def test_a_query_and_a_static_value_together_are_refused(self):
        with self.assertRaises(ValueError):
            Config(mode="on", ceiling_query="up", ceiling_static=1).validate()
        with self.assertRaises(ValueError):
            # 0 is a value, so it conflicts too.
            Config(mode="on", ceiling_query="up", ceiling_static=0).validate()

    def test_negative_values_are_refused(self):
        for kw in (dict(ceiling_static=-1), dict(ceiling_release_hold_s=-1),
                   dict(ceiling_drain_deadline_s=-1)):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                Config(mode="on", **kw).validate()

    def test_zero_is_allowed_for_both_durations(self):
        Config(mode="on", ceiling_release_hold_s=0,
               ceiling_drain_deadline_s=0).validate()

    def test_a_deadline_of_zero_is_warned_about_at_start(self):
        warned = Config(mode="on", ceiling_drain_deadline_s=0).warnings()
        self.assertTrue(any("CEILING_DRAIN_DEADLINE_S" in w for w in warned),
                        warned)
        self.assertFalse(any("CEILING" in w for w in Config(mode="on")
                             .warnings()), "warned about the default")


class TestShedNote(unittest.TestCase):
    """`metalnap.io/shed` is written faithfully and has to be read back, or the
    drain deadline lives in memory and a restart resets it."""

    def source(self, anns):
        from metalnap.kube import KubeNodeSource
        k = TestKubeNodeSource.Kube(anns)
        return k, KubeNodeSource(k, annotation="metalnap.io/cordoned")

    def test_it_is_read_back(self):
        _k, src = self.source({"metalnap.io/shed": "2026-09-18T01:17:34Z"})
        self.assertEqual(src.state("node2").shed_at, 1789694254.0)

    def test_absent_or_unparseable_reads_as_none(self):
        _k, src = self.source({})
        self.assertIsNone(src.state("node2").shed_at)
        _k, src = self.source({"metalnap.io/shed": "soon"})
        self.assertIsNone(src.state("node2").shed_at)

    def test_it_is_written_as_metadata_only(self):
        k, src = self.source({})
        src.note("node2", "shed", 1789694254.0)
        src.note("node2", "shed", None)
        self.assertEqual(k.patches[0], {"metadata": {"annotations": {
            "metalnap.io/shed": "2026-09-18T01:17:34+00:00"}}})
        self.assertEqual(k.patches[1], {"metadata": {"annotations": {
            "metalnap.io/shed": None}}})


class _Sink:
    """What the controller publishes each tick, kept."""

    def __init__(self, fail=False):
        self.reports, self.fail = [], fail

    def publish(self, report):
        if self.fail:
            raise RuntimeError("the apiserver said no")
        self.reports.append(report)

    @property
    def last(self):
        return self.reports[-1]


class World(Harness):
    """A Harness whose cluster answers back, for the tests that span ticks.

    A cordon changes the node, a soft shutdown takes it dark and a power-on
    brings it up: the three effects a shed is made of, which the plain Harness
    leaves each test to write by hand.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        #: Nodes that ignore a soft shutdown and stay up.
        self.ignores_off = set()

    def set_cordon(self, n, v):
        super().set_cordon(n, v)
        self.sequence.append(("cordon" if v else "uncordon", n))
        self.states[n] = dataclasses.replace(
            self.states[n], cordoned=v, ours=v,
            ours_since=self.t if v else None)

    @property
    def power(self):
        return _WorldPower(self)


class _WorldPower(_Power):
    def on(self, n):
        super().on(n)
        self.h.states[n] = dataclasses.replace(
            self.h.states[n], ready=True, ready_since=self.h.t,
            down_since=None)

    def soft_off(self, n):
        super().soft_off(n)
        self.h.sequence.append(("off", n))
        if n in self.h.ignores_off:
            return
        self.h.chassis[n] = "off"
        self.h.states[n] = dataclasses.replace(
            self.h.states[n], ready=False, ready_since=None,
            down_since=self.h.t)


class OrderedSpy(Spy):
    """A notifier that writes into the world's sequence, so a test can say
    which came first: the announcement or the power-off."""

    def __init__(self, h, fail=False):
        super().__init__(fail=fail)
        self.h = h

    def going_down(self, n):
        super().going_down(n)
        self.h.sequence.append(("down", n))


class WarmSpy:
    def __init__(self):
        self.started, self.cleaned = [], []

    def start(self, n):
        self.started.append(n)

    def done(self, n):
        return True

    def cleanup(self, n):
        self.cleaned.append(n)


def run(h, c, ticks, dt=60.0):
    """`ticks` reconciles, `dt` apart; the clock moves after each."""
    for _ in range(ticks):
        c.tick()
        h.t += dt


def cordoned(h):
    return [n for n, v in h.acted["cordon"] if v]


def shed_notes(h):
    return [(n, v) for n, k, v in h.acted["note"] if k == "shed"]


def world(states, **kw):
    h = World(states, **kw)
    h.t = T0
    return h


#: The Harness sleeps an idle node at once -- no sustain window -- which would
#: cordon the very nodes a "the ceiling did nothing" test is watching. Where
#: the ceiling is meant to be the only thing that could, this switches the
#: ordinary sleep off.
ONLY_THE_CEILING = dict(sleep_sustain_s=10 ** 9)


class TestCeilingFailsOpen(unittest.TestCase):
    """An unreachable Prometheus never sheds a node; one that goes away in the
    middle of an event lifts the ceiling at once."""

    def test_a_raising_ceiling_sheds_nothing_and_says_so_once(self):
        h = world({"a": node(), "b": node()},
                  ceiling=RuntimeError("prometheus down"))
        c = h.controller(**ONLY_THE_CEILING)
        run(h, c, 3)
        self.assertEqual((h.acted["cordon"], h.acted["off"]), ([], []))
        self.assertEqual(len(h.at_level("warn", "unavailable")), 1,
                         "the outage was logged on every tick, or not at all")

    def test_no_series_sheds_nothing(self):
        h = world({"a": node(), "b": node()}, ceiling=None)
        run(h, h.controller(**ONLY_THE_CEILING), 3)
        self.assertEqual(h.acted["cordon"], [])

    def test_an_error_releases_an_engaged_ceiling_on_the_same_tick(self):
        h = world({"a": node(), "b": node()}, ceiling=0)
        c = h.controller()
        c.tick()
        self.assertEqual(sorted(cordoned(h)), ["a", "b"])
        h._ceiling = RuntimeError("prometheus down")
        h.t += 60
        c.tick()
        self.assertEqual(sorted(n for n, v in shed_notes(h) if v is None),
                         ["a", "b"], "the ceiling outlived its signal")

    def test_an_error_lets_demand_wake_a_node_straight_away(self):
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=0)
        c = h.controller()
        c.tick()
        self.assertEqual(h.acted["on"], [])
        h._ceiling = RuntimeError("prometheus down")
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])

    def test_an_error_discards_the_hold_it_is_not_averaged_in(self):
        """engaged, error, engaged-or-none must not leave the hold standing:
        an unavailable reading is not a looser reading, it is no reading."""
        # Busy, so the drains are still running when demand turns up and the
        # mid-sleep rescue is the thing that shows whether a hold stands.
        h = world({"a": node(), "b": node()}, ceiling=0,
                  busy={"a": ["j1"], "b": ["j2"]})
        c = h.controller()
        c.tick()                                  # engaged: both shed
        h._ceiling = RuntimeError("prometheus down")
        h.t += 60
        c.tick()                                  # released
        h._ceiling, h._shortfall = None, 400.0
        h.t += 60
        c.tick()                                  # no hold left to stand
        self.assertIn(("a", False), h.acted["cordon"],
                      "demand was still refused by a hold the error had "
                      "already discarded")

    def test_a_demand_signal_that_raises_does_not_stop_a_shed(self):
        """The default shortfall is read off the Kubernetes API, not
        Prometheus, so the two fail independently -- and a ceiling that waited
        for demand to read would stop shedding whenever the demand path is
        failing."""
        h = world({"a": node(), "b": node()}, ceiling=0,
                  shortfall=RuntimeError("scrape failed"))
        c = h.controller()
        for _ in range(4):
            c.tick()                                # must not raise
            h.t += 60
        self.assertEqual(sorted(cordoned(h)), ["a", "b"])
        self.assertEqual(sorted(h.acted["off"]), ["a", "b"],
                         "the shed began and was never carried through")

    def test_without_a_ceiling_a_raising_demand_signal_still_fails_the_tick(self):
        """Unchanged: raising is treated as "do not act", which is safe."""
        h = world({"a": node(), "b": node()}, ceiling=None,
                  shortfall=RuntimeError("scrape failed"))
        with self.assertRaises(RuntimeError):
            h.controller().tick()

    def test_the_ceiling_is_read_before_the_demand_signal(self):
        h = world({"a": node(), "b": node()}, ceiling=None)
        seen = []
        h.shortfall = lambda: seen.append(h.ceiling_reads) or 0.0
        h.controller().tick()
        self.assertEqual(seen, [1], "the demand signal was read first")

    def test_mode_off_reads_nothing(self):
        h = world({"a": node(), "b": node()}, ceiling=0)
        c = h.controller(mode="off")
        run(h, c, 2)
        self.assertEqual(h.ceiling_reads, 0)
        self.assertEqual(h.acted["cordon"], [])

    def test_a_reading_is_clamped_to_the_nodes_there_are(self):
        h = world({"a": node(), "b": node()}, ceiling=99)
        sink = _Sink()
        h.controller(metrics=sink, **ONLY_THE_CEILING).tick()
        self.assertEqual((sink.last["limit"], sink.last["engaged"]),
                         (2, False))
        self.assertEqual(h.acted["cordon"], [])


class TestCeilingBlocksWakes(unittest.TestCase):
    """While a ceiling is in force there are no wakes beyond it, even with
    demand -- and the refused demand stays visible."""

    def test_a_ceiling_of_zero_wakes_nothing_whatever_the_demand(self):
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=0)
        run(h, h.controller(), 3)
        self.assertEqual(h.acted["on"], [], "woke a node past a ceiling of 0")

    def test_refused_demand_stays_visible_in_the_log(self):
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=0)
        h.controller().tick()
        seen = h.logged("observed")[-1]
        self.assertEqual((seen["ceiling"], seen["want"], seen["wanted"]),
                         (0, 0, 2))

    def test_a_full_ceiling_wakes_nothing_more(self):
        h = world({"a": node(), "b": asleep()}, shortfall=400.0, ceiling=1,
                  busy={"a": ["j1"]})
        run(h, h.controller(), 3)
        self.assertEqual(h.acted["on"], [])

    def test_demand_under_the_ceiling_is_still_met_up_to_it(self):
        """A ceiling that only ever refused would pass the tests above too."""
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=1)
        run(h, h.controller(), 5)
        self.assertEqual(h.acted["on"], ["a"], "one node of two was allowed")
        self.assertIn(("a", False), h.acted["cordon"],
                      "the node the ceiling allowed never entered service")

    def test_the_fit_guard_cannot_hold_the_pool_above_the_ceiling(self):
        """The guard assigns `want = min(wakeable, in_use)` when the waiting
        work cannot run here, and that can exceed the ceiling. The clamp has
        to come AFTER it. dry_run, so nothing is shed and the pool stays
        over the ceiling for the clamp alone to answer for."""
        h = world({"a": node(), "b": node()}, shortfall=60.0, fits=False,
                  busy={"a": ["j1"], "b": ["j2"]}, ceiling=1)
        h.controller(mode="dry_run").tick()
        self.assertEqual(h.logged("observed")[-1]["want"], 1,
                         "the fit guard overrode the ceiling")

    def test_a_fit_check_that_fails_cannot_hold_the_pool_above_it_either(self):
        h = world({"a": node(), "b": node()}, shortfall=60.0, ceiling=1,
                  fits=RuntimeError("apiserver"))
        h.controller(mode="dry_run").tick()
        self.assertEqual(h.logged("observed")[-1]["want"], 1,
                         "'holding the pool as it is' held it over the ceiling")

    def test_saturation_is_clamped_like_the_rest(self):
        h = world({"a": asleep(), "b": asleep()}, saturated=2, ceiling=0)
        run(h, h.controller(), 3)
        self.assertEqual(h.acted["on"], [])

    def test_no_mid_sleep_rescue_beyond_the_ceiling(self):
        for ceiling, rescued in ((None, True), (0, False)):
            with self.subTest(ceiling=ceiling):
                h = world({"a": node(cordoned=True, ours=True,
                                     ours_since=T0 - 10), "b": None},
                          shortfall=400.0, ceiling=ceiling,
                          busy={"a": ["j1"]})
                c = h.controller()
                c.st["a"] = {"phase": "sleeping", "phase_since": T0 - 10}
                c.tick()
                self.assertEqual(("a", False) in h.acted["cordon"], rescued)

    def test_no_maintenance_window_takeover_beyond_the_ceiling(self):
        for ceiling, taken in ((None, True), (0, False)):
            with self.subTest(ceiling=ceiling):
                h = world({"a": node(cordoned=True, ours=True,
                                     ours_since=T0 - 10), "b": None},
                          shortfall=400.0, ceiling=ceiling)
                c = h.controller(**TestScheduledMaintenance.MAINT)
                c.st["a"] = {"phase": "maintaining", "phase_since": T0 - 10,
                             "maintenance_until": T0 + 300}
                c.tick()
                self.assertEqual(("a", False) in h.acted["cordon"], taken)

    def test_the_stranded_repair_completes_the_sleep_not_the_wake(self):
        """A Ready node under our cordon and in no operation is returned to
        service when it is needed -- and not over a ceiling."""
        h = world({"a": node(cordoned=True, ours=True, ours_since=T0 - 1000),
                   "b": None}, shortfall=400.0, ceiling=0)
        c = h.controller()
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"],
                         "returned a stranded node to service over the ceiling")
        self.assertEqual(c.st["a"]["phase"], "sleeping")
        self.assertEqual(shed_notes(h), [("a", T0)])

    def test_a_shadow_does_not_say_it_would_return_a_stranded_node_over_the_ceiling(self):
        """The stranded repair reads `unguarded`, which the clamp has to reach
        as well as `want`. Live, a shed takes the stranded node first and the
        question never arises; a shadow does not shed, and it must still tell
        the truth about what it would do."""
        h = world({"a": node(cordoned=True, ours=True, ours_since=T0 - 1000),
                   "b": None}, shortfall=400.0, ceiling=0)
        h.controller(mode="dry_run").tick()
        self.assertFalse(h.logged("stranded node needed"),
                         "would have returned a node to service over the "
                         "ceiling")
        self.assertTrue(h.logged("stranded node not needed"))

    def test_nothing_that_depends_on_demand_is_decided_while_it_cannot_be_read(self):
        """A failing demand signal lets the shed be seen through -- and no more.
        The ordinary sleep of an idle node is a decision about demand, and
        demand is unknown: only what the ceiling requires goes down."""
        h = world({n: node() for n in ("a", "b", "c")}, ceiling=2,
                  shortfall=RuntimeError("scrape failed"))
        c = h.controller(nodes=("a", "b", "c"))
        for _ in range(3):
            c.tick()
            h.t += 60
        self.assertEqual(cordoned(h), ["c"],
                         "slept nodes on a guess, with demand unreadable")

    def test_wakes_are_gated_on_nodes_still_powered_including_draining(self):
        """A shed node is cordoned, so it is not awake -- but it is drawing
        power until it is off. Counting only awake nodes lets a budget ceiling
        be exceeded for the length of a drain."""
        h = Harness({"a": node(cordoned=True, ours=True, ours_since=T0 - 100,
                               shed_at=T0 - 100),
                     "b": asleep()}, shortfall=400.0, ceiling=1,
                    chassis={"a": "on"})
        h.t = T0
        c = h.controller()
        c.st["a"] = {"phase": "powering_off", "phase_since": T0 - 10}
        c.tick()
        self.assertEqual(h.acted["on"], [],
                         "woke a second node while the first was still on")
        h.states["a"] = dataclasses.replace(h.states["a"], ready=False)
        h.chassis["a"] = "off"
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["on"], ["b"],
                         "kept refusing after the shed node was off")

    def test_a_cold_boot_in_flight_counts_toward_the_gate(self):
        h = Harness({"a": node(), "b": asleep(), "c": asleep()},
                    shortfall=1000.0, ceiling=2, busy={"a": ["j1"]})
        h.t = T0
        run(h, h.controller(nodes=("a", "b", "c")), 4)
        self.assertEqual(h.acted["on"], ["b"],
                         "a second node was woken beside one already coming")


class TestCeilingShedsAtOnce(unittest.TestCase):
    """Excess nodes are put down through the ordinary sleep, but without
    waiting out the timers that exist to hold evidence of DEMAND."""

    NODES = ("a", "b", "c")

    def three(self, **kw):
        states = {n: node(ready_since=T0 - 10) for n in self.NODES}
        return world(states, **kw)

    def controller(self, h, **kw):
        """The ceiling is meant to be the only thing that sleeps a node here:
        the Harness would otherwise put an idle one down at once."""
        return h.controller(nodes=self.NODES, **dict(ONLY_THE_CEILING, **kw))

    def test_every_excess_node_is_cordoned_on_the_first_tick_in_reverse(self):
        h = self.three(ceiling=1)
        # The timers a shed must not wait out: all of them unmet.
        c = self.controller(h, sleep_sustain_s=1200, min_uptime_s=2700)
        c.tick()
        self.assertEqual(cordoned(h), ["c", "b"],
                         "not all at once, or not the end of the list first")
        self.assertEqual(h.acted["off"], [], "a tick cordons; it does not "
                                             "cut power in the same step")

    def test_none_beyond_the_excess(self):
        h = self.three(ceiling=2)
        self.controller(h).tick()
        self.assertEqual(cordoned(h), ["c"])

    def test_idle_nodes_go_before_busy_ones_even_when_the_busy_one_is_later(self):
        h = self.three(ceiling=1, busy={"c": ["job"]})
        self.controller(h).tick()
        self.assertEqual(cordoned(h), ["b", "a"],
                         "a node with a job on it went before idle ones")

    def test_a_busy_node_is_shed_when_nothing_else_can_be(self):
        h = self.three(ceiling=1, busy={"b": ["j1"], "c": ["j2"]})
        self.controller(h).tick()
        self.assertEqual(cordoned(h), ["a", "c"],
                         "idle first, then busy -- each in reverse order")

    def test_a_cooldown_from_an_abandoned_sleep_does_not_protect_a_node(self):
        h = self.three(ceiling=0)
        c = self.controller(h)
        c.st["b"] = {"cooldown_until": T0 + 99_999}
        c.tick()
        self.assertIn("b", cordoned(h))

    def test_the_shed_is_recorded_on_the_node_with_the_time_it_began(self):
        h = self.three(ceiling=1)
        self.controller(h).tick()
        self.assertEqual(sorted(shed_notes(h)), [("b", T0), ("c", T0)])

    def test_it_finishes_the_ordinary_way_announced_before_it_is_off(self):
        h = self.three(ceiling=2)
        spy = OrderedSpy(h)
        c = self.controller(h, notifier=spy)
        run(h, c, 4)
        self.assertEqual(h.acted["off"], ["c"])
        seq = [e for e in h.sequence if e[1] == "c"]
        self.assertLess(seq.index(("down", "c")), seq.index(("off", "c")),
                        "powered off before announcing")

    def test_a_shed_node_is_muted_like_a_sleep_and_never_trouble(self):
        h = self.three(ceiling=2, shortfall=0.0)
        spy = OrderedSpy(h)
        c = self.controller(h, notifier=spy)
        run(h, c, 12)
        self.assertIn("c", spy.down, "a shed node was not muted")
        self.assertEqual(spy.alerts, {}, "a shed node raised an alert")
        self.assertIsNone(h.states["c"].trouble)
        self.assertEqual(h.acted["cycle"], [], "a shed node was power-cycled")

    def test_a_shed_node_stays_down_through_demand_until_the_ceiling_goes(self):
        h = self.three(ceiling=2)
        c = self.controller(h)
        run(h, c, 5)
        self.assertEqual(h.acted["off"], ["c"])
        h._shortfall = 1000.0
        run(h, c, 15)
        self.assertEqual(h.acted["on"], [], "woke past the ceiling")
        self.assertEqual(h.acted["cycle"], [])


class TestCeilingBusyNodes(unittest.TestCase):
    """Busy nodes get a graceful drain with a deadline -- the one place this
    controller deliberately relaxes "never interrupt running work"."""

    def one(self, **kw):
        kw.setdefault("busy", {"a": ["job-1"]})
        kw.setdefault("ceiling", 0)
        return world({"a": node(ready_since=T0 - 10)}, **kw)

    def drive(self, h, c, until):
        """Tick once a minute until `until` seconds after the first tick."""
        while h.t - T0 <= until:
            c.tick()
            h.t += 60

    def test_it_is_cordoned_and_left_running_until_the_deadline(self):
        h = self.one()
        c = h.controller(nodes=("a",))
        self.drive(h, c, 599)
        self.assertIn("a", cordoned(h))
        self.assertEqual(h.acted["off"], [], "cut power to running work early")

    def test_after_the_deadline_it_is_announced_and_soft_shut_down(self):
        h = self.one()
        spy = OrderedSpy(h)
        c = h.controller(nodes=("a",), notifier=spy)
        self.drive(h, c, 660)
        self.assertEqual(h.acted["off"], ["a"])
        self.assertLess(h.sequence.index(("down", "a")),
                        h.sequence.index(("off", "a")))
        self.assertEqual(c.forced_total, 1)
        forced = h.at_level("error", "SHED FORCED")
        self.assertEqual(len(forced), 1, "work ended without an error line")
        self.assertEqual(forced[0]["units"], ["job-1"])

    def test_the_deadline_is_exact(self):
        h = self.one()
        c = h.controller(nodes=("a",))
        c.tick()                                   # T0: the shed begins
        h.t = T0 + 599
        c.tick()
        self.assertEqual(h.acted["off"], [])
        h.t = T0 + 600
        c.tick()
        self.assertEqual(h.acted["off"], ["a"])

    def test_a_deadline_of_zero_never_forces(self):
        h = self.one()
        c = h.controller(nodes=("a",), ceiling_drain_deadline_s=0)
        self.drive(h, c, 20_000)
        self.assertEqual(h.acted["off"], [], "forced with the deadline off")
        self.assertEqual(c.forced_total, 0)

    def test_a_drain_on_a_node_cut_off_from_the_cluster_is_still_forced(self):
        """NotReady is not off: a kubelet cut off from the API server leaves
        its work running, and the node draws the power the ceiling is about.
        It counts as powered, and past its deadline it is shut down like any
        other -- announced first."""
        h = world({"a": node(ready=False, cordoned=True, ours=True,
                             ours_since=T0 - 100)},
                  busy={"a": ["job-1"]}, ceiling=0)
        spy = OrderedSpy(h)
        c = h.controller(nodes=("a",), notifier=spy)
        c.st["a"] = {"phase": "sleeping", "phase_since": T0 - 100}
        for t in (0, 300, 599):
            h.t = T0 + t
            c.tick()
        self.assertEqual(h.acted["off"], [])
        h.t = T0 + 600
        c.tick()
        self.assertEqual(h.acted["off"], ["a"], "a node cut off from the "
                                                "cluster was never shed")
        self.assertEqual(spy.down, ["a"])

    def test_a_node_woken_under_a_ceiling_drops_the_anchor_of_the_shed_before_it(self):
        """The anchor is a node's own record of THIS shed. One woken out of an
        earlier shed, while the ceiling stays in force, and then shed again must
        be given a fresh deadline -- not judged by one that expired before the
        wake began. (Released and woken is the easy case: the release clears it.
        This is the ceiling that never went away.)"""
        h = world({"a": asleep(shed_at=T0 - 5000, ours_since=T0 - 5000),
                   "b": asleep()}, shortfall=400.0, ceiling=1)
        c = h.controller()
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])
        self.assertIn(("a", "shed", None),
                      [(n, k, v) for n, k, v in h.acted["note"]],
                      "woke a node and left it carrying the old shed's anchor")
        h._ceiling = 0                  # tightens: it arrives into no room
        h.t += 60
        c.tick()
        stamped = [v for n, v in shed_notes(h) if n == "a" and v is not None]
        self.assertEqual(stamped, [T0 + 60],
                         "shed again against the anchor of the shed before it")

    def test_the_drain_timeout_does_not_return_a_shed_node_to_service(self):
        """sleep()'s own exits both end in _abandon_sleep(), which uncordons,
        and a shed that returns its node to service defeats the ceiling."""
        h = self.one()
        c = h.controller(nodes=("a",), drain_timeout_s=100)
        self.drive(h, c, 500)
        self.assertNotIn(("a", False), h.acted["cordon"])
        self.assertEqual(c.st["a"]["phase"], "sleeping")

    def test_the_attempt_bound_does_not_return_it_either(self):
        h = self.one()
        c = h.controller(nodes=("a",))
        c.st["a"] = {"sleep_attempts": 99}
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"])
        self.assertEqual(c.st["a"]["phase"], "sleeping")

    def test_the_deadline_survives_a_restart(self):
        """In memory, a restart resets the deadline and a busy node is held for
        as long as the controller keeps restarting."""
        h = self.one()
        c = h.controller(nodes=("a",))
        c.tick()
        for t in (120, 240, 360, 480, 540):
            h.t = T0 + t
            c.st = {}                              # the process restarted
            c.tick()
        self.assertEqual(h.acted["off"], [])
        h.t = T0 + 601
        c.st = {}
        c.tick()
        c.tick()
        self.assertEqual(h.acted["off"], ["a"],
                         "a restart pushed the deadline out")
        self.assertEqual(shed_notes(h), [("a", T0)],
                         "the anchor was rewritten by a restart")

    def test_a_drain_already_running_is_re_anchored_when_the_ceiling_engages(self):
        """The cordon is as old as the node's last sleep. A deadline measured
        from it would force a drain that began a second ago."""
        h = self.one(ceiling=None)
        h.states["a"] = node(cordoned=True, ours=True, ours_since=T0 - 5000)
        c = h.controller(nodes=("a",), drain_timeout_s=99_999)
        c.st["a"] = {"phase": "sleeping", "phase_since": T0 - 5000}
        c.tick()                                   # an ordinary drain
        h._ceiling = 0
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["off"], [], "forced a drain at engagement")
        self.assertEqual(shed_notes(h), [("a", T0 + 60)])
        h.t = T0 + 60 + 599
        c.tick()
        self.assertEqual(h.acted["off"], [])
        h.t = T0 + 60 + 600
        c.tick()
        self.assertEqual(h.acted["off"], ["a"])

    def test_the_deadline_is_not_enforced_during_the_release_hold(self):
        """A one-sample spike followed by a long hold must not force a busy
        node off: the hold keeps nodes down, it does not justify killing
        work."""
        h = self.one()
        c = h.controller(nodes=("a",))
        c.tick()                                   # the spike
        h._ceiling = None
        for t in range(60, 900, 60):               # the hold: 15 minutes
            h.t = T0 + t
            c.tick()
        self.assertEqual(h.acted["off"], [], "forced during the hold")
        h.t = T0 + 900                             # the hold ends: released
        c.tick()
        h.t += 60
        c.tick()
        self.assertEqual(h.acted["off"], [])
        self.assertNotIn(("a", False), h.acted["cordon"])

    def test_it_is_not_forced_if_the_reading_no_longer_asks_for_it(self):
        """Two nodes, one busy and draining under a ceiling of 1; the reading
        loosens to 2 -- nothing now needs to go, whatever the hold says."""
        h = world({"a": node(ready_since=T0 - 10),
                   "b": node(ready_since=T0 - 10)},
                  busy={"b": ["job-1"]}, ceiling=0)
        c = h.controller()
        c.tick()
        h._ceiling = 2
        for t in range(60, 1200, 60):
            h.t = T0 + t
            c.tick()
        self.assertEqual([n for n in h.acted["off"] if n == "b"], [])

    def test_a_failed_announcement_blocks_the_power_off_past_the_deadline(self):
        """metalnap does not power off a node it could not announce, and the
        ceiling does not override that. The consequence is stated: a shed node
        stays powered, and it is logged at error every tick."""
        h = self.one()
        c = h.controller(nodes=("a",), notifier=OrderedSpy(h, fail=True))
        self.drive(h, c, 900)
        self.assertEqual(h.acted["off"], [], "cut power to an unannounced node")
        late = [kv for kv in h.at_level("error", "announce")]
        self.assertGreaterEqual(len(late), 3,
                                "not logged at error on every tick")

    def test_a_busy_check_that_raises_reads_as_busy_until_the_deadline(self):
        h = self.one(busy=RuntimeError("arc down"))
        c = h.controller(nodes=("a",))
        self.drive(h, c, 540)
        self.assertEqual(h.acted["off"], [])
        self.drive(h, c, 700)
        self.assertEqual(h.acted["off"], ["a"])

    def test_idle_units_are_still_released_when_forced(self):
        h = self.one(idle=["u1"], holds=False)
        c = h.controller(nodes=("a",))
        self.drive(h, c, 700)
        self.assertEqual(h.acted["released"], ["u1"])
        self.assertEqual(h.acted["off"], ["a"])

    def test_a_unit_that_gained_work_is_left_alone_but_does_not_stop_it(self):
        h = self.one(idle=["u1"], holds=True)
        c = h.controller(nodes=("a",))
        self.drive(h, c, 700)
        self.assertEqual(h.acted["released"], [], "released a unit with work")
        self.assertEqual(h.acted["off"], ["a"])

    def test_a_node_that_ignores_the_shutdown_is_reported_and_never_forced(self):
        h = self.one(busy=())
        h.ignores_off.add("a")
        spy = OrderedSpy(h)
        c = h.controller(nodes=("a",), notifier=spy)
        self.drive(h, c, 1500)
        self.assertEqual(h.acted["cycle"], [], "a hard cut for a slow shutdown")
        self.assertIn("a", spy.alerts, "the node that ignored it was not "
                                       "reported")
        self.assertGreaterEqual(len(h.acted["off"]), 2,
                                "metalnap stopped asking")


class TestCeilingTakesOnlyWhatItNeeds(unittest.TestCase):
    """A shed ends running work after a deadline, and bypasses the drain
    timeout that would hand a node back. Both are for a node the ceiling
    needs gone -- not for every node that happens to be draining when it
    engages, which is what the simulation caught: two nodes draining, a
    ceiling of one, and the one that was not needed pinned cordoned for as long
    as its hung work ran."""

    def draining(self, **kw):
        h = world({n: node(cordoned=True, ours=True, ours_since=T0 - 100)
                   for n in ("a", "b")}, **kw)
        return h

    def test_only_as_many_drains_as_must_go_become_sheds(self):
        h = self.draining(busy={"b": ["job"]}, ceiling=1)
        c = h.controller(**ONLY_THE_CEILING)
        for n in ("a", "b"):
            c.st[n] = {"phase": "sleeping", "phase_since": T0 - 100}
        c.tick()
        self.assertEqual(shed_notes(h), [("a", T0)],
                         "the idle one goes; the busy one is not needed gone")

    def test_a_drain_the_ceiling_does_not_need_is_still_an_ordinary_drain(self):
        """...with an ordinary drain's timeout, which hands it back."""
        h = self.draining(busy={"b": ["job"]}, ceiling=1)
        c = h.controller(**ONLY_THE_CEILING)
        for n in ("a", "b"):
            c.st[n] = {"phase": "sleeping", "phase_since": T0 - 100}
        for t in range(0, 2000, 60):
            h.t = T0 + t
            c.tick()
        self.assertIn(("b", False), h.acted["cordon"],
                      "pinned cordoned by a shed the ceiling did not need")
        self.assertEqual(h.acted["off"], ["a"])

    def test_a_shed_the_reading_no_longer_needs_is_given_back(self):
        """The reading loosens while two busy nodes drain, and the hold runs
        out: one is needed gone no longer. It returns to an ordinary drain --
        the one earlier in the list, since the later one is the first to go --
        and the other, which the ceiling still needs gone, is forced at its
        deadline as it always was."""
        h = world({"a": node(), "b": node()}, ceiling=0,
                  busy={"a": ["j1"], "b": ["j2"]})
        c = h.controller(ceiling_release_hold_s=300)
        c.tick()
        self.assertEqual(sorted(n for n, _t in shed_notes(h)), ["a", "b"])
        h._ceiling = 1
        for t in range(60, 1000, 60):
            h.t = T0 + t
            c.tick()
        self.assertEqual([n for n, v in shed_notes(h) if v is None], ["a"],
                         "gave back the wrong node, or none")
        self.assertEqual(h.acted["off"], ["b"],
                         "forced the node it gave back, or neither")


class TestCeilingForcesOnlyWhatTheReadingStillAsks(unittest.TestCase):
    def test_one_forced_node_is_enough_when_one_must_go(self):
        """Three nodes, a ceiling that left a in service and shed b and c, both
        busy. The reading loosens to two: past the deadline ONE of them has to
        go, not both."""
        h = world({n: node() for n in ("a", "b", "c")}, ceiling=1,
                  busy={"a": ["j0"], "b": ["j1"], "c": ["j2"]})
        c = h.controller(nodes=("a", "b", "c"))
        c.tick()
        self.assertEqual(sorted(cordoned(h)), ["b", "c"])
        h._ceiling = 2
        h.t = T0 + 600                       # the deadline, the hold running
        c.tick()
        self.assertEqual(len(h.acted["off"]), 1,
                         "forced two nodes where the reading asks for one")
        self.assertEqual(c.forced_total, 1)


class TestCeilingHysteresis(unittest.TestCase):
    """Tighten at once, loosen reluctantly: the effective limit is the minimum
    of the readings in the trailing hold, and a flapping signal must not cost a
    wake and a sleep per flap."""

    def test_it_tightens_on_the_first_reading(self):
        h = world({"a": node(), "b": node()}, ceiling=None)
        c = h.controller(**ONLY_THE_CEILING)
        run(h, c, 3)
        self.assertEqual(h.acted["cordon"], [])
        h._ceiling = 0
        c.tick()
        self.assertEqual(sorted(cordoned(h)), ["a", "b"],
                         "waited for evidence before tightening")

    def test_a_flapping_signal_costs_no_wake_and_sleep_cycle(self):
        h = world({"a": node(), "b": node()}, ceiling=0)
        c = h.controller()
        for i, reading in enumerate([0, None] * 5):
            h._ceiling = reading
            h._shortfall = 400.0 if i >= 3 else 0.0
            c.tick()
            h.t += 60
        self.assertEqual(h.acted["on"], [], "woke a node on a None reading")
        self.assertEqual(sorted(h.acted["off"]), ["a", "b"],
                         "each node should go down exactly once")
        self.assertEqual([n for n, v in h.acted["cordon"] if not v], [])

    def test_it_releases_only_after_the_hold_and_wakes_nothing_by_itself(self):
        h = world({"a": node(), "b": node()}, ceiling=0)
        c = h.controller(wake_sustain_s=120)
        c.tick()                                     # T0: engaged
        h._ceiling, h._shortfall = None, 400.0
        for t in (60, 120, 600, 840):
            h.t = T0 + t
            c.tick()
        self.assertEqual(h.acted["on"], [], "released before the hold ran out")
        self.assertEqual([k for _n, k, v in h.acted["note"]
                          if k == "shed" and v is None], [])
        h.t = T0 + 900
        c.tick()                                     # the hold has run out
        self.assertEqual(sorted(n for n, v in shed_notes(h) if v is None),
                         ["a", "b"], "the notes outlived the ceiling")
        self.assertEqual(h.acted["on"], [],
                         "waking at the release, with no sustained demand")
        h.t = T0 + 1019
        c.tick()
        self.assertEqual(h.acted["on"], [])
        h.t = T0 + 1020
        c.tick()
        self.assertEqual(h.acted["on"], ["a"],
                         "demand did not wake a node through the usual window")

    def test_a_looser_reading_waits_out_the_hold_too(self):
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=0)
        c = h.controller()
        c.tick()
        h._ceiling = 1
        for t in (60, 600, 899):
            h.t = T0 + t
            c.tick()
        self.assertEqual(h.acted["on"], [], "loosened before the hold")
        h.t = T0 + 900
        run(h, c, 6)
        self.assertEqual(h.acted["on"], ["a"], "one node of two was allowed")


class TestCeilingInteractions(unittest.TestCase):
    def test_held_and_maintenance_nodes_are_neither_shed_nor_counted(self):
        def states():
            return {"a": node(), "b": node(cordoned=True, ours=False),
                    "c": node(maintenance="kernel")}
        nodes = ("a", "b", "c")
        # A ceiling of 1 over a pool of ONE: nothing to shed. Counting the
        # exempt nodes would shed a, the only node the ceiling governs.
        h = world(states(), ceiling=1)
        run(h, h.controller(nodes=nodes, **ONLY_THE_CEILING), 3)
        self.assertEqual(h.acted["cordon"], [])
        h = world(states(), ceiling=0)
        c = h.controller(nodes=nodes, **ONLY_THE_CEILING)
        c.tick()
        self.assertEqual(cordoned(h), ["a"], "touched an exempt node")
        said = h.logged("capacity ceiling")
        self.assertTrue(any(kv.get("exempt") == ["b", "c"] for kv in said),
                        "the log does not say how many nodes are exempt")

    def test_a_shed_in_flight_is_abandoned_when_an_operator_cordons(self):
        h = world({"a": node(), "b": node()}, ceiling=0,
                  busy={"a": ["j1"], "b": ["j2"]})
        c = h.controller()
        c.tick()
        h.states["a"] = dataclasses.replace(h.states["a"], ours=False,
                                            ours_since=None)
        h.t += 60
        c.tick()
        self.assertIsNone(c.st["a"]["phase"])
        self.assertNotIn("a", h.acted["off"])
        self.assertIn(("a", "shed", None),
                      [(n, k, v) for n, k, v in h.acted["note"]],
                      "an exempt node kept its shed note")

    def test_an_operator_uncordon_mid_shed_is_not_fought(self):
        """The ceiling does not fight a person: the node they put back into
        service is left alone, and another is shed in its place if the ceiling
        still needs one."""
        h = world({"a": node(), "b": node()}, ceiling=0)
        c = h.controller()
        c.tick()
        # kubectl uncordon: the cordon goes; our mark is left behind for the
        # controller to clear, which is what disown() does.
        h.states["a"] = dataclasses.replace(h.states["a"], cordoned=False,
                                            ours=False, ours_since=None)
        for _ in range(5):
            h.t += 60
            c.tick()
        self.assertEqual(cordoned(h).count("a"), 1,
                         "re-cordoned a node an operator had just put back")
        self.assertIn("b", h.acted["off"], "the rest of the shed went on")
        self.assertTrue(h.logged("leaves it alone"),
                        "the ceiling did not say it was standing aside")

    def test_a_wake_that_arrives_into_a_full_ceiling_is_shed_not_served(self):
        """A booting OS cannot be asked to shut down, so a wake already in
        flight is let to arrive. It is then not put into service and not
        warmed -- shed, the tick it is Ready, in place of the node that was
        already serving."""
        warm = WarmSpy()
        h = world({"a": asleep(), "b": node()}, shortfall=400.0, ceiling=None,
                  busy={"b": ["j"]})
        c = h.controller(warmup=warm)
        c.tick()
        self.assertEqual(h.acted["on"], ["a"])
        h._ceiling = 1
        h.t += 60
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"],
                         "put a node into service over the ceiling")
        self.assertEqual(warm.started, [], "warmed a node about to be shed")
        self.assertEqual(c.st["a"]["phase"], "sleeping")
        self.assertIn(("a", T0 + 60), shed_notes(h))
        self.assertNotIn("b", cordoned(h), "shed the node in service instead "
                                           "of the one that had just arrived")

    def test_a_wake_that_arrives_between_the_look_and_its_own_read(self):
        """The wake finishes on a fresh read of the node, as the power cycle
        does, and that read can find it Ready when this tick's observation did
        not. It completes without being put into service."""
        warm = WarmSpy()
        h = Harness({"a": asleep(), "b": node()}, ceiling=1,
                    busy={"b": ["j"]})
        h.t = T0
        h.fresh["a"] = node(ready=True, cordoned=True, ours=True)
        c = h.controller(warmup=warm)
        c.st["a"] = {"phase": "waking", "phase_since": T0, "booting": True}
        c.tick()
        self.assertNotIn(("a", False), h.acted["cordon"])
        self.assertEqual(warm.started, [])
        self.assertIsNone(c.st["a"]["phase"])
        self.assertTrue(h.logged("leaves no room"))

    def test_a_warming_node_is_cleaned_up_then_shed(self):
        warm = WarmSpy()
        h = world({"a": node()}, ceiling=0)
        c = h.controller(nodes=("a",), warmup=warm)
        c.st["a"] = {"phase": "warming", "phase_since": T0}
        c.tick()
        self.assertEqual(warm.cleaned, ["a"])
        self.assertEqual(c.st["a"]["phase"], "sleeping")

    def test_a_visit_is_ended_at_once_through_the_ordinary_sleep(self):
        h = world({"a": node(cordoned=True, ours=True, ours_since=T0 - 10)},
                  ceiling=0)
        c = h.controller(nodes=("a",), **TestScheduledMaintenance.MAINT)
        c.st["a"] = {"phase": "maintaining", "phase_since": T0 - 10,
                     "maintenance_until": T0 + 250}
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "sleeping",
                         "the visit was left to run out its window")
        self.assertNotIn("maintenance_until", c.st["a"])
        self.assertIn(("a", True), h.acted["cordon"],
                      "ended without refreshing the cordon its drain anchors")

    def test_a_visit_ends_even_while_the_ceiling_has_room(self):
        """A visit powers hardware on for nobody, so none runs while a ceiling
        is in force -- whether or not it is binding on this node."""
        h = world({"a": node(cordoned=True, ours=True, ours_since=T0 - 10),
                   "b": asleep()}, ceiling=1)
        c = h.controller(**TestScheduledMaintenance.MAINT)
        c.st["a"] = {"phase": "maintaining", "phase_since": T0 - 10,
                     "maintenance_until": T0 + 250}
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "sleeping")
        self.assertEqual(shed_notes(h), [], "an ordinary end to a visit was "
                                            "recorded as a shed")

    def test_a_visit_still_booting_is_ended_the_moment_it_arrives(self):
        h = Harness({"a": asleep()}, ceiling=0, chassis={"a": "on"})
        h.t = T0
        c = h.controller(nodes=("a",), **TestScheduledMaintenance.MAINT)
        c.st["a"] = {"phase": "maintaining", "phase_since": T0 - 10}
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "maintaining",
                         "tore down a node that was still booting")
        h.states["a"] = node(cordoned=True, ours=True, ours_since=T0 - 500)
        h.t += 60
        c.tick()
        self.assertEqual(c.st["a"]["phase"], "sleeping")

    def test_a_node_already_powering_off_is_left_alone(self):
        h = Harness({"a": node(cordoned=True, ours=True, ours_since=T0 - 50)},
                    ceiling=0, chassis={"a": "on"})
        h.t = T0
        c = h.controller(nodes=("a",))
        c.st["a"] = {"phase": "powering_off", "phase_since": T0 - 5}
        c.tick()
        self.assertEqual(h.acted["off"], [], "asked a shutting-down node again")
        self.assertEqual(c.st["a"]["phase"], "powering_off")

    def test_a_cold_boot_is_not_shed_in_place_of_a_node_in_service(self):
        h = world({"a": node(), "b": asleep()}, shortfall=400.0, ceiling=None,
                  busy={"a": ["j1"]})
        c = h.controller()
        c.tick()
        self.assertEqual(h.acted["on"], ["b"])
        h._ceiling = 1
        for _ in range(4):
            h.t += 60
            c.tick()
        self.assertNotIn("a", cordoned(h),
                         "shed a busy node in service to make room for a "
                         "node still booting")
        self.assertEqual([n for n, _t in shed_notes(h)], ["b"],
                         "the node that arrived was not the one shed")

    def test_a_wedged_wake_is_not_power_cycled_while_a_ceiling_is_engaged(self):
        """A cycle would power back on a node the ceiling wants down. It is
        handed to a human like any wedged node instead."""
        for ceiling, cycled in ((None, True), (1, False)):
            with self.subTest(ceiling=ceiling):
                h = Harness({"a": crashed(), "b": node()}, ceiling=ceiling,
                            busy={"b": ["j"]}, chassis={"a": "on"})
                h.t = T0
                c = h.controller()
                c.st["a"] = {"phase": "waking", "phase_since": T0 - 901,
                             "booting": False}
                c.tick()
                self.assertEqual(h.acted["cycle"], ["a"] if cycled else [])
                if not cycled:
                    self.assertIn("ceiling", c.st["a"]["trouble"])

    def test_an_operators_maintenance_power_on_is_not_blocked_by_a_ceiling(self):
        """A person asked. The node is exempt from the ceiling, and nothing a
        ceiling does stands between them and the machine."""
        h = world({"a": held(asleep(), reason="firmware"), "b": node()},
                  ceiling=0)
        c = h.controller()
        c.tick()
        self.assertEqual(h.acted["on"], ["a"],
                         "a ceiling stood in the way of a person's request")

    def test_a_ceiling_that_does_not_limit_the_pool_holds_nothing_back(self):
        """A standing budget above the pool is no ceiling in practice, and must
        not hold scheduled visits or a wedged node's power cycle back."""
        h = world({"a": asleep(), "b": None}, ceiling=5)
        c = h.controller(nodes=("a",), **TestScheduledMaintenance.MAINT)
        c.tick()
        self.assertEqual(h.acted["on"], ["a"], "a visit was held back by a "
                                               "ceiling that limits nothing")
        h = Harness({"a": crashed()}, shortfall=400.0, chassis={"a": "on"},
                    ceiling=5)
        h.t = T0
        c = wedged(h, h.controller(nodes=("a",)))
        self.assertEqual(h.acted["cycle"], ["a"],
                         "the cycle was refused for a ceiling above the pool")

    def test_visits_held_back_by_a_ceiling_say_so_once(self):
        h = world({"a": asleep(), "b": None}, ceiling=0)
        c = h.controller(nodes=("a",), **TestScheduledMaintenance.MAINT)
        run(h, c, 4)
        self.assertEqual(len(h.logged("visits are held back")), 1)

    def test_dry_run_logs_what_it_would_shed_and_touches_nothing(self):
        spy, sink, status = Spy(), _Sink(), _Sink()
        h = world({"a": node(), "b": node()}, ceiling=1)
        c = h.controller(mode="dry_run", notifier=spy, metrics=sink,
                         status=status, **ONLY_THE_CEILING)
        run(h, c, 3)
        self.assertEqual((h.acted["cordon"], h.acted["note"], h.acted["off"],
                          h.acted["on"]), ([], [], [], []))
        self.assertEqual((spy.down, spy.alerts), ([], {}))
        self.assertEqual(len(h.logged("would shed")), 1,
                         "a shadow should say it once per change")
        self.assertEqual(status.reports, [], "wrote outside the process")
        self.assertEqual((sink.last["engaged"], sink.last["shed"]),
                         (True, []))
        h._ceiling = 0                # tightens at once: a different decision
        c.tick()
        self.assertEqual(len(h.logged("would shed")), 2,
                         "a changed decision was not said")

    def test_dry_run_says_it_would_not_wake_and_live_says_it_is_not_waking(self):
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=0)
        h.controller(mode="dry_run").tick()
        self.assertTrue(h.logged("would not wake"))
        h = world({"a": asleep(), "b": asleep()}, shortfall=400.0, ceiling=0)
        h.controller().tick()
        self.assertTrue(h.logged("not waking"))

    def test_a_failing_status_write_never_stops_a_shed(self):
        h = world({"a": node(), "b": node()}, ceiling=0)
        c = h.controller(status=_Sink(fail=True), metrics=_Sink(fail=True))
        run(h, c, 4)
        self.assertEqual(sorted(cordoned(h)), ["a", "b"])
        self.assertEqual(sorted(h.acted["off"]), ["a", "b"])
        self.assertEqual(len(h.at_level("warn", "status")), 1,
                         "the failure was not logged, or logged every tick")

    def test_status_is_written_only_when_the_controller_is_live(self):
        for mode, written in (("on", True), ("dry_run", False)):
            with self.subTest(mode=mode):
                status = _Sink()
                h = world({"a": node()}, ceiling=None)
                h.controller(nodes=("a",), mode=mode, status=status).tick()
                self.assertEqual(bool(status.reports), written)


class TestCeilingReport(unittest.TestCase):
    """What the controller publishes -- to metrics, and to the status object
    `metalnap status` reads."""

    def test_nothing_in_force(self):
        h = world({"a": node(), "b": node()}, ceiling=None)
        sink = _Sink()
        h.controller(metrics=sink).tick()
        r = sink.last
        self.assertEqual((r["engaged"], r["limit"], r["signal"],
                          r["signal_ok"], r["shed"], r["draining"]),
                         (False, None, None, True, [], []))

    def test_engaged_and_draining_with_the_time_it_will_be_forced(self):
        h = world({"a": node(), "b": node()}, ceiling=0, busy={"a": ["j1"]})
        sink = _Sink()
        c = h.controller(metrics=sink)
        c.tick()
        r = sink.last
        self.assertEqual((r["engaged"], r["limit"], r["signal"], r["pool"],
                          r["since"]), (True, 0, 0, 2, T0))
        self.assertEqual(sorted(r["shed"]), ["a", "b"])
        h.t += 60
        c.tick()
        h.t += 60
        c.tick()
        self.assertEqual(sink.last["draining"],
                         [{"node": "a", "until": T0 + 600}])
        self.assertEqual(sorted(sink.last["shed"]), ["a", "b"],
                         "nodes asleep under the ceiling are still shed")

    def test_unavailable_is_reported_as_no_ceiling(self):
        h = world({"a": node(), "b": node()}, ceiling=RuntimeError("down"))
        sink = _Sink()
        h.controller(metrics=sink).tick()
        r = sink.last
        self.assertEqual((r["signal_ok"], r["engaged"], r["limit"],
                          r["signal"]), (False, False, None, None))

    def test_the_hold_reports_the_limit_it_is_holding(self):
        h = world({"a": node(), "b": node()}, ceiling=0)
        sink = _Sink()
        c = h.controller(metrics=sink)
        c.tick()
        h._ceiling = None
        h.t += 60
        c.tick()
        r = sink.last
        self.assertEqual((r["engaged"], r["limit"], r["signal"]),
                         (True, 0, None))

    def test_since_survives_a_restart(self):
        """From the notes on the nodes, not from the process: an operator
        reading `metalnap status` after a restart is told when it began."""
        h = world({"a": node(), "b": node()}, ceiling=0)
        sink = _Sink()
        c = h.controller(metrics=sink)
        c.tick()
        h.t += 3600
        c.st = {}
        c.tick()
        self.assertEqual(sink.last["since"], T0)

    def test_exempt_nodes_are_listed(self):
        h = world({"a": node(), "b": node(cordoned=True, ours=False)},
                  ceiling=0)
        sink = _Sink()
        h.controller(metrics=sink).tick()
        self.assertEqual(sink.last["exempt"], ["b"])
        self.assertEqual(sink.last["pool"], 1)

    def test_forced_sheds_are_counted(self):
        h = world({"a": node()}, ceiling=0, busy={"a": ["j1"]})
        sink = _Sink()
        c = h.controller(nodes=("a",), metrics=sink)
        for t in (0, 60, 600, 660):
            h.t = T0 + t
            c.tick()
        self.assertEqual(sink.last["forced"], 1)


class TestMetrics(unittest.TestCase):
    def report(self, **kw):
        base = dict(configured=True, mode="on", engaged=False, limit=None,
                    signal=None, signal_ok=True, since=None, pool=2, shed=[],
                    draining=[], exempt=[], forced=0)
        base.update(kw)
        return base

    def lines(self, m):
        return [ln for ln in m.render().splitlines()
                if ln and not ln.startswith("#")]

    def test_before_the_first_tick_nothing_is_engaged(self):
        from metalnap.metrics import Metrics
        got = self.lines(Metrics())
        self.assertIn("metalnap_capacity_ceiling_engaged 0", got)
        self.assertIn("metalnap_capacity_ceiling_signal_ok 1", got)
        self.assertIn("metalnap_nodes_shed 0", got)
        self.assertIn("metalnap_shed_forced_total 0", got)

    def test_the_limit_is_absent_when_there_is_none(self):
        from metalnap.metrics import Metrics
        m = Metrics()
        m.publish(self.report())
        self.assertFalse([ln for ln in self.lines(m)
                          if ln.startswith("metalnap_capacity_ceiling ")],
                         "reported a limit where there is none")
        m.publish(self.report(limit=0, engaged=True))
        self.assertIn("metalnap_capacity_ceiling 0", self.lines(m))

    def test_engaged_shed_forced_and_unavailable(self):
        from metalnap.metrics import Metrics
        m = Metrics()
        m.publish(self.report(engaged=True, limit=1, shed=["a", "b"],
                              forced=3, signal_ok=False))
        got = self.lines(m)
        for want in ("metalnap_capacity_ceiling 1",
                     "metalnap_capacity_ceiling_engaged 1",
                     "metalnap_capacity_ceiling_signal_ok 0",
                     "metalnap_nodes_shed 2", "metalnap_shed_forced_total 3"):
            self.assertIn(want, got)

    def test_every_metric_is_typed_and_the_counter_is_a_counter(self):
        from metalnap.metrics import Metrics
        text = Metrics().render()
        for name in ("metalnap_capacity_ceiling",
                     "metalnap_capacity_ceiling_engaged",
                     "metalnap_capacity_ceiling_signal_ok",
                     "metalnap_nodes_shed"):
            self.assertIn("# TYPE %s gauge" % name, text)
        self.assertIn("# TYPE metalnap_shed_forced_total counter", text)
        for ln in self.lines(Metrics()):
            self.assertRegex(ln, r"^[a-z_]+ -?[0-9]+$")

    def test_it_serves_the_text_format_over_http(self):
        import urllib.error
        import urllib.request
        from metalnap.metrics import Metrics, serve
        m = Metrics()
        m.publish(self.report(engaged=True, limit=0, shed=["a"]))
        server = serve(m, 0, host="127.0.0.1")
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
                body = r.read().decode()
                self.assertEqual(r.status, 200)
                self.assertTrue(r.headers["Content-Type"]
                                .startswith("text/plain; version=0.0.4"))
            self.assertEqual(body, m.render())
            self.assertIn("metalnap_nodes_shed 1", body)
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + "/elsewhere", timeout=5)
            self.assertEqual(err.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


class TestMetricsListensOnEveryInterface(unittest.TestCase):
    def test_the_default_host_is_reachable_from_the_pod_network(self):
        """No host means every interface -- which is how a scraper reaches a
        pod -- over IPv4 whether or not the machine has IPv6."""
        import urllib.request
        from metalnap.metrics import Metrics, serve
        server = serve(Metrics(), 0)
        try:
            url = "http://127.0.0.1:%d/metrics" % server.server_address[1]
            with urllib.request.urlopen(url, timeout=5) as r:
                self.assertEqual(r.status, 200)
        finally:
            server.shutdown()
            server.server_close()


class _FakeKube:
    """The three calls ConfigMapStatus makes, against one stored object."""

    def __init__(self, fail=None):
        self.cm = {"metadata": {"name": "metalnap-status", "namespace": "ops",
                                "resourceVersion": "7"},
                   "data": {"other": "kept"}}
        self.calls, self.fail = [], fail

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if self.fail:
            raise self.fail
        if method == "GET":
            return json.loads(json.dumps(self.cm))
        self.cm = json.loads(json.dumps(body))
        return self.cm

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]


class TestConfigMapStatus(unittest.TestCase):
    PATH = "/api/v1/namespaces/ops/configmaps/metalnap-status"

    def writer(self, kube, clock):
        from metalnap.status import ConfigMapStatus
        return ConfigMapStatus(kube, "ops", "metalnap-status", clock=clock,
                               refresh_s=300)

    def test_it_replaces_the_object_it_read_and_keeps_what_else_is_in_it(self):
        """get/update, the verbs the chart grants: a replace, not a patch."""
        kube, now = _FakeKube(), [1000.0]
        self.writer(kube, lambda: now[0]).publish({"engaged": True, "limit": 0})
        self.assertEqual(kube.calls, [("GET", self.PATH), ("PUT", self.PATH)])
        self.assertEqual(kube.cm["metadata"]["resourceVersion"], "7")
        self.assertEqual(kube.cm["data"]["other"], "kept")
        body = json.loads(kube.cm["data"]["ceiling"])
        self.assertEqual((body["engaged"], body["limit"], body["updated"],
                          body["refresh_s"]), (True, 0, 1000.0, 300))

    def test_an_unchanged_report_is_not_rewritten_until_it_is_due(self):
        kube, now = _FakeKube(), [1000.0]
        w = self.writer(kube, lambda: now[0])
        w.publish({"engaged": False})
        now[0] += 60
        w.publish({"engaged": False})
        self.assertEqual(len(kube.puts()), 1, "wrote an unchanged report")
        now[0] += 300
        w.publish({"engaged": False})
        self.assertEqual(len(kube.puts()), 2,
                         "never refreshed, so a dead controller looks alive")

    def test_a_changed_report_is_written_at_once(self):
        kube, now = _FakeKube(), [1000.0]
        w = self.writer(kube, lambda: now[0])
        w.publish({"engaged": False})
        now[0] += 60
        w.publish({"engaged": True})
        self.assertEqual(len(kube.puts()), 2)

    def test_a_failed_write_raises_and_is_retried_next_time(self):
        kube, now = _FakeKube(fail=RuntimeError("forbidden")), [1000.0]
        w = self.writer(kube, lambda: now[0])
        with self.assertRaises(RuntimeError):
            w.publish({"engaged": True})
        kube.fail = None
        now[0] += 60
        w.publish({"engaged": True})
        self.assertEqual(len(kube.puts()), 1,
                         "a write that failed was remembered as written")


class TestCeilingWiring(unittest.TestCase):
    """python -m metalnap builds the ceiling from the environment."""

    ENV = {"NODES": "a,b", "BMC_HOST_FMT": "{node}-bmc.", "BMC_USER": "u",
           "BMC_PASS": "p", "PROM_URL": "http://prom", "MODE": "dry_run"}

    def build(self, **extra):
        from metalnap import __main__ as entry
        built = {}
        saved = dict(os.environ)
        real = entry.Controller.run_forever
        entry.Controller.run_forever = lambda c: built.update(c=c)
        try:
            for k in [k for k in os.environ if k.startswith(
                    ("CEILING_", "STATUS_", "METRICS_"))]:
                del os.environ[k]
            os.environ.update(self.ENV, **extra)
            entry.main([])
        finally:
            entry.Controller.run_forever = real
            os.environ.clear()
            os.environ.update(saved)
        return built["c"]

    def test_no_ceiling_unless_one_is_asked_for(self):
        c = self.build()
        self.assertIsInstance(c.ceiling, NullCeiling)
        self.assertIsNone(c.status)

    def test_a_query_reads_the_prometheus_the_demand_signal_reads(self):
        c = self.build(CEILING_QUERY="vector(0)")
        self.assertIsInstance(c.ceiling, PrometheusCeiling)
        self.assertEqual((c.ceiling.url, c.ceiling.query),
                         ("http://prom", "vector(0)"))

    def test_a_static_zero_is_a_ceiling(self):
        c = self.build(CEILING_STATIC="0")
        self.assertIsInstance(c.ceiling, StaticCeiling)
        self.assertEqual(c.ceiling.limit(), 0)

    def test_both_sources_are_refused_at_start(self):
        with self.assertRaises(ValueError):
            self.build(CEILING_QUERY="vector(0)", CEILING_STATIC="1")

    def test_the_status_object_is_named_by_the_chart_and_found_in_its_namespace(self):
        import tempfile
        from metalnap import __main__ as entry
        from metalnap.status import ConfigMapStatus
        with tempfile.TemporaryDirectory() as sa:
            with open(os.path.join(sa, "namespace"), "w") as f:
                f.write("ops\n")
            real = entry.Kube
            entry.Kube = lambda **kw: real(sa=sa, **kw)
            try:
                c = self.build(CEILING_STATIC="0",
                               STATUS_CONFIGMAP="metalnap-status")
            finally:
                entry.Kube = real
        self.assertIsInstance(c.status, ConfigMapStatus)
        self.assertEqual((c.status.namespace, c.status.name),
                         ("ops", "metalnap-status"))

    def test_a_metrics_port_that_is_not_one_stops_the_start(self):
        for bad in ("abc", "-1", "65536", "9100.5"):
            with self.subTest(port=bad), self.assertRaises(SystemExit) as e:
                self.build(METRICS_PORT=bad)
            self.assertIn("METRICS_PORT", str(e.exception))

    def test_metrics_listen_only_when_a_port_is_set(self):
        from metalnap import __main__ as entry
        served = []
        real = entry.serve_metrics
        entry.serve_metrics = lambda m, port: served.append(port)
        try:
            c = self.build()
            self.assertEqual((served, c.metrics is None), ([], True))
            self.build(METRICS_PORT="0")
            self.assertEqual(served, [])
            c = self.build(METRICS_PORT="9100")
            self.assertEqual(served, [9100])
            self.assertIsNotNone(c.metrics)
        finally:
            entry.serve_metrics = real


if __name__ == "__main__":
    unittest.main(verbosity=1)
