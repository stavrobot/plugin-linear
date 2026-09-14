#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for linear.py.

Every network call is replaced with a fake and no credentials are read, so
these cover the branching that a live smoke check cannot reach reliably:
GraphQL error handling, the deadline, and the name-resolution rules.

Run with: uv run tests/test_linear.py
"""

import json
import pathlib
import sys
import tempfile
import time
import traceback

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
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


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_post(response):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response

    return post, calls


def run_graphql(response, deadline=None, variables=None, api_key="test-key"):
    """Patch requests.post, call graphql_request, return (result, calls, err)."""
    post, calls = make_post(response)
    original = linear.requests.post
    linear.requests.post = post
    try:
        result = linear.graphql_request(
            "{ viewer { id } }",
            variables,
            api_key=api_key,
            deadline=deadline if deadline is not None else time.monotonic() + 10,
        )
        return result, calls, None
    except ToolError as err:
        return None, calls, err
    finally:
        linear.requests.post = original


# ---------------------------------------------------------------------------
# 1. GraphQL transport/error handling.
# ---------------------------------------------------------------------------
result, calls, err = run_graphql(FakeResponse(body={"data": {"viewer": {"id": "u1"}}}))
check(
    "1a: success returns the data object",
    result == {"viewer": {"id": "u1"}},
    detail=repr(result),
)
check("1b: posts to the GraphQL endpoint", calls[0]["url"] == linear.GRAPHQL_URL)
check(
    "1c: Authorization header is the bare key (no Bearer)",
    calls[0]["headers"]["Authorization"] == "test-key",
    detail=repr(calls[0]["headers"]),
)
check(
    "1d: variables included when supplied",
    run_graphql(FakeResponse(body={"data": {}}), variables={"x": 1})[1][0]["json"].get(
        "variables"
    )
    == {"x": 1},
)

# A 200 carrying a GraphQL errors array is a failure, not a success.
result, calls, err = run_graphql(
    FakeResponse(body={"data": None, "errors": [{"message": "Query too complex"}]})
)
check("1e: 200 + errors raises ToolError", isinstance(err, ToolError), detail=repr(err))
check(
    "1f: error message carries the GraphQL message",
    "Query too complex" in str(err),
    detail=str(err),
)
check("1g: nothing is returned on a 200 + errors", result is None)

# A non-200 must fail loudly.
result, calls, err = run_graphql(
    FakeResponse(
        status_code=400, body={"errors": [{"message": "bad key"}]}, text='{"errors":[]}'
    )
)
check("1h: non-200 raises ToolError", isinstance(err, ToolError), detail=repr(err))
check("1i: non-200 message names the status", "400" in str(err), detail=str(err))

# A transport failure must fail loudly.
result, calls, err = run_graphql(requests.exceptions.ConnectionError("boom"))
check(
    "1j: transport failure raises ToolError",
    isinstance(err, ToolError),
    detail=repr(err),
)
check(
    "1k: transport message mentions the failure", "failed" in str(err), detail=str(err)
)

# Unparseable body and missing data must fail loudly.
result, calls, err = run_graphql(FakeResponse(body=None, text="not json"))
check(
    "1l: unparseable body raises ToolError",
    isinstance(err, ToolError),
    detail=repr(err),
)
result, calls, err = run_graphql(FakeResponse(body={"data": "nope"}))
check(
    "1m: response without a data object raises",
    isinstance(err, ToolError),
    detail=repr(err),
)

# Regression: a transport exception can embed the API key. A key with an
# embedded newline makes requests raise InvalidHeader with the full key in its
# message, and a chained traceback would print it. Synthetic value only.
SYNTHETIC_KEY = "fake\nkey-not-a-real-secret"
result, calls, err = run_graphql(
    requests.exceptions.InvalidHeader(f"Invalid header value: {SYNTHETIC_KEY}"),
    api_key=SYNTHETIC_KEY,
)
check(
    "1n: leaky transport failure still raises ToolError",
    isinstance(err, ToolError),
    detail=repr(err),
)
printed = "".join(traceback.format_exception(type(err), err, err.__traceback__))
check(
    "1o: the API key never appears in the printable error",
    SYNTHETIC_KEY not in str(err) and SYNTHETIC_KEY not in printed,
    detail=printed,
)

# Response-derived text can itself echo the Authorization value: an HTTP body
# or a GraphQL error message. Both are redacted before they are surfaced. The
# body case uses a key with an embedded newline, so it also proves redaction
# runs before whitespace-normalization rather than after it. Synthetic values.
LEAK_BODY = "fake\nresponse-body-key-not-real"
result, calls, err = run_graphql(
    FakeResponse(status_code=500, body=None, text=f"Authorization: {LEAK_BODY}"),
    api_key=LEAK_BODY,
)
check(
    "1p: HTTP-body leak still raises ToolError",
    isinstance(err, ToolError),
    detail=repr(err),
)
check(
    "1q: the key never appears in an HTTP-body error",
    LEAK_BODY not in str(err)
    and "fake response-body-key-not-real" not in str(err)
    and "[redacted]" in str(err),
    detail=str(err),
)

LEAK_GQL = "sk-synthetic-graphql-key-do-not-use"
result, calls, err = run_graphql(
    FakeResponse(
        body={"data": None, "errors": [{"message": f"bad auth header: {LEAK_GQL}"}]}
    ),
    api_key=LEAK_GQL,
)
check(
    "1r: GraphQL-error leak still raises ToolError",
    isinstance(err, ToolError),
    detail=repr(err),
)
check(
    "1s: the key never appears in a GraphQL-error message",
    LEAK_GQL not in str(err) and "[redacted]" in str(err),
    detail=str(err),
)

# ---------------------------------------------------------------------------
# 2. Deadline: an expired budget aborts before any request.
# ---------------------------------------------------------------------------
result, calls, err = run_graphql(
    FakeResponse(body={"data": {}}), deadline=time.monotonic() - 1.0
)
check(
    "2a: expired deadline raises ToolError",
    isinstance(err, ToolError),
    detail=repr(err),
)
check("2b: expired deadline issues no request", len(calls) == 0)
check(
    "2c: expired deadline message mentions the budget",
    "budget" in str(err),
    detail=str(err),
)

# ---------------------------------------------------------------------------
# 3. config loading.
# ---------------------------------------------------------------------------
original_root = linear.ROOT
try:
    with tempfile.TemporaryDirectory() as tmp:
        linear.ROOT = pathlib.Path(tmp)

        (linear.ROOT / "config.json").write_text("{ not json")
        try:
            linear.load_config()
            check("3a: damaged config fails", False)
        except ToolError as e:
            check(
                "3a: damaged config fails",
                "Could not read config.json" in str(e),
                detail=str(e),
            )

        (linear.ROOT / "config.json").write_text(json.dumps({"other": "x"}))
        try:
            linear.load_config()
            check("3b: missing api_key fails", False)
        except ToolError as e:
            check(
                "3b: missing api_key fails",
                "No Linear API key" in str(e),
                detail=str(e),
            )

        (linear.ROOT / "config.json").write_text(json.dumps({"api_key": "   "}))
        try:
            linear.load_config()
            check("3c: whitespace api_key fails", False)
        except ToolError as e:
            check(
                "3c: whitespace api_key fails",
                "No Linear API key" in str(e),
                detail=str(e),
            )

        (linear.ROOT / "config.json").write_text(json.dumps({"api_key": "k"}))
        check("3d: valid config loads", linear.load_config()["api_key"] == "k")
finally:
    linear.ROOT = original_root


# ---------------------------------------------------------------------------
# 4. Metadata fetch: one request, flattened team, per-collection truncation.
# ---------------------------------------------------------------------------
def make_team(
    states_next=False, labels_next=False, projects_next=False, members_next=False
):
    return {
        "id": "team-1",
        "key": "ENG",
        "name": "Engineering",
        "states": {
            "nodes": [{"id": "s1", "name": "Backlog"}],
            "pageInfo": {"hasNextPage": states_next},
        },
        "labels": {
            "nodes": [{"id": "l1", "name": "Bug"}],
            "pageInfo": {"hasNextPage": labels_next},
        },
        "projects": {
            "nodes": [{"id": "p1", "name": "Roadmap"}],
            "pageInfo": {"hasNextPage": projects_next},
        },
        "members": {
            "nodes": [{"id": "m1", "name": "Ada"}],
            "pageInfo": {"hasNextPage": members_next},
        },
    }


TEAM = make_team()


def metadata_body(teams=TEAM, users_next=False):
    return {
        "data": {
            "viewer": {"id": "viewer-1", "name": "Me"},
            "users": {
                "nodes": [{"id": "u1", "name": "Ada", "active": True}],
                "pageInfo": {"hasNextPage": users_next},
            },
            "teams": {"nodes": [teams]},
        }
    }


original_post = linear.requests.post
post, calls = make_post(FakeResponse(body=metadata_body()))
linear.requests.post = post
try:
    metadata = linear.fetch_metadata("test-key", time.monotonic() + 10)
finally:
    linear.requests.post = original_post
check("4a: metadata is one request", len(calls) == 1, detail=repr(len(calls)))
check(
    "4b: team flattened from teams[0]",
    metadata["team"] == {"id": "team-1", "key": "ENG", "name": "Engineering"},
    detail=repr(metadata["team"]),
)
check(
    "4c: states/labels/projects/members exposed",
    [len(metadata[k]) for k in ("states", "labels", "projects", "members")]
    == [1, 1, 1, 1],
)
check(
    "4d: viewer and users exposed",
    metadata["viewer"]["id"] == "viewer-1" and metadata["users"][0]["id"] == "u1",
)
check(
    "4e: no truncation when no page is full",
    metadata["truncated"]
    == {
        "users": False,
        "states": False,
        "labels": False,
        "projects": False,
        "members": False,
    },
    detail=repr(metadata["truncated"]),
)

post, calls = make_post(FakeResponse(body=metadata_body(users_next=True)))
linear.requests.post = post
try:
    metadata = linear.fetch_metadata("test-key", time.monotonic() + 10)
finally:
    linear.requests.post = original_post
check(
    "4f: truncation is tracked per collection",
    metadata["truncated"]["users"] is True
    and metadata["truncated"]["states"] is False
    and metadata["truncated"]["projects"] is False,
    detail=repr(metadata["truncated"]),
)

# An overflowing project list must not mark any other collection incomplete.
post, calls = make_post(
    FakeResponse(body=metadata_body(teams=make_team(projects_next=True)))
)
linear.requests.post = post
try:
    metadata = linear.fetch_metadata("test-key", time.monotonic() + 10)
finally:
    linear.requests.post = original_post
check(
    "4h: only the overflowing collection is flagged",
    metadata["truncated"]["projects"] is True
    and metadata["truncated"]["states"] is False
    and metadata["truncated"]["labels"] is False
    and metadata["truncated"]["users"] is False,
    detail=repr(metadata["truncated"]),
)

post, calls = make_post(FakeResponse(body={"data": {"teams": {"nodes": []}}}))
linear.requests.post = post
try:
    linear.fetch_metadata("test-key", time.monotonic() + 10)
    check("4g: no team fails loudly", False)
except ToolError as e:
    check("4g: no team fails loudly", "No Linear team" in str(e), detail=str(e))
finally:
    linear.requests.post = original_post

# ---------------------------------------------------------------------------
# 5. Name resolution rules.
# ---------------------------------------------------------------------------
STATES = [
    {"id": "s1", "name": "Backlog"},
    {"id": "s2", "name": "In Progress"},
    {"id": "s3", "name": "In Review"},
]
check(
    "5a: exact case-insensitive state match",
    linear.resolve_state("in progress", STATES, truncated=False) == "s2",
)
check(
    "5b: unique substring state match",
    linear.resolve_state("prog", STATES, truncated=False) == "s2",
)
try:
    linear.resolve_state("in", STATES, truncated=False)
    check("5c: ambiguous state lists candidates", False)
except ToolError as e:
    check(
        "5c: ambiguous state lists candidates",
        "Candidates:" in str(e) and "In Progress" in str(e),
        detail=str(e),
    )
try:
    linear.resolve_state("Done", STATES, truncated=False)
    check("5d: unknown state lists valid candidates", False)
except ToolError as e:
    check(
        "5d: unknown state lists valid candidates",
        "No state matches" in str(e)
        and "Backlog" in str(e)
        and "In Progress" in str(e)
        and "In Review" in str(e),
        detail=str(e),
    )
try:
    linear.resolve_state("   ", STATES, truncated=False)
    check("5e: empty state fails", False)
except ToolError as e:
    check("5e: empty state fails", "empty" in str(e), detail=str(e))

MEMBERS = [
    {"id": "m1", "name": "Ada Lovelace", "displayName": "ada"},
    {"id": "m2", "name": "Bob", "displayName": "bobby"},
]
check(
    "5f: assignee exact name",
    linear.resolve_assignee("Ada Lovelace", MEMBERS, {"id": "v1"}, truncated=False)
    == "m1",
)
check(
    "5g: assignee displayName match",
    linear.resolve_assignee("bobby", MEMBERS, {"id": "v1"}, truncated=False) == "m2",
)
check(
    "5h: 'me' maps to the viewer",
    linear.resolve_assignee("me", MEMBERS, {"id": "v1"}, truncated=False) == "v1",
)
check(
    "5i: 'ME' is case-insensitive",
    linear.resolve_assignee("ME", MEMBERS, {"id": "v1"}, truncated=False) == "v1",
)
try:
    linear.resolve_assignee("me", MEMBERS, {}, truncated=False)
    check("5j: 'me' without a viewer fails", False)
except ToolError as e:
    check("5j: 'me' without a viewer fails", "viewer" in str(e), detail=str(e))

# Projects and labels use the same machinery.
check(
    "5k: project exact match",
    linear.resolve_project(
        "Roadmap", [{"id": "p1", "name": "Roadmap"}], truncated=False
    )
    == "p1",
)
check(
    "5l: label exact match",
    linear.resolve_label("bug", [{"id": "l1", "name": "Bug"}], truncated=False) == "l1",
)

# An incomplete collection must not turn a substring match into a confidently
# wrong entity, and a miss must not be stated as fact.
PARTIAL_PROJECTS = [{"id": "p1", "name": "Website refresh"}]
try:
    linear.resolve_project("Website", PARTIAL_PROJECTS, truncated=True)
    check("5m: truncated substring match is refused", False)
except ToolError as e:
    check(
        "5m: truncated substring match is refused",
        "incomplete" in str(e) and "partial" in str(e) and "exact" in str(e),
        detail=str(e),
    )
check(
    "5n: exact match is trusted even when truncated",
    linear.resolve_project("Website refresh", PARTIAL_PROJECTS, truncated=True) == "p1",
)
try:
    linear.resolve_project("Nonexistent", PARTIAL_PROJECTS, truncated=True)
    check("5o: truncated miss is not definitive", False)
except ToolError as e:
    check(
        "5o: truncated miss is not definitive",
        "incomplete" in str(e) and "No project matches" not in str(e),
        detail=str(e),
    )
# A truncated collection must not change the ordinary complete-collection path.
check(
    "5p: complete substring match still resolves",
    linear.resolve_project("Web", PARTIAL_PROJECTS, truncated=False) == "p1",
)
try:
    linear.resolve_project("Nonexistent", PARTIAL_PROJECTS, truncated=False)
    check("5q: complete miss stays definitive", False)
except ToolError as e:
    check(
        "5q: complete miss stays definitive",
        "No project matches" in str(e),
        detail=str(e),
    )

# ---------------------------------------------------------------------------
# 6. Priority mapping.
# ---------------------------------------------------------------------------
check(
    "6a: priorities map to the schema integers",
    [linear.resolve_priority(w) for w in ("none", "urgent", "high", "medium", "low")]
    == [0, 1, 2, 3, 4],
)
check("6b: priority is case-insensitive", linear.resolve_priority("URGENT") == 1)
try:
    linear.resolve_priority("critical")
    check("6c: unknown priority fails", False)
except ToolError as e:
    check(
        "6c: unknown priority fails listing accepted words",
        all(word in str(e) for word in ("none", "urgent", "high", "medium", "low")),
        detail=str(e),
    )

# ---------------------------------------------------------------------------
# 7. Comma-separated list parser.
# ---------------------------------------------------------------------------
check(
    "7a: trims and drops empties",
    linear.parse_csv_list(" a, b ,,c , ") == ["a", "b", "c"],
)
check("7b: None yields an empty list", linear.parse_csv_list(None) == [])
check(
    "7c: an existing list is cleaned too",
    linear.parse_csv_list(["x", " ", "y"]) == ["x", "y"],
)

# ---------------------------------------------------------------------------
# 8. Issue identifier resolution (no lookup, per the spike).
# ---------------------------------------------------------------------------
check(
    "8a: valid identifier normalized upper-case",
    linear.resolve_issue_identifier("eng-123") == "ENG-123",
)
try:
    linear.resolve_issue_identifier("not an issue")
    check("8b: malformed identifier fails", False)
except ToolError as e:
    check("8b: malformed identifier fails", "not a valid" in str(e), detail=str(e))
try:
    linear.resolve_issue_identifier("")
    check("8c: empty identifier fails", False)
except ToolError as e:
    check("8c: empty identifier fails", "empty" in str(e), detail=str(e))

# ---------------------------------------------------------------------------
# 9. Issue formatter shape.
# ---------------------------------------------------------------------------
formatted = linear.format_issue(
    {
        "id": "i1",
        "identifier": "ENG-1",
        "title": "Fix it",
        "state": {"name": "In Progress", "type": "started"},
        "assignee": {"name": "Ada"},
        "priority": 1,
        "priorityLabel": "Urgent",
        "labels": {"nodes": [{"name": "Bug"}, {"name": "P1"}]},
        "project": {"name": "Roadmap"},
        "url": "https://linear.app/x/ENG-1",
        "createdAt": "2026-01-01",
        "updatedAt": "2026-01-02",
    }
)
check(
    "9a: formatter flattens state/assignee/labels/project",
    formatted["state"] == "In Progress"
    and formatted["state_type"] == "started"
    and formatted["assignee"] == "Ada"
    and formatted["labels"] == ["Bug", "P1"]
    and formatted["project"] == "Roadmap",
    detail=repr(formatted),
)
check("9b: formatter keeps priority label", formatted["priority_label"] == "Urgent")
check(
    "9c: formatter tolerates a null state/assignee",
    linear.format_issue({})["state"] is None,
)

# ---------------------------------------------------------------------------
# 10. Shared due-date validation, null-dropping formatters, and the deliberate
#     split between the metadata query and list_projects.
# ---------------------------------------------------------------------------
check(
    "10a: a valid due date is accepted",
    linear.validate_due_date("2026-09-30") == "2026-09-30",
)
try:
    linear.validate_due_date("2026-9-30")
    check("10b: a non-ISO due date fails", False)
except ToolError as e:
    check("10b: a non-ISO due date fails", "ISO date" in str(e), detail=str(e))
try:
    linear.validate_due_date("2026-13-99")
    check("10c: an impossible due date fails", False)
except ToolError as e:
    check(
        "10c: an impossible due date fails",
        "calendar date" in str(e),
        detail=str(e),
    )
check(
    "10d: format_state drops null fields",
    linear.format_state({"id": "s1", "name": "Backlog"})
    == {"id": "s1", "name": "Backlog"},
)
check(
    "10e: format_label drops null fields",
    linear.format_label({"id": "l1", "name": "Bug", "color": "#f00"})
    == {"id": "l1", "name": "Bug", "color": "#f00"},
)
check(
    "10f: format_project is gone (list_projects owns its own formatter)",
    not hasattr(linear, "format_project"),
)
# The metadata query must not grow list_projects' rich fields: it runs on
# every call, and externalLinks would blow the complexity budget.
check(
    "10g: the metadata query never fetches externalLinks",
    "externalLinks" not in linear._METADATA_QUERY,
)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
