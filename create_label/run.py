#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""Create a Linear issue label.

The label is created at the workspace level by omitting teamId, which the
spike confirmed is supported; teamId is optional and the workspace-level
label is available to every team.
"""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear  # noqa: E402
from linear import ToolError  # noqa: E402

_CREATE_LABEL_MUTATION = """
mutation CreateLabel($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) {
    success
    issueLabel { id name color description }
  }
}
"""

_HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_color(value):
    """Return a Linear hex color, or fail loudly on anything else."""
    text = str(value or "").strip()
    if not text:
        return None
    # Accept a bare six-digit hex and add the leading '#', matching the
    # example the tool documents. Anything else must already be '#rrggbb'.
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        text = f"#{text}"
    if not _HEX_COLOR_PATTERN.match(text):
        raise ToolError(
            f"'{value}' is not a valid label color. Use a hex value such as '#ff0000'."
        )
    return text


def create_label(name, description, color, *, api_key, deadline):
    """Create the label and return its GraphQL node."""
    create_input = {"name": name}
    # teamId is deliberately omitted: the label is workspace-level.
    if description:
        create_input["description"] = description
    if color:
        create_input["color"] = color

    data = linear.graphql_request(
        _CREATE_LABEL_MUTATION,
        {"input": create_input},
        api_key=api_key,
        deadline=deadline,
    )
    payload = data.get("issueLabelCreate") or {}
    label = payload.get("issueLabel")
    if not payload.get("success") or not isinstance(label, dict) or not label.get("id"):
        raise ToolError("Linear did not confirm the label was created.")
    return label


def main() -> None:
    params = json.load(sys.stdin)

    name = str(params.get("name") or "").strip()
    if not name:
        raise ToolError("Missing required parameter: name")
    description = str(params.get("description") or "").strip()
    color = normalize_color(params.get("color"))

    config = linear.load_config()
    deadline = linear.api_deadline()
    label = create_label(
        name,
        description,
        color,
        api_key=str(config["api_key"]).strip(),
        deadline=deadline,
    )
    json.dump(
        {
            "label": {
                "id": label.get("id"),
                "name": label.get("name"),
                "color": label.get("color"),
                "description": label.get("description"),
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
