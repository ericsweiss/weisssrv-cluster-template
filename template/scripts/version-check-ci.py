#!/usr/bin/env python3
"""CI wrapper for check-versions.py — runs the check, posts an MR comment when
updates are available, and writes the JSON report artifact.

  version-check-ci.py                    # writes ./version-report.json
  version-check-ci.py --output PATH      # writes the report elsewhere

Environment:
  CHECK_VERSIONS_CMD      command to run (default ./scripts/check-versions.py)
  CHECK_VERSIONS_LOCAL    command named in the MR comment footer
                          (default: the same command)
  VERSION_CHECK_TIMEOUT   seconds before the subprocess is killed (default 600)
  GITLAB_API_TOKEN        PRIVATE-TOKEN used to post the MR note
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from urllib.request import Request, urlopen

CHECK_CMD = os.environ.get("CHECK_VERSIONS_CMD", "./scripts/check-versions.py")
LOCAL_CMD = os.environ.get("CHECK_VERSIONS_LOCAL", CHECK_CMD)
# CWD-relative default: the `artifacts:` path CI collects. Keep it stable —
# a consumer's .gitlab-ci.yml names it.
DEFAULT_OUTPUT = "version-report.json"


def post_mr_comment(body: str) -> None:
    """Post a comment to the current MR via GitLab API."""
    api_url = os.environ.get("CI_API_V4_URL", "")
    project_id = os.environ.get("CI_PROJECT_ID", "")
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID", "")
    token = os.environ.get("GITLAB_API_TOKEN", "")

    if not all([api_url, project_id, mr_iid, token]):
        if mr_iid:
            # In an MR pipeline but a credential/URL is missing — surface it
            # so a revoked/absent GITLAB_API_TOKEN doesn't silently swallow
            # the version comment.
            print(
                "Warning: in an MR pipeline but GitLab API URL/project/token is "
                "incomplete; skipping MR comment (check GITLAB_API_TOKEN).",
                file=sys.stderr,
            )
        return

    url = f"{api_url}/projects/{project_id}/merge_requests/{mr_iid}/notes"
    data = json.dumps({"body": body}).encode()
    req = Request(url, data=data, headers={
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=30) as resp:
            resp.read()
        print("MR comment posted")
    except Exception as e:
        print(f"Warning: could not post MR comment: {e}")


def _services(data: dict) -> list:
    """Return only well-formed (dict) service entries from a parsed payload.

    `data` is validated to be a dict, but its `services` value comes from an
    external subprocess and isn't otherwise checked: a forged/skewed producer
    could emit `null` (TypeError on iteration) or non-dict entries (AttributeError
    on `svc.get`). Neither is caught by main()'s parse `except`, so an unguarded
    loop would crash the wrapper and bypass the stub-artifact contract.
    """
    services = data.get("services")
    if not isinstance(services, list):
        return []
    return [svc for svc in services if isinstance(svc, dict)]


def _write_report(path: str, text: str) -> None:
    """Write the report artifact, creating a --output parent dir if needed.

    The default lands in the CWD, but a consumer redirecting into an artifacts
    subdir (`--output reports/version-report.json`) would otherwise get a
    FileNotFoundError on the very first run.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        metavar="PATH",
        help=f"where to write the JSON report artifact (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    # Run version check once with --json. check-versions checks ~50 services
    # sequentially, each with its own request timeout + bounded retries, so a few
    # slow/unreachable endpoints under a partial outage can take minutes. Give
    # generous headroom (env-tunable) so that doesn't SIGKILL the run and report
    # a false "timed out" when most services actually succeeded.
    _timeout_raw = os.environ.get("VERSION_CHECK_TIMEOUT", "600")
    try:
        timeout = int(_timeout_raw)
        if timeout <= 0:
            raise ValueError("must be positive")
    except ValueError:
        print(f"Warning: invalid VERSION_CHECK_TIMEOUT={_timeout_raw!r}; using 600s")
        timeout = 600
    try:
        result = subprocess.run(
            [*shlex.split(CHECK_CMD), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print("Error: version check timed out")
        sys.exit(2)
    except OSError as e:
        print(f"Error: failed to execute version check: {e}")
        sys.exit(2)

    rc = result.returncode

    # Parse JSON for human-readable output
    updates = 0
    errors = 0
    data = {}
    try:
        data = json.loads(result.stdout)
        # A valid-but-non-dict payload (list/number/string/bool/null) would
        # otherwise hit `data.get(...)` below with an uncaught AttributeError,
        # bypassing the stub-artifact contract. Reject it as a parse failure so
        # the ValueError branch writes the self-describing stub and exits 2.
        if not isinstance(data, dict):
            raise ValueError(
                f"version-check output is not a JSON object (got {type(data).__name__})"
            )
        # Persist the validated JSON as the artifact.
        _write_report(args.output, result.stdout)
        summary = data.get("summary")
        if not isinstance(summary, dict):
            # A null/non-dict summary from a skewed producer would otherwise
            # raise AttributeError on the .get() calls below (uncaught by the
            # parse except). Treat it as empty; the .get(..., 0) defaults apply.
            summary = {}
        total = summary.get("total", 0)
        up_to_date = summary.get("up_to_date", 0)
        updates = summary.get("updates_available", 0)
        held = summary.get("updates_held", 0)
        errors = summary.get("errors", 0)

        print(f"Version check: {total} services, {up_to_date} up to date, {updates} updates, {held} held, {errors} errors")

        # Use .get() for field access below: a malformed service entry must not
        # raise KeyError here, since the surrounding except would then overwrite
        # the already-written valid artifact with an error stub.
        if updates > 0:
            print("\nUpdates available:")
            for svc in _services(data):
                if svc.get("update_available") and not svc.get("held"):
                    print(f"  {svc.get('name', '?')}: {svc.get('current_version', '?')} -> {svc.get('latest_version', '?')}")

        if errors > 0:
            print("\nErrors:")
            for svc in _services(data):
                if svc.get("error"):
                    print(f"  {svc.get('name', '?')}: {svc.get('error')}")

        # Reconcile rc with parsed summary in case they diverge
        if errors > 0:
            rc = 2
        elif updates > 0:
            rc = 1
        else:
            rc = 0
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        print("Warning: could not parse version check output")
        print(result.stdout)
        rc = 2
        # A wrong-shape (non-dict) payload would have left `data` holding the
        # parsed list/scalar; reset it so the MR-comment block treats this as a
        # parse failure (the `rc == 2 and not data` branch) rather than trying
        # to itemize services from a non-dict.
        data = {}
        # Write a self-describing stub so the artifact isn't a 0-byte or
        # raw-text file that reads like a successful empty report.
        _write_report(args.output, json.dumps(
            {
                "error": f"version-check output not parseable: {type(e).__name__}: {e}",
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            indent=2,
        ))

    # Post MR comment when there are actionable updates and/or errors.
    # Report BOTH together — a transient single-service error must not
    # suppress the actionable update table (or vice versa).
    if os.environ.get("CI_MERGE_REQUEST_IID"):
        sections = []
        # Build the row lists first, then gate each section header on the list
        # being non-empty (NOT on the summary counters) so a future
        # producer/consumer skew can't emit a header with zero rows.
        update_lines = []
        for svc in _services(data):
            # Held updates are documented non-actionable holds; they'd
            # otherwise re-post the same comment on every pipeline.
            if svc.get("update_available") and not svc.get("held"):
                # Registry notes carry intent (e.g. "intentionally held
                # back: open upstream regression").
                notes = svc.get("notes", "")
                update_lines.append(
                    f"| {svc.get('name', '?')} | {svc.get('current_version', '?')} | "
                    f"{svc.get('latest_version', '?')} | {notes} |"
                )
        if update_lines:
            sections.append(
                "### Updates available\n\n"
                "| Service | Current | Latest | Notes |\n"
                "|---------|---------|--------|-------|\n"
                + "\n".join(update_lines)
            )
        err_lines = [
            f"- {svc.get('name', '?')}: {svc.get('error', 'unknown error')}"
            for svc in _services(data)
            if svc.get("error")
        ]
        if err_lines:
            sections.append("### Errors\n\n" + "\n".join(err_lines))
        elif rc == 2 and not data:
            # Parse failure — no structured services to itemize.
            error_output = (result.stderr or result.stdout or "No error output").strip()
            sections.append("### Version check failed\n\n```\n" + error_output + "\n```")

        if sections:
            body = (
                "## Version Check\n\n"
                + "\n\n".join(sections)
                + f"\n\nRun `{LOCAL_CMD}` locally for details."
            )
            post_mr_comment(body)

    sys.exit(rc)


if __name__ == "__main__":
    main()
