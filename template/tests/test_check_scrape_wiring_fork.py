"""The forked scrape-wiring gate treats a ValidatingAdmissionPolicyBinding as
cluster-scoped, so a corpus carrying one is not refused as ungroupable.

The fork lasts until weisssrv-lib ships the same line (vendored-manifest.yml).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import SCRIPTS, load_script

SCRIPT = SCRIPTS / "check-scrape-wiring.py"
gate = load_script("check-scrape-wiring.py")

CORPUS = textwrap.dedent(
    """\
    apiVersion: v1
    kind: ConfigMap
    metadata: {name: one, namespace: app-a}
    ---
    apiVersion: v1
    kind: ConfigMap
    metadata: {name: two, namespace: app-b}
    ---
    apiVersion: admissionregistration.k8s.io/v1
    kind: ValidatingAdmissionPolicyBinding
    metadata: {name: tenant-traefik-route-refs}
    spec: {policyName: tenant-traefik-route-refs, validationActions: [Deny]}
    """
)


def test_the_binding_kind_is_cluster_scoped():
    assert "ValidatingAdmissionPolicyBinding" in gate.CLUSTER_SCOPED_KINDS


def test_a_corpus_with_a_binding_is_not_an_operator_error(tmp_path):
    corpus = tmp_path / "corpus.yaml"
    corpus.write_text(CORPUS)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "grouped by guess" not in result.stderr
