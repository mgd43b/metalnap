"""Minimal Kubernetes client and a NodeSource over it.

Deliberately not the official client: this needs six calls, and a REST client
with no dependency surface keeps the image small and the failure modes legible.
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


def mem_to_gib(v):
    v = str(v)
    for suffix, mult in (("Ki", 1 / 1048576), ("Mi", 1 / 1024), ("Gi", 1.0),
                         ("Ti", 1024.0)):
        if v.endswith(suffix):
            return float(v[:-2]) * mult
    return float(v) / (1024 ** 3)


class KubeNodeSource:
    """NodeState from the Kubernetes API, with cordon ownership by annotation."""

    def __init__(self, kube, annotation, capacity_of=None):
        self.kube = kube
        #: Presence of this annotation marks a cordon as ours. Anything else is
        #: an operator's, and is never touched.
        self.annotation = annotation
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
        ready, ready_since = False, None
        for c in n["status"].get("conditions", []):
            if c["type"] == "Ready":
                ready = c["status"] == "True"
                if ready:
                    from datetime import datetime
                    try:
                        ready_since = datetime.fromisoformat(
                            c["lastTransitionTime"].replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:          # noqa: BLE001
                        ready_since = None
        anns = n["metadata"].get("annotations") or {}
        ours_since = None
        if anns.get(self.annotation):
            from datetime import datetime
            try:
                ours_since = datetime.fromisoformat(
                    anns[self.annotation].replace("Z", "+00:00")).timestamp()
            except Exception:                  # noqa: BLE001
                ours_since = None
        return NodeState(
            ready=ready,
            cordoned=bool(n["spec"].get("unschedulable")),
            ours=self.annotation in anns,
            ready_since=ready_since,
            capacity=self.capacity_of(n),
            ours_since=ours_since,
        )

    def set_cordon(self, name, cordoned):
        from datetime import datetime, timezone
        # Ownership and the cordon move together, in ONE patch. Split across
        # two calls, a crash between them leaves a cordon nobody claims.
        self.kube.request("PATCH", "/api/v1/nodes/" + name, {
            "spec": {"unschedulable": bool(cordoned)},
            "metadata": {"annotations": {
                self.annotation: (datetime.now(timezone.utc).isoformat()
                                  if cordoned else None)}},
        })
