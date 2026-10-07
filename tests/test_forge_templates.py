"""Assert the change-request skeletons under template/.gitlab/ ship with every
generated cluster: the three paths exist, carry their prompt headings, and are
answer-free so no site value leaks into a forge-rendered form."""

from __future__ import annotations

import pytest

import render_cluster

TEMPLATE_ROOT = render_cluster.REPO_ROOT / "template"

# Headings each skeleton must prompt for. GitLab renders these files verbatim
# into the description box, so a missing section is a prompt nobody answers.
SKELETONS = {
    ".gitlab/merge_request_templates/Default.md": (
        "## Summary",
        "## Changes",
        "## Testing done",
        "## Deploy notes",
    ),
    ".gitlab/issue_templates/Bug.md": (
        "## What happened",
        "## What you expected",
        "## Steps to reproduce",
    ),
    ".gitlab/issue_templates/Feature.md": (
        "## Problem / motivation",
        "## Proposed change",
        "## Platform impact",
    ),
}


@pytest.mark.parametrize("relpath", sorted(SKELETONS))
def test_every_skeleton_ships_with_its_prompts(relpath):
    path = TEMPLATE_ROOT / relpath
    assert path.is_file(), f"{relpath} is missing from template/"
    text = path.read_text(encoding="utf-8")
    missing = [heading for heading in SKELETONS[relpath] if heading not in text]
    assert not missing, f"{relpath} lost {missing}"


@pytest.mark.parametrize("relpath", sorted(SKELETONS))
def test_every_skeleton_renders_unconditionally_and_answer_free(relpath):
    """`.gitlab/` has no `git_backend` arm because `gitlab_selfhosted` is the
    only implemented forge, so these render for every answer. Jinja in the path
    or the body would make that silently untrue."""
    rel = TEMPLATE_ROOT / relpath
    for segment in rel.relative_to(TEMPLATE_ROOT).parts:
        assert "{{" not in segment and "{%" not in segment, f"{relpath} path is conditional"
    assert rel.suffix == ".md", f"{relpath} must be copied verbatim, not rendered"
    text = rel.read_text(encoding="utf-8")
    assert "{{" not in text and "{%" not in text, f"{relpath} carries Jinja"
