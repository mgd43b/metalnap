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
keeps. It handles a bounded number of connections at once, so that a client
that opens many and says nothing cannot cost the process a thread each.
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
             "The most managed nodes allowed awake: the effective limit, the "
             "release hold applied, as read and not clamped to the pool; "
             "absent while there is no reading or the signal is unavailable.",
             limit),
            ("metalnap_capacity_ceiling_pool_nodes", "gauge",
             "The managed nodes the ceiling counts: every one except those "
             "held by an operator or taken for maintenance. The ceiling binds "
             "when metalnap_capacity_ceiling is below this.",
             r.get("pool")),
            ("metalnap_capacity_ceiling_engaged", "gauge",
             "1 while a capacity ceiling is in force and limiting the pool "
             "(the limit is below metalnap_capacity_ceiling_pool_nodes).",
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


#: How many connections may be handled at once. A scraper is one connection
#: every few seconds, so this is generous; it exists so that a client that
#: opens a few hundred and says nothing costs eight threads, not a few hundred.
MAX_CONNECTIONS = 8


class _Server(ThreadingHTTPServer):
    """A thread per connection, bounded.

    The standard library starts a thread for every connection it accepts, with
    no limit. This listener lives in the process that makes every call to the
    cluster, the BMCs and Alertmanager, so it takes a slot before it starts a
    thread and, with none left, closes the new connection at once: no thread
    is started, and a legitimate scrape is turned away only while every slot is
    held, which the per-connection timeout bounds.
    """
    daemon_threads = True
    max_connections = MAX_CONNECTIONS

    def __init__(self, *args, max_connections=None, **kw):
        if max_connections is not None:
            self.max_connections = max_connections
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._count = threading.Lock()
        #: For tests and for a curious operator with a debugger: how many
        #: connections have arrived, how many were turned away, how many
        #: handlers are running now.
        self.connections = self.refused = self.slots_in_use = 0
        super().__init__(*args, **kw)

    def process_request(self, request, client_address):
        with self._count:
            self.connections += 1
        if not self._slots.acquire(blocking=False):
            with self._count:
                self.refused += 1
            self.shutdown_request(request)        # closed at once, no thread
            return
        with self._count:
            self.slots_in_use += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release()                       # the thread never started
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def _release(self):
        with self._count:
            self.slots_in_use -= 1
        self._slots.release()


class _DualStackServer(_Server):
    """Both families on one socket, so a cluster that is IPv6-only is scraped
    as readily as one that is not."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def serve(metrics, port, host="", timeout=10, max_connections=MAX_CONNECTIONS):
    """Start serving /metrics in a daemon thread; the server, for shutdown().

    No host means every interface, as a pod's scraper needs: dual-stack where
    the machine has IPv6, and IPv4 where it does not.

    `timeout` is how long one connection may sit silent before it is dropped.
    Without it a client that connects and says nothing holds a thread and a
    descriptor for ever, inside the process that also makes every call to the
    cluster, the BMCs and Alertmanager -- surface that did not exist before this
    listener did.

    `max_connections` is how many may be handled at once (eight); the rest are
    closed as they arrive. A slot held by a silent client is freed by `timeout`.
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
            server = _DualStackServer(("::", port), Handler,
                                      max_connections=max_connections)
        except OSError:
            server = None
    if server is None:
        server = _Server((host or "0.0.0.0", port), Handler,
                         max_connections=max_connections)
    threading.Thread(target=server.serve_forever, name="metalnap-metrics",
                     daemon=True).start()
    return server
