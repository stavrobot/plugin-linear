#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for update_issue's label delta handling.

Runs offline: fetch_metadata and graphql_request are replaced with fakes. The
live API proves that addedLabelIds preserves the other labels; these checks
cover the part a live call cannot show cheaply, namely that the tool sends the
native delta fields (and never the whole-set labelIds) and so makes no
read-modify-write round trip.

Section 5 pins down the two strict behaviours of those native fields: removing
a label that is not on the issue is an error, and naming the same label in both
lists is an error. The tool deliberately forwards both rather than reconciling
them locally, so the checks assert the fields are sent unchanged and the server
error surfaces. Section 5 also contains the single most important assertion in
this suite: adding one label must not remove the labels already on the issue.

Run with: uv run tests/test_update_issue.py
"""

import importlib.util
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL_DIR = ROOT / "update_issue"
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
        "update_issue_run", TOOL_DIR / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = load_run()

BUG_ID = "l-bug"
FEATURE_ID = "l-feature"

METADATA = {
    "viewer": {"id": "viewer-1", "name": "Me"},
    "team": {"id": "team-1", "key": "ENG", "name": "Engineering"},
    "states": [],
    "labels": [
        {"id": BUG_ID, "name": "Bug"},
        {"id": FEATURE_ID, "name": "Feature"},
    ],
    "projects": [],
    "members": [],
    "users": [],
    "truncated": {
        "users": False,
        "states": False,
        "labels": False,
        "projects": False,
        "members": False,
    },
}

UPDATED_ISSUE = {
    "id": "issue-1",
    "identifier": "ENG-1",
    "title": "Fix it",
    "state": {"name": "In Progress", "type": "started"},
    "assignee": {"name": "Me"},
    "priority": 3,
    "priorityLabel": "Medium",
    "labels": {"nodes": [{"id": BUG_ID, "name": "Bug"}]},
    "project": None,
    "url": "https://linear.app/x/ENG-1",
    "createdAt": "2026-01-01",
    "updatedAt": "2026-01-02",
}


LABELS_BY_ID = {
    BUG_ID: {"id": BUG_ID, "name": "Bug"},
    FEATURE_ID: {"id": FEATURE_ID, "name": "Feature"},
}


def delta_server(existing_ids):
    """Model Linear's native label-delta semantics for one issue.

    Linear applies addedLabelIds/removedLabelIds against the issue's current
    labels, and it is strict about both: removing a label that is not present
    is an error, and naming the same label in both lists is an error. The
    returned 'seen' dict records the last input the tool actually sent, so a
    test can prove the tool forwards the strict delta instead of pre-filtering
    or reconciling it away.
    """
    seen = {}

    def server(query, variables=None, *, api_key, deadline):
        issue_input = variables["input"]
        seen["input"] = issue_input
        added = issue_input.get("addedLabelIds", [])
        removed = issue_input.get("removedLabelIds", [])
        if set(added) & set(removed):
            raise ToolError(
                "Linear reported an error: the same label cannot be added "
                "and removed in one update."
            )
        label_ids = list(existing_ids)
        for label_id in removed:
            if label_id not in label_ids:
                raise ToolError(
                    "Linear reported an error: the label is not on this issue."
                )
            label_ids.remove(label_id)
        for label_id in added:
            if label_id not in label_ids:
                label_ids.append(label_id)
        issue = dict(UPDATED_ISSUE)
        issue["labels"] = {"nodes": [LABELS_BY_ID[i] for i in label_ids]}
        return {"issueUpdate": {"success": True, "issue": issue}}

    return server, seen


def run_main(params, graphql=None):
    """Run run.main() with fakes installed, returning (output, calls)."""
    calls = []

    def fake_graphql(query, variables=None, *, api_key, deadline):
        calls.append({"query": query, "variables": variables})
        return {
            "issueUpdate": {"success": True, "issue": UPDATED_ISSUE},
        }

    original_graphql = linear.graphql_request
    original_metadata = linear.fetch_metadata
    original_config = linear.load_config
    linear.graphql_request = graphql or fake_graphql
    linear.fetch_metadata = lambda api_key, deadline: METADATA
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
        linear.fetch_metadata = original_metadata
        linear.load_config = original_config
    return json.loads(out.getvalue()), calls


# ---------------------------------------------------------------------------
# 1. add_labels sends only addedLabelIds: never the whole-set labelIds, and no
#    read query. This delta is what makes the add non-destructive.
# ---------------------------------------------------------------------------
output, calls = run_main({"issue": "ENG-1", "add_labels": "Feature"})
sent = calls[0]["variables"]["input"]
check("1a: one request only, the mutation", len(calls) == 1, detail=repr(len(calls)))
check(
    "1b: addedLabelIds carries the resolved id",
    sent.get("addedLabelIds") == [FEATURE_ID],
    detail=repr(sent),
)
check("1c: whole-set labelIds is not sent", "labelIds" not in sent, detail=repr(sent))
check(
    "1d: removedLabelIds is absent when nothing is removed",
    "removedLabelIds" not in sent,
    detail=repr(sent),
)
check(
    "1e: the updated issue is returned",
    output["identifier"] == "ENG-1",
    detail=repr(output),
)

# ---------------------------------------------------------------------------
# 2. remove_labels sends only removedLabelIds.
# ---------------------------------------------------------------------------
output, calls = run_main({"issue": "ENG-1", "remove_labels": "Bug"})
sent = calls[0]["variables"]["input"]
check(
    "2a: removedLabelIds carries the resolved id",
    sent.get("removedLabelIds") == [BUG_ID],
    detail=repr(sent),
)
check("2b: labelIds is not sent", "labelIds" not in sent, detail=repr(sent))

# ---------------------------------------------------------------------------
# 3. Both deltas can travel in the same mutation.
# ---------------------------------------------------------------------------
output, calls = run_main(
    {"issue": "ENG-1", "add_labels": "Feature", "remove_labels": "Bug"}
)
sent = calls[0]["variables"]["input"]
check(
    "3a: both delta fields are present",
    sent.get("addedLabelIds") == [FEATURE_ID]
    and sent.get("removedLabelIds") == [BUG_ID],
    detail=repr(sent),
)

# Duplicate names collapse rather than being sent twice.
output, calls = run_main({"issue": "ENG-1", "add_labels": "Feature, Feature"})
check(
    "3b: duplicate add names are de-duplicated",
    calls[0]["variables"]["input"]["addedLabelIds"] == [FEATURE_ID],
    detail=repr(calls[0]["variables"]["input"]),
)

# ---------------------------------------------------------------------------
# 4. An unknown label name fails loudly before any mutation.
# ---------------------------------------------------------------------------
try:
    run_main({"issue": "ENG-1", "add_labels": "Nonexistent"})
    check("4a: an unknown label name fails loudly", False)
except ToolError as err:
    check(
        "4a: an unknown label name fails loudly",
        "No label matches" in str(err),
        detail=str(err),
    )

# ---------------------------------------------------------------------------
# 5. THE DATA-LOSS GUARD (the single most important assertion in this suite):
#    adding one label must not remove the labels already on the issue. The tool
#    sends addedLabelIds only, so the server applies a delta and the labels the
#    call did not name survive. Anything that made the tool send the whole-set
#    labelIds would fail here.
# ---------------------------------------------------------------------------
server, seen = delta_server([BUG_ID])
output, calls = run_main({"issue": "ENG-1", "add_labels": "Feature"}, graphql=server)
check(
    "5a: adding a label keeps the labels already on the issue",
    set(output["labels"]) == {"Bug", "Feature"},
    detail=repr(output["labels"]),
)
check(
    "5b: exactly one label field is sent, and it is the add delta",
    seen["input"] == {"addedLabelIds": [FEATURE_ID]},
    detail=repr(seen["input"]),
)

# Removing a label that is NOT on the issue is a loud server error, not a
# silent no-op. The tool must still send the removal and surface the error
# rather than reading the issue first or dropping the removal.
server, seen = delta_server([BUG_ID])
try:
    run_main({"issue": "ENG-1", "remove_labels": "Feature"}, graphql=server)
    check("5c: removing a label that is not on the issue fails loudly", False)
except ToolError as err:
    check(
        "5c: removing a label that is not on the issue fails loudly",
        "not on this issue" in str(err),
        detail=str(err),
    )
check(
    "5d: the removal is sent, not pre-filtered away",
    seen["input"].get("removedLabelIds") == [FEATURE_ID],
    detail=repr(seen["input"]),
)

# The same label named in both lists is a loud server error too. The tool must
# not silently reconcile it: both strict fields travel to Linear.
server, seen = delta_server([BUG_ID])
try:
    run_main(
        {"issue": "ENG-1", "add_labels": "Feature", "remove_labels": "Feature"},
        graphql=server,
    )
    check("5e: the same label in add and remove fails loudly", False)
except ToolError as err:
    check(
        "5e: the same label in add and remove fails loudly",
        "same label" in str(err),
        detail=str(err),
    )
check(
    "5f: both strict delta fields carry the overlapping id",
    seen["input"].get("addedLabelIds") == [FEATURE_ID]
    and seen["input"].get("removedLabelIds") == [FEATURE_ID],
    detail=repr(seen["input"]),
)

# A real removal on a present label still works and leaves the rest alone.
server, seen = delta_server([BUG_ID, FEATURE_ID])
output, calls = run_main({"issue": "ENG-1", "remove_labels": "Bug"}, graphql=server)
check(
    "5g: removing one label keeps the others",
    output["labels"] == ["Feature"],
    detail=repr(output["labels"]),
)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
