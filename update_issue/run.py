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

# IssueUpdateInput has native label delta fields: addedLabelIds and
# removedLabelIds. They are applied server-side against the issue's current
# labels, so an add cannot clobber labels this call did not mention. That is
# both less code than reading the labels and re-sending the full labelIds set,
# and it removes the read-then-write race where a concurrent label change was
# silently overwritten. (labelIds still exists and still REPLACES the whole
# set; this tool no longer uses it.)
UPDATE_ISSUE_MUTATION = """
mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue {
      %(fields)s
    }
  }
}
""" % {"fields": linear.ISSUE_FIELDS}

KNOWN_PARAMS = {
    "issue",
    "title",
    "description",
    "state",
    "assignee",
    "priority",
    "project",
    "due_date",
    "add_labels",
    "remove_labels",
}

NO_CHANGES_MESSAGE = (
    "No changes were requested. Pass at least one of: title, description, "
    "state, assignee, priority, project, due_date, add_labels, remove_labels."
)


def _unique(ids: list[str]) -> list[str]:
    """Keep the first occurrence of each id, preserving order."""
    unique = []
    for label_id in ids:
        if label_id not in unique:
            unique.append(label_id)
    return unique


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    if "issue" not in params:
        raise ToolError("Missing required parameter: issue")
    issue_identifier = linear.resolve_issue_identifier(params["issue"])

    # An update with nothing to change is a caller error, not a no-op write.
    # Key presence, not an effective value, decides this: description="" is a
    # deliberate clear, while add_labels="" changes nothing and is caught
    # below once the mutation input turns out empty.
    if set(params.keys()) - {"issue"} == set():
        raise ToolError(NO_CHANGES_MESSAGE)

    add_names = linear.parse_csv_list(params.get("add_labels"))
    remove_names = linear.parse_csv_list(params.get("remove_labels"))
    needs_labels = bool(add_names or remove_names)

    config = linear.load_config()
    api_key = str(config["api_key"])
    deadline = linear.api_deadline()

    # Metadata is only fetched when a name has to be resolved against it.
    needs_metadata = needs_labels or any(
        key in params for key in ("state", "assignee", "project")
    )
    metadata = linear.fetch_metadata(api_key, deadline) if needs_metadata else None

    issue_input = {}

    if "title" in params:
        title = str(params["title"])
        if not title.strip():
            raise ToolError("The new title is empty; a title cannot be cleared.")
        issue_input["title"] = title
    if "description" in params:
        issue_input["description"] = str(params["description"])
    if "state" in params:
        issue_input["stateId"] = linear.resolve_state(
            params["state"],
            metadata["states"],
            truncated=metadata["truncated"]["states"],
        )
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
    if "due_date" in params:
        issue_input["dueDate"] = linear.validate_due_date(params["due_date"])

    if needs_labels:
        # Send only the delta. Linear applies it against the issue's current
        # labels, so labels not named here are untouched and a concurrent
        # change is not overwritten. Unresolvable names still fail loudly in
        # resolve_label before the mutation runs, and duplicates collapse.
        add_ids = _unique(
            [
                linear.resolve_label(
                    name,
                    metadata["labels"],
                    truncated=metadata["truncated"]["labels"],
                )
                for name in add_names
            ]
        )
        remove_ids = _unique(
            [
                linear.resolve_label(
                    name,
                    metadata["labels"],
                    truncated=metadata["truncated"]["labels"],
                )
                for name in remove_names
            ]
        )
        if add_ids:
            issue_input["addedLabelIds"] = add_ids
        if remove_ids:
            issue_input["removedLabelIds"] = remove_ids

    if not issue_input:
        raise ToolError(NO_CHANGES_MESSAGE)

    data = linear.graphql_request(
        UPDATE_ISSUE_MUTATION,
        {"id": issue_identifier, "input": issue_input},
        api_key=api_key,
        deadline=deadline,
    )

    payload = data.get("issueUpdate")
    issue = payload.get("issue") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or not payload.get("success")
        or not isinstance(issue, dict)
    ):
        raise ToolError(
            f"Linear did not confirm that issue {issue_identifier} was updated."
        )

    json.dump(linear.format_issue(issue), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
