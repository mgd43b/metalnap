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

> **Status: pre-1.0, early.** The core is exercised hard — see [Testing](#testing)
> — but the API is not stable and it has run in exactly one environment. The
> safety rules below are the mature part; the packaging is not.

## What it does

```
     demand signal ──┐
  unmet work, and a  │          this controller            your hardware
   queue at its cap  ├──▶  should N nodes be awake?  ──▶  IPMI / Redfish
                     │     drain safely, then power          WoL / PDU
     the calendar ───┤
  asleep long enough │
   to have missed a  │
   month of updates  │
                     │
       an operator ──┘
   "I need node1 for
    a kernel upgrade"
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
  flicker; a timer that resets on every dip never fires. A node carrying work
  is demand too: counting only the work still *waiting* read a backlog the
  pool had just absorbed as no demand at all, and drained every busy node in
  it. Only a node that has carried no work for a whole sleep window is put
  down.
- **A node nobody wants still has to be maintained.** One that sleeps for
  weeks misses every package update and config run, then has to catch all of
  it up at the exact moment demand finally wanted it. Scheduled wakeups fix
  that — and they are the lowest-priority thing the controller does, yielding
  to demand, to an operator, and to any operation already in flight.
- **Mute only what you put down.** A node that crashed looks exactly like one
  that was slept — dark, unreachable — and the more reliable the sleeps are,
  the more routine a crash looks. Silencing every dark managed node once kept a
  crashed one quiet for 21 hours.
- **Escalate once, then hand it to a human.** A machine whose kernel has locked
  up reads "on" to its BMC and ignores a soft shutdown indefinitely; one power
  cycle is what a person would try first. One that needs a second inside the
  cooldown needs the person — see [When a node will not come
  back](#when-a-node-will-not-come-back).
- **When a person asks for a node, give it to them and get out of the way.**
  [Maintenance mode](#maintenance-mode) powers the node on once, then leaves it
  alone until it is given back. Someone mid-upgrade reboots and powers off on
  purpose, and every remedy this controller has for a node doing that — sleep
  it, cycle it, mute it — is the wrong one.

## The three seams

`metalnap` is opinionated about safety and unopinionated about everything else.
Implement three small duck-typed interfaces (`metalnap/types.py`):

| seam | question it answers | reference implementation |
|---|---|---|
| `DemandSignal` | how much capacity is wanted, and would it fit here? | `PrometheusSignal` + `PendingPodFit` |
| `DrainPolicy` | what is *busy* here, and how do I release an idle unit? | `ArcDrain` (GitHub ARC runners) |
| `PowerBackend` | how do I turn this box on and off — and power-cycle it when it wedges? | `IpmiPower` (ipmitool) |
| `Notifier` *(optional)* | mute a node metalnap put down; raise an alert when one needs a human | `AlertmanagerNotifier` |
| `Warmup` *(optional)* | prepare a node before it takes work | `ImagePrepull` |

Plus `NodeSource` for reading node state and applying cordons — `KubeNodeSource`
covers Kubernetes.

`PowerBackend.cycle()`, `NodeSource.note()` / `disown()` and
`Notifier.alert()` / `clear_alert()` are optional: a backend without them
still works, and a wedged node is then alerted on rather than power-cycled.

**Configure a `Notifier` even though it is optional.** A node powering off looks
exactly like a node dying, so without one every sleep pages someone — and worse,
it teaches people to ignore precisely the alerts that would tell them a node had
genuinely failed. metalnap refuses to power off a node whose shutdown it could
not announce, for the same reason.

It mutes **only nodes metalnap put down**: dark and carrying its cordon. A node
that crashed in service, one an operator holds or has asked for in
[maintenance mode](#maintenance-mode), and one metalnap has stopped being able
to account for all stay loud. `AlertmanagerNotifier` creates one
silence per label in `ALERTMANAGER_SILENCE_LABELS` (default `instance,node`),
because a node's alerts do not agree on one: kube-state-metrics alerts such as
`KubeNodeUnreachable` name it in `node`, relabelled node-exporter alerts in
`instance`. Muting `instance` alone let every sleep through as
`KubeNodeUnreachable` — and a real `KubeNodeNotReady` arrived buried among
them. Narrow the silences to what a sleep is *expected* to trip with
`ALERTMANAGER_SILENCE_MATCHERS`, so anything else a sleeping node raises still
arrives. Every silence also excludes metalnap's own alert, which carries the
node's labels, so it is never muted by a silence on the node it is about —
that exclusion is a negative matcher, and **needs Alertmanager 0.22 or later**.

### One thing worth stealing even if you use none of the above

`DemandSignal.saturated_units()`. Most demand signals are derived from work the
scheduler has **already admitted** — pending pods, queued items. A queue at its
own ceiling admits nothing further, so real demand becomes invisible *exactly*
when extra capacity is most needed. This bit us in production: a runner pool
pinned at its cap with jobs waiting, and the controller reporting zero unmet
demand and preparing to sleep the last awake node.

## When a node will not come back

A wake waits `WAKE_TIMEOUT_S` for the node to become Ready. What happens then
depends on what its BMC says:

- **Chassis off.** The power-on did not take. The next wake tries again.
- **Chassis on.** The machine is up and wedged — the state a locked kernel sits
  in, reading "on" for as long as anyone waits. metalnap **power-cycles it
  once** and gives it one more wake timeout. It will not, and says why, if:
  - an operator's cordon is on it (checked on a fresh read, at the moment of
    the cycle);
  - the cluster still lists work running on it — NotReady is not dead, and a
    kubelet cut off from the API server leaves its jobs running;
  - it was cycled within `POWER_CYCLE_COOLDOWN_S` (default a day), per the
    `metalnap.io/power-cycled` annotation on the node;
  - a maintenance visit powered it on within two `MAINTENANCE_TIMEOUT_S`, and
    it may be rebooting into an update — unless this wake found it off and
    powered it on itself.

A node that is still not Ready after its cycle, or that may not be cycled, is
**handed to a human**: unmuted, alerted on as `MetalnapNodeNeedsAttention`
(labels `node`, `instance`, `severity=critical` — route it to a page), and
passed over by demand, which wakes other nodes instead, and by maintenance
visits. The alert clears when the node becomes Ready or an operator takes it.

**Taking a node from metalnap** means taking its mark off, not cordoning it: a
node metalnap slept is already cordoned, so `kubectl cordon` changes nothing it
can see. Keep the cordon and remove the mark, and it is yours:

```bash
kubectl annotate node <node> metalnap.io/cordoned-     # your cordonAnnotation
```

**Re-arming the power cycle** before the cooldown is out is removing its
record; metalnap may try the node again from the next tick:

```bash
kubectl annotate node <node> metalnap.io/power-cycled-
```

Everything metalnap must remember about a node across its own restarts lives
on the node as a `metalnap.io/` annotation — `power-cycled`, `visited` (a
maintenance visit's power-on), `shutdown` (a shutdown requested and not yet
confirmed), `trouble` (why it was handed to a human) and `maintenance-started`
(an operator's [maintenance request](#maintenance-mode) taken up). Each was
once held in memory, and each time a restart could undo a safety decision with
it. `metalnap.io/maintenance` is the one metalnap reads and never writes: it is
the operator's.

A soft shutdown is confirmed, not assumed: the node stays in flight, and muted,
until its BMC reads off **and** it reads NotReady. One that has not gone down
after `SHUTDOWN_TIMEOUT_S` is reported — never forced, because a hard cut is
not a fix for a slow shutdown: if it is dark and powered, it is handed to a
human like any wedged node; if it is still Ready, having ignored the request,
it is alerted on while metalnap keeps asking, bounded by `MAX_SLEEP_ATTEMPTS`.
A node carrying metalnap's cordon that is found powered with no operation to
explain it — a shutdown that wedged, a wake a restart forgot — or whose BMC
cannot be read at all, is given one wake timeout's grace and then reported
too.

A wake that metalnap starts from cold counts as capacity on its way, so one
node's worth of demand boots one node rather than one per tick until the first
comes up. A node found already powered does not count: it may be wedged, and
never arrive.

## Maintenance mode

A node metalnap has put to sleep is dark, cordoned and ownerless as far as
anyone at a terminal can tell, and powering it on by hand just hands it back to
a controller that will put it to sleep again. So ask metalnap for it:

```bash
metalnap maintenance start node1 --reason "kernel 6.8"     # or --all
metalnap status
metalnap maintenance stop node1
```

or, with nothing but `kubectl`:

```bash
kubectl annotate --overwrite node node1 metalnap.io/maintenance="kernel 6.8"
kubectl annotate node node1 metalnap.io/maintenance- metalnap.io/maintenance-started-
```

What metalnap does with a node while the request stands:

- **Powers it on — once.** If the chassis is off, it is powered on, one node
  per tick, so asking for a whole rack does not start it in unison. That
  power-on is put on record on the node *before* it is made
  (`metalnap.io/maintenance-started`), and a request carrying that record is
  never powered on again: if you power the machine off to reseat a DIMM, it
  stays off. `maintenance stop` then `start` asks for another (or remove the
  record by hand). `start` on a node already asked for only updates the
  reason, so `start --all` cannot power back on a machine somebody switched
  off.
- **Once, and bounded.** A power-on that keeps failing is retried for a wake
  timeout, then given up on and said so; the node is yours to power at the
  BMC.
- **Then nothing else.** No sleep, no drain, no power cycle, however long it
  sits dark and powered — that is a reboot here, not a wedge. No silence, and
  no `MetalnapNodeNeedsAttention`: a node being worked on is loud to whoever is
  working on it, and a silence is theirs to make.
- **It leaves the cordon as it found it.** A node woken from sleep keeps
  metalnap's cordon, so no work lands while you reboot it; host-level updates,
  config management and DaemonSets run regardless. A node that was in service
  stays in service. `kubectl cordon` and `uncordon` are yours throughout.
- **It is out of the pool.** Demand neither wakes, sleeps nor counts it, the
  way it does not count a node an operator cordoned; scheduled visits pass it
  over. A wake, sleep or visit already under way is abandoned where it stands.
  A shutdown already requested cannot be recalled, so it is seen through and
  the node powered back on after it.

**Give it back when it is Ready**, and it is metalnap's again from the next
tick: put into service if demand wants it, or through the ordinary sleep — with
every rule a sleep keeps — if not. A node given back dark under metalnap's
cordon is checked like any it put to sleep: off is asleep, and powered but not
Ready gets a wake timeout's grace and is then handed to a human. One given back
dark and uncordoned reads as down, loudly, like any node that crashed.

It is not a `MODE` and not a Helm value. It is per node, asked for on the node,
so it needs no redeploy and nothing to remember to set back.

## Operating it from your machine

`metalnap` is also a small CLI for the person at the keyboard. It runs
`kubectl` with your kubeconfig and your RBAC, and holds no credential of its
own:

```bash
pipx install git+https://github.com/mgd43b/metalnap   # or: pip install -e .

metalnap status                         # every managed node, and its state
metalnap maintenance start node1 node2 --reason "firmware"
metalnap maintenance stop node1 node2
metalnap logs -f --node node1            # the controller's log, readable
```

It names the context it is acting on every time, and pins every call to it —
`--context`, or `METALNAP_CONTEXT`, or your current context, named. It finds
the controller by its Helm chart's labels (`--namespace` or `--release` if a
cluster has several, `--selector` if the chart's `nameOverride` changed them)
and reads the node list, cordon annotation and mode from it, so `status` reads
a cordon exactly as the controller does. It refuses a
node the controller does not manage: that annotation would do nothing, and a
typo would look exactly like a request being ignored.

What `status` cannot show is what the controller is *doing* — a wake or a
drain in progress lives in its memory. `logs` is where it says so.

## Scheduled wakeups

A node that sleeps for three weeks comes back three weeks behind: unattended
upgrades, config management, a new CA bundle, firmware. All of it lands at once
on the machine that just woke because something was waiting for it — which is
the worst possible moment for a forty-minute upgrade run.

So bring idle nodes up briefly, on a schedule, when nothing is waiting:

```yaml
maintenance:
  intervalS: 86400      # a node asleep this long gets a visit
  windowS: 300          # it stays up five minutes, measured from Ready
  staggerS: 3600        # spread across an hour so a rack does not wake in unison
```

**Off unless you set `intervalS`.** It powers hardware on when nothing asked
for it, and that should be a decision somebody made.

What a visit actually does, and why:

- **The node comes up cordoned and stays cordoned.** Uncordoning would
  advertise capacity that is about to be taken away again, so every visit would
  end by draining real work under a five-minute deadline — and that drain would
  then be the thing keeping the node up. Host-level updates, config management
  and anything running as a DaemonSet all proceed regardless of a cordon. If
  you need a node for longer, or schedulable, ask for it with [maintenance
  mode](#maintenance-mode) and uncordon it yourself.
- **One node at a time, staggered.** Nodes fall asleep in a herd — a cluster
  goes quiet and they follow each other down — so an unstaggered schedule
  brings the same herd back up in unison, which is a current spike your PSUs
  did not agree to. Each node's offset comes from a hash of its name: random
  across a fleet, identical across a redeploy, so a controller that restarts
  often cannot keep re-bunching the nodes the stagger exists to spread.
- **It yields to everything.** Unmet demand, an operator's cordon or
  maintenance request, any wake, sleep, drain or warmup already in flight —
  all of them win. A visit deferred
  by a tick, or by a thousand, costs nothing when the schedule is measured in
  days.
- **Demand mid-visit takes the node.** It is already booted and one uncordon
  away, which makes it the cheapest capacity available anywhere.
- **A node that goes NotReady inside its window is waited for, not powered
  off.** That state is, far more often than not, a node rebooting into the
  kernel it just installed, and cutting power to it is how a routine update
  becomes an unbootable machine. If it is *still* not back when the bound
  fires, metalnap lets go of the visit and leaves the machine **powered**,
  unmuted and alerted on: it cannot tell "mid-update" from "broken", and
  leaving a node powered costs watts where cutting power to one writing its
  own firmware costs the machine. The ordinary stranded repair finishes the job
  the moment it comes back.
- **`MIN_UPTIME_S` does not apply.** It exists to stop *demand* thrashing a
  node up and down; a visit is not demand, and the whole point is a short stay.

The schedule is measured from Kubernetes' own `Ready` condition transition
time, so it survives a controller restart — a process that forgot a node had
been dark for a fortnight would visit it a fortnight late.

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
cluster and fake BMCs, with phased demand, hung work, operators cordoning
nodes and asking for them for maintenance, nodes whose kernels lock up, and
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
# The password is read from the terminal and piped straight in: it reaches
# neither shell history, nor the process list (--from-literal puts it in
# both), nor a variable that outlives the subshell reading it.
(stty -echo; trap 'stty echo' EXIT; printf 'BMC password: ' >&2
 IFS= read -r pass; echo >&2; printf %s "$pass") |
  kubectl -n metalnap create secret generic metalnap-bmc \
    --from-literal=user=ADMIN --from-file=pass=/dev/stdin

CHART_VERSION=0.4.0 # x-release-please-version

# A values file, not --set: `{node}` is Helm list syntax and the dots in a
# hostname are read as key paths, so --set cannot express bmc.hostFormat.
cat > metalnap.yaml <<'YAML'
nodes: [node1, node2]
bmc:
  hostFormat: "{node}-ipmi.internal.example.org."   # trailing dot required
prometheus:
  url: http://prometheus-k8s.monitoring.svc:9090
YAML

helm install metalnap oci://ghcr.io/mgd43b/charts/metalnap \
  -n metalnap --version "$CHART_VERSION" -f metalnap.yaml
```

> **The trailing dot on `bmc.hostFormat` is required, not stylistic.** The image
> is Alpine, and musl's resolver does not fall back: glibc tries the search list
> and *then* the absolute name, musl tries one and gives up. A dotted BMC
> hostname without the trailing dot does not resolve, so metalnap cannot power
> nodes on or read their power state — while the pod starts cleanly and every
> test passes. `values.schema.json` rejects it at install time so you find out
> then rather than during an incident. Setting `ndots` is not an alternative:
> metalnap also resolves short service names (`prometheus-k8s.monitoring.svc`),
> which need the search list, so no single `ndots` value serves both classes.
> IP literals are exempt — they never touch DNS.

It installs in **`dry_run`** and touches nothing. Watch what it decides:

```bash
kubectl -n metalnap logs -l app.kubernetes.io/name=metalnap -f
# or, with the CLI: metalnap logs -f
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
NODES=node1,node2 BMC_HOST_FMT='{node}-ipmi.internal.example.org' BMC_USER=... BMC_PASS=... PROM_URL=http://prometheus:9090 MODE=dry_run   python3 -m metalnap
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

`MAINTENANCE_INTERVAL_S` enables [scheduled wakeups](#scheduled-wakeups) and is
`0` — off — by default. [Maintenance mode](#maintenance-mode) has no setting:
it is asked for on the node.

The pool is sized on **memory and CPU**, whichever needs more nodes
(set `CPU_SHORTFALL_QUERY=""` to size on memory alone). Runners that run out
of CPU first, sized on memory alone, woke about half the nodes a backlog
needed. By default both are read off the unschedulable pods with the
scheduler's own effective-request formula — so a runner's `dind`, a native
sidecar asking for as much as the runner does, is counted, and an ordinary
init container only as the floor it is. `SHORTFALL_QUERY` (GiB) and
`CPU_SHORTFALL_QUERY` (cores) replace either with PromQL. A custom `DemandSignal` can
do the same by returning `{"memory": …, "cpu": …}` from `shortfall()` with a
`NodeSource` that reports capacity the same way (`kube.allocatable()`); a bare
number on both sides is one resource, as before.

`POWER_CYCLE_COOLDOWN_S` bounds the [power cycle of a wedged
node](#when-a-node-will-not-come-back) to one per node per this long (default
`86400`); `0` disables the cycle and alerts straight away.
`SHUTDOWN_TIMEOUT_S` (default `600`) is how long a soft shutdown may take before
it is reported.

`ALERTMANAGER_SILENCE_LABELS` (default `instance,node`) and
`ALERTMANAGER_SILENCE_MATCHERS` (one amtool-syntax matcher per line) shape the
silences; see [the seams](#the-three-seams).

## Base image

`alpine:3.22`, carrying `ipmitool`, `python3` and `py3-requests` — 58MB and 45
packages, against 159MB and 105 for the Debian equivalent.

The size is incidental; the reason is that **Debian's CVEs are unfixable here**.
Every finding against `debian:13-slim` reports no fix available, including three
criticals in `perl-base`, which is `priority:required` and cannot be removed. A
grade that rebuilding cannot improve makes rebuilding — and Dependabot — purely
decorative. Alpine's findings all carry fixed versions, so a rebuild clears them
and the dependency automation does real work.

The cost is musl, and it is a real one: see the note on `bmc.hostFormat` above.

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
