#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

import json
import pathlib
import sys

# The shared root module holds config loading, the GraphQL helper, the name
# resolvers and the issue formatter. It lives at the plugin root so every
# tool adopts it the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError

# issueCreate is the one mutation that cannot take a human issue identifier:
# it needs the team UUID, which comes from the shared metadata fetch. The
# single team is not a parameter because the workspace has exactly one.
CREATE_ISSUE_MUTATION = """
mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      %(fields)s
    }
  }
}
""" % {"fields": linear.ISSUE_FIELDS}

KNOWN_PARAMS = {
    "title",
    "description",
    "assignee",
    "priority",
    "project",
    "labels",
    "due_date",
}


def resolve_label_ids(names, metadata) -> list[str]:
    """Resolve comma-separated label names to UUIDs, de-duplicated in order.

    A new issue has nothing to lose, so duplicate names are collapsed rather
    than treated as an error; an unresolvable name still fails loudly inside
    resolve_label.
    """
    label_ids = []
    for name in names:
        label_id = linear.resolve_label(
            name, metadata["labels"], truncated=metadata["truncated"]["labels"]
        )
        if label_id not in label_ids:
            label_ids.append(label_id)
    return label_ids


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolError("Missing required parameter: title")

    config = linear.load_config()
    api_key = str(config["api_key"])
    deadline = linear.api_deadline()
    metadata = linear.fetch_metadata(api_key, deadline)

    issue_input = {"teamId": metadata["team"]["id"], "title": title}

    if "description" in params:
        issue_input["description"] = str(params["description"])
    if "assignee" in params:
        issue_input["assigneeId"] = linear.resolve_assignee(
            params["assignee"],
            metadata["members"],
            metadata["viewer"],
            truncated=metadata["truncated"]["members"],
        )
    if "priority" in params:
        issue_input["priority"] = linear.resolve_priority(params["priority"])
    if "project" in params:
        issue_input["projectId"] = linear.resolve_project(
            params["project"],
            metadata["projects"],
            truncated=metadata["truncated"]["projects"],
        )
    if "labels" in params:
        issue_input["labelIds"] = resolve_label_ids(
            linear.parse_csv_list(params["labels"]), metadata
        )
    if "due_date" in params:
        issue_input["dueDate"] = linear.validate_due_date(params["due_date"])

    data = linear.graphql_request(
        CREATE_ISSUE_MUTATION,
        {"input": issue_input},
        api_key=api_key,
        deadline=deadline,
    )

    payload = data.get("issueCreate")
    issue = payload.get("issue") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or not payload.get("success")
        or not isinstance(issue, dict)
    ):
        # A falsy success means the mutation was accepted but rejected the
        # input; without an issue node there is nothing to confirm, so this
        # must not be reported as created.
        raise ToolError("Linear did not confirm that the issue was created.")

    json.dump(linear.format_issue(issue), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
