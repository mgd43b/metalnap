"""
Runnable reference deployment: GitHub ARC runners, Kubernetes, IPMI, Prometheus.

    python -m metalnap

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
    BURST_TAINT_KEY         taint keeping other work off sleepable nodes
                            (default ci-burst); used for the warmup pod's
                            toleration and for the pending-pod fit check
    ARC_NAMESPACE           default arc-runners
    CORDON_ANNOTATION       default metalnap.io/cordoned
    SHORTFALL_QUERY         override the default PromQL for unmet MEMORY, GiB
    CPU_SHORTFALL_QUERY     override the default PromQL for unmet CPU, cores,
                            or "" to size on memory alone. The pool is sized
                            on whichever of the two needs more nodes.
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
    SHUTDOWN_TIMEOUT_S      how long a soft shutdown may take before the node
                            is reported as one that would not power off
                            (default 600). Never forced.
    ... plus every timer in metalnap/config.py
"""
import os
import sys

from . import Config, Controller
from .drain import ArcDrain
from .drain.arc import ARC_SATURATION_QUERY
from .kube import Kube, KubeNodeSource, PendingPodFit, allocatable
from .notify import AlertmanagerNotifier
from .power import IpmiPower
from .signal import PrometheusSignal
from .warmup import ImagePrepull


def default_shortfall_query(ns, resource="memory"):
    # Requests of pods the scheduler admitted but cannot place: memory in GiB,
    # CPU in cores.
    #
    # Init containers are counted as well as containers. A runner's dind is a
    # NATIVE SIDECAR -- an init container that keeps running -- and it asks
    # for as much as the runner does, so counting containers alone read half
    # of every backlog. It cannot be told apart from an ordinary init
    # container here: kube-state-metrics labels restart_policy from container
    # STATUS, and a pod that was never scheduled has none. So every init
    # container is summed, which over-counts an ordinary one with requests of
    # its own. That errs toward waking, which is the cheap direction.
    #
    # `and on(pod)` means this yields NO SERIES when nothing is unschedulable,
    # rather than a zero sample. PrometheusSignal reads an empty result as 0.0,
    # which is correct here -- but it is a real distinction, and a query that
    # returns nothing on the happy path surprises people who did not write it.
    sel = '{namespace="%s",resource="%s"}' % (ns, resource)
    return (
        'sum((kube_pod_container_resource_requests%s '
        'or kube_pod_init_container_resource_requests%s) '
        'and on(pod) kube_pod_status_unschedulable{namespace="%s"} == 1)%s'
        % (sel, sel, ns, " / 1024/1024/1024" if resource == "memory" else "")
    )


def require(name):
    v = os.environ.get(name)
    if not v:
        sys.exit("metalnap: %s is required (see `python -m metalnap --help`)" % name)
    return v


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    nodes = [n.strip() for n in require("NODES").split(",") if n.strip()]
    ns = os.environ.get("ARC_NAMESPACE", "arc-runners")
    host_fmt = require("BMC_HOST_FMT")
    kube = Kube()

    sat_q = os.environ.get("SATURATION_QUERY", ARC_SATURATION_QUERY)
    # Sized per resource: this pool's runners run out of CPU well before
    # memory, and sizing on memory alone woke about half the nodes a backlog
    # needed. CPU_SHORTFALL_QUERY="" goes back to memory alone.
    queries = {"memory": os.environ.get("SHORTFALL_QUERY")
               or default_shortfall_query(ns)}
    cpu_q = os.environ.get("CPU_SHORTFALL_QUERY")
    if cpu_q is None:
        cpu_q = default_shortfall_query(ns, "cpu")
    if cpu_q:
        queries["cpu"] = cpu_q

    # Silencing is opt-in by URL, but strongly recommended: without it every
    # sleep looks like a node dying and pages someone.
    am = os.environ.get("ALERTMANAGER_URL")
    notifier = None
    if am:
        labels = [l.strip() for l in os.environ.get(
            "ALERTMANAGER_SILENCE_LABELS", "instance,node").split(",")
            if l.strip()]
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
    taint = os.environ.get("BURST_TAINT_KEY", "ci-burst")
    warmup = ImagePrepull(
        kube, warm_image, namespace=ns,
        tolerations=[{"key": taint, "operator": "Equal", "value": "true",
                      "effect": "NoSchedule"}],
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
            fit_check=PendingPodFit(kube, ns, toleration_key=taint)),
        drain=ArcDrain(kube, namespace=ns),
        config=Config(),
    ).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
