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
    ARC_NAMESPACE           default arc-runners
    CORDON_ANNOTATION       default metalnap.io/cordoned
    SHORTFALL_QUERY         override the default PromQL
    SATURATION_QUERY        override, or "" to disable the saturation term
    ... plus every timer in metalnap/config.py
"""
import os
import sys

from . import Config, Controller
from .drain import ArcDrain
from .drain.arc import ARC_SATURATION_QUERY
from .kube import Kube, KubeNodeSource
from .power import IpmiPower
from .signal import PrometheusSignal


def default_shortfall_query(ns):
    # Memory of pods the scheduler admitted but cannot place.
    #
    # `and on(pod)` means this yields NO SERIES when nothing is unschedulable,
    # rather than a zero sample. PrometheusSignal reads an empty result as 0.0,
    # which is correct here -- but it is a real distinction, and a query that
    # returns nothing on the happy path surprises people who did not write it.
    return (
        'sum(kube_pod_container_resource_requests'
        '{namespace="%s",resource="memory"} '
        'and on(pod) kube_pod_status_unschedulable{namespace="%s"} == 1)'
        ' / 1024/1024/1024' % (ns, ns)
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

    Controller(
        nodes=nodes,
        node_source=KubeNodeSource(
            kube, annotation=os.environ.get("CORDON_ANNOTATION",
                                            "metalnap.io/cordoned")),
        power=IpmiPower(host_for=lambda n: host_fmt.format(node=n),
                        user=require("BMC_USER"), password=require("BMC_PASS")),
        signal=PrometheusSignal(
            require("PROM_URL"),
            os.environ.get("SHORTFALL_QUERY") or default_shortfall_query(ns),
            sat_q or None),
        drain=ArcDrain(kube, namespace=ns),
        config=Config(),
    ).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
