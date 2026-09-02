# metalnap needs ipmitool to reach BMCs and python3 to run. Nothing
# off-the-shelf carries both, so this is a thin distro layer rather than a
# python base image.
#
# Alpine over Debian deliberately: 58MB vs 159MB, 45 packages vs 105, and --
# the reason that actually matters -- Debian's CVEs are unfixable. Every
# finding against debian:13-slim reports "no fix available", including three
# criticals in perl-base, which is priority:required and so cannot be removed.
# A scanner grade you cannot improve by rebuilding makes rebuilding pointless.
# Alpine's findings all carry fixed versions, so a rebuild actually clears them.
#
# THE TRADE, and read this before changing bmc.hostFormat: Alpine is musl, and
# musl's resolver does not fall back. glibc tries the search list and THEN the
# absolute name; musl tries one class and gives up. metalnap resolves both
# classes -- BMCs by FQDN, Prometheus/Alertmanager by short service name -- so
# no single `ndots` value can serve both. Measured in-cluster at ndots:5:
#
#   k8s14-ipmi.internal.mattd.org     FAILS (rc=2)
#   k8s14-ipmi.internal.mattd.org.    resolves
#   prometheus-k8s.monitoring.svc     resolves (via search list)
#
# Hence bmc.hostFormat must be ABSOLUTE (trailing dot). A trailing dot is
# absolute under any ndots, so it is the only form that is correct in both
# libcs. values.schema.json enforces it; do not relax that guard.
FROM alpine:3.22

RUN apk add --no-cache \
      ipmitool \
      python3 \
      py3-requests \
      ca-certificates

WORKDIR /app
COPY metalnap /app/metalnap

# Runs unprivileged. It needs no host access: BMCs are reached over ordinary
# pod networking, and the Kubernetes API via the ServiceAccount token.
USER 65534:65534

# Ships as dry_run on purpose. Observe the decisions it WOULD take before
# letting it touch hardware.
ENV MODE=dry_run PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "metalnap"]
