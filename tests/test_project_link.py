#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///
"""Mocked checks for the shared project-link mutation.

Runs offline: linear.graphql_request is replaced with a fake, so no key is
read and no request is made. Covers the two things the live API cares about
that a typo would silently break: url validation and the default label.

Run with: uv run tests/test_project_link.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import linear  # noqa: E402
import project_link  # noqa: E402
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


# ---------------------------------------------------------------------------
# 1. default_label derives the hostname.
# ---------------------------------------------------------------------------
check(
    "1a: hostname from a normal URL",
    project_link.default_label("https://figma.com/file/abc") == "figma.com",
)
check(
    "1b: hostname is lower-cased",
    project_link.default_label("https://Figma.COM/x") == "figma.com",
)
check(
    "1c: port is stripped from the hostname",
    project_link.default_label("http://example.com:8080/x") == "example.com",
)

# ---------------------------------------------------------------------------
# 2. validate_url rejects junk before it reaches the API.
# ---------------------------------------------------------------------------
for bad in (
    "",
    "   ",
    "not a url",
    "figma.com/file/abc",
    "ftp://example.com/x",
    "javascript:alert(1)",
    "http://",
    "mailto:me@example.com",
):
    try:
        project_link.validate_url(bad)
        check(f"2: rejects {bad!r}", False)
    except ToolError:
        check(f"2: rejects {bad!r}", True)

check(
    "2a: accepts https",
    project_link.validate_url(" https://figma.com/x ") == "https://figma.com/x",
)
check(
    "2b: accepts http",
    project_link.validate_url("http://example.com") == "http://example.com",
)

# ---------------------------------------------------------------------------
# 3. attach_project_link sends projectId/url/label and defaults the label.
# ---------------------------------------------------------------------------
calls = []


def fake_graphql(query, variables=None, *, api_key, deadline):
    calls.append({"query": query, "variables": variables})
    return {
        "entityExternalLinkCreate": {
            "success": True,
            "entityExternalLink": {
                "id": "l1",
                "url": variables["input"]["url"],
                "label": variables["input"]["label"],
                "project": {"id": "p1", "name": "Proj"},
            },
        }
    }


original = linear.graphql_request
linear.graphql_request = fake_graphql
try:
    result = project_link.attach_project_link(
        "p1", "https://figma.com/file/abc", None, api_key="k", deadline=1.0
    )
finally:
    linear.graphql_request = original

check("3a: returns the created link", result["id"] == "l1", detail=repr(result))
check(
    "3b: label defaults to the URL hostname",
    calls[0]["variables"]["input"]["label"] == "figma.com",
    detail=repr(calls[0]["variables"]),
)
check(
    "3c: exactly one target id is sent, as projectId",
    calls[0]["variables"]["input"].get("projectId") == "p1"
    and not (
        set(calls[0]["variables"]["input"])
        & {"teamId", "releaseId", "cycleId", "initiativeId"}
    ),
    detail=repr(calls[0]["variables"]),
)

# An explicit label wins over the hostname.
calls.clear()
linear.graphql_request = fake_graphql
try:
    project_link.attach_project_link(
        "p1", "https://figma.com/file/abc", "Design file", api_key="k", deadline=1.0
    )
finally:
    linear.graphql_request = original
check(
    "3d: an explicit label is used verbatim",
    calls[0]["variables"]["input"]["label"] == "Design file",
    detail=repr(calls[0]["variables"]),
)

# A blank explicit label still must not produce an empty label.
calls.clear()
linear.graphql_request = fake_graphql
try:
    project_link.attach_project_link(
        "p1", "https://figma.com/file/abc", "   ", api_key="k", deadline=1.0
    )
finally:
    linear.graphql_request = original
check(
    "3e: a blank label falls back to the hostname",
    calls[0]["variables"]["input"]["label"] == "figma.com",
    detail=repr(calls[0]["variables"]),
)

# A payload without a confirmed link must fail loudly, not report success.
linear.graphql_request = lambda *a, **k: {
    "entityExternalLinkCreate": {"success": True, "entityExternalLink": None}
}
try:
    project_link.attach_project_link(
        "p1", "https://figma.com/x", None, api_key="k", deadline=1.0
    )
    check("3f: unconfirmed link fails loudly", False)
except ToolError:
    check("3f: unconfirmed link fails loudly", True)
finally:
    linear.graphql_request = original

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES out of {CHECKS} checks: {FAILURES}")
    sys.exit(1)
print(f"ALL {CHECKS} CHECKS PASSED")
