"""Site wiring for the cloudflare-ddns CronJob.

The program is vendored from weisssrv-lib, whose suite covers it. What this repo
owns is the env the CronJob hands it and the record list cluster-config holds.
"""

from __future__ import annotations

import os

import pytest
import yaml
from conftest import REPO, load_script

DDNS_DIR = REPO / "kubernetes/infrastructure/configs/cloudflare-ddns"
MODULE_PATH = DDNS_DIR / "cloudflare-ddns.py"
CRONJOB_PATH = DDNS_DIR / "cronjob.yaml"
CLUSTER_CONFIG = REPO / "kubernetes/infrastructure/sources/cluster-config.yaml"

pytestmark = pytest.mark.skipif(
    not MODULE_PATH.is_file(),
    reason="this cluster ships no cloudflare-ddns module (dns_backend)",
)


def _cluster_config() -> dict:
    return yaml.safe_load(CLUSTER_CONFIG.read_text(encoding="utf-8"))["data"]


def _container() -> dict:
    cronjob = yaml.safe_load(CRONJOB_PATH.read_text(encoding="utf-8"))
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


def _substituted(value: str) -> str:
    """The env value Flux produces: the placeholder lands in an unquoted scalar
    once kustomize has built the CronJob, so the value is parsed as YAML."""
    return yaml.safe_load(f"value: {value}")["value"]


@pytest.fixture(scope="module")
def ddns():
    """The program, loaded under the env the CronJob gives it."""
    config = _cluster_config()
    saved = {key: os.environ.get(key) for key in ("DDNS_ZONE", "DDNS_RECORDS")}
    os.environ["DDNS_ZONE"] = config["cluster_external_domain"]
    os.environ["DDNS_RECORDS"] = _substituted(config["cluster_ddns_records"])
    try:
        return load_script(MODULE_PATH)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_generator_mounts_the_vendored_program():
    kustomization = yaml.safe_load((DDNS_DIR / "kustomization.yaml").read_text())
    (generator,) = kustomization["configMapGenerator"]
    assert generator["files"] == [MODULE_PATH.name]
    assert _container()["command"][-1] == f"/opt/ddns/{MODULE_PATH.name}"


def test_the_cronjob_passes_both_values_from_cluster_config():
    """The env wiring is the program's whole configuration."""
    env = {e["name"]: e.get("value") for e in _container()["env"]}
    assert env["DDNS_ZONE"] == "${cluster_external_domain}"
    assert env["DDNS_RECORDS"] == "${cluster_ddns_records}"


def test_the_site_record_list_parses_without_errors(ddns):
    """A rejected entry fails every run, so the shipped list must parse clean."""
    zone = _cluster_config()["cluster_external_domain"]
    assert ddns.zone_is_valid(zone)
    assert ddns.RECORD_ERRORS == ()
    assert ddns.records_are_valid(ddns.RECORDS)
    assert (zone, True) in ddns.RECORDS


def test_the_cronjob_runs_python_unbuffered():
    """Block-buffered stdout prints every ERROR ahead of the record it concerns."""
    container = _container()
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env.get("PYTHONUNBUFFERED") == "1" or "-u" in container["command"]
