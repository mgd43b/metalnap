"""
DrainPolicy for GitHub Actions Runner Controller (ARC).

The reference implementation, and the one that taught the rules. Two facts
about ARC drive the whole design:

  * `.status.jobId` is what marks a runner busy. NOT `.status.jobRequestId`,
    which does not exist on the CRD -- reading a field that is always absent
    makes every runner look idle, and draining on that destroyed four live CI
    jobs.
  * an ephemeral runner exits after it RUNS a job. One that never receives a
    job waits forever, which is exactly what a `minRunners` warm pool is. On a
    cordoned node no work is coming, so waiting for it to drain never returns.
"""
GROUP = "/apis/actions.github.com/v1alpha1/namespaces"


class ArcDrain:
    def __init__(self, kube, namespace="arc-runners",
                 pod_label="actions.github.com/scale-set-name"):
        self.kube, self.ns, self.pod_label = kube, namespace, pod_label

    def _runners_on(self, node):
        """Every EphemeralRunner whose pod sits on `node`.

        One pair of list calls answers both the busy and the idle question.
        Raises on failure -- a caller that cannot tell must not act.
        """
        ers = self.kube.request(
            "GET", "%s/%s/ephemeralrunners" % (GROUP, self.ns))
        pods = self.kube.request(
            "GET", "/api/v1/namespaces/%s/pods" % self.ns)
        on_node = {p["metadata"]["name"] for p in pods.get("items", [])
                   if p["spec"].get("nodeName") == node}
        return [er for er in ers.get("items", [])
                if er["metadata"]["name"] in on_node]

    def busy(self, node):
        return [er["metadata"]["name"] for er in self._runners_on(node)
                if er.get("status", {}).get("jobId")]

    def idle(self, node):
        return [er["metadata"]["name"] for er in self._runners_on(node)
                if not er.get("status", {}).get("jobId")]

    def holds_work(self, unit):
        """Fresh single-runner read of jobId. Raises rather than guessing."""
        cur = self.kube.request(
            "GET", "%s/%s/ephemeralrunners/%s" % (GROUP, self.ns, unit))
        return bool(cur.get("status", {}).get("jobId"))

    def release(self, unit):
        """Delete the EphemeralRunner CR -- NOT the pod.

        This is ARC's own scale-down path: it deregisters the runner from
        GitHub before tearing the pod down, so no job can be dispatched into
        something about to be destroyed. Deleting or evicting the pod directly
        is what destroyed four live jobs, and the RBAC for this deliberately
        grants no pod deletion at all.
        """
        self.kube.delete("%s/%s/ephemeralrunners/%s" % (GROUP, self.ns, unit))

    def residual(self, node):
        """Runner pods only.

        Scoped by label on purpose: this gates the power-off, so listing the
        whole namespace lets any unrelated DaemonSet keep a node awake forever.
        """
        import urllib.parse
        sel = urllib.parse.quote(self.pod_label, safe="")
        pods = self.kube.request(
            "GET", "/api/v1/namespaces/%s/pods?labelSelector=%s"
                   % (self.ns, sel))
        return [p["metadata"]["name"] for p in pods.get("items", [])
                if p["spec"].get("nodeName") == node]


#: Queue depth beyond a scale set's own ceiling is invisible to any
#: pending-pod query: ARC creates pods up to maxRunners and no further, so a
#: capped pool produces nothing unschedulable while jobs pile up on GitHub.
#: `max by(name)` on both sides is load-bearing -- re-applying a scale set
#: rolls its listener, and for the lookback window two series share a name;
#: without the aggregation the comparison fails outright, precisely when a cap
#: has just been changed.
ARC_SATURATION_QUERY = (
    "count("
    "  max by(name) (gha_desired_runners)"
    "  >= on(name)"
    "  max by(name) (gha_max_runners)"
    ") or vector(0)"
)
