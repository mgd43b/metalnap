"""
`metalnap` on your own machine: see what the controller sees, and ask it for a
node.

    metalnap status
    metalnap maintenance start k8s7 k8s12 --reason "kernel 6.8"
    metalnap maintenance start --all --reason "firmware"
    metalnap maintenance stop k8s7
    metalnap logs [-f] [--node k8s7] [--tail 200]

Everything goes through `kubectl`, so it runs with your kubeconfig, your RBAC
and your audit trail, and holds no credential of its own. It names the context
it is acting on every time, and passes it to every call it makes: a
kubeconfig's current context moves between sessions, and a command that
quietly lands on another cluster is how the wrong machine gets worked on. Pin
it with --context or METALNAP_CONTEXT.

The controller is found by the labels its Helm chart puts on the Deployment,
and its own configuration -- the node list, the cordon annotation, the mode --
is read from there, so `status` judges a cordon exactly the way the controller
does. What it cannot show is what the controller is DOING, a wake or a drain
in progress: that lives in the controller's memory, and `logs` is where it
says so.
"""
import argparse
import getpass
import json
import os
import subprocess
import sys

from .kube import KubeNodeSource

#: The operator's request, and metalnap's record that it took it up. Always
#: under this prefix, whatever cordon annotation the controller is given.
MAINTENANCE = KubeNodeSource.NOTE_PREFIX + "maintenance"
STARTED = KubeNodeSource.NOTE_PREFIX + "maintenance-started"
SELECTOR = "app.kubernetes.io/name=metalnap"


class CliError(Exception):
    """Something the person at the keyboard can fix; said plainly, no trace."""


class Kubectl:
    """kubectl, pinned to one context. The only way this module reaches a
    cluster, so nothing here can forget the --context."""

    def __init__(self, context=None, run=subprocess.run, popen=subprocess.Popen):
        self._run, self._popen = run, popen
        self.context = context or self.current_context()

    def _argv(self, args):
        return ["kubectl", "--context", self.context] + list(args)

    def _call(self, argv):
        try:
            r = self._run(argv, capture_output=True, text=True)
        except FileNotFoundError:
            raise CliError("kubectl is not on your PATH")
        if r.returncode != 0:
            raise CliError("%s failed: %s" % (" ".join(argv[:4]),
                                              (r.stderr or r.stdout).strip()))
        return r.stdout

    def current_context(self):
        return self._call(["kubectl", "config", "current-context"]).strip()

    def __call__(self, *args):
        return self._call(self._argv(args))

    def json(self, *args):
        return json.loads(self(*args, "-o", "json"))

    def stream(self, *args):
        """Lines of a long-running command, as they arrive."""
        try:
            p = self._popen(self._argv(args), stdout=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise CliError("kubectl is not on your PATH")
        try:
            yield from p.stdout
        finally:
            p.stdout.close()
            code = p.wait()
        # Its complaint has already gone to the terminal; what must not be
        # lost is that it failed, or `metalnap logs | grep` reads as "no
        # lines matched" when it was "no permission to read any".
        if code:
            raise CliError("%s exited %d" % (" ".join(self._argv(args)[:4]),
                                             code))


class Target:
    """One metalnap controller in one cluster, and what it was told."""

    def __init__(self, kubectl, namespace=None, release=None,
                 selector=SELECTOR):
        self.k = kubectl
        selector = (selector or SELECTOR) + (
            ",app.kubernetes.io/instance=" + release if release else "")
        where = ["-n", namespace] if namespace else ["-A"]
        found = self.k.json("get", "deployments", *where, "-l", selector)
        items = found.get("items", [])
        if not items:
            raise CliError(
                "no metalnap controller in context %s (looked for a Deployment "
                "labelled %s%s)" % (self.k.context, selector,
                                   " in namespace " + namespace
                                   if namespace else ""))
        if len(items) > 1:
            raise CliError(
                "more than one metalnap controller in context %s: %s -- pick "
                "one with --namespace or --release"
                % (self.k.context, ", ".join(_where(d) for d in items)))
        d = items[0]
        self.namespace = d["metadata"]["namespace"]
        self.name = d["metadata"]["name"]
        self.env = self._env(d)
        self.nodes = [n.strip() for n in self.env.get("NODES", "").split(",")
                      if n.strip()]
        self.mode = self.env.get("MODE", "dry_run").strip()
        self.source = KubeNodeSource(
            None, annotation=self.env.get("CORDON_ANNOTATION",
                                          "metalnap.io/cordoned"))

    @property
    def where(self):
        return "%s/%s" % (self.namespace, self.name)

    def _env(self, deployment):
        """The controller's environment: its ConfigMap, then literal values."""
        env = {}
        for c in deployment["spec"]["template"]["spec"]["containers"]:
            for src in c.get("envFrom", []):
                ref = src.get("configMapRef")
                if ref:
                    cm = self.k.json("get", "configmap", ref["name"],
                                     "-n", self.namespace)
                    env.update(cm.get("data") or {})
            for e in c.get("env", []):
                if "value" in e:
                    env[e["name"]] = e["value"]
        return env

    def states(self):
        """{node: NodeState or None}, for every node the controller manages."""
        listing = self.k.json("get", "nodes")
        by_name = {n["metadata"]["name"]: n for n in listing.get("items", [])}
        return {n: self.source.parse(by_name[n]) if n in by_name else None
                for n in self.nodes}

    def pick(self, names, everything):
        if everything:
            if names:
                raise CliError("name nodes or pass --all, not both")
            return list(self.nodes)
        if not names:
            raise CliError("name at least one node, or pass --all")
        unknown = [n for n in names if n not in self.nodes]
        if unknown:
            # Refused rather than written: an annotation on a node this
            # controller does not manage does nothing, silently, and a typo
            # would look exactly like a request that is being ignored.
            raise CliError("not managed by %s: %s (it manages %s)"
                           % (self.where, ", ".join(unknown),
                              ", ".join(self.nodes) or "nothing"))
        return list(names)

    def annotate(self, node, values):
        """One merge patch per node; None deletes."""
        self.k("patch", "node", node, "--type", "merge", "-p",
               json.dumps({"metadata": {"annotations": values}}))


def describe(state):
    """(state, detail) for one node, as the controller would read it."""
    if state is None:
        return "absent", "not in the cluster"
    if state.protected:
        return "protected", "a control-plane node; never power-managed"
    if state.maintenance:
        return ("maintenance",
                "%s -- %s, %s" % (state.maintenance,
                                  "up" if state.ready else "down",
                                  "taken up %s" % _when(
                                      state.maintenance_started_at)
                                  if state.maintenance_started_at
                                  else "not yet taken up"))
    if state.cordoned and not state.ours:
        return "held", "cordoned by someone other than metalnap; left alone"
    if state.ready and not state.cordoned:
        return "in service", ""
    if state.ready:
        return ("up, cordoned", "by metalnap: waking, visiting, draining, or "
                                "about to be repaired")
    if state.cordoned:
        return "asleep", ""
    return "DOWN", "dark and uncordoned -- metalnap did not put it down"


def _where(d):
    return "%s/%s" % (d["metadata"]["namespace"], d["metadata"]["name"])


def _when(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _header(t, out):
    print("context:    %s" % t.k.context, file=out)
    print("controller: %s (mode=%s)" % (t.where, t.mode), file=out)
    if t.mode != "on":
        print("note:       mode=%s -- metalnap %s, so a maintenance request "
              "is recorded but not acted on" % (
                  t.mode, "only logs what it would do" if t.mode == "dry_run"
                  else "is switched off"), file=out)


def cmd_status(t, args, out):
    _header(t, out)
    rows = []
    for n, state in t.states().items():
        what, detail = describe(state)
        notes = []
        if state is not None and state.trouble:
            notes.append("NEEDS A HUMAN: " + state.trouble)
        if state is not None and state.power_cycled_at:
            notes.append("power-cycled " + _when(state.power_cycled_at))
        rows.append((n, what, "; ".join(filter(None, [detail] + notes))))
    print(file=out)
    _table([("NODE", "STATE", "DETAIL")] + rows, out)
    return 0


def cmd_maintenance(t, args, out):
    nodes = t.pick(args.nodes, args.all)
    _header(t, out)
    start = args.action == "start"
    reason = args.reason or "asked for by %s" % getpass.getuser()
    states = t.states() if start else {}
    already = [n for n in nodes
               if states.get(n) is not None and states[n].maintenance]
    done = []
    for n in nodes:
        if not start:
            # Both, so a request withdrawn while the controller was not
            # running cannot leave a record that stops the next one powering
            # the node on.
            patch = {MAINTENANCE: None, STARTED: None}
        elif n in already:
            # Its record stays. Clearing it would have metalnap power on a
            # machine somebody has switched off to work on -- and `--all`
            # would do that to every node already asked for. Stop, then
            # start, is how to ask for another power-on.
            patch = {MAINTENANCE: reason}
        else:
            # A record with no request is left from one withdrawn while the
            # controller was not running; it would stop this one powering on.
            patch = {MAINTENANCE: reason, STARTED: None}
        try:
            t.annotate(n, patch)
        except CliError as e:
            if not done:
                raise
            # Stopping at the first failure is right; leaving the person to
            # believe nothing changed is not.
            raise CliError("%s -- %s already %s" % (
                e, ", ".join(done), "asked for" if start else "given back"))
        done.append(n)
    if start:
        print("asked for %s: %s" % (", ".join(nodes), reason), file=out)
        print("metalnap powers each one on if it is off -- one per tick -- "
              "then leaves it alone until `metalnap maintenance stop`.",
              file=out)
        if already:
            print("%s already asked for: reason updated, and not powered on "
                  "again -- stop, then start, to ask for that."
                  % ", ".join(already), file=out)
    else:
        print("gave back %s. metalnap puts each into service or to sleep, as "
              "demand says." % ", ".join(nodes), file=out)
    return 0


def cmd_logs(t, args, out):
    argv = ["logs", "-n", t.namespace, "deployment/" + t.name,
            "--tail", str(args.tail)] + (["-f"] if args.follow else [])
    print("context:    %s" % t.k.context, file=out)
    print("controller: %s" % t.where, file=out)
    for line in t.k.stream(*argv):
        shown = format_log(line, args.node)
        if shown is not None:
            print(shown, file=out, flush=True)
    return 0


def format_log(line, node=None):
    """One controller log line, readable; None if filtered out.

    The controller logs one JSON object per line. Anything else -- a
    traceback, kubectl's own complaints -- is passed through as it is.
    """
    line = line.rstrip("\n")
    try:
        rec = json.loads(line)
    except ValueError:
        return None if node else line
    if not isinstance(rec, dict):
        return None if node else line
    if node and not _mentions(rec, node):
        return None
    ts = str(rec.pop("ts", ""))[:19].replace("T", " ")
    level = str(rec.pop("level", "")).upper()
    msg = rec.pop("msg", "")
    rec.pop("mode", None)
    extra = " ".join("%s=%s" % (k, v if isinstance(v, str)
                                else json.dumps(v, sort_keys=True))
                     for k, v in rec.items())
    return ("%s %-5s %s  %s" % (ts, level, msg, extra)).rstrip()


def _mentions(rec, node):
    for v in rec.values():
        if v == node:
            return True
        if isinstance(v, (list, tuple)) and node in v:
            return True
        if isinstance(v, dict) and node in v:
            return True
    return False


def _table(rows, out):
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        print(("  ".join(c.ljust(w) for c, w in zip(r, widths))
               + "  " + r[-1]).rstrip(), file=out)


def parser(command):
    """The parser for one command.

    One per command rather than argparse subcommands, so each can be parsed
    INTERMIXED: `maintenance start --reason x k8s7` and `start k8s7 --reason
    x` mean the same thing. With subcommands, argparse fills the node list
    from the first unbroken run of names only, and a name after any option
    was rejected as an unrecognised argument.
    """
    p = argparse.ArgumentParser(prog="metalnap " + command,
                                description=_HELP[command])
    p.add_argument("--context", default=os.environ.get("METALNAP_CONTEXT"),
                   help="kubeconfig context (default $METALNAP_CONTEXT, else "
                        "your current context -- named either way)")
    p.add_argument("-n", "--namespace",
                   help="namespace of the controller (default: search all "
                        "namespaces)")
    p.add_argument("--release",
                   help="Helm release, if one cluster runs several")
    p.add_argument("-l", "--selector", default=SELECTOR,
                   help="labels the controller's Deployment carries (default "
                        "%s; the chart's nameOverride changes it)" % SELECTOR)
    if command == "maintenance":
        p.add_argument("action", choices=("start", "stop"))
        p.add_argument("nodes", nargs="*")
        p.add_argument("--all", action="store_true",
                       help="every node the controller manages")
        p.add_argument("--reason",
                       help="why, shown in logs and `status` (start only)")
    elif command == "logs":
        p.add_argument("-f", "--follow", action="store_true")
        p.add_argument("--node", help="only lines about this node")
        p.add_argument("--tail", type=int, default=200)
    return p


_HELP = {
    "status": "Every node the controller manages, and what state it is in.",
    "maintenance": "Ask for nodes to work on, and give them back. metalnap "
                   "powers each on once, then leaves it alone until it is "
                   "given back.",
    "logs": "The controller's log, readable.",
}


COMMANDS = {"status": cmd_status, "maintenance": cmd_maintenance,
            "logs": cmd_logs}


def main(argv, out=sys.stdout, kubectl=Kubectl):
    """`argv` begins with the command; __main__ has already checked it."""
    args = parser(argv[0]).parse_intermixed_args(argv[1:])
    args.command = argv[0]
    try:
        t = Target(kubectl(args.context), args.namespace, args.release,
                   args.selector)
        return COMMANDS[args.command](t, args, out)
    except CliError as e:
        print("metalnap: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # The reader went away -- `| head`, a pager quit early. Nothing is
        # wrong, and a traceback into a closed pipe helps nobody. Pointed at
        # /dev/null so the interpreter's own flush at exit is quiet too.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0
