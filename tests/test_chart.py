"""
The Helm chart, rendered. `helm lint` and `helm template` are the only things
that run a chart, so these run them -- and are skipped, loudly, where helm is
not installed. CI has it.

What they pin is what has no other test: that `static: 0` survives the template
(the one value a template is most likely to read as unset), that a ceiling's
two sources are refused together, and that the objects a ceiling needs exist
only when one is set.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHART = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "charts", "metalnap")
HELM = shutil.which("helm")
#: `--skip-schema-validation` is how the template's own checks are reached past
#: the schema's, and older helms do not have it.
CAN_SKIP_SCHEMA = bool(HELM) and "skip-schema-validation" in subprocess.run(
    [HELM, "template", "--help"], capture_output=True, text=True).stdout


class TestHelmIsThere(unittest.TestCase):
    def test_helm_is_installed_where_it_is_required(self):
        """Without it every test below is skipped, which reads as green. CI
        sets REQUIRE_HELM so that a runner image that loses helm fails here
        instead of passing nothing."""
        if not os.environ.get("REQUIRE_HELM"):
            self.skipTest("REQUIRE_HELM is not set")
        self.assertTrue(HELM, "helm is required and is not on PATH")


@unittest.skipUnless(HELM, "helm is not installed")
class TestChart(unittest.TestCase):
    def render(self, values="", *flags):
        """(returncode, stdout, stderr) of `helm template` on `values`."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write("nodes: [node1, node2]\n" + values)
            f.flush()
            r = subprocess.run([HELM, "template", "metalnap", CHART, "-f",
                                f.name, *flags], capture_output=True,
                               text=True)
        return r.returncode, r.stdout, r.stderr

    def ok(self, values="", *flags):
        code, out, err = self.render(values, *flags)
        self.assertEqual(code, 0, err)
        return out

    def kinds(self, out):
        return [ln.split(":", 1)[1].strip() for ln in out.splitlines()
                if ln.startswith("kind:")]

    def test_it_lints(self):
        for values in ("", "capacityCeiling: {static: 0}\n",
                       "metrics: {port: 9100}\n"):
            with self.subTest(values=values):
                with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
                    f.write("nodes: [node1]\n" + values)
                    f.flush()
                    r = subprocess.run([HELM, "lint", CHART, "-f", f.name],
                                       capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # -- off by default ------------------------------------------------------
    def test_a_default_install_has_no_ceiling_no_status_and_no_listener(self):
        out = self.ok()
        for absent in ("CEILING_QUERY", "CEILING_STATIC", "STATUS_CONFIGMAP",
                       "METRICS_PORT", "containerPort", "metalnap-status"):
            self.assertNotIn(absent, out)
        self.assertNotIn("Service", self.kinds(out),
                         "there is deliberately no Service")
        self.assertEqual(self.kinds(out).count("Role"), 0)

    def test_the_two_timers_are_always_rendered(self):
        out = self.ok()
        self.assertIn('CEILING_RELEASE_HOLD_S: "900"', out)
        self.assertIn('CEILING_DRAIN_DEADLINE_S: "600"', out)

    def test_the_read_timeout_is_rendered_and_bounded_by_the_interval(self):
        self.assertIn('CEILING_TIMEOUT_S: "5"', self.ok())
        self.assertIn('CEILING_TIMEOUT_S: "2"',
                      self.ok("capacityCeiling: {timeoutS: 2}\n"))
        for values in ("capacityCeiling: {timeoutS: 0}\n",
                       "capacityCeiling: {timeoutS: -1}\n",
                       "capacityCeiling: {timeoutS: 61}\n",
                       "timers: {intervalS: 10}\ncapacityCeiling: "
                       "{timeoutS: 11}\n"):
            with self.subTest(values=values):
                code, _out, _err = self.render(values)
                self.assertNotEqual(code, 0, "rendered a timeout the "
                                             "controller refuses to start on")
                if CAN_SKIP_SCHEMA:
                    # The template says so itself, for the one the schema
                    # cannot know (the interval is another value).
                    code, _out, err = self.render(values,
                                                  "--skip-schema-validation")
                    self.assertNotEqual(code, 0)
                    self.assertIn("timeoutS", err)

    # -- a query -------------------------------------------------------------
    def test_a_query_reaches_the_controller_verbatim(self):
        q = 'vector(0) and on() (max(temp{sensor="Inlet Temp"}) > 35)'
        out = self.ok("capacityCeiling:\n  query: '%s'\n" % q)
        self.assertIn('CEILING_QUERY: "vector(0) and on() (max(temp{sensor='
                      '\\"Inlet Temp\\"}) > 35)"', out)
        self.assertNotIn("CEILING_STATIC", out)

    # -- static: 0 is a ceiling ----------------------------------------------
    def test_a_static_zero_is_rendered_not_dropped(self):
        """THE trap. A template that tests `if .static`, or pipes through
        `default`, reads 0 as unset -- and renders no ceiling at all, in the one
        case where the ceiling is shed-everything."""
        out = self.ok("capacityCeiling:\n  static: 0\n")
        self.assertIn('CEILING_STATIC: "0"', out)
        self.assertIn("STATUS_CONFIGMAP", out)

    def test_a_static_value_is_rendered_as_an_integer(self):
        self.assertIn('CEILING_STATIC: "2"',
                      self.ok("capacityCeiling:\n  static: 2\n"))

    def test_null_is_no_ceiling(self):
        self.assertNotIn("CEILING_STATIC",
                         self.ok("capacityCeiling:\n  static: null\n"))

    # -- the two sources together --------------------------------------------
    def test_a_query_and_a_static_value_together_are_refused(self):
        both = "capacityCeiling:\n  query: 'vector(1)'\n  static: 0\n"
        code, _out, err = self.render(both)
        self.assertNotEqual(code, 0, "rendered a ceiling with two sources")
        # The schema says so first. With it skipped, the template does -- and
        # 0 counts as a source there too.
        if CAN_SKIP_SCHEMA:
            code, _out, err = self.render(both, "--skip-schema-validation")
            self.assertNotEqual(code, 0)
            self.assertIn("mutually exclusive", err)

    def test_a_negative_number_is_refused(self):
        for values in ("static: -1", "releaseHoldS: -1", "drainDeadlineS: -1"):
            with self.subTest(values=values):
                if CAN_SKIP_SCHEMA:
                    code, _out, err = self.render(
                        "capacityCeiling: {%s}\n" % values,
                        "--skip-schema-validation")
                    self.assertNotEqual(code, 0)
                    self.assertIn("must be 0 or more", err)
                code, _out, _err = self.render("capacityCeiling: {%s}\n"
                                               % values)
                self.assertNotEqual(code, 0, "the schema let it through")

    def test_zero_is_allowed_for_both_durations(self):
        out = self.ok("capacityCeiling: {releaseHoldS: 0, drainDeadlineS: 0}\n")
        self.assertIn('CEILING_RELEASE_HOLD_S: "0"', out)
        self.assertIn('CEILING_DRAIN_DEADLINE_S: "0"', out)

    # -- the status object and its RBAC ---------------------------------------
    def test_a_ceiling_brings_an_empty_status_object_and_a_scoped_role(self):
        out = self.ok("capacityCeiling:\n  static: 0\n")
        self.assertIn('STATUS_CONFIGMAP: "metalnap-status"', out)
        self.assertEqual(self.kinds(out).count("Role"), 1)
        self.assertEqual(self.kinds(out).count("RoleBinding"), 1)
        role = out[out.index("kind: Role\n"):]
        role = role[:role.index("\n---")]
        self.assertIn('verbs: ["get", "update"]', role)
        self.assertIn("resourceNames:\n      - metalnap-status", role)
        for forbidden in ("create", "patch", "list", "delete", "watch"):
            self.assertNotIn(forbidden, role.split("rules:")[1].replace(
                "# create", ""), "the status role grants more than it needs")

    def test_the_status_object_carries_no_data_so_an_upgrade_leaves_it_alone(self):
        out = self.ok("capacityCeiling:\n  static: 0\n")
        obj = out[out.index("name: metalnap-status"):]
        obj = obj[:obj.index("\n---")]
        self.assertNotIn("data:", obj)

    def test_no_role_when_rbac_is_not_created_but_the_object_still_is(self):
        out = self.ok("capacityCeiling:\n  static: 0\nrbac: {create: false}\n")
        self.assertEqual(self.kinds(out).count("Role"), 0)
        self.assertIn("name: metalnap-status", out)

    def test_the_status_name_follows_the_release_fullname(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write("nodes: [node1]\ncapacityCeiling: {static: 0}\n")
            f.flush()
            out = subprocess.run(
                [HELM, "template", "other", CHART, "-f", f.name],
                capture_output=True, text=True).stdout
        # The CLI derives it from the Deployment's name, so the two must agree.
        self.assertIn("name: other-metalnap\n", out)
        self.assertIn('STATUS_CONFIGMAP: "other-metalnap-status"', out)

    # -- metrics --------------------------------------------------------------
    def test_a_metrics_port_opens_exactly_one_containerport_and_no_service(self):
        out = self.ok("metrics:\n  port: 9100\n")
        self.assertIn('METRICS_PORT: "9100"', out)
        self.assertEqual(out.count("containerPort: 9100"), 1)
        self.assertNotIn("Service", self.kinds(out))

    def test_a_port_the_container_cannot_bind_is_refused(self):
        """It runs unprivileged with every capability dropped, so a port below
        1024 would start a pod that never listens."""
        for port in (-1, 80, 1023, 65536):
            with self.subTest(port=port):
                code, _out, _err = self.render("metrics: {port: %d}\n" % port)
                self.assertNotEqual(code, 0)

    def notes(self, values=""):
        """NOTES.txt as `helm install` would print it, on every helm.

        `helm template` does not render NOTES.txt, and `helm install
        --dry-run=client` reaches for an API server on some versions and not
        on others. So the chart is copied, NOTES.txt becomes a named template,
        and an ordinary manifest carries what it renders -- from the same
        values and the same template text, on every version, which is all that
        is being asserted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            chart = os.path.join(tmp, "metalnap")
            shutil.copytree(CHART, chart)
            templates = os.path.join(chart, "templates")
            with open(os.path.join(templates, "NOTES.txt")) as f:
                text = f.read()
            os.remove(os.path.join(templates, "NOTES.txt"))
            with open(os.path.join(templates, "_zz.tpl"), "w") as f:
                f.write('{{- define "zz.notes" -}}\n%s\n{{- end -}}\n' % text)
            with open(os.path.join(templates, "zz-notes.yaml"), "w") as f:
                f.write("apiVersion: v1\nkind: ConfigMap\n"
                        "metadata:\n  name: notes\ndata:\n  notes: |\n"
                        '{{ include "zz.notes" . | indent 4 }}\n')
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
                f.write("nodes: [node1]\n" + values)
                f.flush()
                r = subprocess.run(
                    [HELM, "template", "metalnap", chart, "-f", f.name,
                     "--show-only", "templates/zz-notes.yaml"],
                    capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        block = r.stdout.split("  notes: |\n", 1)[1]
        out = "\n".join(ln[4:] for ln in block.splitlines())
        self.assertIn("installed as metalnap", out,
                      "rendered something, but not the notes")
        return out

    def test_notes_say_nothing_of_a_ceiling_that_is_not_set(self):
        self.assertNotIn("CEILING", self.notes())

    def test_notes_say_what_a_ceiling_does_and_that_it_is_in_dry_run(self):
        text = self.notes("capacityCeiling: {static: 0}\n"
                          "metrics: {port: 9100}\n")
        self.assertIn("A CAPACITY CEILING IS CONFIGURED (static: at most 0 "
                      "node(s) awake)", text)
        self.assertIn("600s after it was shed is shut down", text)
        self.assertIn("get configmap metalnap-status", text)
        self.assertIn("In dry_run it only logs what it WOULD shed", text)
        self.assertIn("Metrics are served on :9100/metrics", text)

    def test_notes_warn_when_a_busy_node_will_never_be_shed(self):
        text = self.notes("capacityCeiling: {static: 1, drainDeadlineS: 0}\n")
        self.assertIn("drainDeadlineS is 0, so a node carrying work is never "
                      "shed", text)
        self.assertNotIn("is shut down", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
