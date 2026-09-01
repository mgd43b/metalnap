# Changelog

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
