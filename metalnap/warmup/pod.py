"""Pull a container image onto a node before it takes work. Reference Warmup."""


class ImagePrepull:
    """
    Runs a one-shot Pod pinned to the node, so its image lands in the local
    cache. Measured on the deployment this came from: 450s cold, 0.69s warm.

    Pinned with `nodeName` rather than a selector, deliberately -- it bypasses
    the scheduler, which would refuse a node that is cordoned or tainted, and
    those are exactly the states this runs in.
    """

    def __init__(self, kube, image, namespace="default", tolerations=None,
                 image_pull_secrets=None, name_prefix="metalnap-warmup"):
        self.kube, self.image, self.ns = kube, image, namespace
        self.tolerations = tolerations or []
        self.pull_secrets = image_pull_secrets or []
        self.prefix = name_prefix

    def _name(self, node):
        return "%s-%s" % (self.prefix, node)

    def _path(self, node):
        return "/api/v1/namespaces/%s/pods/%s" % (self.ns, self._name(node))

    def start(self, node):
        self.cleanup(node)               # a leftover from a previous attempt
        self.kube.request("POST", "/api/v1/namespaces/%s/pods" % self.ns, {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": self._name(node),
                         "labels": {"app": self.prefix}},
            "spec": {
                "nodeName": node,
                "restartPolicy": "Never",
                "tolerations": self.tolerations,
                "imagePullSecrets": self.pull_secrets,
                "containers": [{
                    "name": "warmup", "image": self.image,
                    "command": ["true"],
                    "resources": {"requests": {"cpu": "10m",
                                               "memory": "32Mi"}},
                }],
            },
        })

    def done(self, node):
        try:
            phase = self.kube.request("GET", self._path(node))["status"].get("phase")
        except Exception:                # noqa: BLE001
            return False                 # cannot tell => not finished
        # Failed counts as done: the image either pulled or it will not, and
        # either way there is nothing further to wait for.
        return phase in ("Succeeded", "Failed")

    def cleanup(self, node):
        self.kube.delete(self._path(node))
