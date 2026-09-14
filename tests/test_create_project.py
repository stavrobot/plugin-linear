#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for create_project.

Runs offline: fetch_metadata, graphql_request and the shared link attachment
are replaced with fakes. The point is the partial-success contract, which a
live check confirms once but should not be the only guard: when a link fails
the project must still be reported as created, with the failed links named.

Run with: uv run tests/test_create_project.py
"""

import importlib.util
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL_DIR = ROOT / "create_project"
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
        "create_project_run", TOOL_DIR / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load_run()


METADATA = {
    "viewer": {"id": "viewer-1", "name": "Me"},
    "team": {"id": "team-1", "key": "ENG", "name": "Engineering"},
    "members": [{"id": "m1", "name": "Ada"}],
    "projects": [],
    "labels": [],
    "states": [],
    "users": [],
    "truncated": {
        "users": False,
        "states": False,
        "labels": False,
        "projects": False,
        "members": False,
    },
}

CREATED_PROJECT = {
    "id": "p1",
    "name": "plugin test - delete me",
    "identifier": None,
    "url": "https://linear.app/x/project/p1",
    "targetDate": "2026-12-31",
    "lead": {"id": "viewer-1", "name": "Me"},
}


def run_main(params, graphql=None, attach=None, api_key="test-key"):
    """Run run.main() with fakes installed, returning (parsed output, calls)."""
    calls = []

    def default_graphql(query, variables=None, *, api_key, deadline):
        calls.append(variables)
        return {"projectCreate": {"success": True, "project": CREATED_PROJECT}}

    original_post = linear.graphql_request
    original_metadata = linear.fetch_metadata
    original_config = linear.load_config
    original_attach = RUN.project_link.attach_project_link
    linear.graphql_request = graphql or default_graphql
    linear.fetch_metadata = lambda api_key, deadline: METADATA
    # No test may read config.json: supply a synthetic key instead.
    linear.load_config = lambda: {"api_key": api_key}
    if attach is not None:
        RUN.project_link.attach_project_link = attach
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(params))
    out = io.StringIO()
    sys.stdout = out
    try:
        RUN.main()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
        linear.graphql_request = original_post
        linear.fetch_metadata = original_metadata
        linear.load_config = original_config
        RUN.project_link.attach_project_link = original_attach
    return json.loads(out.getvalue()), calls


# ---------------------------------------------------------------------------
# 1. The create input carries the one team id, the resolved lead and date.
# ---------------------------------------------------------------------------
output, calls = run_main(
    {
        "name": "plugin test - delete me",
        "description": "temp",
        "lead": "me",
        "target_date": "2026-12-31",
    }
)
sent = calls[0]["input"]
check(
    "1a: project name is sent",
    sent["name"] == "plugin test - delete me",
    detail=repr(sent),
)
check(
    "1b: teamIds carries the single team",
    sent["teamIds"] == ["team-1"],
    detail=repr(sent),
)
check(
    "1c: 'me' resolves through the viewer",
    sent["leadId"] == "viewer-1",
    detail=repr(sent),
)
check(
    "1d: target date is sent as given",
    sent["targetDate"] == "2026-12-31",
    detail=repr(sent),
)
check("1e: description is sent", sent["description"] == "temp", detail=repr(sent))
check(
    "1f: created project is returned with url",
    output["project"]["url"] == CREATED_PROJECT["url"],
    detail=repr(output),
)
check(
    "1g: no links means empty lists",
    output["links"] == [] and output["links_failed"] == [],
)


# ---------------------------------------------------------------------------
# 2. Partial success: a failing link must not sink the created project.
# ---------------------------------------------------------------------------
GOOD = "https://good.example.com/spec"
BAD = "https://bad.example.com/missing"


def fake_attach(project_id, url, label, *, api_key, deadline):
    if url == GOOD:
        return {"id": "l1", "url": url, "label": "good.example.com"}
    raise ToolError("Linear reported an error: bad link")


output, calls = run_main(
    {"name": "plugin test - delete me", "links": f"{GOOD}, {BAD}"},
    attach=fake_attach,
)
check(
    "2a: project is reported as created despite the failed link",
    output["project"]["id"] == "p1",
    detail=repr(output),
)
check(
    "2b: the good link is reported attached",
    [link["url"] for link in output["links"]] == [GOOD],
    detail=repr(output),
)
check(
    "2c: the failed link is named",
    len(output["links_failed"]) == 1 and output["links_failed"][0]["url"] == BAD,
    detail=repr(output),
)
check(
    "2d: a warning tells the caller not to retry",
    "created" in output.get("warning", "")
    and "Do not create" in output.get("warning", ""),
    detail=repr(output.get("warning")),
)

# ---------------------------------------------------------------------------
# 3. Bad input fails before any mutation.
# ---------------------------------------------------------------------------
try:
    run_main({"name": "plugin test - delete me", "target_date": "2026-13-99"})
    check("3a: an invalid calendar date fails", False)
except ToolError as err:
    check(
        "3a: an invalid calendar date fails",
        "calendar date" in str(err),
        detail=str(err),
    )

try:
    run_main({"name": "plugin test - delete me", "target_date": "31/12/2026"})
    check("3b: a non-ISO date fails", False)
except ToolError as err:
    check("3b: a non-ISO date fails", "ISO date" in str(err), detail=str(err))

try:
    run_main({"name": "   "})
    check("3c: an empty name fails", False)
except ToolError as err:
    check("3c: an empty name fails", "name" in str(err), detail=str(err))

# A supplied-but-blank lead is a caller value, not an absent one: it must
# route through the resolver and fail loudly instead of creating a project
# with no lead.
try:
    run_main({"name": "plugin test - delete me", "lead": "   "})
    check("3d: a blank lead prevents creation", False)
except ToolError as err:
    check(
        "3d: a blank lead prevents creation",
        "empty" in str(err),
        detail=str(err),
    )

# Same for a supplied-but-blank target date.
try:
    run_main({"name": "plugin test - delete me", "target_date": "  "})
    check("3e: a blank target_date fails", False)
except ToolError as err:
    check("3e: a blank target_date fails", "ISO date" in str(err), detail=str(err))

# ---------------------------------------------------------------------------
# 4. The partial-success path writes to STDOUT, which can reach the assistant.
#    A link error that echoes the key must be redacted there too.
# ---------------------------------------------------------------------------
LEAK = "sk-synthetic-project-key-do-not-use"


def leaky_attach(project_id, url, label, *, api_key, deadline):
    raise ToolError(f"Linear reported an error: unauthorized {LEAK}")


output, calls = run_main(
    {"name": "plugin test - delete me", "links": "https://bad.example.com/x"},
    attach=leaky_attach,
    api_key=LEAK,
)
serialized = json.dumps(output)
check(
    "4a: a link error on stdout never leaks the key",
    LEAK not in serialized and "[redacted]" in serialized,
    detail=serialized,
)
check(
    "4b: the failed link is still reported",
    output["links_failed"][0]["url"] == "https://bad.example.com/x",
    detail=repr(output),
)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
