#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for create_label.

Runs offline: graphql_request is replaced with a fake. The important
assertion is that teamId is omitted, because sending it would silently move
the label from workspace level to a single team.

Run with: uv run tests/test_create_label.py
"""

import importlib.util
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL_DIR = ROOT / "create_label"
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
        "create_label_run", TOOL_DIR / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load_run()
CREATED = {
    "id": "l1",
    "name": "plugin test - delete me",
    "color": "#ff0000",
    "description": "temp",
}


def run_main(params):
    calls = []

    def fake_graphql(query, variables=None, *, api_key, deadline):
        calls.append(variables)
        return {"issueLabelCreate": {"success": True, "issueLabel": CREATED}}

    original = linear.graphql_request
    original_config = linear.load_config
    linear.graphql_request = fake_graphql
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
        linear.graphql_request = original
        linear.load_config = original_config
    return json.loads(out.getvalue()), calls


# ---------------------------------------------------------------------------
# 1. The input carries name/description/color and never teamId.
# ---------------------------------------------------------------------------
output, calls = run_main(
    {"name": "plugin test - delete me", "description": "temp", "color": "#ff0000"}
)
sent = calls[0]["input"]
check("1a: name is sent", sent["name"] == "plugin test - delete me", detail=repr(sent))
check("1b: description is sent", sent["description"] == "temp", detail=repr(sent))
check("1c: color is sent", sent["color"] == "#ff0000", detail=repr(sent))
check(
    "1d: teamId is omitted for a workspace-level label",
    "teamId" not in sent,
    detail=repr(sent),
)
check(
    "1e: the created label is returned",
    output["label"]["id"] == "l1",
    detail=repr(output),
)

# Optional description and color are omitted rather than sent empty.
output, calls = run_main({"name": "plugin test - delete me"})
sent = calls[0]["input"]
check(
    "1f: omitted optionals are absent from the input",
    set(sent) == {"name"},
    detail=repr(sent),
)

# A bare six-digit color gains the leading '#'.
output, calls = run_main({"name": "plugin test - delete me", "color": "00ff00"})
check("1g: a bare hex color is normalized", calls[0]["input"]["color"] == "#00ff00")

# ---------------------------------------------------------------------------
# 2. Color and name validation fail before any mutation.
# ---------------------------------------------------------------------------
for bad in ("red", "#ff00", "#gggggg", "#ff00000"):
    try:
        run_main({"name": "plugin test - delete me", "color": bad})
        check(f"2: rejects color {bad!r}", False)
    except ToolError:
        check(f"2: rejects color {bad!r}", True)

try:
    run_main({"name": "   "})
    check("2a: an empty name fails", False)
except ToolError as err:
    check("2a: an empty name fails", "name" in str(err), detail=str(err))

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
