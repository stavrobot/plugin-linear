#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""List the workspace's projects with status, lead, target date and progress.

linear.py's shared metadata query only requests id/name for projects, because
it runs on every tool call and exists purely for name resolution, so the
richer fields the assistant needs come from a query local to this tool.
Config loading, the time budget and the GraphQL POST helper still come from
linear.py, so auth and error handling are identical to the other tools.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError

# This query is deliberately separate from linear.py's _METADATA_QUERY and
# must stay that way. _METADATA_QUERY runs on EVERY tool call just to resolve
# names, so folding these fields into it would make every call pay for them.
# Worse, nesting externalLinks(first: 50) inside projects(first: 50) multiplies
# the nested connections and inflates the query against Linear's hard
# complexity cap of 10000. Only list_projects needs status, lead, target date,
# progress and external links, so only list_projects asks for them. Nested
# connections default to 50, and the explicit first args keep this query well
# inside the cap.
PROJECTS_QUERY = """
query {
  projects(first: 50) {
    nodes {
      id
      name
      status { name }
      lead { name displayName }
      targetDate
      progress
      externalLinks(first: 50) { nodes { url label } }
    }
    pageInfo { hasNextPage }
  }
}
"""


def _compact(item: dict) -> dict:
    """Drop null fields; this output feeds an LLM context."""
    return {key: value for key, value in item.items() if value is not None}


def _user_name(user) -> str | None:
    if not user:
        return None
    return user.get("name") or user.get("displayName")


def _external_links(project: dict) -> list[dict]:
    connection = project.get("externalLinks")
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    if not isinstance(nodes, list):
        return []
    return [
        _compact({"url": link.get("url"), "label": link.get("label")})
        for link in nodes
        if isinstance(link, dict) and link.get("url")
    ]


def _format_project(project: dict) -> dict:
    """Reduce a Project node to the compact shape list_projects returns."""
    status = project.get("status") or {}
    formatted = {
        "id": project.get("id"),
        "name": project.get("name"),
        "status": status.get("name"),
        "lead": _user_name(project.get("lead")),
        "target_date": project.get("targetDate"),
        "progress": project.get("progress"),
    }
    links = _external_links(project)
    if links:
        formatted["links"] = links
    return _compact(formatted)


def main() -> None:
    params = json.load(sys.stdin)
    if params:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(params.keys()))}")

    config = linear.load_config()
    deadline = linear.api_deadline()
    data = linear.graphql_request(
        PROJECTS_QUERY, api_key=config["api_key"], deadline=deadline
    )

    connection = data.get("projects") or {}
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    nodes = nodes if isinstance(nodes, list) else []
    page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
    truncated = bool(isinstance(page_info, dict) and page_info.get("hasNextPage"))

    result = {
        "projects": [
            _format_project(project) for project in nodes if isinstance(project, dict)
        ],
        "truncated": truncated,
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
