"""Alertmanager silences and alerts. The reference Notifier."""
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

#: What a sleeping node's alerts identify it by, and why there are two. A
#: node's alerts do not agree on a label: kube-state-metrics alerts such as
#: KubeNodeUnreachable carry the node in `node` (their `instance` is the
#: kube-state-metrics pod), while node-exporter alerts carry it in `instance`
#: once relabelled. Matching `instance` alone silenced the second family and
#: not the first -- so every sleep still emailed KubeNodeUnreachable, and a
#: real KubeNodeNotReady from a crashed node arrived buried among them.
DEFAULT_LABELS = ("instance", "node")

#: `name="value"`, `name!="value"`, `name=~"regex"` or `name!~"regex"`, the
#: syntax amtool and Alertmanager's own UI use. The value may be left unquoted
#: when it has no spaces.
#:
#: REQUIRES Alertmanager 0.22 or later (2021): every silence carries a negative
#: matcher, and older versions drop `isEqual` and store `!=` as `=` -- which
#: would turn "anything but our own alert" into "only our own alert".
_MATCHER = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*'
    r'(?:"((?:[^"\\]|\\.)*)"|([^\s"=!~][^\s"]*))\s*$')


#: What Python's re accepts and Go's RE2 -- Alertmanager's engine -- does
#: not: lookaround, atomic groups, comments and conditionals, backreferences,
#: \Z, and possessive quantifiers. Caught at start, because the first place it
#: would surface otherwise is a 400 on the silence, and a sleep refuses to
#: power off a node it could not announce.
_NOT_RE2 = re.compile(r'\(\?(?:[=!>#(]|<[=!]|P=)|\\[1-9]|\\Z'
                      r'|(?<!\\)[*+?}]\+')
#: Inline flags RE2 understands; Python's others (a, L, u, x) it rejects.
_RE2_FLAGS = set("imsU-")


def parse_matcher(text):
    """One matcher, as the dict the v2 API takes. Raises ValueError."""
    m = _MATCHER.match(text)
    if not m:
        raise ValueError('not a matcher: %r -- want name="value", name!="value",'
                         ' name=~"regex" or name!~"regex"' % text)
    name, op, quoted, bare = m.groups()
    # Only \" and \\ are escapes of the quoting, as in amtool. Anything else
    # belongs to the value -- above all a regex's own \d or \. -- and
    # stripping it would quietly match something else.
    value = (re.sub(r'\\(["\\])', r'\1', quoted) if quoted is not None
             else bare)
    is_regex = op in ("=~", "!~")
    if is_regex:
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError("not a regex: %r in %r (%s)" % (value, text, e))
        flags = re.findall(r'\(\?([A-Za-z-]+)[:)]', value)
        if (_NOT_RE2.search(value)
                or any(set(f) - _RE2_FLAGS for f in flags)):
            raise ValueError("%r uses regex syntax Alertmanager's RE2 does "
                             "not support" % text)
    return {"name": name, "value": value, "isRegex": is_regex,
            "isEqual": op in ("=", "=~")}


def _key(matchers):
    """A matcher list as something comparable, whatever order it came in."""
    return frozenset((m["name"], m["value"], bool(m.get("isRegex")),
                      bool(m.get("isEqual", True))) for m in matchers)


class AlertmanagerNotifier:
    """
    Silence a node's alerts while it is deliberately down, and raise one of our
    own when a node needs a human.

    Every method is idempotent and self-healing, because the controller calls
    them every tick rather than on transitions: a silence lost to a restart is
    re-created, and -- the half people forget -- a silence left over on a node
    that is UP is expired, so a genuine failure of it is not swallowed by a
    silence the controller forgot to clean up.
    """

    #: The alert raised by alert(). One name, so it can be routed.
    ALERTNAME = "MetalnapNodeNeedsAttention"

    def __init__(self, url, matchers=None, hours=12, timeout=20,
                 created_by="metalnap", labels=DEFAULT_LABELS,
                 alert_ttl_s=900):
        self.url = url.rstrip("/")
        #: Extra matchers ANDed into every silence, as v2 API dicts or as
        #: strings in amtool syntax. The way to narrow a silence to the alerts
        #: a sleep is EXPECTED to trip, so that anything else a sleeping node
        #: raises still arrives.
        self.matchers = [parse_matcher(m) if isinstance(m, str) else m
                         for m in (matchers or [])]
        #: One silence per label, each matching `<label>=<node>`. Alertmanager
        #: ANDs the matchers inside a silence, so "instance OR node" cannot be
        #: one silence.
        self.labels = tuple(labels)
        if not self.labels:
            raise ValueError("at least one silence label is required")
        self.hours, self.timeout, self.created_by = hours, timeout, created_by
        #: How long a raised alert outlives the last time it was asserted. The
        #: controller re-asserts every tick, so this only matters when the
        #: controller stops -- and then the alert resolves itself rather than
        #: firing forever about a node nobody is watching any more.
        self.alert_ttl_s = alert_ttl_s
        #: Nodes we have an alert out for, and since when. In memory only:
        #: after a restart an orphan simply expires at its endsAt, and a
        #: re-raised one starts again from then.
        self._alerting = {}

    def _comment(self, node):
        # The IDENTITY of our silences -- _find() matches on it -- so it must
        # not change: a new wording would orphan every silence already out
        # there, and an orphan on a node that is up hides its next failure for
        # up to `hours`.
        return "metalnap: %s is deliberately powered down" % node

    def _find(self, node):
        r = requests.get(self.url + "/api/v2/silences", timeout=self.timeout)
        r.raise_for_status()
        return [s for s in r.json()
                if s.get("status", {}).get("state") in ("active", "pending")
                and s.get("comment") == self._comment(node)]

    def _wanted(self, node):
        # Every silence excludes our own alert. It carries `node` and
        # `instance` so it routes like the node's other alerts -- which also
        # means a silence on the node would swallow it, on exactly the node
        # metalnap is shouting about.
        own = {"name": "alertname", "value": self.ALERTNAME,
               "isRegex": False, "isEqual": False}
        return [[{"name": label, "value": node, "isRegex": False,
                  "isEqual": True}] + self.matchers + [own]
                for label in self.labels]

    def going_down(self, node):
        wanted = {_key(ms): ms for ms in self._wanted(node)}
        stale = []
        for s in self._find(node):
            if wanted.pop(_key(s.get("matchers", [])), None) is None:
                # Ours, but not a shape we would create now -- the matchers were
                # reconfigured. Left in place it goes on silencing whatever the
                # OLD matchers covered, which is exactly what the new
                # configuration was written to stop.
                stale.append(s["id"])
        now = datetime.now(timezone.utc)
        # Create before deleting, so a failure part-way leaves the node over-
        # rather than under-silenced -- and raises, which a sleep treats as
        # "could not announce" and refuses to power off on.
        for matchers in wanted.values():
            r = requests.post(self.url + "/api/v2/silences", json={
                "matchers": matchers,
                "startsAt": now.isoformat(),
                "endsAt": (now + timedelta(hours=self.hours)).isoformat(),
                "createdBy": self.created_by,
                "comment": self._comment(node),
            }, timeout=self.timeout)
            r.raise_for_status()
        for sid in stale:
            self._delete(sid)

    def back_up(self, node):
        for s in self._find(node):
            self._delete(s["id"])

    def _delete(self, sid):
        # NOTE the singular path: the v2 API deletes one silence at
        # DELETE /api/v2/silence/{id}; the plural collection path takes no id.
        # The plural form is tried only after a 404, for an
        # Alertmanager-compatible API that routes it differently -- and the
        # status is checked either way, because an unchecked 404 is how a
        # silence survives a wake and hides the next real failure.
        sid = urllib.parse.quote(sid, safe="")
        r = requests.delete("%s/api/v2/silence/%s" % (self.url, sid),
                            timeout=self.timeout)
        if r.status_code == 404:
            r = requests.delete("%s/api/v2/silences/%s" % (self.url, sid),
                                timeout=self.timeout)
        r.raise_for_status()

    def _post_alert(self, node, reason, starts, ends):
        r = requests.post(self.url + "/api/v2/alerts", json=[{
            # `node` AND `instance`, for the same reason silences carry both:
            # whatever an operator already routes or groups node alerts by
            # should catch this one too.
            "labels": {"alertname": self.ALERTNAME, "node": node,
                       "instance": node, "severity": "critical",
                       "source": self.created_by},
            "annotations": {
                "summary": "%s needs attention; metalnap has stopped trying"
                           % node,
                "description": reason},
            # startsAt ALWAYS, and the same one every time. Omitted,
            # Alertmanager sets it to endsAt -- in the future -- and the page
            # says the trouble starts in fifteen minutes, every minute.
            "startsAt": starts.isoformat(),
            "endsAt": ends.isoformat(),
        }], timeout=self.timeout)
        r.raise_for_status()

    def alert(self, node, reason):
        now = datetime.now(timezone.utc)
        starts = self._alerting.get(node, now)
        self._post_alert(node, reason, starts,
                         now + timedelta(seconds=self.alert_ttl_s))
        self._alerting[node] = starts

    def clear_alert(self, node):
        if node not in self._alerting:
            return          # one POST per node per tick otherwise, for nothing
        self._post_alert(node, "recovered", self._alerting[node],
                         datetime.now(timezone.utc))
        del self._alerting[node]
