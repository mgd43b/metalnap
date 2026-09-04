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
         capacity=100.0, ours_since=None, protected=False, down_since=None):
    return NodeState(ready=ready, cordoned=cordoned, ours=ours,
                     ready_since=ready_since, capacity=capacity,
                     protected=protected, ours_since=ours_since,
                     down_since=down_since)


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
        self.assertTrue(all(0 <= o < 3600 for o in offsets), offsets)
        self.assertEqual(len(set(offsets)), 4, "every node got the same slot")
        fresh = h.controller(nodes=("a", "b", "c", "d"), **cfg)
        self.assertEqual([fresh._maintenance_offset(n)
                          for n in ("a", "b", "c", "d")], offsets,
                         "a restart re-rolled the schedule")

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
