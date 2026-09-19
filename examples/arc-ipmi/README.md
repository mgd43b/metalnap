# Reference wiring: GitHub ARC runners on bare metal, powered over IPMI

This is the deployment metalnap was extracted from — two Supermicro Twin nodes
serving CI, asleep most of the day.

```python
from metalnap import Config, Controller
from metalnap.kube import Kube, KubeNodeSource, PendingPodFit
from metalnap.notify import AlertmanagerNotifier
from metalnap.power import IpmiPower
from metalnap.signal import PrometheusSignal
from metalnap.drain import ArcDrain
from metalnap.drain.arc import ARC_SATURATION_QUERY
import os

kube = Kube()

# Unschedulable pod memory: work the scheduler admitted but cannot place.
# NOTE the `and on(pod)` -- when nothing matches, this yields NO SERIES rather
# than zero, which PrometheusSignal reads as 0.0. That is deliberate and the
# distinction matters: absent means idle here, not broken.
SHORTFALL = (
    'sum(kube_pod_container_resource_requests'
    '{namespace="arc-runners",resource="memory"} '
    'and on(pod) kube_pod_status_unschedulable{namespace="arc-runners"} == 1)'
    ' / 1024/1024/1024'
)

Controller(
    nodes=["node1", "node2"],
    node_source=KubeNodeSource(kube, annotation="metalnap.io/cordoned"),
    power=IpmiPower(host_for=lambda n: f"{n}-ipmi.internal.example.org",
                    user=os.environ["BMC_USER"],
                    password=os.environ["BMC_PASS"]),
    signal=PrometheusSignal(os.environ["PROM_URL"], SHORTFALL,
                            ARC_SATURATION_QUERY,
                            # Without it, "does the waiting work fit here?"
                            # always answers yes.
                            fit_check=PendingPodFit(
                                kube, "arc-runners",
                                # Whole, as `kubectl taint` below sets it.
                                taint={"key": "ci-burst", "value": "true",
                                       "effect": "NoSchedule"})),
    drain=ArcDrain(kube, namespace="arc-runners"),
    # Optional in the protocol, not in practice: without it every sleep looks
    # like a node dying. It mutes only nodes metalnap put down, and raises
    # MetalnapNodeNeedsAttention when one needs a human.
    notifier=AlertmanagerNotifier(os.environ["ALERTMANAGER_URL"]),
    config=Config(),
).run_forever()
```

## RBAC

The controller needs very little, and what it is *denied* matters more than
what it is granted:

```yaml
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list", "patch"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]          # NOT delete, NOT create pods/eviction
- apiGroups: ["actions.github.com"]
  resources: ["ephemeralrunners"]
  verbs: ["get", "list", "delete"]
```

`delete` on `ephemeralrunners` is how idle runners are released — ARC's own
scale-down path, which deregisters the runner from GitHub *before* removing the
pod. There is deliberately **no** `pods/delete` and no `pods/eviction`:
evicting runner pods directly is what destroyed four live CI jobs, and the
controller should not be able to do it even if a future bug tells it to.

If you also grant pod `create`/`delete` for some scratch pod of your own, scope
the delete with `resourceNames`. An unqualified grant silently hands back the
permission the ClusterRole above is carefully withholding — we shipped exactly
that mistake.

## Taint the burst nodes

```
kubectl taint node node1 node2 ci-burst=true:NoSchedule
```

Taint answers *what may land here*; cordon answers *when*. Both are needed:
cordon alone lets any Deployment schedule onto a node you are about to power
off. Give the workloads you want there a matching toleration, and no
nodeSelector — a toleration permits, it does not pull. metalnap counts only
pending work whose toleration matches the whole taint — key, value and effect,
as the scheduler matches it — so keep `burstTaintKey`, `burstTaintValue` and
`burstTaintEffect` in step with the taint above.
