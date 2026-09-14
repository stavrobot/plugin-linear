"""Attach an external link to a Linear project.

Both create_project's optional 'links' parameter and the add_project_link
tool create project links through the same entityExternalLinkCreate mutation,
so the mutation and its argument rules are defined once here. This module
lives at the plugin root next to linear.py, the same convention the rest of
the shared code uses, and both tools import it from there.

The mutation requires BOTH url and label (both are String! in the schema),
plus exactly one target id, here projectId. A caller that supplies only a URL
therefore gets the URL's hostname as the label, which is what the spike
settled on: an invented "url|label" micro-syntax would be mis-typed.
"""

import urllib.parse

import linear


_CREATE_PROJECT_LINK_MUTATION = """
mutation CreateProjectLink($input: EntityExternalLinkCreateInput!) {
  entityExternalLinkCreate(input: $input) {
    success
    entityExternalLink { id url label project { id name } }
  }
}
"""


def default_label(url: str) -> str:
    """Derive a link label from a URL, falling back to the raw URL."""
    return urllib.parse.urlsplit(url).hostname or url


def validate_url(url) -> str:
    """Return a trimmed http(s) URL, or fail loudly on anything else.

    Rejecting junk here keeps it away from the API, where it would come back
    as an opaque GraphQL error rather than a fixable instruction.
    """
    text = str(url or "").strip()
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as err:
        raise linear.ToolError(
            f"'{url}' is not a valid link. Supply a full http:// or https:// URL."
        ) from err
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise linear.ToolError(
            f"'{url}' is not a valid link. Supply a full http:// or https:// URL."
        )
    return text


def attach_project_link(
    project_id: str, url, label, *, api_key: str, deadline: float
) -> dict:
    """Create one external link on a project and return its compact shape.

    The API demands a non-empty label, so an omitted one is replaced by the
    URL hostname rather than sent as null or empty.
    """
    text = validate_url(url)
    display_label = str(label or "").strip() or default_label(text)
    data = linear.graphql_request(
        _CREATE_PROJECT_LINK_MUTATION,
        {"input": {"projectId": project_id, "url": text, "label": display_label}},
        api_key=api_key,
        deadline=deadline,
    )
    payload = data.get("entityExternalLinkCreate") or {}
    link = payload.get("entityExternalLink")
    if not payload.get("success") or not isinstance(link, dict) or not link.get("id"):
        raise linear.ToolError("Linear did not confirm the project link was created.")
    return {
        "id": link.get("id"),
        "url": link.get("url") or text,
        "label": link.get("label") or display_label,
    }
