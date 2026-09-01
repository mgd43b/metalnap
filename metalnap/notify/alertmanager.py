"""Alertmanager silences. The reference Notifier."""
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests


class AlertmanagerNotifier:
    """
    Silence a node's alerts while it is deliberately down.

    Both methods are idempotent and self-healing, because the controller calls
    them every tick rather than on transitions: a silence lost to a restart is
    re-created, and -- the half people forget -- a silence left over on a node
    that is UP is expired, so a genuine failure of it is not swallowed by a
    silence the controller forgot to clean up.
    """

    def __init__(self, url, matchers=None, hours=12, timeout=20,
                 created_by="metalnap"):
        self.url = url.rstrip("/")
        #: Extra label matchers. `instance`/`node` is added per node.
        self.matchers = matchers or []
        self.hours, self.timeout, self.created_by = hours, timeout, created_by

    def _comment(self, node):
        return "metalnap: %s is deliberately powered down" % node

    def _find(self, node):
        r = requests.get(self.url + "/api/v2/silences", timeout=self.timeout)
        r.raise_for_status()
        return [s for s in r.json()
                if s.get("status", {}).get("state") in ("active", "pending")
                and s.get("comment") == self._comment(node)]

    def going_down(self, node):
        if self._find(node):
            return                       # already silenced; nothing to do
        now = datetime.now(timezone.utc)
        body = {
            "matchers": [{"name": "instance", "value": node,
                          "isRegex": False, "isEqual": True}] + self.matchers,
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(hours=self.hours)).isoformat(),
            "createdBy": self.created_by,
            "comment": self._comment(node),
        }
        r = requests.post(self.url + "/api/v2/silences", json=body,
                          timeout=self.timeout)
        r.raise_for_status()

    def back_up(self, node):
        for s in self._find(node):
            # NOTE the singular path. /api/v2/silences/{id} deletes one;
            # /api/v2/silence/{id} is a 404 that looks like success if you do
            # not check the status code, which is how a silence survives a wake
            # and hides the next real failure.
            sid = urllib.parse.quote(s["id"], safe="")
            r = requests.delete("%s/api/v2/silence/%s" % (self.url, sid),
                                timeout=self.timeout)
            if r.status_code == 404:
                r = requests.delete("%s/api/v2/silences/%s" % (self.url, sid),
                                    timeout=self.timeout)
            r.raise_for_status()
