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
"""
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

    # -- reconcile -------------------------------------------------------
    def tick(self):
        cfg, st = self.cfg, self.st
        if cfg.mode == "off":
            self.log("info", "mode=off; no observation, no action")
            return

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

        # Scale-up simulation: would the waiting work actually FIT here?
        # shortfall() is a sum, which assumes everything waiting is waiting on
        # capacity. Work blocked on a selector, a taint or a volume inflates it
        # and powers on a machine that cannot help.
        #
        # Skipped when saturation drove the demand: a saturated queue has
        # nothing pending to inspect -- that is the entire problem -- so
        # applying the check there would veto every saturation-driven wake.
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


def _default_log(level, msg, **kv):
    import json
    from datetime import datetime, timezone
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "level": level, "msg": msg}
    rec.update(kv)
    print(json.dumps(rec), flush=True)
