"""
The three seams metalnap plugs into, and the state it reasons about.

Everything here is duck-typed: implement the methods, pass the object in. No
registry, no entry points, no base classes to inherit. The controller is the
opinionated part; these are deliberately thin.
"""
from dataclasses import dataclass
from typing import List, Optional, Protocol


@dataclass
class NodeState:
    """What the controller needs to know about one node."""
    ready: bool
    cordoned: bool
    #: True only if THIS controller applied the cordon. A cordon it does not
    #: own is an operator's, and outranks every decision the controller makes.
    ours: bool
    #: Unix timestamp the node last became Ready, or None. Drives min-uptime.
    ready_since: Optional[float]
    #: Schedulable capacity in whatever unit the demand signal reports.
    capacity: float
    #: True if this node must never be power-managed, whatever the config
    #: says. A control plane node is the obvious case. This exists because
    #: configuration is the weakest link: the node list arrives from a
    #: ConfigMap or a Helm value, and a typo there should not be able to power
    #: off a machine the cluster cannot survive losing.
    protected: bool = False
    #: When this controller applied the cordon, if it can be recovered. This is
    #: DURABLE state -- it lives on the node, not in the controller's memory --
    #: so a drain deadline measured from it survives a restart. Measured from
    #: in-memory state instead, a process that restarts mid-drain resets its
    #: own deadline and can hold a node cordoned indefinitely.
    ours_since: Optional[float] = None
    #: Unix timestamp the node last STOPPED being Ready, or None while it is
    #: Ready. The mirror image of ready_since, and durable for the same reason:
    #: it is how long a node has been dark, which is what the maintenance
    #: schedule is measured against. Held in the controller's memory instead,
    #: every restart would forget that a node had been asleep for a fortnight.
    #:
    #: A source that cannot recover it may leave it None -- the controller then
    #: falls back to its own start time, which is safe but resets the
    #: maintenance clock on every restart.
    down_since: Optional[float] = None
    # -- durable notes ----------------------------------------------------
    # What this controller has to remember about a node across its own
    # restarts. Each lives ON THE NODE (see NodeSource.note), because each one
    # held in memory was a way for a restart to undo a safety decision.
    #
    #: When this controller last power-cycled the node. It bounds cycles to
    #: one per node per POWER_CYCLE_COOLDOWN_S; in memory, a controller that
    #: restarted more often than a broken machine fails to boot would cycle it
    #: indefinitely.
    power_cycled_at: Optional[float] = None
    #: When a maintenance visit last powered the node on. A node dark soon
    #: after one may be rebooting into firmware it just installed, and must
    #: not be power-cycled; in memory, a restart mid-update forgot that.
    visited_at: Optional[float] = None
    #: When a soft shutdown was requested and not yet confirmed. The kubelet
    #: reports Ready for most of a minute after the OS starts going down; in
    #: memory, a restart in that minute read the node as stranded and sent a
    #: second soft-off into the shutdown.
    shutdown_at: Optional[float] = None
    #: Why this controller handed the node to a human, or None. In memory, a
    #: restart re-muted a node it had given up on and resolved its alert.
    trouble: Optional[str] = None


class NodeSource(Protocol):
    """Where node state comes from and how cordons are applied."""

    def state(self, name: str) -> Optional[NodeState]:
        """None if the node does not exist yet -- not an error."""

    def set_cordon(self, name: str, cordoned: bool) -> None:
        """Cordon/uncordon, stamping or clearing this controller's ownership."""

    def note(self, name: str, key: str, value) -> None:
        """Durably record one of the notes above; None removes it.

        OPTIONAL. `key` is "power-cycled", "visited" or "shutdown" with a
        unix timestamp, or "trouble" with a reason. Without it the controller
        never power-cycles -- a cycle it cannot put on record is one the bound
        cannot see -- and remembers the rest only until it restarts.
        """

    def disown(self, name: str) -> None:
        """Remove this controller's ownership mark WITHOUT touching the cordon.

        OPTIONAL; set_cordon(name, False) is the fallback. Used when the mark
        has outlived its cordon, where writing the cordon at all would undo an
        operator who re-cordoned the node a moment ago.
        """


class PowerBackend(Protocol):
    """Physical power control. IPMI, Redfish, WoL, a PDU, a hypervisor."""

    def state(self, name: str) -> str:
        """'on' or 'off'."""

    def on(self, name: str) -> None:
        ...

    def soft_off(self, name: str) -> None:
        """Request a graceful shutdown. Never a hard cut."""

    def cycle(self, name: str) -> None:
        """Hard power cycle: off, then on again. OPTIONAL.

        Without it a wedged node is alerted on rather than cycled. The one
        hard cut in this protocol, and it is reserved for a single
        case: a node that is powered, has not been Ready for a full wake
        timeout, and that no operator holds. A machine whose kernel has locked
        up ignores a soft shutdown and reads "on" to its BMC for as long as
        anyone cares to wait, so without this the only remedy is a human.
        """


class DemandSignal(Protocol):
    """How much capacity is wanted beyond what is currently awake."""

    def shortfall(self) -> float:
        """
        Unmet demand, in the same unit as NodeState.capacity.

        Must be 0.0 when nothing is waiting -- NOT an error, and not an absent
        series. Returning a stale or unknown value is worse than raising:
        raising is treated as "do not act", which is always safe.
        """

    def saturated_units(self) -> int:
        """
        How many work queues are pinned at their own ceiling.

        This exists because shortfall() is usually derived from work the
        scheduler has ALREADY admitted, and a queue at its cap admits nothing
        further -- so genuine demand becomes invisible exactly when capacity is
        most needed. Each saturated unit counts as one node's worth of demand.

        Return 0 if the concept does not apply to your signal.
        """

    def fits_node(self, capacity: float) -> bool:
        """
        Could the waiting work actually run on a node of this size?

        Borrowed from Cluster Autoscaler's scale-up simulation, and the reason
        it matters: shortfall() is usually a SUM, which silently assumes every
        waiting item is waiting on capacity. Work blocked on a node selector, a
        taint it does not tolerate, or an unbound volume inflates that sum and
        wakes hardware that cannot help it.

        Return True if you cannot tell -- but know that "always True" means
        powering on a machine for work it can never run.
        """


class DrainPolicy(Protocol):
    """
    What 'busy' means on a node, and how to release work that will not leave.

    The distinction between busy and idle is the whole safety story. Getting it
    wrong destroys running work; assuming idle units eventually exit on their
    own deadlocks the drain forever.
    """

    def busy(self, node: str) -> List[str]:
        """
        Units currently executing work. NEVER interrupted, no exceptions.

        Raise rather than guess. The controller treats an exception as "busy",
        because the only safe reading of "I could not tell" is "do not touch".
        """

    def idle(self, node: str) -> List[str]:
        """
        Units holding no work.

        These are the ones that will never leave by themselves -- a warm pool
        waiting for work that a cordoned node will not receive. Waiting for
        them to drain is waiting forever.
        """

    def holds_work(self, unit: str) -> bool:
        """
        Does this ONE unit hold work, right now?

        Read fresh -- do not answer from a cached listing. The controller calls
        this immediately before releasing a unit, because work can be
        dispatched into an idle unit between listing it and releasing it, and
        that window is how running work gets destroyed.

        This lives in the protocol rather than inside release() on purpose: it
        is a safety rule, not an implementation detail, so the controller
        enforces it for every policy instead of trusting each author to
        remember. The simulation harness caught exactly that mistake within
        minutes of this project being extracted.
        """

    def release(self, unit: str) -> None:
        """
        Gracefully remove one idle unit.

        Must deregister the unit from its scheduler BEFORE destroying it, so
        work cannot be dispatched into something about to disappear. Must treat
        'already gone' as success.
        """

    def residual(self, node: str) -> List[str]:
        """
        Anything else still present that should block power-off.

        Scope this tightly. It gates the power-off, so counting unrelated
        workloads keeps the node awake forever.
        """


class Notifier(Protocol):
    """
    Tell something that a node is going away, and that it came back.

    OPTIONAL -- defaults to a no-op. Skipping it is the single most likely way
    to make an operator hate this controller: a node powering off looks exactly
    like a node dying, so every sleep pages someone. Worse than the noise, it
    trains people to ignore precisely the alerts that would tell them a node
    had genuinely failed.

    Implementations must be IDEMPOTENT and self-healing. The controller calls
    these on every relevant tick, not only on transitions, so a notification
    lost to a restart or an outage is re-asserted rather than lost forever.
    """

    def going_down(self, node: str) -> None:
        """Called before a node is powered off, and while it stays down.

        ONLY for a node this controller put down. A managed node that crashed
        looks exactly like one that was slept -- dark, unreachable -- and
        muting it as though it were routine is the one mistake a notifier must
        never make, because the more reliable the sleeps are, the more routine
        a crash looks.
        """

    def back_up(self, node: str) -> None:
        """Called for every node NOT deliberately down. Must clear
        `going_down`: a node that is up, or that is down for any reason other
        than this controller, must not be left muted."""

    def alert(self, node: str, reason: str) -> None:
        """A managed node needs a human, and the controller has stopped trying.

        Called every tick while it holds -- a node that stayed powered but not
        Ready through a power cycle, one that could not be cycled, one that
        would not power off. Never for a node that is merely asleep."""

    def clear_alert(self, node: str) -> None:
        """Called every tick the node is NOT in trouble. Must clear `alert`."""


class Warmup(Protocol):
    """
    Prepare a node to take work, after it is Ready but before it matters.

    OPTIONAL -- defaults to a no-op. The reference use is pulling a large
    container image so the first jobs do not each pay for it; measured on the
    deployment this came from, a cold pull was 450s against 0.69s warm.

    Runs as its own phase AFTER the node is already uncordoned and schedulable.
    That ordering is deliberate and was learned the hard way: warming before
    uncordoning means a slow warmup strands a node that is powered, Ready and
    serving nothing. Worst case here is a few early jobs paying the pull, which
    is exactly the behaviour you had before any warmup existed.
    """

    def start(self, node: str) -> None:
        """Begin. MUST NOT block -- completion is polled by done()."""

    def done(self, node: str) -> bool:
        """True once finished, either way. Never blocks."""

    def cleanup(self, node: str) -> None:
        """Remove any leftovers. Called on success and on timeout."""


class NullNotifier:
    """Default. Does nothing, loudly documented so it is a choice."""

    def going_down(self, node):
        pass

    def back_up(self, node):
        pass

    def alert(self, node, reason):
        pass

    def clear_alert(self, node):
        pass


class NullWarmup:
    """Default. Nothing to warm, so a node is ready the moment it is Ready."""

    def start(self, node):
        pass

    def done(self, node):
        return True

    def cleanup(self, node):
        pass
