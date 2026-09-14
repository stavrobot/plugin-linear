#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

import json
import pathlib
import sys

# The shared root module holds config loading, the GraphQL helper and the
# issue identifier normalizer. It lives at the plugin root so every tool
# adopts it the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError

# commentCreate accepts the human issue identifier directly (per the spike),
# so no lookup to a UUID happens here and no metadata fetch is needed.
COMMENT_MUTATION = """
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment {
      id
      body
      url
      createdAt
      user { name displayName }
      issue { identifier url }
    }
  }
}
"""

KNOWN_PARAMS = {"issue", "body"}


def format_comment(comment: dict) -> dict:
    """Reduce a GraphQL comment node to the compact shape add_comment returns.

    The issue identifier and URL are included so the caller can confirm where
    the comment landed without a second call.
    """
    user = comment.get("user") or {}
    issue = comment.get("issue") or {}
    return {
        "id": comment.get("id"),
        "issue": issue.get("identifier"),
        "issue_url": issue.get("url"),
        "body": comment.get("body"),
        "author": user.get("name") or user.get("displayName"),
        "url": comment.get("url"),
        "created_at": comment.get("createdAt"),
    }


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    if "issue" not in params:
        raise ToolError("Missing required parameter: issue")
    issue_identifier = linear.resolve_issue_identifier(params["issue"])

    body = params.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ToolError("Missing required parameter: body")

    config = linear.load_config()
    api_key = str(config["api_key"])
    deadline = linear.api_deadline()

    data = linear.graphql_request(
        COMMENT_MUTATION,
        {"input": {"issueId": issue_identifier, "body": body}},
        api_key=api_key,
        deadline=deadline,
    )

    payload = data.get("commentCreate")
    comment = payload.get("comment") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or not payload.get("success")
        or not isinstance(comment, dict)
    ):
        raise ToolError(
            f"Linear did not confirm that the comment was added to {issue_identifier}."
        )

    json.dump(format_comment(comment), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
