#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""Get one Linear issue in full by its human identifier.

Linear accepts the identifier (e.g. "ENG-123") directly on issue(id:), so no
lookup is performed. The response carries the compact issue summary plus the
fields only a detail view needs: description, due date, parent, sub-issues
and the 20 most recent comments. Linear returns comments oldest-first, so the
newest page is requested with 'last'; an older page being dropped is reported
in the output.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError

# Both are deliberate, explicit caps: comments are a connection whose default
# size is 50 and sub-issues could be many, and unbounded nested connections
# risk the query-complexity cap.
COMMENT_LIMIT = 20
SUB_ISSUE_LIMIT = 20

_ISSUE_QUERY = """
query($id: String!) {
  issue(id: $id) {
    %(fields)s
    description
    dueDate
    parent {
      identifier
      title
      url
      state { name }
    }
    children(first: %(sub_issues)d) {
      nodes {
        identifier
        title
        url
        state { name }
        assignee { name displayName }
      }
      pageInfo { hasNextPage }
    }
    comments(last: %(comments)d) {
      nodes {
        body
        createdAt
        url
        user { name displayName }
      }
      pageInfo { hasPreviousPage }
    }
  }
}
""" % {
    "fields": linear.ISSUE_FIELDS,
    "comments": COMMENT_LIMIT,
    "sub_issues": SUB_ISSUE_LIMIT,
}


def _person(user) -> str | None:
    if not user:
        return None
    return user.get("name") or user.get("displayName")


def _issue_ref(issue: dict) -> dict:
    """A compact reference to a related (parent or sub-) issue."""
    return {
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "state": (issue.get("state") or {}).get("name"),
        "assignee": _person(issue.get("assignee")),
        "url": issue.get("url"),
    }


def _comment(comment: dict) -> dict:
    return {
        "author": _person(comment.get("user")),
        "body": comment.get("body"),
        "created_at": comment.get("createdAt"),
        "url": comment.get("url"),
    }


def main() -> None:
    params = json.load(sys.stdin)
    identifier = linear.resolve_issue_identifier(params.get("identifier"))

    config = linear.load_config()
    deadline = linear.api_deadline()
    data = linear.graphql_request(
        _ISSUE_QUERY,
        {"id": identifier},
        api_key=config["api_key"],
        deadline=deadline,
    )

    issue = data.get("issue")
    if not isinstance(issue, dict):
        raise ToolError(f"Issue '{identifier}' was not found.")

    full_issue = linear.format_issue(issue)
    full_issue["description"] = issue.get("description")
    full_issue["due_date"] = issue.get("dueDate")

    parent = issue.get("parent")
    full_issue["parent"] = _issue_ref(parent) if isinstance(parent, dict) else None

    children = issue.get("children") or {}
    full_issue["sub_issues"] = [
        _issue_ref(child) for child in (children.get("nodes") or [])
    ]
    full_issue["sub_issues_truncated"] = bool(
        (children.get("pageInfo") or {}).get("hasNextPage")
    )

    comments_connection = issue.get("comments") or {}
    comments = [
        _comment(comment) for comment in (comments_connection.get("nodes") or [])
    ]
    comments_truncated = bool(
        (comments_connection.get("pageInfo") or {}).get("hasPreviousPage")
    )

    result = {
        "issue": full_issue,
        "comments": comments,
        "comments_truncated": comments_truncated,
    }
    if comments_truncated:
        result["note"] = (
            f"Older comments were dropped; showing the {COMMENT_LIMIT} most recent."
        )
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
