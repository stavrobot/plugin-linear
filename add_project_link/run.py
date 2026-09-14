#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""Attach an external URL to an existing Linear project."""

import json
import pathlib
import sys

TOOL_DIR = pathlib.Path(__file__).resolve().parent
# linear.py and project_link.py are both shared modules at the plugin root,
# which both importing tools put on sys.path.
sys.path.insert(0, str(TOOL_DIR.parent))

import linear  # noqa: E402
import project_link  # noqa: E402
from linear import ToolError  # noqa: E402


def main() -> None:
    params = json.load(sys.stdin)

    project_name = str(params.get("project") or "").strip()
    if not project_name:
        raise ToolError("Missing required parameter: project")
    url = str(params.get("url") or "").strip()
    if not url:
        raise ToolError("Missing required parameter: url")
    # Reject junk before spending a metadata round trip; the shared helper
    # validates again when it builds the mutation.
    url = project_link.validate_url(url)
    label = str(params.get("label") or "").strip() or None

    config = linear.load_config()
    api_key = str(config["api_key"]).strip()
    deadline = linear.api_deadline()
    metadata = linear.fetch_metadata(api_key, deadline)

    project_id = linear.resolve_project(
        project_name,
        metadata["projects"],
        truncated=metadata["truncated"]["projects"],
    )
    resolved = next(
        (item for item in metadata["projects"] if item.get("id") == project_id), {}
    )

    link = project_link.attach_project_link(
        project_id, url, label, api_key=api_key, deadline=deadline
    )
    json.dump(
        {
            "project": {"id": project_id, "name": resolved.get("name") or project_name},
            "link": link,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
