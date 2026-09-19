"""Minimal Kubernetes client and a NodeSource over it.

Deliberately not the official client: this needs a few verbs on a few
resources, and a REST client with no dependency surface keeps the image small
and the failure modes legible.
"""
import json
import requests

from .types import NodeState

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"


class Kube:
    def __init__(self, api=API, sa=SA, timeout=30):
        self.api, self.sa, self.timeout = api, sa, timeout

    def request(self, method, path, body=None):
        with open(self.sa + "/token") as f:
            token = f.read().strip()
        ctype = ("application/strategic-merge-patch+json"
                 if method == "PATCH" else "application/json")
        r = requests.request(
            method, self.api + path,
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": ctype},
            data=json.dumps(body) if body is not None else None,
            verify=self.sa + "/ca.crt", timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.text else {}

    def delete(self, path):
        """DELETE where 'already gone' is success.

        A 404 from a concurrent deletion means the desired end state was
        reached. Treating it as an error aborts whatever sequence is running
        and burns a retry for no reason.
        """
        try:
            self.request("DELETE", path)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return
            raise


#: Every suffix a Kubernetes quantity may carry. Binary first: "Mi" must not
#: be read as "M" followed by junk.
_SUFFIX = (("Ki", 2 ** 10), ("Mi", 2 ** 20), ("Gi", 2 ** 30), ("Ti", 2 ** 40),
           ("Pi", 2 ** 50), ("Ei", 2 ** 60),
           ("n", 1e-9), ("u", 1e-6), ("m", 1e-3), ("k", 1e3), ("M", 1e6),
           ("G", 1e9), ("T", 1e12), ("P", 1e15), ("E", 1e18))


def quantity(v):
    """A Kubernetes quantity -- "129Mi", "1G", "500m", "12e6", 4 -- as a number.

    All three forms the API accepts: binary-SI, decimal-SI and a bare or
    exponent number. Reading only some of them raised on a valid request like
    "1G", and on the default demand path that failed the whole tick.
    """
    v = str(v).strip()
    for suffix, mult in _SUFFIX:
        if v.endswith(suffix) and not v[:-len(suffix)].endswith(("e", "E")):
            return float(v[:-len(suffix)]) * mult
    return float(v)                      # plain, or an exponent: "12e6"


def mem_to_gib(v):
    return quantity(v) / 2 ** 30


def cpu_to_cores(v):
    return quantity(v)


#: How each resource is read off a Kubernetes quantity, in the unit its
#: shortfall is reported in: GiB of memory, cores of CPU.
QUANTITY = {"memory": mem_to_gib, "cpu": cpu_to_cores}


def effective_requests(spec, resources=("memory", "cpu")):
    """What the scheduler reserves for a pod, per resource.

    Kubernetes' own formula, so a shortfall and a fit check agree with the
    scheduler they are second-guessing: the larger of the steady state --
    every app container plus every native sidecar, an init container with
    restartPolicy Always that keeps running -- and the init phase, where each
    ordinary init container runs beside the sidecars started before it. Plus
    the pod's overhead.

    Counting app containers alone missed a runner's dind sidecar: half of
    every backlog. Summing every init container instead over-counts one that
    only runs first.
    """
    out = {}
    for r in resources:
        def req(c, q=QUANTITY[r], r=r):
            return q(c.get("resources", {}).get("requests", {}).get(r, "0"))
        sidecars = init_peak = 0.0
        for c in spec.get("initContainers", []):
            if c.get("restartPolicy") == "Always":
                sidecars += req(c)
                init_peak = max(init_peak, sidecars)
            else:
                init_peak = max(init_peak, sidecars + req(c))
        steady = sum(req(c) for c in spec.get("containers", [])) + sidecars
        out[r] = (max(steady, init_peak)
                  + QUANTITY[r](spec.get("overhead", {}).get(r, "0")))
    return out


def tolerates(pod, key):
    """Could this pod land on a node carrying taint `key`? True with no key."""
    if not key:
        return True
    return any(t.get("key") == key
               or (t.get("operator") == "Exists" and not t.get("key"))
               for t in pod["spec"].get("tolerations", []))


def _unschedulable(pod):
    return any(c.get("type") == "PodScheduled" and c.get("status") == "False"
               and c.get("reason") == "Unschedulable"
               for c in pod.get("status", {}).get("conditions", []))


class PendingPodShortfall:
    """Unmet demand, read off the pods the scheduler tried and failed to place.

    The default shortfall. A metrics pipeline cannot give the scheduler's
    effective request for such a pod: kube-state-metrics labels which init
    containers are sidecars from container STATUS, and a pod that was never
    scheduled has none. The pod spec says so directly.

    `of(resource)` is one resource's shortfall -- memory in GiB, CPU in cores
    -- for PrometheusSignal to use as a source.

    `toleration_key` as for PendingPodFit, and for the same reason: a pod that
    does not tolerate the taint keeping work off these nodes can never land
    on one, so it is no demand for them -- counted, it wakes nodes for work
    they cannot run.
    """

    def __init__(self, kube, namespace, toleration_key=None):
        self.kube, self.ns = kube, namespace
        self.toleration_key = toleration_key

    def of(self, resource):
        def shortfall():
            pods = self.kube.request(
                "GET", "/api/v1/namespaces/%s/pods?fieldSelector="
                       "status.phase=Pending" % self.ns)
            return sum(effective_requests(p["spec"], (resource,))[resource]
                       for p in pods.get("items", [])
                       if not p["spec"].get("nodeName") and _unschedulable(p)
                       and tolerates(p, self.toleration_key))
        return shortfall


def allocatable(resources=("memory", "cpu")):
    """A KubeNodeSource `capacity_of` that reports several resources.

    Pair it with a demand signal that reports a shortfall for the same ones,
    and the pool is sized on whichever runs out first. The default stays
    memory alone, as a bare number, so a signal written for that keeps its
    meaning.
    """
    def capacity_of(n):
        alloc = n["status"].get("allocatable", {})
        return {r: QUANTITY[r](alloc.get(r, "0")) for r in resources}
    return capacity_of


class KubeNodeSource:
    """NodeState from the Kubernetes API, with cordon ownership by annotation."""

    #: Labels that mark a node as never power-manageable. A cluster does not
    #: survive losing its control plane, and no configuration mistake should be
    #: able to make that happen.
    PROTECTED_LABELS = ("node-role.kubernetes.io/control-plane",
                        "node-role.kubernetes.io/master")

    #: Where the controller's durable notes live: `<prefix><key>`, so
    #: metalnap.io/power-cycled and friends. Not derived from the cordon
    #: annotation: that one is configurable so a predecessor's name can be
    #: kept, and these have no predecessor.
    NOTE_PREFIX = "metalnap.io/"

    def __init__(self, kube, annotation, capacity_of=None,
                 protected_labels=None, note_prefix=None):
        self.protected_labels = (protected_labels
                                 if protected_labels is not None
                                 else self.PROTECTED_LABELS)
        self.kube = kube
        #: Presence of this annotation marks a cordon as ours. Anything else is
        #: an operator's, and is never touched.
        self.annotation = annotation
        self.note_prefix = note_prefix or self.NOTE_PREFIX
        self.capacity_of = capacity_of or (
            lambda n: mem_to_gib(n["status"].get("allocatable", {})
                                 .get("memory", "0")))

    def state(self, name):
        try:
            n = self.kube.request("GET", "/api/v1/nodes/" + name)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None          # not racked yet is not an error
            raise
        ready, ready_since, down_since = False, None, None
        for c in n["status"].get("conditions", []):
            if c["type"] == "Ready":
                # Note "True", not truthiness: a node whose kubelet has stopped
                # heartbeating goes to "Unknown", not "False", and that is the
                # state a powered-off node actually sits in.
                ready = c["status"] == "True"
                ts = _timestamp(c.get("lastTransitionTime"))
                # One condition, one transition time: it is when the node
                # became Ready, or when it stopped being. Which of the two it
                # is depends entirely on where the condition stands now.
                ready_since, down_since = (ts, None) if ready else (None, ts)
        anns = n["metadata"].get("annotations") or {}
        labels = n["metadata"].get("labels") or {}
        notes = {k[len(self.note_prefix):]: v for k, v in anns.items()
                 if k.startswith(self.note_prefix) and k != self.annotation}
        return NodeState(
            ready=ready,
            cordoned=bool(n["spec"].get("unschedulable")),
            ours=self.annotation in anns,
            ready_since=ready_since,
            capacity=self.capacity_of(n),
            protected=any(l in labels for l in self.protected_labels),
            ours_since=_timestamp(anns.get(self.annotation)),
            down_since=down_since,
            power_cycled_at=_timestamp(notes.get("power-cycled")),
            visited_at=_timestamp(notes.get("visited")),
            shutdown_at=_timestamp(notes.get("shutdown")),
            trouble=notes.get("trouble") or None,
        )

    def set_cordon(self, name, cordoned):
        # Ownership and the cordon move together, in ONE patch. Split across
        # two calls, a crash between them leaves a cordon nobody claims.
        self.kube.request("PATCH", "/api/v1/nodes/" + name, {
            "spec": {"unschedulable": bool(cordoned)},
            "metadata": {"annotations": {
                self.annotation: _now_iso() if cordoned else None}},
        })

    def note(self, name, key, value):
        # Metadata only. The cordon is not ours to touch here: the node noted
        # is as likely to be an uncordoned one that crashed in service as one
        # we put to sleep. None deletes the annotation; a timestamp is written
        # as RFC 3339, for whoever reads it off `kubectl describe node`.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            from datetime import datetime, timezone
            value = datetime.fromtimestamp(value, timezone.utc).isoformat()
        self.kube.request("PATCH", "/api/v1/nodes/" + name, {
            "metadata": {"annotations": {self.note_prefix + key: value}},
        })

    def disown(self, name):
        self.kube.request("PATCH", "/api/v1/nodes/" + name, {
            "metadata": {"annotations": {self.annotation: None}},
        })


def _timestamp(value):
    """An RFC 3339 string as unix time; None for absent or unparseable.

    Unparseable reads as absent rather than raising: every caller treats None
    as "no durable evidence", and the fallbacks for that are all safe.
    """
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:                          # noqa: BLE001
        return None


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class PendingPodFit:
    """
    Would at least one PENDING pod actually run on a node of this size?

    Borrowed from Cluster Autoscaler's scale-up simulation. A demand signal is
    usually a sum, which quietly assumes everything waiting is waiting on
    capacity -- a pod stuck on a nodeSelector, an unbound PVC, or a taint it
    does not tolerate inflates that sum and powers on hardware that cannot help
    it.

    `toleration_key` matters: a pod that does not tolerate the taint keeping
    work off your sleepable nodes can never land there, however much room the
    node has.

    Capacity is a bare number of GiB, or {resource: amount} -- and then a pod
    fits only if EVERY resource named fits: a runner that needs 8 cores does
    not run on a node with 4, whatever its memory.
    """

    def __init__(self, kube, namespace, toleration_key=None):
        self.kube, self.ns, self.toleration_key = kube, namespace, toleration_key

    def __call__(self, capacity):
        if not isinstance(capacity, dict):
            capacity = {"memory": capacity}
        pods = self.kube.request(
            "GET", "/api/v1/namespaces/%s/pods?fieldSelector=status.phase=Pending"
                   % self.ns)
        for p in pods.get("items", []):
            if p["spec"].get("nodeName"):
                continue                      # already placed
            if not tolerates(p, self.toleration_key):
                continue
            # The scheduler's effective request, init-phase floor and all: a pod
            # whose init container needs more than its steady state does not
            # fit a node that only holds the steady state.
            need = effective_requests(
                p["spec"], [r for r in capacity if r in QUANTITY])
            if all(need[r] <= cap for r, cap in capacity.items()
                   if r in QUANTITY):
                return True
        return False
