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
    powered and Ready-but-cordoned intermediate states -- and the kubelet is
    reported Ready for a while after the BMC already reads off, sometimes past
    the next tick, which is where a controller that trusts either signal
    alone sends a second shutdown into the first.
  * a node sometimes IGNORES a soft shutdown and stays up.
  * a quarter of the seeds size on memory AND CPU, with the CPU a phase's
    work asks for per GiB drawn per phase -- so either can be the resource
    that runs out.
  * an operator periodically takes a node for maintenance, cordoning it
    WITHOUT the controller's annotation.
  * on half the seeds an operator ASKS for a node -- metalnap.io/maintenance
    -- whatever it is doing: asleep, in service, mid-drain, mid-visit, and
    sometimes the whole fleet at once. While they hold it they reboot it,
    switch it off at the BMC and sometimes back on themselves: everything a
    node in trouble does, done on purpose. They give it back with plain
    kubectl, which leaves the controller's record of the request behind for
    the controller to clear.
  * a node woken for a SCHEDULED maintenance visit sometimes reboots into the
    update it just installed, going NotReady in the middle of its own window.
    Cutting power there is how a routine update becomes an unbootable
    machine, so it is modelled and asserted rather than assumed.
  * demand is phased and, in one mode, deliberately shaped to flicker: parked
    just below one node's worth while saturation toggles around a ceiling.
  * a node WEDGES: its kernel locks up while it is in service, or during a
    boot, and it stays powered and NotReady -- ignoring a soft shutdown --
    until something power-cycles it. Some are simply broken, and wedge again
    after every cycle until an operator takes them away and fixes them. This
    is the shape of the 2026-09-17 incident, and before it was modelled the
    harness passed a controller that retried a wedged node's wake forever and
    muted it the whole time.
  * a node is PARTITIONED: NotReady to the cluster while its work carries on,
    until the partition heals. From outside it looks exactly like a wedge, and
    it is the case that makes "never interrupt running work" apply to a power
    cycle too.

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
(60 seeds x 900 ticks). The first three tables were re-measured together,
against the harness as it stood before operator maintenance requests were
modelled; a rate measured against an older harness says nothing about this
one, and what the requests did to them is set out after the fourth:

    no pre-release re-check                50/60
    controller never sleeps anything       16/60
    busy work ignored when draining        10/60
    in-flight operation ignores a cordon    2/60
    cordon timestamp re-stamped             1/60
    idle units never released               0/60
    saturation signal ignored               0/60
    wake timer reset by a flickering signal  0/60

"Busy work ignored" was 60/60 until only idle nodes were put to sleep; after
that no drain here met work it had to time out on, and it fell to 0/60 without
anything failing. Work that lands mid-drain now sometimes hangs, which is what
brought it back.

and for scheduled maintenance, on the 30 of those 60 seeds that run it:

    power cut to a node rebooting mid-visit 29/30
    a maintenance window that never closes  26/30
    the schedule silently stops firing      23/30
    visits run in parallel                   0/30
    visits ignore unmet demand               0/30
    a failed visit is retried every tick     0/30
    a visit leaves the node schedulable      0/30

The four zeroes are not gaps in cover, they are gaps in THIS harness: each is
caught deterministically in test_controller.py, and each describes a
priority or scheduling mistake rather than a safety one -- the sim would need a
model of what the fleet ought to be doing, not just what it is doing, to see
them. The top two rows are the ones that destroy hardware, and they are the
ones this harness is good at.

and for wedged, partitioned and shutdown-ignoring nodes (60 x 900):

    a crashed node muted like a slept one (#14)          60/60
    a power cycle made before it is on record            51/60
    a wedged node's wake retried forever (#13)           33/60
    recovery re-alerting from a stale note               16/60
    the cycle cooldown forgotten across a restart        16/60
    a shutdown not resumed after a restart                5/60
    a node handed to a human left muted                   4/60
    a hand-off forgotten across a restart                 3/60
    a power cycle over running work (a partition)         1/60
    "would not power off" outliving the retry             1/60
    a node dark mid-drain muted as asleep                 0/60
    a power cycle mid-update after a visit                0/60
    every visit renewing the mid-update grace             0/60
    a sleep "complete" at the soft-off request            0/60
    a sleeping node never checked for power               0/60
    no operator check at the moment of the cycle          0/60
    a booting node not counted (one node per tick)        0/60

and for operator maintenance requests, on the 30 of those 60 seeds that run
them:

    a request powered on again once taken up             30/30
    a node asked for muted as asleep                     28/30
    the power-on made before it is on record             24/30
    the stranded repair takes a node asked for           23/30
    an operation in flight not let go for a request      10/30
    demand wakes and sleeps a node asked for             10/30
    the record left behind when the node is given back   10/30
    more than one maintenance power-on in a tick          9/30
    a visit started on a node asked for                   2/30
    no fresh-read check where a wake finishes             0/30
    no fresh-read check at a wake timeout                 0/30
    taken up on the tick its shutdown settles             0/30

The zeroes are out of reach rather than missed. Nothing here changes inside a
tick, so a request never lands between the observation a tick begins with and
the fresh read a wake takes where it finishes -- the race both fresh-read
checks exist for, and the reason the operator check at the moment of the cycle
reads 0 above. And the last needs a shutdown confirmed on its timeout while the
kubelet still reads Ready; the kubelet here never lags that far on its own,
and where it came closest the operator had switched the node off themselves,
and leaving it dark was not wrong.

Requests hold a node out of most of this harness's budgets while they stand,
so on their half of the seeds they thin the cover the first three tables were
measured with. Four of those rows, the bug reintroduced afresh -- not quite
the original patch for the second, which starts at 15 rather than 16 -- and
measured without requests and with them (60 x 900, then 600 x 900):

    no pre-release re-check                   50 -> 47/60    512 -> 477/600
    controller never sleeps anything          15 -> 16/60    206 -> 175/600
    a crashed node muted like a slept one     60 -> 60/60    600 -> 600/600
    a power cycle made before it is on record 51 -> 49/60    479 -> 475/600

The rest have not been re-measured. The other half of the seeds draw nothing
for requests, and play out exactly as they did before them.

Every row in all four tables -- the zeroes and the ones -- also fails a
deterministic test in test_controller.py. The low rows are the ones that need
two rare things at once (a restart inside a shutdown, a partition that
outlasts a wake timeout, a visit's reboot that wedges), which is exactly why
the deterministic test comes first.

The low-rate rows are BACKSTOPS, not primary cover. Anything guarding running
work or an operator's cordon also has a deterministic test in
test_controller.py, because a 1-in-60 chance is not a safety guarantee. Use
this harness for emergent, sequence-dependent failures; use the unit tests for
precise scenarios you can already name.

WHAT IT CANNOT REACH: any interleaving this generator does not produce. One
worth naming: demand here is EXOGENOUS. Nothing waiting is ever absorbed by a
node that wakes, so the shape behind #16 -- a backlog the pool has just soaked
up reading as no demand at all -- never arises, and neither does a full node
beside a fresh backlog. Those are covered in test_controller.py's TestSizing;
what this harness does add for them is the invariant that no node is ever
taken out of service while it carries work. The
signal model is shaped from real incidents -- phased demand, a capped-queue
mode where shortfall parks just below one node's worth while saturation
toggles, hung work that outlives the drain timeout, operator maintenance,
injected restarts -- but it is a model, and a bug outside its shape will not be
found here.

Run: python3 -B tests/sim.py [--seeds N] [--ticks N]
     (the defaults, 60 x 900, are the CI gate and the tables above)
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
#: How long the kubelet is still reported Ready after the BMC reads off --
#: drawn per shutdown, either side of one tick. Fixed at one tick, the harness
#: flipped every node NotReady before the controller could look, and the
#: window a real one sees for most of a minute was never observed at all.
SHUTDOWN_S = (20.0, 110.0)
CAPACITY = 125.7
CPU_CAPACITY = 40.0


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
        #: Powered and NotReady, and staying that way: a locked kernel. Only a
        #: power cycle clears it, and a soft shutdown does nothing at all.
        self.hung = False
        #: Wedges again after every boot, until an operator repairs it.
        self.broken = False
        #: NotReady to the cluster, but running: work carries on and finishes.
        #: The tick the partition heals at, or None.
        self.partitioned = None
        #: DURABLE notes, like `ours`: they live on the node and survive the
        #: controller restarts injected below.
        self.power_cycled_at = self.visited_at = self.shutdown_at = None
        self.trouble = None
        #: When the controller took up the current maintenance request. Its
        #: note, durable like the rest -- and left behind when the operator
        #: gives the node back, because plain `kubectl annotate` removes only
        #: the annotation it names.
        self.maintenance_started_at = None
        #: An operator's maintenance request -- their reason -- or None. The
        #: one field here that a PERSON writes and the controller only reads.
        self.maintenance = None
        #: Ignored the last soft shutdown, and is still up because of it.
        self.ignored_off = False


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
        self.cordoned_busy = []
        #: Size per resource on a quarter of the seeds; the rest keep the
        #: single-number form every signal used before resources had names.
        self.per_resource = seed % 4 == 3
        self.cpu_per_gib = 0.3
        self.interrupted_update = []
        self.blocked = None
        self.zombie_since = {}
        self.sleepable_since = {}
        self.unvisited_since = {}
        self.visit_since = {}
        self.notified = {}
        self.alerted = {}
        self.warming = {}
        self.cycles = {}
        self.bad_cycle = []
        self.muted_hung_since = {}
        self.was_ours = {}
        self.stuck_wake_since = {}
        #: Operator maintenance requests run on half the seeds, for the reason
        #: default_cfg() gives visits half: a node asked for is exempt from
        #: most of the budgets below, and asking on every seed would quietly
        #: thin the cover those budgets give everywhere else. Split on
        #: seed // 3, so it is independent of the visit, cooldown and
        #: per-resource splits; the other half draws nothing for it.
        self.operator_requests = (seed // 3) % 2 == 0
        #: The operator's own dice. Drawn from the world's, every request
        #: would reshuffle everything after it -- and every tick's chance of
        #: one would reshuffle the seed from the first tick, before any
        #: request existed. Apart, a seed plays out exactly as it did without
        #: requests until the first one lands.
        self.ops = random.Random("operator-%d" % seed)
        #: Those standing now, by node: when each is to be given back, and
        #: what the controller has done about it.
        self.requests = {}
        #: Each node's request, and its maintenance-started record, as the
        #: controller observed them at the start of this tick. Every
        #: maintenance invariant is judged against what it could see.
        self.observed, self.observed_started = {}, {}
        self.intruded = []
        self.maint_power_ons = 0
        self.untaken_since = {}
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
        # A third of the seeds shorten the power-cycle cooldown to an hour, so
        # a broken node is cycled more than once in a run and the bound is
        # actually tested rather than trivially satisfied by the default day.
        esc = {} if seed % 3 else dict(power_cycle_cooldown_s=3600)
        if seed % 2 == 0:
            return Config(mode="on", **esc)
        return Config(mode="on", maintenance_interval_s=1800,
                      maintenance_window_s=300, maintenance_stagger_s=600,
                      maintenance_timeout_s=1200, **esc)

    # -- clock -----------------------------------------------------------
    def now(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        for n in self.nodes.values():
            if n.change_at is not None and self.t >= n.change_at:
                was = n.ready
                n.ready = n.powered
                if n.ready and (n.broken or self.rnd.random() < 0.02):
                    # Wedged during boot: powered, and never comes up. An
                    # update reboot that wedges has FINISHED rebooting -- into
                    # a hang -- so there is no update left to interrupt.
                    n.ready, n.hung, n.updating = False, True, None
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
                         capacity=({"memory": CAPACITY, "cpu": CPU_CAPACITY}
                                   if self.per_resource else CAPACITY),
                         # durable: survives the controller restarts injected
                         # below, which is the whole point of it
                         ours_since=n.ours,
                         down_since=None if n.ready else n.down_since,
                         power_cycled_at=n.power_cycled_at,
                         visited_at=n.visited_at,
                         shutdown_at=n.shutdown_at, trouble=n.trouble,
                         maintenance=n.maintenance,
                         maintenance_started_at=n.maintenance_started_at)

    def _asked_for(self, name, what):
        """Record `what` as an intrusion if an operator's request stood on
        this node in the state the controller observed this tick.

        The observation, not the node as it is now: the invariant is about
        what the controller KNEW, and a request it could not yet have seen
        is not one it ignored.
        """
        if self.observed.get(name):
            self.intruded.append((self.t, name, what))

    def set_cordon(self, name, cordoned):
        n = self.nodes[name]
        self._asked_for(name, "uncordoned" if not cordoned else "cordoned")
        if name in self.human_held and not cordoned:
            self.stomped.append((self.t, name, "uncordoned an operator"))
        if cordoned and not n.cordoned and any(
                w.node == name and w.work for w in self.workers):
            # Taking a node out of service while it carries work: the drain
            # then holds it, serving nothing new, for as long as the work
            # takes. Sleeps are for idle nodes.
            self.cordoned_busy.append((self.t, name))
        n.cordoned = cordoned
        n.ours = self.t if cordoned else None

    def note(self, name, key, value):
        setattr(self.nodes[name], {"power-cycled": "power_cycled_at",
                                   "visited": "visited_at",
                                   "shutdown": "shutdown_at",
                                   "trouble": "trouble",
                                   "maintenance-started":
                                       "maintenance_started_at"}[key], value)
        req = self.requests.get(name)
        if key == "maintenance-started" and req is not None:
            # Taken up, by the controller's own account. Kept apart from the
            # node's field because a record left over from an EARLIER request
            # sits in that field too, and must not count as this one's.
            req["taken"] = self.t if value is not None else None

    def disown(self, name):
        self._asked_for(name, "cleared the ownership mark")
        self.nodes[name].ours = None

    # -- PowerBackend ----------------------------------------------------
    def power_state(self, name):
        return "on" if self.nodes[name].powered else "off"

    def power_on(self, name):
        n = self.nodes[name]
        if self.observed.get(name):
            # The one thing the controller may do to a node asked for, and
            # only once per request: after that the machine's power is the
            # operator's, and they switch it off on purpose.
            req = self.requests[name]
            req["power_ons"] += 1
            self.maint_power_ons += 1
            if self.observed_started.get(name) is not None:
                self.intruded.append((self.t, name, "powered on a node whose "
                                      "request was already taken up"))
            elif n.maintenance_started_at != self.t:
                # A power-on the record does not show is one a restart makes
                # again after the operator has switched the machine off.
                self.intruded.append((self.t, name, "powered on for "
                                      "maintenance before putting it on "
                                      "record"))
            if req["power_ons"] > 1:
                self.intruded.append((self.t, name, "powered on %d times for "
                                      "one request" % req["power_ons"]))
            if self.maint_power_ons > 1:
                self.intruded.append((self.t, name, "%d maintenance power-ons "
                                      "in one tick" % self.maint_power_ons))
        if not n.powered:
            n.powered = True
            n.change_at = self.t + BOOT_S

    def soft_off(self, name):
        self._asked_for(name, "soft-off")
        if name in self.human_held:
            self.stomped.append((self.t, name, "powered off while held"))
        if self.nodes[name].updating:
            self.interrupted_update.append((self.t, name))
            self.nodes[name].updating = None
        for w in self.workers:
            if w.node == name and w.work:
                self.powered_off_busy.append((self.t, name, w.name, w.work))
        n = self.nodes[name]
        if n.hung:
            return          # a locked kernel does not act on an ACPI request
        if n.powered and not n.ignored_off and self.rnd.random() < 0.05:
            n.ignored_off = self.t     # this once; the next request is heard
            return
        n.ignored_off = False
        if n.powered:
            n.powered = False
            n.change_at = self.t + self.rnd.uniform(*SHUTDOWN_S)
            # Powered off, so no longer partitioned from anything. Left set,
            # the old heal deadline fired after the next boot and declared the
            # node Ready over whatever that boot was doing.
            n.partitioned = None
        self.workers = [w for w in self.workers if w.node != name]

    def power_cycle(self, name):
        n = self.nodes[name]
        self._asked_for(name, "power cycle")
        if name in self.human_held:
            self.stomped.append((self.t, name, "power-cycled while held"))
        if n.updating:
            self.interrupted_update.append((self.t, name))
            n.updating = None
        for w in self.workers:
            if w.node == name and w.work:
                self.bad_cycle.append((self.t, name, "power-cycled a node "
                                       "running %s" % w.work))
        if n.ready:
            self.bad_cycle.append((self.t, name, "power-cycled a Ready node"))
        if not n.powered:
            self.bad_cycle.append((self.t, name, "power-cycled a node that "
                                                 "was off"))
        if n.power_cycled_at != self.t:
            self.bad_cycle.append((self.t, name, "power-cycled without "
                                                 "recording it first"))
        last = self.cycles.get(name)
        if (last is not None
                and self.t - last < self.cfg.power_cycle_cooldown_s):
            self.bad_cycle.append((self.t, name, "power-cycled %.0fs after "
                                   "the last cycle (cooldown %ds)"
                                   % (self.t - last,
                                      self.cfg.power_cycle_cooldown_s)))
        self.cycles[name] = self.t
        n.hung, n.powered, n.ready = False, True, False
        n.partitioned = None
        n.ignored_off = False
        n.change_at = self.t + BOOT_S
        self.workers = [w for w in self.workers if w.node != name]

    # -- DemandSignal ----------------------------------------------------
    def shortfall(self):
        if self.per_resource:
            return {"memory": self.demand,
                    "cpu": self.demand * self.cpu_per_gib}
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
        # A person is working on it: whatever it does is news to nobody, and
        # muted, a real failure of it is news nobody gets.
        self._asked_for(node, "muted")
        self.notified[node] = "down"

    def back_up(self, node):
        self.notified[node] = "up"

    def alert(self, node, reason):
        self._asked_for(node, "alerted on (%s)" % reason)
        self.alerted[node] = reason

    def clear_alert(self, node):
        self.alerted.pop(node, None)

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
        #
        # And some of it hangs. Only an idle node is ever put to sleep, so work
        # landing in this window is the ONLY way a drain meets work it has to
        # wait out -- and without some that outlives the drain timeout, that
        # branch never ran here and ignoring busy work went unseen.
        for w in listed:
            if self.rnd.random() < 0.15:
                w.work = "late-" + w.name
                w.ticks_left = (self.rnd.randint(150, 400)
                                if self.rnd.random() < 0.1
                                else self.rnd.randint(2, 6))
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
            if self.per_resource:
                # Either side of 40/125.7: sometimes CPU runs out first.
                self.cpu_per_gib = self.rnd.uniform(0.15, 0.6)

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
                    and n.partitioned is None and not n.hung
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

        # A node wedges. In service, mostly -- that is the incident -- but
        # anywhere it is powered and up, an operator's node included, because
        # the guard against cycling a node someone is working on has to be
        # exercised by one that genuinely needs cycling.
        for n in self.nodes.values():
            if (n.powered and n.ready and n.updating is None
                    and self.rnd.random() < 0.003):
                n.ready = False
                n.down_since = self.t
                if self.rnd.random() < 0.3:
                    # Partitioned, not wedged. Its units stay, and so does their
                    # work -- it carries on, and a power cycle would kill it.
                    n.partitioned = self.t + self.rnd.randint(
                        10, 40) * self.cfg.interval_s
                    continue
                n.hung = True
                n.broken = self.rnd.random() < 0.3
                # Its work died with it. Leaving the units behind would
                # have the harness blame the controller for "interrupting"
                # work the machine had already lost.
                self.workers = [w for w in self.workers if w.node != n.name]
            elif n.partitioned is not None and self.t >= n.partitioned:
                n.partitioned = None
                if n.updating is None:
                    # A reboot in flight decides readiness itself, when its
                    # own change_at comes round. Declaring the node Ready here
                    # had the harness blame the controller for ending a visit
                    # on a node that only looked recovered.
                    n.ready, n.ready_since = n.powered, self.t

        # An operator eventually takes a broken node away and repairs it. They
        # remove metalnap's mark as they cordon, which is what taking a node
        # from the controller means; and it is back in service when released.
        for n in self.nodes.values():
            if (n.broken and n.hung and n.name not in self.human_held
                    and self.rnd.random() < 0.01):
                self.human_held.add(n.name)
                n.cordoned, n.ours = True, None
                n.broken = n.hung = False
                n.powered, n.ready = True, False
                n.change_at = self.t + BOOT_S

        # An operator takes a node for maintenance, and later releases it.
        for n in self.nodes.values():
            if n.name in self.human_held:
                if self.rnd.random() < 0.03:
                    self.human_held.discard(n.name)
                    n.cordoned, n.ours = False, None
            elif not n.cordoned and n.ready and self.rnd.random() < 0.01:
                self.human_held.add(n.name)
                n.cordoned, n.ours = True, None      # NOT ours: a human did it

        self.step_maintenance_requests()

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
            # The flicker shape. A queue hovering at its ceiling: saturation
            # toggles, and the backlog straddles a node's worth with it --
            # just below, then just above -- so `want` flips by one, tick by
            # tick. That keeps every "wants more" run to one or two ticks,
            # which is what starves a timer needing three. Independent
            # randomness produces long runs instead, and a broken controller
            # wakes during them.
            if self.rnd.random() < 0.75:
                self.saturated = 0 if self.saturated else 1
            self.demand = (self.rnd.uniform(0.55, 0.95)
                           + self.saturated) * CAPACITY
        elif self.big:
            self.demand = self.rnd.choice([260.0, 300.0, 280.0])
            self.saturated = self.rnd.choice([0, 1, 1, 2])
        else:
            self.demand = self.rnd.choice([8.0, 40.0, 100.0, 130.0])
            self.saturated = self.rnd.choice([0, 1, 1, 2])

    def step_maintenance_requests(self):
        """An operator asks for a node -- `metalnap.io/maintenance` -- works
        on it, and gives it back.

        Asked for in whatever state the node is in: asleep, in service,
        mid-drain, mid-visit, booting, wedged. Sometimes for the whole fleet at
        once, which is the case one power-on per tick exists for. While they
        hold it they do what a person upgrading a machine does, all of which
        looks, from outside, like a node in trouble: they reboot it, they
        switch it off at the BMC, and sometimes they switch it back on
        themselves. Every one of the controller's remedies for a node doing
        that is wrong here, which is what the invariants below assert.

        Given back with plain kubectl, which removes the one annotation and
        leaves the controller's maintenance-started record for it to clear --
        so a controller that forgets to is caught by the NEXT request on that
        node, which it then never takes up.
        """
        if not self.operator_requests:
            return
        interval = self.cfg.interval_s
        released = set()
        for name, req in list(self.requests.items()):
            n = self.nodes[name]
            if self.t >= req["until"]:
                # Usually once it is back up; sometimes left dark, because the
                # work that wanted it off is done and the rest is metalnap's.
                # Never in the minute the kubelet outlives a power-off: that
                # is a node Ready and off, and giving THAT back is a race of
                # the operator's making, not a state the controller owns.
                if ((n.ready and n.powered)
                        or (not n.ready and self.ops.random() < 0.15)):
                    n.maintenance = None
                    del self.requests[name]
                    released.add(name)
                elif not n.powered and not n.ready and self.ops.random() < 0.2:
                    self._operator_power_on(n)
                continue
            r = self.ops.random()
            if n.powered and n.ready and n.updating is None and r < 0.03:
                # A reboot into what they just installed: NotReady while
                # powered, which is a wedge to anything that does not know.
                n.ready = False
                n.down_since = self.t
                n.change_at = self.t + 2 * interval + BOOT_S
                self.workers = [w for w in self.workers if w.node != name]
            elif n.powered and r < 0.045:
                # Off at the BMC, to reseat a DIMM or flash firmware that wants
                # a cold start. The kubelet outlives it, as it does any other.
                n.powered, n.hung, n.partitioned = False, False, None
                n.ignored_off, n.updating = False, None
                n.change_at = (self.t + self.ops.uniform(*SHUTDOWN_S)
                               if n.ready else None)
                self.workers = [w for w in self.workers if w.node != name]
            elif not n.powered and not n.ready and r < 0.07:
                self._operator_power_on(n)

        free = [n for n in self.nodes.values()
                if n.name not in self.requests and n.name not in released]
        if free and self.ops.random() < 0.004:
            # About a third for the whole fleet: `maintenance start --all`.
            for n in (free if self.ops.random() < 0.3
                      else [self.ops.choice(free)]):
                self._ask_for(n)
        elif (len(free) == len(self.nodes) and not any(n.powered for n in free)
                and self.ops.random() < 0.004):
            # And the fleet is likeliest asked for when it is asleep --
            # nothing running, so the time to flash firmware -- which is also
            # the one time several nodes that are off wait on one power-on per
            # tick. Two nodes are both off about a seventh of the time here,
            # so by chance alone that rule was barely reached.
            for n in free:
                self._ask_for(n)
        # By chance a request lands mid-operation about one time in ten, which
        # is too seldom for the case the controller's abandon logic exists
        # for, so some are timed to the controller's own phase -- read from
        # it the way the reboot model reads a visit's. Not a wake: one lingers
        # only on a wedged node, where the power-cycle rows find their cover,
        # and timing requests to it handed that cover to the operator.
        for n in free:
            if (n.maintenance is None and self.controller is not None
                    and (self.controller.st.get(n.name) or {}).get("phase")
                        not in (None, "waking")
                    and self.ops.random() < 0.015):
                self._ask_for(n)

    def _ask_for(self, n):
        n.maintenance = "kernel upgrade"
        self.requests[n.name] = {
            "until": self.t + self.ops.randint(5, 60) * self.cfg.interval_s,
            "taken": None, "power_ons": 0}

    def _operator_power_on(self, n):
        if n.shutdown_at is not None:
            # Switched back on under a shutdown the controller asked for and
            # has not yet seen finish. To the controller that is exactly a
            # node that ignored the request -- up, Ready, and inside the
            # shutdown's bound -- and it is judged as one.
            n.ignored_off = self.t
        n.powered = True
        n.change_at = self.t + BOOT_S

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
            ours = n.cordoned and n.ours is not None
            # `powered AND ready` -- not ready alone. A node mid-shutdown is
            # briefly still Ready while the OS goes down, and announcing it as
            # down is exactly right there.
            # Save for one that ignored a shutdown: nothing can know that
            # until the shutdown has had its bound, and it is muted meanwhile.
            ignoring = (n.ignored_off and self.t - n.ignored_off
                        <= self.cfg.shutdown_timeout_s + self.cfg.interval_s)
            if n.powered and n.ready and ann == "down" and not ignoring:
                fail("%s is Ready but still announced as down -- a real "
                     "failure of it would be silenced" % n.name)
            # Save one an operator has asked for, which is never muted at all:
            # switched off at the BMC, it is off because they are working.
            if (not n.powered and ours and ann != "down"
                    and n.maintenance is None):
                fail("%s was powered off without being announced as down "
                     "(announced=%r)" % (n.name, ann))
            # The incident: a node that went down on its own, muted as though
            # it had been slept. Only a node carrying OUR cordon may be muted.
            if ann == "down" and not ours:
                fail("%s is announced down without carrying our cordon -- it "
                     "went down on its own, and its alerts are muted" % n.name)
            # Never about a node that is fine -- save the one that ignored a
            # shutdown, which is Ready and exactly what the alert is for.
            # The ignored-shutdown exemption holds only while metalnap is still
            # asking -- the node carries our cordon, or did a tick ago (the
            # alert is raised before the stranded repair can uncordon it).
            asking = n.ignored_off and (ours or self.was_ours.get(n.name))
            if n.name in self.alerted and (
                    (n.powered and n.ready and not asking)
                    or (not n.powered and not n.ready)):
                fail("%s is alerted on while %s" % (
                    n.name, "Ready" if n.ready else "asleep"))
            # A wedged node may be muted while metalnap is still entitled to
            # believe it is shutting down or booting -- never beyond. Measured
            # per controller lifetime: a restart wipes what it had learned,
            # and re-learning it takes one more of the same windows.
            if n.hung and ann == "down":
                self.muted_hung_since.setdefault(n.name, self.t)
                budget = (self.cfg.shutdown_timeout_s
                          + 2 * self.cfg.wake_timeout_s
                          + 4 * self.cfg.interval_s
                          # A node that went dark during a maintenance visit
                          # gets two visit bounds from the visit's power-on
                          # before anything cuts its power: it may be
                          # rebooting into an update.
                          + (2 * self.cfg.maintenance_timeout_s
                             if self.cfg.maintenance_interval_s else 0))
                if self.t - self.muted_hung_since[n.name] > budget:
                    fail("%s is wedged and has been muted for %.0fs "
                         "(budget %.0fs)" % (
                             n.name, self.t - self.muted_hung_since[n.name],
                             budget))
            else:
                self.muted_hung_since.pop(n.name, None)

        self.was_ours = {n.name: n.cordoned and n.ours is not None
                         for n in self.nodes.values()}
        if self.bad_cycle:
            fail("unsafe power cycle: %s" % (self.bad_cycle[0],))
        # Once a wake has found a wedged node, it must be cycled back to
        # health or handed to a human within bound. The clock starts at the
        # first wake and does NOT reset when that wake times out: timing out
        # and being chosen again, silently, for 21 hours, is the incident --
        # and a clock that restarted with each attempt never saw it.
        for n in self.nodes.values():
            waking = (self.controller is not None
                      and (self.controller.st.get(n.name) or {}).get("phase")
                      == "waking")
            # Nor one an operator has asked for: its wake is abandoned, and it
            # is theirs to cycle. The clock restarts with the next wake after.
            if not n.hung or n.name in self.alerted or n.maintenance:
                self.stuck_wake_since.pop(n.name, None)
                continue
            if waking:
                self.stuck_wake_since.setdefault(n.name, self.t)
            if n.name in self.stuck_wake_since:
                budget = (2 * self.cfg.wake_timeout_s
                          + self.cfg.wake_sustain_s + 4 * self.cfg.interval_s
                          # the mid-update grace a visit's power-on buys
                          + (2 * self.cfg.maintenance_timeout_s
                             if self.cfg.maintenance_interval_s else 0))
                if self.t - self.stuck_wake_since[n.name] > budget:
                    fail("%s is wedged, was woken %.0fs ago, and has been "
                         "neither cycled back nor handed to a human (budget "
                         "%.0fs)" % (n.name,
                                     self.t - self.stuck_wake_since[n.name],
                                     budget))

        if self.cordoned_busy:
            fail("took a node carrying work out of service: %s"
                 % (self.cordoned_busy[0],))
        if self.powered_off_busy:
            fail("powered off a node running work: %s"
                 % (self.powered_off_busy[0],))
        if self.killed_work:
            fail("released a unit that was executing work: %s"
                 % (self.killed_work[0],))
        if self.stomped:
            fail("overrode an operator's maintenance cordon: %s"
                 % (self.stomped[0],))
        if self.intruded:
            fail("acted on a node an operator asked for maintenance: %s"
                 % (self.intruded[0],))
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
            # looks like, and the controller is right to leave it. So is one
            # asked for, until it is given back; then the budget starts afresh,
            # and a stranded node gets the whole of it to be put right.
            zombie = (n.powered and n.ready and n.cordoned
                      and n.name not in self.human_held
                      and n.maintenance is None)
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
            # The operator's own power-off is their own race to lose.
            if (not n.powered and n.ready and not n.cordoned
                    and n.maintenance is None):
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
            # A wedged node is not sleepable: a soft shutdown does nothing to
            # it and a hard cut is not metalnap's to make. It is left loud or
            # alerted, for a human -- which the notification checks assert.
            sleepable = (self.busy_phase is False
                         and n.powered and not n.hung
                         and n.partitioned is None
                         and n.name not in self.human_held
                         and n.maintenance is None
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
                             and n.name not in self.human_held
                             and n.maintenance is None)
                if not candidate:
                    self.unvisited_since.pop(n.name, None)
                    continue
                self.unvisited_since.setdefault(n.name, self.t)
                dark = self.t - self.unvisited_since[n.name]
                if dark > budget:
                    fail("%s has been asleep and due for %.0fs (budget %.0fs) "
                         "without a maintenance visit -- the schedule has "
                         "stopped firing" % (n.name, dark, budget))

        # An operator who asks for a dark node gets it powered on, and soon.
        # This is the check that fails if a request is never taken up -- a
        # record left behind by the last one, say -- which looks from outside
        # exactly like a feature that is not there.
        #
        # Measured only while the request is waiting on the controller: the
        # chassis OFF and the node NotReady (a node still reported Ready is
        # one the controller is right to record as already up), and not yet
        # taken up. Taken up and then dark is the operator's doing, and the
        # controller must leave it so. Every term is reachable:
        #   shutdown/warmup   the take-up yields to an operation of ours that
        #                     cannot be recalled -- a shutdown already
        #                     requested (or resumed from its note after a
        #                     restart), or a warmup; never both at once
        #   1 tick            and waits for a fresh observation after it ends,
        #                     since the one the tick began with predates it
        #   N ticks           one maintenance power-on per tick, so the last
        #                     of the fleet waits behind every other
        #   1 tick            measurement grain
        if self.cfg.mode == "on":
            budget = (max(self.cfg.shutdown_timeout_s,
                          self.cfg.warmup_timeout_s)
                      + (len(self.nodes) + 2) * self.cfg.interval_s)
            for n in self.nodes.values():
                req = self.requests.get(n.name)
                if (req is None or req["taken"] is not None or n.powered
                        or n.ready):
                    self.untaken_since.pop(n.name, None)
                    continue
                self.untaken_since.setdefault(n.name, self.t)
                waited = self.t - self.untaken_since[n.name]
                if waited > budget:
                    fail("%s was asked for maintenance and has been dark and "
                         "off for %.0fs (budget %.0fs) without being powered "
                         "on" % (n.name, waited, budget))

        # Sustained demand must actually produce a node. A WINDOWED MAJORITY,
        # not a run length: the signal flickers by nature, so a counter that
        # resets on any dip measures the flicker -- which is the mistake the
        # controller itself once made -- and a +1/-1 decay cancels exactly
        # under a 50/50 oscillation and never fires either.
        # A wedged node is not capacity the controller can provide -- it has
        # tried, cycled and handed it to a human -- so it does not raise the
        # ceiling either. Whether it escalated at all is asserted separately.
        # Nor is one an operator has, by cordon or by request, powered or
        # not: the controller may neither wake it nor count it, and a node
        # counted here would hide the peer it failed to wake beside it. That
        # was harmless while an operator's node was always powered; once they
        # switch one off, a window filled while they held it failed the tick
        # they let go of it, on a controller that had had no node to wake.
        pool = [n for n in self.nodes.values()
                if n.maintenance is None and n.name not in self.human_held]
        backlog = math.ceil(self.demand / CAPACITY)
        if self.per_resource:
            backlog = max(backlog, math.ceil(self.demand * self.cpu_per_gib
                                             / CPU_CAPACITY))
        # Saturation is a floor on what is wanted, not more of it: a capped
        # queue's runners are already on powered nodes.
        need = min(sum(1 for n in pool
                       if not n.hung and n.partitioned is None),
                   max(backlog, self.saturated))
        # A wedged node is powered and serves nothing, so it is not capacity.
        powered = sum(1 for n in pool
                      if n.powered and not n.hung and n.partitioned is None)
        self.need_window.append(need > powered)
        if len(self.need_window) > 40:
            self.need_window.pop(0)
        if (len(self.need_window) == 40 and sum(self.need_window) >= 16
                and any(not n.powered for n in pool)):
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
                    self.muted_hung_since.clear()
                    self.stuck_wake_since.clear()
                self.blocked = None
                self.observed = {k: v.maintenance
                                 for k, v in self.nodes.items()}
                self.observed_started = {k: v.maintenance_started_at
                                         for k, v in self.nodes.items()}
                self.maint_power_ons = 0
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

    def cycle(self, name):
        self.sim.power_cycle(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=900)
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
