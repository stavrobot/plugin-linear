#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""List the workspace's issue labels.

A label may belong to a group; linear.py's formatter exposes that as the
'parent' name (and marks the group itself with is_group). Workspaces without
groups simply have no parent on any label, which needs no special case.
"""

import json
import pathlib
import sys

# The shared root module holds config loading, the GraphQL helper, the
# combined metadata fetch and the label formatter. It lives at the plugin
# root so every tool resolves configuration and HTTP the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError


def main() -> None:
    params = json.load(sys.stdin)
    if params:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(params.keys()))}")

    config = linear.load_config()
    deadline = linear.api_deadline()
    metadata = linear.fetch_metadata(config["api_key"], deadline)

    result = {
        "labels": [linear.format_label(label) for label in metadata["labels"]],
        "truncated": metadata["truncated"]["labels"],
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
