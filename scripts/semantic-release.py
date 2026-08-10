#!/usr/bin/env python3
"""Cut a release from the conventional commits since the last version tag.

Decides the bump (feat -> minor, fix/perf/refactor -> patch, `!` or a BREAKING
CHANGE trailer -> major), renders notes grouped by type, and creates the tag and
the Release in ONE Releases API call — both forges create the tag from the ref
when `tag_name` does not exist yet, which on GitLab is the only tag-write a
CI_JOB_TOKEN can perform (the Tags API is read-only for job tokens).

Two backends, selected with `--platform`; only the two API calls differ.
  gitlab (default)  POST $CI_API_V4_URL/projects/:id/releases, `JOB-TOKEN:`
  github            POST $GITHUB_API_URL/repos/:owner/:repo/releases,
                    `Authorization: Bearer` + the versioned Accept header
Everything above them — parsing, the bump decision, the notes — is forge-neutral.

No releasable commit -> no release, exit 0. Re-running on an already-released
commit is therefore a no-op — EXCEPT when the last version tag carries no
Release (a run that died between the two halves of that one call, or, on
GitHub, a tag pushed by hand or a Release deleted out from under its tag): that
half-finished state is detected wherever the orphan tag sits, and the missing
Release is backfilled from the tag's own commit range before any new tag is cut.

Stdlib only. The decision path is `plan_release(tags, log_output, ...)`: it takes
raw `git` output and returns the plan, so it is testable without a repo or a
server.

Usage (see ci/release/semantic-release.yml and
ci/release/github-release-workflow.example.yml):
  scripts/semantic-release.py --dry-run
  scripts/semantic-release.py --output release.json
  scripts/semantic-release.py --platform github --output release.json
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

# git log record/field separators — safe against any commit-message content.
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"
LOG_FORMAT = "%H" + FIELD_SEP + "%B" + RECORD_SEP

HEADER_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:[ \t]+(?P<summary>.+?)[ \t]*$"
)
BREAKING_TRAILER_RE = re.compile(r"^BREAKING[ -]CHANGE:[ \t]*(?P<text>.+?)[ \t]*$", re.MULTILINE)
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Types absent from this map appear in the notes but never trigger a release on
# their own (docs, ci, build, test, chore, style, revert).
BUMP_BY_TYPE = {"feat": "minor", "fix": "patch", "perf": "patch", "refactor": "patch"}
LEVEL_RANK = {"patch": 0, "minor": 1, "major": 2}

SECTIONS = (
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactors"),
    ("docs", "Documentation"),
    ("ci", "CI"),
    ("build", "Build"),
    ("test", "Tests"),
    ("style", "Style"),
    ("chore", "Chores"),
    ("revert", "Reverts"),
)


@dataclass
class Commit:
    sha: str
    type: str
    scope: str
    breaking: bool
    summary: str
    breaking_notes: List[str] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:8]


@dataclass
class Plan:
    released: bool
    level: Optional[str]
    previous_tag: Optional[str]
    tag: Optional[str]
    version: Optional[str]
    notes: str
    commits: List[Commit]


def parse_commit(sha: str, message: str) -> Optional[Commit]:
    """Parse one commit into a Commit, or None when it is not conventional."""
    lines = message.strip().splitlines()
    if not lines:
        return None
    header = HEADER_RE.match(lines[0].strip())
    if not header:
        return None
    body = "\n".join(lines[1:])
    notes = [m.group("text") for m in BREAKING_TRAILER_RE.finditer(body)]
    return Commit(
        sha=sha,
        type=header.group("type").lower(),
        scope=(header.group("scope") or "").strip(),
        breaking=bool(header.group("breaking")) or bool(notes),
        summary=header.group("summary"),
        breaking_notes=notes,
    )


def parse_log(log_output: str) -> List[Commit]:
    """Parse `git log --format=<LOG_FORMAT>` output; non-conventional commits drop out."""
    commits = []
    for record in log_output.split(RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, _, message = record.partition(FIELD_SEP)
        commit = parse_commit(sha.strip(), message)
        if commit:
            commits.append(commit)
    return commits


def bump_level(commits: Sequence[Commit]) -> Optional[str]:
    """Highest release level the commits demand, or None when none is releasable."""
    level = None
    for commit in commits:
        candidate = "major" if commit.breaking else BUMP_BY_TYPE.get(commit.type)
        if candidate and (level is None or LEVEL_RANK[candidate] > LEVEL_RANK[level]):
            level = candidate
    return level


def latest_version_tag(tags: Sequence[str], prefix: str = "v") -> Optional[str]:
    """Highest `<prefix>MAJOR.MINOR.PATCH` tag; tags in any other shape are ignored."""
    best = None
    best_key = ()
    for tag in tags:
        if not tag.startswith(prefix):
            continue
        match = SEMVER_RE.match(tag[len(prefix):])
        if not match:
            continue
        key = tuple(int(part) for part in match.groups())
        if best is None or key > best_key:
            best, best_key = tag, key
    return best


def applied_level(level: str, current: Optional[str], major_on_zero: bool = False) -> str:
    """Demote a breaking change to MINOR while the version is 0.x.

    Matches the documented pre-1.0 allowance: leaving initial development stays a
    deliberate call (`major_on_zero`), not something a `feat!:` subject triggers.
    """
    if level != "major" or major_on_zero or current is None:
        return level
    match = SEMVER_RE.match(current)
    return "minor" if match and match.group(1) == "0" else level


def next_version(current: Optional[str], level: str, initial: str = "0.1.0") -> str:
    """Apply `level` to a bare `MAJOR.MINOR.PATCH` string."""
    if current is None:
        return initial
    match = SEMVER_RE.match(current)
    if not match:
        raise ValueError("not a semver version: %s" % current)
    major, minor, patch = (int(part) for part in match.groups())
    if level == "major":
        return "%d.0.0" % (major + 1)
    if level == "minor":
        return "%d.%d.0" % (major, minor + 1)
    return "%d.%d.%d" % (major, minor, patch + 1)


def render_notes(commits: Sequence[Commit], compare_url: Optional[str] = None) -> str:
    """Release notes grouped by commit type, breaking changes first."""
    blocks = []
    breaking = [c for c in commits if c.breaking]
    if breaking:
        lines = ["### Breaking changes", ""]
        for commit in breaking:
            for note in commit.breaking_notes or [commit.summary]:
                lines.append("- %s (%s)" % (_prefixed(commit, note), commit.short_sha))
        blocks.append("\n".join(lines))

    grouped: Dict[str, List[Commit]] = {}
    for commit in commits:
        grouped.setdefault(commit.type, []).append(commit)
    for type_name, heading in SECTIONS:
        group = grouped.pop(type_name, [])
        if not group:
            continue
        lines = ["### %s" % heading, ""]
        lines += ["- %s (%s)" % (_prefixed(c, c.summary), c.short_sha) for c in group]
        blocks.append("\n".join(lines))
    for type_name in sorted(grouped):
        lines = ["### %s" % type_name, ""]
        lines += ["- %s (%s)" % (_prefixed(c, c.summary), c.short_sha) for c in grouped[type_name]]
        blocks.append("\n".join(lines))

    if compare_url:
        blocks.append("[Full changes](%s)" % compare_url)
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _prefixed(commit: Commit, text: str) -> str:
    return "**%s**: %s" % (commit.scope, text) if commit.scope else text


def plan_release(
    tags: Sequence[str],
    log_output: str,
    tag_prefix: str = "v",
    initial_version: str = "0.1.0",
    compare_url_template: Optional[str] = None,
    major_on_zero: bool = False,
) -> Plan:
    """Turn raw `git tag` + `git log` output into the release decision.

    `compare_url_template` is formatted with `previous` and `tag`; it is dropped
    when there is no previous tag to compare against.
    """
    previous = latest_version_tag(tags, tag_prefix)
    commits = parse_log(log_output)
    level = bump_level(commits)
    if level is None:
        return Plan(False, None, previous, None, None, "", commits)
    current = previous[len(tag_prefix):] if previous else None
    level = applied_level(level, current, major_on_zero)
    version = next_version(current, level, initial_version)
    tag = tag_prefix + version
    compare_url = (
        compare_url_template.format(previous=previous, tag=tag)
        if compare_url_template and previous
        else None
    )
    return Plan(True, level, previous, tag, version, render_notes(commits, compare_url), commits)


def plan_existing_tag(
    tags: Sequence[str],
    tag: str,
    log_reader: Callable[[str], str],
    tag_prefix: str = "v",
    compare_url_template: Optional[str] = None,
) -> Plan:
    """Plan that re-creates the Release for a tag that ALREADY exists.

    Crash recovery only. The notes come from the tag's own commit range
    (`<earlier tag>..<tag>`), so the recovered Release reads exactly like the one
    the half-failed run would have published. `log_reader` takes a git range and
    returns raw `git log` output, keeping this testable without a repo.
    """
    earlier = latest_version_tag([t for t in tags if t != tag], tag_prefix)
    commits = parse_log(log_reader("%s..%s" % (earlier, tag) if earlier else tag))
    compare_url = (
        compare_url_template.format(previous=earlier, tag=tag)
        if compare_url_template and earlier
        else None
    )
    version = tag[len(tag_prefix):] if tag.startswith(tag_prefix) else tag
    return Plan(True, bump_level(commits), earlier, tag, version, render_notes(commits, compare_url), commits)


# Every way a forge call can fail. Only HTTPError carries a status and a body;
# a DNS failure, connection reset, timeout or non-JSON response arrives as one
# of the others with neither, so a handler that reaches for `exc.code` raises
# AttributeError from inside itself and loses the record it exists to write.
# URLError and timeouts are already OSError subclasses — named anyway, because
# the point of this tuple is to be read as the list of what can go wrong.
#
# The last two are NOT OSError subclasses and are easy to miss:
#   UnicodeDecodeError   json.loads() on a non-UTF-8 body raises this, NOT
#                        JSONDecodeError — verified, they are disjoint here.
#   http.client.HTTPException  a TRUNCATED response surfaces as IncompleteRead,
#                        whose base is HTTPException (BadStatusLine and friends
#                        share it). Nothing in the OSError family covers it.
# Both are exactly the flaky-network shapes this tuple exists for, so leaving
# them out would drop the failure artifact on the failures hardest to reproduce.
API_ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    OSError,
    json.JSONDecodeError,
    UnicodeDecodeError,
    http.client.HTTPException,
)


def describe_api_error(exc: BaseException) -> str:
    """One line naming a failed forge call, whatever shape the failure took.

    Must never raise. An HTTPError body is stream-backed, so `read()` can fail
    on its own — a reset or a truncated response mid-read — and a throw from
    HERE lands inside the handler that called it, skipping the write_plan below
    and losing the artifact. That is the exact failure this helper exists to
    prevent, so the status is kept and the unreadable body is reported as such.
    """
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode(errors="replace")
        except Exception as body_exc:  # noqa: BLE001 - a diagnostic must not throw
            detail = "<body unreadable: %s: %s>" % (type(body_exc).__name__, body_exc)
        return "HTTP %s: %s" % (exc.code, detail)
    return "%s: %s" % (type(exc).__name__, exc)


PLATFORMS = ("gitlab", "github")

# GitHub wants the media type and the API version on every call; GitLab needs
# neither. Pinning the version keeps a future default flip from changing the
# response shape under a vendored copy nobody is watching.
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# The env each CI injects, in the same four roles. `token` is the env var the
# job is expected to put the token in — GitLab's is set by
# ci/release/semantic-release.yml, GitHub's is the built-in name that
# `env: GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` conventionally fills.
ENV_BY_PLATFORM = {
    "gitlab": {
        "api_url": "CI_API_V4_URL",
        "project": "CI_PROJECT_ID",
        "ref": "CI_COMMIT_SHA",
        "token": "RELEASE_TOKEN",
    },
    "github": {
        "api_url": "GITHUB_API_URL",
        "project": "GITHUB_REPOSITORY",
        "ref": "GITHUB_SHA",
        "token": "GITHUB_TOKEN",
    },
}


def _json_call(
    url: str, headers: Dict[str, str], payload: Optional[dict], opener: Callable
) -> dict:
    """POST (or GET when payload is None) `url` with `headers`; return the JSON body."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for name, value in headers.items():
        request.add_header(name, value)
    if data:
        request.add_header("Content-Type", "application/json")
    with opener(request, timeout=60) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def api_request(
    url: str,
    token: str,
    token_header: str = "JOB-TOKEN",
    payload: Optional[dict] = None,
    opener: Callable = urllib.request.urlopen,
) -> dict:
    """POST (or GET when payload is None) a GitLab API call and return the JSON body."""
    return _json_call(url, {token_header: token}, payload, opener)


def github_api_request(
    url: str,
    token: str,
    token_header: str = "",
    payload: Optional[dict] = None,
    opener: Callable = urllib.request.urlopen,
) -> dict:
    """The same call against GitHub: bearer auth plus the versioned Accept header.

    `token_header` is accepted and ignored — GitHub's auth header is fixed — so
    the two requesters stay interchangeable at the one seam that picks between
    them, and neither the callers nor their test doubles change arity.
    """
    headers = {"Authorization": "Bearer %s" % token}
    headers.update(GITHUB_HEADERS)
    return _json_call(url, headers, payload, opener)


def _requester(platform: str) -> Callable:
    """The api_request variant for `platform`, resolved by name at call time."""
    return github_api_request if platform == "github" else api_request


def _releases_url(api_url: str, project_id: str, platform: str = "gitlab") -> str:
    """The Releases collection URL.

    GitLab addresses a project by numeric id or fully URL-encoded path, so a
    path-style id's separator becomes `%2F`; GitHub's `:owner/:repo` is TWO path
    segments and that slash has to survive.
    """
    base = api_url.rstrip("/")
    if platform == "github":
        return "%s/repos/%s/releases" % (base, urllib.parse.quote(str(project_id), safe="/"))
    return "%s/projects/%s/releases" % (base, urllib.parse.quote(str(project_id), safe=""))


def _release_by_tag_url(
    api_url: str, project_id: str, tag: str, platform: str = "gitlab"
) -> str:
    """URL of the Release attached to `tag` — GitHub nests it under `/tags/`."""
    releases = _releases_url(api_url, project_id, platform)
    quoted = urllib.parse.quote(str(tag), safe="")
    if platform == "github":
        return "%s/tags/%s" % (releases, quoted)
    return "%s/%s" % (releases, quoted)


def get_release(
    api_url: str,
    project_id: str,
    token: str,
    tag: str,
    token_header: str = "JOB-TOKEN",
    request: Optional[Callable] = None,
    platform: str = "gitlab",
) -> Optional[dict]:
    """The Release for `tag`, or None when the tag carries none (HTTP 404).

    Both forges answer 404 for a tag that exists with no Release, so the
    crash-recovery probe reads the same on either. GitHub documents its
    releases/tags endpoint as returning the PUBLISHED release, so a draft someone
    left behind should read as "missing" here and the backfill below should fail
    loudly against that tag rather than quietly publishing a second Release.

    The explicit draft check below does not assume that documented behaviour: if
    the endpoint never returns a draft it is a no-op, and if it ever does (an
    authenticated token with push access is the case usually cited) a draft would
    otherwise be mistaken for a published release and skip recovery entirely.
    """
    url = _release_by_tag_url(api_url, project_id, tag, platform)
    try:
        release = (request or _requester(platform))(url, token, token_header, None)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if platform == "github" and isinstance(release, dict) and release.get("draft"):
        return None
    return release


def create_release(
    api_url: str,
    project_id: str,
    token: str,
    plan: Plan,
    ref: str,
    token_header: str = "JOB-TOKEN",
    request: Optional[Callable] = None,
    platform: str = "gitlab",
) -> dict:
    """Create the tag (from `ref`) and the Release in one call.

    Both forges create `plan.tag` from the ref when it does not exist yet and
    ignore the ref when it does (GitHub documents `target_commitish` as "unused
    if the Git tag already exists"), which is what lets the crash-recovery
    backfill be a plain create against the orphan tag. The tags themselves
    differ: GitLab's is ANNOTATED and carries `tag_message`, while GitHub's
    Releases API only writes a lightweight ref — there the notes live in the
    Release body alone.
    """
    url = _releases_url(api_url, project_id, platform)
    if platform == "github":
        payload = {
            "tag_name": plan.tag,
            "target_commitish": ref,
            "name": plan.tag,
            "body": plan.notes,
        }
    else:
        payload = {
            "tag_name": plan.tag,
            "ref": ref,
            "name": plan.tag,
            "tag_message": "%s\n\n%s" % (plan.tag, plan.notes),
            "description": plan.notes,
        }
    return (request or _requester(platform))(url, token, token_header, payload)


def compare_url_template(platform: str, project_id: str = "") -> Optional[str]:
    """`{previous}`/`{tag}` template for the notes' compare link, read from CI env.

    None when the env does not name the project's web URL (a local run); the
    notes then simply carry no compare link.
    """
    if platform == "github":
        server = os.environ.get("GITHUB_SERVER_URL", "")
        if not (server and project_id):
            return None
        return "%s/%s/compare/{previous}...{tag}" % (server.rstrip("/"), project_id)
    project_url = os.environ.get("CI_PROJECT_URL", "")
    return project_url + "/-/compare/{previous}...{tag}" if project_url else None


def git(args: Sequence[str], repo_dir: str = ".") -> str:
    result = subprocess.run(
        ["git", "-C", repo_dir] + list(args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return result.stdout


def write_plan(
    path: str,
    plan: Plan,
    released: bool,
    dry_run: bool = False,
    error: str = "",
    recovered: str = "",
    recovery_check: str = "",
) -> None:
    """Serialise the outcome. `released` is what actually happened, not the plan.

    The artifact is published `when: always`, so it must never claim a tag that
    the API call did not create; a failed run carries the reason instead.
    `recovered` names an earlier tag whose missing Release this run backfilled;
    `recovery_check` is "failed" when the run could not determine whether there
    was anything to back-fill, so a skipped repair is visible to whoever reads
    the artifact rather than only to whoever scrolls the job log.
    """
    if not path:
        return
    payload = {
        "released": released,
        "dry_run": dry_run,
        "level": plan.level,
        "previous_tag": plan.previous_tag,
        "tag": plan.tag,
        "version": plan.version,
        "notes": plan.notes,
    }
    if error:
        payload["error"] = error
    if recovered:
        payload["recovered"] = recovered
    if recovery_check:
        payload["recovery_check"] = recovery_check
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--tag-prefix", default="v")
    parser.add_argument("--initial-version", default="0.1.0")
    parser.add_argument(
        "--major-on-zero",
        action="store_true",
        help="Let a breaking change cut 1.0.0 from a 0.x version (default: bump MINOR).",
    )
    parser.add_argument(
        "--platform",
        default="gitlab",
        choices=list(PLATFORMS),
        help="Forge whose Releases API is called (default: gitlab).",
    )
    parser.add_argument(
        "--ref",
        default="",
        help="Commit the tag is created from (default: $CI_COMMIT_SHA / $GITHUB_SHA).",
    )
    parser.add_argument(
        "--api-url", default="", help="API base (default: $CI_API_V4_URL / $GITHUB_API_URL)."
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="GitLab project id or path, or GitHub owner/repo "
        "(default: $CI_PROJECT_ID / $GITHUB_REPOSITORY).",
    )
    parser.add_argument(
        "--token-env",
        default="",
        help="Env var holding the token (default: RELEASE_TOKEN / GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--token-header",
        default="JOB-TOKEN",
        choices=["JOB-TOKEN", "PRIVATE-TOKEN"],
        help="GitLab only — GitHub always sends `Authorization: Bearer`.",
    )
    parser.add_argument("--output", default="", help="Write the plan as JSON to this path.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; create nothing.")
    args = parser.parse_args(argv)

    # Each forge names the same four facts differently; a flag always wins.
    env = ENV_BY_PLATFORM[args.platform]
    api_url = args.api_url or os.environ.get(env["api_url"], "")
    project_id = args.project_id or os.environ.get(env["project"], "")
    token_env = args.token_env or env["token"]
    compare_template = compare_url_template(args.platform, project_id)
    # `--merged HEAD` is load-bearing: the newest tag chosen here fixes BOTH the
    # next version and the commit range that becomes the notes. A bare
    # `--list` returns every tag in the repo, so a higher version cut on an
    # unrelated branch (a maintenance line, an abandoned spike, an upstream tag
    # in a fork) silently rebases this release onto a commit that is not an
    # ancestor — wrong version, and a log range spanning history that never
    # shipped here. Restricting to tags reachable from HEAD keeps the choice on
    # this branch's own line. CI sets GIT_DEPTH: 0, so reachability is real
    # rather than an artefact of a shallow clone.
    tags = git(["tag", "--list", "--merged", "HEAD"], args.repo_dir).split()
    previous = latest_version_tag(tags, args.tag_prefix)
    # A shallow clone can lack the previous tag's commit; the job sets GIT_DEPTH: 0.
    log_range = "%s..HEAD" % previous if previous else "HEAD"
    plan = plan_release(
        tags,
        git(["log", "--no-merges", "--format=" + LOG_FORMAT, log_range], args.repo_dir),
        args.tag_prefix,
        args.initial_version,
        compare_template,
        args.major_on_zero,
    )

    token = os.environ.get(token_env, "")
    have_api = bool(token and api_url and project_id)
    # Only the two paths that talk to the API need HEAD.
    ref = (
        (args.ref or os.environ.get(env["ref"], "")
         or git(["rev-parse", "HEAD"], args.repo_dir).strip())
        if have_api and not args.dry_run
        else ""
    )

    # Crash recovery: on GitLab the Releases API creates the TAG before the
    # Release, so a run that died in between leaves the tag with no Release. On
    # GitHub the two are one request, but the same state arrives by ordinary
    # routes — a `vX.Y.Z` pushed by hand, or a Release deleted while its tag
    # stayed. Either way every later run computes an empty range and reports
    # "nothing to release", green forever. The orphan is looked up WHEREVER it
    # sits, not just while it is still on HEAD: gating on that lost the tag
    # permanently the moment one more commit landed on the release branch,
    # which is the common case.
    recovery = None
    recovery_check = ""
    if previous and have_api and not args.dry_run:
        try:
            existing = get_release(
                api_url, project_id, token, previous, args.token_header, platform=args.platform
            )
        except API_ERRORS as exc:
            # The probe is a REPAIR check, not a precondition for the release it
            # precedes: a 429/502/timeout/garbled body here must not cost a
            # healthy release that the POST below would have created.
            # Unknown -> assume healthy.
            existing = {}
            recovery_check = "failed"
            print(
                "WARNING: could not check whether %s has a Release (%s); skipping crash "
                "recovery this run. If %s is missing its Release, re-run this job."
                % (previous, exc, previous),
                file=sys.stderr,
            )
        if existing is None:
            print("Tag %s exists with no Release (a previous run half-failed)." % previous)
            recovery = plan_existing_tag(
                tags,
                previous,
                lambda rng: git(["log", "--no-merges", "--format=" + LOG_FORMAT, rng], args.repo_dir),
                args.tag_prefix,
                compare_template,
            )
            if not plan.released:
                # Nothing new on top: the orphan IS this run's release.
                plan, recovery = recovery, None

    if not plan.released:
        write_plan(args.output, plan, released=False, recovery_check=recovery_check)
        print("No releasable commits since %s — nothing to release." % (plan.previous_tag or "the start of history"))
        return 0

    print("%s -> %s (%s bump)\n" % (plan.previous_tag or "(no tag)", plan.tag, plan.level or "no"))
    print(plan.notes)
    if args.dry_run:
        write_plan(args.output, plan, released=False, dry_run=True)
        print("--dry-run: no release created.")
        return 0

    if not have_api:
        write_plan(args.output, plan, released=False, error="missing token / api url / project id")
        print(
            "ERROR: need $%s, $%s and $%s to create the release."
            % (token_env, env["api_url"], env["project"]),
            file=sys.stderr,
        )
        return 1
    # The backfill carries its OWN handler and its own record. Sharing one with
    # the new tag's POST below misattributes every outcome: a failure reads as
    # the new tag failing (wrong tag in the artifact, wrong tag in the log), and
    # a success followed by a failed cut is lost entirely.
    recovered_tag = ""
    if recovery is not None:
        # New work landed on top of the orphan: backfill its Release from its own
        # commit range first, so those commits appear in exactly one set of
        # notes, then cut the new tag below. The tag already exists, so the API
        # ignores `ref`; pass the tag itself as the truthful value.
        try:
            create_release(
                api_url,
                project_id,
                token,
                recovery,
                recovery.tag,
                args.token_header,
                platform=args.platform,
            )
        except API_ERRORS as exc:
            detail = describe_api_error(exc)
            write_plan(
                args.output,
                plan,
                released=False,
                error="backfill of %s failed: %s" % (recovery.tag, detail),
                recovery_check=recovery_check,
            )
            print(
                "ERROR: backfilling the missing Release for %s failed (%s) — the new "
                "tag %s was NOT cut. Fix or delete %s, then re-run."
                % (recovery.tag, detail, plan.tag, recovery.tag),
                file=sys.stderr,
            )
            return 1
        recovered_tag = recovery.tag
        print("Backfilled the missing Release for %s." % recovery.tag)

    try:
        release = create_release(
            api_url, project_id, token, plan, ref, args.token_header, platform=args.platform
        )
    except API_ERRORS as exc:
        detail = describe_api_error(exc)
        write_plan(
            args.output,
            plan,
            released=False,
            error=detail,
            recovered=recovered_tag,
            recovery_check=recovery_check,
        )
        print("ERROR: release creation failed (%s)" % detail, file=sys.stderr)
        return 1
    write_plan(
        args.output,
        plan,
        released=True,
        recovered=recovered_tag,
        recovery_check=recovery_check,
    )
    # GitLab answers with `_links.self`, GitHub with `html_url`; neither is
    # load-bearing, so fall through to the tag when the body carries no link.
    print(
        "Created release %s"
        % (release.get("_links", {}).get("self") or release.get("html_url") or plan.tag)
    )
    return 0


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    """main() with each failure mode reduced to one actionable line.

    Mirrors version-bump-mr.py: a shallow clone (`fatal: bad revision`), an
    unreachable API and a non-JSON body all surface as a message, not a traceback.
    """
    try:
        return main(argv)
    except subprocess.CalledProcessError as exc:
        print(
            "ERROR: %s failed: %s" % (" ".join(exc.cmd), (exc.stderr or "").strip()),
            file=sys.stderr,
        )
    except urllib.error.HTTPError as exc:
        print(
            "ERROR: GitLab API call failed (HTTP %s): %s"
            % (exc.code, exc.read().decode(errors="replace")),
            file=sys.stderr,
        )
    except (urllib.error.URLError, OSError) as exc:
        # URLError covers DNS/connection failures; socket timeouts arrive as OSError.
        print("ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    except json.JSONDecodeError as exc:
        print("ERROR: GitLab returned a non-JSON body: %s" % exc, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(run_cli())
