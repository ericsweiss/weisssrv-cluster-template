"""The vendored deploy-preflight gate against this repo's pipeline.

The gate's behaviour is tested in weisssrv-lib. This suite holds the site case:
the default `--extends` pair still finds the deploy jobs.
"""
from __future__ import annotations

from conftest import REPO, load_script

preflight = load_script("check-deploy-preflight.py")


def test_the_repo_pipeline_still_resolves_deploy_jobs():
    """The default `--extends` pair must still match this repo's deploy jobs."""
    doc = preflight.load_ci(REPO / ".gitlab-ci.yml")
    found = preflight._candidates(doc, [".deploy-base", ".maintenance-base"])
    assert found, "no job extends .deploy-base or .maintenance-base any more"
