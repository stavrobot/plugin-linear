#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for list_issues' assignee filter.

Runs offline: load_config, fetch_metadata and graphql_request are replaced with
fakes, so no key is read and no request is made. Covers the three assignee
modes: the viewer default, an explicit 'me', and 'anyone', which must drop the
assignee filter while leaving the rest of the base view intact.

Run with: uv run tests/test_list_issues.py
"""

import importlib.util
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL_DIR = ROOT / "list_issues"
sys.path.insert(0, str(ROOT))

import linear  # noqa: E402
from linear import ToolError  # noqa: E402

FAILURES = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name} {detail}")
        FAILURES.append(name)


def load_run():
    spec = importlib.util.spec_from_file_location(
        "list_issues_run", TOOL_DIR / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load_run()

METADATA = {
    "viewer": {"id": "viewer-1", "name": "Me"},
    "team": {"id": "team-1", "key": "ENG", "name": "Engineering"},
    "states": [{"id": "s1", "name": "Backlog"}],
    "labels": [{"id": "l1", "name": "Bug"}],
    "projects": [{"id": "p1", "name": "Roadmap"}],
    "members": [{"id": "m1", "name": "Ada"}, {"id": "m2", "name": "Bob"}],
    "users": [],
    "truncated": {
        "users": False,
        "states": False,
        "labels": False,
        "projects": False,
        "members": False,
    },
}


def run_main(params):
    """Run run.main() with fakes installed, returning the sent filter."""
    calls = []

    def fake_graphql(query, variables=None, *, api_key, deadline):
        calls.append(variables)
        return {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}

    original_graphql = linear.graphql_request
    original_metadata = linear.fetch_metadata
    original_config = linear.load_config
    linear.graphql_request = fake_graphql
    linear.fetch_metadata = lambda api_key, deadline: METADATA
    linear.load_config = lambda: {"api_key": "test-key"}
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(params))
    out = io.StringIO()
    sys.stdout = out
    try:
        RUN.main()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
        linear.graphql_request = original_graphql
        linear.fetch_metadata = original_metadata
        linear.load_config = original_config
    return calls[0]["filter"]


# ---------------------------------------------------------------------------
# 1. Assignee modes. The viewer is the default so 'my open issues' really
#    means mine; 'anyone' drops the filter; an explicit name resolves.
# ---------------------------------------------------------------------------
default_filter = run_main({})
check(
    "1a: no assignee defaults to the viewer",
    default_filter.get("assignee") == {"id": {"eq": "viewer-1"}},
    detail=repr(default_filter),
)

me_filter = run_main({"assignee": "me"})
check(
    "1b: assignee 'me' resolves to the viewer",
    me_filter.get("assignee") == {"id": {"eq": "viewer-1"}},
    detail=repr(me_filter),
)

anyone_filter = run_main({"assignee": "anyone"})
check(
    "1c: assignee 'anyone' drops the assignee filter",
    "assignee" not in anyone_filter,
    detail=repr(anyone_filter),
)
check(
    "1d: 'anyone' still keeps the rest of the base view",
    anyone_filter.get("state") == {"type": {"nin": ["completed", "canceled"]}},
    detail=repr(anyone_filter),
)

named_filter = run_main({"assignee": "Ada"})
check(
    "1e: an explicit assignee resolves to the member",
    named_filter.get("assignee") == {"id": {"eq": "m1"}},
    detail=repr(named_filter),
)

# The special value is case-insensitive, like the other name resolution.
check(
    "1f: 'ANYONE' is matched case-insensitively",
    "assignee" not in run_main({"assignee": "ANYONE"}),
)


# ---------------------------------------------------------------------------
# 2. A supplied-but-blank filter is NOT the same as an absent one. Absent
#    takes the default; blank is a caller value and must fail loudly rather
#    than silently running a widened or unfiltered search.
# ---------------------------------------------------------------------------
def run_main_err(params):
    """Run run.main() expecting a ToolError; return the error, or None."""
    try:
        run_main(params)
    except ToolError as err:
        return err
    return None


for param in ("project", "state", "label", "assignee"):
    err = run_main_err({param: "   "})
    check(
        f"2: a blank {param} fails loudly",
        isinstance(err, ToolError) and "empty" in str(err),
        detail=repr(err),
    )

# A blank assignee must not fall back to the viewer default.
err = run_main_err({"assignee": "   "})
check(
    "2a: a blank assignee does not default to the viewer",
    err is not None and "empty" in str(err),
    detail=repr(err),
)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
