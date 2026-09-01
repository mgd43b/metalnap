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
                 log=None, clock=time.time):
        self.nodes: List[str] = list(nodes)
        self.node_source = node_source
        self.power = power
        self.signal = signal
        self.drain = drain
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
            self.node_source.set_cordon(name, False)
            s["awake_since"] = self.now()
            s["sleep_attempts"] = 0
            s.pop("cooldown_until", None)
            s["phase"] = None
            self.log("info", "WAKE complete -- node uncordoned", node=name)
            return
        if self.now() - s.get("phase_since", 0) > self.cfg.wake_timeout_s:
            self.log("error", "WAKE TIMEOUT -- node did not become Ready",
                     node=name, timeout_s=self.cfg.wake_timeout_s)
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
            self.node_source.set_cordon(name, False)
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
                self.node_source.set_cordon(name, True)
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

        present = [n for n in self.nodes if states[n] is not None]
        absent = [n for n in self.nodes if states[n] is None]
        if absent:
            self.log("info", "configured nodes not present; ignoring them",
                     absent=absent)
        if not present:
            self.log("info", "no configured node exists yet")
            return

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
                continue
            if phase == "sleeping" and want > len(awake):
                self.log("info", "demand arrived mid-sleep; keeping the node",
                         node=n, want=want, awake=len(awake))
                st[n]["phase"] = None
                try:
                    self.node_source.set_cordon(n, False)
                    awake.append(n)
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not uncordon", node=n, err=str(e))
                continue
            try:
                if phase == "waking":
                    self.wake(n)
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
                    self.node_source.set_cordon(n, False)
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
