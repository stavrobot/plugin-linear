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

# issueDelete is a soft delete by default: it moves the issue to the trash,
# where Linear keeps it restorable for about 30 days. The mutation also accepts
# permanentlyDelete, but this tool never sends it, so every call here is
# recoverable. The mutation selects entity because IssueArchivePayload is the
# documented return type and entity carries the trashed issue's identifier and
# title (verified against Linear's published schema).
DELETE_ISSUE_MUTATION = """
mutation($id: String!) {
  issueDelete(id: $id) {
    success
    entity {
      identifier
      title
    }
  }
}
"""

KNOWN_PARAMS = {"issue"}

TRASH_NOTE = (
    "The issue is in the trash. It can be restored in Linear for about 30 days."
)


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    if "issue" not in params:
        raise ToolError("Missing required parameter: issue")
    issue_identifier = linear.resolve_issue_identifier(params["issue"])

    config = linear.load_config()
    api_key = str(config["api_key"])
    deadline = linear.api_deadline()

    data = linear.graphql_request(
        DELETE_ISSUE_MUTATION,
        {"id": issue_identifier},
        api_key=api_key,
        deadline=deadline,
    )

    payload = data.get("issueDelete")
    if not isinstance(payload, dict) or not payload.get("success"):
        raise ToolError(
            f"Linear did not confirm that issue {issue_identifier} was deleted."
        )

    # IssueArchivePayload.entity is nullable in the schema (it is null for a
    # permanent delete, which this tool never performs), so fall back to the
    # identifier we were given if Linear omits it.
    entity = payload.get("entity")
    if not isinstance(entity, dict):
        entity = {}

    json.dump(
        {
            "identifier": entity.get("identifier") or issue_identifier,
            "title": entity.get("title"),
            "note": TRASH_NOTE,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
