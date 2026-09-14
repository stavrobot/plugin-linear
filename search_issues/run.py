#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""Full-text search across Linear issues.

This uses Linear's searchIssues mechanism, which ranks keyword matches in
titles, descriptions and comments. It is deliberately separate from
list_issues' structured issues(filter:) path and must not be reimplemented
with filters. Only one page is returned; the output says so when the limit
cut the results short.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError

DEFAULT_LIMIT = 25

# The largest useful page size on this API, verified against the live
# workspace.
MAX_PAGE = 250

_SEARCH_QUERY = """
query($term: String!, $first: Int) {
  searchIssues(term: $term, first: $first, includeArchived: false) {
    nodes {
      %(fields)s
    }
    pageInfo { hasNextPage }
    totalCount
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


def main() -> None:
    params = json.load(sys.stdin)
    term = str(params.get("query") or "").strip()
    if not term:
        raise ToolError("A search query is required.")

    config = linear.load_config()
    deadline = linear.api_deadline()
    first = _limit(params)
    data = linear.graphql_request(
        _SEARCH_QUERY,
        {"term": term, "first": first},
        api_key=config["api_key"],
        deadline=deadline,
    )

    connection = data.get("searchIssues") or {}
    issues = [linear.format_issue(node) for node in (connection.get("nodes") or [])]
    truncated = bool((connection.get("pageInfo") or {}).get("hasNextPage"))
    total = connection.get("totalCount")

    result = {
        "query": term,
        "issues": issues,
        "count": len(issues),
        "limit": first,
        "truncated": truncated,
    }
    total_matches = int(total) if isinstance(total, (int, float)) else None
    if total_matches is not None:
        result["total_matches"] = total_matches
    if truncated:
        if total_matches is not None:
            result["note"] = (
                f"Showing {len(issues)} of {total_matches} matches; increase "
                "limit to see more."
            )
        else:
            result["note"] = (
                f"Showing {len(issues)} matches; the limit of {first} was reached."
            )
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
