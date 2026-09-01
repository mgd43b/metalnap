"""
Deterministic tests for the scenarios the simulation harness catches only
probabilistically.

The division is deliberate. sim.py is for EMERGENT failures across long
sequences; these are for known, precise timing scenarios where a 1-in-40
backstop is not good enough -- particularly anything guarding running work or
an operator's cordon.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalnap import Config, Controller          # noqa: E402
from metalnap.types import NodeState             # noqa: E402


def node(ready=True, cordoned=False, ours=False, ready_since=0.0,
         capacity=100.0, ours_since=None, protected=False):
    return NodeState(ready=ready, cordoned=cordoned, ours=ours,
                     ready_since=ready_since, capacity=capacity,
                     protected=protected, ours_since=ours_since)


class Harness:
    """A minimal stub world. Records what the controller tried to do."""

    def __init__(self, states, shortfall=0.0, saturated=0, busy=(), idle=(),
                 holds=False, fits=True):
        self.states = states
        self._shortfall, self._saturated, self._fits = shortfall, saturated, fits
        self._busy, self._idle, self._holds = list(busy), list(idle), holds
        self.acted = {"cordon": [], "on": [], "off": [], "released": []}
        self.t = 1000.0

    # NodeSource
    def state(self, n):
        return self.states.get(n)

    def set_cordon(self, n, v):
        self.acted["cordon"].append((n, v))

    # PowerBackend lives on its own object: NodeSource.state() and
    # PowerBackend.state() share a name, so one class implementing both hands
    # the power check a NodeState instead of "on"/"off" -- silently, because
    # NodeState == "off" is simply False.
    @property
    def power(self):
        return _Power(self)

    # DemandSignal
    def shortfall(self):
        return self._shortfall

    def saturated_units(self):
        return self._saturated

    def fits_node(self, capacity):
        return self._fits

    # DrainPolicy
    def busy(self, n):
        return list(self._busy)

    def idle(self, n):
        return list(self._idle)

    def holds_work(self, u):
        return self._holds

    def release(self, u):
        self.acted["released"].append(u)

    def residual(self, n):
        return []

    def controller(self, nodes=("a", "b"), **cfg):
        defaults = dict(mode="on", wake_sustain_s=0, sleep_sustain_s=0,
                        min_uptime_s=0)
        defaults.update(cfg)          # let a test override any of them
        c = Config(**defaults)
        return Controller(nodes=list(nodes), node_source=self,
                          power=self.power, signal=self, drain=self, config=c,
                          log=lambda *a, **k: None, clock=lambda: self.t)


class _Power:
    def __init__(self, h):
        self.h = h

    def state(self, n):
        st = self.h.states.get(n)
        return "on" if (st and st.ready) else "off"

    def on(self, n):
        self.h.acted["on"].append(n)

    def soft_off(self, n):
        self.h.acted["off"].append(n)


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

        def going_down(self, n):
            if self.fail:
                raise RuntimeError("alertmanager unreachable")
            self.down.append(n)

        def back_up(self, n):
            self.up.append(n)

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
        h = Harness({"a": node(ready=False), "b": None}, shortfall=400.0)
        spy = self.Spy()
        c = h.controller(mode="dry_run")
        c.notifier = spy
        c.tick()
        self.assertEqual(spy.down, [], "dry_run announced a node as down")
        self.assertEqual(spy.up, [], "dry_run cleared a notification")

    def test_a_node_that_is_up_gets_its_notification_cleared(self):
        """A leftover silence on a live node swallows a real failure."""
        h = Harness({"a": node(ready=True), "b": None})
        spy = self.Spy()
        c = h.controller()
        c.notifier = spy
        c.tick()
        self.assertIn("a", spy.up, "never cleared the notification")


class TestFlickeringDemand(unittest.TestCase):
    """`want` oscillates when a queue sits exactly at its ceiling."""

    def test_dip_to_equality_does_not_reset_the_wake_timer(self):
        h = Harness({"a": node(), "b": node(ready=False)},
                    shortfall=60.0, saturated=1)
        c = h.controller(wake_sustain_s=120)
        # t, saturated: 2 ticks wanting more, a dip, then wanting more again.
        woken = []
        for dt, sat in ((0, 1), (60, 1), (90, 0), (200, 1)):
            h.t = 1000.0 + dt
            h._saturated = sat
            c.tick()
            woken += h.acted["on"]
            h.acted["on"] = []
        self.assertIn("b", woken,
                      "flickering demand never accumulated the sustain window")

    def test_a_one_off_spike_does_not_leave_a_primed_timer(self):
        h = Harness({"a": node(), "b": node(ready=False)},
                    shortfall=60.0, saturated=1)
        c = h.controller(wake_sustain_s=120)
        woken = []
        for dt, sat in ((0, 1), (60, 0), (400, 0), (460, 1)):
            h.t = 1000.0 + dt
            h._saturated = sat
            c.tick()
            woken += h.acted["on"]
            h.acted["on"] = []
        self.assertEqual(woken, [], "a stale timer fired on a transient spike")


if __name__ == "__main__":
    unittest.main(verbosity=1)
