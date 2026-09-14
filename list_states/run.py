#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests"]
# ///

"""List the team's workflow states in workflow (position) order.

The state 'type' is the field that matters most: it is how the assistant
knows which states mean work is done (completed), abandoned (canceled) or
merely waiting (backlog, unstarted).
"""

import json
import pathlib
import sys

# The shared root module holds config loading, the GraphQL helper, the
# combined metadata fetch and the state formatter. It lives at the plugin
# root so every tool resolves configuration and HTTP the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import linear
from linear import ToolError


def _position(state: dict) -> float:
    """Sort key that puts states in workflow order.

    Linear's states connection is not returned in position order, so the
    order the assistant needs has to be imposed here from the position field.
    """
    value = state.get("position")
    return value if isinstance(value, (int, float)) else float("inf")


def main() -> None:
    params = json.load(sys.stdin)
    if params:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(params.keys()))}")

    config = linear.load_config()
    deadline = linear.api_deadline()
    metadata = linear.fetch_metadata(config["api_key"], deadline)

    result = {
        "states": [
            linear.format_state(state)
            for state in sorted(metadata["states"], key=_position)
        ],
        "truncated": metadata["truncated"]["states"],
    }
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
