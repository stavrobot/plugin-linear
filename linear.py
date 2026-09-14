"""Shared internals for the Linear plugin tools.

Linear exposes a single GraphQL endpoint, so every tool needs the same
handful of things regardless of what it does with issues: an API key loaded
from config.json, a POST helper that fails loudly on transport errors, HTTP
errors and GraphQL error arrays, one combined metadata fetch, and the
name-to-UUID resolvers that turn human input into the IDs Linear's
mutations actually want.

Metadata is fetched fresh on every tool call and never cached. A cache
would save a round trip nobody notices and add a stale-data failure mode
that produces confusing errors after a state rename or a new project. That
tradeoff is deliberate.

Wherever a caller supplies a name, resolution follows the same rule as
resolve_carrier() in the 17track plugin: exact case-insensitive match
first, then a unique case-insensitive substring match, then a loud failure
listing the valid candidates. A confidently wrong name must never be
silently ignored.

There is exactly one team in the workspace this plugin targets, so no tool
takes a team parameter and fetch_metadata() exposes that team's UUID for
the one call that needs it (issueCreate). Issue identifiers such as
"ENG-123" are accepted directly by Linear on every path except issue
creation, so resolve_issue_identifier() only normalizes and validates them;
it does not perform a lookup.

Tools import this module by inserting the plugin root on sys.path, the same
way they locate ../config.json:

    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    import linear
"""

import json
import pathlib
import re
import time
from datetime import date

import requests

ROOT = pathlib.Path(__file__).resolve().parent

GRAPHQL_URL = "https://api.linear.app/graphql"

# Both tools and this module must fit the runner's hard ~30s kill. requests
# timeouts are per-read, not wall clock, so a single request can consume its
# entire remaining budget. Every request therefore derives its timeout from
# the time left under one overall deadline that the calling tool starts once
# and threads through fetch_metadata() and graphql_request().
API_PATH_DEADLINE_SECONDS = 20

# Linear caps GraphQL query complexity at 10000. Nested connections default
# to 50 each and multiply, so the combined metadata query is rejected without
# explicit first args. Verified against the live API: teams(first: 15)
# passes, teams(first: 20) fails at complexity 11547. These values are the
# largest that fit.
TEAMS_PAGE_SIZE = 10
NESTED_PAGE_SIZE = 50

# The per-team workflow state connection is called 'states'; 'workflowStates'
# exists only as a top-level Query field. State types are backlog, unstarted,
# started, completed, canceled and duplicate ("canceled" has one l).
#
# The projects connection here exists only for name resolution by the tools
# that write, so it deliberately fetches id and name and nothing else. This
# query runs on EVERY tool call, and nesting a link connection inside
# projects(first: 50) just to serve list_projects would multiply the nested
# connections and inflate query complexity against Linear's hard cap of 10000.
# list_projects needs status, lead, target date, progress and external links,
# so it keeps its own richer query and formatter instead. Do not
# "consolidate" the two: that would make every call pay for fields only one
# tool wants. ('state' was dropped here because it is Linear's deprecated
# project slug, not ProjectStatus.name, and no resolver reads it.)
_METADATA_QUERY = """
query {
  viewer { id name }
  users(first: %(nested)d) {
    nodes { id name displayName email active }
    pageInfo { hasNextPage }
  }
  teams(first: %(teams)d) {
    nodes {
      id key name
      states(first: %(nested)d) {
        nodes { id name type color position }
        pageInfo { hasNextPage }
      }
      labels(first: %(nested)d) {
        nodes { id name color description isGroup parent { id name } }
        pageInfo { hasNextPage }
      }
      projects(first: %(nested)d) {
        nodes { id name }
        pageInfo { hasNextPage }
      }
      members(first: %(nested)d) {
        nodes { id name displayName email }
        pageInfo { hasNextPage }
      }
    }
  }
}
""" % {"teams": TEAMS_PAGE_SIZE, "nested": NESTED_PAGE_SIZE}

# Priority is a Float in the schema, not an enum. The schema description
# confirms 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low. The accepted
# words are deliberately the ones an assistant can spell from the schema's
# own priorityLabel strings.
PRIORITY_VALUES = {
    "none": 0,
    "urgent": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}

# Team keys are alphanumeric and issue numbers are decimal: ENG-123, OPS2-45.
_ISSUE_IDENTIFIER_PATTERN = re.compile(r"^[A-Z0-9]+-\d+$")

# Linear's TimelessDate is a calendar date with no time or zone, so the shape
# check is deliberately stricter than date.fromisoformat(), which also accepts
# the compact YYYYMMDD (and, on newer Pythons, other ISO forms).
_DUE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Expected issue shape for format_issue(); tools embed this in their queries
# so the responder and the formatter cannot drift apart.
ISSUE_FIELDS = """
  id
  identifier
  title
  url
  priority
  priorityLabel
  createdAt
  updatedAt
  state { id name type }
  assignee { id name displayName }
  labels { nodes { id name color } }
  project { id name }
"""

# Cap on the number of candidates named in a resolution failure. There can be
# hundreds of labels; the caller only needs enough to recognise the mistake.
_MAX_CANDIDATES = 20


class ToolError(Exception):
    """Fatal, user-facing error whose message is safe to print."""


def load_config() -> dict:
    try:
        with (ROOT / "config.json").open() as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as err:
        raise ToolError(f"Could not read config.json: {err}") from err
    if not isinstance(config, dict):
        raise ToolError("config.json must contain a JSON object.")
    api_key = config.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ToolError(
            "No Linear API key found in config.json. Set api_key to a Linear "
            "personal API key (Linear Settings > Security and access > "
            "Personal API keys)."
        )
    return config


# Known and accepted bound: the timeout handed to requests is per-read, not a
# total wall-clock deadline, so a pathological server trickling bytes could
# overshoot the runner's ~30s kill. The only trigger is a pathological server
# and the consequence is a runner timeout rather than a wrong answer, so this
# is accepted rather than overlooked. Do not "fix" it blindly; it needs a
# real wall-clock mechanism, not a smaller timeout.
def api_deadline() -> float:
    """Start the overall time budget for one tool call."""
    return time.monotonic() + API_PATH_DEADLINE_SECONDS


def api_timeout_remaining(deadline: float) -> float:
    """Per-request timeout derived from the time left in the budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError(
            "The Linear API calls exceeded the "
            f"{API_PATH_DEADLINE_SECONDS}-second tool budget and were "
            "aborted. Try again shortly."
        )
    return remaining


# Substituted for the API key when it appears in text derived from an API
# response. A response or a GraphQL error message can echo the Authorization
# header, and any such text must be scrubbed before it reaches stderr (or,
# in create_project's partial-success path, stdout and the assistant).
_REDACTED = "[redacted]"


def redact_secret(text: str, secret: str) -> str:
    """Replace every occurrence of a secret with a placeholder.

    Response bodies and GraphQL error messages are remote and can echo the
    Authorization header, so they are treated as unsafe text. Redaction runs
    on the raw string before it is whitespace-normalized or truncated:
    normalizing first would split a key containing whitespace so a plain
    replace no longer matched it, and truncating first could leave a partial
    key behind.
    """
    if not secret:
        return text
    return text.replace(secret, _REDACTED)


def _body_snippet(response, api_key: str) -> str:
    """A short, single-line excerpt of a failed response for error messages."""
    redacted = redact_secret(response.text or "", api_key)
    text = " ".join(redacted.split())
    return text[:300] if text else "(empty response body)"


def _graphql_error_message(errors, api_key: str) -> str:
    messages = []
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, dict) and error.get("message"):
            messages.append(str(error["message"]))
        elif error:
            messages.append(str(error))
    joined = "; ".join(messages) if messages else "unknown error"
    return redact_secret(joined, api_key)


def _transport_error_message(err: requests.exceptions.RequestException) -> str:
    """Classify a transport failure without echoing the exception text.

    requests exceptions can embed the request, and an API key with an embedded
    newline makes requests raise InvalidHeader with the whole key in the
    message. That text is unsafe to print, so only a fixed classification is
    returned.
    """
    if isinstance(err, requests.exceptions.Timeout):
        detail = "the request timed out"
    elif isinstance(err, requests.exceptions.ConnectionError):
        detail = "the connection failed"
    else:
        detail = "the request could not be completed"
    return f"Could not reach the Linear API: {detail}."


def graphql_request(
    query: str,
    variables: dict | None = None,
    *,
    api_key: str,
    deadline: float,
) -> dict:
    """POST a GraphQL query and return its 'data' object.

    Every failure mode raises ToolError: transport failure, a non-200 status,
    an unparseable body, and a 200 response that carries a GraphQL 'errors'
    array. The last one is a failure, not a success, even though Linear can
    technically return partial data alongside errors.

    The Authorization header carries the bare API key with no 'Bearer'
    prefix, which is how Linear personal API keys are sent.
    """
    payload = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    # The Authorization header carries the stripped key, so that exact value
    # is what a response could echo; redact response-derived text against it.
    secret = api_key.strip()
    try:
        response = requests.post(
            GRAPHQL_URL,
            headers={
                "Authorization": secret,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=api_timeout_remaining(deadline),
        )
    except requests.exceptions.RequestException as err:
        # from None drops the chain on purpose: the original exception can
        # carry the Authorization header, and a chained traceback would print
        # it. Only the fixed classification is safe to surface.
        raise ToolError(_transport_error_message(err)) from None

    if response.status_code != 200:
        raise ToolError(
            f"The Linear API returned HTTP {response.status_code}: "
            f"{_body_snippet(response, secret)}"
        )

    try:
        body = response.json()
    except ValueError as err:
        raise ToolError(
            "The Linear API returned a response that was not valid JSON."
        ) from err

    if not isinstance(body, dict):
        raise ToolError("The Linear API returned an unexpected response.")

    errors = body.get("errors")
    if errors:
        raise ToolError(
            f"Linear reported an error: {_graphql_error_message(errors, secret)}"
        )

    data = body.get("data")
    if not isinstance(data, dict):
        raise ToolError("The Linear API response contained no data.")
    return data


def _nodes(connection) -> list:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    return nodes if isinstance(nodes, list) else []


def _has_next_page(connection) -> bool:
    if not isinstance(connection, dict):
        return False
    page_info = connection.get("pageInfo")
    return bool(isinstance(page_info, dict) and page_info.get("hasNextPage"))


def fetch_metadata(api_key: str, deadline: float) -> dict:
    """Fetch viewer, users, team, states, labels, projects and members once.

    The whole workspace is described by a single GraphQL request. The
    'truncated' mapping records, per collection, whether the page cap was hit,
    so a name that exists in Linear may be missing here. Resolvers take the
    flag for the collection they search and refuse to make a definitive claim
    about an incomplete list. Keeping this per collection stops an overflowing
    project list from affecting state or label resolution.
    """
    data = graphql_request(_METADATA_QUERY, api_key=api_key, deadline=deadline)

    teams = _nodes(data.get("teams"))
    if not teams:
        raise ToolError(
            "No Linear team was found for this account. The plugin needs one "
            "team to work with; create one in Linear and retry."
        )
    team = teams[0]

    connections = {
        "users": data.get("users"),
        "states": team.get("states"),
        "labels": team.get("labels"),
        "projects": team.get("projects"),
        "members": team.get("members"),
    }
    truncated = {
        name: _has_next_page(connection) for name, connection in connections.items()
    }

    return {
        "viewer": data.get("viewer") or {},
        "team": {
            "id": team.get("id"),
            "key": team.get("key"),
            "name": team.get("name"),
        },
        "states": _nodes(connections["states"]),
        "labels": _nodes(connections["labels"]),
        "projects": _nodes(connections["projects"]),
        "members": _nodes(connections["members"]),
        "users": _nodes(connections["users"]),
        "truncated": truncated,
    }


def _candidate_name(candidate: dict) -> str:
    for field in ("name", "displayName"):
        value = candidate.get(field)
        if value:
            return str(value)
    return str(candidate.get("id", "?"))


def _name_list(candidates: list, display) -> str:
    names = sorted({display(candidate) for candidate in candidates})
    return ", ".join(names[:_MAX_CANDIDATES]) if names else "(none)"


def _resolve_named(
    query,
    candidates: list,
    kind: str,
    fields: tuple,
    *,
    truncated: bool,
) -> str:
    """Resolve a human name to a candidate UUID, or fail loudly.

    Exact case-insensitive match wins, and is trusted even when the collection
    is truncated: a match on the whole name is unambiguous. Otherwise a
    case-insensitive substring match wins only when it is unique and the
    collection is complete. On a truncated collection a substring match is not
    trusted and a miss is not a fact, because the full list was not seen.

    'truncated' is required and keyword-only on purpose: a caller must state
    whether the collection it read was complete, so forgetting it cannot
    silently restore the unsound behaviour.
    """
    text = str(query or "").strip()
    if not text:
        raise ToolError(f"The {kind} name is empty.")
    lowered = text.lower()

    def aliases(candidate):
        return [
            str(candidate[field]).lower() for field in fields if candidate.get(field)
        ]

    exact = [c for c in candidates if any(alias == lowered for alias in aliases(c))]
    if len(exact) == 1:
        # Residual, accepted risk: an exact match is trusted even when the
        # collection is truncated, so a duplicate name living on a later page
        # can make this return the wrong entity. Refusing to resolve on a
        # truncated collection was rejected because it would break resolution
        # entirely for any workspace past the page cap (a certainty), to guard
        # a case that needs >50 of a kind, two entities with identical names,
        # and the wrong one on page one.
        return exact[0]["id"]
    if len(exact) > 1:
        raise ToolError(
            f"The {kind} name '{query}' is ambiguous. Candidates: "
            f"{_name_list(exact, _candidate_name)}. Retry with one of these "
            "exact names."
        )

    hits = [c for c in candidates if any(lowered in alias for alias in aliases(c))]
    if len(hits) > 1:
        raise ToolError(
            f"The {kind} name '{query}' is ambiguous. Candidates: "
            f"{_name_list(hits, _candidate_name)}. Retry with one of these "
            "exact names."
        )
    if len(hits) == 1:
        if truncated:
            raise ToolError(
                f"The {kind} list is incomplete, so '{query}' is only a partial "
                f"match for '{_candidate_name(hits[0])}' and cannot be trusted. "
                f"Retry with the exact {kind} name."
            )
        return hits[0]["id"]

    if truncated:
        raise ToolError(
            f"'{query}' was not found in the first {len(candidates)} {kind}s "
            f"fetched, and the {kind} list is incomplete, so it may exist "
            f"further down. Retry with the exact {kind} name."
        )
    raise ToolError(
        f"No {kind} matches '{query}'. Valid {kind}s: "
        f"{_name_list(candidates, _candidate_name)}."
    )


def resolve_state(state_name, states: list, *, truncated: bool) -> str:
    """Resolve a workflow state name (e.g. 'In Progress') to its UUID."""
    return _resolve_named(state_name, states, "state", ("name",), truncated=truncated)


def resolve_label(label_name, labels: list, *, truncated: bool) -> str:
    """Resolve an issue label name to its UUID."""
    return _resolve_named(label_name, labels, "label", ("name",), truncated=truncated)


def resolve_project(project_name, projects: list, *, truncated: bool) -> str:
    """Resolve a project name to its UUID."""
    return _resolve_named(
        project_name, projects, "project", ("name",), truncated=truncated
    )


def resolve_assignee(
    assignee_name, members: list, viewer: dict, *, truncated: bool
) -> str:
    """Resolve an assignee name, or 'me', to a user UUID.

    Resolution runs against the team's members because only members can be
    assigned. 'me' short-circuits to the viewer and never touches the member
    list, so it works even if the API key's user is not a member.
    """
    if str(assignee_name or "").strip().lower() == "me":
        user_id = (viewer or {}).get("id")
        if not user_id:
            raise ToolError(
                "'me' was requested as the assignee, but the Linear viewer "
                "could not be determined. Retry, or pass an explicit "
                "assignee name."
            )
        return user_id
    return _resolve_named(
        assignee_name,
        members,
        "assignee",
        ("name", "displayName"),
        truncated=truncated,
    )


def resolve_priority(value) -> int:
    """Map a priority word to Linear's integer value (0-4)."""
    text = str(value or "").strip().lower()
    if text in PRIORITY_VALUES:
        return PRIORITY_VALUES[text]
    raise ToolError(
        f"'{value}' is not a recognised priority. Valid priorities: "
        + ", ".join(PRIORITY_VALUES)
        + "."
    )


def resolve_issue_identifier(identifier) -> str:
    """Normalize and validate a human issue identifier (e.g. 'ENG-123').

    Linear accepts the identifier directly on issue, issueUpdate and
    commentCreate, so no lookup to a UUID happens here. This only trims,
    uppercases and shape-checks it so a typo fails before a round trip.
    """
    text = str(identifier or "").strip().upper()
    if not text:
        raise ToolError(
            "The issue identifier is empty. Pass a Linear identifier such as 'ENG-123'."
        )
    if not _ISSUE_IDENTIFIER_PATTERN.match(text):
        raise ToolError(
            f"'{identifier}' is not a valid Linear issue identifier. Expected "
            "a team key and issue number, such as 'ENG-123'."
        )
    return text


def parse_csv_list(value) -> list[str]:
    """Parse a comma-separated string into a trimmed list, dropping empties.

    Tool manifests have no array parameter type, so names that accept several
    values arrive as one comma-separated string. A list is accepted too so
    callers that already have one do not need to join and re-split it.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def validate_due_date(value) -> str:
    """Return an ISO due date string, or fail loudly.

    Shared by the tools that write issue due dates so the shape check and the
    real-calendar-date check have exactly one implementation.
    """
    text = str(value or "").strip()
    if not _DUE_DATE_PATTERN.match(text):
        raise ToolError(
            f"'{value}' is not a valid due date. Expected an ISO date such as "
            "'2026-09-30'."
        )
    try:
        date.fromisoformat(text)
    except ValueError as err:
        raise ToolError(f"'{value}' is not a real calendar date.") from err
    return text


def _user_name(user) -> str | None:
    if not user:
        return None
    return user.get("name") or user.get("displayName")


def format_issue(issue: dict) -> dict:
    """Reduce a GraphQL issue node to the compact shape list tools return.

    Tool output consumes conversation context, so this keeps the fields an
    assistant acts on and drops the rest. The query side uses ISSUE_FIELDS.
    """
    labels = issue.get("labels")
    if isinstance(labels, dict):
        labels = labels.get("nodes")
    labels = labels if isinstance(labels, list) else []
    state = issue.get("state") or {}
    return {
        "id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "state": state.get("name"),
        "state_type": state.get("type"),
        "assignee": _user_name(issue.get("assignee")),
        "priority": issue.get("priority"),
        "priority_label": issue.get("priorityLabel"),
        "labels": [label.get("name") for label in labels if label.get("name")],
        "project": (issue.get("project") or {}).get("name"),
        "url": issue.get("url"),
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
    }


def _compact(item: dict) -> dict:
    """Drop null fields; formatter output feeds an LLM context."""
    return {key: value for key, value in item.items() if value is not None}


def format_state(state: dict) -> dict:
    """Reduce a workflow state node to the shape list_states returns."""
    return _compact(
        {
            "id": state.get("id"),
            "name": state.get("name"),
            "type": state.get("type"),
            "color": state.get("color"),
        }
    )


def format_label(label: dict) -> dict:
    """Reduce an issue label node to the shape list_labels returns."""
    parent = label.get("parent") or {}
    return _compact(
        {
            "id": label.get("id"),
            "name": label.get("name"),
            "color": label.get("color"),
            "description": label.get("description"),
            "is_group": label.get("isGroup"),
            "parent": parent.get("name"),
        }
    )
