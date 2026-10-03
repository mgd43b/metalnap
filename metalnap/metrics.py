"""A metrics listener, and the Prometheus text format it speaks.

The controller had no HTTP listener at all before this, so this is new surface,
and it is kept as small as it can be: the standard library's http.server in a
daemon thread and the text format written by hand, so that `requests` stays the
only runtime dependency and there is nothing here to keep patched. It is off
unless METRICS_PORT is set.

It reports; it does not decide. The controller hands it what a tick concluded,
and a scrape reads that. Nothing here is read by the controller, so a listener
that is wedged, scraped by a hundred clients or never scraped changes nothing
about what the controller does -- which is the same rule the status object
keeps.
"""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    """The latest report, rendered in the Prometheus text format."""

    def __init__(self):
        self._lock = threading.Lock()
        self._report = {}

    def publish(self, report):
        with self._lock:
            self._report = dict(report)

    def render(self):
        with self._lock:
            r = dict(self._report)
        limit = r.get("limit")
        samples = (
            ("metalnap_capacity_ceiling", "gauge",
             "The most managed nodes allowed awake; absent while no ceiling "
             "is in force.", limit),
            ("metalnap_capacity_ceiling_engaged", "gauge",
             "1 while a capacity ceiling is in force and limiting the pool.",
             int(bool(r.get("engaged")))),
            ("metalnap_capacity_ceiling_signal_ok", "gauge",
             "0 while the ceiling signal is unavailable and is being treated "
             "as no ceiling.", 1 if r.get("signal_ok", True) else 0),
            ("metalnap_nodes_shed", "gauge",
             "Nodes currently held down by the capacity ceiling.",
             len(r.get("shed") or [])),
            ("metalnap_shed_forced_total", "counter",
             "Nodes shut down at the shed deadline with work still running, "
             "counted once per shed when the power-off is confirmed, since "
             "the controller started.",
             int(r.get("forced") or 0)),
        )
        out = []
        for name, kind, help_, value in samples:
            out.append("# HELP %s %s" % (name, help_))
            out.append("# TYPE %s %s" % (name, kind))
            if value is not None:       # absent is how "no limit" is said
                out.append("%s %d" % (name, value))
        return "\n".join(out) + "\n"


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class _DualStackServer(_Server):
    """Both families on one socket, so a cluster that is IPv6-only is scraped
    as readily as one that is not."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def serve(metrics, port, host="", timeout=10):
    """Start serving /metrics in a daemon thread; the server, for shutdown().

    No host means every interface, as a pod's scraper needs: dual-stack where
    the machine has IPv6, and IPv4 where it does not.

    `timeout` is how long one connection may sit silent before it is dropped.
    Without it a client that connects and says nothing holds a thread and a
    descriptor for ever, inside the process that also makes every call to the
    cluster, the BMCs and Alertmanager -- surface that did not exist before this
    listener did.
    """
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/metrics":
                self.send_error(404)
                return
            body = metrics.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass            # a scrape every few seconds is not a log line

    Handler.timeout = timeout

    server = None
    if not host:
        try:
            server = _DualStackServer(("::", port), Handler)
        except OSError:
            server = None
    if server is None:
        server = _Server((host or "0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="metalnap-metrics",
                     daemon=True).start()
    return server
