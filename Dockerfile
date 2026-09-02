# metalnap needs ipmitool to reach BMCs and python3 to run. Nothing
# off-the-shelf carries both, so this is a thin debian layer rather than a
# python base image.
FROM debian:13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ipmitool \
      python3 \
      python3-requests \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY metalnap /app/metalnap

# Runs unprivileged. It needs no host access: BMCs are reached over ordinary
# pod networking, and the Kubernetes API via the ServiceAccount token.
USER 65534:65534

# Ships as dry_run on purpose. Observe the decisions it WOULD take before
# letting it touch hardware.
ENV MODE=dry_run PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "metalnap"]
