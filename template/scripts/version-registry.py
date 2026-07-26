"""Tracked-version registry — site data for weisssrv-lib's check-versions.py.

`task maintenance:check-versions` reads this file (the checker's default lookup
path) and compares each entry's upstream against the pin in `vars_file`;
`task maintenance:update-version SERVICE=<name>` rewrites the pin and
regenerates the cluster-versions ConfigMap.

What ships here is the PLATFORM set — the pins that exist in every cluster
generated from this template. Add an entry per application you adopt (its
`var_name` is the key in the vars file, so a typo shows up as "pin not found"),
and delete the entry for anything you remove: an entry with no matching pin is
an error, not a warning.

Field reference: weisssrv-lib docs/SCRIPTS.md. Categories are
github | dockerhub | ghcr | lsio | helm | apt_repo | manual, each with its own
fetch fields. `held: True` reports an upstream but never writes it — say why in
`notes`, which is the most valuable column in this file.
"""

# Charts reconciled by Flux: nothing to deploy, the push is the deploy.
_FLUX = "git push (Flux reconciles)"


def _chart(name, key, repo, chart, **extra):
    entry = {
        "name": name,
        "var_name": f"helm_chart_versions.{key}",
        "category": "helm",
        "helm_repo": repo,
        "helm_chart": chart,
        "deploy_command": _FLUX,
    }
    entry.update(extra)
    return entry


CONFIG = {
    # Every path is repo-root relative.
    "vars_file": "ansible/inventories/prod/group_vars/all.yml",
    "cache_dir": ".version-cache",
    "default_deploy_command": "task infra:deploy",
    # Named files holding pins that live outside vars_file — digest-locked CI
    # images, for instance: {"ci": ".gitlab-ci.yml"}.
    "version_file_aliases": {},
    # Pins with no upstream to track (--check-coverage ignores these).
    "untracked_allowlist": [],
    "services": [
        {
            "name": "k3s",
            "var_name": "k3s_version",
            "category": "github",
            "github_repo": "k3s-io/k3s",
            "version_prefix": "v",
            "strip_prefix": False,
            # Upstream also tags rc/alpha builds and the +k3sN suffix moves
            # independently of the Kubernetes version.
            "tag_filter": r"^v\d+\.\d+\.\d+\+k3s\d+$",
            "deploy_command": "task maintenance:update-k3s-nodes",
            "notes": "bump the install-script sha256 in the same change",
        },
        _chart("cert-manager", "cert_manager", "https://charts.jetstack.io", "cert-manager"),
        _chart(
            "external-dns",
            "external_dns",
            "https://kubernetes-sigs.github.io/external-dns",
            "external-dns",
        ),
        _chart(
            "external-secrets",
            "external_secrets",
            "https://charts.external-secrets.io",
            "external-secrets",
        ),
        _chart("MetalLB", "metallb", "https://metallb.github.io/metallb", "metallb"),
        _chart("Traefik", "traefik", "https://traefik.github.io/charts", "traefik"),
        _chart(
            "kube-prometheus-stack",
            "kube_prometheus_stack",
            "https://prometheus-community.github.io/helm-charts",
            "kube-prometheus-stack",
            notes="bump prometheus_operator_crds in lockstep — the CRDs must match the operator",
        ),
        _chart(
            "prometheus-operator-crds",
            "prometheus_operator_crds",
            "https://prometheus-community.github.io/helm-charts",
            "prometheus-operator-crds",
        ),
        _chart("Loki", "loki", "https://grafana.github.io/helm-charts", "loki"),
        _chart("Alloy", "alloy", "https://grafana.github.io/helm-charts", "alloy"),
        _chart("VPA", "vpa", "https://charts.fairwinds.com/stable", "vpa"),
        _chart("kured", "kured", "https://kubereboot.github.io/charts", "kured"),
        _chart("Reloader", "reloader", "https://stakater.github.io/stakater-charts", "reloader"),
    ],
}
