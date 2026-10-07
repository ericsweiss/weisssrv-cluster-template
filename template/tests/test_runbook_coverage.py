"""Every alert's runbook target must exist and name the alert it is linked from.

An anchored URL is held to that heading's span. EXEMPT lists the alerts the runbook
does not name yet; it only shrinks, and a stale entry fails.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
from conftest import REPO, alert_rules, loki_alert_rules

DOCS = REPO / "docs"
OBSERVABILITY = REPO / "kubernetes" / "infrastructure" / "observability"
BASE_PLACEHOLDER = "${cluster_runbook_base_url}/"

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# (alert, doc) pairs whose runbook target never names the alert. Write the
# section, then delete the entry.
EXEMPT: set[tuple[str, str]] = set()

needs_corpus = pytest.mark.skipif(
    not OBSERVABILITY.is_dir(), reason="no observability tree in this repository"
)


def slug(heading: str) -> str:
    """GitHub's heading slug: inline markup dropped, lowercased, spaces to
    hyphens, everything else but word characters and hyphens removed."""
    text = _LINK_RE.sub(r"\1", heading)
    text = text.replace("`", "").replace("*", "")
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text.strip())


def runbook_url(rule: dict) -> str:
    return (rule.get("annotations") or {}).get("runbook_url", "")


def alerts() -> tuple[list[tuple[str, str]], list[str]]:
    """(alert name, runbook_url) across the whole shipped rule corpus, plus the
    rule files that would not parse. A dropped file silently shrinks the corpus."""
    found, unreadable = alert_rules()
    assert found, "parsed no alerts out of the rule corpus"
    return [(alert, runbook_url(rule)) for alert, rule in found], unreadable


def sections(text: str) -> dict[str, str]:
    """{anchor: the heading and everything under it}.

    A section runs to the next heading of the same or higher level, so the body
    of `#### Alert` under `## Backup` belongs to both.
    """
    lines = text.splitlines()
    seen: dict[str, int] = {}
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for number, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if not match:
            continue
        base = slug(match.group("text"))
        if not base:
            continue
        count = seen.get(base, 0)
        seen[base] = count + 1
        headings.append(
            (number, len(match.group("hashes")), base if count == 0 else f"{base}-{count}")
        )

    found = {}
    for index, (start, level, anchor) in enumerate(headings):
        end = len(lines)
        for later_start, later_level, _ in headings[index + 1 :]:
            if later_level <= level:
                end = later_start
                break
        found[anchor] = "\n".join(lines[start:end])
    return found


def uncovered(
    corpus: list[tuple[str, str]], docs: Path
) -> tuple[list[tuple[str, str]], list[str]]:
    """(alert, doc target) for every runbook that does not name its alert, plus
    the alerts whose runbook_url is not a `${cluster_runbook_base_url}/` target."""
    cache: dict[Path, dict[str, str]] = {}
    found = []
    off_base: list[str] = []
    for alert, url in corpus:
        if not url.startswith(BASE_PLACEHOLDER):
            if url:
                off_base.append(f"{alert}: {url}")
            continue
        target, _, anchor = url[len(BASE_PLACEHOLDER) :].partition("#")
        doc = docs / target
        if not doc.is_file():
            found.append((alert, target))
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        if not anchor:
            if alert not in text:
                found.append((alert, target))
            continue
        if doc not in cache:
            cache[doc] = sections(text)
        body = cache[doc].get(anchor)
        if body is None or alert not in body:
            found.append((alert, f"{target}#{anchor}"))
    return found, off_base


@pytest.fixture(scope="module")
def report() -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """(uncovered runbooks, off-base runbook_urls, unparseable rule files)."""
    found, unreadable = alerts()
    missing, off_base = uncovered(found, DOCS)
    return missing, off_base, unreadable


def _doc(target: str) -> str:
    return target.split("#", 1)[0]


@needs_corpus
def test_every_runbook_names_the_alert_that_points_at_it(report):
    missing, _, _ = report
    new = sorted(
        (alert, target) for alert, target in missing if (alert, _doc(target)) not in EXEMPT
    )
    assert not new, (
        f"runbook targets that never name their alert: {new} — write the section, "
        "or point runbook_url at the doc that already covers it. An operator "
        "following the link has to find the alert on the page."
    )


@needs_corpus
def test_exemptions_are_still_uncovered(report):
    """An exemption outliving its gap hides the next alert to lose its runbook."""
    missing, _, _ = report
    live = {(alert, _doc(target)) for alert, target in missing}
    stale = sorted(EXEMPT - live)
    assert not stale, f"these runbooks now name their alert: {stale} — drop them from EXEMPT."


@needs_corpus
def test_every_rule_manifest_parsed(report):
    """A file that will not parse drops its alerts from the corpus, which reads
    as a pass."""
    _, _, unreadable = report
    assert not unreadable, (
        "unparseable manifests — any alert they declare went unchecked:\n  "
        + "\n  ".join(unreadable)
    )


@needs_corpus
def test_every_runbook_url_uses_the_substituted_base(report):
    """A hand-written absolute URL satisfies the carries-a-URL gate and is then
    exempt from the names-the-alert gate with nothing recording the decision."""
    _, off_base, _ = report
    assert not off_base, (
        "alerts whose runbook_url is not a ${cluster_runbook_base_url}/ target, so "
        "the runbook that answers them is unchecked — use the placeholder, or add "
        "an EXEMPT entry:\n  " + "\n  ".join(off_base)
    )


@needs_corpus
def test_every_alert_carries_a_runbook_url():
    found, _ = alerts()
    absent = sorted(alert for alert, url in found if not url)
    assert not absent, f"alerts with no runbook_url annotation: {absent}"


def test_the_collector_reads_the_anchored_section(tmp_path):
    """Mutation case: an anchor pointing at a neighbouring section must fail."""
    (tmp_path / "doc.md").write_text(
        "# Runbooks\n\n"
        "## Backup and Recovery\n\n"
        "#### ArchiveBackupFailed / ArchiveBackupStale\n\n"
        "Re-run the archive job.\n\n"
        "## Networking\n\n"
        "Nothing about backups here.\n"
    )
    good = [("ArchiveBackupStale", f"{BASE_PLACEHOLDER}doc.md#backup-and-recovery")]
    wrong = [("ArchiveBackupStale", f"{BASE_PLACEHOLDER}doc.md#networking")]
    absent = [("ArchiveBackupStale", f"{BASE_PLACEHOLDER}doc.md#nowhere")]
    assert uncovered(good, tmp_path) == ([], [])
    assert uncovered(wrong, tmp_path) == ([("ArchiveBackupStale", "doc.md#networking")], [])
    assert uncovered(absent, tmp_path) == ([("ArchiveBackupStale", "doc.md#nowhere")], [])
    off = [("ArchiveBackupStale", "https://runbooks.example/archive")]
    assert uncovered(off, tmp_path) == ([], ["ArchiveBackupStale: https://runbooks.example/archive"])


def test_the_slug_keeps_underscores(tmp_path):
    """GitHub and GitLab treat `_` as a word character, so stripping it here
    would make every underscore-bearing heading read as a dangling anchor."""
    assert slug("Role: `nas_storage`") == "role-nas_storage"
    (tmp_path / "doc.md").write_text(
        "# Runbooks\n\n## Role: `nas_storage`\n\nNasStorageStale: remount the export.\n"
    )
    corpus = [("NasStorageStale", f"{BASE_PLACEHOLDER}doc.md#role-nas_storage")]
    assert uncovered(corpus, tmp_path) == ([], [])


def test_a_named_but_unshipped_loki_rule_file_is_reported(tmp_path):
    """Mutation case: a generator entry whose file is absent must be reported,
    not dropped, and the files beside it must still be read."""
    (tmp_path / "kustomization.yaml").write_text(
        "configMapGenerator:\n"
        "  - name: loki-rules\n"
        "    files:\n"
        "      - shipped.yaml\n"
        "      - key.yaml=also-shipped.yaml\n"
        "      - gone.yaml\n"
    )
    (tmp_path / "shipped.yaml").write_text(
        "groups:\n  - name: g\n    rules:\n      - alert: Shipped\n"
        "        annotations:\n          runbook_url: here\n"
    )
    (tmp_path / "also-shipped.yaml").write_text(
        "groups:\n  - name: g\n    rules:\n      - alert: Keyed\n"
        "        annotations:\n          runbook_url: here\n"
    )
    found, unreadable = loki_alert_rules(tmp_path)
    assert [(alert, runbook_url(rule)) for alert, rule in found] == [
        ("Shipped", "here"),
        ("Keyed", "here"),
    ]
    assert len(unreadable) == 1
    assert "gone.yaml" in unreadable[0]


# A `docs/<name> § <Title>` pointer written inside an alert's own prose. It is
# not a link, so no link checker resolves it, and the woken operator lands on a
# page with no such heading.
_POINTER_RE = re.compile(
    r"docs/(?P<doc>[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.md)?`?\s*§\s*(?P<title>[^.,;)\n]+)"
)


def doc_for(name: str, docs: Path) -> Path | None:
    """`docs/RUNBOOKS.md` and the number-only `docs/06` form both resolve."""
    exact = docs / f"{name}.md"
    if exact.is_file():
        return exact
    matches = sorted(docs.glob(f"{name}*.md"))
    return matches[0] if len(matches) == 1 else None


def pointers(corpus: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(alert, doc name, section title) for every pointer in the prose."""
    found = []
    for alert, prose in corpus:
        for match in _POINTER_RE.finditer(" ".join(prose.split())):
            found.append(
                (alert, match.group("doc"), match.group("title").strip(" `*"))
            )
    return found


def unresolved(corpus: list[tuple[str, str]], docs: Path) -> list[str]:
    """Pointers naming a doc or a heading that does not exist.

    A title is resolved against the slug of every heading, by prefix: prose
    truncates a long heading, and a fabricated section still matches nothing.
    """
    cache: dict[Path, set[str]] = {}
    found = []
    for alert, name, title in pointers(corpus):
        doc = doc_for(name, docs)
        if doc is None:
            found.append(f"{alert}: docs/{name} does not exist")
            continue
        if doc not in cache:
            cache[doc] = set(sections(doc.read_text(encoding="utf-8", errors="replace")))
        wanted = slug(title)
        if wanted and not any(anchor.startswith(wanted) for anchor in cache[doc]):
            found.append(f"{alert}: {doc.name} has no section {title!r}")
    return found


def prose() -> list[tuple[str, str]]:
    """(alert, its summary and description joined) across the rule corpus."""
    found, _ = alert_rules()
    return [
        (
            alert,
            " ".join(
                str((rule.get("annotations") or {}).get(key, ""))
                for key in ("summary", "description")
            ),
        )
        for alert, rule in found
    ]


@needs_corpus
def test_every_section_pointer_in_an_alert_resolves():
    corpus = prose()
    if not pointers(corpus):
        pytest.skip("no alert prose points at a doc section")
    found = unresolved(corpus, DOCS)
    assert not found, (
        "alerts pointing at a heading that does not exist:\n  "
        + "\n  ".join(found)
        + "\n\nWrite the section, or point the alert at the heading that answers it."
    )


def test_a_fabricated_section_pointer_is_reported(tmp_path):
    """Mutation case: the doc exists, the heading does not."""
    (tmp_path / "RUNBOOKS.md").write_text("# Runbooks\n\n## Host swap\n\nRe-key it.\n")
    good = [("SwapNotEncrypted", "Unencrypted swap is live (docs/RUNBOOKS.md § Host swap).")]
    wrong = [("SwapNotEncrypted", "See docs/RUNBOOKS.md § Swap encryption.")]
    absent = [("SwapNotEncrypted", "See docs/GONE.md § Host swap.")]
    assert unresolved(good, tmp_path) == []
    assert unresolved(wrong, tmp_path) == [
        "SwapNotEncrypted: RUNBOOKS.md has no section 'Swap encryption'"
    ]
    assert unresolved(absent, tmp_path) == ["SwapNotEncrypted: docs/GONE does not exist"]
