"""The status object: what the controller has concluded, for `metalnap status`.

`metalnap status` reads node annotations and the Deployment's configuration.
What the controller is DOING lives in its memory, and the one controller-wide
fact an operator needs at a terminal -- is a capacity ceiling engaged, and what
for -- belongs to no node. So the controller publishes it to one ConfigMap,
which the chart creates empty and grants `get`/`update` on by name, and which
the CLI reads with the same `get configmap` it already uses for the
controller's configuration.

It is REPORTING, not control, and it is written OFF the tick path. The tick hands
the latest report to a single background thread and goes on: it never waits on
the API server for a status write, so a slow or dead one cannot delay a decision.
Nothing the controller decides is read from the object, a failed or hung write
is logged and changes nothing, and it is never written in dry_run, which must
not mutate anything outside the process -- a shadow would overwrite the live
controller's. Per-node facts stay on the nodes, where `metalnap status` already
finds them without asking anyone's memory.
"""
import json
import threading
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
        #: Why the last write failed, or None. Read by the controller, which
        #: says so once per change; set by the writer thread, from a single
        #: assignment of a string, which needs no lock to be read whole.
        self.error = None
        self._cond = threading.Condition()
        self._pending = None            # the latest report, not yet taken
        self._busy = False              # a write is in flight
        self._thread = None

    # -- the tick's side: a hand-over, never a wait ---------------------------
    def publish(self, report):
        """Hand over the latest report and return at once.

        One slot, latest value wins: a write that hangs does not queue a
        backlog behind it, and the report written when it comes back is the
        newest, not the oldest. The writer is started on the first report, a
        daemon, so a write stuck on a dead API server cannot keep the process
        alive. Whether a write worked is `error`, which the next tick reads.
        """
        with self._cond:
            self._pending = report
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="metalnap-status", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def drain(self, timeout=5.0):
        """Wait until nothing is pending or in flight; False if it timed out.
        For tests, and for a caller that wants the last report out before it
        exits. The tick never calls it."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._pending is None and not self._busy,
                timeout=timeout)

    # -- the writer's side ----------------------------------------------------
    def _run(self):
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending is not None)
                report, self._pending = self._pending, None
                self._busy = True
            try:
                self.write(report)
                self.error = None
            except Exception as e:                    # noqa: BLE001
                self.error = str(e) or type(e).__name__
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def write(self, report):
        """Write `report`, unless it is what is already there and still fresh.

        Raises on failure, and does not remember a write that failed as one
        that happened. Each call is bounded by the Kube client's own timeout,
        which the entry point sets short: that is the only thing that bounds a
        hung connection, and it bounds the writer thread's call, not the tick.

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
