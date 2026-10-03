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
  * Mute only what you put down. A node that crashed looks exactly like one
    that was slept -- dark, unreachable -- and the more reliable the sleeps
    are, the more routine a crash looks. A dark node without our cordon, or
    one we have stopped being able to account for, is left loud.
  * Escalate once, then hand it to a human. A machine whose kernel has locked
    up reads "on" to its BMC and ignores a soft shutdown for ever; one power
    cycle is what a person would try first. A machine that needs a second
    inside the cooldown needs the person.
  * When a person asks for a node, give it to them and get out of the way.
    Maintenance mode powers the node on ONCE and then leaves it alone -- no
    sleep, no drain, no power cycle, no mute -- until the request is
    withdrawn. Someone mid-upgrade reboots, and powers off, on purpose, and
    every one of this controller's remedies for a node doing that is wrong.
  * A capacity ceiling is the one thing that may make nodes go down that
    demand wants up -- and the one thing that may, after a deadline, end work
    that is running. It is held to the same rules as everything else here:
    off unless an operator wrote one; it fails OPEN, in both directions (an
    error never engages it and releases one that is engaged); it goes through
    the ordinary sleep, so every node is announced before it is powered off,
    the deadline included; nothing running is interrupted before the deadline
    and none is while the CURRENT reading no longer asks for it; and what a
    restart must not forget -- when the shed began -- is on the node.
"""
import copy
import dataclasses
import hashlib
import math
import time
from typing import Dict, List


class Controller:
    def __init__(self, nodes, node_source, power, signal, drain, config,
                 notifier=None, warmup=None, log=None, clock=time.time,
                 ceiling=None, status=None, metrics=None):
        self.nodes: List[str] = list(nodes)
        self.node_source = node_source
        self.power = power
        self.signal = signal
        self.drain = drain
        from .types import NullCeiling, NullNotifier, NullWarmup
        #: The capacity ceiling. A null object, like the two below, so a
        #: wiring without one is exactly the controller that had none.
        self.ceiling = ceiling or NullCeiling()
        #: Where tick() reports what it concluded -- a status object that
        #: `metalnap status` reads, and the metrics. Both are REPORTING: a
        #: failure to publish is logged and never touches a decision, and
        #: neither is waited on -- the status writer is a background thread the
        #: tick only hands the latest report to. The first writes outside this
        #: process, so dry_run does not use it.
        self.status, self.metrics = status, metrics
        # Both default to no-ops so a minimal wiring still works, but the
        # defaults are named types rather than `if self.notifier:` scattered
        # through the reconcile -- a null object cannot be forgotten at one
        # call site the way a None check can.
        self.notifier = notifier or NullNotifier()
        if not all(callable(getattr(self.notifier, m, None))
                   for m in ("alert", "clear_alert")):
            # A notifier written before alert() existed still works: it keeps
            # muting and unmuting, and metalnap's own alerts go nowhere.
            self.notifier = _WithoutAlerts(self.notifier)
        self.warmup = warmup or NullWarmup()
        self.cfg = config.validate()
        self.now = clock
        self._log = log or _default_log
        #: Per-node operational state, plus a few controller-wide timers. In
        #: memory only: a restart must be survivable, so nothing here may be
        #: required for correctness.
        self.st: Dict[str, dict] = {}
        #: Busy nodes powered off at a shed's deadline, since this process
        #: began: work ended on purpose, which is what the counter is for.
        self.forced_total = 0
        # What the ceiling concluded THIS tick, for the steps below it and for
        # sleep(), which is called from outside tick() as well and must then
        # read as "no ceiling". Rebuilt from scratch every tick.
        self._limit = None      # the effective limit while it binds, else None
        self._eff = None        # the effective limit, bound or not
        self._cur = None        # the current reading, None if there is none
        self._engaged = False   # a limit is in force and below the pool
        self._ceiling_err = None
        self._states, self._pool = {}, []
        self._busy_seen = {}    # busy() answers already read this tick
        self._rep = None        # what to publish, if the tick got that far

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

    def _note(self, name, key, value):
        """Durably record a note on the node; False if there is nowhere to.

        Raises if the write fails, so a caller for whom the record is a
        precondition -- the power cycle -- can refuse to go on without it.
        dry_run writes nothing outside this process, notes included.
        """
        if self.cfg.mode != "on":
            return False
        fn = getattr(self.node_source, "note", None)
        if not callable(fn):
            return False
        fn(name, key, value)
        return True

    def _try_note(self, name, key, value):
        """_note for records that improve on memory but gate nothing."""
        try:
            return self._note(name, key, value)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "could not record a note on the node; "
                             "remembering it only until a restart",
                     node=name, note=key, err=str(e))
            return False

    def _disown(self, name):
        """Remove our ownership mark without writing the cordon."""
        if self.cfg.mode != "on":
            self.log("info", "dry_run: would clear the ownership mark",
                     node=name)
            return
        fn = getattr(self.node_source, "disown", None)
        if callable(fn):
            fn(name)
        else:
            self.node_source.set_cordon(name, False)

    def _set_trouble(self, name, reason, state=None):
        """Hand the node to a human: unmuted and alerted on from this tick.

        On the node as well as in memory. Held only in memory, a restart
        re-muted a node metalnap had given up on and resolved its alert.
        """
        s = self._node(name)
        s["trouble"] = reason
        # Compared with what the NODE says, not with memory: a write that
        # failed is retried by the reconcile until the node agrees.
        if state is None or state.trouble != reason:
            self._try_note(name, "trouble", reason)

    def _clear_trouble(self, name, state):
        """Returns the state as it now stands: the observation still carries
        the note just deleted, and reading it back would re-raise the alert
        on the very tick the node recovered."""
        self._node(name).pop("trouble", None)
        if state.trouble is None:
            return state
        self._try_note(name, "trouble", None)
        return dataclasses.replace(state, trouble=None)

    def _visit_until(self, s, state):
        """Until when a dark node may be rebooting into a visit's update.

        One visit bound from going dark in a visit this process saw, or two
        from a visit's power-on read off the node -- enough to cover the visit
        and the same again after it, whichever this process can still see.
        """
        mt = self.cfg.maintenance_timeout_s
        return max((s["dark_after_visit"] + mt) if s.get("dark_after_visit")
                   else 0,
                   (state.visited_at + 2 * mt)
                   if state is not None and state.visited_at else 0)

    def run_forever(self):
        self.log("info", "metalnap starting", nodes=self.nodes,
                 interval_s=self.cfg.interval_s)
        for w in self.cfg.warnings():
            self.log("warn", "suspicious configuration: " + w)
        while True:
            try:
                self.tick()
            except Exception as e:                    # noqa: BLE001
                self.log("error", "tick failed; no action taken", err=str(e))
            time.sleep(self.cfg.interval_s)

    # -- wake ------------------------------------------------------------
    def wake(self, name, room=True):
        """One non-blocking step of a wake. True on the step that completes it.

        `room` is whether a capacity ceiling leaves space for one more node in
        service. A wake that began before the ceiling engaged cannot be
        recalled -- a booting OS cannot be asked to shut down -- so it is let
        to arrive and then NOT put into service: see where it completes.
        """
        s = self._node(name)
        phase = s.get("phase")

        if phase != "waking":
            self.log("info", "WAKE begin", node=name)
            if self.cfg.mode != "on":
                self.log("info", "dry_run: would power on and uncordon",
                         node=name)
                return False
            # A node being woken is being brought back, so whatever a shed
            # last wrote on it is over. Left, a node woken into a ceiling that
            # has no room for it would be shed again against the OLD anchor --
            # a deadline that expired before this wake began.
            self._forget_shed(name)
            power = self.power.state(name)
            if power == "off":
                self.power.on(name)
            else:
                # Powered, yet not Ready -- or it would not be a candidate. It
                # is booting from an attempt a restart forgot, or it is wedged,
                # and nothing observable tells those apart yet. The timeout
                # does; see _wake_timed_out().
                self.log("info", "node is already powered; waiting for it to "
                                 "become Ready", node=name)
            for k in ("cycled", "power_confirmed", "off"):
                s.pop(k, None)
            #: A cold boot WE started is capacity on its way, and tick() counts
            #: it as such. One found already powered is not: it may never come.
            s["booting"] = power == "off"
            #: And one we started from OFF cannot be rebooting into an update
            #: from an earlier visit, which is what the visit guard is for.
            s["cold_start"] = power == "off"
            s["phase"] = "waking"
            s["phase_since"] = self.now()
            return False

        st = None
        try:
            st = self.node_source.state(name)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "readiness check failed; retrying next tick",
                     node=name, err=str(e))
        if st and st.ready and (st.maintenance
                                or (st.cordoned and not st.ours)):
            # Taken by a person since this tick began. The uncordon below is
            # where a wake FINISHES, and finishing it would put their node into
            # service under them -- checked on this fresh read, as the cycle
            # is, because that is the whole of the rule.
            self.log("info", "WAKE abandoned -- an operator has the node; "
                             "leaving it to them", node=name,
                     maintenance=st.maintenance)
            s["phase"] = None
            s["booting"] = False
            return False
        if st and st.ready and not room:
            # Arrived into a ceiling that has no room for it. Not uncordoned
            # and not warmed -- putting it into service would be the wake the
            # ceiling refused, and warming it spends a pull on a node about to
            # be shed. It is left Ready, cordoned and ours, in no operation,
            # which the next tick's shed takes first: nothing has been
            # promised to it that a shed has to take back.
            s["phase"] = None
            s["booting"] = False
            for k in ("cycled", "power_confirmed", "cold_start"):
                s.pop(k, None)
            self.log("info", "WAKE complete -- a capacity ceiling leaves no "
                             "room for the node; not putting it into service",
                     node=name, ceiling=self._eff)
            return False
        if st and st.ready:
            # Uncordon FIRST. Anything after this point is an optimisation, and
            # an optimisation must never be able to strand a node that is
            # already powered and Ready but not yet schedulable.
            self._set_cordon(name, False)
            s["awake_since"] = self.now()
            s["sleep_attempts"] = 0
            s.pop("cooldown_until", None)
            s["booting"] = False
            for k in ("cycled", "power_confirmed", "cold_start"):
                s.pop(k, None)
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
            return True
        if s.get("booting") and not s.get("power_confirmed"):
            # A cold boot counts as capacity on its way, and holds back every
            # other wake while it does -- so it had better be real. A BMC that
            # accepted `power on` and did nothing would otherwise stall demand
            # for a full wake timeout before anybody noticed. Checked once:
            # the chassis reports power within a second, not a boot later.
            try:
                power = self.power.state(name)
            except Exception as e:                    # noqa: BLE001
                power = None
                self.log("warn", "could not confirm the power-on; retrying "
                                 "next tick", node=name, err=str(e))
            if power == "off":
                self._give_up_wake(name, s, "the chassis did not power on",
                                   self.now() + self.cfg.wake_timeout_s,
                                   power="off", state=st)
                return False
            if power == "on":
                s["power_confirmed"] = True
        if self.now() - s.get("phase_since", 0) > self.cfg.wake_timeout_s:
            self._wake_timed_out(name, s, st)
        return False

    def _wake_timed_out(self, name, s, st):
        """A wake ran out of time. What that means depends on the chassis.

        OFF: the power-on did not take, or did not stay. The next wake simply
        tries again -- unless we SAW it come on, in which case something
        powered it back off mid-boot and that is a human's problem.

        ON: the machine is up and wedged -- the state a locked kernel sits in,
        reading "on" to its BMC for as long as anyone cares to wait. Waiting
        longer changes nothing, so it is power-cycled, once, and given one
        more wake timeout to come back. If it does not, or it may not be
        cycled, it is left for a human: unmuted, alerted on, and passed over
        by demand until it can be tried again.
        """
        cfg, now = self.cfg, self.now()
        s["booting"] = False
        if st is None:
            # The readiness read failed this step. That is not a verdict on
            # the node; ask again next tick rather than acting on no data.
            return
        # The operator check uses the FRESH read taken this step, not the one
        # tick() began with: this is where the operation finishes, and a hard
        # power cut is the last thing to get wrong on a stale read.
        if st.cordoned and not st.ours:
            self.log("info", "WAKE TIMEOUT -- an operator has cordoned the "
                             "node; leaving it to them", node=name)
            s["phase"] = None
            return
        if st.maintenance:
            # Asked for since this tick began. A node being worked on is not
            # Ready because somebody is working on it.
            self.log("info", "WAKE TIMEOUT -- an operator has asked for the "
                             "node for maintenance; leaving it to them",
                     node=name, maintenance=st.maintenance)
            s["phase"] = None
            return
        try:
            power = self.power.state(name)
        except Exception as e:                        # noqa: BLE001
            # Reasons are FIXED phrases. They are written to Alertmanager and
            # onto the node; an exception's text is not ours to publish.
            self.log("warn", "could not read power state at the wake timeout",
                     node=name, err=str(e))
            self._give_up_wake(name, s, "its BMC could not be read",
                               now + cfg.wake_timeout_s, power="unknown",
                               state=st)
            return
        if power != "on":
            if s.get("power_confirmed"):
                self._give_up_wake(name, s, "it powered on, then was off "
                                            "again before it became Ready",
                                   now + cfg.wake_timeout_s, power=power,
                                   state=st)
                return
            self.log("error", "WAKE TIMEOUT -- node did not become Ready",
                     node=name, timeout_s=cfg.wake_timeout_s, power=power)
            s["phase"] = None
            return

        reason, retry_at, alert = self._cycle_refusal(name, s, st, now)
        if reason is None:
            alert = True        # any failure from here on is a human's
            # On record BEFORE the cycle, and no record means no cycle: a
            # cycle the bound cannot see is how a bound becomes a loop.
            try:
                self._note(name, "power-cycled", now)
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "could not record the power cycle",
                         node=name, err=str(e))
                reason, retry_at = ("the power cycle could not be recorded, "
                                    "so it was not attempted",
                                    now + cfg.wake_timeout_s)
            else:
                # What the node now says, so the give-up below does not read
                # our own new record as an operator's re-arm.
                st = dataclasses.replace(st, power_cycled_at=now)
                try:
                    self.power.cycle(name)
                except Exception as e:                # noqa: BLE001
                    # Stays on record. A cycle on record that did not happen
                    # costs one retry; one that happened off the record costs
                    # the bound.
                    self.log("warn", "the power cycle failed", node=name,
                             err=str(e))
                    reason, retry_at = ("the power cycle failed",
                                        now + cfg.power_cycle_cooldown_s)
                else:
                    self.log("error", "WAKE TIMEOUT -- node is powered but "
                                      "not Ready; power-cycling it",
                             node=name, timeout_s=cfg.wake_timeout_s,
                             down_since=_iso(st.down_since)
                             if st.down_since else None)
                    s["cycled"] = True
                    s["phase_since"] = now
                    return
        self._give_up_wake(name, s, reason, retry_at, power="on", alert=alert,
                           state=st)

    def _cycle_refusal(self, name, s, st, now):
        """Why this wedged node may NOT be power-cycled now.

        (reason, retry_at, alert), or (None, None, None) if it may. Each guard
        is a way a hard power cut goes wrong, and each is checked where the
        operation finishes rather than trusted from where it began.
        """
        cfg = self.cfg
        if self._limit is not None:
            # A cycle powers the machine back on, so it is for a pool with room
            # for it. The wedged node is counted as powered -- it is drawing the
            # power the ceiling is about -- so at the limit it keeps its slot
            # and may be cycled, and only a pool already OVER the limit is
            # refused: there the node is one the ceiling may need down, and what
            # it comes back as is not for this controller to decide mid-event.
            # Counted as a wake is gated: nodes still powered, draining ones
            # included. A refused node is handed to a human like any wedged one.
            powered = self._powered(cutting=True)
            if powered > self._limit:
                return ("the pool is over its capacity ceiling (%d nodes "
                        "powered, %d allowed), and a power cycle would power on "
                        "a node it may need down" % (powered, self._limit),
                        now + cfg.wake_timeout_s, True)
        cooldown = cfg.power_cycle_cooldown_s
        if not cooldown:
            return ("power-cycle escalation is disabled "
                    "(POWER_CYCLE_COOLDOWN_S=0)", math.inf, True)
        # Optional seams. A backend that cannot cycle (Wake-on-LAN cannot), or
        # a source that cannot record one durably, degrades to "alert and
        # leave it" -- never to an unbounded cycle.
        if not callable(getattr(self.power, "cycle", None)):
            return "the power backend cannot power-cycle", math.inf, True
        if not callable(getattr(self.node_source, "note", None)):
            return ("the node source cannot record a power cycle, so none "
                    "can be bounded", math.inf, True)
        # The record on the node is the ONLY bound, deliberately: an operator
        # re-arms it by deleting the annotation, and a copy held in memory
        # would quietly overrule them.
        last = st.power_cycled_at
        if s.get("cycled"):
            # No record now means an operator deleted ours mid-wait: re-armed.
            return ("a power cycle during this wake did not bring it back",
                    last + cooldown if last is not None
                    else now + cfg.wake_timeout_s, True)
        if last is not None and now - last < cooldown:
            return ("it was already power-cycled at %s" % _iso(last),
                    last + cooldown, True)
        visit_until = self._visit_until(s, st)
        if now < visit_until and not s.get("cold_start"):
            # Dark since a maintenance visit, and maybe rebooting into
            # firmware it just installed -- see maintain(). Not a failure yet,
            # so no alert; demand is simply pointed elsewhere until the visit
            # bound has passed. A wake that found the chassis OFF is exempt: a
            # machine that was powered down is not mid-update.
            return ("it went dark soon after a maintenance visit and may be "
                    "mid-update", visit_until, False)
        # NEVER interrupt running work -- not by power either. NotReady is not
        # dead: a kubelet cut off from the API server leaves its jobs running,
        # and busy() reads the work queue's own records, not the kubelet. If
        # it cannot tell, that reads as busy. The cost is a node whose stale
        # records keep it from being cycled, and that is left to a human.
        try:
            busy = self.drain.busy(name)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "busy check failed at the wake timeout",
                     node=name, err=str(e))
            return ("could not tell whether it is running work, which reads "
                    "as busy", now + cfg.wake_timeout_s, True)
        if busy:
            return ("the cluster still lists running work on it: %s"
                    % ", ".join(busy[:5]), now + cfg.wake_timeout_s, True)
        return None, None, None

    def _give_up_wake(self, name, s, reason, retry_at, power, alert=True,
                      state=None):
        """Stop, say why, and point demand at other nodes until retry_at."""
        now = self.now()
        s["phase"] = None
        s["booting"] = False
        s["wake_backoff_until"] = retry_at
        # Remember which record the backoff is waiting out, so that deleting
        # the annotation -- the documented re-arm -- lifts it straight away.
        if state is not None:
            s["backoff_record"] = state.power_cycled_at
        else:
            s.pop("backoff_record", None)
        if alert:
            self._set_trouble(name, "not Ready after a wake: " + reason, state)
        self.log("error", "WAKE FAILED -- node did not become Ready; %s"
                          % ("leaving it for a human" if alert
                             else "trying other nodes first"),
                 node=name, reason=reason, power=power,
                 retry_in_s=None if retry_at == math.inf
                 else int(retry_at - now))

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
        """One non-blocking step of a sleep.

        A node a capacity ceiling is holding down -- one with a shed anchor --
        takes this same path, and every rule below still holds for it: it is
        cordoned as ours, its idle units are released after a fresh
        holds_work(), it is announced BEFORE power is cut -- the deadline
        included -- and a shutdown it ignores is reported, never forced.

        What a shed must not be allowed to do is leave. The drain timeout and
        the attempt bound both end in _abandon_sleep(), which uncordons: right
        for a drain that is not working, wrong for one whose reason is a
        ceiling, since a shed that returns its node to service defeats the
        ceiling. So a shed has neither. It has a deadline of its own, from the
        anchor on the node, after which the wait on running work is skipped.
        """
        s = self._node(name)
        anchor = self._shed_anchor(name, state)
        shed = anchor is not None

        if s.get("phase") != "sleeping":
            self.log("info", "SLEEP begin", node=name)
            if self.cfg.mode != "on":
                self.log("info", "dry_run: would cordon, drain, power off",
                         node=name)
                return
            attempts = s.get("sleep_attempts", 0) + 1
            s["sleep_attempts"] = attempts
            if attempts > self.cfg.max_sleep_attempts and not shed:
                self._abandon_sleep(
                    name, s, "sleep did not complete after repeated attempts; "
                             "returning the node to service and backing off")
                return
            # Only cordon if it is not ALREADY cordoned by us. Re-stamping
            # rewrites the ownership timestamp, which is the durable anchor for
            # the drain deadline -- so a restarted sleep would reset the very
            # clock that is supposed to survive a restart, and a node with hung
            # work could be held indefinitely, one restart at a time.
            if not self._ours(state):
                self._set_cordon(name, True)
            if state is not None and state.shutdown_at is not None:
                # Left by a shutdown this process never saw finish. This sleep
                # starts its own; a stale one would read as already underway.
                self._try_note(name, "shutdown", None)
            s["phase"] = "sleeping"
            s["phase_since"] = self.now()
            return

        # Past a shed's deadline -- and only while the current reading still
        # asks for it -- the node is no longer waited for. Everything that
        # follows is the ordinary sleep with the waits taken out.
        forced = shed and self._shed_overdue(anchor)
        known = True
        try:
            busy = self.drain.busy(name)
        except Exception as e:                        # noqa: BLE001
            if not forced:
                # Could not tell => assume busy => do nothing.
                self.log("warn", "busy check failed; retrying next tick",
                         node=name, err=str(e))
                return
            # Unreadable reads as busy until the deadline, and the deadline
            # has passed: it is the answer. Said, so that what ended is not a
            # mystery.
            busy, known = [], False
            self.log("warn", "busy check failed past the shed deadline; "
                             "going on without it", node=name, err=str(e))
        if busy and not forced:
            if shed:
                deadline = self.cfg.ceiling_drain_deadline_s
                if deadline and self.now() - anchor >= deadline:
                    # Past it, and spared: the reading in hand no longer asks
                    # for the node to go, so the hold is keeping it down and
                    # the work is left alone. Said, since the next question is
                    # "why was it not forced".
                    self.log("info", "waiting on running work; the shed "
                                     "deadline has passed, but the current "
                                     "reading no longer asks for this node to "
                                     "go", node=name, busy=len(busy),
                             units=busy[:5], reading=self._cur)
                else:
                    self.log("info", "waiting on running work; the shed "
                                     "deadline has not passed", node=name,
                             busy=len(busy), units=busy[:5],
                             deadline_in_s=(max(0, int(anchor + deadline
                                                       - self.now()))
                                            if deadline else None))
                return
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
        skipped = []
        try:
            idle = self.drain.idle(name)
        except Exception as e:                        # noqa: BLE001
            if not forced:
                self.log("warn", "idle check failed; retrying next tick",
                         node=name, err=str(e))
                return
            idle = []
        if idle:
            self.log("info", "releasing idle units blocking the drain",
                     node=name, count=len(idle), units=idle[:5])
            for unit in idle:
                # Re-read each unit immediately before releasing it. The
                # listing above is already stale; work can land in that window.
                # Anything uncertain leaves the unit alone and aborts the tick
                # -- unless the deadline has passed, when it leaves the unit
                # alone and goes on: the unit is not released, but it no longer
                # holds the node.
                try:
                    if self.drain.holds_work(unit):
                        if forced:
                            skipped.append(unit)
                            continue
                        self.log("info", "unit was given work during the "
                                         "drain; waiting", node=name,
                                 unit=unit)
                        return
                except Exception as e:                # noqa: BLE001
                    if forced:
                        skipped.append(unit)
                        continue
                    self.log("warn", "could not re-check unit before "
                                     "releasing it; retrying next tick",
                             node=name, unit=unit, err=str(e))
                    return
                try:
                    self.drain.release(unit)
                except Exception as e:                # noqa: BLE001
                    if forced:
                        self.log("warn", "could not release idle unit; "
                                         "going on", node=name, unit=unit,
                                 err=str(e))
                        continue
                    self.log("warn", "could not release idle unit; retrying "
                                     "next tick", node=name, unit=unit,
                             err=str(e))
                    return
            if not forced:
                # Release is asynchronous. Re-observe next tick rather than
                # racing the teardown.
                return

        if not forced:
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
            # fire and nobody would know it was us. The deadline does not
            # change that: a shed past it stays powered until it can be
            # announced, and says so at error on every tick it does.
            self.log("error" if shed else "warn",
                     "could not announce the shutdown; not powering off this "
                     "tick", node=name, err=str(e))
            return
        # On the node before the request, so a restart in the minute the
        # kubelet goes on reporting Ready resumes waiting instead of reading
        # the node as stranded and asking a second time.
        self._try_note(name, "shutdown", self.now())
        self.power.soft_off(name)
        ended = list(busy) + skipped
        if forced and (ended or not known):
            # Work ended on purpose: the one place this controller does that.
            # Logged at error with what it ended -- once per shed, not once per
            # retry -- and NOT counted yet: a soft-off is a request, and a node
            # that ignores it has not been interrupted. The counter moves when
            # the power-off is confirmed (confirm_off), once, whatever it took.
            s["forced_units"] = ended[:5]
            if s.get("forced_logged") != anchor:
                s["forced_logged"] = anchor
                self.log("error", "SHED FORCED -- the drain deadline passed "
                                  "with work still running; the node is shut "
                                  "down the ordinary way and that work ends",
                         node=name, units=ended[:5],
                         deadline_s=self.cfg.ceiling_drain_deadline_s)
        # Not done yet: a soft shutdown is a REQUEST. The node stays in flight
        # until the chassis confirms it, which does two jobs. The kubelet keeps
        # reporting Ready for most of a minute after the OS starts going down,
        # and a node that is Ready, cordoned and ours with no operation in
        # flight reads as STRANDED -- so the repair used to "complete the
        # sleep" of a machine already shutting down, a second soft-off into a
        # shutdown in progress. And an OS that never finishes going down --
        # wedged on an unmount, or ignoring the request -- is caught rather
        # than muted as asleep for as long as nobody wants it.
        s["phase"] = "powering_off"
        s["phase_since"] = self.now()
        s["awake_since"] = None
        self.log("info", "SLEEP shutdown requested -- waiting for power-off",
                 node=name)

    def _shed_overdue(self, anchor):
        """Has a shed's deadline passed, and does the reading still want it?

        The CURRENT reading, not the effective limit. The limit is held -- the
        minimum over the release hold -- so that nodes stay down; it is not a
        reason to end work. A one-sample spike followed by a fifteen-minute
        hold would otherwise force a busy node off for a ceiling that went
        away at the next sample. So the deadline is enforced only while the
        reading in hand allows fewer nodes powered than there are. 0 is never.
        """
        deadline = self.cfg.ceiling_drain_deadline_s
        if not deadline or self._cur is None:
            return False
        if self.now() - anchor < deadline:
            return False
        return self._cur < self._powered(cutting=False)

    def confirm_off(self, name, state=None):
        """One non-blocking step of waiting for a soft shutdown to finish.

        A shutdown that outlasts its bound is reported, never forced. The
        choice is the same one maintain() makes about a node that may be
        mid-update: a hard cut is the reading that is expensive to get wrong,
        and the machine is going nowhere while a human looks. If demand wants
        it, a wake finds it powered and wedged and escalates exactly as it
        would for a node that crashed in service.
        """
        s = self._node(name)
        try:
            power = self.power.state(name)
        except Exception as e:                        # noqa: BLE001
            power = None
            self.log("warn", "could not read power state while waiting for "
                             "the shutdown", node=name, err=str(e))
        timed_out = (self.now() - s.get("phase_since", 0)
                     > self.cfg.shutdown_timeout_s)
        # Off AND NotReady. The BMC can read off while the kubelet is still
        # reported Ready, and ending the phase then hands a node that reads
        # Ready, cordoned and ours to the stranded repair -- which "completes
        # the sleep" with a second soft-off into a chassis already off.
        if power == "off" and (timed_out or not (state and state.ready)):
            s["phase"] = None
            s["sleep_attempts"] = 0
            s["off"] = True
            s.pop("shutdown_failed", None)
            self._try_note(name, "shutdown", None)
            if s.pop("forced_units", None) is not None:
                # A shed past its deadline that ended work, now confirmed: the
                # one count per shed, and only for one that actually went down.
                s.pop("forced_logged", None)
                self.forced_total += 1
            self.log("info", "SLEEP complete -- powered off", node=name)
            return
        if not timed_out:
            return
        s["phase"] = None
        self._try_note(name, "shutdown", None)
        if power == "on":
            reason = _NOT_OFF % self.cfg.shutdown_timeout_s
            if state is not None and not state.ready:
                # Wedged partway down: dark, powered, and going nowhere. That
                # is a node in trouble like any other dark one.
                self._set_trouble(name, reason, state)
            else:
                # Ignoring the request, and still Ready. Kept apart from
                # `trouble`, which Ready clears, and raised only while Ready:
                # once it reads NotReady it is going down after all.
                #
                # sleep_attempts is deliberately NOT reset. The node reads as
                # stranded and is slept again, and it is the attempt bound,
                # counting across those, that finally returns it to service
                # instead of asking forever.
                s["shutdown_failed"] = reason
            self.log("error", "SLEEP FAILED -- node did not power off; "
                              "leaving it powered rather than cutting power",
                     node=name, timeout_s=self.cfg.shutdown_timeout_s)
        else:
            self.log("error", "could not confirm the node powered off",
                     node=name, timeout_s=self.cfg.shutdown_timeout_s)

    # -- scheduled maintenance visits ------------------------------------
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
        return spread * _hash_fraction(
            hashlib.sha256(name.encode("utf-8")).digest())

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
            power = self.power.state(name)
            if power == "off":
                # On the node BEFORE the power-on: the one thing a restart
                # must not forget about a visit is that it may have started an
                # update. Only a visit that powers the node on can have -- one
                # that finds it already powered and dark is visiting a node
                # that is wedged, and stamping it would renew the mid-update
                # grace on every visit, so it was never cycled or alerted on.
                self._try_note(name, "visited", self.now())
                self.power.on(name)
            # Kept for the case where demand takes the visit over mid-boot:
            # the wake it becomes is then a cold boot we started, and counts.
            s["booting"] = power == "off"
            #: Whether THIS visit could have started an update. One that found
            #: the node already powered and dark is visiting a wedged node, and
            #: must not renew the mid-update grace -- it would keep the node
            #: from ever being cycled or alerted on, one visit at a time.
            s["visit_powered_on"] = power == "off"
            s.pop("off", None)
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
            # It is not abandoned silently. It is NOT muted -- a node that
            # outlasted a whole visit NotReady needs a human to look, which is
            # exactly what muting would stop -- it is alerted on, this logs at
            # error, and the ordinary stranded repair finishes the job the
            # moment it comes back Ready.
            self.log("error", "MAINTENANCE TIMEOUT -- node is still not Ready; "
                              "leaving it powered rather than cutting power to "
                              "a node that may be mid-update", node=name,
                     timeout_s=cfg.maintenance_timeout_s)
            self._release_visit(name)
            if s.get("visit_powered_on"):
                s.setdefault("dark_after_visit", self.now())
            self._set_trouble(name, "still not Ready %ds into a maintenance "
                                    "visit" % cfg.maintenance_timeout_s, state)
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
        """Nodes asleep under our cordon, not handed to a human or backed
        off, not asked for by an operator, and due -- longest dark first."""
        due = []
        for n in present:
            state = states[n]
            if state.ready:
                continue          # already up, and already getting its updates
            if state.maintenance:
                continue          # an operator has it; see maintenance mode
            if not self._ours(state):
                # A dark node WITHOUT our cordon is not one we put to sleep.
                # Somebody pulled it for a disk swap or a firmware flash, and
                # powering it on underneath them is the single worst thing this
                # feature could do. An operator's cordon outranks the
                # controller; so does an operator's screwdriver.
                continue
            s = self.st.get(n) or {}
            if (s.get("trouble") or state.trouble
                    or self.now() < s.get("wake_backoff_until", 0)):
                # Handed to a human, or given up on for now. A visit would
                # power it on under the person now holding it.
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

    # -- operator maintenance mode ---------------------------------------
    def _maintenance_requests(self, present, states):
        """The nodes an operator has asked for, tidying up after any given back.

        Read off the node every tick, so there is nothing here for a restart
        to lose: the request is the operator's annotation, and the one thing
        metalnap adds to it -- that it has been taken up -- is on the node too.
        """
        st = self.st
        maint = [n for n in present if states[n].maintenance]
        for n in present:
            state = states[n]
            if state.maintenance:
                # Whatever metalnap last concluded about this machine, a person
                # has had it since. Its power is checked afresh when it is given
                # back, rather than muted on an "off" from before somebody
                # switched it on; and a wake metalnap gave up on is forgotten,
                # or a node fixed and given back dark is passed over by demand
                # for a day with nothing saying why. The power-cycle bound
                # lives on the node, so forgetting this cannot make a loop.
                for k in ("off", "dark_on_since", "wake_backoff_until",
                          "backoff_record"):
                    self._node(n).pop(k, None)
                continue
            for k in ("maintenance_dry_run", "maintenance_power_failed_at"):
                (st.get(n) or {}).pop(k, None)
            if state.maintenance_started_at is not None:
                # Given back. The record is what makes the power-on happen once
                # per request, so it goes with the request: left behind, the
                # NEXT request would find it and never power the node on.
                self._try_note(n, "maintenance-started", None)
        last = st.get("_maint_last") or []
        if maint != last:
            if maint:
                self.log("info", "nodes asked for by an operator for "
                                 "maintenance; powered on once, then left "
                                 "alone until the request is withdrawn",
                         maintenance={n: states[n].maintenance
                                      for n in maint})
            released = [n for n in last if n not in maint]
            if released:
                self.log("info", "maintenance request withdrawn; the nodes "
                                 "are metalnap's again", released=released)
            st["_maint_last"] = maint
        return maint

    def _take_up_maintenance(self, maint, states, settling):
        """Power on, ONCE per request, each node an operator has asked for.

        Once, not whenever it is dark. A person mid-maintenance powers the
        machine off on purpose -- to reseat a DIMM, or flash firmware that
        wants a cold start -- and powering it back on underneath them is the
        screwdriver problem the visits are careful to avoid, arriving by the
        front door. So the power-on goes on record on the node BEFORE it is
        made, and a request carrying that record is never powered on again.
        Removing the record is how an operator asks for another.

        One maintenance power-on per tick, for the reason visits are
        serialised: someone asking for the whole fleet at once is not asking
        for a rack to power on in unison. A node that is already up is only
        recorded, and does not wait its turn. True if it powered one on.

        A power-on that keeps failing is retried for a wake timeout, then left
        on record and said once -- bounded, like every retry here. The node is
        the operator's, so it is theirs to power at the BMC, or to ask for
        again with a fresh request.

        Not while a shutdown of ours is still settling, either, nor on the
        tick one ends. It cannot be recalled, so it is seen through to OFF and
        the node powered on after it -- and not on the observation this tick
        began with, which predates it: that still reads Ready, and the node
        would be recorded as taken up and left dark. Nothing else waits: a
        node up and warming is up, and waiting for its warmup left a window in
        which an operator's power-off was undone the moment it ended.
        """
        powered, now = False, self.now()
        for n in maint:
            state, s = states[n], self._node(n)
            if (state.maintenance_started_at is not None
                    or s.get("phase") == "powering_off" or n in settling):
                continue
            if self.cfg.mode != "on":
                # Remembered in memory only, so the shadow says it once per
                # request rather than every tick for as long as it stands.
                if not s.get("maintenance_dry_run"):
                    self.log("info", "dry_run: would take up the maintenance "
                                     "request, powering the node on if it is "
                                     "off", node=n,
                             maintenance=state.maintenance)
                    s["maintenance_dry_run"] = True
                continue
            power = "on" if state.ready else None
            if power is None:
                if powered:
                    continue          # its turn is next tick
                try:
                    power = self.power.state(n)
                except Exception as e:                # noqa: BLE001
                    self.log("warn", "could not read power state for a "
                                     "maintenance request; retrying next "
                                     "tick", node=n, err=str(e))
                    continue
            # On record BEFORE the power-on, and no record means no power-on:
            # one this process cannot see is one it makes again after the
            # operator has switched the machine off.
            try:
                if not self._note(n, "maintenance-started", self.now()):
                    raise RuntimeError("the node source cannot record it")
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "could not record the maintenance request, "
                                 "so the node was not powered on; retrying "
                                 "next tick", node=n, err=str(e))
                continue
            if power != "off":
                self.log("info", "MAINTENANCE MODE -- node is already "
                                 "powered; leaving it alone until the request "
                                 "is withdrawn", node=n,
                         maintenance=state.maintenance, ready=state.ready)
                continue
            try:
                self.power.on(n)
            except Exception as e:                    # noqa: BLE001
                first = s.setdefault("maintenance_power_failed_at", now)
                if now - first >= self.cfg.wake_timeout_s:
                    # Left ON the record, which is what stops the retries --
                    # across a restart, too.
                    s.pop("maintenance_power_failed_at", None)
                    self.log("error", "MAINTENANCE MODE -- could not power "
                                      "the node on for a whole wake timeout; "
                                      "giving up. Power it on at the BMC, or "
                                      "withdraw the request and ask again",
                             node=n, err=str(e))
                    continue
                # Off the record again: a request that did not power the node
                # on has not been taken up, and the next tick tries again.
                self.log("error", "could not power the node on for "
                                  "maintenance; retrying next tick", node=n,
                         err=str(e))
                self._try_note(n, "maintenance-started", None)
                continue
            s.pop("maintenance_power_failed_at", None)
            powered = True
            self.log("info", "MAINTENANCE MODE -- powering on for an operator; "
                             "leaving it alone until the request is withdrawn",
                     node=n, maintenance=state.maintenance)
        return powered

    # -- sizing ----------------------------------------------------------
    def _size(self, shortfall, capacities):
        """(node capacity, whole nodes the backlog needs).

        Per resource when the signal and the nodes name them -- {"memory": GiB,
        "cpu": cores} -- and then the node count is the most any one resource
        needs. Sized on memory alone, a pool whose work runs out of CPU first
        woke about half the nodes an e2e backlog needed. A bare number on both
        sides is one resource, as before.
        """
        cfg = self.cfg
        dicts = [isinstance(c, dict) for c in capacities]
        if not isinstance(shortfall, dict) and not any(dicts):
            cap = min([c for c in capacities if c > 0]
                      or [cfg.default_capacity])
            return cap, math.ceil(max(shortfall, 0.0) / cap)
        if not isinstance(shortfall, dict) or not all(dicts):
            # Refused rather than guessed at: dividing CPU by GiB sizes the
            # pool on nonsense, and the tick fails toward "change nothing".
            raise TypeError("the demand signal and the node source disagree: "
                            "one sizes per resource and the other does not")
        cap = {}
        for r in set(shortfall).union(*capacities):
            have = [c.get(r, 0) for c in capacities if c.get(r, 0) > 0]
            if have:
                cap[r] = min(have)
            elif r == "memory":
                cap[r] = cfg.default_capacity
        unsized = sorted(r for r, v in shortfall.items()
                         if v > 0 and r not in cap)
        if unsized != self.st.get("_unsized_last"):
            if unsized:
                self.log("warn", "no node reports capacity for a resource the "
                                 "demand signal asks for; not sizing on it",
                         resources=unsized)
            self.st["_unsized_last"] = unsized
        return cap, max((math.ceil(v / cap[r]) for r, v in shortfall.items()
                         if v > 0 and r in cap), default=0)

    def _in_use(self, awake):
        """Awake nodes carrying work, by the work queue's own records.

        One that cannot be read counts as in use: if a check cannot tell
        whether a node is busy, that reads as busy.
        """
        out = []
        for n in awake:
            # Once a tick: the ceiling's shed order asks about the same nodes
            # the demand decisions do, and each ask is a listing against the
            # work queue's API -- on the path that runs during an emergency.
            if n not in self._busy_seen:
                try:
                    self._busy_seen[n] = bool(self.drain.busy(n))
                except Exception as e:                # noqa: BLE001
                    self.log("warn", "busy check failed; counting the node as "
                                     "in use", node=n, err=str(e))
                    self._busy_seen[n] = True
            if self._busy_seen[n]:
                out.append(n)
        return out

    # -- what we did, and what we know ------------------------------------
    @staticmethod
    def _ours(state):
        """Carrying OUR cordon. Every "did we do this?" question starts here.

        One definition. There were three inline copies, and the place that
        most needed the question -- the notification reconcile -- did not ask
        it at all, and muted every dark node, crashed ones included.
        """
        return bool(state is not None and state.cordoned and state.ours)

    def _asleep(self, name, state):
        """Put down by us and still down the way we left it: the one kind of
        dark node whose alerts are noise rather than news."""
        if not self._ours(state) or state.maintenance:
            return False        # crashed, or an operator's: either way, loud
        phase = (self.st.get(name) or {}).get("phase")
        if not state.ready:
            # Dark mid-drain means it went down before we took it down: the
            # sleep announces itself immediately before cutting power, and
            # leaves this phase in the same step.
            return phase != "sleeping"
        # Ready, but already asked to shut down. The kubelet outlives the OS
        # going down by most of a minute, and unmuting for that minute lets
        # through exactly the alerts the mute was for.
        return phase == "powering_off"

    def _dark_trouble(self, name, state):
        """Why this dark node needs a human, or None. The caller unmutes a node
        that has one: its own alerts are exactly what should now arrive."""
        return (self.st.get(name) or {}).get("trouble") or state.trouble

    def _trouble(self, name, state):
        """Why this node needs a human, or None.

        Recorded where metalnap gave up, in memory and on the node, and
        cleared when the node is seen Ready or an operator takes it. A node
        that ignored a shutdown is the exception: that trouble is raised only
        WHILE it is Ready, and cleared by a shutdown that is confirmed.
        """
        s = self.st.get(name) or {}
        return (s.get("trouble") or state.trouble
                or (s.get("shutdown_failed") if state.ready else None))

    def _check_dark(self, name, state):
        """Confirm that a node we believe asleep is actually OFF. Returns the
        state as it now stands, for the same reason _clear_trouble does.

        Everything else here assumes a dark node carrying our cordon is one we
        powered off. It is, until it is not: an OS wedged partway through
        shutting down, or a wake a restart forgot, leaves a machine powered,
        dark and ours -- muted as asleep for as long as nobody wants it, which
        is the incident this controller's mute rule exists to prevent, arriving
        by the side door. So check: once per sleep (confirm_off() does it) and
        once after every restart. A powered one gets a wake timeout's grace,
        since it may simply be booting, and then is called what it is.

        What this cannot see: a node confirmed off that something ELSE later
        powers on -- a BMC restoring power after an outage -- and that then
        wedges during boot. One that boots cleanly is caught by the stranded
        repair; one that wedges stays muted until demand or a visit wants it.
        """
        s = self._node(name)
        if s.get("phase"):
            # An operation accounts for it, and times itself out. The grace
            # below starts afresh if the operation ends with the node dark.
            s.pop("dark_on_since", None)
            return state
        trouble = s.get("trouble") or state.trouble
        if trouble and trouble.startswith(_RECHECKABLE):
            # Trouble this function -- or a shutdown that outlasted its bound
            # -- raised from a power reading. A later reading can take it
            # back: a BMC that answers again, a slow shutdown that finished.
            try:
                power = self.power.state(name)
            except Exception:                         # noqa: BLE001
                return state
            if power == "off":
                self.log("info", "node carrying our cordon reads off after "
                                 "all; muting it again", node=name,
                         was=trouble)
                state = self._clear_trouble(name, state)
                s["off"] = True
                s.pop("dark_on_since", None)
            return state
        if s.get("off") or trouble:
            return state
        if self.now() < self._visit_until(s, state):
            return state  # may be rebooting into an update; see maintain()
        since = s.setdefault("dark_on_since", self.now())
        try:
            power = self.power.state(name)
        except Exception as e:                        # noqa: BLE001
            # Bounded like everything else. A BMC that cannot be read for a
            # whole wake timeout leaves a dark node nobody can vouch for, and
            # muting that is the failure this function exists to prevent.
            if self.now() - since > self.cfg.wake_timeout_s:
                self._set_trouble(name, _BMC_UNREADABLE, state)
            else:
                self.log("warn", "could not read power state of a sleeping "
                                 "node", node=name, err=str(e))
            return state
        if power == "off":
            s["off"] = True
            s.pop("dark_on_since", None)
            return state
        if self.now() - since > self.cfg.wake_timeout_s:
            self._set_trouble(name, _POWERED_UNEXPLAINED, state)
            self.log("error", "node carrying our cordon is powered but not "
                              "Ready, and no operation of ours explains it",
                     node=name, powered_for_s=int(self.now() - since))
        return state

    # -- capacity ceiling ------------------------------------------------
    def _shed_anchor(self, name, state):
        """When a capacity ceiling began holding this node down, or None.

        The note on the node first -- it is the one a restart cannot take --
        and this process's own copy for a node whose note could not be
        written. Either one is "held down by a ceiling".
        """
        if state is not None and state.shed_at is not None:
            return state.shed_at
        return (self.st.get(name) or {}).get("shed_at")

    def _forget_shed(self, name, state=None):
        """Drop a node's shed anchor, on the node and in memory. Returns the
        state as it now stands, for the reason _clear_trouble does."""
        s = self._node(name)
        for k in ("shed_at", "forced_units", "forced_logged"):
            s.pop(k, None)
        state = state if state is not None else self._states.get(name)
        if state is None or state.shed_at is None:
            return state
        self._try_note(name, "shed", None)
        return dataclasses.replace(state, shed_at=None)

    def _powered(self, cutting):
        """Nodes the ceiling counts that are still drawing power, as far as
        can be told without asking a BMC: Ready, or on their way up -- waking,
        or up for a visit.

        `cutting` is whether a node whose shutdown has been requested counts
        while it still reads Ready. It does when the question is the budget --
        a wake is not made beside a node that is still on -- and not when it is
        whether the ceiling still wants somebody gone, which a node on its way
        out has already answered. Worked out from the phases as they stand,
        not as the tick began, so a node that went down a moment ago in this
        same tick is not counted.
        """
        n = 0
        for name in self._pool:
            phase = (self.st.get(name) or {}).get("phase")
            if phase == "powering_off":
                n += bool(cutting and self._states[name].ready)
            elif (self._states[name].ready
                    or phase in ("waking", "sleeping", "maintaining")):
                # A drain counts Ready or not: a node that went NotReady
                # mid-drain -- cut off from the cluster, its work carrying
                # on -- is as powered as it was a moment ago.
                n += 1
        return n

    def _read_ceiling(self):
        """(reading, ok): what the signal says now, or that it cannot say.

        None is a reading -- no series, no ceiling. An exception is not one,
        and neither is a value that is not a number of nodes: ok is False and
        it is read as no ceiling. Never as 0, which is an order to power the
        pool off.
        """
        self._ceiling_err = None
        try:
            r = self.ceiling.limit()
        except Exception as e:                        # noqa: BLE001
            self._ceiling_err = str(e) or type(e).__name__
            return None, False
        if r is None:
            return None, True
        if (isinstance(r, bool) or not isinstance(r, (int, float))
                or r != r or r < 0 or r == math.inf):
            self._ceiling_err = "not a number of nodes: %r" % (r,)
            return None, False
        return int(r), True

    def _ceiling_section(self, present, states, held, maint, awake, wakeable):
        """Read the capacity ceiling, and act on it. Returns the nodes whose
        shed began this tick.

        Called BEFORE the demand signal is read. That read is not wrapped --
        an exception from it aborts the tick, which is right for a signal that
        is down -- and a ceiling read after it would stop shedding whenever the
        demand path is failing: the two fail independently, one off the
        Kubernetes API and the other off Prometheus. It mutates `awake` and
        `states` for what it began, so that nothing after it sees a node it
        has just cordoned as still in service.

        What it concludes is left on self for the steps below it: `_limit`,
        the limit while it binds, is what clamps demand; `_engaged` is what
        holds visits and power cycles back.
        """
        cfg, st = self.cfg, self.st
        now = self.now()
        self._states, self._pool = states, wakeable
        self._limit = self._eff = self._cur = None
        self._engaged = False

        reading, ok = self._read_ceiling()
        # The effective limit is the minimum of the readings in the trailing
        # hold: tightening is immediate, loosening waits a whole window of
        # looser readings, and `0, none, 0, none` costs no wake and no sleep.
        # An UNAVAILABLE reading is not a looser one -- it is no reading -- so
        # it empties the window instead of being averaged into it: fail open,
        # literally, and at once. (The window lives in memory. A restart
        # forgets the hold, which is the same direction.)
        window = st.setdefault("_ceiling_window", [])
        if not ok:
            window.clear()
        else:
            if reading is not None:
                window.append((now, reading))
            window[:] = [(t, v) for t, v in window
                         if t == now or now - t < cfg.ceiling_release_hold_s]
        # Clamped to the pool it counts -- not to every node there is: nodes an
        # operator holds are not its to limit, so a reading of 99 over three
        # governed nodes and two held ones is a limit of three, not four.
        eff = min((v for _t, v in window), default=None)
        if eff is not None:
            eff = min(eff, len(wakeable))
        self._eff = eff
        self._cur = None if reading is None else min(reading, len(wakeable))
        # It binds only if it is below what it counts. A ceiling at or above
        # the pool changes nothing, and is treated as none, so that a standing
        # budget that is not binding does not hold visits or cycles back.
        engaged = eff is not None and eff < len(wakeable)
        self._engaged = engaged
        self._limit = eff if engaged else None
        exempt = [n for n in present if n in held or n in maint]

        # The note on a node is true only while the ceiling holds it down. Any
        # other time -- released, brought back by a wake or a rescue, taken by
        # an operator -- it goes, and a node that is shed again gets a fresh
        # anchor. One note per concern is the whole reason for a note.
        for n in present:
            state = states[n]
            if (engaged and n not in exempt and not state.cordoned
                    and (st.get(n) or {}).get("phase") == "sleeping"):
                # A person uncordoned a node that was draining -- one the
                # ceiling was holding down (it carries a shed note) or an
                # ordinary drain it had not yet picked. The ordinary path backs
                # the sleep off; the ceiling does not fight them either. SPARED
                # means: counted as powered, but never picked to be shed, until
                # `sleep_cooldown_s` has passed -- the same interval an
                # ordinary sleep is backed off for. It is not stamped as a shed
                # on the way.
                if (st.get(n) or {}).get("ceiling_spared_until", 0) <= now:
                    self.log("info", "node was put back into service mid-drain "
                                     "by an operator; the capacity ceiling "
                                     "leaves it alone", node=n,
                             for_s=cfg.sleep_cooldown_s)
                self._node(n)["ceiling_spared_until"] = (
                    now + cfg.sleep_cooldown_s)
            if self._shed_anchor(n, state) is None:
                continue
            if engaged and state.cordoned and state.ours and n not in exempt:
                if state.shed_at is None:
                    # Held down on this process's own say-so: the write that
                    # should have put it on the node failed. Retried until the
                    # node agrees, as the trouble note is -- or a restart in
                    # the middle of the drain finds no anchor, and hands a busy
                    # node a fresh deadline.
                    mine = (st.get(n) or {}).get("shed_at")
                    if self._try_note(n, "shed", mine):
                        states[n] = dataclasses.replace(state, shed_at=mine)
                continue
            states[n] = self._forget_shed(n, state)

        begun = set()
        picks = []
        if not engaged:
            st["_shed_dry_last"] = None
        else:
            L = eff

            def phase(n):
                return (st.get(n) or {}).get("phase")

            # How many nodes must go, and which. Every node still drawing
            # power that is not already on its way out counts, and the ceiling
            # allows L of them:
            #   held   already held down by it -- a note on the node -- and
            #          still up: draining, or Ready and in no operation after
            #          a restart;
            #   drains an ordinary drain under way, which a ceiling that needs
            #          the node gone turns into a shed;
            #   up     in service, or Ready in no operation of the kind that is
            #          going down -- stranded, visiting, warming, a wake that
            #          has just arrived. A node still booting is none of these:
            #          it cannot be asked to shut down, and is shed the tick it
            #          is Ready.
            held_down, drains, up = [], [], []
            for n in wakeable:
                ph = phase(n)
                if ph == "powering_off":
                    continue            # its power is already being cut
                mine = self._shed_anchor(n, states[n]) is not None
                if ph == "sleeping":
                    (held_down if mine else drains).append(n)
                elif states[n].ready:
                    (held_down if mine else up).append(n)
            required = len(held_down) + len(drains) + len(up) - L
            # Anyone an operator put back in service, whichever list it landed
            # in this tick: it is counted, and it is not picked.
            spared = {n for n in wakeable if (st.get(n) or {}).get(
                "ceiling_spared_until", 0) > now}

            def order(nodes):
                """The order they go in: nodes carrying no work first, then
                nodes carrying work -- the mirror of the wake order, with a node
                that has a job on it the last to go. Within each, the ones that
                cost nothing to lose before the ones that do: a drain already
                on its way out, then a node powered for nobody (stranded,
                visiting, just arrived), then one in service; and each in
                reverse list order, so `nodes:` still decides among equals.
                Picked the other way round, a node already leaving is left to
                finish while one in service is shed beside it, and the pool
                ends one under the ceiling."""
                free = [n for n in reversed(wakeable) if n in nodes]
                working = set(self._in_use(free))

                def cost(n):
                    if phase(n) == "sleeping":
                        return 0
                    return 1 if states[n].cordoned else 2
                return sorted(free, key=lambda n: (n in working, cost(n)))

            unshed = []
            if required > len(held_down):
                # More must go than are going: the rest are picked.
                picks = order([n for n in drains + up if n not in spared])[
                    :required - len(held_down)]
            elif required < len(held_down) and cfg.mode == "on":
                # Fewer must go than are held down -- the reading loosened, or
                # a node went down some other way. The surplus goes back to
                # being what it was: not shed, with a drain timeout and a
                # rescue like any other, and no deadline that would end its
                # work for a ceiling that no longer asks it. The ones that
                # carry work are given back first.
                keep = order(held_down)[:max(required, 0)]
                unshed = [n for n in held_down if n not in keep]
            if cfg.mode != "on":
                # A shadow says what it WOULD do, once per change, and does
                # nothing: no cordon, no note, no silence.
                if picks and st.get("_shed_dry_last") != (tuple(picks), L):
                    self.log("info", "dry_run: would shed %s (ceiling %d, "
                                     "awake %d)" % (", ".join(picks), L,
                                                    len(awake)),
                             nodes=picks)
                st["_shed_dry_last"] = (tuple(picks), L) if picks else None
            else:
                for n in unshed:
                    states[n] = self._forget_shed(n, states[n])
                    self.log("info", "no longer shed: the ceiling does not ask "
                                     "for this node to go", node=n, ceiling=L)
                for n in picks:
                    if phase(n) == "sleeping":
                        # A drain that was running when the ceiling engaged
                        # BECOMES a shed: still cordoned, its deadline anchored
                        # now and not at a cordon that may be a day old, and no
                        # drain timeout to give the node back.
                        self._node(n)["shed_at"] = now
                        self._try_note(n, "shed", now)
                        states[n] = dataclasses.replace(states[n], shed_at=now)
                        self.log("warn", "a drain under way is now a shed; its "
                                         "deadline starts here", node=n,
                                 ceiling=L)
                    else:
                        self._start_shed(n, states, awake, begun)
                # A node held down but with no operation -- a restart lost it --
                # is started again, on the anchor it already carries.
                for n in held_down:
                    if (n not in unshed and phase(n) != "sleeping"
                            and n not in begun):
                        self._start_shed(n, states, awake, begun)
                # And the nodes already down. A ceiling that has loosened to two
                # of three holds ONE node down, whatever it had put down at
                # zero: the others are merely asleep, and wake in the order the
                # list gives. Their notes go -- the earliest in the list, which
                # are the first demand would wake -- so that `status` and the
                # metric say how many the ceiling is actually holding.
                kept = len(held_down) - len(unshed) + len(picks)
                dark = [n for n in wakeable
                        if self._shed_anchor(n, states[n]) is not None
                        and not states[n].ready and not phase(n)]
                surplus = len(dark) - max(len(wakeable) - L - kept, 0)
                for n in dark[:max(surplus, 0)]:
                    states[n] = self._forget_shed(n, states[n])
        self._ceiling_report(present, states, exempt, ok)
        return begun

    def _start_shed(self, n, states, awake, begun):
        """Begin holding one node down: on record first, then through the
        ordinary sleep. True if it began."""
        s, state, now = self._node(n), states[n], self.now()
        phase = s.get("phase")
        try:
            if phase == "warming":
                try:
                    self.warmup.cleanup(n)
                except Exception as e:                # noqa: BLE001
                    self.log("warn", "warmup cleanup failed", node=n,
                             err=str(e))
                s["phase"] = None
            elif phase == "waking":
                # Arrived this tick, with the wake not yet completed: it is not
                # put into service, and not warmed -- the wake is let go of
                # where it stands, as an operator's cordon lets go of one.
                s["phase"] = None
                s["booting"] = False
                for k in ("cycled", "power_confirmed", "cold_start"):
                    s.pop(k, None)
            # The anchor is written BEFORE the cordon, as every note here is
            # written before what it records: a restart between the two finds
            # a note on a node that is not cordoned, which is cleared, and the
            # shed begins again -- not a cordon with no deadline. An anchor the
            # node already carries is KEPT: that is a shed that began earlier,
            # and its deadline must survive the restart that found it.
            if self._shed_anchor(n, state) is None:
                s["shed_at"] = now
                self._try_note(n, "shed", now)
                state = states[n] = dataclasses.replace(state, shed_at=now)
            self.log("warn", "SHED begin -- a capacity ceiling is in force",
                     node=n, ceiling=self._eff, shed_at=_iso(
                         self._shed_anchor(n, state)))
            if phase == "maintaining":
                self._end_visit(n, state, "MAINTENANCE ended -- a capacity "
                                          "ceiling is in force")
            else:
                self.sleep(n, state)
        except Exception as e:                        # noqa: BLE001
            self.log("error", "could not begin the shed; retrying next tick",
                     node=n, err=str(e))
            return False
        states[n] = dataclasses.replace(
            states[n], cordoned=True, ours=True,
            ours_since=(states[n].ours_since if self._ours(states[n])
                        and phase != "maintaining" else now))
        if n in awake:
            awake.remove(n)
        begun.add(n)
        return True

    def _ceiling_report(self, present, states, exempt, ok):
        """What this tick concluded about the ceiling, for the status object
        and the metrics -- and for the one log line a change earns.

        Built whether or not anything is listening: it is a few lists, and the
        log line is the same facts.
        """
        cfg, st = self.cfg, self.st
        now = self.now()
        deadline = cfg.ceiling_drain_deadline_s
        eff, engaged = self._eff, self._engaged
        # Held down by the ceiling, and not an operator's: asleep or draining.
        shed, draining = [], []
        if cfg.mode == "on" and engaged:
            for n in present:
                state = states[n]
                anchor = self._shed_anchor(n, state)
                if (anchor is None or n in exempt or not self._ours(state)):
                    continue
                shed.append(n)
                phase = (st.get(n) or {}).get("phase")
                if state.ready or phase == "sleeping":
                    draining.append({"node": n, "until": (
                        anchor + deadline if deadline else None)})
        if engaged:
            if st.get("_ceiling_since") is None:
                # From the notes on the nodes where there are any, so a
                # restart does not move it: it is when this began, not when
                # this process did.
                st["_ceiling_since"] = min(
                    [states[n].shed_at for n in present
                     if states[n].shed_at is not None] + [now])
        else:
            st["_ceiling_since"] = None
        powered_exempt = [n for n in exempt if states[n].ready]
        key = (eff, engaged, ok)
        last = st.get("_ceiling_last")
        if key != last and not (last is None and key == (None, False, True)):
            fields = dict(limit=eff, signal=self._cur, engaged=engaged,
                          exempt=powered_exempt, shed=shed)
            if not ok:
                self.log("warn", "capacity ceiling signal unavailable; "
                                 "treating it as no ceiling",
                         err=self._ceiling_err, **fields)
            elif engaged and not (last and last[1]):
                self.log("warn", "capacity ceiling ENGAGED", **fields)
            elif engaged:
                self.log("warn", "capacity ceiling changed", **fields)
            elif last and last[1]:
                self.log("info", "capacity ceiling RELEASED", **fields)
            elif eff is not None:
                self.log("info", "capacity ceiling does not limit the pool",
                         **fields)
            elif last and not last[2]:
                self.log("info", "capacity ceiling signal readable again",
                         **fields)
            else:
                self.log("info", "capacity ceiling no longer set", **fields)
        st["_ceiling_last"] = key
        from .types import NullCeiling
        self._rep = {
            "configured": not isinstance(self.ceiling, NullCeiling),
            "mode": cfg.mode, "engaged": engaged, "limit": eff,
            "signal": self._cur, "signal_ok": ok,
            "since": st.get("_ceiling_since"), "pool": len(self._pool),
            "shed": shed, "draining": draining, "exempt": powered_exempt,
            "forced": self.forced_total,
        }

    def _publish(self):
        """Hand the report to whoever is listening. Never raises and never
        waits on a decision: a failure is logged once per change and the next
        tick tries again.

        Each sink gets a snapshot of its own, taken here. Both read the report
        on other threads after the tick has gone on -- the metrics listener when
        it is scraped, the status writer when its turn comes -- so a report they
        shared would be one the next tick, or the other reader, could change
        under them. The status sink is asynchronous, and says whether its last
        write worked in `error`, read here on the following tick; the metrics
        sink is a lock and a dict, and raises if it is going to.
        """
        rep = self._rep
        if rep is None:
            return
        rep["forced"] = self.forced_total
        for what, sink, live_only in (("metrics", self.metrics, False),
                                      ("status", self.status, True)):
            if sink is None or (live_only and self.cfg.mode != "on"):
                continue
            key = "_publish_" + what
            try:
                # Read BEFORE handing over the next report: publishing wakes
                # the writer, whose retry could succeed and clear `error`
                # before it was read, and a failure nobody saw is not logged.
                err = getattr(sink, "error", None)
                sink.publish(copy.deepcopy(rep))
            except Exception as e:                    # noqa: BLE001
                err = str(e) or type(e).__name__
            if err:
                if self.st.get(key) != err:
                    self.log("warn", _PUBLISH_FAILED[what], err=err)
                    self.st[key] = err
            elif self.st.pop(key, None) is not None:
                self.log("info", "publishing the %s again" % what)

    # -- reconcile -------------------------------------------------------
    def tick(self):
        """One reconcile, and then the report of it -- whichever way it ends.

        The report is made even when the tick fails: a demand signal that is
        down must not also silence the one place an operator reads that a
        ceiling is engaged.
        """
        self._rep = None
        # Rebuilt from scratch: a tick that returns before the ceiling is read
        # -- no node exists yet, mode=off -- must not leave the last tick's
        # conclusion for sleep(), maintain() or a power cycle to act on.
        self._limit = self._eff = self._cur = None
        self._engaged = False
        self._states, self._pool, self._busy_seen = {}, [], {}
        try:
            self._reconcile()
        finally:
            self._publish()

    def _reconcile(self):
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

        # An operator who uncordons a node we cordoned leaves our ownership
        # mark behind -- `kubectl uncordon` knows nothing of it. Left there, the
        # operator's NEXT cordon of that node reads as ours, and every guard
        # that defers to an operator stops deferring: the stranded repair would
        # uncordon them, a visit would power the node on under their hands, a
        # wedged one would be power-cycled. Clear it the moment it is seen.
        for n in present:
            if states[n].ours and not states[n].cordoned:
                self.log("warn", "our ownership mark outlived its cordon -- "
                                 "the node was uncordoned by someone else; "
                                 "clearing the mark", node=n)
                try:
                    self._disown(n)
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not clear the ownership mark",
                             node=n, err=str(e))
                states[n] = dataclasses.replace(states[n], ours=False,
                                                ours_since=None)

        maint = self._maintenance_requests(present, states)

        # Reconcile notifications every tick rather than only on transitions.
        # A notification lost to a restart or an Alertmanager outage is then
        # re-asserted, and -- more importantly -- one left over on a node that
        # is UP is cleared, so a real failure of it is not silently swallowed.
        #
        # Muted means put down BY US: dark and carrying our cordon, and not a
        # node we have stopped being able to account for. Readiness alone once
        # decided this, which muted a node that crashed in service exactly as
        # if it had been slept -- for the whole of a 21-hour outage.
        for n in present:
            state, s = states[n], self._node(n)
            by_operator = state.cordoned and not state.ours
            #: A person has this node, one way or the other: it is theirs to
            #: watch, and nothing metalnap would say about it is news to them.
            theirs = by_operator or bool(state.maintenance)
            if state.ready:
                for k in ("dark_on_since", "off", "wake_backoff_until",
                          "backoff_record", "dark_after_visit"):
                    s.pop(k, None)
                state = states[n] = self._clear_trouble(n, state)
            elif theirs:
                # A human has it; they know.
                state = states[n] = self._clear_trouble(n, state)
            if theirs or not state.ready or not self._ours(state):
                # Raised only while metalnap keeps asking a Ready node to go
                # down. Going dark, or back to service, ends that.
                s.pop("shutdown_failed", None)
            if (s.get("trouble") and s["trouble"] != state.trouble
                    and not theirs):
                self._try_note(n, "trouble", s["trouble"])
            if ("backoff_record" in s
                    and state.power_cycled_at != s["backoff_record"]):
                # Somebody deleted the power-cycle record -- the documented
                # way to re-arm the cycle. Honour it now, not after the
                # backoff it was waiting out.
                self.log("info", "power-cycle record changed; the node may be "
                                 "tried again", node=n)
                s.pop("wake_backoff_until", None)
                s.pop("backoff_record", None)
            if (not s.get("phase") and state.shutdown_at is not None
                    and self._ours(state)):
                if self.now() - state.shutdown_at <= cfg.shutdown_timeout_s:
                    # A restart interrupted a shutdown. Resume waiting for it,
                    # rather than read a node the kubelet still reports Ready
                    # as stranded and ask it to shut down a second time.
                    self.log("info", "resuming a shutdown a restart "
                                     "interrupted", node=n)
                    s["phase"] = "powering_off"
                    s["phase_since"] = state.shutdown_at
                else:
                    self._try_note(n, "shutdown", None)
            if not state.ready and not theirs and self._ours(state):
                state = states[n] = self._check_dark(n, state)
            trouble = None if theirs else self._trouble(n, state)
            # Dark trouble unmutes the node, so its own alerts arrive. A node
            # that ignored a shutdown stays muted while it retries -- it is
            # going down -- and is alerted on all the same: our own alert is
            # excluded from our own silences, so the two do not collide.
            want_down = (self._asleep(n, state)
                         and (by_operator or not self._dark_trouble(n, state)))
            if cfg.mode != "on":
                # dry_run must not mutate ANYTHING outside this process, and a
                # notifier writes to a real system. A shadow deployment that
                # silences alerts is not observing, it is participating -- and
                # it will fight the controller it was meant to be compared
                # against. Caught by exactly that: a dry_run metalnap created a
                # live Alertmanager silence next to the incumbent's.
                self.log("info", "dry_run: would mark node %s"
                                 % ("down" if want_down else "up"), node=n,
                         trouble=trouble)
                continue
            try:
                if want_down:
                    self.notifier.going_down(n)
                else:
                    self.notifier.back_up(n)
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "notification reconcile failed", node=n,
                         err=str(e))
            try:
                if trouble:
                    self.notifier.alert(n, trouble)
                else:
                    self.notifier.clear_alert(n)
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "alert reconcile failed", node=n,
                         err=str(e))

        # Dark, and nobody's cordon on it: it went down on its own. Its own
        # alerts are left alone -- that is the point -- and it is said once
        # here, in case those alerts are not being read.
        crashed = [n for n in present
                   if not states[n].ready and not states[n].cordoned
                   and not states[n].maintenance]
        if crashed != st.get("_crashed_last"):
            if crashed:
                self.log("error", "managed nodes are down and metalnap did not "
                                  "put them down; their alerts are not muted",
                         nodes=crashed)
            elif st.get("_crashed_last"):
                self.log("info", "no managed node is down unaccounted for",
                         recovered=st["_crashed_last"])
            st["_crashed_last"] = crashed

        # A node asked for maintenance is out of the pool altogether, like one
        # an operator cordoned: never woken, slept or counted for demand. Even
        # in service -- counted as awake while it could not be slept, it capped
        # `want` below at the nodes that can, and one busy held node would
        # have kept a peer from ever being woken for the backlog beside it.
        awake = [n for n in present
                 if states[n].ready and not states[n].cordoned
                 and n not in maint]
        # A cordon this controller does not own belongs to an operator.
        held = [n for n in present
                if states[n].cordoned and not states[n].ours]
        wakeable = [n for n in present if n not in held and n not in maint]
        if held != st.get("_held_last"):
            if held:
                self.log("info", "nodes cordoned by an operator; held out of "
                                 "wake candidates", held=held)
            elif st.get("_held_last"):
                self.log("info", "operator cordon cleared", released=st["_held_last"])
            st["_held_last"] = held

        # The capacity ceiling: read, and acted on, BEFORE the demand signal.
        # See _ceiling_section() for why the order is the point.
        begun = self._ceiling_section(present, states, held, maint, awake,
                                      wakeable)
        limit = self._limit

        # Read AFTER the notification reconcile, which needs only node state: a
        # demand signal that is down must not also stop re-asserting alerts --
        # they carry a TTL, and would resolve themselves in the outage.
        demand_known = True
        try:
            shortfall = self.signal.shortfall()
            capacity, backlog = self._size(
                shortfall, [states[n].capacity for n in present])
        except Exception as e:                        # noqa: BLE001
            if limit is None:
                raise                  # as it always did: do not act
            # With a ceiling in force the shed has begun and has to be seen
            # through, and nothing it needs depends on demand. So the phases
            # advance -- and nothing that WOULD depend on demand is decided.
            self.log("error", "demand signal failed; carrying on with the "
                              "capacity ceiling alone", err=str(e))
            demand_known = False
            shortfall, capacity, backlog = 0.0, None, 0
        try:
            saturated = (self.signal.saturated_units() if demand_known else 0)
        except Exception as e:                        # noqa: BLE001
            self.log("warn", "saturation check failed; sizing on shortfall "
                             "alone", err=str(e))
            saturated = 0
        st["_tick"] = st.get("_tick", 0) + 1
        # `want` is how many nodes should be AWAKE, so it has to count the
        # ones already carrying work, not only the work still waiting. The
        # shortfall sees only what the scheduler cannot place; once awake nodes
        # absorbed a backlog it read zero, `want` read zero, and every busy node
        # in the pool was cordoned and drained, one a tick -- while a full node
        # plus a new backlog read as "demand met" and woke nothing.
        in_use = self._in_use(awake)
        for n in present:
            if n in awake and n not in in_use:
                self._node(n).setdefault("idle_since", self.now())
            else:
                self._node(n).pop("idle_since", None)
        # A saturated queue admits no more work, so its demand is invisible to
        # shortfall() -- and it is a FLOOR, not more demand. Its runners sit on
        # nodes already counted in use; a node woken on top of them is one the
        # capped queue can never use. Counted as an addend it did exactly
        # that, and held the extra node awake and idle for as long as the cap
        # held.
        want = max(0, min(len(wakeable),
                          max(len(in_use) + backlog, saturated)))
        # Scale-up simulation: would the waiting work actually FIT here?
        # shortfall() is a sum, which assumes everything waiting is waiting on
        # capacity. Work blocked on a selector or a volume, or too big for a
        # node, inflates it -- and would wake a node that cannot help, keep an
        # idle one awake, and pull a draining one back into service.
        #
        # Skipped when saturation drove the demand: a saturated queue has
        # nothing pending to inspect -- that is the entire problem.
        #
        # Decided here, BEFORE anything acts on `want`, so a mid-sleep rescue
        # or a visit takeover cannot fire on demand that cannot land. The one
        # exception is the stranded repair below, which keeps `unguarded`: a
        # differential test against the controller this replaced found 40
        # divergences in 3000 states, every one with fits=False, and every one
        # a STRANDED node -- already powered, already cordoned -- put to sleep
        # instead of returned to service. Both are safe, but returning it
        # matches "wake readily, sleep reluctantly", and a transient fit-check
        # failure cannot power off a node that was only ever mid-wake.
        unguarded = want
        if backlog > 0 and saturated == 0:
            try:
                if not self.signal.fits_node(capacity):
                    self.log("info", "demand present but none of it could run "
                                     "on a node this size; not counting it",
                             shortfall=_show(shortfall))
                    want = max(0, min(len(wakeable), len(in_use)))
            except Exception as e:                    # noqa: BLE001
                self.log("warn", "fit check failed; holding the pool as it is",
                         err=str(e))
                want = len(awake)
        # The ceiling caps what is wanted -- AFTER the fit guard, which can
        # hand `want` back as "the pool as it is", and to `unguarded` as well,
        # which the stranded repair reads: clamped before either, a guard that
        # holds the pool, or a repair that returns a stranded node to service,
        # puts nodes in service over the ceiling. One clamp, here, covers the
        # wake, the mid-sleep rescue, the visit takeover and the visit
        # schedule below.
        wanted = want
        if limit is not None:
            want = min(want, limit)
            unguarded = min(unguarded, limit)
        if not demand_known:
            want = unguarded = 0       # nothing below may rescue on a guess

        # Advance in-flight operations, then CARRY ON. Returning here would
        # reintroduce the starvation the phase machines exist to remove: one
        # node draining would stop any other being woken.
        in_flight = [n for n in present if st.get(n, {}).get("phase")]
        settling = [n for n in in_flight if st[n]["phase"] == "powering_off"]
        for n in in_flight:
            phase = st[n]["phase"]
            if n in begun:
                # Cordoned a moment ago by the ceiling: its first step is next
                # tick, the cadence an ordinary sleep keeps -- and this tick's
                # observation of it, from before the cordon, would read as an
                # operator having uncordoned it.
                continue
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
            if n in maint and phase in ("waking", "sleeping", "maintaining"):
                # Asked for mid-operation. None of the three finishes the way
                # the operator wants: a wake uncordons their node, a sleep
                # powers it off, a visit does the latter on a timer. Let go of
                # it where it stands -- cordon and power as they are -- and let
                # maintenance mode take it from there. A shutdown already
                # requested, and a warmup on a node already in service, are
                # left to finish: neither can be recalled, nor needs to be.
                self.log("info", "operator asked for the node for maintenance "
                                 "mid-operation; abandoning the operation",
                         node=n, phase=phase,
                         maintenance=states[n].maintenance)
                if phase == "maintaining":
                    self._release_visit(n)
                st[n]["phase"] = None
                st[n]["booting"] = False
                continue
            if not demand_known and not (
                    phase in ("sleeping", "powering_off")
                    and self._shed_anchor(n, states[n]) is not None):
                # The guards above are an operator's; this one is demand's. An
                # ordinary drain, a wake, a visit and a warmup all wait for a
                # signal -- finishing one with the rescue switched off is a
                # decision on demand nobody can read. A shed is the ceiling's,
                # and is seen through.
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
                    # and its timeout runs from power-on either way -- but it
                    # is marked, because a node dark partway through a visit
                    # may be rebooting into an update, and must not be the one
                    # a wake timeout power-cycles.
                    st[n]["phase"] = "waking"
                    if st[n].get("visit_powered_on"):
                        st[n].setdefault("dark_after_visit", self.now())
                    for k in ("cycled", "power_confirmed", "cold_start"):
                        st[n].pop(k, None)
                    continue
                try:
                    self._set_cordon(n, False)
                    st[n]["awake_since"] = self.now()
                    awake.append(n)
                    st["_joined_tick"] = st["_tick"]
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not uncordon", node=n, err=str(e))
                continue
            if phase == "sleeping" and not states[n].cordoned:
                # Somebody uncordoned it mid-drain. The cordon is how a drain
                # holds a node out of service, so losing it means an operator
                # wants the node working -- and finishing the sleep would power
                # off the machine they just put back. maintain() reads the
                # same signal the same way.
                self.log("info", "node was uncordoned mid-drain; abandoning "
                                 "the sleep and leaving it in service", node=n,
                         cooldown_s=cfg.sleep_cooldown_s)
                st[n]["phase"] = None
                # Backed off like any abandoned sleep, or the next tick would
                # start it again and re-cordon the node they just put back.
                st[n]["sleep_attempts"] = 0
                st[n]["cooldown_until"] = self.now() + cfg.sleep_cooldown_s
                continue
            if phase == "sleeping" and want > len(awake):
                self.log("info", "demand arrived mid-sleep; keeping the node",
                         node=n, want=want, awake=len(awake))
                st[n]["phase"] = None
                try:
                    self._set_cordon(n, False)
                    awake.append(n)
                    st["_joined_tick"] = st["_tick"]
                except Exception as e:                # noqa: BLE001
                    self.log("error", "could not uncordon", node=n, err=str(e))
                continue
            try:
                if phase == "waking":
                    if self.wake(n, room=limit is None or len(awake) < limit):
                        # Serving from this tick on. Counted now, or the
                        # decisions below see demand this node already meets.
                        awake.append(n)
                        st["_joined_tick"] = st["_tick"]
                elif phase == "warming":
                    self.warm(n)
                elif phase == "maintaining":
                    self.maintain(n, states[n])
                elif phase == "powering_off":
                    self.confirm_off(n, states[n])
                else:
                    self.sleep(n, states[n])
            except Exception as e:                    # noqa: BLE001
                self.log("error", "phase step failed", node=n, phase=phase,
                         err=str(e))

        # Before the stranded repair, which ends the tick: a request is an
        # operator waiting, and must not queue behind a repair on another node.
        took_up = self._take_up_maintenance(maint, states, settling)
        if not demand_known:
            # What follows decides on demand, which could not be read. The
            # phases above are already advanced; the rest waits for a signal.
            return

        # A node powered on but cordoned is in NEITHER desired state: burning
        # power, serving nothing. It gets there when an operation is
        # interrupted. Only cordons this controller owns are touched -- and
        # not on a node asked for maintenance, which is exactly that shape on
        # purpose, and is the operator's until they give it back.
        stranded = [n for n in present
                    if states[n].ready and self._ours(states[n])
                    and n not in in_flight and n not in maint]
        for n in stranded:
            if len(stranded) > 1:
                self.log("info", "more nodes are stranded; they wait their "
                                 "turn", node=n, waiting=stranded[1:])
            if len(awake) < unguarded:
                self.log("warn", "stranded node needed; completing the wake",
                         node=n)
                try:
                    self._set_cordon(n, False)
                    self._node(n)["awake_since"] = self.now()
                    awake.append(n)
                    st["_joined_tick"] = st["_tick"]
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
        # Capacity already on its way. Only a cold boot WE started counts: one
        # found powered may be wedged, and counting it would hold demand back
        # for a whole wake timeout behind a machine that never arrives.
        coming = [n for n in present
                  if st.get(n, {}).get("phase") == "waking"
                  and st[n].get("booting")]
        self.log("info", "observed", shortfall=_show(shortfall),
                 saturated=saturated, want=want, awake=awake, in_use=in_use,
                 coming=coming, ceiling=self._eff, wanted=wanted)
        # Demand the ceiling is refusing stays visible, once per change -- it
        # is the one reason a node does not come up that is not a fault -- and
        # a shadow says what it would not have done.
        refused = (wanted, want) if (
            limit is not None and wanted > want
            and wanted > len(awake) + len(coming)) else None
        if refused != st.get("_refused_last"):
            if refused:
                self.log("info", "dry_run: would not wake: a capacity ceiling "
                                 "forbids it" if cfg.mode != "on" else
                                 "not waking: a capacity ceiling forbids it",
                         wanted=wanted, allowed=want, ceiling=limit)
            st["_refused_last"] = refused
        # A node that joined service last tick carries work a SCRAPED signal
        # may not have seen placed yet: busy() counts it, and a shortfall
        # from before the scrape counts it again. Hold new wakes for that one
        # tick. The default signal reads pods live and cannot double-count,
        # but a PromQL override scraped once a minute can.
        just_joined = st.get("_joined_tick") == st["_tick"] - 1

        if want > len(awake):
            st["want_high_since"] = st.get("want_high_since") or now
            st["want_high_last"] = now
            st["want_low_since"] = None
            # `coming` counts here and nowhere else. A cold boot takes minutes
            # and this runs every one of them, so without it one node's worth
            # of demand powered on one more machine per tick until the first
            # came up -- measured in production at three and four nodes for
            # want=1, every demand episode, each then held up by min_uptime.
            if (now - st["want_high_since"] >= cfg.wake_sustain_s
                    and want > len(awake) + len(coming) and not just_joined
                    # Gated on nodes still POWERED, draining ones included, and
                    # not only on those in service: a shed node is cordoned and
                    # so not awake, but it draws power until it is off, and a
                    # budget ceiling would otherwise be exceeded for the length
                    # of every drain.
                    and (limit is None
                         or self._powered(cutting=True) < limit)):
                for n in wakeable:
                    if n in awake or n in in_flight:
                        continue
                    if now < st.get(n, {}).get("wake_backoff_until", 0):
                        continue          # given up on, or failed to power on
                    try:
                        self.wake(n)
                    except Exception as e:            # noqa: BLE001
                        # A BMC that cannot be reached must not abort the rest
                        # of the tick -- maintenance is decided below -- nor
                        # be first in line again next tick, ahead of every
                        # node that could actually come up.
                        self.log("error", "could not begin the wake; trying "
                                          "other nodes first", node=n,
                                 err=str(e), retry_in_s=cfg.wake_timeout_s)
                        self._node(n)["wake_backoff_until"] = (
                            now + cfg.wake_timeout_s)
                    break
        elif want < len(awake):
            st["want_low_since"] = st.get("want_low_since") or now
            st["want_high_since"] = None
            st["want_high_last"] = None
            if now - st["want_low_since"] >= cfg.sleep_sustain_s:
                for n in reversed(wakeable):
                    # Only a node carrying no work, and carrying none for a
                    # whole sleep window: sleep reluctantly. A node that has
                    # just finished a job is the likeliest to be handed the
                    # next, and cordoning a busy one takes capacity away for up
                    # to a drain timeout while it finishes.
                    if (n in awake and n not in in_flight and n not in in_use
                            and now - st[n].get("idle_since", now)
                            >= cfg.sleep_sustain_s):
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
        # left over and nothing more -- not even a tick in which an operator's
        # request has just powered a node on, which serialisation would have
        # stopped had the request been a phase.
        #
        # And only with room for it. A visit powers a node on for nobody, so it
        # may start only if that cannot take the powered count over the limit:
        # a steady budget works as it always did, and an emergency -- a limit
        # of 0, or a pool already at or over it -- holds visits back. Counted as
        # a wake is gated, so a visit already up, and a node still draining,
        # take a slot. A visit in progress is ended by the shed when the pool
        # goes OVER the limit, which takes it first (it serves nothing). They
        # are owed from the moment there is room, and the schedule is measured
        # from when a node went dark, so they come due then. Said once: it would
        # otherwise look like the schedule had silently stopped.
        if limit is not None and self._powered(cutting=True) + 1 > limit:
            if cfg.maintenance_interval_s and not st.get("_visits_held"):
                self.log("info", "scheduled maintenance visits are held back "
                                 "while a capacity ceiling leaves no headroom",
                         powered=self._powered(cutting=True), allowed=limit)
                st["_visits_held"] = True
        else:
            st["_visits_held"] = False
            if not took_up:
                self._maybe_maintain(present, states, awake, want)


#: Trouble raised from a power reading, which a later reading can take back.
_BMC_UNREADABLE = "its BMC could not be read while it carries our cordon"
_POWERED_UNEXPLAINED = ("powered but not Ready, and nothing metalnap is "
                        "doing explains it")
_NOT_OFF = "did not power off within %ds of a soft shutdown"
_RECHECKABLE = (_BMC_UNREADABLE, _POWERED_UNEXPLAINED,
                _NOT_OFF.split("%")[0])

#: What is logged when a report cannot be published. Fixed phrases: the
#: status one names what `metalnap status` reads, and must not read as a
#: failure of the thing it reports on.
_PUBLISH_FAILED = {
    "status": "could not publish the capacity ceiling status; decisions are "
              "unaffected",
    "metrics": "could not update the metrics; decisions are unaffected",
}


class _WithoutAlerts:
    """A Notifier from before alert(), given no-op alerts."""

    def __init__(self, inner):
        self.inner = inner

    def going_down(self, node):
        self.inner.going_down(node)

    def back_up(self, node):
        self.inner.back_up(node)

    def alert(self, node, reason):
        pass

    def clear_alert(self, node):
        pass


def _hash_fraction(digest):
    """A digest as a fraction in [0, 1) -- and the half-open end is load-bearing.

    FOUR bytes, not eight. A 64-bit numerator does not fit a double's 53-bit
    mantissa, so `(2**64 - 1) / 2.0**64` rounds UP to exactly 1.0 and the
    caller's offset lands ON the full stagger rather than inside it. 32 bits
    divides exactly, and four billion slots is more spread than a rack needs.

    Its own function so the boundary is reachable from a test: the inputs that
    break it are about one name in 2**54, which no sweep over plausible node
    names will ever produce -- but an all-ones digest produces it every time.
    """
    return int.from_bytes(digest[:4], "big") / 2.0 ** 32


def _show(amount):
    """A shortfall for a log line, one resource or several."""
    if isinstance(amount, dict):
        return {k: round(v, 1) for k, v in amount.items()}
    return round(amount, 1)


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _default_log(level, msg, **kv):
    import json
    from datetime import datetime, timezone
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "level": level, "msg": msg}
    rec.update(kv)
    print(json.dumps(rec), flush=True)
