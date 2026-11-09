"""Exercise the forge seam in partials/links.jinja: the macros must emit GitHub's
`/tree/` and `/blob/` path shape for a github.com lib_url and GitLab's `/-/tree/`
and `/-/blob/` shape for anything else, off a `.git`-stripped web root.
"""

from __future__ import annotations

import jinja2

import render_cluster
from conftest import copier_env

REPO_ROOT = render_cluster.REPO_ROOT
PARTIAL = REPO_ROOT / "partials" / "links.jinja"

GITLAB_URL = "https://git.example.com/org/weisssrv-lib.git"
GITHUB_URL = "https://github.com/org/weisssrv-lib.git"


def _links(lib_url: str):
    """The partial's macros, bound to one lib_url answer."""
    env = copier_env(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(PARTIAL.read_text()).make_module({"lib_url": lib_url})


def test_self_hosted_gitlab_takes_the_dash_prefixed_paths():
    links = _links(GITLAB_URL)
    assert str(links.lib_home()) == "https://git.example.com/org/weisssrv-lib"
    assert str(links.lib_tree()) == "https://git.example.com/org/weisssrv-lib/-/tree/"
    assert str(links.lib_blob()) == "https://git.example.com/org/weisssrv-lib/-/blob/"


def test_github_takes_the_bare_paths():
    """Neither answer fixture points lib_url at GitHub, so this is the only
    coverage of that arm; a wrong separator ships dead doc links."""
    links = _links(GITHUB_URL)
    assert str(links.lib_home()) == "https://github.com/org/weisssrv-lib"
    assert str(links.lib_tree()) == "https://github.com/org/weisssrv-lib/tree/"
    assert str(links.lib_blob()) == "https://github.com/org/weisssrv-lib/blob/"


def test_the_git_suffix_strip_is_unconditional():
    """`lib_url[:-4]` truncates any answer that does not end in `.git`. The
    lib_url row of test_copier_config.py's VALIDATOR_CASES proves copier.yml
    refuses such an answer before it reaches the macros."""
    links = _links("https://git.example.com/org/weisssrv-lib")
    assert str(links.lib_home()) == "https://git.example.com/org/weisssrv"
