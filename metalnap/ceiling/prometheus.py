"""A PromQL-backed CapacityCeiling."""
import math

from ..signal.prometheus import instant_query


class PrometheusCeiling:
    """The most managed nodes that may be awake, read off a PromQL query.

    The query's VALUE is the ceiling and NO SERIES MEANS NO CEILING, which is
    what lets an operator write a gate:

        vector(0) and on() (max(ipmi_temperature_celsius) > 35)

    -- no series on every ordinary day, and 0 while it is hot. That is the
    reverse of how PrometheusSignal reads an empty result (as 0.0, "nothing
    waiting", which is right for demand), and the difference is the whole
    safety of this class: reading an empty result as 0 here is an order to
    power the pool off on the first day the signal is quiet. So it parses for
    itself, and a test pins empty to None.

    Three outcomes, and the second two must never be confused with the first:
    a number of nodes; None, "there is no ceiling"; and an exception, "cannot
    tell", which the controller reads as no ceiling too -- loudly, and
    releasing one that is engaged. A value that is not a number of nodes (NaN,
    infinity, a negative) is the exception, not a clamp: a reading that cannot
    be trusted must not power anything off.

    Staleness is not this class's to detect. An instant query stamps its answer
    with the time it was evaluated, not the time of the sample, so the age of
    the underlying metric cannot be read off the response. End the expression
    with a freshness guard instead -- `... and on() (time() - timestamp(m) <
    120)` -- and a stale metric returns no series, which is no ceiling.
    """

    def __init__(self, url, query, timeout=20):
        self.url = url.rstrip("/")
        self.query = query
        self.timeout = timeout

    def limit(self):
        data = instant_query(self.url, self.query, self.timeout)
        kind, result = data["resultType"], data["result"]
        if kind == "scalar":                 # `query: "2"`: [ts, "2"]
            samples = [result]
        elif kind == "vector":
            samples = [series["value"] for series in result]
        else:
            raise ValueError("a capacity ceiling query must return an instant "
                             "vector or a scalar, not a %s" % kind)
        # Every sample is read, and one that cannot be makes the whole reading
        # unavailable: better a loud fail-open than a minimum taken over what
        # happened to be readable. Several series take the MINIMUM -- the most
        # restrictive -- because the order Prometheus returns them in is not
        # defined, so an unaggregated per-UPS expression just works.
        nodes = [_nodes(sample[1]) for sample in samples]
        return min(nodes) if nodes else None


def _nodes(value):
    """A sample's value as a whole number of nodes. Fractions floor."""
    x = float(value)                        # the API sends it as a string
    if not math.isfinite(x) or x < 0:
        raise ValueError("%r is not a number of nodes" % (value,))
    return math.floor(x)
