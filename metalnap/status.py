"""The status object: what the controller has concluded, for `metalnap status`.

`metalnap status` reads node annotations and the Deployment's configuration.
What the controller is DOING lives in its memory, and the one controller-wide
fact an operator needs at a terminal -- is a capacity ceiling engaged, and what
for -- belongs to no node. So the controller publishes it to one ConfigMap,
which the chart creates empty and grants `get`/`update` on by name, and which
the CLI reads with the same `get configmap` it already uses for the
controller's configuration.

It is REPORTING, not control. Nothing the controller decides is read from it, it
is written after the tick's decisions are made, a failed write is logged and the
next tick tries again, and it is never written in dry_run, which must not mutate
anything outside the process -- a shadow would overwrite the live controller's.
Per-node facts stay on the nodes, where `status` already finds them without
asking anyone's memory.
"""
import json
import time

#: The key in the ConfigMap's data. One key, so more can be added beside it
#: without a reader of this one noticing.
KEY = "ceiling"

#: A report that has not changed is written again after this long anyway, so
#: that its age says whether the controller is alive. `status` calls one three
#: times older than this stale.
STATUS_REFRESH_S = 300


class ConfigMapStatus:
    def __init__(self, kube, namespace, name, clock=time.time,
                 refresh_s=STATUS_REFRESH_S):
        self.kube, self.namespace, self.name = kube, namespace, name
        self.clock, self.refresh_s = clock, refresh_s
        self._written = None            # (what was written, when)

    def publish(self, report):
        """Write `report`, unless it is what is already there and still fresh.

        Raises on failure, so the controller can say so once -- and does not
        remember a write that failed as one that happened.

        A replace, not a patch: the chart grants get and update, and a patch is
        a third verb for one object. The object is read first, so its other
        keys and its resourceVersion are kept.
        """
        now = self.clock()
        core = json.dumps(report, sort_keys=True)
        if (self._written is not None and self._written[0] == core
                and now - self._written[1] < self.refresh_s):
            return
        path = "/api/v1/namespaces/%s/configmaps/%s" % (self.namespace,
                                                         self.name)
        cm = self.kube.request("GET", path)
        data = dict(cm.get("data") or {})
        data[KEY] = json.dumps(dict(report, updated=now,
                                    refresh_s=self.refresh_s), sort_keys=True)
        cm["data"] = data
        self.kube.request("PUT", path, cm)
        self._written = (core, now)
