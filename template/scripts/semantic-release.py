#!/usr/bin/env python3
"""Cut a release from the conventional commits since the last version tag.

Creates the tag and the Release in one API call; --platform picks gitlab or
github. Stdlib only. Contract: docs/SCRIPTS.md - semantic-release.py.
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

    Crash recovery only: the notes come from `<earlier tag>..<tag>`, so the
    recovered Release reads like the one the half-failed run would have cut.
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


# Every way a forge call can fail; only HTTPError carries a status and a body.
# json.loads raises UnicodeDecodeError on a non-UTF-8 body; a truncated read
# raises http.client.HTTPException, which is not an OSError.
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

    Must never raise: a throw here lands in the caller's handler and loses the
    plan artifact, so an unreadable HTTPError body is reported, not propagated.
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

# The env each CI injects, in the same four roles. `token` names the variable
# the job puts the token in: ci/release/semantic-release.yml sets GitLab's,
# GITHUB_TOKEN is GitHub's conventional name.
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

    `token_header` is accepted and ignored, so the two requesters stay
    interchangeable at the seam that picks between them.
    """
    headers = {"Authorization": "Bearer %s" % token}
    headers.update(GITHUB_HEADERS)
    return _json_call(url, headers, payload, opener)


def _requester(platform: str) -> Callable:
    """The api_request variant for `platform`, resolved by name at call time."""
    return github_api_request if platform == "github" else api_request


def _releases_url(api_url: str, project_id: str, platform: str = "gitlab") -> str:
    """The Releases collection URL.

    A GitLab path-style id is URL-encoded to `%2F`; GitHub's `:owner/:repo` is
    two path segments, so that slash survives.
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
    """The Release for `tag`, or None when the tag carries none (404 on both forges).

    A GitHub draft reads as missing here, so crash-recovery is never skipped by one.
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

    Both forges ignore `ref` when the tag exists, so the crash-recovery backfill
    is a plain create. GitLab's tag is annotated; GitHub's is a lightweight ref.
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
    """Serialise the outcome. `released` is what happened, not the plan.

    Published `when: always`, so a failed run carries the reason instead of a
    tag. `recovery_check` is "failed" when a skipped repair could not be judged.
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


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser."""
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
    return parser


def _fail(output: str, plan: Plan, message: str, **plan_fields) -> int:
    """Record a failed run in the plan artifact, print one line, return 1."""
    write_plan(output, plan, released=False, **plan_fields)
    print("ERROR: %s" % message, file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Each forge names the same four facts differently; a flag always wins.
    env = ENV_BY_PLATFORM[args.platform]
    api_url = args.api_url or os.environ.get(env["api_url"], "")
    project_id = args.project_id or os.environ.get(env["project"], "")
    token_env = args.token_env or env["token"]
    compare_template = compare_url_template(args.platform, project_id)
    # `--merged HEAD` is load-bearing: the newest reachable tag fixes both the
    # next version and the notes range, so a higher tag on an unrelated branch
    # is ignored. CI sets GIT_DEPTH: 0 so reachability is real.
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

    # Crash recovery: a tag that exists with no Release is backfilled from its
    # own commit range. See docs/VERSIONING.md.
    recovery = None
    recovery_check = ""
    if previous and have_api and not args.dry_run:
        try:
            existing = get_release(
                api_url, project_id, token, previous, args.token_header, platform=args.platform
            )
        except API_ERRORS as exc:
            # The probe is a repair check, not a precondition: a transient
            # failure here must not cost the release the POST below creates.
            # Unknown means assume healthy.
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
        return _fail(
            args.output,
            plan,
            "need $%s, $%s and $%s to create the release."
            % (token_env, env["api_url"], env["project"]),
            error="missing token / api url / project id",
        )
    # Separate handler and record so a backfill failure is reported against the
    # backfill tag, not the new one.
    recovered_tag = ""
    if recovery is not None:
        # Backfill the orphan's Release from its own commit range first, so its
        # commits appear in exactly one set of notes. The tag exists, so the API
        # ignores `ref`; the tag itself is passed as the truthful value.
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
            return _fail(
                args.output,
                plan,
                "backfilling the missing Release for %s failed (%s) — the new tag %s "
                "was NOT cut. Fix or delete %s, then re-run."
                % (recovery.tag, detail, plan.tag, recovery.tag),
                error="backfill of %s failed: %s" % (recovery.tag, detail),
                recovery_check=recovery_check,
            )
        recovered_tag = recovery.tag
        print("Backfilled the missing Release for %s." % recovery.tag)

    try:
        release = create_release(
            api_url, project_id, token, plan, ref, args.token_header, platform=args.platform
        )
    except API_ERRORS as exc:
        detail = describe_api_error(exc)
        return _fail(
            args.output,
            plan,
            "release creation failed (%s)" % detail,
            error=detail,
            recovered=recovered_tag,
            recovery_check=recovery_check,
        )
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

    CalledProcessError first: it is not an OSError, so the order is safe.
    """
    try:
        return main(argv)
    except subprocess.CalledProcessError as exc:
        print(
            "ERROR: %s failed: %s" % (" ".join(exc.cmd), (exc.stderr or "").strip()),
            file=sys.stderr,
        )
    except API_ERRORS as exc:
        print("ERROR: Releases API call failed: %s" % describe_api_error(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(run_cli())
