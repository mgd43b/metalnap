#!/usr/bin/env python3
"""
Validate Chart.yaml's artifacthub.io/* annotations against the documented spec.

`helm lint` does not check these -- it has no idea what Artifact Hub expects --
so an invalid annotation packages cleanly, publishes cleanly, and then fails
silently at INDEXING time. The only signal is an email hours later. v0.1.0
shipped with `signKey: {fingerprint: none}` and no url, which fails with
"sign key url not provided" and takes the whole package with it.

Run: python3 charts/validate.py charts/metalnap/Chart.yaml
"""
import sys

import yaml

# https://artifacthub.io/docs/topics/annotations/helm/
CATEGORIES = {
    "ai-machine-learning", "database", "integration-delivery",
    "monitoring-logging", "networking", "security", "storage",
    "streaming-messaging", "skip-prediction",
}


def validate(path):
    chart = yaml.safe_load(open(path))
    ann = chart.get("annotations") or {}
    errors = []

    for required in ("name", "version", "appVersion", "description"):
        if not chart.get(required):
            errors.append("Chart.yaml is missing %s" % required)

    cat = ann.get("artifacthub.io/category")
    if cat is not None and cat not in CATEGORIES:
        errors.append("category %r is not one of: %s"
                      % (cat, ", ".join(sorted(CATEGORIES))))

    # `url` is mandatory ONCE the entry exists. Omitting signKey entirely is
    # fine; a partial one is not.
    if "artifacthub.io/signKey" in ann:
        try:
            key = yaml.safe_load(ann["artifacthub.io/signKey"]) or {}
        except yaml.YAMLError as e:
            key, _ = {}, errors.append("signKey is not valid YAML: %s" % e)
        if not key.get("url"):
            errors.append("signKey is present but has no url -- indexing will "
                          "fail with 'sign key url not provided'. Remove the "
                          "annotation unless the chart is actually signed.")
        if not key.get("fingerprint"):
            errors.append("signKey is present but has no fingerprint")

    for field in ("artifacthub.io/links", "artifacthub.io/maintainers"):
        if field in ann:
            try:
                items = yaml.safe_load(ann[field])
            except yaml.YAMLError as e:
                errors.append("%s is not valid YAML: %s" % (field, e))
                continue
            if not isinstance(items, list):
                errors.append("%s must be a YAML list" % field)
                continue
            for i in items:
                if not isinstance(i, dict) or "name" not in i or "url" not in i:
                    errors.append("%s entries need both name and url: %r"
                                  % (field, i))

    lic = ann.get("artifacthub.io/license")
    if lic and (" " in lic or lic != lic.strip()):
        errors.append("license %r should be a bare SPDX identifier" % lic)

    return errors


def main():
    paths = sys.argv[1:] or ["charts/metalnap/Chart.yaml"]
    failed = False
    for p in paths:
        errs = validate(p)
        if errs:
            failed = True
            print("%s:" % p)
            for e in errs:
                print("  - %s" % e)
        else:
            print("%s: annotations valid" % p)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
