"""Shared helpers for the suites under tests/.

load_script() resolves a hyphenated, unimportable gate, as the helper shipped
into a render does. copier_env() is the Jinja environment the suites render with.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import jinja2
import jinja2_ansible_filters

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def copier_env(**options) -> jinja2.Environment:
    """Jinja carrying the filter set copier renders this template with.

    The ansible filters plus `regex_search`, which the questions and template
    files both use. `options` pass through, for a loader or stricter undefined.
    """
    env = jinja2.Environment(  # noqa: S701 - rendering this repo's own templates
        extensions=[jinja2_ansible_filters.AnsibleCoreFiltersExtension], **options
    )
    env.filters.setdefault(
        "regex_search", lambda value, pattern: re.search(pattern, str(value))
    )
    return env


def load_script(name: str | Path):
    """Import a hyphenated script under a module name Python accepts.

    A bare basename resolves under scripts/; a path is taken as given, for the
    gates that ship inside the template tree rather than in scripts/.
    """
    path = Path(name)
    path = path if path.is_absolute() else SCRIPTS / path
    spec = importlib.util.spec_from_file_location(
        path.name.replace("-", "_").removesuffix(".py"), path
    )
    assert spec and spec.loader, f"{path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
