# metalnap

**Suspend idle bare-metal Kubernetes nodes to save power — and never kill
running work to do it.**

[![ci](https://github.com/mgd43b/metalnap/actions/workflows/ci.yml/badge.svg)](https://github.com/mgd43b/metalnap/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/mgd43b/metalnap?label=release&color=blue)](https://github.com/mgd43b/metalnap/releases/latest)
[![Artifact Hub](https://img.shields.io/endpoint?url=https://artifacthub.io/badge/repository/metalnap)](https://artifacthub.io/packages/search?repo=metalnap)
[![image](https://img.shields.io/badge/ghcr.io-metalnap-2496ED?logo=docker&logoColor=white)](https://github.com/mgd43b/metalnap/pkgs/container/metalnap)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

metalnap watches a demand signal, works out how many machines should be awake,
and powers physical nodes off and on over IPMI (or Redfish, or Wake-on-LAN, or
a PDU) to match. It drains a node gracefully before pulling its power, and it
will not interrupt work that is running.

**Why not an existing autoscaler?** Cluster Autoscaler and Karpenter scale by
calling a cloud API — there is nothing to call when the machine is in your
rack. Metal3 and Ironic manage the whole bare-metal provisioning lifecycle,
which is a much larger commitment than "turn this box off overnight".
`kube-green` and similar scale *workloads*, not hardware, so the power bill
does not move. metalnap does the one narrow thing none of those do.

**Good fit if:** you run self-hosted CI runners, batch or burst capacity on
your own hardware, a homelab or on-prem cluster with a real power bill, and
your jobs take minutes to hours and must not be killed mid-flight.

**Not a fit if:** your nodes are cloud instances (use Karpenter), your
workloads are stateless web services you can simply scale to zero, or you need
full provisioning and inventory management (use Metal3).

Extracted from a controller that has been sleeping and waking a two-node
Supermicro Twin serving GitHub Actions CI.

> **Status: v0.1, early.** The core is exercised hard — see [Testing](#testing)
> — but the API is not stable and it has run in exactly one environment. The
> safety rules below are the mature part; the packaging is not.

## What it does

```
       demand signal              this controller              your hardware
  unmet work + saturated  ──▶  should N nodes be awake?  ──▶  IPMI / Redfish
        queues                  drain safely, then power        WoL / PDU
```

Each tick: observe every node, work out how many should be awake, and take **at
most one** corrective action. Waking and sleeping are phase machines that take a
single non-blocking step per tick, so a node draining for half an hour never
stops another being woken.

## The rules it will not break

Every one of these exists because breaking it cost something real.

- **Never interrupt running work.** Not by eviction, not by power. If a check
  cannot tell whether a node is busy, that reads as busy. Reading the wrong
  field once destroyed four live CI jobs.
- **An operator's cordon outranks the controller** — including decisions
  already in flight. Enforce it where operations *finish*, not only where they
  start. A wake begun before the cordon once completed and uncordoned the
  operator, in the same tick that logged the node as held.
- **Idle workers do not leave on their own.** A warm pool waits forever for
  work a cordoned node will not receive, so it must be released explicitly.
  Waiting for it instead livelocked a controller for two and a half hours.
- **Never block the reconcile loop.** A blocking drain froze every other
  decision for up to the drain timeout.
- **Bound every retry, and say so when a bound is hit.** Silent
  non-convergence hides longest.
- **Wake readily, sleep reluctantly** — and hold evidence of demand across the
  dips a noisy signal produces. A queue sitting at its ceiling makes demand
  flicker; a timer that resets on every dip never fires.

## The three seams

`metalnap` is opinionated about safety and unopinionated about everything else.
Implement three small duck-typed interfaces (`metalnap/types.py`):

| seam | question it answers | reference implementation |
|---|---|---|
| `DemandSignal` | how much capacity is wanted, and would it fit here? | `PrometheusSignal` + `PendingPodFit` |
| `DrainPolicy` | what is *busy* here, and how do I release an idle unit? | `ArcDrain` (GitHub ARC runners) |
| `PowerBackend` | how do I turn this box on and off? | `IpmiPower` (ipmitool) |
| `Notifier` *(optional)* | tell something a node is going away, and came back | `AlertmanagerNotifier` |
| `Warmup` *(optional)* | prepare a node before it takes work | `ImagePrepull` |

Plus `NodeSource` for reading node state and applying cordons — `KubeNodeSource`
covers Kubernetes.

**Configure a `Notifier` even though it is optional.** A node powering off looks
exactly like a node dying, so without one every sleep pages someone — and worse,
it teaches people to ignore precisely the alerts that would tell them a node had
genuinely failed. metalnap refuses to power off a node whose shutdown it could
not announce, for the same reason.

### One thing worth stealing even if you use none of the above

`DemandSignal.saturated_units()`. Most demand signals are derived from work the
scheduler has **already admitted** — pending pods, queued items. A queue at its
own ceiling admits nothing further, so real demand becomes invisible *exactly*
when extra capacity is most needed. This bit us in production: a runner pool
pinned at its cap with jobs waiting, and the controller reporting zero unmet
demand and preparing to sleep the last awake node.

## Testing

The controller this came from shipped eight bugs to production. Its unit suite
caught **zero** of them — three were found in production, two by external
review, two by re-reading the code, one by an operator noticing the numbers
didn't add up. Every one lived in a *sequence*: a restart then a sleep, a
warm-pool worker landing then a sleep, demand oscillating across three ticks. A
test that calls `tick()` once cannot see any of them.

So there are two suites, and they do different jobs:

```bash
python3 -B tests/test_controller.py               # deterministic, precise
python3 -B tests/sim.py --seeds 60 --ticks 900    # ~54k ticks, ~2s
```

`tests/sim.py` drives the controller through thousands of ticks against a fake
cluster and fake BMCs, with phased demand, hung work, operator maintenance and
injected restarts, asserting **safety and liveness** after every tick. Liveness
matters more than it looks: safety alone is satisfied by a controller that does
nothing, and the first version of this harness reported OK across 250 ticks
while never once sleeping a node.

Both suites are validated by mutation — reintroduce a bug, watch it fail. The
harness's own docstring records what it catches, at what rate, and what it
cannot reach.

## Try it

The simulation harness needs no cluster, no hardware and no dependencies:

```bash
git clone https://github.com/mgd43b/metalnap && cd metalnap
python3 -B tests/sim.py --seeds 20 --ticks 400
```

## Install with Helm

```bash
# BMC credentials are created out of band -- they do not belong in values.yaml
kubectl create ns metalnap
kubectl -n metalnap create secret generic metalnap-bmc \
  --from-literal=user=ADMIN --from-literal=pass='<bmc-password>'

helm install metalnap oci://ghcr.io/mgd43b/charts/metalnap \
  -n metalnap --version 0.1.0 \
  --set 'nodes={node1,node2}' \
  --set bmc.hostFormat='{node}-ipmi.internal.example.org' \
  --set prometheus.url=http://prometheus-k8s.monitoring.svc:9090
```

It installs in **`dry_run`** and touches nothing. Watch what it decides:

```bash
kubectl -n metalnap logs -l app.kubernetes.io/name=metalnap -f
```

When the decisions look right:

```bash
helm upgrade metalnap oci://ghcr.io/mgd43b/charts/metalnap \
  -n metalnap --reuse-values --set mode=on
```

`--set mode=off` is the rollback, and it never touches a node on the way out.

> The chart sets **no tolerations**, deliberately. metalnap must not be
> scheduled onto a node it manages, or it will cordon and power off the machine
> it is running on.

## Run it directly

A container image is published to GitHub Container Registry on each release:

```bash
docker pull ghcr.io/mgd43b/metalnap:latest
docker run --rm ghcr.io/mgd43b/metalnap:latest --help
```

`python -m metalnap` wires up the reference stack — Kubernetes + GitHub ARC +
IPMI + Prometheus — entirely from environment variables, so the image is
useful without writing code:

```bash
NODES=k8s14,k8s15 BMC_HOST_FMT='{node}-ipmi.internal.example.org' BMC_USER=... BMC_PASS=... PROM_URL=http://prometheus:9090 MODE=dry_run   python3 -m metalnap
```

**It ships as `MODE=dry_run`** and will not touch anything until you say
otherwise. Leave it there until the decisions in the log look right.

If your stack differs, import `Controller` and pass your own seams — see
[`examples/arc-ipmi/`](examples/arc-ipmi/) for the full wiring including RBAC,
and `metalnap/__main__.py` as a worked example.

Installing as a library: `pip install -e .` (`requests` is the only runtime
dependency; `ipmitool` on PATH for the IPMI backend). Not on PyPI yet.

## Configuration

Everything is an environment variable, because everything here is an
operational knob you may need to turn during an incident (`metalnap/config.py`).

`MODE` is `off` | `dry_run` | `on`. **`dry_run` observes and logs every decision
it would take without touching anything** — run it there first, for as long as
it takes to trust the numbers.

## Artifact Hub

Listed as a **Helm charts** repository (there is no "OCI" kind — OCI is
expressed by the URL scheme) pointing at `oci://ghcr.io/mgd43b/charts/metalnap`.

The `artifacthub-repo.yml` ownership file is **not** served over HTTP and
**not** packaged inside the chart. For an OCI repository Artifact Hub reads a
separate OCI artifact in the same repository, tagged `artifacthub.io`, carrying
a layer of media type
`application/vnd.cncf.artifacthub.repository-metadata.layer.v1.yaml`. The
release workflow pushes it; `oras repo tags` should show both `artifacthub.io`
and the chart version.

## Releasing

Releases are automated by
[release-please](https://github.com/googleapis/release-please). There is no
script to run and nobody to ask:

1. Merge changes to `main` using **Conventional Commit** subjects
   (`feat:`, `fix:`, `safety:`, `docs:`, `ci:`, `refactor:`, `test:`).
2. release-please keeps one open PR titled `chore(main): release X.Y.Z`,
   accumulating everything unreleased.
3. **Merging that PR is the release.** It bumps every version location, writes
   the changelog, tags, and creates the GitHub Release. The tag then triggers
   the image and chart publish.

The version bump derives from the commits: `fix:` → patch, `feat:` → minor, and
`!` or a `BREAKING CHANGE:` footer → minor while pre-1.0.

Two things worth knowing:

- **Chart version and appVersion now move together.** Every release rebuilds
  the image, even for a chart-only change. That costs ~2 minutes of CI and
  removes the drift that came of maintaining them separately — image `0.2.4`
  once shipped carrying `__version__ = "0.2.3"`.
- **Write a real commit body.** The changelog takes the subject line, but the
  body is where the reasoning lives, and in this project that reasoning *is*
  the documentation. A subject alone tells the next person what changed and
  never why.

## Contributing

Two things make a change reviewable here:

1. **Add the test before the fix**, and show it failing. Both suites are
   mutation-verified; a test that passes against the broken code is worse than
   none, and this project has shipped that mistake more than once.
2. **Say which rule the change touches.** If it relaxes one of the safety rules
   above, the pull request should say which incident makes that safe now.

Bug reports are most useful with the seed and tick count if the simulation
harness found it — every run is reproducible from those two numbers.

## Licence

MIT.
