# metalnap

Power your bare-metal Kubernetes nodes down when the queue is empty — and never
kill running work to do it.

Cluster Autoscaler and Karpenter assume a cloud API. Metal3 and Ironic assume
you want the whole provisioning lifecycle. metalnap does one narrow thing:
watches a demand signal, and turns physical machines off and on to match, with
a safety posture built for work that takes minutes to hours and must not be
interrupted.

It was extracted from a controller that has been sleeping and waking a
two-node Supermicro Twin serving CI since 2026.

> **Status: v0.1, early.** The core is exercised hard (see Testing) but the
> API is not yet stable and it has run in exactly one environment.

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
| `DemandSignal` | how much capacity is wanted? | `PrometheusSignal` (any PromQL) |
| `DrainPolicy` | what is *busy* here, and how do I release an idle unit? | `ArcDrain` (GitHub ARC runners) |
| `PowerBackend` | how do I turn this box on and off? | `IpmiPower` (ipmitool) |

Plus `NodeSource` for reading node state and applying cordons — `KubeNodeSource`
covers Kubernetes.

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
python3 -B tests/test_controller.py          # deterministic, precise scenarios
python3 -B tests/sim.py --seeds 60 --ticks 500   # ~30k ticks, ~1s
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

```bash
git clone https://github.com/mgd43b/metalnap && cd metalnap
python3 -B tests/sim.py --seeds 20 --ticks 400
```

No dependencies for the harness. `requests` for the Kubernetes and Prometheus
adapters; `ipmitool` on PATH for the IPMI backend.

See `examples/arc-ipmi/` for the wiring the reference deployment uses.

## Configuration

Everything is an environment variable, because everything here is an
operational knob you may need to turn during an incident (`metalnap/config.py`).

`MODE` is `off` | `dry_run` | `on`. **`dry_run` observes and logs every decision
it would take without touching anything** — run it there first, for as long as
it takes to trust the numbers.

## Licence

MIT.
