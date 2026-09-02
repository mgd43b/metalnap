# Changelog

## chart 0.2.7 — 2026-09-02

Makes the Artifact Hub listing actually useful. The package indexed fine and
showed as a verified publisher, but the page was nearly blank because the chart
was missing everything the page renders from.

- **README.md** — the package page body IS the chart README, and there wasn't
  one. Covers install, the three values that are load-bearing, a values table,
  the safety rules, and what the RBAC deliberately withholds.
- **values.schema.json** — validated by `helm install/template`, so bad values
  fail at the client rather than producing a broken Deployment. Verified it
  rejects: an invalid mode, an *unquoted* mode (the YAML-boolean trap), a
  non-list `nodes`, and an out-of-range timer.
- **icon.svg** — three rack units, the bottom one powered down. Also clears
  `helm lint`'s "icon is recommended".
- **maintainers** now carry an email. Artifact Hub keys maintainers on email
  and silently drops an entry that has only a name and url, which is why the
  listing showed none despite Chart.yaml having one.


## chart 0.2.6 — 2026-09-01

Fails readably when `mode` is not a valid quoted string.

`on`, `off`, `yes` and `no` are YAML 1.1 **booleans**, so an unquoted
`mode: on` in a values file arrives as `true`. Without this check the chart
failed with *"incompatible types for comparison: bool and string"* from a
template comparison — which points at the template rather than at the one
character that caused it. And had it rendered, it would have emitted
`MODE: "true"`, an invalid mode that makes the controller refuse to start.

The trap is worst on the documented rollback: `--set mode=off` is safe (that
path stringifies), but `mode: off` written into a values file is not, and those
look interchangeable.

Now:

    mode: on        -> mode must be a QUOTED string, one of off|dry_run|on --
                       got true of type bool. Unquoted on/off/yes/no are YAML
                       booleans, so quote it.
    mode: banana    -> mode must be one of off|dry_run|on; got "banana"
    mode: "on"      -> renders


## chart 0.2.5 — 2026-09-01

Stops the name doubling when the release is named after the chart. `helm
install metalnap` produced `metalnap-metalnap`; it now produces `metalnap`,
using the standard Helm idiom. Other release names are unaffected
(`prod` still yields `prod-metalnap`).

Cosmetic, and deliberately NOT applied to a running install: changing resource
names on upgrade makes Helm create the new Deployment before removing the old,
which would briefly run two controllers in `on` — and two controllers race each
other's cordons, with the loser acting on state the winner already changed.
Worth having for the next install, not worth a race to retrofit.


## chart 0.2.4 — 2026-09-01

Chart-only fix: **the Warmup seam had no RBAC**. It was added in 0.2.0 and the
chart was never updated, so a real wake logged:

    warmup could not start; first work may pay the cost
    403 Forbidden .../pods/metalnap-warmup-k8s15

Found on metalnap's first production wake, minutes after cutover.

The design held, which is the point worth recording: because warmup runs
*after* the node is uncordoned, a broken warmup costs a slow first image pull
rather than stranding a node that is powered, Ready and serving nothing. Had
the ordering been the other way round this would have been an outage instead of
a warning.

The new Role is namespaced and narrow. `create` cannot be constrained by
resourceNames — RBAC has no name to match at admission time — but `delete` is
scoped to exactly `metalnap-warmup-<node>` for the nodes in `.Values.nodes`,
generated from that list so there is no second list to drift. It is emitted
only when `warmup.image` is set, and it deliberately does **not** grant
delete-any-pod: metalnap must not be able to remove a runner pod.


## v0.2.3 — 2026-09-01

Moves the scale-up fit guard to **after** the stranded reconcile, matching the
controller this replaces.

Found by a differential test that drives both implementations through identical
synthetic states: 40 divergences in 3,000 cases, every one of them here and
every one with `fits_node()` returning False. Guarding first meant a *stranded*
node — already powered, already cordoned — was put to sleep rather than
returned to service.

Both behaviours are safe; neither destroys work. Returning it wins on two
grounds: it matches the documented "wake readily, sleep reluctantly" bias, and
it means a transient fit-check failure cannot power off a node that was only
ever mid-wake.

After the move: **5,000/5,000 identical decisions.**


## v0.2.2 — 2026-09-01

**`dry_run` was not dry.** Two external mutations escaped it, both found within
an hour of shadowing metalnap next to the controller it is meant to replace —
which is the entire argument for shadowing.

1. **Notifications fired in `dry_run`.** A shadow created a live Alertmanager
   silence beside the incumbent's. A dry-run controller that silences alerts is
   not observing, it is participating.

2. **Cordon changes escaped `dry_run` via `tick()`.** `wake()` and `sleep()`
   check the mode and return early, but tick() also cordons directly — the
   mid-sleep abort and the stranded repair — and neither had a mode check.
   This one is dangerous: `stranded` is read from the CLUSTER, not from the
   controller's own state, so a shadow sharing the cordon annotation would
   **uncordon a node the live controller was mid-drain on**. It had not fired
   only because no sleep happened to occur while the shadow was up.

Every cordon change now goes through one mode-checked choke point. A guard at
each call site is a guard that gets missed at one of them — which is exactly
what happened.

Tests 19 → 23.


## v0.2.1 — 2026-09-01

Refuses to power-manage a **protected** node — one carrying
`node-role.kubernetes.io/control-plane` or `.../master`. Protected nodes are
dropped from consideration before any other code path sees them, so nothing
downstream can act on one by accident, and its healthy peers keep being
managed normally.

The controller this was extracted from had a hardcoded allowlist for the same
reason, with the comment *"a ConfigMap must never be able to aim this
controller at a core node"*. metalnap takes its node list from a ConfigMap or a
Helm value, which is the weakest link in the whole system: a typo there should
not be able to power off a machine the cluster cannot survive losing.

`NodeSource` implementations may set `NodeState.protected`; `KubeNodeSource`
derives it from labels and the set is overridable.


## v0.2.0 — 2026-09-01

Closes the three gaps between metalnap and the controller it was extracted
from. Each is a new seam rather than a hardcode, so they generalise past the
one deployment that needed them.

### Notifier (new, optional — but configure it)

Tell something a node is going away, and that it came back.
`AlertmanagerNotifier` is the reference.

Without this, every sleep pages someone: a node powering off looks exactly like
a node dying. Worse than the noise, it teaches people to ignore precisely the
alerts that would tell them a node had genuinely failed. metalnap now **refuses
to power off a node whose shutdown it could not announce** — the alert would
fire and nobody would know it was us.

Notifications are reconciled every tick, not on transitions, so one lost to a
restart is re-asserted, and — the half people forget — a stale one on a node
that is UP is cleared.

### Warmup (new, optional)

Prepare a node after it is Ready but before it matters. `ImagePrepull` runs a
one-shot pod so a large image lands in the local cache; measured on the origin
deployment, 450s cold against 0.69s warm.

Runs as its own phase **after** the node is already uncordoned. Warming first
means a slow warmup strands a node that is powered, Ready and serving nothing —
learned the hard way.

### DemandSignal.fits_node (new method — breaking)

`shortfall()` is a sum, which assumes everything waiting is waiting on
capacity. Work blocked on a selector, a taint or an unbound volume inflates it
and powers on hardware that cannot help. `PendingPodFit` answers it for
Kubernetes.

Skipped when saturation drove the demand: a saturated queue has nothing pending
to inspect, so applying the check there would veto every saturation-driven
wake.

**Breaking:** existing `DemandSignal` implementations must add `fits_node`.
Return `True` if you cannot tell. The API is explicitly unstable pre-1.0.

### Tests

16 unit tests (was 11) and three new simulation invariants. Verified by
mutation: dropping the pre-power-off announcement, and never clearing a stale
notification, each fail 40/40 seeds.


## chart 0.1.1 — 2026-09-01

Chart-only fix; `appVersion` stays 0.1.0.

Removes an invalid `artifacthub.io/signKey` annotation. It carried a
placeholder fingerprint and no `url`, and `url` is mandatory once the entry
exists — so Artifact Hub failed to index the package with *"sign key url not
provided"*. Chart 0.1.0 is left as published; versions are immutable.

Adds `charts/validate.py`, run by CI on every push and again before release.
`helm lint` has no knowledge of Artifact Hub's annotation spec, so an invalid
annotation packages cleanly, publishes cleanly, and only fails at indexing —
surfacing as an email hours later, if anyone is watching for it.

## v0.1.0 — 2026-09-01

First release. Extracted from a controller that has been sleeping and waking a
two-node Supermicro Twin serving GitHub Actions CI.

### What it does

Watches a demand signal, works out how many bare-metal nodes should be awake,
and powers them off and on to match — draining gracefully first, and never
interrupting running work.

Three duck-typed seams (`DemandSignal`, `DrainPolicy`, `PowerBackend`) with
Prometheus, GitHub ARC and IPMI as reference implementations. `NodeSource`
covers reading node state and applying cordons.

### Ships with

- `python -m metalnap` — the reference stack wired entirely from environment
  variables, so the container image is useful without writing code
- container image at `ghcr.io/mgd43b/metalnap`, linux/amd64 and linux/arm64
- Helm chart at `oci://ghcr.io/mgd43b/charts/metalnap`
- two test suites: deterministic unit tests, and a simulation harness that
  drives thousands of ticks against a fake cluster asserting safety **and**
  liveness after every tick

### Known limits

- Run in exactly one environment. The API is not stable.
- The durable drain deadline (anchoring the timeout to the cordon annotation
  rather than in-memory state) is new and has not yet run against real
  hardware, unlike the rest of the safety logic.
- Not on PyPI; that needs a trusted publisher configured first.
- `DemandSignal.saturated_units()` is modelled on ARC's listener metrics. If
  your scheduler exposes no equivalent, return 0 and lose only the
  capped-queue case.

### Defaults worth knowing

`MODE` ships as `dry_run`. The chart sets no tolerations, deliberately: metalnap
must never be scheduled onto a node it manages, or it will cordon and power off
the machine it is running on.
