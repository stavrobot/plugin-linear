#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""List Linear issues with optional structured filters.

With no parameters this is the default view: issues assigned to the viewer
that are not completed or cancelled. Supplying a filter narrows that base
view rather than replacing it, except that an explicit 'state' or 'assignee'
overrides the corresponding default. Passing assignee 'anyone' drops the
assignee filter entirely, so 'open issues in project X' can mean the whole
project rather than only the viewer's slice. Every name is resolved through
linear.py's resolvers so a typo fails loudly instead of matching the wrong
thing.

Only one page is ever fetched; the output says so when the limit cut the
results short.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError

DEFAULT_LIMIT = 25

# Linear caps a single page at 250 useful nodes, verified against the live
# API. Asking for more than this is pointless, so the effective limit is
# clamped and reported.
MAX_PAGE = 250

# Completed and cancelled issues are hidden unless a state is requested
# explicitly. "canceled" is Linear's one-l spelling.
_OPEN_STATE_TYPES = ["completed", "canceled"]

_ISSUES_QUERY = """
query($first: Int, $filter: IssueFilter!) {
  issues(first: $first, filter: $filter, includeArchived: false) {
    nodes {
      %(fields)s
    }
    pageInfo { hasNextPage }
  }
}
""" % {"fields": linear.ISSUE_FIELDS}


def _limit(params: dict) -> int:
    """Read and bound the requested page size."""
    raw = params.get("limit", DEFAULT_LIMIT)
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"limit must be a whole number, got '{raw}'.") from None
    if limit < 1:
        raise ToolError("limit must be at least 1.")
    return min(limit, MAX_PAGE)


def _filter(metadata: dict, params: dict) -> dict:
    """Build the Linear IssueFilter from the parameters.

    The viewer is the default assignee and completed/cancelled states are
    excluded by default; an explicit assignee or state replaces that piece.
    The special assignee value 'anyone' drops the assignee filter so issues
    assigned to no one, or to somebody else, are included too.
    """
    truncated = metadata["truncated"]
    issue_filter = {}

    # An absent parameter takes the default; a supplied one is never silently
    # ignored. A blank supplied value therefore routes through the resolver,
    # which fails loudly on an empty name, rather than widening the search.
    assignee = params.get("assignee")
    if assignee is None:
        assignee_id = (metadata["viewer"] or {}).get("id")
        if not assignee_id:
            raise ToolError(
                "Could not determine the Linear viewer, so the default "
                "'assigned to me' filter cannot be applied. Pass an explicit "
                "assignee, or 'anyone'."
            )
        issue_filter["assignee"] = {"id": {"eq": assignee_id}}
    elif str(assignee).strip().lower() == "anyone":
        # No assignee filter at all: this is how a caller asks for everyone's
        # issues rather than their own open issues.
        pass
    else:
        assignee_id = linear.resolve_assignee(
            assignee,
            metadata["members"],
            metadata["viewer"],
            truncated=truncated["members"],
        )
        issue_filter["assignee"] = {"id": {"eq": assignee_id}}

    state = params.get("state")
    if state is None:
        issue_filter["state"] = {"type": {"nin": _OPEN_STATE_TYPES}}
    else:
        state_id = linear.resolve_state(
            state, metadata["states"], truncated=truncated["states"]
        )
        issue_filter["state"] = {"id": {"eq": state_id}}

    project = params.get("project")
    if project is not None:
        project_id = linear.resolve_project(
            project, metadata["projects"], truncated=truncated["projects"]
        )
        issue_filter["project"] = {"id": {"eq": project_id}}

    label = params.get("label")
    if label is not None:
        label_id = linear.resolve_label(
            label, metadata["labels"], truncated=truncated["labels"]
        )
        # A collection filter needs 'some': the issue carries the label.
        issue_filter["labels"] = {"some": {"id": {"eq": label_id}}}

    return issue_filter


def main() -> None:
    params = json.load(sys.stdin)

    config = linear.load_config()
    deadline = linear.api_deadline()
    metadata = linear.fetch_metadata(config["api_key"], deadline)

    first = _limit(params)
    data = linear.graphql_request(
        _ISSUES_QUERY,
        {"first": first, "filter": _filter(metadata, params)},
        api_key=config["api_key"],
        deadline=deadline,
    )

    connection = data.get("issues") or {}
    issues = [linear.format_issue(node) for node in (connection.get("nodes") or [])]
    truncated = bool((connection.get("pageInfo") or {}).get("hasNextPage"))

    result = {
        "issues": issues,
        "count": len(issues),
        "limit": first,
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (
            f"Showing {len(issues)} issues; the limit of {first} was reached "
            "and more match."
        )
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
