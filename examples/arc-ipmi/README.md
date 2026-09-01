# Reference wiring: GitHub ARC runners on bare metal, powered over IPMI

This is the deployment metalnap was extracted from — two Supermicro Twin nodes
serving CI, asleep most of the day.

```python
from metalnap import Config, Controller
from metalnap.kube import Kube, KubeNodeSource
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
    nodes=["k8s14", "k8s15"],
    node_source=KubeNodeSource(kube, annotation="metalnap.io/cordoned"),
    power=IpmiPower(host_for=lambda n: f"{n}-ipmi.internal.example.org",
                    user=os.environ["BMC_USER"],
                    password=os.environ["BMC_PASS"]),
    signal=PrometheusSignal(os.environ["PROM_URL"], SHORTFALL,
                            ARC_SATURATION_QUERY),
    drain=ArcDrain(kube, namespace="arc-runners"),
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
kubectl taint node k8s14 k8s15 ci-burst=true:NoSchedule
```

Taint answers *what may land here*; cordon answers *when*. Both are needed:
cordon alone lets any Deployment schedule onto a node you are about to power
off. Give the workloads you want there a matching toleration, and no
nodeSelector — a toleration permits, it does not pull.
