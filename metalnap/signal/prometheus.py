"""A PromQL-backed DemandSignal. The reference implementation."""
import requests


class PrometheusSignal:
    def __init__(self, url, shortfall_query, saturation_query=None,
                 timeout=20):
        self.url = url.rstrip("/")
        self.shortfall_query = shortfall_query
        self.saturation_query = saturation_query
        self.timeout = timeout

    def _scalar(self, query):
        r = requests.get(self.url + "/api/v1/query",
                         params={"query": query}, timeout=self.timeout)
        r.raise_for_status()
        res = r.json()["data"]["result"]
        # An EMPTY result means "nothing matched", which for both of these
        # queries means zero. Note that a query using `and on(...)` produces no
        # series at all when nothing matches -- absent, not zero -- so this
        # branch is load-bearing, not defensive padding.
        return float(res[0]["value"][1]) if res else 0.0

    def shortfall(self):
        return self._scalar(self.shortfall_query)

    def saturated_units(self):
        if not self.saturation_query:
            return 0
        return int(self._scalar(self.saturation_query))
