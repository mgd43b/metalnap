# Changelog

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
