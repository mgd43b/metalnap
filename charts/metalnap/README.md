# metalnap

Suspend idle bare-metal Kubernetes nodes to save power — and never kill running
work to do it.

metalnap watches a demand signal, works out how many machines should be awake,
and powers physical nodes off and on over IPMI to match. It drains a node
gracefully before pulling its power, and it will not interrupt work that is
running.

Cluster Autoscaler and Karpenter scale by calling a cloud API — there is
nothing to call when the machine is in your rack. Metal3 and Ironic manage the
whole bare-metal provisioning lifecycle, a far larger commitment than "turn
this box off overnight". This does the one narrow thing neither does.

## Install

```bash
# BMC credentials are created out of band — they do not belong in values.yaml
kubectl create ns metalnap
kubectl -n metalnap create secret generic metalnap-bmc \
  --from-literal=user=ADMIN --from-literal=pass='<bmc-password>'

helm install metalnap oci://ghcr.io/mgd43b/charts/metalnap \
  -n metalnap \
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

## Three values that are load-bearing

**`tolerations` must stay empty.** metalnap must never be scheduled onto a node
it manages, or it will cordon and power off the machine it is running on. The
chart sets none by default, deliberately.

**`mode` must be QUOTED.** `on`, `off`, `yes` and `no` are YAML 1.1 booleans, so
an unquoted `mode: on` in a values file arrives as `true` and the controller
refuses to start. Use `mode: "on"`. The chart fails with an explicit message if
you get this wrong.

**`cordonAnnotation` marks which cordons are metalnap's own.** A cordon without
it belongs to an operator, and metalnap will never override one. Changing this
value on a running install orphans every cordon it currently holds — those
nodes stay asleep with nothing to explain why.

## Values

| key | default | what it does |
|---|---|---|
| `nodes` | `[]` | Nodes metalnap may manage. It touches nothing else. |
| `mode` | `dry_run` | `off` \| `dry_run` \| `on`. Quote it. |
| `bmc.hostFormat` | — | BMC hostname pattern; `{node}` is substituted. |
| `bmc.existingSecret` | `metalnap-bmc` | Secret with `user` / `pass` keys. |
| `prometheus.url` | in-cluster | Where the demand signal is read from. |
| `alertmanager.url` | in-cluster | Silences a node's alerts while it is deliberately down. Strongly recommended. |
| `warmup.image` | `""` | Pulled onto a node after waking, so the first jobs do not each pay for it. |
| `cordonAnnotation` | `metalnap.io/cordoned` | Marks a cordon as metalnap's own. |
| `burstTaintKey` | `ci-burst` | Taint keeping other work off sleepable nodes. |
| `timers.*` | see `values.yaml` | Sustain windows, timeouts, retry bounds. |

## Safety rules it will not break

Each exists because breaking it cost something real.

- **Never interrupt running work.** If a check cannot tell whether a node is
  busy, that reads as busy.
- **An operator's cordon outranks the controller** — including operations
  already in flight.
- **Idle workers do not leave on their own**, so they are released explicitly
  rather than waited on.
- **Never block the reconcile loop.** Wake and sleep are phase machines taking
  one non-blocking step per tick.
- **Wake readily, sleep reluctantly**, and hold evidence of demand across the
  dips a noisy signal produces.

## RBAC

The chart grants `nodes: get/list/patch`, `pods: get/list`, and
`ephemeralrunners: get/list/delete`. What it **withholds** matters more: there
is no `pods/delete` and no `pods/eviction`. Evicting worker pods directly is
how running work gets destroyed; metalnap releases idle units through their own
scheduler's API, which deregisters them before teardown.

With `warmup.image` set it also gets `pods: create`, plus `delete` scoped by
name to exactly its own warmup pods.

[Source and full documentation](https://github.com/mgd43b/metalnap)
