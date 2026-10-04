"""A PromQL-backed DemandSignal. The reference implementation."""
import json
import time

import requests


def instant_query(url, query, timeout, deadline=None):
    """The `data` of an instant query: {"resultType": ..., "result": ...}.

    The ONE place this package asks Prometheus a question, so that how it
    reaches it -- the URL, the timeout, and whatever auth or TLS it grows --
    cannot drift between the demand signal and the capacity ceiling, which read
    the answer very differently (an empty result is zero to one and "no
    ceiling" to the other) but must reach it the same way.

    `timeout` is `requests`': it applies to each socket operation (connect, the
    wait for headers, each read), so a server that sends a byte now and then is
    never a timeout to it and can hold the caller for as long as it likes. A
    caller that must not be held -- the capacity ceiling is read on the tick --
    passes `deadline`, seconds of elapsed time for the whole read. The answer
    is then streamed and read a chunk at a time against the clock, a read that
    outlasts it raises TimeoutError, and each socket operation waits at most
    half of it, so connecting and waiting for headers cannot use more than the
    deadline between them. One chunk read in progress when the deadline passes
    can still finish (at most half the deadline more), so the bound is the
    deadline plus that. Without `deadline` nothing here changes.
    """
    path = url.rstrip("/") + "/api/v1/query"
    if deadline is None:
        r = requests.get(path, params={"query": query}, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    else:
        body = _read_before(path, query, min(timeout, deadline) / 2.0,
                            deadline)
    # A body that says it failed is a failure whatever else it carries. Absent
    # is accepted: the answers this has always read carry no status of their
    # own, and an answer with no `data` fails on the next line anyway.
    if body.get("status", "success") != "success":
        raise ValueError("prometheus answered %r: %s" % (
            body.get("status"), body.get("error", "")))
    return body["data"]


def _read_before(path, query, socket_timeout, deadline):
    """The JSON body of a query, read against a deadline on the clock."""
    end = time.monotonic() + deadline
    r = requests.get(path, params={"query": query},
                     timeout=(socket_timeout, socket_timeout), stream=True)
    try:
        r.raise_for_status()
        chunks = []
        for chunk in r.iter_content(chunk_size=8192):
            if time.monotonic() > end:
                raise TimeoutError("prometheus did not finish answering "
                                   "within the %gs deadline" % deadline)
            chunks.append(chunk)
        if time.monotonic() > end:
            raise TimeoutError("prometheus did not finish answering within "
                               "the %gs deadline" % deadline)
        return json.loads(b"".join(chunks))
    finally:
        r.close()


class PrometheusSignal:
    def __init__(self, url, shortfall_query, saturation_query=None,
                 timeout=20, fit_check=None):
        self.url = url.rstrip("/")
        #: A PromQL query or a callable returning the amount, or {resource:
        #: either} to size on several -- which then needs a NodeSource that
        #: reports capacity for the same resources. The reference wiring reads
        #: unmet demand off the pods themselves (kube.PendingPodShortfall) and
        #: keeps PromQL for overrides.
        self.shortfall_query = shortfall_query
        self.saturation_query = saturation_query
        self.timeout = timeout
        #: Optional callable(capacity) -> bool. Prometheus can tell you HOW
        #: MUCH work is waiting but not WHY, so the fit question needs a
        #: different source -- see PendingPodFit in metalnap/kube.py.
        self.fit_check = fit_check

    def _scalar(self, query):
        res = instant_query(self.url, query, self.timeout)["result"]
        # An EMPTY result means "nothing matched", which for both of these
        # queries means zero. Note that a query using `and on(...)` produces no
        # series at all when nothing matches -- absent, not zero -- so this
        # branch is load-bearing, not defensive padding.
        return float(res[0]["value"][1]) if res else 0.0

    def _source(self, source):
        return float(source()) if callable(source) else self._scalar(source)

    def shortfall(self):
        if isinstance(self.shortfall_query, dict):
            return {r: self._source(q) for r, q in self.shortfall_query.items()}
        return self._source(self.shortfall_query)

    def saturated_units(self):
        if not self.saturation_query:
            return 0
        return int(self._scalar(self.saturation_query))

    def fits_node(self, capacity):
        if self.fit_check is None:
            # No way to tell. True means "go ahead", which risks waking for
            # work that cannot land -- the alternative, refusing every wake,
            # is worse.
            return True
        return bool(self.fit_check(capacity))
