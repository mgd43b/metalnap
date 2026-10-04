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
    passes `deadline`, seconds of elapsed time, and the body is then read
    against the clock (see _read_before): the clock is checked after every read,
    each of which returns as soon as any bytes arrive, no redirect is followed,
    and each socket wait is at most half the deadline. What that bounds, and
    what it does not: the read is over within the deadline plus at most one
    socket wait (half the deadline) -- once the response headers are in. Not
    bounded by it: DNS resolution, and a server that trickles the response
    HEADERS themselves (each recv is its own half-deadline wait; a header block
    is capped at 64 KiB and 100 lines by http.client, so the worst case is
    finite but long). Without `deadline` nothing here changes.
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


#: The most a deadline-bound answer may be. An instant query's answer is small;
#: this stops a server that never stops from also filling memory.
_MAX_BODY = 8 * 1024 * 1024


def _read_before(path, query, socket_timeout, deadline):
    """The JSON body of a query, read against a deadline on the clock.

    Control comes back to the clock after every read, which needs a read that
    returns as soon as ANY bytes have arrived: `read1()` (urllib3 2.x). The
    chunked `iter_content(8192)` blocks until 8 KiB have arrived or the body
    ends, so a server that sends a byte every few tens of milliseconds never
    trips a socket timeout and never gives control back. Where `read1` is
    missing (urllib3 1.x) it reads one byte at a time, which is slow for a big
    body and exact for an answer this size.

    No redirect is followed (`requests` would, each hop with socket timeouts of
    its own, none of them this deadline's) and none is expected from a
    Prometheus; a 3xx is an error. The body is asked for uncompressed, because
    `read1` hands back what is on the wire and an encoded one is refused.
    """
    end = time.monotonic() + deadline
    r = requests.get(path, params={"query": query},
                     timeout=(socket_timeout, socket_timeout), stream=True,
                     allow_redirects=False,
                     headers={"Accept-Encoding": "identity"})
    try:
        if 300 <= r.status_code < 400:
            raise ValueError("prometheus redirected (HTTP %d) and a redirect "
                             "is not followed" % r.status_code)
        r.raise_for_status()
        encoding = (r.headers.get("Content-Encoding") or "identity").lower()
        if encoding != "identity":
            raise ValueError("prometheus answered with a %r content encoding "
                             "that was not asked for" % encoding)
        read = getattr(r.raw, "read1", None) or (lambda n: r.raw.read(1))
        chunks, size = [], 0
        while True:
            chunk = read(4096)
            if time.monotonic() > end:
                raise TimeoutError("prometheus did not finish answering "
                                   "within the %gs deadline" % deadline)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_BODY:
                raise ValueError("prometheus answered with more than %d "
                                 "bytes" % _MAX_BODY)
            chunks.append(chunk)
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
