#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for delete_issue.

Runs offline: graphql_request and load_config are replaced with fakes. The
central assertion is the safety one: the mutation must never carry
permanentlyDelete, so a delete_issue call can only ever move the issue to the
trash, where Linear restores it for about 30 days.

Run with: uv run tests/test_delete_issue.py
"""

import importlib.util
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL_DIR = ROOT / "delete_issue"
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
        "delete_issue_run", TOOL_DIR / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load_run()

DELETED = {"identifier": "ENG-123", "title": "Fix it"}


def run_main(params, graphql=None):
    """Run run.main() with fakes installed, returning (output, calls)."""
    calls = []

    def fake_graphql(query, variables=None, *, api_key, deadline):
        calls.append({"query": query, "variables": variables})
        return {"issueDelete": {"success": True, "entity": DELETED}}

    original_graphql = linear.graphql_request
    original_config = linear.load_config
    linear.graphql_request = graphql or fake_graphql
    # No test may read config.json: supply a synthetic key instead.
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
        linear.load_config = original_config
    return json.loads(out.getvalue()), calls


# ---------------------------------------------------------------------------
# 1. The happy path. The mutation is issueDelete with the issue id only, and
#    the output names the trashed issue and tells the caller it is recoverable.
# ---------------------------------------------------------------------------
output, calls = run_main({"issue": "eng-123"})
sent_query = calls[0]["query"]
sent_variables = calls[0]["variables"]
check(
    "1a: the mutation never contains permanentlyDelete",
    "permanentlyDelete" not in sent_query,
    detail=repr(sent_query),
)
check(
    "1b: the variables never carry permanentlyDelete",
    "permanentlyDelete" not in sent_variables,
    detail=repr(sent_variables),
)
check(
    "1c: the identifier is normalized and sent as the id",
    sent_variables == {"id": "ENG-123"},
    detail=repr(sent_variables),
)
check(
    "1d: the query selects the returned entity's identifier and title",
    "entity" in sent_query and "identifier" in sent_query and "title" in sent_query,
    detail=repr(sent_query),
)
check(
    "1e: output identifier comes from the returned entity",
    output["identifier"] == "ENG-123",
    detail=repr(output),
)
check(
    "1f: output title comes from the returned entity",
    output["title"] == "Fix it",
    detail=repr(output),
)
check(
    "1g: the note says the issue is in the trash and restorable for ~30 days",
    "trash" in output["note"]
    and "30 days" in output["note"]
    and "restore" in output["note"],
    detail=repr(output.get("note")),
)

# ---------------------------------------------------------------------------
# 2. A missing issue fails before any request.
# ---------------------------------------------------------------------------
try:
    run_main({})
    check("2a: a missing issue is rejected", False)
except ToolError as err:
    check("2a: a missing issue is rejected", "issue" in str(err), detail=str(err))

# ---------------------------------------------------------------------------
# 3. Unknown parameters fail before any request. permanentlyDelete is
#    deliberately not a parameter: this tool has no permanent mode.
# ---------------------------------------------------------------------------
for unknown in ("permanentlyDelete", "confirm", "ids"):
    try:
        run_main({"issue": "ENG-1", unknown: "true"})
        check(f"3: unknown parameter {unknown!r} is rejected", False)
    except ToolError as err:
        check(
            f"3: unknown parameter {unknown!r} is rejected",
            "Unknown parameters" in str(err),
            detail=str(err),
        )

# ---------------------------------------------------------------------------
# 4. A success=false payload is not a delete.
# ---------------------------------------------------------------------------
def failed_server(query, variables=None, *, api_key, deadline):
    return {"issueDelete": {"success": False, "entity": DELETED}}


try:
    run_main({"issue": "ENG-1"}, graphql=failed_server)
    check("4a: success false is a ToolError", False)
except ToolError as err:
    check(
        "4a: success false is a ToolError",
        "did not confirm" in str(err),
        detail=str(err),
    )

# ---------------------------------------------------------------------------
# 5. The schema types entity as nullable, so a success with no entity falls
#    back to the requested identifier rather than crashing.
# ---------------------------------------------------------------------------
def no_entity_server(query, variables=None, *, api_key, deadline):
    return {"issueDelete": {"success": True, "entity": None}}


output, calls = run_main({"issue": "eng-1"}, graphql=no_entity_server)
check(
    "5a: a null entity falls back to the requested identifier",
    output["identifier"] == "ENG-1" and output["title"] is None,
    detail=repr(output),
)

# ---------------------------------------------------------------------------
# 6. A server error surfaces unchanged and prints nothing.
# ---------------------------------------------------------------------------
def error_server(query, variables=None, *, api_key, deadline):
    raise ToolError("Linear reported an error: it did not work.")


try:
    run_main({"issue": "ENG-1"}, graphql=error_server)
    check("6a: a server error surfaces", False)
except ToolError as err:
    check(
        "6a: a server error surfaces",
        "it did not work" in str(err),
        detail=str(err),
    )

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
