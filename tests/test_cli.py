"""
Tests for the operator's CLI (metalnap/cli.py) and the command dispatch in
metalnap/__main__.py.

No real kubectl is ever invoked. `FakeCluster` stands in for it at the two
seams Kubectl actually uses -- `run` (subprocess.run) and `popen`
(subprocess.Popen) -- and everything above that (Kubectl, Target, cmd_status,
cmd_maintenance, cmd_logs, format_log, and KubeNodeSource.parse via kube.py)
runs for real. That matters most for `status`: it must read a node exactly
the way the controller does, so these tests feed raw Kubernetes Node JSON
into the fake cluster and let the real parser turn it into a NodeState,
never construct a NodeState by hand.
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metalnap import cli                         # noqa: E402
import metalnap.__main__ as main_mod             # noqa: E402


# ---------------------------------------------------------------------------
# A fake kubectl: enough of `run` and `popen` to drive cli.py, nothing more.
# ---------------------------------------------------------------------------

class Result:
    """Stands in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeStdout:
    def __init__(self, lines):
        self._lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class _FakeProcess:
    """Stands in for subprocess.Popen, for `kubectl logs [-f]`."""

    def __init__(self, lines, returncode=0):
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


_VALUE_FLAGS = {"-n", "-l", "-o", "-p", "--type", "--tail", "--context"}
_BOOL_FLAGS = {"-A", "-f"}


def _split(tokens):
    """argv tokens -> (positional args, {flag: value-or-True})."""
    pos, flags = [], {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in _VALUE_FLAGS:
            flags[t] = tokens[i + 1]
            i += 2
        elif t in _BOOL_FLAGS:
            flags[t] = True
            i += 1
        else:
            pos.append(t)
            i += 1
    return pos, flags


def _matches_selector(labels, selector):
    if not selector:
        return True
    for pair in selector.split(","):
        k, _, v = pair.partition("=")
        if labels.get(k) != v:
            return False
    return True


class FakeCluster:
    """A pretend kubectl talking to a pretend cluster.

    Holds Deployments, ConfigMaps and Nodes as plain dicts -- the same shape
    the real Kubernetes API returns -- and answers exactly the calls cli.py
    makes: `config current-context`, `get deployments`, `get configmap`,
    `get nodes`, `patch node`, and `logs` (via `popen`, streamed). Every argv
    `run` sees is recorded in `calls`, and every one `popen` sees in
    `stream_calls`, so a test can assert --context reached each one.
    """

    def __init__(self, context="prod-cluster"):
        self.context = context
        self.deployments = []
        self.configmaps = {}          # (namespace, name) -> {"data": {...}}
        self.nodes = {}                # name -> Node dict
        self.patches = []              # [(node, parsed merge patch), ...]
        self.log_lines = []            # lines `logs` streams back
        self.log_exit = 0              # and how `kubectl logs` exits
        self.calls = []
        self.stream_calls = []
        self.current_context_calls = 0

    # -- subprocess.run / subprocess.Popen stand-ins ----------------------
    def run(self, argv, capture_output=True, text=True):
        self.calls.append(list(argv))
        return self._dispatch(argv)

    def popen(self, argv, stdout=None, text=True):
        self.stream_calls.append(list(argv))
        return _FakeProcess(self.log_lines, self.log_exit)

    # -- dispatch -----------------------------------------------------------
    def _dispatch(self, argv):
        assert argv and argv[0] == "kubectl", argv
        rest = argv[1:]
        if rest[:1] == ["--context"]:
            rest = rest[2:]
        pos, flags = _split(rest)
        if pos == ["config", "current-context"]:
            self.current_context_calls += 1
            return Result(stdout=self.context + "\n")
        if pos == ["get", "deployments"]:
            return self._get_deployments(flags)
        if len(pos) == 3 and pos[:2] == ["get", "configmap"]:
            return self._get_configmap(pos[2], flags)
        if pos == ["get", "nodes"]:
            return Result(stdout=json.dumps(
                {"items": list(self.nodes.values())}))
        if len(pos) == 3 and pos[:2] == ["patch", "node"]:
            return self._patch_node(pos[2], flags)
        raise AssertionError("FakeCluster: unhandled kubectl call %r" % argv)

    def _get_deployments(self, flags):
        ns = flags.get("-n")
        selector = flags.get("-l", "")
        items = [d for d in self.deployments
                if (ns is None or d["metadata"]["namespace"] == ns)
                and _matches_selector(d["metadata"].get("labels", {}),
                                     selector)]
        return Result(stdout=json.dumps({"items": items}))

    def _get_configmap(self, name, flags):
        key = (flags.get("-n"), name)
        if key not in self.configmaps:
            return Result(returncode=1,
                         stderr='Error from server (NotFound): configmaps '
                                '"%s" not found' % name)
        return Result(stdout=json.dumps(self.configmaps[key]))

    def _patch_node(self, name, flags):
        patch = json.loads(flags["-p"])
        node = self.nodes.get(name)
        if node is None:
            return Result(returncode=1,
                          stderr='Error from server (NotFound): nodes "%s" '
                                 'not found' % name)
        self.patches.append((name, patch))
        if node is not None:
            anns = node["metadata"].setdefault("annotations", {})
            for k, v in patch.get("metadata", {}).get("annotations",
                                                       {}).items():
                if v is None:
                    anns.pop(k, None)
                else:
                    anns[k] = v
            if "unschedulable" in patch.get("spec", {}):
                node["spec"]["unschedulable"] = patch["spec"]["unschedulable"]
        return Result(stdout="node/%s patched\n" % name)


# ---------------------------------------------------------------------------
# Fixtures: raw Kubernetes JSON, exactly what the real API would return.
# ---------------------------------------------------------------------------

def deployment(namespace="ops", name="metalnap-controller", release=None,
              configmap_name="metalnap-config", literal_env=None):
    labels = {"app.kubernetes.io/name": "metalnap"}
    if release:
        labels["app.kubernetes.io/instance"] = release
    container = {"envFrom": [{"configMapRef": {"name": configmap_name}}]}
    if literal_env:
        container["env"] = [{"name": k, "value": v}
                            for k, v in literal_env.items()]
    return {"metadata": {"name": name, "namespace": namespace,
                        "labels": labels},
            "spec": {"template": {"spec": {"containers": [container]}}}}


def k8s_node(name, ready=True, cordoned=False, annotations=None,
            labels=None):
    return {
        "metadata": {"name": name, "annotations": dict(annotations or {}),
                    "labels": dict(labels or {})},
        "spec": {"unschedulable": cordoned},
        "status": {
            "conditions": [{"type": "Ready",
                           "status": "True" if ready else "Unknown",
                           "lastTransitionTime": "2024-01-01T00:00:00Z"}],
            "allocatable": {"memory": "16Gi"},
        },
    }


def make_cluster(nodes=("a", "b", "c"), mode="on", cordon_annotation=None,
                 namespace="ops", name="metalnap-controller", release=None,
                 literal_env=None, node_objs=None, context="prod-cluster",
                 configmap_name="metalnap-config"):
    """One metalnap Deployment, its ConfigMap, and one Node per name.

    A node in `node_objs` is used as given; a name mapped to None is left
    OUT of the cluster entirely (for the "absent" state); anything else
    defaults to an ordinary in-service node.
    """
    c = FakeCluster(context=context)
    c.deployments.append(deployment(namespace=namespace, name=name,
                                    release=release,
                                    configmap_name=configmap_name,
                                    literal_env=literal_env))
    data = {"NODES": ",".join(nodes), "MODE": mode}
    if cordon_annotation:
        data["CORDON_ANNOTATION"] = cordon_annotation
    c.configmaps[(namespace, configmap_name)] = {"data": data}
    node_objs = node_objs or {}
    for n in nodes:
        if n in node_objs:
            if node_objs[n] is not None:
                c.nodes[n] = node_objs[n]
            # else: left out on purpose (the "absent" state)
        else:
            c.nodes[n] = k8s_node(n)
    return c


def run_cli(argv, cluster):
    """cli.main(), wired to `cluster`, with stdout/stderr captured."""
    out, err = io.StringIO(), io.StringIO()

    def kubectl_factory(ctx):
        return cli.Kubectl(ctx, run=cluster.run, popen=cluster.popen)

    with contextlib.redirect_stderr(err):
        code = cli.main(list(argv), out=out, kubectl=kubectl_factory)
    return code, out.getvalue(), err.getvalue()


def status_rows(output):
    """The table's data lines, in order (everything after the NODE header)."""
    lines = output.rstrip("\n").splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.startswith("NODE"))
    return lines[header + 1:]


def status_state(row):
    return re.split(r"\s{2,}", row.rstrip())[1]


def status_detail(row):
    parts = re.split(r"\s{2,}", row.rstrip())
    return parts[2] if len(parts) > 2 else ""


# ---------------------------------------------------------------------------
# Context pinning: the whole point of the module's own docstring.
# ---------------------------------------------------------------------------

class TestContextPinning(unittest.TestCase):
    def test_explicit_context_pins_every_call_and_skips_current_context(self):
        cluster = make_cluster(nodes=("a",))
        code, out, err = run_cli(
            ["status", "--context", "pinned-ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(cluster.current_context_calls, 0)
        for argv in cluster.calls:
            self.assertEqual(argv[1:3], ["--context", "pinned-ctx"], argv)
        self.assertIn("context:    pinned-ctx", out)

    def test_env_var_pins_context_too(self):
        cluster = make_cluster(nodes=("a",))
        with mock.patch.dict(os.environ, {"METALNAP_CONTEXT": "env-ctx"}):
            code, out, err = run_cli(["status"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(cluster.current_context_calls, 0)
        for argv in cluster.calls:
            self.assertEqual(argv[1:3], ["--context", "env-ctx"], argv)
        self.assertIn("context:    env-ctx", out)

    def test_explicit_flag_wins_over_env_var(self):
        cluster = make_cluster(nodes=("a",))
        with mock.patch.dict(os.environ, {"METALNAP_CONTEXT": "env-ctx"}):
            code, out, err = run_cli(
                ["status", "--context", "flag-ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(cluster.current_context_calls, 0)
        self.assertIn("context:    flag-ctx", out)

    def test_no_context_given_reads_current_context_exactly_once(self):
        cluster = make_cluster(nodes=("a",), context="whatever-is-current")
        with mock.patch.dict(os.environ):
            os.environ.pop("METALNAP_CONTEXT", None)
            code, out, err = run_cli(["status"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(cluster.current_context_calls, 1)
        self.assertIn("context:    whatever-is-current", out)
        for argv in cluster.calls:
            if argv[1:3] == ["config", "current-context"]:
                continue
            self.assertEqual(argv[1:3], ["--context", "whatever-is-current"])

    def test_context_pinned_and_printed_for_maintenance_and_logs_too(self):
        cluster = make_cluster(nodes=("a",))
        cluster.log_lines = []
        for argv in (["maintenance", "start", "a", "--context", "c1"],
                    ["maintenance", "stop", "a", "--context", "c1"],
                    ["logs", "--context", "c1"]):
            cluster.calls, cluster.stream_calls = [], []
            code, out, err = run_cli(argv, cluster)
            self.assertEqual(code, 0, err)
            self.assertIn("context:    c1", out)
            for a in cluster.calls + cluster.stream_calls:
                self.assertEqual(a[1:3], ["--context", "c1"])


# ---------------------------------------------------------------------------
# Discovery: finding the one Deployment the CLI should act on.
# ---------------------------------------------------------------------------

class TestDiscovery(unittest.TestCase):
    def test_zero_deployments_is_a_clear_error(self):
        cluster = FakeCluster(context="ctx")
        code, out, err = run_cli(["status", "--context", "ctx"], cluster)
        self.assertEqual(code, 1)
        self.assertIn("ctx", err)
        self.assertIn(cli.SELECTOR, err)

    def test_two_deployments_lists_both(self):
        cluster = FakeCluster(context="ctx")
        cluster.deployments = [
            deployment(namespace="ns1", name="metalnap-a"),
            deployment(namespace="ns2", name="metalnap-b"),
        ]
        code, out, err = run_cli(["status", "--context", "ctx"], cluster)
        self.assertEqual(code, 1)
        self.assertIn("ns1/metalnap-a", err)
        self.assertIn("ns2/metalnap-b", err)

    def test_namespace_and_release_narrow_the_query(self):
        cluster = FakeCluster(context="ctx")
        cluster.deployments = [
            deployment(namespace="myns", name="mine", release="myrel",
                      literal_env={"NODES": "a"}),
            deployment(namespace="other", name="theirs"),
        ]
        cluster.configmaps[("myns", "metalnap-config")] = {"data": {}}
        cluster.nodes["a"] = k8s_node("a")
        # Unnarrowed, both match app.kubernetes.io/name=metalnap: ambiguous.
        code, out, err = run_cli(["status", "--context", "ctx"], cluster)
        self.assertEqual(code, 1)
        # --namespace/--release narrow it to exactly one.
        code, out, err = run_cli(
            ["status", "--context", "ctx", "--namespace", "myns",
            "--release", "myrel"], cluster)
        self.assertEqual(code, 0, err)
        deploy_calls = [a for a in cluster.calls
                       if a[3:5] == ["get", "deployments"]]
        argv = deploy_calls[-1]
        self.assertIn("myns", argv)
        self.assertNotIn("-A", argv)
        self.assertTrue(any("instance=myrel" in tok for tok in argv), argv)


# ---------------------------------------------------------------------------
# Config: read from the ConfigMap via envFrom, literal env overrides it.
# ---------------------------------------------------------------------------

class TestConfigRead(unittest.TestCase):
    def test_literal_env_overrides_configmap(self):
        # ConfigMap says NODES=a,b; a literal env NODES=a on the container
        # must win -- that is what actually reaches the running process
        # when both set the same variable.
        cluster = make_cluster(nodes=("a", "b"), literal_env={"NODES": "a"})
        code, out, err = run_cli(["status", "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual([r.split()[0] for r in status_rows(out)], ["a"])


class TestCordonAnnotation(unittest.TestCase):
    def test_custom_annotation_reads_as_our_own_cordon(self):
        custom = "legacy-controller.example.org/cordoned"
        n = k8s_node("a", ready=False, cordoned=True,
                    annotations={custom: "2024-01-01T00:00:00Z"})
        cluster = make_cluster(nodes=("a",), cordon_annotation=custom,
                               node_objs={"a": n})
        code, out, err = run_cli(["status", "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(status_state(status_rows(out)[0]), "asleep")

    def test_without_matching_config_the_same_node_reads_as_held(self):
        """Same node, default CORDON_ANNOTATION: ownership is judged by the
        annotation the controller was told about, not by who else might
        have cordoned it."""
        custom = "legacy-controller.example.org/cordoned"
        n = k8s_node("a", ready=False, cordoned=True,
                    annotations={custom: "2024-01-01T00:00:00Z"})
        cluster = make_cluster(nodes=("a",), node_objs={"a": n})
        code, out, err = run_cli(["status", "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(status_state(status_rows(out)[0]), "held")


class TestModeNote(unittest.TestCase):
    def test_dry_run_note(self):
        cluster = make_cluster(nodes=("a",), mode="dry_run")
        _, out, _ = run_cli(["status", "--context", "ctx"], cluster)
        self.assertIn("mode=dry_run -- metalnap only logs what it would "
                      "do, so a maintenance request is recorded but not "
                      "acted on", out)

    def test_off_note(self):
        cluster = make_cluster(nodes=("a",), mode="off")
        _, out, _ = run_cli(["status", "--context", "ctx"], cluster)
        self.assertIn("mode=off -- metalnap is switched off, so a "
                      "maintenance request is recorded but not acted on",
                      out)

    def test_on_mode_has_no_note(self):
        cluster = make_cluster(nodes=("a",), mode="on")
        _, out, _ = run_cli(["status", "--context", "ctx"], cluster)
        self.assertNotIn("note:", out)


# ---------------------------------------------------------------------------
# Status: every state describe() can report, read off raw Kubernetes JSON.
# ---------------------------------------------------------------------------

class TestStatusStates(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            "svc": k8s_node("svc", ready=True, cordoned=False),
            "asleep1": k8s_node(
                "asleep1", ready=False, cordoned=True,
                annotations={"metalnap.io/cordoned": "2024-01-01T00:00:00Z"}),
            "held1": k8s_node("held1", ready=True, cordoned=True),
            "upcordoned": k8s_node(
                "upcordoned", ready=True, cordoned=True,
                annotations={"metalnap.io/cordoned": "2024-01-01T00:00:00Z"}),
            "down1": k8s_node("down1", ready=False, cordoned=False),
            "gone": None,
            "cp1": k8s_node(
                "cp1", ready=True, cordoned=True,
                labels={"node-role.kubernetes.io/control-plane": ""}),
            "maintup": k8s_node(
                "maintup", ready=True, cordoned=False,
                annotations={"metalnap.io/maintenance": "kernel 6.8",
                            "metalnap.io/maintenance-started":
                            "2024-03-01T12:34:56Z"}),
            "maintdown": k8s_node(
                "maintdown", ready=False, cordoned=False,
                annotations={"metalnap.io/maintenance": "firmware"}),
            "emptymaint": k8s_node(
                "emptymaint", ready=False, cordoned=False,
                annotations={"metalnap.io/maintenance": ""}),
            "troubled": k8s_node(
                "troubled", ready=True, cordoned=False,
                annotations={"metalnap.io/trouble": "bmc unreachable"}),
        }
        names = tuple(self.fixture)
        self.cluster = make_cluster(nodes=names, node_objs=self.fixture)
        code, self.out, err = run_cli(["status", "--context", "ctx"],
                                      self.cluster)
        self.assertEqual(code, 0, err)
        self.rows = {r.split()[0]: r for r in status_rows(self.out)}

    def test_order_matches_nodes_config(self):
        self.assertEqual([r.split()[0] for r in status_rows(self.out)],
                         list(self.fixture))

    def test_in_service(self):
        self.assertEqual(status_state(self.rows["svc"]), "in service")

    def test_asleep(self):
        self.assertEqual(status_state(self.rows["asleep1"]), "asleep")

    def test_operator_held(self):
        self.assertEqual(status_state(self.rows["held1"]), "held")
        self.assertIn("cordoned by someone other than metalnap",
                      status_detail(self.rows["held1"]))

    def test_up_and_cordoned_by_metalnap(self):
        self.assertEqual(status_state(self.rows["upcordoned"]),
                         "up, cordoned")

    def test_down(self):
        self.assertEqual(status_state(self.rows["down1"]), "DOWN")

    def test_absent(self):
        self.assertEqual(status_state(self.rows["gone"]), "absent")

    def test_protected_overrides_cordon(self):
        # cp1 is cordoned in the fixture -- protected must still win.
        self.assertEqual(status_state(self.rows["cp1"]), "protected")

    def test_maintenance_up_and_taken_up(self):
        self.assertIn("kernel 6.8 -- up, taken up 2024-03-01 12:34 UTC",
                      status_detail(self.rows["maintup"]))

    def test_maintenance_down_and_not_yet_taken_up(self):
        self.assertIn("firmware -- down, not yet taken up",
                      status_detail(self.rows["maintdown"]))

    def test_empty_maintenance_value_still_reads_as_a_request(self):
        self.assertIn("no reason given",
                      status_detail(self.rows["emptymaint"]))

    def test_trouble_is_flagged_needs_a_human(self):
        self.assertIn("NEEDS A HUMAN: bmc unreachable",
                      status_detail(self.rows["troubled"]))


# ---------------------------------------------------------------------------
# maintenance start / stop
# ---------------------------------------------------------------------------

class TestMaintenanceStart(unittest.TestCase):
    def test_refuses_a_node_not_in_nodes(self):
        cluster = make_cluster(nodes=("a", "b"))
        code, out, err = run_cli(
            ["maintenance", "start", "c", "--reason", "x",
            "--context", "ctx"], cluster)
        self.assertEqual(code, 1)
        self.assertIn("not managed by ops/metalnap-controller: c "
                      "(it manages a, b)", err)
        self.assertEqual(cluster.patches, [])

    def test_all_and_names_together_is_refused(self):
        for argv in (["maintenance", "start", "a", "--all"],
                     ["maintenance", "start", "--all", "a"]):
            cluster = make_cluster(nodes=("a", "b"))
            code, out, err = run_cli(argv + ["--reason", "x",
                                             "--context", "ctx"], cluster)
            self.assertEqual(code, 1, argv)
            self.assertIn("name nodes or pass --all, not both", err)
            self.assertEqual(cluster.patches, [])

    def test_a_node_already_asked_for_keeps_its_record(self):
        """Clearing it would power back on a machine someone switched off to
        work on -- and `--all` would do that to every node already held."""
        started = "2026-09-18T01:17:34Z"
        cluster = make_cluster(nodes=("a", "b"), node_objs={
            "a": k8s_node("a", ready=False, cordoned=True, annotations={
                "metalnap.io/cordoned": started,
                "metalnap.io/maintenance": "dimm swap",
                "metalnap.io/maintenance-started": started})})
        code, out, err = run_cli(["maintenance", "start", "--all",
                                  "--reason", "fw", "--context", "ctx"],
                                 cluster)
        self.assertEqual(code, 0, err)
        patches = dict(cluster.patches)
        self.assertEqual(patches["a"]["metadata"]["annotations"],
                         {"metalnap.io/maintenance": "fw"})
        self.assertEqual(patches["b"]["metadata"]["annotations"],
                         {"metalnap.io/maintenance": "fw",
                          "metalnap.io/maintenance-started": None})
        self.assertIn("a already asked for", out)

    def test_logs_fails_when_kubectl_does(self):
        """`metalnap logs | grep` must not read "no permission to read any"
        as "no lines matched"."""
        cluster = make_cluster()
        cluster.log_exit = 1
        code, out, err = run_cli(["logs", "--context", "ctx"], cluster)
        self.assertEqual(code, 1)
        self.assertIn("exited 1", err)

    def test_a_closed_pipe_is_not_a_traceback(self):
        """`metalnap logs | head -1` closes the pipe under us."""
        cluster = make_cluster()

        class Closed(io.StringIO):
            def write(self, s):
                raise BrokenPipeError()
        # The handler points the real stdout at /dev/null. Left unpatched it
        # did exactly that to this test process, and everything written to
        # fd 1 after this test -- another test's failure, say -- vanished.
        with mock.patch("os.dup2") as dup2, \
                mock.patch("os.open", return_value=99):
            code = cli.main(["status", "--context", "ctx"], out=Closed(),
                            kubectl=lambda ctx: cli.Kubectl(
                                ctx, run=cluster.run, popen=cluster.popen))
        self.assertEqual(code, 0)
        dup2.assert_called_once()

    def test_a_partial_failure_names_what_was_already_changed(self):
        cluster = make_cluster(nodes=("a", "b", "c"))
        cluster.nodes.pop("b")            # listed, but gone from the cluster
        code, out, err = run_cli(["maintenance", "stop", "--all",
                                  "--context", "ctx"], cluster)
        self.assertEqual(code, 1)
        self.assertIn("a already given back", err)
        self.assertEqual([n for n, _p in cluster.patches], ["a"])

    def test_options_before_the_command_are_the_commands(self):
        """`metalnap --context prod status` -- the kubectl and helm habit."""
        with mock.patch.object(main_mod.cli, "main", return_value=0) as m:
            main_mod.main(["--context", "prod", "-n", "ops", "logs", "-f"])
        m.assert_called_once_with(["logs", "--context", "prod", "-n", "ops",
                                   "-f"])

    def test_options_and_names_go_in_any_order(self):
        """Parsed as subcommands, a node named after any option was rejected
        by argparse as an unrecognised argument -- and `start --reason x
        node1` is the order people type."""
        for argv in (["maintenance", "start", "--reason", "x", "a"],
                     ["maintenance", "--reason", "x", "start", "a"],
                     ["maintenance", "start", "a", "--reason", "x"]):
            cluster = make_cluster(nodes=("a", "b"))
            code, out, err = run_cli(argv + ["--context", "ctx"], cluster)
            self.assertEqual(code, 0, (argv, err))
            self.assertEqual(len(cluster.patches), 1, argv)

    def test_neither_names_nor_all_is_refused(self):
        cluster = make_cluster(nodes=("a", "b"))
        code, out, err = run_cli(
            ["maintenance", "start", "--reason", "x", "--context", "ctx"],
            cluster)
        self.assertEqual(code, 1)
        self.assertIn("name at least one node, or pass --all", err)
        self.assertEqual(cluster.patches, [])

    def test_all_patches_every_managed_node_in_order(self):
        cluster = make_cluster(nodes=("z", "a", "m"))
        code, out, err = run_cli(
            ["maintenance", "start", "--all", "--reason", "firmware",
            "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual([n for n, _ in cluster.patches], ["z", "a", "m"])
        for _, patch in cluster.patches:
            self.assertEqual(patch, {"metadata": {"annotations": {
                "metalnap.io/maintenance": "firmware",
                "metalnap.io/maintenance-started": None}}})

    def test_default_reason_names_the_user(self):
        cluster = make_cluster(nodes=("a",))
        with mock.patch("getpass.getuser", return_value="alice"):
            code, out, err = run_cli(
                ["maintenance", "start", "a", "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        _, patch = cluster.patches[0]
        self.assertEqual(
            patch["metadata"]["annotations"]["metalnap.io/maintenance"],
            "asked for by alice")
        self.assertIn("asked for by alice", out)


class TestMaintenanceStop(unittest.TestCase):
    def test_stop_nulls_both_annotations(self):
        cluster = make_cluster(nodes=("a", "b"))
        code, out, err = run_cli(
            ["maintenance", "stop", "a", "b", "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual([n for n, _ in cluster.patches], ["a", "b"])
        for _, patch in cluster.patches:
            self.assertEqual(patch, {"metadata": {"annotations": {
                "metalnap.io/maintenance": None,
                "metalnap.io/maintenance-started": None}}})
        self.assertIn("gave back a, b", out)


# ---------------------------------------------------------------------------
# logs: format_log(), and the argv it drives.
# ---------------------------------------------------------------------------

class TestFormatLog(unittest.TestCase):
    def test_basic_fields(self):
        line = json.dumps({"ts": "2024-03-01T12:34:56.789012Z",
                          "level": "info", "msg": "hello", "k": "v"})
        self.assertEqual(cli.format_log(line),
                         "2024-03-01 12:34:56 INFO  hello  k=v")

    def test_drops_mode_key(self):
        line = json.dumps({"ts": "2024-01-01T00:00:00Z", "level": "info",
                          "msg": "m", "mode": "dry_run"})
        self.assertNotIn("mode", cli.format_log(line))

    def test_json_encodes_non_string_values(self):
        line = json.dumps({"ts": "2024-01-01T00:00:00Z", "level": "info",
                          "msg": "m", "count": 3, "tags": ["a", "b"]})
        shown = cli.format_log(line)
        self.assertIn("count=3", shown)
        self.assertIn('tags=["a", "b"]', shown)

    def test_non_json_line_passes_through_when_unfiltered(self):
        self.assertEqual(cli.format_log("a raw kubectl complaint"),
                         "a raw kubectl complaint")

    def test_non_json_line_is_dropped_when_node_filtered(self):
        # Even though it names the node as plain text: only structured
        # records can be matched to a node, so an unparseable line is
        # dropped rather than guessed at.
        self.assertIsNone(
            cli.format_log("mentions node1 as plain text", node="node1"))

    def test_node_filter_matches_a_scalar_field(self):
        line = json.dumps({"ts": "t", "level": "i", "msg": "m",
                          "node": "node1"})
        self.assertIsNotNone(cli.format_log(line, node="node1"))
        self.assertIsNone(cli.format_log(line, node="node2"))

    def test_node_filter_matches_a_list_field(self):
        line = json.dumps({"ts": "t", "level": "i", "msg": "m",
                          "nodes": ["node1", "node2"]})
        self.assertIsNotNone(cli.format_log(line, node="node1"))
        self.assertIsNone(cli.format_log(line, node="node3"))

    def test_node_filter_matches_a_dict_key(self):
        line = json.dumps({"ts": "t", "level": "i", "msg": "m",
                          "per_node": {"node1": "cordoned"}})
        self.assertIsNotNone(cli.format_log(line, node="node1"))
        self.assertIsNone(cli.format_log(line, node="node3"))


class TestLogsCommand(unittest.TestCase):
    def test_follow_and_tail_reach_kubectl_argv(self):
        cluster = make_cluster(nodes=("a",))
        cluster.log_lines = [json.dumps({"ts": "2024-01-01T00:00:00Z",
                                        "level": "info", "msg": "hi",
                                        "node": "a"})]
        code, out, err = run_cli(
            ["logs", "-f", "--tail", "50", "--node", "a",
            "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(cluster.stream_calls), 1)
        argv = cluster.stream_calls[0]
        self.assertIn("-f", argv)
        self.assertEqual(argv[argv.index("--tail") + 1], "50")
        self.assertEqual(argv[1:3], ["--context", "ctx"])
        self.assertIn("hi", out)

    def test_header_names_context_and_controller(self):
        cluster = make_cluster(nodes=("a",), namespace="ops")
        cluster.log_lines = []
        code, out, err = run_cli(["logs", "--context", "ctx"], cluster)
        self.assertEqual(code, 0, err)
        self.assertIn("context:    ctx", out)
        self.assertIn("controller: ops/", out)


# ---------------------------------------------------------------------------
# kubectl missing entirely.
# ---------------------------------------------------------------------------

class TestKubectlMissing(unittest.TestCase):
    def test_missing_kubectl_on_an_ordinary_call(self):
        def boom(*a, **kw):
            raise FileNotFoundError()

        def kubectl_factory(ctx):
            return cli.Kubectl(ctx, run=boom, popen=boom)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["status", "--context", "ctx"], out=out,
                            kubectl=kubectl_factory)
        self.assertEqual(code, 1)
        self.assertIn("kubectl is not on your PATH", err.getvalue())

    def test_missing_kubectl_on_the_logs_stream(self):
        cluster = make_cluster(nodes=("a",))

        def boom(*a, **kw):
            raise FileNotFoundError()

        def kubectl_factory(ctx):
            return cli.Kubectl(ctx, run=cluster.run, popen=boom)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["logs", "--context", "ctx"], out=out,
                            kubectl=kubectl_factory)
        self.assertEqual(code, 1)
        self.assertIn("kubectl is not on your PATH", err.getvalue())


# ---------------------------------------------------------------------------
# __main__.main(): dispatch to the CLI vs. starting the controller.
# ---------------------------------------------------------------------------

class TestMainDispatch(unittest.TestCase):
    def test_known_command_goes_to_the_cli(self):
        with mock.patch.object(main_mod.cli, "main",
                              return_value=0) as m:
            code = main_mod.main(["status", "--context", "x"])
        m.assert_called_once_with(["status", "--context", "x"])
        self.assertEqual(code, 0)

    def test_unknown_command_exits_without_starting_the_controller(self):
        with self.assertRaises(SystemExit) as cm:
            main_mod.main(["bogus"])
        self.assertIn("unknown command", str(cm.exception))
        self.assertIn("bogus", str(cm.exception))

    def test_no_args_and_no_nodes_exits_with_nodes_required(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("NODES", None)
            with self.assertRaises(SystemExit) as cm:
                main_mod.main([])
        self.assertIn("NODES", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=1)
