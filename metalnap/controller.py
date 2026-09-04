"""
The reconciler.

Design rules, each of which exists because it was learned the hard way running
this against real hardware and real CI:

  * NEVER interrupt running work. Not by eviction, not by power. If a check
    cannot tell whether a node is busy, that reads as busy.
  * An operator's cordon outranks every decision the controller made, INCLUDING
    ones already in flight. Enforce that where operations finish, not only
    where they start.
  * Idle workers do not leave on their own. A warm pool waits forever for work
    a cordoned node will not receive, so it must be released explicitly.
  * Never block. wake and sleep are phase machines taking one step per tick.
    A blocking drain froze every other decision for up to the drain timeout.
  * Bound every retry, and log when a bound is hit. Silent non-convergence is
    the failure mode that hides longest.
  * Wake readily, sleep reluctantly, and hold evidence of demand across the
    dips a noisy signal produces.
  * A node nobody wants still has to be maintained. One that sleeps for weeks
    misses every package update and config run, and then has to catch up at
    the exact moment demand finally wanted it. Scheduled visits are the answer,
    and they are the LOWEST-priority thing here: they yield to demand, to an
    operator, and to any operation already in flight.
"""
import hashlib
import math
import time
from typing import Dict, List, Optional


class Controller:
    def __init__(self, nodes, node_source, power, signal, drain, config,
                 notifier=None, warmup=None, log=None, clock=time.time):
        self.nodes: List[str] = list(nodes)
        self.node_source = node_source
        self.power = power
        self.signal = signal
        self.drain = drain
        from .types import NullNotifier, NullWarmup
        # Both default to no-ops so a minimal wiring still works, but the
        # defaults are named types rather than `if self.notifier:` scattered
        # through the reconcile -- a null object cannot be forgotten at one
        # call site the way a None check can.
        self.notifier = notifier or NullNotifier()
        self.warmup = warmup or NullWarmup()
        self.cfg = config.validate()
        self.now = clock
        self._log = log or _default_log
        #: Per-node operational state, plus a few controller-wide timers. In
        #: memory only: a restart must be survivable, so nothing here may be
        #: required for correctness.
        self.st: Dict[str, dict] = {}

    def log(self, level, msg, **kv):
        self._log(level, msg, mode=self.cfg.mode, **kv)

    # -- helpers ---------------------------------------------------------
    def _node(self, name) -> dict:
        return self.st.setdefault(name, {})

    def _set_cordon(self, name, cordoned):
        """
        The ONLY route to a cordon change, so `dry_run` cannot be bypassed.

        wake() and sleep() return early when the mode is not `on`, but tick()
        also cordons directly -- the mid-sleep abort and the stranded repair --
        and those paths had no mode check at all. A dry_run shadow would
        therefore uncordon a node the LIVE controller was mid-drain on, because
        `stranded` is read from the cluster rather than from our own state.
        It had not fired yet only because no sleep happened to occur while the
        shadow was up.

        A guard at every call site is a guard that gets missed at one of them.
        """
        if self.cfg.mode != "on":
            self.log("info", "dry_run: would %s" %
                     ("cordon" if cordoned else "uncordon"), node=name)
            return
        self.node_source.set_cordon(name, cordoned)

    def run_forever(self):
        self.log("info", "metalnap starting", nodes=self.nodes,
                 interval_s=self.cfg.interval_s)
        while True:
            try:
                self.tick()
            except Exception as e:                    # noqa: BLE001
                self.log("error", "tick failed; no action taken", err=str(e))
            time.sleep(self.cfg.interval_s)

    # -- wake ------------------------------------------------------------
    def wake(self, name):
        """One non-blocking step of a wake."""
        s = self._node(name)
        phase = s.get("phase")

        if phase != "waking":
            self.log("info", "WAKE begin", node=name)
            if self.cfg.mode != "on":
                self.log("info", "dry_run: would power on and uncordon",
                         node=name)
                return
            if self.power.state(name) == "off":
                self.power.on(name)
            s["phase"] = "waking"
            s["phase_since"] = self.now()
            return

        st = None
        try:
            st = self.node_source.state(name)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "readiness check failed; retrying next tick",
                     node=name, err=str(e))
        if st and st.ready:
            # Uncordon FIRST. Anything after this point is an optimisation, and
            # an optimisation must never be able to strand a node that is
            # already powered and Ready but not yet schedulable.
            self._set_cordon(name, False)
            s["awake_since"] = self.now()
            s["sleep_attempts"] = 0
            s.pop("cooldown_until", None)
            self.log("info", "WAKE complete -- node uncordoned", node=name)
            # Warm AFTER the node is already schedulable. Warming first means a
            # slow warmup strands a node that is powered, Ready and serving
            # nothing; this way the worst case is a few early items paying the
            # cost, which is what happened before any warmup existed.
            try:
                self.warmup.start(name)
                s["phase"] = "warming"
                s["phase_since"] = self.now()
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "warmup could not start; first work may pay "
                                 "the cost", node=name, err=str(e))
                s["phase"] = None
            return
        if self.now() - s.get("phase_since", 0) > self.cfg.wake_timeout_s:
            self.log("error", "WAKE TIMEOUT -- node did not become Ready",
                     node=name, timeout_s=self.cfg.wake_timeout_s)
            s["phase"] = None
        return

    def warm(self, name):
        """One non-blocking step of the warmup phase."""
        s = self._node(name)
        timed_out = (self.now() - s.get("phase_since", 0)
                     > self.cfg.warmup_timeout_s)
        finished = False
        try:
            finished = self.warmup.done(name)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "warmup check failed", node=name, err=str(e))
        if finished or timed_out:
            # The node is already in service, so a timeout here is a warning,
            # never a failure.
            self.log("warn" if timed_out else "info",
                     "warmup did not finish in time" if timed_out
                     else "warmup finished", node=name)
            try:
                self.warmup.cleanup(name)
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "warmup cleanup failed", node=name,
                         err=str(e))
            s["phase"] = None

    # -- sleep -----------------------------------------------------------
    def _abandon_sleep(self, name, s, reason):
        """Hand the node back and refuse to re-enter a sleep for a while.

        Shared by both give-up paths -- the attempt bound and a drain that ran
        past its timeout. Both mean the same thing, so both must leave the same
        state: uncordoned, counters reset, backed off. Clearing the phase while
        leaving the node cordoned strands it, and the stranded repair then
        re-enters the sleep immediately, burning a full drain timeout per try.
        """
        s["phase"] = None
        s["sleep_attempts"] = 0
        s["cooldown_until"] = self.now() + self.cfg.sleep_cooldown_s
        self.log("error", reason, node=name,
                 cooldown_s=self.cfg.sleep_cooldown_s)
        try:
            self._set_cordon(name, False)
        except Exception as e:                        # noqa: BLE001
            self.log("error", "could not uncordon after abandoning sleep",
                     node=name, err=str(e))

    def sleep(self, name, state=None):
        """One non-blocking step of a sleep."""
        s = self._node(name)

        if s.get("phase") != "sleeping":
            self.log("info", "SLEEP begin", node=name)
            if self.cfg.mode != "on":
                self.log("info", "dry_run: would cordon, drain, power off",
                         node=name)
                return
            attempts = s.get("sleep_attempts", 0) + 1
            s["sleep_attempts"] = attempts
            if attempts > self.cfg.max_sleep_attempts:
                self._abandon_sleep(
                    name, s, "sleep did not complete after repeated attempts; "
                             "returning the node to service and backing off")
                return
            # Only cordon if it is not ALREADY cordoned by us. Re-stamping
            # rewrites the ownership timestamp, which is the durable anchor for
            # the drain deadline -- so a restarted sleep would reset the very
            # clock that is supposed to survive a restart, and a node with hung
            # work could be held indefinitely, one restart at a time.
            if not (state and state.cordoned and state.ours):
                self._set_cordon(name, True)
            s["phase"] = "sleeping"
            s["phase_since"] = self.now()
            return

        try:
            busy = self.drain.busy(name)
        except Exception as e:                        # noqa: BLE001
            # Could not tell => assume busy => do nothing.
            self.log("warn", "busy check failed; retrying next tick",
                     node=name, err=str(e))
            return
        if busy:
            # Prefer the DURABLE cordon timestamp over in-memory phase_since.
            # A restart wipes the latter, and the stranded repair then starts a
            # fresh sleep that resets its own deadline -- so a node with hung
            # work could stay cordoned and powered far past the timeout, one
            # restart at a time. Found by the simulation harness.
            began = s.get("phase_since", 0)
            if state is not None and state.ours_since:
                began = min(began, state.ours_since) or state.ours_since
            if self.now() - began > self.cfg.drain_timeout_s:
                self._abandon_sleep(
                    name, s, "work did not finish before the drain timeout; "
                             "returning the node to service and backing off")
                return
            self.log("info", "waiting on running work", node=name,
                     busy=len(busy), units=busy[:5])
            return

        # Nothing here holds work, so anything left is idle -- and idle units
        # never exit by themselves. Waiting for them is waiting forever.
        try:
            idle = self.drain.idle(name)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "idle check failed; retrying next tick",
                     node=name, err=str(e))
            return
        if idle:
            self.log("info", "releasing idle units blocking the drain",
                     node=name, count=len(idle), units=idle[:5])
            for unit in idle:
                # Re-read each unit immediately before releasing it. The
                # listing above is already stale; work can land in that window.
                # Anything uncertain leaves the unit alone and aborts the tick.
                try:
                    if self.drain.holds_work(unit):
                        self.log("info", "unit was given work during the "
                                         "drain; waiting", node=name,
                                 unit=unit)
                        return
                except Exception as e:                # noqa: BLE001
                    self.log("warn", "could not re-check unit before "
                                     "releasing it; retrying next tick",
                             node=name, unit=unit, err=str(e))
                    return
                try:
                    self.drain.release(unit)
                except Exception as e:                # noqa: BLE001
                    self.log("warn", "could not release idle unit; retrying "
                                     "next tick", node=name, unit=unit,
                             err=str(e))
                    return
            # Release is asynchronous. Re-observe next tick rather than racing
            # the teardown.
            return

        residual = self.drain.residual(name)
        if residual:
            self.log("info", "units still present; waiting", node=name,
                     units=residual[:5])
            return

        # Tell the world BEFORE cutting power. A node going down looks exactly
        # like a node dying; without this every sleep pages someone, and people
        # who are paged for routine events stop reading the alerts that matter.
        try:
            self.notifier.going_down(name)
        except Exception as e:                        # noqa: BLE001
            # Do not power off a node we could not announce -- the alert would
            # fire and nobody would know it was us.
            self.log("warn", "could not announce the shutdown; not powering "
                             "off this tick", node=name, err=str(e))
            return
        self.power.soft_off(name)
        s["phase"] = None
        s["sleep_attempts"] = 0
        s["awake_since"] = None
        self.log("info", "SLEEP complete -- powered off", node=name)

    # -- scheduled maintenance -------------------------------------------
    def _maintenance_offset(self, name):
        """A stable per-node offset, so nodes do not all come due together.

        Nodes fall asleep in a herd -- a cluster goes quiet and they follow
        each other down within a tick or two -- so an unstaggered schedule
        brings the same herd back up in unison. Serialising the visits stops
        that being a power spike, but it also means the last node in a large
        fleet waits behind every other, so spread them out as well.

        Drawn from a hash of the node name rather than random(): it looks
        random across a fleet, which is all that is wanted, but it is
        IDENTICAL after a restart. A freshly seeded RNG re-rolls every offset
        on every deploy, so a controller that redeploys often would keep
        re-bunching the very nodes this exists to spread apart.
        """
        spread = self.cfg.maintenance_stagger_s
        if spread <= 0:
            return 0.0
        digest = hashlib.sha256(name.encode("utf-8")).digest()
        return spread * (int.from_bytes(digest[:8], "big") / 2.0 ** 64)

    def _dark_since(self, name, state):
        """When this node was last known to be up, best evidence first.

        `down_since` is DURABLE -- it lives on the node, not in this process --
        so a controller that restarts still knows the machine has been dark for
        a fortnight. Falling back to our own start time is safe but resets the
        clock on every restart, which is why KubeNodeSource takes the trouble
        to report it.

        The last ATTEMPT counts too, and it counts even when it failed. A node
        that will not come back Ready never updates `down_since`, so without
        this it would read as due on every single tick and be power-cycled
        forever.
        """
        base = state.down_since
        if base is None:
            base = self.st.get("_started") or self.now()
        last_try = (self.st.get(name) or {}).get("maintenance_at")
        # Explicit comparison rather than max() against a zero floor: these are
        # unix timestamps, and a floor is only ever wrong -- a clock the caller
        # measures from an arbitrary origin lands below it and reads as "dark
        # since the beginning of time", which is due on every tick forever.
        return last_try if last_try is not None and last_try > base else base

    def _maintenance_due(self, name, state):
        return (self.now() - self._dark_since(name, state)
                >= self.cfg.maintenance_interval_s
                + self._maintenance_offset(name))

    def maintain(self, name, state=None):
        """One non-blocking step of a scheduled maintenance visit.

        The node is left CORDONED throughout. Uncordoning it would advertise
        capacity that is about to be taken away again, so every visit would end
        by draining real work under a five-minute deadline -- and the drain
        would then be the thing keeping the node up. Cordoned, the visit costs
        nothing but power: host-level updates, config management and anything
        running as a DaemonSet all proceed regardless of a cordon, and if
        demand does turn up the node is already booted and one uncordon away.
        """
        s = self._node(name)
        cfg = self.cfg

        if s.get("phase") != "maintaining":
            self.log("info", "MAINTENANCE begin -- waking for updates",
                     node=name, window_s=cfg.maintenance_window_s,
                     dark_s=int(self.now() - self._dark_since(name, state))
                     if state is not None else None)
            # Stamped BEFORE the power call, and on the attempt rather than
            # on success: a node that never comes back Ready is exactly the
            # node that must not be retried on every tick.
            #
            # Stamped in dry_run too. That is internal state, not the outside
            # world, and a shadow that does not advance its own schedule picks
            # the same overdue node every tick forever -- re-logging one line
            # and never once showing you the second machine it would visit.
            s["maintenance_at"] = self.now()
            s.pop("maintenance_until", None)
            if cfg.mode != "on":
                self.log("info", "dry_run: would power on for a maintenance "
                                 "window", node=name)
                return
            if self.power.state(name) == "off":
                self.power.on(name)
            s["phase"] = "maintaining"
            s["phase_since"] = self.now()
            return

        if self.now() - s.get("phase_since", 0) > cfg.maintenance_timeout_s:
            if state is not None and state.ready:
                # Up, but the visit has outstayed its bound -- a node that
                # flapped in and out of Ready long enough to burn it. It is
                # observable and healthy right now, so end the visit the
                # ordinary way.
                self._end_visit(name, state,
                                "MAINTENANCE TIMEOUT -- ending the visit",
                                level="error")
                return
            # Still NotReady when the bound fired. Let go of it, but do NOT
            # power it off. The node is either partway through the updates it
            # was woken to collect or it is broken, and nothing the controller
            # can observe distinguishes those -- which is the same shape as
            # "cannot tell whether a node is busy", and takes the same answer:
            # assume the reading that is expensive to get wrong. Leaving a
            # machine powered costs watts. Cutting power to one writing its own
            # firmware costs the machine, and no remote hands can undo it.
            #
            # It is not abandoned silently. The node is announced down for as
            # long as it stays down, this logs at error, and the next scheduled
            # visit re-checks it -- while the ordinary stranded repair finishes
            # the job the moment it comes back Ready.
            self.log("error", "MAINTENANCE TIMEOUT -- node is still not Ready; "
                              "leaving it powered rather than cutting power to "
                              "a node that may be mid-update", node=name,
                     timeout_s=cfg.maintenance_timeout_s)
            self._release_visit(name)
            return

        if state is not None and state.ready and not state.cordoned:
            # Somebody uncordoned it. The cordon is how a visit holds a node
            # out of service, so losing it means an operator has decided the
            # node should be working -- and ending the visit the normal way
            # would drain and power off the machine they just put back. Let go
            # of it instead; it is in service now, and the ordinary demand
            # logic owns it from here.
            self.log("info", "node was uncordoned during its maintenance "
                             "window; leaving it in service", node=name)
            self._release_visit(name)
            return

        if not (state is not None and state.ready):
            # Two cases, and NEITHER may cut power: still booting, or gone
            # NotReady inside its own window -- which is precisely what a node
            # applying a kernel update and rebooting looks like. Powering that
            # off mid-flight is the failure this feature would otherwise
            # introduce, so the window simply waits, bounded by the timeout
            # above.
            if s.get("maintenance_until"):
                self.log("info", "node went NotReady inside its maintenance "
                                 "window; waiting for it to come back",
                         node=name)
            return

        until = s.get("maintenance_until")
        if until is None:
            # Measured from READY, not from power-on: a node that took eleven
            # minutes to POST would otherwise get no window at all.
            s["maintenance_until"] = self.now() + cfg.maintenance_window_s
            self.log("info", "MAINTENANCE up -- holding the node",
                     node=name, window_s=cfg.maintenance_window_s)
            return
        if self.now() < until:
            return
        self._end_visit(name, state, "MAINTENANCE window over -- sleeping")

    def _release_visit(self, name):
        """Let go of a visit without touching the node's power.

        `maintenance_at` is stamped on the way OUT as well as on the way in, so
        the schedule runs from whichever was later. Stamped only on the way in,
        a visit that burned its whole bound would leave the node due again the
        instant it let go, and retry back-to-back forever.
        """
        s = self._node(name)
        s["phase"] = None
        s["maintenance_at"] = self.now()
        s.pop("maintenance_until", None)

    def _end_visit(self, name, state, reason, level="info"):
        """Finish a visit by handing the node to the ordinary sleep path.

        Every safety rule that governs a sleep governs this one: it announces
        the shutdown, it refuses to power off a node it could not announce, and
        it will not touch running work.
        """
        self._release_visit(name)
        self.log(level, reason, node=name)
        # Re-stamp the cordon before the drain starts. This node has been
        # cordoned since it was PUT TO SLEEP -- possibly weeks -- and sleep()
        # anchors its drain deadline on that timestamp, so left alone the first
        # busy unit would trip a deadline that expired long before this drain
        # began, and the sleep would be abandoned instantly. Re-stamping is the
        # exact thing that is WRONG when a sleep restarts mid-drain, and the
        # exact thing that is right here: this drain genuinely starts now.
        try:
            self._set_cordon(name, True)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "could not refresh the cordon before draining; "
                             "the drain deadline may be short", node=name,
                     err=str(e))
        self.sleep(name, state)

    def _overdue(self, present, states):
        """Nodes that are asleep, ours, and due -- longest dark first."""
        due = []
        for n in present:
            state = states[n]
            if state.ready:
                continue          # already up, and already getting its updates
            if not (state.cordoned and state.ours):
                # A dark node WITHOUT our cordon is not one we put to sleep.
                # Somebody pulled it for a disk swap or a firmware flash, and
                # powering it on underneath them is the single worst thing this
                # feature could do. An operator's cordon outranks the
                # controller; so does an operator's screwdriver.
                continue
            if self._maintenance_due(n, state):
                due.append((self._dark_since(n, state), n))
        # Longest dark first, so a backlog drains oldest-first rather than
        # letting one node at the end of the node list starve behind the rest.
        return [n for _dark, n in sorted(due)]

    def _maybe_maintain(self, present, states, awake, want):
        """Start at most one visit, and only when nothing else is happening."""
        if not self.cfg.maintenance_interval_s:
            return
        if want > len(awake):
            # Demand is unmet. Whatever the tick just did about that, a
            # maintenance visit would be competing with it for the same
            # hardware -- and demand is the reason this controller exists.
            return
        if any(self.st.get(n, {}).get("phase") for n in present):
            # ONE line, doing two jobs. It keeps visits serialised, so a fleet
            # never powers on in unison; and it makes maintenance yield to
            # every wake, sleep, drain and warmup already in flight, which is
            # what "lowest priority" has to mean in a loop that takes one
            # corrective action per tick. A visit deferred by a tick, or by a
            # thousand, costs nothing: the schedule is measured in days.
            return
        overdue = self._overdue(present, states)
        if not overdue:
            return
        node = overdue[0]                 # one node at a time, one per tick
        if len(overdue) > 1:
            self.log("info", "more nodes are due a maintenance visit; they "
                             "wait their turn", node=node,
                     waiting=overdue[1:])
        try:
            self.maintain(node, states[node])
        except Exception as e:                        # noqa: BLE001
            self.log("error", "could not begin a maintenance visit",
                     node=node, err=str(e))

    # -- reconcile -------------------------------------------------------
    def tick(self):
        cfg, st = self.cfg, self.st
        if cfg.mode == "off":
            self.log("info", "mode=off; no observation, no action")
            return
        # The floor under the maintenance schedule for a node source that
        # cannot report when a node went dark. Seeded here rather than in
        # __init__ so that a restart -- which is what wiping `st` models --
        # re-seeds it, exactly as a real process restart would.
        st.setdefault("_started", self.now())

        # Observe. ANY failure here means no action this tick -- fail toward
        # "everything stays on", which is the whole safety posture.
        states = {n: self.node_source.state(n) for n in self.nodes}
        shortfall = self.signal.shortfall()

        # Drop protected nodes before anything else looks at them, so no code
        # path further down can act on one by accident. Configuration is the
        # weakest link here -- the node list comes from a ConfigMap or a Helm
        # value, and a typo must not be able to power off a control plane.
        protected = [n for n in self.nodes
                     if states[n] is not None and states[n].protected]
        if protected:
            if protected != st.get("_protected_last"):
                self.log("error", "REFUSING to manage protected nodes; remove "
                                  "them from the node list", nodes=protected)
                st["_protected_last"] = protected
            for n in protected:
                states[n] = None

        present = [n for n in self.nodes if states[n] is not None]
        absent = [n for n in self.nodes if states[n] is None]
        if absent:
            self.log("info", "configured nodes not present; ignoring them",
                     absent=absent)
        if not present:
            self.log("info", "no configured node exists yet")
            return

        # Reconcile notifications every tick rather than only on transitions.
        # A notification lost to a restart or an Alertmanager outage is then
        # re-asserted, and -- more importantly -- one left over on a node that
        # is UP is cleared, so a real failure of it is not silently swallowed.
        for n in present:
            want_down = not states[n].ready
            if cfg.mode != "on":
                # dry_run must not mutate ANYTHING outside this process, and a
                # notifier writes to a real system. A shadow deployment that
                # silences alerts is not observing, it is participating -- and
                # it will fight the controller it was meant to be compared
                # against. Caught by exactly that: a dry_run metalnap created a
                # live Alertmanager silence next to the incumbent's.
                self.log("info", "dry_run: would mark node %s"
                                 % ("down" if want_down else "up"), node=n)
                continue
            try:
                if want_down:
                    self.notifier.going_down(n)
                else:
                    self.notifier.back_up(n)
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "notification reconcile failed", node=n,
                         err=str(e))

        awake = [n for n in present
                 if states[n].ready and not states[n].cordoned]
        # A cordon this controller does not own belongs to an operator.
        held = [n for n in present
                if states[n].cordoned and not states[n].ours]
        wakeable = [n for n in present if n not in held]
        if held != st.get("_held_last"):
            if held:
                self.log("info", "nodes cordoned by an operator; held out of "
                                 "wake candidates", held=held)
            elif st.get("_held_last"):
                self.log("info", "operator cordon cleared", released=st["_held_last"])
            st["_held_last"] = held

        capacity = min([states[n].capacity for n in present
                        if states[n].capacity > 0] or [cfg.default_capacity])
        try:
            saturated = self.signal.saturated_units()
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "saturation check failed; sizing on shortfall "
                             "alone", err=str(e))
            saturated = 0
        # A saturated queue admits no more work, so its demand is invisible to
        # shortfall(). Count each as one node's worth.
        effective = shortfall + saturated * capacity
        want = max(0, min(len(wakeable), math.ceil(effective / capacity)))

        # Advance in-flight operations, then CARRY ON. Returning here would
        # reintroduce the starvation the phase machines exist to remove: one
        # node draining would stop any other being woken.
        in_flight = [n for n in present if st.get(n, {}).get("phase")]
        for n in in_flight:
            phase = st[n]["phase"]
            if n in held:
                # An operator cordoned this node mid-operation. Finishing a
                # wake would uncordon them. Their intent wins.
                self.log("info", "operator cordoned a node mid-operation; "
                                 "abandoning the operation", node=n, phase=phase)
                st[n]["phase"] = None
                # The visit deadline goes with the phase. Left behind, the
                # NEXT visit would read a window that expired while an
                # operator held the node, and end the moment it began.
                st[n].pop("maintenance_until", None)
                continue
            if phase == "maintaining" and want > len(awake):
                # Demand turned up while the node was up for its own sake.
                # It is already booted and cordoned, which makes it the
                # cheapest capacity available anywhere -- far cheaper than
                # cold-starting a peer. Take it, exactly as a sleep is
                # abandoned when demand arrives mid-drain.
                self.log("info", "demand arrived during a maintenance window; "
                                 "putting the node into service", node=n,
                         want=want, awake=len(awake))
                st[n]["phase"] = None
                st[n].pop("maintenance_until", None)
                if not states[n].ready:
                    # Still booting. The wake machine finishes it properly,
                    # and its timeout runs from power-on either way.
                    st[n]["phase"] = "waking"
                    continue
                try:
                    self._set_cordon(n, False)
                    st[n]["awake_since"] = self.now()
                    awake.append(n)
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not uncordon", node=n, err=str(e))
                continue
            if phase == "sleeping" and want > len(awake):
                self.log("info", "demand arrived mid-sleep; keeping the node",
                         node=n, want=want, awake=len(awake))
                st[n]["phase"] = None
                try:
                    self._set_cordon(n, False)
                    awake.append(n)
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not uncordon", node=n, err=str(e))
                continue
            try:
                if phase == "waking":
                    self.wake(n)
                elif phase == "warming":
                    self.warm(n)
                elif phase == "maintaining":
                    self.maintain(n, states[n])
                else:
                    self.sleep(n, states[n])
            except Exception as e:                    # noqa: BLE001
                self.log("error", "phase step failed", node=n, phase=phase,
                         err=str(e))

        # A node powered on but cordoned is in NEITHER desired state: burning
        # power, serving nothing. It gets there when an operation is
        # interrupted. Only cordons this controller owns are touched.
        stranded = [n for n in present
                    if states[n].ready and states[n].cordoned
                    and states[n].ours and n not in in_flight]
        for n in stranded:
            if len(awake) < want:
                self.log("warn", "stranded node needed; completing the wake",
                         node=n)
                try:
                    self._set_cordon(n, False)
                    self._node(n)["awake_since"] = self.now()
                    awake.append(n)
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not uncordon", node=n, err=str(e))
            else:
                self.log("warn", "stranded node not needed; completing the "
                                 "sleep", node=n)
                try:
                    self.sleep(n, states[n])
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not sleep", node=n, err=str(e))
            return  # one corrective action per tick; re-observe next

        # Scale-up simulation: would the waiting work actually FIT here?
        # shortfall() is a sum, which assumes everything waiting is waiting on
        # capacity. Work blocked on a selector, a taint or a volume inflates it
        # and powers on a machine that cannot help.
        #
        # Skipped when saturation drove the demand: a saturated queue has
        # nothing pending to inspect -- that is the entire problem -- so
        # applying the check there would veto every saturation-driven wake.
        #
        # POSITION MATTERS, and it is deliberately after the stranded reconcile
        # rather than before it. A differential test against the controller
        # this replaces found 40 divergences in 3000 states, every one of them
        # here and every one with fits=False: guarding first meant a STRANDED
        # node -- already powered, already cordoned -- got put to sleep instead
        # of returned to service. Both are safe, but returning it matches the
        # documented "wake readily, sleep reluctantly" bias, and it means a
        # transient fit-check failure cannot power off a node that was only
        # ever mid-wake.
        if want > len(awake) and saturated == 0:
            try:
                if not self.signal.fits_node(capacity):
                    self.log("info", "demand present but none of it could run "
                                     "on a node this size; not waking",
                             shortfall=round(shortfall, 1))
                    want = len(awake)
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "fit check failed; not waking", err=str(e))
                want = len(awake)

        now = self.now()
        self.log("info", "observed", shortfall=round(shortfall, 1),
                 saturated=saturated, want=want, awake=awake)

        if want > len(awake):
            st["want_high_since"] = st.get("want_high_since") or now
            st["want_high_last"] = now
            st["want_low_since"] = None
            if now - st["want_high_since"] >= cfg.wake_sustain_s:
                for n in wakeable:
                    if n not in awake and n not in in_flight:
                        self.wake(n)
                        break
        elif want < len(awake):
            st["want_low_since"] = st.get("want_low_since") or now
            st["want_high_since"] = None
            st["want_high_last"] = None
            if now - st["want_low_since"] >= cfg.sleep_sustain_s:
                for n in reversed(wakeable):
                    if n in awake and n not in in_flight:
                        if now < st.get(n, {}).get("cooldown_until", 0):
                            self.log("info", "sleep backing off; skipping",
                                     node=n)
                            continue           # per-node backoff, not a stop
                        since = states[n].ready_since
                        if since and now - since < cfg.min_uptime_s:
                            self.log("info", "min uptime not met; holding",
                                     node=n, uptime_s=int(now - since))
                            break              # ordering is deliberate
                        self.sleep(n, states[n])
                        break
        else:
            # Demand exactly met. The SLEEP timer resets -- sleeping demands a
            # continuously idle window. The WAKE timer does not: `want`
            # flickers when a queue sits at its ceiling, and clearing on every
            # dip meant the sustain window could never be reached. Evidence is
            # held, and expires only after a full window without demand.
            st["want_low_since"] = None
            last_high = st.get("want_high_last")
            if last_high and now - last_high > cfg.wake_sustain_s:
                st["want_high_since"] = None
                st["want_high_last"] = None

        # LAST, and deliberately so. Everything above is a response to demand
        # or to an operator; a maintenance visit is neither, so it gets what is
        # left over and nothing more.
        self._maybe_maintain(present, states, awake, want)


def _default_log(level, msg, **kv):
    import json
    from datetime import datetime, timezone
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "level": level, "msg": msg}
    rec.update(kv)
    print(json.dumps(rec), flush=True)
