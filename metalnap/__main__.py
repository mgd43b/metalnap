"""
Runnable reference deployment: GitHub ARC runners, Kubernetes, IPMI, Prometheus.

    python -m metalnap

With a command instead, it is the operator's tool, run on your own machine
against the controller in a cluster -- see `metalnap status --help`:

    metalnap status                 every managed node, and its state
    metalnap maintenance start k8s7 --reason "kernel 6.8"
    metalnap maintenance stop k8s7  ask for a node to work on; give it back
    metalnap logs -f --node k8s7    the controller's log, readable

Everything comes from the environment so the container image is useful without
a code change. If your stack differs, import Controller and pass your own
seams -- that is the point of them, and this module is only one wiring of many.

Required:
    NODES           comma-separated node names, e.g. "k8s14,k8s15"
    BMC_USER        BMC credentials
    BMC_PASS
    BMC_HOST_FMT    python format string for the BMC host,
                    e.g. "{node}-ipmi.internal.example.org"
    PROM_URL        Prometheus base URL

Optional:
    MODE                    off | dry_run | on          (default dry_run)
    ALERTMANAGER_URL        silence a node's alerts while it is down, and
                            raise MetalnapNodeNeedsAttention when a node needs
                            a human. STRONGLY recommended -- without it every
                            sleep looks like a node dying and pages someone.
    ALERTMANAGER_SILENCE_LABELS
                            comma-separated labels a node's alerts name it by;
                            one silence each (default instance,node).
                            kube-state-metrics alerts use `node`, relabelled
                            node-exporter alerts use `instance`.
    ALERTMANAGER_SILENCE_MATCHERS
                            extra matchers ANDed into every silence, one per
                            LINE, in amtool syntax -- e.g.
                            alertname=~"KubeNodeUnreachable|KubeletInstanceUnreachable"
                            to mute only what a sleep is expected to trip.
                            Lines, not commas: regexes contain commas.
    WARMUP_IMAGE            pull this onto a node after waking it, so the
                            first jobs do not each pay for it
    WARMUP_PULL_SECRETS     comma-separated imagePullSecrets for the above
    BURST_TAINT_KEY         the taint keeping other work off sleepable
    BURST_TAINT_VALUE       nodes, as they carry it (default
    BURST_TAINT_EFFECT      ci-burst=true:NoSchedule). The warmup pod
                            tolerates it, and only pending work tolerating
                            all three parts counts as demand -- so a taint
                            that differs from the nodes' reads as no demand.
                            An empty key counts every pending pod.
    ARC_NAMESPACE           default arc-runners
    CORDON_ANNOTATION       default metalnap.io/cordoned
    SHORTFALL_QUERY         PromQL for unmet MEMORY in GiB, instead of reading
                            it off the unschedulable pods themselves
    CPU_SHORTFALL_QUERY     the same for CPU in cores, or "" to size on memory
                            alone. The pool is sized on whichever of the two
                            needs more nodes.
    SATURATION_QUERY        override, or "" to disable the saturation term
    MAINTENANCE_INTERVAL_S  wake a node that has been asleep this long, so it
                            collects updates and config changes it would
                            otherwise never see. 0 (the default) disables it;
                            86400 is a sensible start. The node comes up
                            CORDONED, stays for MAINTENANCE_WINDOW_S, and goes
                            back down the ordinary way.
    MAINTENANCE_WINDOW_S    how long it stays up, from Ready (default 300)
    MAINTENANCE_STAGGER_S   per-node spread, so a rack does not power on in
                            unison (default 3600)
    MAINTENANCE_TIMEOUT_S   bound on one visit (default 3600)
    POWER_CYCLE_COOLDOWN_S  a node still powered but not Ready at its wake
                            timeout is power-cycled, at most once per node per
                            this long (default 86400); 0 disables it. Bounded
                            by the metalnap.io/power-cycled annotation, so a
                            restart cannot re-arm it; delete that annotation to
                            re-arm it by hand.
    MAINTENANCE MODE        not a setting: an operator asks for a node by
                            annotating it metalnap.io/maintenance=<reason>
                            (or `metalnap maintenance start`). It is powered
                            on once, then left alone -- no sleep, drain,
                            power cycle or mute -- until the annotation goes.
    SHUTDOWN_TIMEOUT_S      how long a soft shutdown may take before the node
                            is reported as one that would not power off
                            (default 600). Never forced.
    ... plus every timer in metalnap/config.py
"""
import os
import sys

from . import Config, Controller, cli
from .drain import ArcDrain
from .drain.arc import ARC_SATURATION_QUERY
from .kube import (Kube, KubeNodeSource, PendingPodFit, PendingPodShortfall,
                   allocatable)
from .notify import AlertmanagerNotifier
from .power import IpmiPower
from .signal import PrometheusSignal
from .warmup import ImagePrepull


def require(name):
    v = os.environ.get(name)
    if not v:
        sys.exit("metalnap: %s is required (see `python -m metalnap --help`)" % name)
    return v


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        # `metalnap --context prod status`: the kubectl and helm habit. The
        # options are the command's, so hand them to it.
        at = next((i for i, a in enumerate(argv) if a in cli.COMMANDS), None)
        if at is not None:
            argv = [argv[at]] + argv[:at] + argv[at + 1:]
    if argv and argv[0] in cli.COMMANDS:
        return cli.main(argv)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if argv:
        # Anything else was meant as a command. Starting the controller on a
        # laptop instead would only fail on a missing NODES.
        sys.exit("metalnap: unknown command %r (see `metalnap --help`)"
                 % argv[0])

    nodes = [n.strip() for n in require("NODES").split(",") if n.strip()]
    ns = os.environ.get("ARC_NAMESPACE", "arc-runners")
    host_fmt = require("BMC_HOST_FMT")
    kube = Kube()
    # The taint keeping other work off the sleepable nodes: the warmup pod
    # tolerates it, and demand that does not tolerate it is no demand here.
    # An empty key means no taint: every pending pod counts.
    taint = {"key": os.environ.get("BURST_TAINT_KEY", "ci-burst"),
             "value": os.environ.get("BURST_TAINT_VALUE", "true"),
             "effect": os.environ.get("BURST_TAINT_EFFECT", "NoSchedule")}
    if taint["effect"] not in ("NoSchedule", "PreferNoSchedule", "NoExecute"):
        sys.exit("metalnap: BURST_TAINT_EFFECT must be NoSchedule, "
                 "PreferNoSchedule or NoExecute, not %r" % taint["effect"])
    if not taint["key"]:
        taint = None

    sat_q = os.environ.get("SATURATION_QUERY", ARC_SATURATION_QUERY)
    # Sized per resource: runners that run out of CPU before memory, sized on
    # memory alone, woke about half the nodes a backlog needed.
    # CPU_SHORTFALL_QUERY="" goes back to memory alone. By default each is read
    # off the unschedulable pods with the scheduler's own effective-request
    # formula -- see PendingPodShortfall for why PromQL cannot give it.
    pending = PendingPodShortfall(kube, ns, taint=taint)
    queries = {"memory": os.environ.get("SHORTFALL_QUERY")
               or pending.of("memory")}
    cpu_q = os.environ.get("CPU_SHORTFALL_QUERY")
    if cpu_q != "":
        queries["cpu"] = cpu_q or pending.of("cpu")

    # Silencing is opt-in by URL, but strongly recommended: without it every
    # sleep looks like a node dying and pages someone.
    am = os.environ.get("ALERTMANAGER_URL")
    notifier = None
    if am:
        labels = [label.strip() for label in os.environ.get(
            "ALERTMANAGER_SILENCE_LABELS", "instance,node").split(",")
            if label.strip()]
        matchers = [m.strip() for m in os.environ.get(
            "ALERTMANAGER_SILENCE_MATCHERS", "").splitlines() if m.strip()]
        try:
            notifier = AlertmanagerNotifier(am, labels=labels,
                                            matchers=matchers)
        except ValueError as e:
            # At start, not at the first sleep: a matcher that cannot parse
            # would otherwise surface as a node that refuses to power off.
            sys.exit("metalnap: bad Alertmanager silence config: %s" % e)

    # Warming is opt-in by image. Without it the first work after a wake pays
    # the pull.
    warm_image = os.environ.get("WARMUP_IMAGE")
    warmup = ImagePrepull(
        kube, warm_image, namespace=ns,
        tolerations=[dict(taint, operator="Equal")] if taint else [],
        image_pull_secrets=[{"name": s} for s in
                            filter(None, os.environ.get(
                                "WARMUP_PULL_SECRETS", "").split(","))],
    ) if warm_image else None

    Controller(
        nodes=nodes,
        notifier=notifier,
        warmup=warmup,
        node_source=KubeNodeSource(
            kube, annotation=os.environ.get("CORDON_ANNOTATION",
                                            "metalnap.io/cordoned"),
            capacity_of=allocatable(tuple(queries))),
        power=IpmiPower(host_for=lambda n: host_fmt.format(node=n),
                        user=require("BMC_USER"), password=require("BMC_PASS")),
        signal=PrometheusSignal(
            require("PROM_URL"),
            queries,
            sat_q or None,
            fit_check=PendingPodFit(kube, ns, taint=taint)),
        drain=ArcDrain(kube, namespace=ns),
        config=Config(),
    ).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
