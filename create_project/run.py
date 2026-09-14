#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""Create a Linear project, optionally attaching external links.

Links cannot be set in the projectCreate input, so the project is created
first and each link is attached afterwards through entityExternalLinkCreate.
That makes partial success the normal path when links are supplied: if a link
fails the project still exists. Reporting the failure as a whole-call error
would make the caller retry and create a duplicate project, so instead the
result states that the project was created and lists the links that failed.
"""

import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parent
PLUGIN_ROOT = TOOL_DIR.parent
# linear.py and project_link.py are both shared modules at the plugin root,
# which both importing tools put on sys.path.
sys.path.insert(0, str(PLUGIN_ROOT))

import linear  # noqa: E402
import project_link  # noqa: E402
from linear import ToolError  # noqa: E402

_CREATE_PROJECT_MUTATION = """
mutation CreateProject($input: ProjectCreateInput!) {
  projectCreate(input: $input) {
    success
    project {
      id
      name
      identifier
      url
      targetDate
      lead { id name }
    }
  }
}
"""


def create_project(
    name, description, lead_id, target_date, team_id, *, api_key, deadline
):
    """Create the project and return its GraphQL node."""
    create_input = {"name": name, "teamIds": [team_id]}
    if description:
        create_input["description"] = description
    if lead_id:
        create_input["leadId"] = lead_id
    if target_date:
        create_input["targetDate"] = target_date

    data = linear.graphql_request(
        _CREATE_PROJECT_MUTATION,
        {"input": create_input},
        api_key=api_key,
        deadline=deadline,
    )
    payload = data.get("projectCreate") or {}
    project = payload.get("project")
    if (
        not payload.get("success")
        or not isinstance(project, dict)
        or not project.get("id")
    ):
        raise ToolError("Linear did not confirm the project was created.")
    return project


def format_created_project(project: dict) -> dict:
    """Reduce a created project node to the compact shape tools return."""
    lead = project.get("lead") or {}
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "identifier": project.get("identifier"),
        "url": project.get("url"),
        "lead": lead.get("name"),
        "target_date": project.get("targetDate"),
    }


def main() -> None:
    params = json.load(sys.stdin)

    name = str(params.get("name") or "").strip()
    if not name:
        raise ToolError("Missing required parameter: name")
    description = str(params.get("description") or "").strip()
    # An absent target_date means none; a supplied one, even blank, goes
    # through the shared validator, which fails loudly on an empty value.
    # linear.validate_due_date owns the shape and calendar checks.
    target_date = params.get("target_date")
    if target_date is not None:
        target_date = linear.validate_due_date(target_date)
    links = linear.parse_csv_list(params.get("links"))

    config = linear.load_config()
    api_key = str(config["api_key"]).strip()
    deadline = linear.api_deadline()
    metadata = linear.fetch_metadata(api_key, deadline)

    # An absent lead means no lead; a supplied one, even blank, routes through
    # the resolver, which fails loudly on an empty name.
    lead_id = None
    lead = params.get("lead")
    if lead is not None:
        lead_id = linear.resolve_assignee(
            lead,
            metadata["members"],
            metadata["viewer"],
            truncated=metadata["truncated"]["members"],
        )

    project = create_project(
        name,
        description,
        lead_id,
        target_date,
        metadata["team"]["id"],
        api_key=api_key,
        deadline=deadline,
    )

    attached = []
    failed = []
    for url in links:
        try:
            attached.append(
                project_link.attach_project_link(
                    project["id"], url, None, api_key=api_key, deadline=deadline
                )
            )
        except ToolError as err:
            # A link failure must never take the project down with it: the
            # project already exists, and a raised error here would suggest
            # that retrying the whole call is the right move.
            #
            # This error goes to stdout and can reach the assistant, so it is
            # redacted here as well as at the source in graphql_request.
            failed.append(
                {"url": url, "error": linear.redact_secret(str(err), api_key)}
            )

    result = {
        "project": format_created_project(project),
        "links": attached,
        "links_failed": failed,
    }
    if failed:
        details = "; ".join(f"{item['url']} ({item['error']})" for item in failed)
        result["warning"] = (
            f"The project was created, but {len(failed)} link(s) could not be "
            f"attached: {details}. Do not create the project again."
        )
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
